# Repository-owned boundary для Docker Hub MCP.
#
# Upstream (docker/hub-mcp) закрывает caller auth fail-closed в HTTP transport,
# но среди публичных immutable образов такого revision нет: единственный
# опубликованный образ mcp/dockerhub@sha256:76454af4... предшествует
# исправлению RG-4626 и обслуживает неаутентифицированные запросы.
# Поэтому образ собирается здесь из точного upstream commit-а.
#
# Воспроизводимость обеспечивают три закреплённых входа:
#   * digest базового образа node;
#   * полный commit SHA исходников docker/hub-mcp;
#   * закреплённый package-lock.json (upstream lock не содержит
#     platform-specific optional зависимостей и не проходит `npm ci`).
# Обновление — одно контролируемое изменение: поднять commit, перегенерировать
# lock через `npm install --package-lock-only` и синхронно обновить пин в Compose.
FROM node:24-alpine3.22@sha256:191c9f0080fcbbc6547a85dc0ff7988072214a355aabdc1d2ec55a7dae5eea8a AS builder

ARG HUBCP_COMMIT

COPY package.json package-lock.json .npmrc /pinned/

WORKDIR /src
RUN set -eux; \
    echo "${HUBCP_COMMIT}" | grep -Eq '^[0-9a-f]{40}$'; \
    apk add --no-cache curl; \
    curl -fsSL "https://github.com/docker/hub-mcp/archive/${HUBCP_COMMIT}.tar.gz" -o /tmp/src.tgz; \
    tar -xzf /tmp/src.tgz --strip-components=1 -C /src; \
    rm -f /tmp/src.tgz; \
    cmp /pinned/package.json /src/package.json; \
    cp /pinned/package-lock.json /pinned/.npmrc /src/; \
    npm ci --no-audit --no-fund; \
    npm run build

FROM node:24-alpine3.22@sha256:191c9f0080fcbbc6547a85dc0ff7988072214a355aabdc1d2ec55a7dae5eea8a

WORKDIR /app
RUN addgroup -S appgroup && adduser -S appuser -G appgroup

COPY --from=builder /src/package.json /src/package-lock.json /src/.npmrc ./
COPY --from=builder /src/dist/ ./dist/
COPY healthcheck.mjs ./healthcheck.mjs

RUN npm ci --omit=dev --no-audit --no-fund \
 && npm cache clean --force \
 && chown -R appuser:appgroup /app

USER appuser
ENV NODE_ENV=production
ENTRYPOINT ["node", "dist/index.js"]
