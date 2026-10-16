import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  api,
  type DoctrineSnippet,
  type QdrantStatus,
} from "@/lib/api";
import {
  BookOpen,
  CheckCircle2,
  Database,
  ExternalLink,
  Layers,
  RefreshCw,
  Search,
  Sparkles,
  Zap,
} from "lucide-react";

interface QdrantTradingRulesCardProps {
  qdrant?: QdrantStatus | Record<string, unknown> | null;
  ragStatus?: unknown;
  onRefresh?: () => void;
}

const PRESET_QUERIES = [
  { label: "📌 Pullback EMA20", query: "pullback EMA20" },
  { label: "🛡️ Stop Loss & Risk", query: "capital preservation stop loss 1.5 ATR" },
  { label: "📊 Wyckoff Distribution", query: "Wyckoff distribution spring test" },
  { label: "📈 Microstructure & Book", query: "order book imbalance liquidity microstructure" },
  { label: "⚡ Breakout & Momentum", query: "trend_up breakout volume confirmation" },
];

export function QdrantTradingRulesCard({
  qdrant,
  ragStatus,
  onRefresh,
}: QdrantTradingRulesCardProps) {
  const [syncing, setSyncing] = useState(false);
  const [syncResult, setSyncResult] = useState<{ ok: boolean; msg: string } | null>(null);

  // Query tester state
  const [query, setQuery] = useState("pullback EMA20");
  const [queryLimit, setQueryLimit] = useState(3);
  const [searching, setSearching] = useState(false);
  const [hits, setHits] = useState<DoctrineSnippet[] | null>(null);
  const [queryError, setQueryError] = useState<string | null>(null);
  const [hasQueried, setHasQueried] = useState(false);

  // Normalize Qdrant data
  const qData = (qdrant || {}) as QdrantStatus;
  const exists = qData.exists === true;
  const points = qData.points ?? qData.vectors_count ?? 0;
  const rawStatus = String(qData.status || (exists ? (points > 0 ? "green" : "yellow") : "red")).toLowerCase();

  // Compute status indicator
  let statusBadgeVariant: "ok" | "default" | "danger" | "muted" = "danger";
  let statusBadgeLabel = "ขัดข้อง (Offline)";
  let statusDotColor = "bg-rose-500";

  if (exists && rawStatus === "green") {
    statusBadgeVariant = "ok";
    statusBadgeLabel = "พร้อมใช้งาน (Ready · Green)";
    statusDotColor = "bg-emerald-400";
  } else if (exists && rawStatus === "yellow") {
    statusBadgeVariant = "default";
    statusBadgeLabel = "กำลังสร้างดัชนี (Indexing · Yellow)";
    statusDotColor = "bg-amber-400";
  }

  // Last RAG info
  const ragObj = (ragStatus && typeof ragStatus === "object" ? ragStatus : {}) as Record<string, unknown>;
  const lastIndex = (ragObj.index && typeof ragObj.index === "object" ? ragObj.index : {}) as Record<string, unknown>;

  async function handleSync() {
    setSyncing(true);
    setSyncResult(null);
    try {
      const res = await api.syncRag();
      if (res.ok) {
        setSyncResult({
          ok: true,
          msg: "ซิงค์และอัปเดตเวกเตอร์กฎการเทรดลง Qdrant สำเร็จเรียบร้อย",
        });
        onRefresh?.();
      } else {
        setSyncResult({
          ok: false,
          msg: `ซิงค์ไม่สำเร็จ: ${res.error || (res.status_code ? `HTTP ${res.status_code}` : "ดู log ของ jobs")}`,
        });
      }
    } catch (err) {
      setSyncResult({
        ok: false,
        msg: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setSyncing(false);
    }
  }

  async function handleSearch(searchQuery: string = query) {
    if (!searchQuery.trim()) return;
    setSearching(true);
    setQueryError(null);
    setHasQueried(true);
    try {
      const res = await api.queryDoctrine(searchQuery.trim(), queryLimit);
      if (res.ok) {
        setHits(res.hits || []);
      } else {
        setQueryError(res.error || "เกิดข้อผิดพลาดในการดึงข้อมูลจาก Qdrant");
        setHits([]);
      }
    } catch (err) {
      setQueryError(err instanceof Error ? err.message : String(err));
      setHits([]);
    } finally {
      setSearching(false);
    }
  }

  return (
    <Card className="border-[var(--color-border)] shadow-sm">
      <CardHeader className="pb-4">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
              <Database className="h-5 w-5" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <CardTitle className="text-base sm:text-lg font-bold">
                  Qdrant กฎการเทรด (Trading Rules Vector Store)
                </CardTitle>
              </div>
              <CardDescription className="text-xs sm:text-sm">
                ฐานข้อมูลเวกเตอร์สำหรับจัดเก็บและค้นหากฎการเทรด (RAG) ประกอบการวิเคราะห์และตัดสินใจของ AI
              </CardDescription>
            </div>
          </div>

          <div className="flex items-center gap-2">
            <Badge variant={statusBadgeVariant} className="flex items-center gap-1.5 px-2.5 py-1 text-xs">
              <span className={`inline-block h-2 w-2 rounded-full ${statusDotColor} animate-pulse`} />
              <span>{statusBadgeLabel}</span>
            </Badge>
          </div>
        </div>
      </CardHeader>

      <CardContent className="space-y-6">
        {/* Metric Overview Grid */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <div className="p-3.5 rounded-lg border border-[var(--color-border)] bg-[var(--color-card)]/60">
            <div className="flex items-center justify-between text-xs text-[var(--color-muted)] mb-1">
              <span>สถานะ Vector DB</span>
              <Database className="h-3.5 w-3.5 text-emerald-400" />
            </div>
            <div className="text-sm sm:text-base font-semibold text-[var(--color-foreground)] flex items-center gap-1.5">
              <span className={`h-2 w-2 rounded-full ${statusDotColor}`} />
              <span className="capitalize">{rawStatus}</span>
            </div>
            <div className="text-[11px] text-[var(--color-muted)] mt-0.5 truncate">
              {qData.collection || "jarvise_doctrine"}
            </div>
          </div>

          <div className="p-3.5 rounded-lg border border-[var(--color-border)] bg-[var(--color-card)]/60">
            <div className="flex items-center justify-between text-xs text-[var(--color-muted)] mb-1">
              <span>จำนวนกฎ (Vectors)</span>
              <Layers className="h-3.5 w-3.5 text-sky-400" />
            </div>
            <div className="text-sm sm:text-base font-semibold text-[var(--color-foreground)]">
              {points.toLocaleString()} Points
            </div>
            <div className="text-[11px] text-[var(--color-muted)] mt-0.5">
              384 Dimensions (Dense)
            </div>
          </div>

          <div className="p-3.5 rounded-lg border border-[var(--color-border)] bg-[var(--color-card)]/60">
            <div className="flex items-center justify-between text-xs text-[var(--color-muted)] mb-1">
              <span>แหล่งที่มาของกฎ</span>
              <BookOpen className="h-3.5 w-3.5 text-purple-400" />
            </div>
            <div className="text-sm sm:text-base font-semibold text-[var(--color-foreground)]">
              3 Streams
            </div>
            <div className="text-[11px] text-[var(--color-muted)] mt-0.5 truncate">
              Notebook + OpenClaw + Local
            </div>
          </div>

          <div className="p-3.5 rounded-lg border border-[var(--color-border)] bg-[var(--color-card)]/60">
            <div className="flex items-center justify-between text-xs text-[var(--color-muted)] mb-1">
              <span>การประยุกต์ใช้</span>
              <Sparkles className="h-3.5 w-3.5 text-amber-400" />
            </div>
            <div className="text-sm sm:text-base font-semibold text-[var(--color-foreground)]">
              Auto-Decide & Home
            </div>
            <div className="text-[11px] text-[var(--color-muted)] mt-0.5">
              EMA20 Pullback + Safeguards
            </div>
          </div>
        </div>

        {/* Sync Controls & Feedback */}
        <div className="flex flex-col sm:flex-row items-stretch sm:items-center justify-between gap-3 p-3.5 rounded-lg border border-[var(--color-border)] bg-[var(--color-card)]/30">
          <div className="text-xs text-[var(--color-muted)]">
            <div className="font-medium text-[var(--color-foreground)] mb-0.5">
              รอบซิงค์ RAG ล่าสุด
            </div>
            <div>
              {lastIndex.chunks ? (
                <span>Indexed {String(lastIndex.chunks)} chunks เข้า {String(lastIndex.collection || "jarvise_doctrine")}</span>
              ) : (
                <span>พร้อมซิงค์กฎล่าสุดจาก Gemini NotebookLM และ OpenClaw</span>
              )}
            </div>
          </div>

          <div className="flex items-center gap-2">
            <Button
              size="sm"
              variant="default"
              disabled={syncing}
              onClick={() => void handleSync()}
              className="bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-medium gap-1.5 shrink-0"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${syncing ? "animate-spin" : ""}`} />
              {syncing ? "กำลังซิงค์และสร้างดัชนี…" : "🔄 ซิงค์กฎการเทรดทันที"}
            </Button>
          </div>
        </div>

        {syncResult ? (
          <div
            className={`p-3 rounded-lg border text-xs flex items-center gap-2 ${
              syncResult.ok
                ? "bg-emerald-500/10 border-emerald-500/30 text-emerald-300"
                : "bg-rose-500/10 border-rose-500/30 text-rose-300"
            }`}
          >
            {syncResult.ok ? (
              <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-400" />
            ) : (
              <Zap className="h-4 w-4 shrink-0 text-rose-400" />
            )}
            <span>{syncResult.msg}</span>
          </div>
        ) : null}

        {/* Interactive trading-rules query tester */}
        <div className="space-y-3 pt-2 border-t border-[var(--color-border)]">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Search className="h-4 w-4 text-emerald-400" />
              <h3 className="text-sm font-semibold text-[var(--color-foreground)]">
                เครื่องมือทดสอบค้นหากฎการเทรด (Retrieval Tester)
              </h3>
            </div>
            <span className="text-xs text-[var(--color-muted)]">
              ทดสอบเวกเตอร์เสมือนจริงที่ AI ใช้งาน
            </span>
          </div>

          {/* Preset Chips */}
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-xs text-[var(--color-muted)] mr-1">คำค้นหายอดนิยม:</span>
            {PRESET_QUERIES.map((p) => (
              <button
                key={p.label}
                type="button"
                onClick={() => {
                  setQuery(p.query);
                  void handleSearch(p.query);
                }}
                className="text-xs px-2.5 py-1 rounded-md border border-[var(--color-border)] bg-[var(--color-card)] hover:bg-[#1f2937] text-[var(--color-foreground)] transition-colors cursor-pointer"
              >
                {p.label}
              </button>
            ))}
          </div>

          {/* Search Box */}
          <div className="flex flex-col sm:flex-row gap-2">
            <div className="relative flex-1">
              <input
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    void handleSearch();
                  }
                }}
                placeholder="ระบุสถานการณ์หรือกฎการเทรดที่ต้องการค้นหา เช่น 'pullback EMA20', 'cut loss 1.5 ATR'…"
                className="w-full h-9 rounded-md border border-[var(--color-border)] bg-[var(--color-card)] px-3 text-xs text-[var(--color-foreground)] placeholder:text-[var(--color-muted)] focus:outline-none focus:border-emerald-500/50"
              />
            </div>

            <div className="flex items-center gap-2">
              <select
                value={queryLimit}
                onChange={(e) => setQueryLimit(Number(e.target.value))}
                className="h-9 rounded-md border border-[var(--color-border)] bg-[var(--color-card)] px-2.5 text-xs text-[var(--color-foreground)] focus:outline-none"
              >
                <option value={2}>ดึง 2 ข้อ</option>
                <option value={3}>ดึง 3 ข้อ</option>
                <option value={5}>ดึง 5 ข้อ</option>
              </select>

              <Button
                size="sm"
                variant="outline"
                disabled={searching || !query.trim()}
                onClick={() => void handleSearch()}
                className="h-9 px-3.5 text-xs border-[var(--color-border)] hover:border-emerald-500/40 gap-1.5 shrink-0"
              >
                <Search className={`h-3.5 w-3.5 ${searching ? "animate-spin" : ""}`} />
                {searching ? "กำลังค้นหา…" : "ค้นหากฎ"}
              </Button>
            </div>
          </div>

          {/* Search Results Display */}
          {queryError ? (
            <div className="p-3 rounded-md bg-rose-500/10 border border-rose-500/30 text-xs text-rose-300">
              {queryError}
            </div>
          ) : null}

          {hasQueried && !searching && hits && (
            <div className="space-y-2 mt-2">
              {hits.length === 0 ? (
                <div className="p-4 rounded-md border border-dashed border-[var(--color-border)] text-center text-xs text-[var(--color-muted)]">
                  ไม่พบกฎการเทรดที่ตรงกับคำค้นหานี้
                </div>
              ) : (
                hits.map((h, idx) => {
                  const scorePct = h.score != null ? Math.round(h.score * 100) : null;
                  return (
                    <div
                      key={idx}
                      className="p-3 rounded-lg border border-[var(--color-border)] bg-[var(--color-card)]/40 hover:bg-[var(--color-card)]/70 transition-colors space-y-1.5"
                    >
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <div className="flex items-center gap-1.5">
                          <span className="text-xs font-mono font-medium text-emerald-400">
                            กฎข้อที่ #{idx + 1}
                          </span>
                          {h.source ? (
                            <Badge variant="muted" className="text-[10px] py-0 px-1.5 font-mono">
                              📄 {h.source}
                            </Badge>
                          ) : null}
                        </div>

                        {scorePct != null ? (
                          <div className="flex items-center gap-1 text-[11px] text-emerald-400 font-mono">
                            <span>ความตรงกัน (Cosine):</span>
                            <span className="font-bold">{h.score?.toFixed(3)} ({scorePct}%)</span>
                          </div>
                        ) : null}
                      </div>

                      <p className="text-xs text-[var(--color-foreground)] leading-relaxed bg-[#131922] p-2.5 rounded border border-[var(--color-border)] font-mono">
                        {h.text}
                      </p>
                    </div>
                  );
                })
              )}
            </div>
          )}
        </div>

        {/* Source Directory Legend */}
        <div className="p-3 rounded-lg border border-[var(--color-border)] bg-[var(--color-card)]/20 text-xs text-[var(--color-muted)] space-y-1.5">
          <div className="font-medium text-[var(--color-foreground)] flex items-center gap-1.5">
            <BookOpen className="h-3.5 w-3.5 text-purple-400" />
            <span>แหล่งข้อมูลกฎการเทรดที่เชื่อมโยงกับ Qdrant:</span>
          </div>
          <ul className="list-disc list-inside space-y-1 pl-1 text-[11px]">
            <li>
              <span className="text-[var(--color-foreground)] font-mono">Gemini NotebookLM</span>:
              สมุดบันทึกหลัก <code className="bg-[#161d27] px-1 py-0.5 rounded">14e11c63-e2ee-4b49-898f-b0cc4c61cb4e</code> (chatoe@gmail.com)
              <a
                href="https://notebook.google.com/notebook/14e11c63-e2ee-4b49-898f-b0cc4c61cb4e"
                target="_blank"
                rel="noreferrer"
                className="ml-1 text-emerald-400 hover:underline inline-flex items-center gap-0.5"
              >
                เปิด <ExternalLink className="h-2.5 w-2.5" />
              </a>
            </li>
            <li>
              <span className="text-[var(--color-foreground)] font-mono">Jarvise Caution Pullback Rule</span>:
              <code className="bg-[#161d27] px-1 py-0.5 rounded ml-1">sources/jarvise-doctrine-pullback-entry.txt</code> (กฎเข้าไม้ shallow EMA20 pullback ในแนวโน้มขาขึ้น)
            </li>
            <li>
              <span className="text-[var(--color-foreground)] font-mono">OpenClaw Trading Notes</span>:
              <code className="bg-[#161d27] px-1 py-0.5 rounded ml-1">data/openclaw/exports</code> (บันทึกกลยุทธ์และการวิเคราะห์สภาวะตลาด)
            </li>
          </ul>
        </div>
      </CardContent>
    </Card>
  );
}
