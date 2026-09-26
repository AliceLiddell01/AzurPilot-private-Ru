// Принадлежащая репозиторию проверка работоспособности общего Docker Hub MCP HTTP service.
//
// Upstream не публикует отдельный health endpoint, поэтому healthcheck
// проверяет сразу два инварианта общего сервиса:
//   1) HTTP transport слушает и отвечает на POST /mcp;
//   2) caller auth закрыт fail-closed — запрос без bearer token отклоняется 401.
// Второй инвариант делает недоступность проверки auth видимой: сервис,
// который начал принимать неаутентифицированные запросы, считается unhealthy.
const endpoint = process.env.MCP_HEALTHCHECK_ENDPOINT ?? "http://127.0.0.1:3000/mcp";

const response = await fetch(endpoint, {
    method: "POST",
    headers: {
        "content-type": "application/json",
        accept: "application/json, text/event-stream",
    },
    body: JSON.stringify({
        jsonrpc: "2.0",
        id: 1,
        method: "initialize",
        params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "healthcheck", version: "0" } },
    }),
});

process.exit(response.status === 401 ? 0 : 1);
