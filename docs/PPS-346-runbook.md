# PPS-346 runbook — specGraph GraphRAG PoC (로컬 Docker Compose)

product-docs 의 `draft/*` 브랜치 기획 문서를 챕터 단위로 LightRAG(PostgreSQL)에 증분 인덱싱하고,
FastMCP(stdio) 다섯 도구로 Claude Code 에서 조회한다. 이 문서는 실행 절차와 수동 검증(M1~M8)
절차를 담는다. N8 에서는 각 M 단계의 실제 출력(명령 · 시각 · 수치)을 증거로 첨부한다.

## 0. 구성

| 서비스 | 이미지 | 호스트 포트(127.0.0.1) | 역할 |
|---|---|---|---|
| postgres | `deploy/postgres/Dockerfile` (pgvector:pg16 + Apache AGE `PG16/v1.5.0-rc0`) | 5432 | LightRAG KV · 벡터 · 그래프 · doc status 전부 + `specgraph` manifest 스키마 |
| ollama | `ollama/ollama:0.35.0` | 11434 | LLM(Qwen3-8B, non-thinking) + 임베딩(BGE-M3) |
| ollama-pull | 위와 동일(1회성) | — | `OLLAMA_PULL_MODELS` 를 이름 있는 볼륨에 받아 둔다 |
| rerank | `michaelf34/infinity:0.0.75` (arm64 포함 멀티아치) | 7997 | `BAAI/bge-reranker-v2-m3`, Cohere 호환 `/rerank` |
| lightrag | `ghcr.io/hkuds/lightrag:v1.5.7` | 9621 | WebUI · REST 질의 전용(문서 업로드 금지) |
| indexer | `deploy/app.Dockerfile` | — | `draft/*` 폴링 · 챕터 증분 인덱싱 |

- MCP 서버는 컨테이너가 아니라 **호스트에서 stdio 로** 실행한다(`uv run specgraph-mcp`, 결정 D4).
- 쓰기 주체는 indexer 하나다. LightRAG WebUI 의 문서 업로드 기능은 쓰지 않는다
  (같은 PostgreSQL workspace 를 공유하므로 manifest 와 어긋난다). §8 "LightRAG 서버 무인증" 참고.
- 이미지 태그는 2026-10-01 에 arm64 지원을 확인해 고정했다(`deploy/.env.example`). Infinity 는
  0.0.76 부터 arm64 이미지가 없어 0.0.75 로 고정했다. LightRAG 서버 태그는 `uv.lock` 의
  `lightrag-hku` 버전(1.5.7)과 맞춘다.

## 1. 준비

```bash
brew install uv                       # 최초 1회
uv sync                               # Python 3.12 가상환경 + 의존성(uv.lock 고정)
cp deploy/.env.example deploy/.env    # SPECGRAPH_GIT_TOKEN(읽기 전용), POSTGRES_PASSWORD 입력
```

- `POSTGRES_PASSWORD` 는 **기본값이 없다.** 비어 있으면 `docker compose` 가 기동을 거부하고,
  `.mcp.json` 도 셸에 export 돼 있지 않으면 Claude Code 가 설정을 거부한다.

- `SPECGRAPH_GIT_TOKEN` 은 product-docs **읽기 전용** 토큰이다. URL · 로그 · argv 에 남지 않고
  `GIT_ASKPASS` 로만 전달된다. 인덱서가 쓰는 git 명령은 `ls-remote` · `fetch` · `ls-tree` ·
  `show` · `cat-file` 뿐이다(push 없음).
- `deploy/.env` 는 커밋하지 않는다(`.gitignore`).

## 2. 기동 / 정지

```bash
docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d
docker compose -f deploy/docker-compose.yml --env-file deploy/.env ps
docker compose -f deploy/docker-compose.yml --env-file deploy/.env logs -f indexer
docker compose -f deploy/docker-compose.yml --env-file deploy/.env down        # 데이터 유지
docker compose -f deploy/docker-compose.yml --env-file deploy/.env down -v     # 데이터 삭제(롤백)
```

최초 기동은 모델 다운로드(수 GB) 때문에 오래 걸린다. `ollama-pull` 이 exit 0 으로 끝나야
indexer 가 시작한다.

### 호스트 네이티브 Ollama 로 전환 (결정 D1 — AC12 측정)

Docker Desktop on macOS 는 Metal GPU 를 컨테이너에 노출하지 않아 컨테이너 Ollama 는 CPU
추론이다. AC12(10분) 측정은 호스트 Ollama(Metal)로 한다. 코드 변경 없이 env 만 바꾼다.

```bash
brew install ollama && ollama serve &
ollama pull qwen3:8b && ollama pull bge-m3
# deploy/.env
LLM_BINDING_HOST=http://host.docker.internal:11434
EMBEDDING_BINDING_HOST=http://host.docker.internal:11434
```

### 모델 전환 (AC2 — env 만 변경)

- prod LLM: `LLM_MODEL=qwen3:30b-a3b`, `OLLAMA_PULL_MODELS="qwen3:30b-a3b bge-m3"`.
- KURE-v1 임베딩: `EMBEDDING_BINDING=openai`, `EMBEDDING_MODEL=nlpai-lab/KURE-v1`,
  `EMBEDDING_BINDING_HOST=http://rerank:7997`, `INFINITY_EXTRA_ARGS=--model-id nlpai-lab/KURE-v1`.
  차원은 1024 로 같지만 **벡터 재색인이 필요**하다(`down -v` 후 재기동).

## 3. 호스트에서 직접 실행

```bash
set -a; source deploy/.env; set +a
export POSTGRES_HOST=localhost LLM_BINDING_HOST=http://localhost:11434 \
       EMBEDDING_BINDING_HOST=http://localhost:11434 \
       RERANK_BINDING_HOST=http://localhost:7997/rerank
uv run specgraph-indexer --once       # 1회 동기화 후 종료(AC12 측정용). 종료코드 0=성공, 1=브랜치 실패
uv run specgraph-mcp                  # MCP stdio 서버(보통은 Claude Code 가 실행)
```

compose 의 indexer 가 떠 있으면 `--once` 는 advisory lock 때문에 종료코드 2 로 끝난다.
측정 전에 `docker compose ... stop indexer` 한다.

## 4. Claude Code 등록 (AC20)

저장소 루트의 `.mcp.json` 에 `specgraph` 가 등록돼 있다. 값은 셸 환경변수에서 읽는다.
모델명(`LLM_MODEL` · `EMBEDDING_MODEL` · `RERANK_MODEL`)과 `POSTGRES_PASSWORD` 는 `.mcp.json` 에
기본값이 없다 — 모델명의 출처는 `deploy/.env` 하나다. 엔드포인트 · 접속 주소만 로컬 기본값이 있다.
`deploy/.env` 의 엔드포인트는 컨테이너 이름(`ollama`, `rerank`, `postgres`)이므로 호스트 주소로
덮어쓴 뒤 Claude Code 를 실행한다.

```bash
set -a; source deploy/.env; set +a    # 모델명 · POSTGRES_PASSWORD 등
export POSTGRES_HOST=localhost LLM_BINDING_HOST=http://localhost:11434 \
       EMBEDDING_BINDING_HOST=http://localhost:11434 \
       RERANK_BINDING_HOST=http://localhost:7997/rerank
claude                                 # 저장소 루트에서 실행 → .mcp.json 승인
/mcp                                   # specgraph connected 확인
```

## 5. 로그 읽기

한 줄 key=value(`SPECGRAPH_LOG_FORMAT=json` 이면 JSON).

| event | 필드 | 쓰임 |
|---|---|---|
| `poll` | branches · new · changed · deleted · unchanged | 주기마다 브랜치 diff |
| `chapter_sync` | doc_id · action(insert/reinsert/delete/skip, 실패 시 insert_failed/reinsert_failed) · content_hash · llm_calls | AC9 — skip 은 항상 `llm_calls=0`. 브랜치 뒷단계가 실패해도 처리한 챕터는 남는다 |
| `extract` | doc_id · screens · policies · section_refs · cross_doc_refs · dangling_refs | M7 추출 통계. dangling_refs 는 아직 없는 챕터로의 참조(엣지 미생성) |
| `sync_done` | branch · head · elapsed_s · llm_calls_total · inserted · reinserted · deleted · skipped | AC12 측정. llm_calls_total = 그 브랜치 chapter_sync llm_calls 합 |
| `branch_removed` | branch · chapters | AC6 |
| `branch_failed` | branch · error_type · error (예상 못 한 예외는 traceback `exc=`) | 실패 격리 — 다음 주기에 재시도 |
| `poll_crashed` | error_type · error · traceback | 폴링 자체가 예상 못 한 예외로 끝남 — 다음 주기에 재시도 |
| `file_skipped` | branch · path · error | UTF-8 이 아닌 .md — 그 파일만 건너뛴다(그 파일의 기존 챕터는 지워진다) |
| `cross_ref_unresolved` | doc_id · name · kind · section | 문서 간 참조 해석 실패(엣지 미생성). 한글 이름 참조("어드민 PRD §5")는 브랜치 · 파일명과 대조할 수 없어 여기에 남는다 |
| `cross_ref_reconciled` / `cross_ref_reconcile_failed` | doc_id · relations · unresolved · dangling / error · traceback | 참조 재해석. 실패분은 변경 없는 다음 주기에도 다시 시도 |
| `glossary_merge` | target · sources · llm_calls | AC14. 병합 LLM 호출은 챕터가 아니라 `glossary_merge` 키에 귀속(AC9 판정은 chapter_sync 로만) |
| `glossary_failed` | error_type · error | 병합 실패 — 다음 주기에 다시 시도 |
| `block_over_token_limit` | doc_id · blocks · max_tokens · limit | AC10 — 아래 §8 |

## 6. 수동 검증 (N8 판정 기준)

| ID | AC | 절차 | 통과 기준 |
|---|---|---|---|
| M1 | AC1 | §2 의 `up -d` **1회** → `docker compose ps` · `docker stats --no-stream` · `docker compose exec postgres psql -U $POSTGRES_USER -d $POSTGRES_DATABASE -c '\dx'` · `curl -s localhost:9621/health` · `docker compose ps -a ollama-pull` | 상시 서비스(postgres · ollama · rerank · lightrag) `healthy`, indexer `running`, ollama-pull `Exited (0)`, `\dx` 에 `vector` · `age`, health 에 PG 스토리지 4종 · `Korean`, 메모리 합계 기록(OOM 없음). 최초 모델 다운로드 시간은 별도 기록(판정 제외) |
| M2 | AC12 | `git clone --mirror <product-docs> /tmp/pd.git` → `SPECGRAPH_REPO_URL=file:///tmp/pd.git` 로 `uv run specgraph-indexer --once`(초기 전체) → 미러에서 한 draft 브랜치의 챕터 ≤3개 수정 커밋 → 다시 `--once` (호스트 네이티브 Ollama) | `event=sync_done ... elapsed_s ≤ 600`, `reinserted+inserted+deleted ≤ 3`. 컨테이너 Ollama 수치도 참고 기록. **원격(product-docs)에는 쓰지 않는다** |
| M3 | AC15 | 실문서 인덱싱 후 MCP `query("파트너 정산 정책의 근거가 되는 Admin PRD 조항은?", "mix", null)` 등 한국어 질문 2건 | 각 응답 `sources` 에 `draft/settlr-partner-prd:` · `draft/settlr-admin-prd:` 접두어 doc_id 가 모두 있고, 모든 source 에 `commit_sha` |
| M4 | AC20 | §4 → 다섯 도구를 각각 1회 호출 | 5개 모두 오류 없이 응답, `find_screen("ADM-03")` 이 원문 + doc_id 반환 |
| M5 | AC3 | LightRAG WebUI(`http://localhost:9621`) 또는 SQL 로 LLM 추출 엔티티 20개 표본 확인 | 설명 · 요약이 한국어(고유명사 · ID 제외) 20개 중 18개 이상 |
| M6 | AC9 | M2 의 두 번째 실행 로그 | 미변경 챕터 전부 `action=skip llm_calls=0`, `llm_calls>0` 은 수정 챕터 doc_id 에만 |
| M7 | AC13 | 실 admin-prd 인덱싱 로그의 `event=extract` 합계 | 티켓 검토치(화면 110종 · 정책 75종 · § 참조 102건)와 비교해 차이를 원인과 함께 기록. ±5% 초과 시 정규식 보정 + 단위 테스트 추가 |
| M8 | 제약 | 아래 라이선스 표 확인 | 채택 모델 전부 상용 허용 라이선스, EXAONE 계열 미사용 |

통합 테스트(선택, Q14): 스택이 떠 있을 때 §3 의 export 후 `uv run pytest -m integration`.
각 테스트는 별도 manifest 스키마(`itest_*`)와 LightRAG workspace 를 써서 실제 인덱스를 건드리지 않는다.

## 7. 모델 라이선스 (M8)

2026-10-01 Hugging Face 모델 메타데이터(`/api/models/<id>` 의 `cardData.license`)로 확인했다.
N8 에서 모델 카드 원문으로 재확인한다.

| 모델 | 용도 | 라이선스 | 근거 |
|---|---|---|---|
| Qwen3-8B (`qwen3:8b`) | LLM | Apache-2.0 | https://huggingface.co/Qwen/Qwen3-8B |
| BGE-M3 (`bge-m3`) | 임베딩(기본) | MIT | https://huggingface.co/BAAI/bge-m3 |
| bge-reranker-v2-m3 | 리랭커 | Apache-2.0 | https://huggingface.co/BAAI/bge-reranker-v2-m3 |
| KURE-v1 | 임베딩(선택) | MIT (HF 메타데이터 기준 — 모델 카드 원문은 N8 에서 재확인) | https://huggingface.co/nlpai-lab/KURE-v1 |
| EXAONE 계열 | — | NC 라이선스 → **채택하지 않음** | 티켓 제약 |

## 8. 알려진 제약 · 위험

- **LightRAG 토크나이저(tiktoken):** 기본 토크나이저 파일을 처음 쓸 때 외부에서 내려받는다. indexer
  이미지는 빌드 시점에 받아 두지만(`TIKTOKEN_CACHE_DIR`), 호스트 실행(MCP · `--once`)은 첫 실행에
  네트워크가 필요하다. LLM API 호출은 아니다.
- **LightRAG 서버 무인증 (S7):** LightRAG REST · WebUI(9621)에는 인증이 없다. 사용자 인증 · 권한
  분리는 이 티켓 범위 밖(로컬 단일 사용자 전제)이므로 넣지 않고, 포트를 `127.0.0.1` 에만 바인딩해
  같은 머신 밖에서는 닿지 않게 한다. **WebUI · REST 의 쓰기 API(문서 업로드 · 삭제 · 엔티티/관계
  편집 · 병합 · 캐시 삭제)는 쓰지 않는다.** 같은 PostgreSQL workspace 를 indexer 와 공유하므로
  쓰는 순간 manifest 와 어긋나고, 다음 동기화에서 덮어쓰이거나 지워진다. 질의(`/query`)와 그래프
  조회만 쓴다. 포트를 `0.0.0.0` 이나 다른 호스트에 열지 않는다.
- **표 보존(AC10):** 챕터를 표 단위 블록(`SPECGRAPH_MAX_BLOCK_CHARS`, 기본 1000자)으로 미리 나눈 뒤,
  LightRAG 어댑터가 LightRAG 토크나이저로 다시 재서 토큰 한도(`CHUNK_SIZE`, 기본 1200 토큰 —
  `deploy/.env` 한 곳에서 서버 · 인덱서 · MCP 가 같은 값을 쓴다)를 넘는 블록을 행 경계(헤더 반복)에서
  나누고 `split_by_character_only=True` 로 넘긴다. 선행 파이프가 없는 GFM 표(`a | b` + `--- | ---`)도
  표로 인식한다. 단일 행 · 문단이 혼자 한도를 넘으면 `event=block_over_token_limit` WARNING 을 남기고
  그 챕터만 LightRAG 토큰 분할에 맡긴다(그 행은 잘릴 수 있다 — D7 부분 이탈, N8 기록 대상).
- **원격 git 타임아웃:** `ls-remote` · `fetch` 등 git 호출은 `SPECGRAPH_GIT_TIMEOUT_SECONDS`(기본
  120초) 안에 끝나지 않으면 git 과 그 자식(git-remote-https · ssh)을 프로세스 그룹째 종료하고 그 주기는
  실패로 기록된다(다음 주기 재시도). 보조로 `http.lowSpeedLimit=1` · `http.lowSpeedTime=<타임아웃>` 을
  걸어 멈춘 HTTP 전송은 git 이 스스로 끊는다.
- **UTF-8 이 아닌 문서:** 그 파일만 `event=file_skipped` WARNING 과 함께 건너뛴다(브랜치 전체를
  실패시키지 않는다). 이미 인덱싱된 그 파일의 챕터는 "파일이 사라진 것"과 같게 지워진다.
- **챕터 삽입 시작 기록:** 인덱서는 LightRAG 를 건드리기 전에 manifest 에 `content_hash` 가 빈
  시작 기록을 남긴다. 삽입이 중간에 실패하면 다음 주기에 그 챕터를 다시 넣고(이전 시도의 LightRAG
  문서 · custom KG 관계를 지운 뒤), 브랜치가 지워지면 함께 정리된다. 시작 기록만 있는 챕터는
  `get_chapter` · `find_screen` 에 새 내용으로 보이지만 질의(`query`) 근거에는 아직 없을 수 있다.
- **공유 노드(SCREEN · POLICY · DOCUMENT):** custom KG 를 넣을 때 이미 있는 노드의 설명 · 출처
  (LLM 이 추출한 것 포함)를 지우지 않고 합친다. 챕터를 지우거나 바꾸면 그 챕터의 출처만 뺀다.
  없는 § 를 가리키는 참조는 엣지를 만들지 않고(`dangling_refs`), 그 챕터가 생기면 재해석으로 잇는다.
- **삭제 시 재구성 LLM 호출:** 변경 챕터를 지울 때 LightRAG 가 공유 엔티티 요약을 다시 만들 수 있다.
  그 호출은 변경 챕터 doc_id 로 귀속되어 로그에 남는다(무변경 챕터는 LightRAG 무접촉).
- **AC12 경계:** 네이티브 Ollama 에서도 10분 경계선일 수 있다. 미달이면 측정치와 병목을 그대로
  보고한다(조건 완화는 사용자 결정).
- **custom KG 쓰기는 LightRAG 문서 단위 복구 보장 밖이다**(LightRAG `ainsert_custom_kg` 경고). 장애 시
  `down -v` 후 재인덱싱이 가장 단순한 복구다.
