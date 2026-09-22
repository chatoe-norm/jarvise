#!/usr/bin/env bash
# Thin wrapper named in docs/deploy/hostinger-vps.md (VPS cron every 6h).
# Delegates to run-rag-refresh.sh — paper only, no order placement.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
exec bash "$ROOT/infra/n8n/run-rag-refresh.sh" "$@"
