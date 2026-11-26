#!/bin/sh
# Wrap the pinned image entrypoint so local mcp-serve is installed before s6 starts.
# Keep PID 1 semantics: exec the real dispatcher so s6-overlay still owns the tree.
set -eu
/bin/sh /opt/jarvise/init-hermes-plugins.sh
exec /opt/hermes/docker/entrypoint-dispatch.sh "$@"
