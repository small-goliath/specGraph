# specGraph

product-docs 저장소의 `draft/*` 브랜치에 있는 기획 문서(PRD · UI/UX spec · tech-spec · QA)를
**챕터 단위로 증분 인덱싱**해 지식 그래프(GraphRAG)로 만들고, **MCP 도구**로 Claude Code 에서 조회하게 하는 PoC 다.

- 모든 응답에 출처 `doc_id` 와 `commit_sha` 가 붙는다.
- 기본은 LLM · 임베딩 · 리랭커 · 벡터/그래프 데이터 **전부 로컬**(외부 LLM API 호출 없음).
  `LLM_BINDING=claude_code` 면 **추출 LLM 만** 구독 Claude Code 의 `claude -p` 를 쓴다 — API 키 ·
  과금 경로 없음, 구독 로그인된 사용자 PC 에서 호스트 실행 전용(PPS-350).
- MCP `query` 는 LLM 으로 답하지 않고 근거(청크 · 엔티티 · 관계)만 돌려준다. 질의 경로의 LLM 호출은 0 건이다.
- 인덱싱은 변경된 챕터만 다시 처리한다. 바뀌지 않은 챕터는 LLM 을 호출하지 않는다.

## 구성

```
product-docs (git, draft/*)
        │  fetch (읽기 전용 토큰)
        ▼
  indexer ──────────► LightRAG ──► PostgreSQL (KV · 벡터 · 그래프 · doc status · manifest)
  (폴링 · 챕터 증분)      ▲  ▲
                         │  └─ Ollama (LLM Qwen3-8B · 임베딩 BGE-M3)
                         │       └─ 또는 LLM_BINDING=claude_code: 호스트의 `claude -p`(구독, 추출 전용)
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
| Ollama / rerank | 로컬 LLM · 임베딩 / 리랭커. 추출 LLM 은 `LLM_BINDING=claude_code` 로 호스트 `claude -p` 로 바꿀 수 있다(임베딩 · 리랭커는 로컬 유지) |
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
       EMBEDDING_BINDING_HOST=http://localhost:11434 \
       RERANK_BINDING_HOST=http://localhost:7997/rerank
claude        # 저장소 루트에서 실행 → .mcp.json 승인 → /mcp 에서 specgraph connected 확인
```

MCP 서버는 LLM 을 쓰지 않으므로 `LLM_MODEL` · `LLM_BINDING_HOST` 가 필요 없다(`.mcp.json` 에도 없다).

### 도구

| 도구 | 인자 | 설명 |
|---|---|---|
| `query` | `question`, `mode`(`local` `global` `hybrid` `naive` `mix`, 기본 `mix`), `branch`(선택), `hl_keywords`(선택, 관계 검색용 상위 키워드), `ll_keywords`(선택, 엔티티 검색용 하위 키워드) | 근거 반환 전용 — 청크 본문 · 엔티티 · 관계를 각각 `doc_id` · `commit_sha` 와 함께 돌려주고 답변은 만들지 않는다. 키워드는 호출자(Claude Code)가 뽑아 넘기며, 둘 다 비면 질문 전체를 하위 키워드로 쓴다. 근거가 없으면 빈 목록 + `no_evidence: true`. |
| `get_chapter` | `doc`, `section` | 챕터 원문. `doc` 은 `<branch>:<path>`, 브랜치명, 짧은 이름(`admin-prd`) 모두 가능하고 `section` 은 `5`, `§5`, `5.2`, `preamble` 형식이다. |
| `find_screen` | `id` | 화면 ID(예: `ADM-03`, `PTN-P-04`)가 정의된 챕터 원문. |
| `find_policy` | `id` | 정책 ID(예: `P-14.3`, `U-2`)가 정의된 챕터 원문. |
| `list_docs` | — | 인덱싱된 브랜치 · 문서 · 챕터 목록. |

다섯 도구 모두 LLM 을 쓰지 않는다. `query` 는 LightRAG 검색만 하고, 나머지는 manifest 를 직접 조회한다.

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
- `query` 는 숨겨진 챕터의 청크를 빼고, 엔티티 · 관계에는 보이는 출처만 붙인다(보이는 출처가 하나도
  없으면 그 항목을 뺀다). 참조 확장(REFERS_TO 한 단계)으로 덧붙인 챕터 본문은 `expanded: true` 청크다.

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
| `SPECGRAPH_INCLUDE_DIRS` | `prd,ui-ux-spec,tech-spec,qa` | 대상 디렉터리 이름만 나열한다. 인덱싱 대상은 `<프로젝트>/<대상 디렉터리>/…` 아래의 `.md` 이며 프로젝트 이름(예: `settlr`)은 나열하지 않는다 |
| `SPECGRAPH_POLL_INTERVAL_SECONDS` | `120` | 폴링 주기 |
| `SPECGRAPH_LOG_FORMAT` | `kv` | `json` 이면 JSON 로그(stderr) |
| `LLM_BINDING` | `ollama` | 추출 LLM 경로. `claude_code` 면 구독 Claude Code 의 `claude -p` 를 서브프로세스로 쓴다(구독 로그인된 PC 호스트 실행 전용, 컨테이너는 compose 가 `ollama` 고정). 허용값 밖이면 `ConfigError` |
| `LLM_MODEL` | `qwen3:8b` | 추출 LLM. `claude_code` 면 `claude --model` 값(별칭 · ID) — `.env` 의 Ollama 모델명을 셸에서 덮어쓴다. 인덱서만 요구하고 MCP 는 쓰지 않는다 |
| `EMBEDDING_MODEL` | `bge-m3` | 임베딩(차원 1024) |
| `RERANK_MODEL` | `BAAI/bge-reranker-v2-m3` | 리랭커 |
| `LLM_TIMEOUT` | `.env.example` 1800(코드 기본 600) | LLM 호출 하나의 타임아웃(초). ollama: HTTP 읽기 한도는 이 값, LightRAG 워커 한도는 2배. claude_code: `claude -p` 1회 한도(넘기면 프로세스 그룹 종료 후 그 챕터 실패) |
| `OLLAMA_LLM_NUM_CTX` | `16384` | Ollama 요청의 `num_ctx`(토큰, ollama 바인딩에서만). 서버 기본 4096 은 추출 출력이 길면 지시문이 잘린다 |
| `MAX_ASYNC` | `2` | LightRAG 동시 LLM 호출 수. claude_code 면 동시에 뜨는 `claude` 프로세스 수 |
| `POSTGRES_PASSWORD` | — | 필수, 기본값 없음 |

임베딩 모델을 바꾸면 벡터 재색인이 필요하다(`down -v` 후 재기동).

**기존 `deploy/.env` 를 쓰고 있다면 `LLM_TIMEOUT=1800` 과 `OLLAMA_LLM_NUM_CTX=16384` 두 줄을 추가해야
한다(PPS-349).** compose 가 두 변수를 기본값 없이 참조하므로 없으면 빈 값이 컨테이너로 가고 `Settings`
정수 검증에서 실패해 indexer 가 기동하지 않는다. 호스트 Ollama 를 쓰면 `OLLAMA_CONTEXT_LENGTH` 도
같은 값 이상으로 맞춘다(runbook §2).

## 알려진 제약

- LightRAG 서버(9621)는 인증이 없다. 모든 포트는 `127.0.0.1` 에만 바인딩한다. 쓰기 API 는 쓰지 않는다.
  indexer 와 같은 PostgreSQL workspace 를 공유하므로 쓰는 순간 manifest 와 어긋난다.
- 호스트 실행(MCP · `--once`)은 첫 실행에 tiktoken 토크나이저 파일을 내려받는다(LLM API 호출은 아니다).
- `LLM_BINDING=claude_code` 는 구독 로그인된 사용자 PC 에서 **일반 터미널**로 호스트 실행할 때만 쓴다.
  컨테이너 indexer · lightrag 에서는 쓸 수 없고(compose 가 `ollama` 고정), 공용 서버에 올리지 않는다.
  구독 사용량 한도에 걸리면 재시도하지 않고 `event=llm_rate_limited` 를 남긴 뒤 그 poll 의 나머지
  브랜치를 건너뛴다(다음 poll 재시도). 초기 인덱싱 물량이 한도 안에 드는지는 실측 전이다(runbook §9).
- 종료 신호(SIGTERM · SIGINT)를 받으면 진행 중인 처리를 취소하고 종료한다. 취소된 챕터는
  `chapter_sync` 에 `*_failed`(`insert_failed` · `reinsert_failed` · `delete_failed`)로 남고,
  브랜치 HEAD 는 갱신되지 않아 다음 실행에서 다시 처리된다. `poll_cancelled` 로그가 찍힌다.
  compose 의 indexer `stop_grace_period` 는 30초다(실측으로 조정). 종료 코드는 데몬 0, `--once` 취소 1.
- 장애 시 가장 단순한 복구는 `down -v` 후 재인덱싱이다.

더 자세한 실행 절차 · 로그 읽는 법 · 수동 검증은 [docs/PPS-346-runbook.md](docs/PPS-346-runbook.md) 를 본다.
