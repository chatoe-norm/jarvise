"""Market-safety gate — FLAT / block trading; kill-switch on critical failures."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from jarvise_risk.caps import engage_kill_switch


def _fenv(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _benv(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


DEFAULT_MAX_SPREAD_BPS = 50.0
DEFAULT_MIN_DEPTH_USD = 25_000.0
DEFAULT_MAX_AGE_MIN = 90.0


@dataclass(frozen=True)
class MarketSafetyConfig:
    enabled: bool = True
    max_spread_bps: float = DEFAULT_MAX_SPREAD_BPS
    min_depth_usd: float = DEFAULT_MIN_DEPTH_USD
    max_age_min: float = DEFAULT_MAX_AGE_MIN
    require_book: bool = True
    require_derivatives: bool = True
    require_macro: bool = False


@dataclass
class MarketSafetyResult:
    ok: bool
    force_flat: bool
    critical: bool
    reasons: list[str] = field(default_factory=list)
    kill_switch_engaged: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "force_flat": self.force_flat,
            "critical": self.critical,
            "reasons": list(self.reasons),
            "kill_switch_engaged": self.kill_switch_engaged,
        }


def load_market_safety_config(
    *,
    skip_book: bool = False,
    skip_derivatives: bool = False,
    skip_macro: bool = False,
) -> MarketSafetyConfig:
    return MarketSafetyConfig(
        enabled=_benv("JARVISE_MARKET_SAFETY", True),
        max_spread_bps=_fenv(
            "JARVISE_MARKET_SAFETY_MAX_SPREAD_BPS", DEFAULT_MAX_SPREAD_BPS
        ),
        min_depth_usd=_fenv(
            "JARVISE_MARKET_SAFETY_MIN_DEPTH_USD", DEFAULT_MIN_DEPTH_USD
        ),
        max_age_min=_fenv("JARVISE_MARKET_SAFETY_MAX_AGE_MIN", DEFAULT_MAX_AGE_MIN),
        require_book=not skip_book,
        require_derivatives=not skip_derivatives,
        require_macro=not skip_macro
        and _benv("JARVISE_MARKET_SAFETY_REQUIRE_MACRO", False),
    )


def evaluate_market_safety(
    *,
    book: dict[str, Any] | None = None,
    derivatives: dict[str, Any] | None = None,
    macro: dict[str, Any] | None = None,
    config: MarketSafetyConfig | None = None,
    now_ms: int | None = None,
    provider_errors: list[str] | None = None,
) -> MarketSafetyResult:
    """Return gate outcome. Does not engage kill-switch (caller may)."""
    cfg = config or load_market_safety_config()
    if not cfg.enabled:
        return MarketSafetyResult(ok=True, force_flat=False, critical=False)

    now = int(now_ms if now_ms is not None else time.time() * 1000)
    max_age_ms = int(cfg.max_age_min * 60_000)
    reasons: list[str] = []
    critical = False

    for err in provider_errors or []:
        reasons.append(f"provider_error: {err}")
        critical = True

    if cfg.require_book:
        if book is None:
            reasons.append("book_missing")
            critical = True
        else:
            ts = int(book.get("timestamp") or 0)
            if now - ts > max_age_ms:
                reasons.append(f"book_stale_ms={now - ts}")
                critical = True
            spread = book.get("bid_ask_spread")
            if spread is None:
                reasons.append("book_spread_null")
                critical = True
            else:
                spread_bps = float(spread) * 10_000.0
                if spread_bps > cfg.max_spread_bps:
                    reasons.append(
                        f"spread_bps={spread_bps:.1f}>{cfg.max_spread_bps:.1f}"
                    )
            bid_d = book.get("bid_depth_1pct_usd")
            ask_d = book.get("ask_depth_1pct_usd")
            if bid_d is None or ask_d is None:
                reasons.append("book_depth_null")
                critical = True
            elif float(bid_d) < cfg.min_depth_usd and float(ask_d) < cfg.min_depth_usd:
                reasons.append(
                    f"illiquid_depth bid={bid_d} ask={ask_d} floor={cfg.min_depth_usd}"
                )
                critical = True

    if cfg.require_derivatives:
        if derivatives is None:
            reasons.append("derivatives_missing")
            critical = True
        else:
            # Event time or ingested_at — prefer ingested_at when present.
            ts = int(
                derivatives.get("ingested_at")
                or derivatives.get("timestamp")
                or 0
            )
            if now - ts > max_age_ms:
                reasons.append(f"derivatives_stale_ms={now - ts}")
                critical = True
            if derivatives.get("funding_rate") is None and derivatives.get(
                "liquidations_24h_usd"
            ) is None:
                reasons.append("derivatives_metrics_null")
                critical = True

    if cfg.require_macro:
        if macro is None:
            reasons.append("macro_missing")
            critical = True
        else:
            ts = int(macro.get("timestamp") or 0)
            if now - ts > max_age_ms:
                reasons.append(f"macro_stale_ms={now - ts}")
                critical = True
            if macro.get("btc_dominance_pct") is None:
                reasons.append("macro_dominance_null")
                critical = True

    force_flat = bool(reasons)
    return MarketSafetyResult(
        ok=not force_flat,
        force_flat=force_flat,
        critical=critical and force_flat,
        reasons=reasons,
    )


def apply_safety_to_analysis(
    analysis: dict[str, Any],
    safety: MarketSafetyResult,
) -> dict[str, Any]:
    """Force FLAT when unsafe; preserve id material by rebuilding fields."""
    if not safety.force_flat:
        return analysis
    out = dict(analysis)
    out["action"] = "flat"
    out["size_pct_equity"] = 0.0
    out["invalidation_price"] = None
    reason = "; ".join(safety.reasons) or "market_safety"
    thesis = str(out.get("thesis") or "")
    veto = f" Market safety veto ({reason}); FLAT."
    if veto.strip() not in thesis:
        out["thesis"] = (thesis + veto).strip()
    # Cap confidence at threshold so enqueue never treats as actionable.
    conf = float(out.get("confidence_score") or 0.0)
    out["confidence_score"] = min(conf, 0.54)
    return out


def maybe_engage_kill_switch(safety: MarketSafetyResult) -> MarketSafetyResult:
    """Engage kill-switch when gate reports critical; never auto-clear."""
    if not safety.critical:
        return safety
    reason = "market_safety:" + (";".join(safety.reasons) or "critical")
    engaged = engage_kill_switch(reason=reason[:500])
    safety.kill_switch_engaged = engaged
    return safety


def evaluate_from_db(
    conn: Any,
    symbol: str,
    *,
    skip_book: bool = False,
    skip_derivatives: bool = False,
    skip_macro: bool = False,
    now_ms: int | None = None,
    provider_errors: list[str] | None = None,
    engage_ks: bool = False,
) -> MarketSafetyResult:
    """Load latest snapshots from SQLite and evaluate."""
    from jarvise_ingest.db import (
        latest_derivatives_as_of,
        latest_macro_sentiment,
        latest_order_book,
    )

    cfg = load_market_safety_config(
        skip_book=skip_book,
        skip_derivatives=skip_derivatives,
        skip_macro=skip_macro,
    )
    book = None if skip_book else latest_order_book(conn, symbol)
    deriv = None if skip_derivatives else latest_derivatives_as_of(
        conn, symbol, as_of_ms=now_ms
    )
    macro = None if skip_macro else latest_macro_sentiment(conn)
    result = evaluate_market_safety(
        book=book,
        derivatives=deriv,
        macro=macro,
        config=cfg,
        now_ms=now_ms,
        provider_errors=provider_errors,
    )
    if engage_ks:
        return maybe_engage_kill_switch(result)
    return result
