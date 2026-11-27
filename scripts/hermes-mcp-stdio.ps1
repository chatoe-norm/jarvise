# Opt-in Cursor MCP stdio wrapper for Jarvise Hermes (research tools only).
# Does not overwrite the owner's Cursor MCP config. No tokens on stdout.
# Usage: point an example MCP client at this script (see config/hermes/cursor-mcp.example.json).
$ErrorActionPreference = "Stop"
$sshHost = if ($env:JARVISE_HERMES_SSH_HOST) { $env:JARVISE_HERMES_SSH_HOST } else { "Jarvise-VPS" }
$composeDir = if ($env:JARVISE_HERMES_COMPOSE_DIR) { $env:JARVISE_HERMES_COMPOSE_DIR } else { "/opt/jarvise" }

# Diagnostics on stderr only. Stdout is the MCP wire.
[Console]::Error.WriteLine("hermes-mcp-stdio: ssh $sshHost compose $composeDir mcp-serve --tools-only")

$remote = @(
    "cd $composeDir",
    "&& docker compose --profile hermes -f docker-compose.yml -f docker-compose.prod.yml",
    "exec -T --user hermes hermes hermes mcp-serve --tools-only"
) -join " "

# No PTY (-T). Absolute compose paths under /opt/jarvise.
& ssh -T $sshHost $remote
exit $LASTEXITCODE
