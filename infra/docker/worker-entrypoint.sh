#!/bin/sh
set -e
export JARVISE_PAPER_ONLY="${JARVISE_PAPER_ONLY:-true}"
export REDIS_URL="${REDIS_URL:-redis://redis:6379/0}"
export QDRANT_URL="${QDRANT_URL:-http://qdrant:6333}"
exec "$@"
