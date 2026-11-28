# Hermes Stage A — dormant OpenClaw replacement path

Status: **implementation only**. Production OpenClaw remains the digest producer. No pilot, n8n import, VPS `.env` edit, or container replacement is authorized by this document alone. **Stage C remains blocked** until owner gates below are cleared and Claude Stage B has approved the shipped SHA.

## Pins (resolved 2026-11-26)

| Item | Value |
| --- | --- |
| Image tag (human) | `nousresearch/hermes-agent:v0.23.4` |
| Manifest list digest | `sha256:ef0650d523ee1b5ab32c779acabc446d9936869a9932591553688a62bcf0c42f` |
| Compose pin (linux/amd64) | `nousresearch/hermes-agent@sha256:3bc37fd8546e90fc41fe0d29511b4052840c281617cbcec6093bfe03d86a4ad9` |
| Source revision | `8fc0374447313e276f6b9a2764bfc750190c966a` |
| Runtime user | `hermes` UID/GID `10000` |
| Entrypoint | `/opt/jarvise/hermes-entrypoint.sh` → image `/opt/hermes/docker/entrypoint-dispatch.sh` (s6; do not drop dispatch) |
| Gateway command | `gateway run` (empty image `Cmd` would fall through to chat) |
| Plugin | Vendored `config/hermes/plugins/mcp-serve/` = official-plugins commit `80025332933bf84fb3c7b68f369d9fe59a771e98` (`mcp-serve` 1.0.1). Init restores from the read-only vendor when the **tree hash** differs; probes run as uid `hermes`; no install from floating `main`. |
| MCP mode | `hermes mcp-serve --tools-only` (no conversation bridge, no `messages_send`, no `permissions_*`) |
| HTTP `/v1/runs` toolsets | `web` only (`skills` removed — `skill_manage` is a persistent write path). MCP may still list `skill_view` / `skills_list`. |

`HERMES_PROVIDER` and `HERMES_MODEL` have no silent default. Empty values make `/ingest` return `503 provider_unconfigured`. `fallback_providers: []` in the template. Do not use `openrouter/openrouter/auto`. Hermes takes its provider credential from the host variable `JARVISE_HERMES_OPENROUTER_KEY`, which compose injects as `OPENROUTER_API_KEY` inside the `hermes` container only. Jobs and OpenClaw keep the shared `OPENROUTER_API_KEY`. The Hermes key does not fall back to that shared key, and it is not `JARVISE_HERMES_API_KEY` (the adapter↔gateway password). The owner caps the Hermes key on the OpenRouter account (intended cap $50). That cap is not a Jarvise runtime constant.

**Config seeding:** a missing `data/hermes/gateway/config.yaml` is copied from `config/hermes/config.yaml.template`. An existing file is validated by `infra/docker/hermes_config_toolsets.py`: strip only an exact list item `skills` under `platform_toolsets.api_server` / `cli`, then require each set to be exactly `{web}`. Prefers `yaml.safe_load` when PyYAML is present (CI); the pinned Hermes image has no PyYAML, so a structural subset parser handles block/flow lists for those two keys only — it does **not** refuse merely because an unrelated nested `skills:` key exists, and it is not the old text-regex check. Unsafe toolsets (`terminal`, `file`, `skill_manage`, …) refuse start without rewriting. When already valid, the file bytes are left unchanged so model/provider/credential keys are stable.

## Network shape (R1)

| Network | Internal | Members |
| --- | --- | --- |
| `hermes_net` | yes (IPv6 off) | `hermes`, `hermes-egress` |
| `hermes_api` | yes (IPv6 off) | `hermes`, `hermes-ingest` |
| `hermes_ingress` | yes (IPv6 off) | `hermes-ingest`, `n8n` |
| `hermes_egress` | **no** (sole public Hermes route) | `hermes-egress` only |

`HTTPS_PROXY` / `ALL_PROXY` point at `hermes-egress:8899` as a convenience. The control is the internal networks: gateway has no default route; only the proxy attaches to `hermes_egress`.

## Publication and slots (R5/R6)

- Create-if-absent: cross-process file lock, fully written + fsynced temp (`0644`), then `os.link`. Linux fails closed if hard links are unavailable (no partial final `*.md`). Matching hash replays; differing bytes → `filename_conflict`.
- Provenance sidecar (slot, filename, sha256, length) is written before the link. A crash after link and before the slot record is recovered by adopting the file when the sidecar matches — without another Hermes call. Empty/truncated/untrusted files fail closed.
- Smoke slots use distinct `smoke-YYYY-MM-DD-HH.md` names, require `JARVISE_HERMES_SMOKE=1`, and are refused unless the digest directory’s **final** name is `pilot-digests`.
- Normal slots are admitted only for the **current** Asia/Bangkok window (`07` from 07:00–19:00, `19` from 19:00–next 07:00). Historical/future labels return `400`.
- One adapter-wide **cross-process** in-flight lock: a second distinct slot gets `429`. Idempotency keys are `{slot}-a{n}` with `n` capped at 2. A new attempt is minted only after a **verified** terminal failure (`failed` / `cancelled` / …). Transport/`provider_error` mid-poll stores `unknown` with the same `run_id` and does not mint `-a{n+1}`. A lost create response (no `run_id`) stores `create_unconfirmed` and the next call reuses the **same** Idempotency-Key. Gateway status `unknown` is non-terminal. On deadline the adapter calls `POST /v1/runs/{id}/stop`; still-running/unknown blocks a replacement run. `started_at` is persisted with the running record so resume past the deadline stops before accept. Hermes `Idempotency-Key` retention (~24h) is not the publication lock.

## Digest schema and sources (R7)

- `no_news` must be a real boolean (`"false"` is invalid).
- Titles/comments/URLs reject control characters and line separators; URLs must be printable ASCII `[\x21-\x7e]+` and pass the allowlist on the exact rendered string. Titles may not contain `**`. Comments may not start with `http(s)://` or the research footer.
- Adapter source validation is **syntax + allowlist only** (no DNS). Authority may not contain `\`, `%`, or `;`; hostname must match a strict DNS-label grammar before suffix matching (blocks `evil.com\.nvidia.com`-style parser differentials). DNS/IP checks stay on the CONNECT proxy. Experiment (2026-11-27): `getaddrinfo` from an adapter container on an `internal: true` network failed with `gaierror` for `federalreserve.gov`.
- `config/hermes/source-policy.json` denies shorteners and IP-literal hosts. The allowlist includes watchlist publishers **and** secondary outlets (`reuters.com`, `cnbc.com`, …) plus `google.com` (includes user-content subdomains) by owner policy. Other hosts → `unknown_source` and the **whole slot** is rejected (dropping unknown items is a later owner decision, not Stage A default).
- CONNECT uses the same file. `JARVISE_HERMES_CONNECT_ALLOWLIST` defaults to `/config/hermes/source-policy.json` inside `hermes-egress` (the mounted repo file). A host is allowed only when it equals an `allow_suffixes` entry or is a subdomain of one (`www.nvidia.com` matches `nvidia.com`; `notnvidia.com` does not). `deny_hosts` and `deny_suffixes` still deny. Port must stay 443, and private, loopback, link-local, and metadata addresses stay blocked. A missing, empty, unreadable, or non-object file denies every CONNECT (`connect_allowlist_closed`). An empty `JARVISE_HERMES_CONNECT_ALLOWLIST` value also denies. There is no fail-open default.
- After render, `parse_digest_text` must recover the same item count, title, and URL.

## Runtime identity (R8)

Completed runs without `runtime.provider` and `runtime.model` fail closed (`provider_mismatch` / `model_mismatch`). Exact normalized match only (no `str.endswith`). Differing `runtime.requested` is substitution and rejected. **All** completed paths — including restart resume — go through shared `finalize_completed` (budget-exhausted check then identity). Configured env is never substituted for missing actual runtime. Fixture `tests/fixtures/hermes_v0.23.4_run.json` is **source-derived** (not a captured live response); `route_source` is accepted as an ignored extra.

## Capability matrix

| Capability | Stage A |
| --- | --- |
| Public HTTPS web research via CONNECT proxy | Yes, port 443 and `allow_suffixes` only (Hermes → `hermes-egress`) |
| Digest Markdown write | Adapter only (`hermes-ingest` → `pilot-digests`) |
| Canonical `data/openclaw/workspace-ingest/digests` | Still OpenClaw; Hermes has no mount |
| Messaging / Telegram / Discord | Disabled |
| Approvals / shell / terminal / file / orders | Disabled in template + MCP `--tools-only` |
| Qdrant / jobs / web / Redis / ledger | No network path (internal Hermes nets only) |
| Host port 8642 | Not published (LearningYard owns `127.0.0.1:8642`) |
| Cursor MCP | Opt-in SSH stdio example only (`scripts/hermes-mcp-stdio.ps1`) |

## Resource bounds

| Service | mem | cpus | pids |
| --- | --- | --- | --- |
| hermes | 3072m | 1.5 | 256 |
| hermes-ingest | 256m | 0.25 | 64 |
| hermes-egress | 128m | 0.25 | 64 |
| **sum** | **3456m** | **2.0** | — |

OpenClaw remains 4g / 2.0 until Stage C retirement. A concurrent pilot needs free host RAM beside both stacks.

## Local isolation proof

```bash
# From the worktree, Docker Desktop required. Synthetic env only.
bash scripts/hermes-isolation-check.sh
# or: JARVISE_HERMES_ISO=1 pytest tests/test_hermes_isolation.py -q
```

The script cold-starts a throwaway compose project, waits for in-container `http://127.0.0.1:8642/health`, restarts, checks account and gateway-worker UID `10000`, runs `hermes mcp-serve --tools-only` initialize + `tools/list` (fails on forbidden names **or** missing `web_search` / `web_extract`), asserts neither `hermes` nor `hermes-ingest` can open public TCP directly, denies CONNECT to `127.0.0.1` / RFC1918 / link-local metadata and to a public host outside the allowlist (`example.com`), and requires CONNECT to an allowlisted host (`nvidia.com:443`) through the proxy to return HTTP **200** (502 is not success). Config grep for `skills` is not proof of the HTTP `/v1/runs` toolset; that runtime query remains an unrun gap. Remaining nits (documented, not refactored here): egress per-read header timeout is not a total CONNECT ceiling; `printf '%q'` in the `su` fallback is not POSIX.

CI builds `Dockerfile.hermes-ingest` and `Dockerfile.hermes-egress` in addition to worker/web. Worker/web builds alone are **not** proof of Hermes. Plugin restore compares the **installed** tree hash to the read-only vendor (not a writable stamp).

## Shadow-study continuity gate (unresolved)

Pre-registration: `docs/research/2026-10-17-openclaw-shadow-veto-plan.md` §C.

- Window start: `2026-10-18T06:00:00Z`
- Close: 40 scored entries **or** 60 days (optional 30-day extension needs owner)

Stage A does **not** query production. Canonical cutover stays blocked until a later read-only check on merged tooling reports the window closed, or the owner documents an explicit deviation.

## Activation (Stage C — owner instruction required)

1. `wdpy` until CI/Deploy idle. Confirm Claude Stage B approval on the revised SHA.
2. Refresh runtime SHA, health, free RAM, workflow IDs, universe (`jarvise universe show --json`), and shadow gate.
3. Owner sets `JARVISE_HERMES_API_KEY`, `JARVISE_HERMES_INGEST_TOKEN`, `JARVISE_HERMES_OPENROUTER_KEY`, `HERMES_PROVIDER`, and `HERMES_MODEL` in VPS `.env` (agents report set/EMPTY only). `JARVISE_HERMES_OPENROUTER_KEY` is the separate Hermes OpenRouter key. It does not fall back to `OPENROUTER_API_KEY`.
4. Start opt-in stack from merged code: `docker compose --profile hermes up -d` (with prod overlay on VPS).
5. Produce one pilot digest under `data/hermes/pilot-digests` only. Validate auth, timeout, tools, redeploy ownership.
6. Quiesce only `jrvsNewsIngest0719`. Render/import Hermes workflow with `render-n8n-hermes-ingest.sh`; keep inactive until the writer is ready.
7. Single writer: point adapter at the canonical digest directory, activate Hermes schedule, test one new slot without overwriting history.
8. Observe two normal 07:00/19:00 runs plus reaction/RAG. OpenClaw stays running in its current compose service through this step. Retirement waits until every item below is done, and until Chana has given an explicit OK:
   - the shadow study is closed, or the owner has documented a deviation
   - a non-digest OpenClaw function inventory is written
   - parity checks have passed
   - an OpenClaw backup is retained for `JARVISE_HERMES_OPENCLAW_BACKUP_RETENTION_DAYS` (default **30** days; cutover procedure only, not read by the running stack)
   - rollback has been rehearsed
9. Set `JARVISE_DIGEST_PRODUCER=hermes` only after step 8 is complete, including Chana's explicit OK, and a later reviewed change has made Hermes the sole canonical producer. While OpenClaw is still a producer, leave this flag unset.

## Rollback

Triggers: missed/duplicate slots, auth/tool exposure, historical-file changes, repeated provider failures, freshness/parse/RAG regressions, resource pressure, LearningYard/safety impact.

Order:

1. Disable only the Hermes ingest workflow; wait/cancel in-flight adapter work.
2. Stop `hermes-ingest` so it cannot publish later.
3. Restore the pinned OpenClaw workflow/publication state. Step 8 leaves OpenClaw running, so a rollback before a later retirement confirms that OpenClaw is still up and restores its publication state. Start OpenClaw only if a later reviewed change had stopped it.
4. Verify OpenClaw is the sole canonical producer; re-enable `jrvsNewsIngest0719`.
5. Keep already-published valid Hermes files immutable; mark the producer boundary in metadata.
6. Keep trading/safety jobs running. Never `compose down -v`, never prune volumes, never restore the trading ledger for this rollback.

## Owner gates before any pilot (still open)

- [ ] Hermes owner secrets set (`JARVISE_HERMES_API_KEY`, `JARVISE_HERMES_INGEST_TOKEN`, `JARVISE_HERMES_OPENROUTER_KEY`) plus `HERMES_PROVIDER` and `HERMES_MODEL`
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

## Deploy ownership note

`vps-deploy.sh` chowns all of `data/` to `JARVISE_UID`, then re-chowns **only** `data/hermes/gateway` to `HERMES_UID` (10000). Adapter and pilot digests stay on `JARVISE_UID`. Canonical Stage C publication must keep the adapter on `JARVISE_UID` against the existing digest directory without rewriting historical file ownership/mtimes.
