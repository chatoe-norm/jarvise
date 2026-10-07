# Disaster recovery — Jarvise VPS

**Scope:** `/opt/jarvise` on Hostinger KVM `srv1811101` (Tailscale `100.89.171.36`, public `76.13.218.48`). Co-tenant LearningYard is out of scope and must never be touched by these scripts. Former host `srv1269762` (T4Trip) is also out of scope.

## Objectives

| Item | Target | Why |
|------|--------|-----|
| RPO (data loss) | ≤ 24 h | Nightly backup; paper fills at most every 4 h, live orders are also recorded on the venue |
| RTO (time to serve) | ≤ 60 min | Scripts restore SQLite + volumes; redeploy from `main` rebuilds images |
| Safety after restore | Kill-switch engaged until reviewed | Redis state returns with the backup; the owner clears it on Ops after a written review |

## What is backed up

[`infra/backup/backup.sh`](../../infra/backup/backup.sh) writes `/opt/jarvise/backups/YYYY-MM-DD/`:

| Artifact | Source | Method |
|----------|--------|--------|
| `jarvise.db.gz` | `data/analytics/jarvise.db` (market data, paper ledger, approvals, live_orders, LLM reviews) | SQLite online `.backup` + `PRAGMA integrity_check`; works under WAL, no downtime |
| `qdrant.tgz` | `jarvise_qdrant` volume (doctrine RAG) | Container stopped ~seconds, tar of storage |
| `redis.tgz` | `jarvise_redis` volume (kill-switch, job status) | `BGSAVE` then tar |
| `openclaw.tgz`, `sources.tgz` | `data/openclaw`, `data/analytics/sources` | tar |
| `SHA256SUMS`, `MANIFEST` | — | checksums, git SHA, host, UTC time |

Retention: newest 14 days locally (`JARVISE_BACKUP_KEEP`). Off-box copy via `rsync` to `JARVISE_BACKUP_DEST` when set in `/opt/jarvise/.env` (set it to a host or mount the VPS can reach; the Hostinger weekly snapshot is a second layer, not a substitute).

Secrets (`.env`, `secrets/`) are **not** in the archive by design — restore them from the owner's password manager.

## Schedule (root crontab on the VPS)

```bash
# Nightly 03:15 UTC (10:15 Asia/Bangkok)
15 3 * * * cd /opt/jarvise && bash infra/backup/backup.sh >> /var/log/jarvise-backup.log 2>&1
# Weekly retention prune of unbounded market tables (Sunday 03:45 UTC)
45 3 * * 0 cd /opt/jarvise && docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T jobs jarvise db prune --older-than 180d --json >> /var/log/jarvise-prune.log 2>&1
```

Verify a backup any time without touching the stack:

```bash
bash infra/backup/restore.sh --from /opt/jarvise/backups/$(date -u +%F) --verify
```

## Restore procedures

### A. Corrupt or lost SQLite only

```bash
cd /opt/jarvise
bash infra/backup/restore.sh --from backups/2026-10-03 --sqlite
```

Stops `jobs`, `web`, `n8n`; keeps a `jarvise.db.pre-restore.<ts>` copy; removes stale `-wal/-shm`; starts services. Opening the DB re-applies pending migrations (`PRAGMA user_version`).

### B. Full VPS loss (rebuild)

1. Provision Ubuntu 24.04, install Docker + Tailscale, join the tailnet; follow [hostinger-vps.md](../deploy/hostinger-vps.md) §1–3.
2. `git clone` the repo to `/opt/jarvise`; restore `.env` and `secrets/` from the password manager (`JARVISE_LIVE_TRADING=false`).
3. `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build` (creates empty volumes).
4. Copy the latest backup directory from the off-box target, then:

   ```bash
   bash infra/backup/restore.sh --from /opt/jarvise/backups/<date> --all
   ```

5. Reinstall the self-hosted runner (`/opt/actions-runner`) so deploys resume.
6. Re-import n8n workflows from `infra/n8n/workflows/` (they are files, not in the n8n volume unless `redis.tgz`/`jarvise_n8n` were restored).

### C. Qdrant only

`restore.sh --qdrant`, or simply `POST /jobs/rag-refresh` — doctrine is rebuilt from `data/analytics/sources` (allowlisted extracts) in minutes, so the Qdrant archive is convenience, not truth.

## After any restore — smoke checklist

1. `docker compose ... ps` all healthy; `curl -fsS http://$TAILSCALE_IP:8080/healthz`.
2. Ops page: **kill-switch state**. Expect engaged (restored from Redis) or `unknown` if Redis is still starting. Clear only after the written review; approve is blocked until then.
3. `docker compose exec -T jobs jarvise db status --json` → `user_version` matches `schema_version_expected`, row counts sane.
4. `docker compose exec -T jobs jarvise paper status --json` → ledger equity and positions match the last known state.
5. If live was ever enabled: `jarvise trade reconcile --json` → `open: 0` or fills reconciled against the venue.
6. `POST /jobs/ingest-health` → no stale/gap alerts after the first ingest.
7. Telegram: send a test via `python scripts/telegram_chat_id.py` or wait for the next pending alert.

## Failure modes this covers

| Scenario | Covered by |
|----------|------------|
| Disk corruption of `jarvise.db` | A (nightly, integrity-checked copy) |
| Bad migration / schema drift | A (pre-restore copy kept; repairs re-run on open) |
| Redis volume loss (kill-switch unknown) | Services fail closed; B/`--redis` restores state |
| Qdrant loss | C |
| Whole VPS loss | B + off-box copy |
| Operator error during deploy | `vps-deploy.sh` prints image digests; roll back per [ops.md](../deploy/ops.md) |
