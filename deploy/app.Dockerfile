# specGraph indexer 이미지 (빌드 컨텍스트 = 저장소 루트).
#   docker compose -f deploy/docker-compose.yml build indexer
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends git ca-certificates; \
    rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    TIKTOKEN_CACHE_DIR=/opt/tiktoken

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

# LightRAG 기본 토크나이저(tiktoken) 파일을 빌드 시점에 받아 둔다 — 런타임 외부 다운로드 방지.
RUN /app/.venv/bin/python -c "import tiktoken; tiktoken.encoding_for_model('gpt-4o-mini')"

ENV PATH="/app/.venv/bin:${PATH}"
CMD ["specgraph-indexer"]
