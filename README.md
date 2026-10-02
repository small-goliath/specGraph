# specGraph

product-docs 저장소의 `draft/*` 브랜치에 있는 기획 문서(PRD · UI/UX spec · tech-spec · QA)를
**챕터 단위로 증분 인덱싱**해 지식 그래프(GraphRAG)로 만들고, **MCP 도구**로 Claude Code 에서 조회하게 하는 PoC 다.

- 모든 응답에 출처 `doc_id` 와 `commit_sha` 가 붙는다.
- LLM · 임베딩 · 리랭커 · 벡터/그래프 데이터는 **전부 로컬**에서 돈다(외부 LLM API 호출 없음).
- 인덱싱은 변경된 챕터만 다시 처리한다. 바뀌지 않은 챕터는 LLM 을 호출하지 않는다.

## 구성

```
product-docs (git, draft/*)
        │  fetch (읽기 전용 토큰)
        ▼
  indexer ──────────► LightRAG ──► PostgreSQL (KV · 벡터 · 그래프 · doc status · manifest)
  (폴링 · 챕터 증분)      ▲  ▲
                         │  └─ Ollama (LLM Qwen3-8B · 임베딩 BGE-M3)
                         └──── rerank (bge-reranker-v2-m3)
        ▲
        │ stdio
  MCP 서버 (specgraph-mcp) ◄── Claude Code
```

| 구성 요소 | 역할 |
|---|---|
| `specgraph-indexer` | `draft/*` 브랜치를 폴링하고 챕터 단위로 LightRAG 에 넣는다. 쓰기 주체는 이 프로세스 하나다. |
| `specgraph-mcp` | FastMCP(stdio) 서버. 다섯 도구로 조회한다. 컨테이너가 아니라 호스트에서 실행한다. |
| PostgreSQL | pgvector + Apache AGE. LightRAG 스토리지 전부와 `specgraph` manifest 스키마를 담는다. |
| Ollama / rerank | 로컬 LLM · 임베딩 / 리랭커 |
| LightRAG 서버 | WebUI · REST **질의 전용**(포트 9621). 문서 업로드 등 쓰기 API 는 쓰지 않는다. |

### doc_id

`<branch>:<path>#<§번호>` 형식이다. 챕터는 문서의 최상위 번호 헤딩이고, 머리말은 `#preamble` 이다.

## 요구 사항

- Python 3.12, [uv](https://docs.astral.sh/uv/)
- Docker (Docker Compose)
- product-docs 읽기 전용 토큰(아래 참고)
- 모델 다운로드용 디스크와 네트워크: 첫 기동 때 `qwen3:8b`, `bge-m3` 를 받는다(수 GB).

## 빠른 시작

```bash
# 1. 의존성
uv sync

# 2. 환경 파일 — 비밀값(토큰 · 비밀번호)만 채운다
cp deploy/.env.example deploy/.env
#   SPECGRAPH_GIT_TOKEN=<product-docs 읽기 전용 토큰>
#   POSTGRES_PASSWORD=<임의 비밀번호>   (기본값 없음, 비면 기동 거부)

# 3. 스택 기동
docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d
docker compose -f deploy/docker-compose.yml --env-file deploy/.env ps
docker compose -f deploy/docker-compose.yml --env-file deploy/.env logs -f indexer
```

최초 기동은 모델 다운로드 때문에 오래 걸린다. `ollama-pull` 이 `Exited (0)` 으로 끝나야 indexer 가 시작한다.
indexer 는 기본 120초 주기로 `draft/*` 브랜치를 폴링한다.

정지: `... down`(데이터 유지) / `... down -v`(데이터 삭제).

> Docker Desktop on macOS 는 컨테이너에 Metal GPU 를 노출하지 않아 컨테이너 Ollama 는 CPU 추론이다.
> 인덱싱 속도가 중요하면 호스트 Ollama 를 쓴다 — [docs/PPS-346-runbook.md](docs/PPS-346-runbook.md) §2.

### product-docs 토큰

GitHub **Fine-grained personal access token** 을 쓴다.

- Resource owner: `df-planning-dev` · Repository access: `product-docs` 만
- Repository permissions: **Contents: Read-only** (그 외 권한 없음)
- 만료는 짧게 둔다. 조직이 승인을 요구하면 승인 전까지 clone · fetch 가 실패한다.

토큰은 URL · 로그 · argv 에 남지 않고 `GIT_ASKPASS` 로만 전달된다. 인덱서가 쓰는 git 명령은
`ls-remote` · `fetch` · `ls-tree` · `show` · `cat-file` 뿐이며 push 는 없다. `deploy/.env` 는 커밋하지 않는다.

## MCP 서버를 Claude Code 에 연결

저장소 루트의 `.mcp.json` 에 `specgraph` 가 등록돼 있다. 값은 셸 환경변수에서 읽으므로,
`deploy/.env` 의 컨테이너 주소를 호스트 주소로 덮어쓴 뒤 Claude Code 를 실행한다.

```bash
set -a; source deploy/.env; set +a
export POSTGRES_HOST=localhost \
       LLM_BINDING_HOST=http://localhost:11434 \
       EMBEDDING_BINDING_HOST=http://localhost:11434 \
       RERANK_BINDING_HOST=http://localhost:7997/rerank
claude        # 저장소 루트에서 실행 → .mcp.json 승인 → /mcp 에서 specgraph connected 확인
```

### 도구

| 도구 | 인자 | 설명 |
|---|---|---|
| `query` | `question`, `mode`(`local` `global` `hybrid` `naive` `mix`, 기본 `mix`), `branch`(선택) | 한국어 질문에 GraphRAG 로 답하고 출처를 반환한다. |
| `get_chapter` | `doc`, `section` | 챕터 원문. `doc` 은 `<branch>:<path>`, 브랜치명, 짧은 이름(`admin-prd`) 모두 가능하고 `section` 은 `5`, `§5`, `5.2`, `preamble` 형식이다. |
| `find_screen` | `id` | 화면 ID(예: `ADM-03`, `PTN-P-04`)가 정의된 챕터 원문. |
| `find_policy` | `id` | 정책 ID(예: `P-14.3`, `U-2`)가 정의된 챕터 원문. |
| `list_docs` | — | 인덱싱된 브랜치 · 문서 · 챕터 목록. |

`get_chapter` · `find_screen` · `find_policy` · `list_docs` 는 manifest 를 직접 조회하며 LLM 을 쓰지 않는다.

### 사용 예

Claude Code 에서 자연어로 요청하면 도구가 호출된다.

```
> specgraph 로 파트너 정산 정책의 근거가 되는 Admin PRD 조항을 찾아줘      → query
> ADM-03 화면 정의를 보여줘                                                → find_screen
> admin-prd 5장 원문을 가져와줘                                            → get_chapter
> 인덱싱된 문서 목록을 보여줘                                              → list_docs
```

### 조회에서 알아둘 점

- 처음 인덱싱 중이거나 삽입에 실패한 챕터는 조회 도구에 나오지 않는다.
- 재삽입 중이거나 재삽입에 실패한 챕터는 직전에 성공한 내용으로 보인다.
- `query` 의 `sources` 는 인덱싱이 끝난 챕터만 담지만, 답변 본문에는 숨겨진 챕터의 엔티티 · 관계 설명이
  섞일 수 있다(알려진 한계).

## 개발

```bash
uv sync                                                  # 의존성
uv run ruff check . && uv run ruff format --check .      # 린트 · 포맷
uv run pytest -m "not integration"                       # 단위 테스트(외부 서비스 불필요)

uv run pytest -m integration                             # 통합 테스트(스택이 떠 있을 때, 호스트 주소 export 후)

uv run specgraph-indexer --once                          # 1회 동기화 후 종료(호스트)
uv run specgraph-mcp                                     # MCP stdio 서버
```

`--once` 는 compose 의 indexer 가 떠 있으면 advisory lock 때문에 종료코드 2 로 끝난다.
먼저 `docker compose ... stop indexer` 한다. 종료코드는 0 = 성공, 1 = 브랜치 실패다.

### 레이아웃

```
src/specgraph/
  indexer/       폴링 · git 소스 · 챕터 분할 · 추출 · 동기화 계획
  mcp_server/    FastMCP 서버 · manifest 조회 · query 서비스
  lightrag_store.py   LightRAG 어댑터(lightrag 패키지는 여기서만 import)
  manifest.py    챕터 manifest(PostgreSQL) 와 PENDING 판정
  kg_model.py · blocks.py · docid.py · settings.py · errors.py · log.py
deploy/          docker-compose.yml · app.Dockerfile · postgres/ · .env.example
tests/           tests/<모듈>/test_<대상>.py, 가짜 구현은 tests/fakes.py
docs/            PPS-346-runbook.md (실행 · 수동 검증 · 알려진 제약)
```

### 컨벤션

- 의존 방향은 `indexer` · `mcp_server` → 공유 모듈 한 방향이다. 공유 모듈과 `mcp_server` 는 `indexer` 를 import 하지 않는다
  (`tests/test_dependency_rules.py` 가 지킨다).
- I/O 는 `typing.Protocol` 포트(`LightRagPort`, `ManifestPort`, `GitSourcePort`) 뒤에 둔다.
- 설정은 `src/specgraph/settings.py` 의 단일 `Settings`. 자체 변수는 `SPECGRAPH_` 접두어이고, LightRAG 와 공유하는
  변수(`LLM_*`, `EMBEDDING_*`, `RERANK_*`, `POSTGRES_*` 등)는 LightRAG 규약 이름을 그대로 쓴다.
  모델명은 코드에 리터럴로 두지 않는다.
- ruff(line-length 100), pytest + pytest-asyncio. 자세한 규칙은 `CLAUDE.md` 를 본다.

## 설정

모델 · 엔드포인트는 `deploy/.env` 한 곳에서 바꾼다(코드 수정 없음). 전체 목록은 `deploy/.env.example` 을 본다.

| 변수 | 기본값 | 설명 |
|---|---|---|
| `SPECGRAPH_REPO_URL` | `https://github.com/df-planning-dev/product-docs.git` | 인덱싱할 저장소 |
| `SPECGRAPH_GIT_TOKEN` | — | 읽기 전용 토큰 |
| `SPECGRAPH_BRANCH_GLOB` | `draft/*` | 대상 브랜치 |
| `SPECGRAPH_INCLUDE_DIRS` | `prd,ui-ux-spec,tech-spec,qa` | 대상 디렉터리 |
| `SPECGRAPH_POLL_INTERVAL_SECONDS` | `120` | 폴링 주기 |
| `SPECGRAPH_LOG_FORMAT` | `kv` | `json` 이면 JSON 로그(stderr) |
| `LLM_MODEL` | `qwen3:8b` | LLM |
| `EMBEDDING_MODEL` | `bge-m3` | 임베딩(차원 1024) |
| `RERANK_MODEL` | `BAAI/bge-reranker-v2-m3` | 리랭커 |
| `POSTGRES_PASSWORD` | — | 필수, 기본값 없음 |

임베딩 모델을 바꾸면 벡터 재색인이 필요하다(`down -v` 후 재기동).

## 알려진 제약

- LightRAG 서버(9621)는 인증이 없다. 모든 포트는 `127.0.0.1` 에만 바인딩한다. 쓰기 API 는 쓰지 않는다.
  indexer 와 같은 PostgreSQL workspace 를 공유하므로 쓰는 순간 manifest 와 어긋난다.
- 호스트 실행(MCP · `--once`)은 첫 실행에 tiktoken 토크나이저 파일을 내려받는다(LLM API 호출은 아니다).
- 장애 시 가장 단순한 복구는 `down -v` 후 재인덱싱이다.

더 자세한 실행 절차 · 로그 읽는 법 · 수동 검증은 [docs/PPS-346-runbook.md](docs/PPS-346-runbook.md) 를 본다.
