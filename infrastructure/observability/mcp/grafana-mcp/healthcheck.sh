#!/usr/bin/env bash
# Принадлежащая репозиторию проверка работоспособности общего Grafana MCP HTTP service.
#
# В образе grafana/mcp-grafana нет HTTP-клиента (ни curl, ни wget, ни busybox),
# поэтому обе проверки идут через /dev/tcp bash-а:
#   * отдельный healthz-листенер отвечает 200, если процесс обслуживает запросы;
#   * MCP endpoint обязан ответить 401 на запрос без bearer, иначе caller auth
#     выключен и общий HTTP service не должен считаться здоровым.
set -euo pipefail

host="${AZURPILOT_MCP_HEALTHZ_HOST:-127.0.0.1}"
port="${AZURPILOT_MCP_HEALTHZ_PORT:-8081}"
mcp_host="${AZURPILOT_MCP_ENDPOINT_HOST:-127.0.0.1}"
mcp_port="${AZURPILOT_MCP_ENDPOINT_PORT:-8000}"
mcp_path="${AZURPILOT_MCP_ENDPOINT_PATH:-/mcp}"

status_line() {
    local target_host="$1" target_port="$2" request="$3"
    local descriptor status
    exec {descriptor}<>"/dev/tcp/${target_host}/${target_port}" || return 1
    printf '%b' "${request}" >&"${descriptor}"
    IFS= read -r -t 5 status <&"${descriptor}" || status=""
    exec {descriptor}>&- {descriptor}<&-
    printf '%s' "${status}"
}

healthz_status="$(status_line "${host}" "${port}" "GET /healthz HTTP/1.0\r\nHost: ${host}\r\n\r\n")" || exit 1
case "${healthz_status}" in
    *" 200 "*) ;;
    *) exit 1 ;;
esac

body='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"healthcheck","version":"1.0"}}}'
request="POST ${mcp_path} HTTP/1.1\r\nHost: ${mcp_host}:${mcp_port}\r\nAccept: application/json, text/event-stream\r\nContent-Type: application/json\r\nContent-Length: ${#body}\r\nConnection: close\r\n\r\n${body}"
mcp_status="$(status_line "${mcp_host}" "${mcp_port}" "${request}")" || exit 1
case "${mcp_status}" in
    *" 401 "*) exit 0 ;;
esac
exit 1
