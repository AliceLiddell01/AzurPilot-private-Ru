# ARM64 Dockerfile for AzurLaneAutoScript
# Build: docker build -t alas-arm64 -f dev_tools/arm64/Dockerfile .
# Run:   docker run -v ${PWD}:/app/AzurLaneAutoScript -p 25548:25548 --name alas -it --rm alas-arm64

FROM python:3.14-slim-bookworm

ENV PATH=/app/AzurLaneAutoScript/.venv/bin:${PATH}

WORKDIR /app/AzurLaneAutoScript

COPY --from=ghcr.io/astral-sh/uv:0.12.13@sha256:b485bd65cc2cf1c9a93b3554012c9c3778cf7b1b5fd3d3096ce9e1226c97e1e6 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        git \
        adb \
        libgomp1 \
        libgl1 \
        libglib2.0-0 \
        openssh-client && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

RUN git config --global --add safe.directory '*'

RUN uv venv --relocatable --python /usr/local/bin/python .venv && \
    uv sync --frozen --no-dev --no-install-project && \
    cp /usr/local/bin/uv .venv/bin/uv && \
    cp /usr/bin/adb .venv/bin/adb && \
    cp /usr/bin/git .venv/bin/git && \
    rm -rf /root/.cache/uv

COPY . .

CMD [".venv/bin/python", "gui.py"]
