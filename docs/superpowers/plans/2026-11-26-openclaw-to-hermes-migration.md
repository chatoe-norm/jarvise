# Jarvise: replace OpenClaw with Nous Research Hermes Agent

Status: implementation plan; production was inspected read-only. No replacement, image pull, workflow import, or deployment has been performed.

Owner request: plan the replacement of `jarvise-openclaw-1` on Hostinger `76.13.218.48`, then hand implementation to Cursor. The owner confirmed **Nous Research Hermes Agent**.

## 1. Verified baseline — 2026-11-26, approximately 22:29 Asia/Bangkok

| Item | Observed state |
| --- | --- |
| Host | `Jarvise-VPS`, hostname `srv1811101`, `/opt/jarvise`; KVM 1811101 |
| VPS checkout at first inspection | `de93d7316ac3fd27cba7a4e9237544b91495bd38` |
| GitHub main at later inspection | `662d6a0ee868ba89499b4c3592665dd52c356542` (merge #375); deployment may be advancing |
| Local checkout | `0ebabfdb30e0a4ed4389de6d982ea781f1849e1b`, branch `docs/assist-signal-edge-2026-11-10`; substantially behind main |
| Local changes to preserve | Modified `AGENTS.md`, modified `CLAUDE.md`, untracked `.claude/`; do not commit, stash, overwrite, or reset these for this task |
| OpenClaw | `jarvise-openclaw-1`, healthy; `ghcr.io/openclaw/openclaw:2026.9.8` |
| Limits / ingress | 4 GiB, 2 CPU, 90-second stop timeout; `100.89.171.36:18789` |
| Isolation | OpenClaw is on `jarvise_openclaw_net`; jobs/web/Redis are on a different network |
| Persistent OpenClaw state | `/opt/jarvise/data/openclaw`, with read-only config and plugin mounts |
| Digests | 55 Markdown files; newest `2026-11-26-12.md`, modified `2026-11-26T12:01:00Z` |
| Ingest workflow | Active `jrvsNewsIngest0719`; 07:00 / 19:00 Asia/Bangkok; `POST http://openclaw:18789/hooks/agent`; timeout 660000 ms |
| News reaction | Active `jrvsNewsReact0749`; 07:30 / 19:30 Asia/Bangkok; jobs endpoint; timeout 480000 ms |
| RAG refresh | Active `35SsQu3qlaETwgF9`; every 6 hours; jobs endpoint; timeout 600000 ms |
| Other active jobs | Paper, risk monitor, assist, shadow veto, forward H3, ingest health, weekly reports; preserve all |
| Host capacity | About 16 GiB RAM, 11 GiB available, 119 GiB disk available at inspection; recheck before a pilot |
| Co-tenant Hermes | `learningyard-hermes`, image `learningyard-hermes:local`, separate Compose project, 1 GiB limit, host `127.0.0.1:8642` already occupied |

The owner-controlled universe was read using the required merged CLI: `paper_liquid`, 4h, seven active symbols. This migration does not change the universe. Refresh that read for any later symbol-dependent work; this snapshot is not a new symbol configuration.

These are point-in-time observations, not a claim that VPS and GitHub main are synchronized. Refresh main, deployment queue, and runtime SHA before implementation/deployment. Do not run `git pull`, checkout a feature branch, or run unmerged code on production to make the snapshot match.

## 2. Decision and upstream compatibility

Use an isolated **Jarvise Hermes gateway plus an ingest adapter**, with a separate MCP stdio connection for Cursor. Keep n8n as the sole scheduler. Do not share LearningYard's Hermes container, credentials, state, or networks.

The release API reported Hermes `v0.23.4`, published 2026-11-24, source commit `8fc0374447313e276f6b9a2764bfc750190c966a`. Treat it as the candidate, not a tested deployment. Resolve its official image and immutable digest; verify architecture, source labels, license, and startup against that release. Never invent an image tag or use `latest`/`main`. If no matching published image exists, document and review a reproducible pinned-source build before proceeding. [Official release](https://github.com/NousResearch/hermes-agent/releases/tag/v0.23.4)

Hermes supports a gateway HTTP API, while the official `mcp-serve` plugin exposes a stdio MCP tool surface. These are different interfaces. The MCP interface does not provide a generic agent-delegation tool and does not replace OpenClaw's `/hooks/agent` API. [API documentation](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server)

The candidate release's catalog pins `mcp-serve` 1.0.1 to official-plugins commit `80025332933bf84fb3c7b68f369d9fe59a771e98`. Use `hermes mcp-serve --tools-only`: it omits conversation reads, message sending, and approval responses. Enable only web research and skill-reading capabilities; disable image/audio/vision and other unused toolsets. Check the actual `tools/list` result. The plugin's web extraction can reach local network addresses, so its curated tool list alone does not provide network isolation. [Pinned plugin documentation](https://github.com/NousResearch/hermes-official-plugins/blob/80025332933bf84fb3c7b68f369d9fe59a771e98/mcp-serve/README.md)

The upstream importer can migrate settings, skills, and optionally secrets. Do not run it against the production state or use automatic setup/import: runtime config can contain credentials and unsafe legacy tools. Port reviewed, non-secret instructions manually; leave the original state intact. [Migration documentation](https://hermes-agent.nousresearch.com/docs/guides/migrate-from-openclaw)

The container persists state through `/opt/data`. Preserve the selected image's entrypoint/supervision requirements and validate UID/GID behavior before choosing hardening settings. Root bootstrap, if required by the image, must drop the actual gateway/MCP processes to the documented non-root user. [Docker documentation](https://hermes-agent.nousresearch.com/docs/user-guide/docker)

## 3. Target architecture and contracts

```text
n8n (existing schedules)
  -> authenticated hermes-ingest adapter (fixed POST /ingest)
       -> authenticated Hermes gateway API (internal container port 8642)
       -> validate output, render Markdown, publish one digest atomically
            -> existing read-only /digests mount in jobs

Cursor -> SSH stdio -> docker compose exec -T --user hermes
                         hermes hermes mcp-serve --tools-only
```

### Service and network design

- Add `hermes` and `hermes-ingest` services under an opt-in `hermes` Compose profile for Stage A. Leave OpenClaw and the production ingest workflow active until cutover. Use separate pilot output under `data/hermes/pilot-digests`; it must not be visible to production consumers.
- Give Hermes its own `data/hermes` state directory and dedicated networks. It must not join `jarvise_net`, `openclaw_net`, or any LearningYard network. Give the adapter an ingress connection reachable by n8n plus a private gateway connection. Do not give Hermes direct Qdrant, jobs, web, Redis, or n8n access.
- Enforce public-web-only tool requests, including redirects, IPv6, loopback, private/link-local addresses, DNS rebinding, metadata endpoints, and the host's public/Tailscale addresses. Use a tested constrained fetch implementation or an egress proxy/firewall that also covers fallback tools. Disable any tool that bypasses the restriction. Prompt text is not this control.
- Keep API port 8642 internal to Docker. Do not publish host 8642: LearningYard already owns it. MCP uses SSH, so needs no published HTTP MCP port. Keep the dashboard off initially; any future UI requires authenticated private ingress and its own unused port.
- No privileged mode, host networking, Docker socket, host root mount, SSH keys, trading credentials, ledger mount, or NotebookLM credential mount. Only the digest adapter gets a narrow digest-output mount. Hermes gets no canonical digest/export mount and no host config/secrets directories.
- Budget the complete new stack, including adapter/proxy, within the old 4 GiB / 2 CPU envelope initially. Set pids limits and bounded log rotation. Validate `no-new-privileges` and other settings with the official image's startup rather than breaking supervision. Avoid automatic updates and runtime plugin downloads on every restart.
- `vps-deploy.sh` currently recursively chowns `data/` to the Jarvise UID. Explicitly accommodate Hermes ownership after that step, scoped to `data/hermes`; prove a normal redeploy preserves both services' writable state. Never recursively change LearningYard or archived OpenClaw ownership for this purpose.

### Fixed ingest adapter

Implement a small deterministic adapter, proposed as `src/jarvise_intel/ingest.py` plus `infra/docker/Dockerfile.hermes-ingest`. Keep Hermes dependencies outside the Jarvise worker/web lockfiles. Prefer existing libraries or Python stdlib; do not import trading/ledger modules.

1. Expose readiness and authenticated `POST /ingest`. Request fields are a fixed scheduled slot, not arbitrary prompts, tools, paths, model names, callbacks, or shell commands. Missing authentication configuration fails closed. Validate body size, slot, content type, and concurrent requests.
2. Call the candidate release's verified authenticated gateway API with a fixed research prompt and reviewed watchlist. Set overall run deadline at 600 seconds; preserve n8n's 660000 ms timeout. Treat cancellation, incomplete output, max-turn exhaustion, malformed responses, provider errors, and timeouts as failures even when HTTP is 200.
3. Require at most eight sourced items with valid timestamps and primary-source HTTPS URLs. Validate against an explicit source policy. Empty confirmed coverage may publish a clearly labeled no-news result; errors must not become a successful empty digest. Treat retrieved text as untrusted research material.
4. Render the exact digest structure expected by `parse_digest_text` in `src/jarvise_context/news_sources.py`, with the existing research-only footer and no order/size/approval advice. Add fixtures against that parser before choosing the output schema. The adapter writes the file; do not enable Hermes shell/filesystem tools to do it.
5. Use UTC `YYYY-MM-DD-HH.md` filenames, a durable per-slot lock/idempotency record in adapter-owned state, a temporary file outside the `*.md` scan, fsync, then atomic publication. A completed slot is immutable; retry returns its result without another model call. Concurrent/restarted requests must not publish twice. Fail closed on an existing filename conflict. Do not synthesize historical files or alter publication mtimes.
6. Record provider/version, model, requested slot, actual publication time, source URLs, output hash, duration, usage/cost where available, and failure category in adapter-owned metadata outside the digest folder. Never log tokens, raw secret-bearing config, full prompts/responses, or trading/account data.
7. Restrict request frequency to the existing two daily slots plus explicit owner-approved smoke tests. Bound context, tool calls, parallelism, retries, and tokens. Retain the current approved provider/model intent; no silent model/provider substitution. Missing provider configuration is reported, not fixed by enabling another paid provider.

### Data, RAG, and experimental continuity

- Keep jobs' `/digests:ro` contract and initially keep the host canonical directory `data/openclaw/workspace-ingest/digests`. The legacy directory name is a compatibility boundary, not a running OpenClaw dependency. At cutover the adapter becomes its sole producer. Protect all existing files and mtimes; never backfill, overwrite, rename, or resync historical digests.
- `shadow_veto.newest_digest` selects by publication mtime and stores filename/hash. Keep that code and stored verdicts unchanged. The producer replacement changes research context: before cutover, inspect the pre-registered shadow-window status through approved merged tooling. If still open, defer canonical cutover until it closes, or obtain an explicit owner decision documenting closure/deviation and separate reporting. Never silently pool evidence across the producer boundary or reset/extend the study.
- Preserve `jarvise rag sync-openclaw`, existing `source_kind`, IDs, source paths, and `data/openclaw/exports/tactics` compatibility. Python post-mortem generation continues to own approved tactic exports. Hermes news, private memory, conversations, and pilot results must never enter `jarvise_doctrine` or SQLite market truth. Give Hermes no general export-directory write access.
- Do not mechanically translate every OpenClaw plugin. Port ingest/watchlist and safe research instructions; classify the other skills as retained by Python, ported, deferred, or retired. Any required capability without a replacement blocks retirement and must be reported. Do not bring order-router, ledger-write, Redis, shell, or trading tools into Hermes.
- Inventory `src/jarvise/model_routing.py`, `/api/openclaw/model-routing`, Telegram digest-staleness messages, and consuming web components. Preserve API compatibility while reporting the actual active backend and unknown/unconfigured states honestly. A dormant pilot must not make the dashboard claim Hermes is production. Do not create a fictitious chief/researcher/writer/reviewer team.
- Preserve the news-reaction, RAG-refresh, assist, weekly, and safety schedules. No Hermes cron or Telegram bindings. In particular, do not reuse the assist bot token or compete with its listener.

### Credentials and Cursor MCP configuration

- Owner edits VPS `.env` and secret files only. Supply templates/key names and report only `set` / `EMPTY`; never read or print values. Do not copy the full OpenClaw config or mount its state into Hermes, even read-only.
- Proposed dedicated names: `JARVISE_HERMES_API_KEY` (forward as Hermes `API_SERVER_KEY`, and to adapter only), `JARVISE_HERMES_INGEST_TOKEN` (adapter/n8n only), and a pinned `HERMES_IMAGE`. Provider keys are an explicit minimal allowlist. No Binance, Telegram, Redis, n8n admin, NotebookLM, or trading key is forwarded.
- Add an idempotent non-secret state/config initializer and a targeted n8n renderer. Render only the new ingest token placeholder through the existing protected operational pattern; temporary rendered files are root-only and removed. Never commit resolved Compose output, rendered workflows, or credentials.
- Provide an opt-in Cursor MCP example and a Windows wrapper, proposed `scripts/hermes-mcp-stdio.ps1`. The wrapper invokes `ssh -T Jarvise-VPS` and absolute Compose file paths under `/opt/jarvise`, then `exec -T --user hermes hermes hermes mcp-serve --tools-only`. Verify the selected image's account/command paths. Keep stdout exclusively MCP; diagnostics go to stderr. No PTY and no tokens in client JSON. Do not overwrite the owner's current MCP configuration or globally auto-approve tools.
- Test MCP initialization, discovery, one permitted public web call, rejected internal-URL calls, and absence of messaging, approvals, terminal, file, execution, and order tools. This connection supplies research tools, not a remote execution agent.

## 4. Work sequence for Cursor

### Stage A — implement and open a PR; STOP before merge

1. Read current `AGENTS.md`, `CLAUDE.md`, `.cursor/rules/model-tiers.mdc`, `.cursor/rules/fdpy-wdpy.mdc`, this plan, and the files in section 6. Use Grok for implementation under the owner's model rules; Claude reviews.
2. Refresh `origin/main` and use an isolated branch/worktree based on it. Preserve this checkout's owner changes and copy only this plan/handoff as needed. Never work from the old local feature baseline or fold its files into the migration PR.
3. Resolve and verify the candidate image/plugin pins. Build the isolated gateway, adapter, templates, MCP wrapper/example, focused tests, and a runbook. Keep the production default on OpenClaw and make opt-in pilot failure harmless to production. Add a documented later activation/retirement path; do not delete the fallback service yet.
4. Preserve compatibility described above. Update deploy initialization/ownership and digest/status tests. Do not perform broad `openclaw` string replacement or alter frozen research/trading code.
5. Run fixture/stub tests and local isolated-container integration checks. No unmerged production-ledger execution. If a DB is necessary for a reader compatibility test, use an online SQLite backup under `/tmp`, set `JARVISE_DB` to that copy, and remove it afterward.
6. Open a PR with pins, scope, validation, capability matrix, operational diff, unresolved gates, and rollback steps. No merge, production pilot, `.env` edit, n8n import, or container replacement in Stage A. Keep unrelated PRs and dependency cleanup outside scope.

### Stage B — Claude review on the same branch

Review secret boundaries, reachable networks, SSRF controls, tool exposure, publication/idempotency, historical mtime protection, model/cost bounds, Docker UID/supervision, deployment behavior, and rollback. Fix findings on the same branch and rerun affected checks. Green CI alone does not satisfy this review. Preserve the owner-required commit trailer from `AGENTS.md`.

### Stage C — reviewed shipping and explicit cutover

Proceed only after Claude approval and an owner `fdpy`/cutover instruction covering the concrete runbook. First `wdpy`: wait for the existing CI/Deploy queue. Shipping dormant implementation must not implicitly switch the production producer.

1. Refresh runtime SHA, health, image digests, free capacity, workflow IDs/publication state, and universe read-only. Confirm shadow-study gate and owner-provided credentials are ready. Record hashes/mtimes of all historical digests.
2. Prepare restricted, restorable OpenClaw state/config and n8n workflow backups, without displaying their contents. Use the supported SQLite online-backup approach where applicable; do not tar-copy active SQLite files as a consistency guarantee. Record original workflow publication state and image pins. Sensitive backup contents remain owner-operated; agents do not inspect them.
3. Start the isolated opt-in Hermes stack only from merged reviewed code. Produce one pilot digest outside canonical data. Validate tools, auth, timeouts, resource usage, restart behavior, and normal redeploy ownership. Check LearningYard and existing Jarvise health before and after.
4. Quiesce only `jrvsNewsIngest0719` and wait for any active run to finish. Import/publish the rendered replacement under the same workflow ID using the deployed n8n version's supported process. Keep it inactive until the canonical writer is ready. Do not bulk import, enable live-reconcile, or interrupt the other schedules. If n8n requires a restart, schedule and verify all other workflows recover before proceeding.
5. Ensure a single writer, switch the adapter from pilot output to the canonical digest directory, and activate the replacement schedule. Test one permitted new slot without overwriting an existing file. Verify publication, jobs reader parsing/freshness, news reaction, and unchanged RAG source separation. Observe two normal 07:00/19:00 scheduled runs, with the usual following 07:30/19:30 reaction runs, plus the next scheduled RAG refresh. No duplicate cron, messages, or files.
6. Stop OpenClaw gracefully with its 90-second allowance; do not early SIGKILL. Move it to an explicit rollback-only profile in the reviewed activation change so future `compose up -d`/`fdpy` cannot restart it accidentally. Retain its image, data, and workflow backup for at least seven days; no pruning/removal of rollback assets or Docker volumes. Complete a normal redeploy smoke test before declaring replacement complete.
7. Report PR, main CI, Deploy results, active producer/image/plugin pins, MCP discovery, scheduled-run proof, health, historical-hash preservation, and remaining owner actions. Never claim “synced” solely from CI or from a successful container start.

### Rollback triggers and order

Rollback for missed/duplicate digest slots, auth/tool exposure, historical-file changes, repeated provider failures, downstream freshness/parse/RAG regressions, excessive resource use, or impact on LearningYard/safety jobs.

Disable only the Hermes ingest workflow; wait/cancel its in-flight adapter run safely; stop the adapter so it cannot publish later. Restore the original rendered workflow/publication state and start the pinned OpenClaw fallback. Verify it is the sole canonical producer before re-enabling the old schedule. Keep already-published valid Hermes files immutable and mark the producer boundary in metadata. Keep all trading/safety jobs running. Roll back through the saved reviewed Compose artifacts/pins; never checkout old code over the production checkout, restore the whole trading ledger, or run `compose down -v` / volume prune.

## 5. Acceptance checks

Run meaningful adapter and integration tests for successful publication, malformed/empty/error output, bounded timeout, 401/403 handling, incomplete HTTP-200 runs, duplicate/concurrent/restarted slots, path traversal, timestamp validation, immutable output, and no secret logging. Include blocked internal/redirect/DNS-rebinding fetches and actual MCP tool discovery. Fixture test the existing news parser and mtime-based shadow selector without changing its rule.

Verify Compose defaults leave the pilot off; all new services lack trading/ledger/secret mounts; ingress is private; the host port collision is avoided; deploy ownership and profile retirement survive a redeploy; normal jobs remain healthy when Hermes is absent. Extend `tests/test_n8n_workflows.py`, `tests/test_ops_watch.py`, `tests/test_model_routing.py`, `tests/test_news_reaction.py`, and `tests/test_rag.py` as applicable. Preserve existing isolation tests for the fallback rather than deleting them.

Repository checks, using the current worktree environment:

```powershell
.venv\Scripts\ruff.exe check src tests scripts
.venv\Scripts\ruff.exe format --check src tests scripts
.venv\Scripts\python.exe -m mypy src
.venv\Scripts\python.exe -m pytest
```

If `web/` changes, also run its `npm run lint`, `npm test`, and `npm run build`. Run existing Docker/image/security CI. Validate Compose without printing expanded secrets: use config validation/quiet mode with synthetic fixture credentials, not production `.env` dumps. Document any image-specific inability to run a check; do not mark it passed.

## 6. Read-first file map

| Area | Files |
| --- | --- |
| Runtime | `docker-compose.yml` services n8n/jobs/openclaw and networks; `docker-compose.prod.yml`; `infra/deploy/vps-deploy.sh` |
| Ingest contract | `config/openclaw/ingest/AGENTS.md`; `plugins/jarvise-openclaw/skills/jarvise-news-ingest/SKILL.md`; `infra/docker/seed-openclaw-ingest-agent.sh` (inspect source only, do not run) |
| Schedules | `infra/n8n/workflows/jarvise-news-ingest.json`, `jarvise-news-reaction.json`, `jarvise-rag-refresh.json`; `infra/docker/render-n8n-jobs-workflows.sh` |
| Digest readers | `src/jarvise_context/news_sources.py`; `src/jarvise_paper/shadow_veto.py` functions `digest_dir` / `newest_digest`; `src/jarvise/jobs.py` ops-watch path; locate freshness helper through `tests/test_ops_watch.py` |
| RAG/tactics | `src/jarvise/rag.py`; `src/jarvise_paper/post_mortem.py`; `src/jarvise/cli.py` RAG commands |
| Owner surfaces | `src/jarvise/model_routing.py`; `src/jarvise_web/app.py` model-routing route; `src/jarvise_notify/telegram.py` digest alerts; relevant consumers in `web/` |
| Prior constraints | `docs/handoff/2026-10-17-openclaw-prune.md`; `docs/research/2026-10-17-openclaw-shadow-veto-plan.md`; `docs/research/2026-10-25-donchian-trail-forward.md`; `docs/product-usage.md` |

## 7. Hard boundaries

This is a research-runtime replacement. Keep Jarvise live autotrade off and preserve existing owner-confirmed assist behavior without reconfiguring it. Do not touch analyzer floors 0.55/0.70, analyzer formula, 1.5× ATR stop, `flat_exit`, `same_side_hold`, profit lock, EV latch, risk/sizing/order paths, universe, or ledger migrations. While forward H3 is open, do not modify `strategies.py`, `replay.py`, `stops.py`, `profit_lock.py`, or `engine.py`. Preserve quiet/weekly mode. Secrets remain owner-edited. No LearningYard changes, production-ledger experiments, unattended execution expansion, or destructive Docker cleanup.
