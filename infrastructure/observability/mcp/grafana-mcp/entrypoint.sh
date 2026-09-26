#!/bin/sh
# Repository-owned fail-closed entrypoint общего Grafana MCP HTTP service.
#
# Upstream grafana/mcp-grafana не отказывается стартовать при пустом
# -server-auth-token: middleware просто не устанавливается, и MCP endpoint
# обслуживается без аутентификации. Этот guard превращает такую конфигурацию в
# отказ старта, чтобы общий HTTP endpoint никогда не открывался без caller auth.
set -eu

if [ -z "${MCP_GRAFANA_SERVER_TOKEN:-}" ]; then
    echo "shared Grafana MCP HTTP service: MCP_GRAFANA_SERVER_TOKEN не задан, отказ старта." >&2
    exit 78
fi

exec /app/mcp-grafana "$@"
