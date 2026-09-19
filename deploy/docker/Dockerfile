# docker build -t azurpilot-private-ru:local -f deploy/docker/Dockerfile .
# docker run -p 25548:25548 --name AzurPilot --rm azurpilot-private-ru:local

FROM python:3.14-slim-bookworm

ARG UV_INDEX_URL=https://pypi.org/simple
ARG UV_EXTRA_INDEX_URL=
ARG HTTP_PROXY=
ARG HTTPS_PROXY=
ARG NO_PROXY=127.0.0.1,localhost

ENV PATH=/app/AzurPilot/.venv/bin:${PATH}

WORKDIR /app/AzurPilot

COPY --from=ghcr.io/astral-sh/uv:0.12.15@sha256:62f8c047d0a0e9ece6b53fc63df902585a67a47a7f318ddec4a37db586edc8e3 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./

RUN if [ -n "$HTTP_PROXY" ]; then \
    export http_proxy="$HTTP_PROXY" https_proxy="$HTTPS_PROXY" HTTP_PROXY="$HTTP_PROXY" HTTPS_PROXY="$HTTPS_PROXY" no_proxy="$NO_PROXY" NO_PROXY="$NO_PROXY"; \
    printf 'Acquire::http::Proxy "%s";\nAcquire::https::Proxy "%s";\n' "$HTTP_PROXY" "$HTTPS_PROXY" > /etc/apt/apt.conf.d/99proxy; \
    fi; \
    apt-get update && \
    apt-get install -y --no-install-recommends \
    git \
    adb \
    libgomp1 \
    libgl1 \
    libglib2.0-0 \
    openssh-client && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /etc/apt/apt.conf.d/99proxy

RUN git config --global --add safe.directory '*'

RUN if [ -n "$HTTP_PROXY" ]; then \
    export http_proxy="$HTTP_PROXY" https_proxy="$HTTPS_PROXY" HTTP_PROXY="$HTTP_PROXY" HTTPS_PROXY="$HTTPS_PROXY" no_proxy="$NO_PROXY" NO_PROXY="$NO_PROXY"; \
    fi; \
    export UV_INDEX_URL="$UV_INDEX_URL" UV_EXTRA_INDEX_URL="$UV_EXTRA_INDEX_URL"; \
    uv venv --relocatable --python /usr/local/bin/python .venv && \
    uv sync --frozen --no-dev --no-install-project && \
    cp /usr/local/bin/uv .venv/bin/uv && \
    cp /usr/bin/adb .venv/bin/adb && \
    cp /usr/bin/git .venv/bin/git && \
    rm -rf /root/.cache/uv

COPY . .

CMD [".venv/bin/python", "deploy/docker/runtime_entrypoint.py"]
