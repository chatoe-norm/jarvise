"""OpenClaw Tactical Intelligence & Post-Mortem Generator.

Analyzes closed paper round-trips from paper_decision_outcomes / paper_orders,
extracts technical indicators at entry, diagnoses inefficiencies (overextension,
low volume, adverse reversals), and produces structured tactical notes in
data/openclaw/exports/tactics/ for Qdrant doctrine RAG sync.

Paper only. No live trading or exchange orders.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jarvise.rag import repo_root, sync_openclaw
from jarvise_ingest.db import (
    list_paper_decision_outcomes,
    list_paper_orders_asc,
)
from jarvise_paper.metrics import reconstruct_closed_trades

DEFAULT_TACTICS_DIR = Path("data/openclaw/exports/tactics")


def _load_entry_technicals(
    conn: Any,
    symbol: str,
    timeframe: str,
    open_ts_ms: int,
) -> tuple[dict[str, Any] | None, float]:
    """Retrieve the nearest candle <= open_ts_ms and 20-period average volume."""
    cur = conn.execute(
        """
        SELECT timestamp, close, volume, ema_20, ema_200, rsi_14, atr_14
        FROM market_technicals
        WHERE symbol = ? AND timeframe = ? AND timestamp <= ?
        ORDER BY timestamp DESC
        LIMIT 20
        """,
        (symbol.upper(), timeframe, open_ts_ms),
    )
    rows = cur.fetchall()
    if not rows:
        return None, 0.0

    entry_row = dict(rows[0])
    vols = [float(r["volume"]) for r in rows if r["volume"] is not None]
    avg_vol = (sum(vols) / len(vols)) if vols else 0.0
    return entry_row, avg_vol


def diagnose_trade(
    trade: dict[str, Any],
    candle: dict[str, Any] | None,
    avg_vol: float,
) -> dict[str, Any]:
    """Analyze a closed trade and technical conditions to form lessons and rules."""
    symbol = str(trade.get("symbol") or "UNKNOWN").upper()
    side = str(trade.get("side") or "long").lower()
    pnl = float(trade.get("pnl_usd") or 0.0)
    r_mult = trade.get("r_multiple")
    hold_ms = int(trade.get("hold_ms") or (int(trade.get("close_ts", 0)) - int(trade.get("open_ts", 0))))
    hold_hrs = round(hold_ms / 3_600_000.0, 1) if hold_ms > 0 else 0.0
    exit_reason = str(trade.get("exit_reason") or "closed")
    decision_source = str(trade.get("decision_source") or "unknown")
    close_ts = int(trade.get("close_ts") or trade.get("closed_at_ms") or 0)
    date_str = (
        datetime.fromtimestamp(close_ts / 1000.0, tz=UTC).strftime("%Y-%m-%d")
        if close_ts > 0
        else "2026-10-16"
    )

    entry_price = float(trade.get("entry_price") or (candle.get("close") if candle else 0.0) or 0.0)
    exit_price = float(trade.get("exit_price") or entry_price)

    c = candle or {}
    close_px = float(c.get("close") or entry_price)
    ema20 = float(c.get("ema_20") or close_px)
    ema200 = float(c.get("ema_200") or ema20)
    atr = float(c.get("atr_14") or 0.0)
    rsi = float(c.get("rsi_14") or 50.0)
    vol = float(c.get("volume") or 0.0)
    vol_ratio = round(vol / avg_vol, 2) if avg_vol > 0 else 1.0

    dist_ema20_atr = round((close_px - ema20) / atr, 2) if atr > 0 else 0.0
    is_loss = pnl < 0
    is_win = pnl > 0

    # Pattern and inefficiency detection
    inefficiencies: list[str] = []
    rules: list[str] = []

    if side == "long":
        if dist_ema20_atr > 1.5:
            inefficiencies.append(
                f"ราคาไล่ซื้อสูงเกินไป โดยอยู่เหนือ EMA20 ถึง {dist_ema20_atr}x ATR เสี่ยงต่อการเกิด Mean Reversion ย่อตัวกลับ"
            )
            rules.append(
                "ห้ามเปิด Long หากราคาขยายตัวเกิน 1.5x ATR จากแนวรับ EMA20 ให้รอจังหวะ Pullback ย่อลึกทดสอบ EMA20 ก่อน"
            )
        if rsi > 65:
            inefficiencies.append(f"RSI อยู่ในระดับสูง ({rsi:.1f}) สะท้อนภาวะ Overbought และโมเมนตัมชะลอตัว")
            rules.append("หลีกเลี่ยงการเปิด Long เมื่อ RSI > 65 ให้รอการพักตัวของอินดิเคเตอร์เพื่อลดความเสี่ยง")
        if vol_ratio < 0.8:
            inefficiencies.append(f"Volume ขณะเข้าเทรดต่ำกว่าค่าเฉลี่ย ({vol_ratio}x) บ่งบอกว่าขาดแรงหนุนจากฝั่งซื้อ")
            rules.append("ตรวจสอบ Volume Confirm: หาก Volume ratio < 0.8x ให้ Defer หรือ Reject สัญญาณเข้าทันที")
        if ema20 < ema200:
            inefficiencies.append("เปิดสถานะ Long สวนทางกับโครงสร้างใหญ่ (EMA20 ต่ำกว่า EMA200)")
            rules.append("เน้นเปิด Long เฉพาะเมื่อ EMA20 ยืนเหนือ EMA200 หรือมีสัญญาณ Reversal ชัดเจนเท่านั้น")
    else:  # short
        if dist_ema20_atr < -1.5:
            inefficiencies.append(
                f"ราคาไล่ขายต่ำเกินไป โดยอยู่ใต้ EMA20 ถึง {abs(dist_ema20_atr)}x ATR เสี่ยงต่อการเกิด Technical Rebound"
            )
            rules.append("ห้ามเปิด Short หากราคาขยายตัวลงลึกเกิน 1.5x ATR ใต้ EMA20 ให้รอการ Rebound สู่แนวต้านก่อน")
        if rsi < 35:
            inefficiencies.append(f"RSI อยู่ในโซน Oversold ({rsi:.1f}) มีโอกาสเกิดการดีดกลับฉับพลัน")
            rules.append("หลีกเลี่ยงการเปิด Short เมื่อ RSI < 35 เพื่อป้องกันการติดกับดัก Bear Trap")
        if vol_ratio < 0.8:
            inefficiencies.append(f"Volume ฝั่งขายต่ำกว่าค่าเฉลี่ย ({vol_ratio}x) ขาดแรงผลักดันต่อเนื่อง")
            rules.append("ตรวจสอบ Volume Breakdown: หากขาด Volume ยืนยัน ให้ Defer สัญญาณ Short")

    if not inefficiencies:
        if is_loss:
            inefficiencies.append(
                f"ความผันผวนของตลาดชน Stop Loss ({exit_reason}) ในช่วงเวลาถือครอง {hold_hrs} ชม."
            )
        else:
            inefficiencies.append(
                f"การเทรดเป็นไปตามแผนและทำกำไร ปิดสถานะด้วยเหตุผล {exit_reason} (PnL +${pnl:.2f})"
            )

    if not rules:
        rules.append(
            "ปฏิบัติตามวินัย Stop Loss 1.5x ATR และรักษากฎ One Position Per Symbol (same_side_hold) เพื่อรักษาระดับ Capital Preservation"
        )
        rules.append("ตรวจสอบ Market Safety และ Liquidity Microstructure เสมอก่อนยืนยันการเทรด")

    return {
        "symbol": symbol,
        "side": side,
        "pnl_usd": pnl,
        "r_multiple": r_mult,
        "hold_hours": hold_hrs,
        "exit_reason": exit_reason,
        "decision_source": decision_source,
        "date_str": date_str,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "is_loss": is_loss,
        "is_win": is_win,
        "indicators": {
            "close": close_px,
            "ema_20": ema20,
            "ema_200": ema200,
            "atr_14": atr,
            "rsi_14": rsi,
            "volume_ratio": vol_ratio,
            "dist_ema20_atr": dist_ema20_atr,
        },
        "inefficiencies": inefficiencies,
        "rules": rules,
    }


def format_post_mortem_markdown(diag: dict[str, Any]) -> str:
    """Format diagnosis into markdown following the OpenClaw tactical note standard."""
    sym = diag["symbol"]
    side = diag["side"].upper()
    res = "Loss Lesson" if diag["is_loss"] else ("Win Case" if diag["is_win"] else "Neutral")
    date = diag["date_str"]
    pnl = diag["pnl_usd"]
    r_val = f"{diag['r_multiple']:.4f}" if diag["r_multiple"] is not None else "N/A"

    inds = diag["indicators"]
    ineff_bullets = "\n".join(f"- {item}" for item in diag["inefficiencies"])
    rule_bullets = "\n".join(f"- {rule}" for rule in diag["rules"])

    return f"""# Post-Mortem: {sym} {side} {res} ({date})

- **Category**: Post-Mortem
- **Asset**: {sym}
- **Side**: {diag["side"]}
- **PnL**: ${pnl:.2f} (R: {r_val})
- **Hold Duration**: {diag["hold_hours"]}h
- **Decision Source**: {diag["decision_source"]}
- **Exit Reason**: {diag["exit_reason"]}

### Observed Pattern & Market Inefficiency
{ineff_bullets}
- Entry Price: ${diag["entry_price"]:.2f}, Exit Price: ${diag["exit_price"]:.2f}
- Technical Indicators at Entry: Close=${inds["close"]:.2f}, EMA20=${inds["ema_20"]:.2f}, EMA200=${inds["ema_200"]:.2f}, RSI={inds["rsi_14"]:.1f}, Volume Ratio={inds["volume_ratio"]}x, EMA20 Dist={inds["dist_ema20_atr"]}x ATR

### Actionable Rule (กฎการเทรด)
{rule_bullets}
"""


def generate_post_mortems(
    conn: Any,
    *,
    output_dir: Path | None = None,
    limit: int = 20,
    losses_only: bool = True,
    force: bool = False,
    sync_rag: bool = False,
    timeframe: str = "4h",
) -> dict[str, Any]:
    """Generate tactical post-mortem notes from closed trades and optionally sync to RAG."""
    root = repo_root()
    target_dir = output_dir or (root / DEFAULT_TACTICS_DIR)
    target_dir.mkdir(parents=True, exist_ok=True)

    # 1. Fetch closed trades from paper_decision_outcomes or reconstructed orders
    outcomes = list_paper_decision_outcomes(conn, limit=limit * 2)
    orders = list_paper_orders_asc(conn)
    reconstructed, _, _ = reconstruct_closed_trades(orders)

    # Map trades
    trades: list[dict[str, Any]] = []
    if reconstructed:
        for t in reversed(reconstructed):
            if losses_only and float(t.get("pnl_usd") or 0.0) >= 0:
                continue
            trades.append(t)
    elif outcomes:
        for oc in outcomes:
            if losses_only and float(oc.get("pnl_usd") or 0.0) >= 0:
                continue
            trades.append(
                {
                    "symbol": oc["symbol"],
                    "side": oc["side"],
                    "pnl_usd": oc["pnl_usd"],
                    "r_multiple": oc.get("r_multiple"),
                    "hold_ms": oc.get("hold_ms"),
                    "open_order_id": oc["open_order_id"],
                    "close_order_id": oc["close_order_id"],
                    "decision_source": oc.get("decision_source"),
                    "close_ts": oc.get("closed_at_ms"),
                    "exit_reason": "closed",
                }
            )

    trades = trades[:limit]

    written_files: list[str] = []
    skipped_files: list[str] = []

    for t in trades:
        sym = str(t.get("symbol") or "UNKNOWN").lower()
        close_id = str(t.get("close_order_id") or "0")[:8]
        filename = f"post-mortem-{sym}-{close_id}.md"
        filepath = target_dir / filename

        if filepath.exists() and not force:
            skipped_files.append(filename)
            continue

        open_ts = int(t.get("open_ts") or t.get("closed_at_ms") or 0)
        candle, avg_vol = _load_entry_technicals(conn, sym, timeframe, open_ts)
        diag = diagnose_trade(t, candle, avg_vol)
        content = format_post_mortem_markdown(diag)

        filepath.write_text(content, encoding="utf-8")
        written_files.append(str(filepath))

    rag_result = None
    if sync_rag:
        rag_result = sync_openclaw(dry_run=False)

    return {
        "ok": True,
        "paper_only": True,
        "target_dir": str(target_dir),
        "total_trades_analyzed": len(trades),
        "written": written_files,
        "skipped": skipped_files,
        "rag_synced": rag_result is not None and bool(rag_result.get("ok")),
        "rag_details": rag_result,
    }
