# Hermes Stage A — dormant OpenClaw replacement path

Status: **implementation only**. Production OpenClaw remains the digest producer. No pilot, n8n import, VPS `.env` edit, or container replacement is authorized by this document alone.

## Pins (resolved 2026-11-26)

| Item | Value |
| --- | --- |
| Image tag (human) | `nousresearch/hermes-agent:v0.23.4` |
| Manifest list digest | `sha256:ef0650d523ee1b5ab32c779acabc446d9936869a9932591553688a62bcf0c42f` |
| Compose pin (linux/amd64) | `nousresearch/hermes-agent@sha256:3bc37fd8546e90fc41fe0d29511b4052840c281617cbcec6093bfe03d86a4ad9` |
| Source revision | `8fc0374447313e276f6b9a2764bfc750190c966a` |
| Runtime user | `hermes` UID/GID `10000` |
| Entrypoint | `/opt/hermes/docker/entrypoint-dispatch.sh` (s6; do not override) |
| Plugin | `mcp-serve` 1.0.1 at `NousResearch/hermes-official-plugins` commit `80025332933bf84fb3c7b68f369d9fe59a771e98` |
| MCP mode | `hermes mcp-serve --tools-only` (no conversation bridge, no `messages_send`, no `permissions_*`) |

Local image inspect confirmed the version string and UID. A full supervised gateway start was not left running in CI; prove start/restart on an isolated host before Stage C.

`HERMES_PROVIDER` and `HERMES_MODEL` have no silent default. Empty values make `/ingest` return `503 provider_unconfigured`. Do not use `openrouter/openrouter/auto`.

## Capability matrix

| Capability | Stage A |
| --- | --- |
| Public HTTPS web research via CONNECT proxy | Yes (Hermes → `hermes-egress`) |
| Digest Markdown write | Adapter only (`hermes-ingest` → `pilot-digests`) |
| Canonical `data/openclaw/workspace-ingest/digests` | Still OpenClaw; Hermes has no mount |
| Messaging / Telegram / Discord | Disabled |
| Approvals / shell / terminal / file / orders | Disabled in template + MCP `--tools-only` |
| Qdrant / jobs / web / Redis / ledger | No network path (`hermes_net` / `hermes_ingress` only) |
| Host port 8642 | Not published (LearningYard owns `127.0.0.1:8642`) |
| Cursor MCP | Opt-in SSH stdio example only |

## Resource bounds

| Service | mem | cpus | pids |
| --- | --- | --- | --- |
| hermes | 3072m | 1.5 | 256 |
| hermes-ingest | 256m | 0.25 | 64 |
| hermes-egress | 128m | 0.25 | 64 |
| **sum** | **3456m** | **2.0** | — |

OpenClaw remains 4g / 2.0 until Stage C retirement. A concurrent pilot needs free host RAM beside both stacks.

## Skill inventory

| OpenClaw skill | Stage A disposition |
| --- | --- |
| `jarvise-news-ingest` | Ported into `config/hermes/ingest-prompt.md` |
| `jarvise-doctrine-rag` | Retained by Python (`jarvise rag sync-openclaw`) |
| `jarvise-signal-analyzer` / `jarvise-risk-guard` / `jarvise-paper-research` / `jarvise-binance-intel` | Retained by Python |
| `jarvise-order-router` | Never port |
| `crypto-market-intel` / `blackswan` / `secretclaw` / `ggshield-scanner` / `claw-compactor` | Deferred |
| Missing required digest capability | Blocks OpenClaw retirement |

## Shadow-study continuity gate

Pre-registration: `docs/research/2026-10-17-openclaw-shadow-veto-plan.md` §C.

- Window start: `2026-10-18T06:00:00Z`
- Close: 40 scored entries **or** 60 days (optional 30-day extension needs owner)
- On 2026-11-26 the window can still be open

Stage A does **not** query production. Canonical cutover stays blocked until a later read-only check on merged tooling reports the window closed, or the owner documents an explicit deviation. Never pool evidence across the producer boundary or reset/extend the study silently.

## Activation (Stage C — owner instruction required)

1. `wdpy` until CI/Deploy idle. Confirm Claude Stage B approval.
2. Refresh runtime SHA, health, free RAM, workflow IDs, universe (`jarvise universe show --json`), and shadow gate.
3. Owner sets `JARVISE_HERMES_API_KEY`, `JARVISE_HERMES_INGEST_TOKEN`, `HERMES_PROVIDER`, `HERMES_MODEL` in VPS `.env` (agents report set/EMPTY only).
4. Start opt-in stack from merged code: `docker compose --profile hermes up -d` (with prod overlay on VPS).
5. Produce one pilot digest under `data/hermes/pilot-digests` only. Validate auth, timeout, tools, redeploy ownership.
6. Quiesce only `jrvsNewsIngest0719`. Render/import Hermes workflow with `render-n8n-hermes-ingest.sh`; keep inactive until the writer is ready.
7. Single writer: point adapter at the canonical digest directory, activate Hermes schedule, test one new slot without overwriting history.
8. Observe two normal 07:00/19:00 runs plus reaction/RAG. Then stop OpenClaw with its 90s grace; move it to a rollback-only profile in a reviewed change.
9. Set `JARVISE_DIGEST_PRODUCER=hermes` only after Hermes is the sole canonical producer.

## Rollback

Triggers: missed/duplicate slots, auth/tool exposure, historical-file changes, repeated provider failures, freshness/parse/RAG regressions, resource pressure, LearningYard/safety impact.

Order:

1. Disable only the Hermes ingest workflow; wait/cancel in-flight adapter work.
2. Stop `hermes-ingest` so it cannot publish later.
3. Restore the pinned OpenClaw workflow/publication state; start OpenClaw.
4. Verify OpenClaw is the sole canonical producer; re-enable `jrvsNewsIngest0719`.
5. Keep already-published valid Hermes files immutable; mark the producer boundary in metadata.
6. Keep trading/safety jobs running. Never `compose down -v`, never prune volumes, never restore the trading ledger for this rollback.

## Owner gates before any pilot

- [ ] Three Hermes secrets set (API key, ingest token, provider model pin)
- [ ] Free RAM confirmed beside still-running 4 GiB OpenClaw
- [ ] Shadow window status decided
- [ ] Claude Stage B approval on the PR
- [ ] Explicit owner cutover / `fdpy` instruction for Stage C

## MCP manual check (when profile is running)

```powershell
# From an approved SSH session into the hermes container:
# hermes mcp-serve --tools-only → tools/list
# Expect: web_search / web_extract / skill_* only among research tools
# Expect absent: messages_send, permissions_*, terminal, file, execution, order tools
# Expect: internal URL (http://127.0.0.1, http://jobs:8090) refused by egress
```

If the hermes profile is not running, record that the live `tools/list` check was not executed.

## Deploy ownership note

`vps-deploy.sh` chowns all of `data/` to `JARVISE_UID`, then re-chowns **only** `data/hermes/gateway` to `HERMES_UID` (10000). Adapter and pilot digests stay on `JARVISE_UID`. Canonical Stage C publication must keep the adapter on `JARVISE_UID` against the existing digest directory without rewriting historical file ownership/mtimes.
