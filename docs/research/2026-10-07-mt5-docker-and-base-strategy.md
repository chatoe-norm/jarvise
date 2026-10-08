# MT5-Docker notes + Jarvise base strategy (B vs C)

**Research overlay — not trading doctrine.** Do not index this page, MetaTrader / Nautilus / Freqtrade vendor docs, or GitHub issues into Qdrant `jarvise_doctrine`. Do not silent-merge into SQLite or the paper ledger. Do not add MetaTrader5-Docker, NautilusTrader, Freqtrade, ccxt, or Hummingbot as Jarvise dependencies. Do not start P5, do not set `JARVISE_LIVE_TRADING=true`, and do not retune analyzer floors or `same_side_hold`.

Queried **2026-10-07**. Companion gap report: [`2026-10-05-autotrade-stack-gap.md`](2026-10-05-autotrade-stack-gap.md).

---

## MetaTrader5-Docker (what it is)

Source: [gmag11/MetaTrader5-Docker](https://github.com/gmag11/MetaTrader5-Docker) (MIT wrapper; KasmVNC / LinuxServer bases are GPL).

| Piece | Role |
|-------|------|
| Image | `gmag11/metatrader5_vnc` (v2 ~4 GB Debian+Python) or `:1.1` / `:1.0` smaller (MQL-only) |
| Host | **amd64 only** |
| UI | KasmVNC web client on **:3000** (`CUSTOM_USER` / `PASSWORD`) |
| Python bridge | RPyC + [mt5linux](https://github.com/lucas-campagna/mt5linux) on **:8001** |
| Persistence | `/config` volume (Wine + MT5 tree; MQL5 under `config/.wine/drive_c/Program Files/MetaTrader 5/MQL5`) |
| Auto-install | First boot installs/updates MT5; image tag ≠ frozen MT5 build |

**Not a Jarvise crypto base:** MT5 is a broker terminal (FX/CFD/MQL5). It does not own Jarvise’s closed-candle SQLite truth, approval queue, doctrine RAG, market-safety gate, or Command Dashboard. Treat as a possible **future separate lane** for broker/FX only — never as a replacement for the crypto paper path.

OpenClaw on the Jarvise VPS is an agent Control UI (paper-only team). It does **not** run MetaTrader or Nautilus.

---

## Strategy options (definitions)

| Option | Meaning |
|--------|---------|
| **A** | Run MT5-Docker as trading/UI base; put Jarvise brain on top |
| **B** | Adopt NautilusTrader (or same-class crypto bot framework) as OMS/execution base; re-host Jarvise brain |
| **C** | Keep Jarvise as the core stack; borrow ideas from Nautilus / Freqtrade / MT5 as **research overlays only** (same pattern as [`2026-10-05-freqtrade-pairlist-for-jarvise.md`](2026-10-05-freqtrade-pairlist-for-jarvise.md)) |

---

## B vs C — trade-offs

### B — Nautilus (or peer framework) as base

**Pros:** Mature OMS/backtest/multi-venue/risk primitives; clearer “brain vs engine” split in theory; closer to industry bot stacks for heavy live/multi-venue later.

**Cons:** Large rewrite (paper ledger, approval, market_safety, kill-switch, jobs/n8n, Dashboard); fights current `AGENTS.md` ban on Nautilus as execution dependency; heavy PyO3/Rust footprint on a shared 4 vCPU / 16 GB host; does not fix today’s bottleneck (closed `auto_*` n=0); OpenClaw is not a Nautilus runtime (extra process); loses the audited simplicity of httpx + SQLite.

### C — Jarvise core, overlay ideas only

**Pros:** Matches shipped P0–P4-C + Tier 0–2; focuses on paper EV sampling; borrow one idea at a time without silent-merge; keeps kill-switch / approval / doctrine / owner UX as the product; lower ops load on current VPS.

**Cons:** Homegrown OMS must grow for LIMIT/OCO/user-stream/second venue; venue expansion slower than standing on a full framework; long-term reinvention risk if overlays are ignored; insufficient alone if the end-state becomes institutional HFT/multi-venue at Nautilus scale.

---

## Decision (2026-10-07)

**Choose C now.** Do not pursue B until all of the following are true:

1. Closed paper `auto_*` sample is honest (~10+) and EV is readable.
2. Optional P4-C live smoke via [`docs/ops/live-enable-checklist.md`](../ops/live-enable-checklist.md) (flag stays false until the owner enables it).
3. A concrete gap that C cannot reasonably close (e.g. real multi-venue execution) justifies a **separate** B spec.

**Reject A as the crypto base.** MT5-Docker is only worth revisiting for a broker/FX terminal lane separate from crypto spot.

**Do not** add MT5 or Nautilus to `docker-compose.yml`. Status SoT: [`PROJECT_CONTEXT.md`](../../PROJECT_CONTEXT.md) §5.

---

## References

- [MetaTrader5-Docker README](https://github.com/gmag11/MetaTrader5-Docker)
- [NautilusTrader architecture](https://nautilustrader.io/docs/latest/concepts/architecture)
- [`2026-10-05-autotrade-stack-gap.md`](2026-10-05-autotrade-stack-gap.md)
- [`2026-10-05-freqtrade-pairlist-for-jarvise.md`](2026-10-05-freqtrade-pairlist-for-jarvise.md)
