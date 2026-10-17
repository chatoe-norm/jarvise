import { useEffect, useMemo, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { api, type CryptoSuitability, type UniversePayload } from "@/lib/api";
import { copyText } from "@/lib/clipboard";
import { describeIssues } from "@/lib/syncIssues";
import { cn } from "@/lib/utils";

export const POPULAR_CRYPTO_OPTIONS = [
  { symbol: "BTCUSDT", name: "Bitcoin (BTC)" },
  { symbol: "ETHUSDT", name: "Ethereum (ETH)" },
  { symbol: "SOLUSDT", name: "Solana (SOL)" },
  { symbol: "BNBUSDT", name: "BNB (BNB)" },
  { symbol: "XRPUSDT", name: "Ripple (XRP)" },
  { symbol: "DOGEUSDT", name: "Dogecoin (DOGE)" },
  { symbol: "ADAUSDT", name: "Cardano (ADA)" },
  { symbol: "AVAXUSDT", name: "Avalanche (AVAX)" },
  { symbol: "SUIUSDT", name: "Sui (SUI)" },
  { symbol: "LINKUSDT", name: "Chainlink (LINK)" },
  { symbol: "NEARUSDT", name: "NEAR Protocol (NEAR)" },
  { symbol: "DOTUSDT", name: "Polkadot (DOT)" },
  { symbol: "APTUSDT", name: "Aptos (APT)" },
  { symbol: "LTCUSDT", name: "Litecoin (LTC)" },
  { symbol: "PEPEUSDT", name: "Pepe (PEPE)" },
  { symbol: "SHIBUSDT", name: "Shiba Inu (SHIB)" },
  { symbol: "UNIUSDT", name: "Uniswap (UNI)" },
  { symbol: "ICPUSDT", name: "Internet Computer (ICP)" },
  { symbol: "FETUSDT", name: "Artificial Superintelligence (FET)" },
  { symbol: "RENDERUSDT", name: "Render (RENDER)" },
  { symbol: "TAOUSDT", name: "Bittensor (TAO)" },
  { symbol: "AAVEUSDT", name: "Aave (AAVE)" },
  { symbol: "OPUSDT", name: "Optimism (OP)" },
  { symbol: "ARBUSDT", name: "Arbitrum (ARB)" },
  { symbol: "INJUSDT", name: "Injective (INJ)" },
  { symbol: "FILUSDT", name: "Filecoin (FIL)" },
  { symbol: "TIAUSDT", name: "Celestia (TIA)" },
];

export function CryptoUniverseCard() {
  const [data, setData] = useState<UniversePayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Dropdown & Form State
  const [selectedOption, setSelectedOption] = useState<string>("");
  const [customSymbol, setCustomSymbol] = useState<string>("");
  const [actionBusy, setActionBusy] = useState(false);
  const [syncingMarket, setSyncingMarket] = useState(false);
  const [syncingSymbol, setSyncingSymbol] = useState<string | null>(null);
  const [syncingRag, setSyncingRag] = useState(false);
  const [ragSyncResult, setRagSyncResult] = useState<string | null>(null);
  const [confirmRemove, setConfirmRemove] = useState<string | null>(null);
  const [copiedHint, setCopiedHint] = useState(false);
  const [actionResult, setActionResult] = useState<{ ok: boolean; msg: string } | null>(null);
  const { confirm, dialog } = useConfirm();

  // Sorting & Filtering State
  const [sortBy, setSortBy] = useState<string>("suitability_desc");
  const [searchQuery, setSearchQuery] = useState("");
  const [filterTab, setFilterTab] = useState<"all" | "active" | "actionable" | "high_score">("all");

  async function loadUniverse() {
    setLoading(true);
    try {
      const res = await api.universe();
      setData(res);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void loadUniverse();
  }, []);

  const activeSymbols = useMemo(() => new Set(data?.active_symbols || []), [data]);

  const effectiveSymbol = useMemo(() => {
    if (selectedOption === "CUSTOM") {
      return customSymbol.trim().toUpperCase();
    }
    return selectedOption.trim().toUpperCase();
  }, [selectedOption, customSymbol]);

  async function handleAddSymbol(symToAdd?: string) {
    const target = (symToAdd || effectiveSymbol).trim().toUpperCase();
    if (!target) return;
    const ok = await confirm({
      title: `เพิ่ม ${target} เข้า Universe?`,
      body: "ระบบจะ backfill แท่งเทียนย้อนหลังและเริ่มสแกนสัญญาณคู่นี้ในรอบถัดไป",
      confirmLabel: "เพิ่ม",
    });
    if (!ok) return;
    setActionBusy(true);
    setError(null);
    try {
      const res = await api.addUniverseSymbol(target);
      setActionResult(
        res.sync_failed?.length
          ? { ok: false, msg: `เพิ่ม ${target} แล้ว แต่ดึงข้อมูลไม่สำเร็จ: ${describeIssues(res.sync_failed)}` }
          : { ok: true, msg: `เพิ่ม ${target} และดึงข้อมูลเรียบร้อย` },
      );
      setSelectedOption("");
      setCustomSymbol("");
      await loadUniverse();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setActionBusy(false);
    }
  }

  async function handleRemoveSymbol(sym: string) {
    if (confirmRemove !== sym) {
      setConfirmRemove(sym);
      setTimeout(() => {
        setConfirmRemove((curr) => (curr === sym ? null : curr));
      }, 4000);
      return;
    }
    setActionBusy(true);
    setError(null);
    try {
      await api.removeUniverseSymbol(sym);
      setConfirmRemove(null);
      await loadUniverse();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setActionBusy(false);
    }
  }

  async function handleReset(preset: "paper_core" | "paper_liquid") {
    const ok = await confirm({
      title: `รีเซ็ต Universe เป็น ${preset}?`,
      body: `คู่ที่ไม่อยู่ใน ${preset} จะถูกนำออกจาก Universe ตั้งแต่ตอนนี้ (ประวัติย้อนหลังยังเก็บไว้ให้ replay) และคู่ใน preset ที่ขาดอยู่จะถูกเพิ่มกลับ`,
      confirmLabel: "รีเซ็ต",
      tone: "danger",
    });
    if (!ok) return;
    setActionBusy(true);
    setError(null);
    try {
      const res = await api.resetUniverse(preset);
      const parts: string[] = [];
      if (res.removed?.length) parts.push(`นำออก ${res.removed.join(", ")}`);
      if (res.added?.length) parts.push(`เพิ่ม ${res.added.join(", ")}`);
      setActionResult({
        ok: true,
        msg: parts.length ? `รีเซ็ตเป็น ${preset}: ${parts.join(" · ")}` : `Universe ตรงกับ ${preset} อยู่แล้ว`,
      });
      await loadUniverse();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setActionBusy(false);
    }
  }

  async function handleSyncMarket(sym?: string) {
    if (!sym) {
      const ok = await confirm({
        title: "ซิงค์ตลาดทุกคู่ใน Universe?",
        body: "ดึงแท่งเทียนและคำนวณ indicator ใหม่ทุกคู่ อาจใช้เวลาหลายนาที",
        confirmLabel: "ซิงค์ทั้งหมด",
      });
      if (!ok) return;
    }
    if (sym) {
      setSyncingSymbol(sym);
    } else {
      setSyncingMarket(true);
    }
    setError(null);
    try {
      const res = await api.syncUniverse(sym);
      setActionResult(
        res.failed?.length
          ? { ok: false, msg: `ซิงค์สำเร็จ ${res.count} คู่ ไม่สำเร็จ ${res.failed.length}: ${describeIssues(res.failed)}` }
          : { ok: true, msg: `ซิงค์สำเร็จ ${res.count} คู่${res.warnings?.length ? ` (มีคำเตือน ${res.warnings.length})` : ""}` },
      );
      await loadUniverse();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSyncingMarket(false);
      setSyncingSymbol(null);
    }
  }

  async function handleSyncRag() {
    setSyncingRag(true);
    setError(null);
    setRagSyncResult(null);
    try {
      const res = await api.syncRag();
      if (res.ok) {
        setRagSyncResult("ซิงค์กฎการเทรดจาก NotebookLM สำเร็จ (อัปเดตลง Qdrant เรียบร้อย)");
      } else {
        setRagSyncResult(`ซิงค์ไม่สำเร็จ: ${res.error || (res.status_code ? `HTTP ${res.status_code}` : "ดู log ของ jobs")}`);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSyncingRag(false);
    }
  }

  const filteredAndSortedItems = useMemo(() => {
    if (!data?.items) return [];
    let list = [...data.items];

    if (searchQuery.trim()) {
      const q = searchQuery.trim().toUpperCase();
      list = list.filter(
        (i) =>
          i.symbol.includes(q) ||
          i.action.toUpperCase().includes(q) ||
          i.regime_state.toUpperCase().includes(q) ||
          i.label.toUpperCase().includes(q),
      );
    }

    if (filterTab === "active") {
      list = list.filter((i) => i.is_active);
    } else if (filterTab === "actionable") {
      list = list.filter((i) => i.action !== "flat");
    } else if (filterTab === "high_score") {
      list = list.filter((i) => i.score >= 65);
    }

    list.sort((a, b) => {
      switch (sortBy) {
        case "suitability_desc":
          return b.score - a.score;
        case "confidence_desc":
          return b.confidence_score - a.confidence_score;
        case "symbol_asc":
          return a.symbol.localeCompare(b.symbol);
        case "symbol_desc":
          return b.symbol.localeCompare(a.symbol);
        case "action_desc": {
          const rankAction = (act: string) => {
            if (act === "buy" || act === "long") return 3;
            if (act === "sell" || act === "short") return 3;
            if (act === "flat") return 1;
            return 2;
          };
          const diff = rankAction(b.action) - rankAction(a.action);
          return diff !== 0 ? diff : b.score - a.score;
        }
        case "freshness_asc": {
          const ageA = a.health?.newest_age_min ?? 999999;
          const ageB = b.health?.newest_age_min ?? 999999;
          return ageA - ageB;
        }
        case "rows_desc": {
          const rowsA = a.health?.rows ?? 0;
          const rowsB = b.health?.rows ?? 0;
          return rowsB - rowsA;
        }
        case "rsi_asc": {
          const rsiA = a.candle?.rsi_14 ?? 999;
          const rsiB = b.candle?.rsi_14 ?? 999;
          return rsiA - rsiB;
        }
        default:
          return b.score - a.score;
      }
    });

    return list;
  }, [data, searchQuery, filterTab, sortBy]);

  return (
    <Card className="border-[var(--color-border)]">
      {dialog}
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="flex items-center gap-2">
              <CardTitle className="text-base font-bold">
                Crypto Universe &amp; Trade Suitability Ranking
              </CardTitle>
              <Badge variant="ok" className="font-mono text-xs">
                {data?.universe_id || "paper_liquid"} ({activeSymbols.size} active)
              </Badge>
            </div>
            <CardDescription className="mt-1">
              การจัดการรายชื่อเหรียญคริปโตใน Universe, ซิงค์ข้อมูลแท่งเทียนสมบูรณ์ และจัดอันดับสัญญาณเทรด
            </CardDescription>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={loading || actionBusy || syncingMarket}
              onClick={() => void handleSyncMarket()}
              className="border-sky-500/40 text-sky-400 hover:bg-sky-500/10 text-xs font-mono"
              title="ซิงค์ดึง OHLCV ล่าสุดและคำนวณ Indicator/Analysis ทุกเหรียญใน Universe"
            >
              {syncingMarket ? "⏳ กำลังซิงค์ข้อมูล…" : "⚡ ซิงค์ข้อมูลตลาด (Sync)"}
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={loading || actionBusy || syncingMarket}
              onClick={() => void loadUniverse()}
              className="border-[var(--color-border)] text-xs font-mono"
            >
              {loading ? "กำลังโหลด…" : "↻ รีเฟรช"}
            </Button>
            <div className="flex items-center gap-1 border border-[var(--color-border)] rounded-md p-0.5 bg-[var(--color-card)]/40 text-xs">
              <button
                type="button"
                disabled={actionBusy || syncingMarket}
                onClick={() => void handleReset("paper_core")}
                className="px-2 py-1 rounded hover:bg-[#1f2937] text-[var(--color-muted)] hover:text-white transition-colors"
                title="รีเซ็ตเป็นคู่เหรียญเริ่มต้น (BTC, ETH)"
              >
                paper_core (2)
              </button>
              <span className="text-[var(--color-border)]">|</span>
              <button
                type="button"
                disabled={actionBusy || syncingMarket}
                onClick={() => void handleReset("paper_liquid")}
                className="px-2 py-1 rounded hover:bg-[#1f2937] text-[var(--color-muted)] hover:text-white transition-colors"
                title="รีเซ็ตเป็น 4 สกุลหลัก (BTC, ETH, SOL, BNB)"
              >
                paper_liquid (4)
              </button>
            </div>
          </div>
        </div>
      </CardHeader>

      <CardContent className="space-y-5">
        {error ? <p className="text-xs text-[var(--color-danger)] font-mono">{error}</p> : null}
        {actionResult ? (
          <p className={cn("text-xs font-mono", actionResult.ok ? "text-emerald-300" : "text-amber-300")}>
            {actionResult.msg}
          </p>
        ) : null}

        {/* Section 1: Trading rules (NotebookLM) sync card */}
        <div className="rounded-lg border border-sky-500/30 bg-gradient-to-r from-sky-950/30 via-[#101722] to-indigo-950/30 p-4 space-y-3">
          <div className="flex flex-col md:flex-row md:items-center justify-between gap-3">
            <div className="space-y-1">
              <div className="flex items-center gap-2">
                <span className="text-base">📚</span>
                <span className="text-sm font-bold text-white tracking-tight">
                  ซิงค์กฎการเทรด (NotebookLM RAG)
                </span>
                <Badge variant="outline" className="border-sky-500/40 text-sky-400 font-mono text-[10px]">
                  profile: chatoe
                </Badge>
              </div>
              <div className="text-xs text-[var(--color-muted)] flex flex-wrap items-center gap-x-2 gap-y-1">
                <span>บัญชี Google: <strong className="text-slate-300 font-mono">chatoe@gmail.com</strong></span>
                <span>•</span>
                <span>Notebook ID: <strong className="text-slate-300 font-mono">14e11c63-e2ee-4b49-898f-b0cc4c61cb4e</strong></span>
              </div>
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <a
                href="https://notebook.google.com/notebook/14e11c63-e2ee-4b49-898f-b0cc4c61cb4e"
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md border border-[var(--color-border)] bg-[#161d27] hover:bg-[#1f2937] text-xs font-mono text-sky-400 hover:text-sky-300 transition-colors"
              >
                <span>เปิด Gemini NotebookLM</span>
                <span className="text-[10px]">↗</span>
              </a>

              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={syncingRag || actionBusy}
                onClick={() => void handleSyncRag()}
                className="border-indigo-500/50 bg-indigo-500/10 hover:bg-indigo-500/20 text-indigo-300 text-xs font-mono"
              >
                {syncingRag ? "⏳ กำลังซิงค์กฎการเทรด…" : "📚 Sync Gemini LM"}
              </Button>
            </div>
          </div>

          {ragSyncResult ? (
            <div className="text-xs text-emerald-400 font-mono flex items-center gap-1.5 bg-emerald-950/30 border border-emerald-500/30 px-3 py-1.5 rounded">
              <span>✓</span>
              <span>{ragSyncResult}</span>
            </div>
          ) : null}
        </div>

        {/* Section 2: Add Symbol Dropdown List Form */}
        <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-card)]/40 p-4 space-y-3">
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
            <div className="space-y-0.5">
              <div className="text-sm font-semibold text-[var(--color-foreground)]">
                เพิ่มสกุลเงินเข้า Universe (Add Symbol)
              </div>
              <div className="text-xs text-[var(--color-muted)]">
                เลือกเหรียญจาก Dropdown เพื่อดาวน์โหลด OHLCV, คำนวณอินดิเคเตอร์ และคัดกรองสัญญาณอัตโนมัติ
              </div>
            </div>

            <form
              onSubmit={(e) => {
                e.preventDefault();
                void handleAddSymbol();
              }}
              className="flex flex-col sm:flex-row items-stretch sm:items-center gap-2 w-full sm:w-auto"
            >
              <select
                value={selectedOption}
                onChange={(e) => {
                  setSelectedOption(e.target.value);
                  if (e.target.value !== "CUSTOM") {
                    setCustomSymbol("");
                  }
                }}
                disabled={actionBusy}
                className="rounded-md border border-[var(--color-border)] bg-[#101722] px-3 py-1.5 text-xs text-[var(--color-foreground)] font-mono focus:outline-none focus:border-sky-500 cursor-pointer min-w-[240px]"
              >
                <option value="">-- เลือกเหรียญคริปโต (Select Crypto) --</option>
                <optgroup label="🌟 เหรียญยอดนิยม (Top Pairs)">
                  {POPULAR_CRYPTO_OPTIONS.map((opt) => {
                    const inUniverse = activeSymbols.has(opt.symbol);
                    return (
                      <option key={opt.symbol} value={opt.symbol} disabled={inUniverse}>
                        {inUniverse ? `✓ ${opt.symbol} (อยู่ใน Universe แล้ว)` : `${opt.symbol} · ${opt.name}`}
                      </option>
                    );
                  })}
                </optgroup>
                <optgroup label="✍️ อื่นๆ">
                  <option value="CUSTOM">➕ ระบุ Symbol อื่นๆ เอง (Custom)...</option>
                </optgroup>
              </select>

              {selectedOption === "CUSTOM" ? (
                <input
                  type="text"
                  placeholder="เช่น TIAUSDT"
                  value={customSymbol}
                  onChange={(e) => setCustomSymbol(e.target.value.toUpperCase())}
                  disabled={actionBusy}
                  className="w-32 rounded-md border border-[var(--color-border)] bg-[#101722] px-3 py-1.5 text-xs text-[var(--color-foreground)] placeholder:text-[var(--color-muted)] font-mono uppercase focus:outline-none focus:border-sky-500"
                  autoFocus
                />
              ) : null}

              <Button
                type="submit"
                variant="ok"
                size="sm"
                disabled={
                  actionBusy ||
                  !effectiveSymbol ||
                  activeSymbols.has(effectiveSymbol)
                }
                className="whitespace-nowrap text-xs"
              >
                {actionBusy ? "กำลังบันทึก…" : "+ เพิ่มเหรียญ"}
              </Button>
            </form>
          </div>
        </div>

        {/* Section 3: Sorting, Search & Filter Controls */}
        <div className="flex flex-col md:flex-row md:items-center justify-between gap-3 bg-[var(--color-card)]/20 p-3 rounded-lg border border-[var(--color-border)]">
          <div className="flex flex-wrap items-center gap-2 min-w-0">
            <input
              type="text"
              placeholder="ค้นหา Symbol / Regime…"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="w-full sm:w-48 rounded-md border border-[var(--color-border)] bg-[#101722] px-2.5 py-1 text-xs text-[var(--color-foreground)] placeholder:text-[var(--color-muted)] focus:outline-none focus:border-sky-500 font-mono"
            />

            <div className="flex items-center gap-1 text-xs">
              <button
                type="button"
                onClick={() => setFilterTab("all")}
                className={cn(
                  "px-2.5 py-1 rounded transition-colors",
                  filterTab === "all"
                    ? "bg-[#1e293b] text-white font-medium border border-sky-500/40"
                    : "text-[var(--color-muted)] hover:text-white",
                )}
              >
                ทั้งหมด ({data?.items?.length ?? 0})
              </button>
              <button
                type="button"
                onClick={() => setFilterTab("active")}
                className={cn(
                  "px-2.5 py-1 rounded transition-colors",
                  filterTab === "active"
                    ? "bg-[#1e293b] text-white font-medium border border-sky-500/40"
                    : "text-[var(--color-muted)] hover:text-white",
                )}
              >
                ใน Universe ({activeSymbols.size})
              </button>
              <button
                type="button"
                onClick={() => setFilterTab("actionable")}
                className={cn(
                  "px-2.5 py-1 rounded transition-colors",
                  filterTab === "actionable"
                    ? "bg-[#1e293b] text-white font-medium border border-sky-500/40"
                    : "text-[var(--color-muted)] hover:text-white",
                )}
              >
                มีสัญญาณเทรด
              </button>
              <button
                type="button"
                onClick={() => setFilterTab("high_score")}
                className={cn(
                  "px-2.5 py-1 rounded transition-colors",
                  filterTab === "high_score"
                    ? "bg-[#1e293b] text-white font-medium border border-sky-500/40"
                    : "text-[var(--color-muted)] hover:text-white",
                )}
              >
                คะแนนสูง (≥65)
              </button>
            </div>
          </div>

          <div className="flex min-w-0 items-center gap-2">
            <span className="text-xs text-[var(--color-muted)] whitespace-nowrap">เรียงตาม:</span>
            <select
              value={sortBy}
              onChange={(e) => setSortBy(e.target.value)}
              className="min-w-0 max-w-full flex-1 md:flex-none rounded-md border border-[var(--color-border)] bg-[#101722] px-2.5 py-1 text-xs text-[var(--color-foreground)] focus:outline-none focus:border-sky-500 font-medium cursor-pointer"
            >
              <option value="suitability_desc">🌟 โดดเด่น / เหมาะเทรดที่สุด (Trade Suitability)</option>
              <option value="confidence_desc">🎯 คะแนนความมั่นใจ (Confidence Score: สูง ➔ ต่ำ)</option>
              <option value="symbol_asc">🔤 ชื่อเหรียญ (Symbol: A ➔ Z)</option>
              <option value="symbol_desc">🔤 ชื่อเหรียญ (Symbol: Z ➔ A)</option>
              <option value="action_desc">📊 สถานะสัญญาณ (Action Signal: Buy/Sell ➔ Flat)</option>
              <option value="freshness_asc">⏱️ ความสดใหม่ของข้อมูล (Data Age: สดที่สุดก่อน)</option>
              <option value="rows_desc">📈 ปริมาณแท่งเทียน (Candle Rows: มาก ➔ น้อย)</option>
              <option value="rsi_asc">📉 RSI(14) Momentum (ต่ำ ➔ สูง)</option>
            </select>
          </div>
        </div>

        {/* Section 4: Crypto List Cards */}
        {filteredAndSortedItems.length === 0 ? (
          <div className="text-center py-8 text-xs text-[var(--color-muted)]">
            ไม่พบเหรียญที่ตรงกับเงื่อนไขการค้นหา
          </div>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-2">
            {filteredAndSortedItems.map((item, idx) => (
              <CryptoAssetCard
                key={item.symbol}
                item={item}
                rank={sortBy === "suitability_desc" ? idx + 1 : undefined}
                isConfirmingRemove={confirmRemove === item.symbol}
                busy={actionBusy}
                syncing={syncingSymbol === item.symbol || syncingMarket}
                onAdd={() => void handleAddSymbol(item.symbol)}
                onRemove={() => void handleRemoveSymbol(item.symbol)}
                onSync={() => void handleSyncMarket(item.symbol)}
              />
            ))}
          </div>
        )}

        {/* Section 5: Ingest Command Hint */}
        {data?.backfill_hint ? (
          <div className="rounded-md bg-[#161d27] p-3 text-xs border border-[var(--color-border)] space-y-1.5">
            <div className="flex items-center justify-between">
              <span className="text-[var(--color-muted)] font-mono">
                คำสั่ง Backfill ข้อมูลครบทุกเหรียญใน Universe:
              </span>
              <button
                type="button"
                onClick={() => {
                  const hint = data.backfill_hint;
                  if (!hint) return;
                  void copyText(hint).then((ok) => {
                    if (!ok) return;
                    setCopiedHint(true);
                    setTimeout(() => setCopiedHint(false), 2000);
                  });
                }}
                className="text-[11px] text-sky-400 hover:text-sky-300 font-mono transition-colors"
              >
                {copiedHint ? "✓ คัดลอกแล้ว" : "คัดลอกคำสั่ง"}
              </button>
            </div>
            <code className="block overflow-x-auto text-[11px] text-emerald-400 font-mono select-all">
              {data.backfill_hint}
            </code>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}

function CryptoAssetCard({
  item,
  rank,
  isConfirmingRemove,
  busy,
  syncing,
  onAdd,
  onRemove,
  onSync,
}: {
  item: CryptoSuitability;
  rank?: number;
  isConfirmingRemove: boolean;
  busy: boolean;
  syncing?: boolean;
  onAdd: () => void;
  onRemove: () => void;
  onSync?: () => void;
}) {
  return (
    <div
      className={cn(
        "rounded-lg border p-4 transition-all relative flex flex-col justify-between gap-3",
        item.is_active
          ? "border-[var(--color-border)] bg-[var(--color-card)]/50 hover:border-slate-600"
          : "border-dashed border-slate-800 bg-[var(--color-card)]/20 opacity-80",
      )}
    >
      <div className="space-y-3">
        {/* Top bar: Rank, Symbol, Active badge, and Action status */}
        <div className="flex items-start justify-between gap-2">
          <div className="flex items-center gap-2">
            {rank != null ? (
              <span
                className={cn(
                  "flex h-6 w-6 items-center justify-center rounded-full text-xs font-bold font-mono",
                  rank === 1
                    ? "bg-amber-500/20 text-amber-300 border border-amber-500/40"
                    : rank === 2
                    ? "bg-slate-300/20 text-slate-200 border border-slate-400/40"
                    : rank === 3
                    ? "bg-amber-700/20 text-amber-500 border border-amber-700/40"
                    : "bg-slate-800 text-[var(--color-muted)] border border-slate-700",
                )}
              >
                {rank}
              </span>
            ) : null}
            <div>
              <div className="flex items-center gap-1.5">
                <span className="font-bold text-base tracking-tight text-white font-mono">
                  {item.symbol}
                </span>
                {item.is_active ? (
                  <span className="inline-block px-1.5 py-0.5 rounded text-[10px] font-medium bg-emerald-500/10 text-emerald-400 border border-emerald-500/30">
                    Active
                  </span>
                ) : (
                  <span className="inline-block px-1.5 py-0.5 rounded text-[10px] font-medium bg-slate-800 text-slate-400 border border-slate-700">
                    Inactive
                  </span>
                )}
              </div>
              <div className="text-[11px] text-[var(--color-muted)] font-mono">
                {item.candle.close != null ? `$${item.candle.close.toLocaleString()}` : "ไม่มีข้อมูลราคา"}
              </div>
            </div>
          </div>

          <div className="flex flex-col items-end gap-1">
            <span
              className={cn(
                "px-2 py-0.5 rounded text-xs font-bold uppercase tracking-wider font-mono border",
                item.action === "buy" || item.action === "long"
                  ? "bg-emerald-500/15 text-emerald-400 border-emerald-500/30"
                  : item.action === "sell" || item.action === "short"
                  ? "bg-rose-500/15 text-rose-400 border-rose-500/30"
                  : "bg-slate-800 text-slate-400 border-slate-700",
              )}
            >
              {item.action}
            </span>
            <span className="text-[10px] text-[var(--color-muted)] font-mono">
              conf: {(item.confidence_score * 100).toFixed(0)}% · {item.regime_state}
            </span>
          </div>
        </div>

        {/* Suitability Score Bar */}
        <div className="rounded-md border border-[var(--color-border)] bg-[#101722] p-2.5 space-y-1.5">
          <div className="flex items-center justify-between text-xs">
            <span className="text-[var(--color-muted)] flex items-center gap-1">
              <span>ความโดดเด่น / เหมาะเทรด:</span>
              <span className="font-semibold" style={{ color: item.color }}>
                {item.label}
              </span>
            </span>
            <span className="font-mono font-bold text-sm" style={{ color: item.color }}>
              {item.score} / 100
            </span>
          </div>

          <div className="h-1.5 w-full rounded-full bg-slate-800 overflow-hidden">
            <div
              className="h-full rounded-full transition-all duration-500"
              style={{
                width: `${Math.min(100, Math.max(5, item.score))}%`,
                backgroundColor: item.color,
                boxShadow: item.score >= 80 ? `0 0 8px ${item.color}` : undefined,
              }}
            />
          </div>

          {item.reasons && item.reasons.length > 0 ? (
            <div className="flex flex-wrap gap-1 pt-1">
              {item.reasons.slice(0, 3).map((r, i) => (
                <span
                  key={i}
                  className="px-1.5 py-0.5 rounded bg-[#161f2e] text-[10px] text-slate-300 border border-slate-700/60 leading-none"
                >
                  ✓ {r}
                </span>
              ))}
            </div>
          ) : null}
        </div>

        {/* Key Indicators Mini Grid */}
        <div className="grid grid-cols-4 gap-1.5 text-center text-xs">
          <div className="rounded border border-[var(--color-border)] bg-[#141b26] p-1.5">
            <div className="text-[10px] text-[var(--color-muted)]">EMA20</div>
            <div className="font-mono font-medium text-[11px] truncate">
              {item.candle.ema_20 != null ? item.candle.ema_20.toFixed(1) : "—"}
            </div>
          </div>
          <div className="rounded border border-[var(--color-border)] bg-[#141b26] p-1.5">
            <div className="text-[10px] text-[var(--color-muted)]">EMA200</div>
            <div className="font-mono font-medium text-[11px] truncate">
              {item.candle.ema_200 != null ? item.candle.ema_200.toFixed(1) : "—"}
            </div>
          </div>
          <div className="rounded border border-[var(--color-border)] bg-[#141b26] p-1.5">
            <div className="text-[10px] text-[var(--color-muted)]">RSI(14)</div>
            <div
              className={cn(
                "font-mono font-medium text-[11px]",
                item.candle.rsi_14 != null && item.candle.rsi_14 <= 30
                  ? "text-emerald-400"
                  : item.candle.rsi_14 != null && item.candle.rsi_14 >= 70
                  ? "text-rose-400"
                  : "text-slate-300",
              )}
            >
              {item.candle.rsi_14 != null ? item.candle.rsi_14.toFixed(1) : "—"}
            </div>
          </div>
          <div className="rounded border border-[var(--color-border)] bg-[#141b26] p-1.5">
            <div className="text-[10px] text-[var(--color-muted)]">แท่งเทียน</div>
            <div className="font-mono font-medium text-[11px] truncate">
              {item.health.rows.toLocaleString()}
            </div>
          </div>
        </div>
      </div>

      {/* Action Footer: Age, Sync button & Remove / Add button */}
      <div className="flex items-center justify-between pt-2 border-t border-[var(--color-border)]/50 text-xs gap-2">
        <div className="flex items-center gap-1.5 min-w-0">
          <span className="text-[11px] text-[var(--color-muted)] font-mono truncate">
            {item.health.newest_age_min != null ? `อัปเดต ${item.health.newest_age_min.toFixed(0)} น.ที่แล้ว` : "—"}
          </span>
          {onSync && item.is_active ? (
            <button
              type="button"
              disabled={busy || syncing}
              onClick={onSync}
              className="text-[10px] text-sky-400 hover:text-sky-300 font-mono underline disabled:opacity-50"
              title="ซิงค์ดึงข้อมูลแท่งเทียนล่าสุดเฉพาะเหรียญนี้"
            >
              {syncing ? "ซิงค์…" : "⚡ ซิงค์"}
            </button>
          ) : null}
        </div>

        {item.is_active ? (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            disabled={busy || syncing}
            onClick={onRemove}
            className={cn(
              "h-11 sm:h-7 text-xs font-mono transition-colors",
              isConfirmingRemove
                ? "bg-rose-500/20 text-rose-300 hover:bg-rose-500/30 border border-rose-500/50"
                : "text-slate-400 hover:text-rose-400 hover:bg-rose-500/10",
            )}
          >
            {isConfirmingRemove ? "ยืนยันลบ?" : "✕ นำออก"}
          </Button>
        ) : (
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={busy || syncing}
            onClick={onAdd}
            className="h-11 sm:h-7 text-xs font-mono border-emerald-500/40 text-emerald-400 hover:bg-emerald-500/10"
          >
            + เพิ่มเข้า Universe
          </Button>
        )}
      </div>
    </div>
  );
}
