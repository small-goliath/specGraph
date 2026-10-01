# PostgreSQL 16 + pgvector(베이스 이미지) + Apache AGE(고정 태그 소스 빌드) — 결정 D3.
# pgvector/pgvector:pg16 은 amd64 · arm64 멀티아치라 M2 에서 그대로 쓴다.
# LightRAG 1.5 는 AGE 1.8.0 미만만 지원한다.
FROM pgvector/pgvector:pg16

ARG AGE_REF=PG16/v1.5.0-rc0

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        build-essential ca-certificates git flex bison \
        postgresql-server-dev-16 libreadline-dev zlib1g-dev; \
    git clone --depth 1 --branch "${AGE_REF}" https://github.com/apache/age.git /tmp/age; \
    make -C /tmp/age; \
    make -C /tmp/age install; \
    rm -rf /tmp/age; \
    apt-get purge -y --auto-remove build-essential git flex bison \
        postgresql-server-dev-16 libreadline-dev zlib1g-dev; \
    rm -rf /var/lib/apt/lists/*
