#!/usr/bin/env bash
# Repository-owned healthcheck общего Grafana MCP HTTP service.
#
# В образе grafana/mcp-grafana нет HTTP-клиента (ни curl, ни wget, ни busybox),
# поэтому проверка идёт через /dev/tcp bash-а по отдельному healthz-листенеру,
# который upstream не пропускает через проверки --allowed-hosts/--allowed-origins.
set -euo pipefail

host="${AZURPILOT_MCP_HEALTHZ_HOST:-127.0.0.1}"
port="${AZURPILOT_MCP_HEALTHZ_PORT:-8081}"

exec 3<>"/dev/tcp/${host}/${port}"
printf 'GET /healthz HTTP/1.0\r\nHost: %s\r\n\r\n' "${host}" >&3
IFS= read -r -t 5 status <&3 || exit 1
exec 3>&- 3<&-

case "${status}" in
    *" 200 "*) exit 0 ;;
esac
exit 1
