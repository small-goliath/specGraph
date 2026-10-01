## Graph Engineering (opt-in)

JIRA 티켓 하나로 개발을 끝까지 굴리는 **Graph Engineering** 파이프라인이 설치돼 있다.
**기본은 꺼져 있고, 켜지 않으면 평소 작업과 100% 동일하게 동작한다.**

- 켜기: `/graph-run <TICKET>` (예: `/graph-run PPS-283`)
- 조회/재개/중단: `/graph-status`, `/graph-resume <TICKET>`, `/graph-abort`
- 승인: 채팅에 `approve`(또는 `승인`) / 반려: `reject <사유>`
- 그래프 SOP: `.claude/skills/graph-engineering/SKILL.md`

활성 판정은 `.claude/graph/state/current` 파일의 존재로만 한다.
이 파일이 없으면 `.claude/hooks/ge-*.sh` 는 전부 즉시 통과한다.

훅이나 상태 런타임을 고쳤으면 반드시 회귀 테스트를 돌린다:

```bash
.claude/graph/tests/run-tests.sh
```

## JIRA Ticket (상시)

논의를 **정해진 양식**의 JIRA 티켓으로 만든다. 그래프와 무관하게 단독으로 동작한다.

- 만들기: `/jira-ticket [주제]` · 승인: `approve` / `/jira-approve` · 반려: `/jira-reject <사유>`
- 양식: `.claude/skills/jira-ticket/references/template.md`

**등록·승인된 초안 없이는 `mcp__atlassian__createJiraIssue` 가 훅에 차단된다.**
양식 검증을 통과한 티켓은 `/graph-run` 이 양식 위반으로 멈추지 않는다(같은 규격).

```bash
.claude/jira/tests/run-tests.sh
```

## 코드 컨벤션

specGraph 애플리케이션 코드(`src/`, `tests/`, `deploy/`)의 규칙이다. `.claude/**` 는 대상이 아니다.

- **언어 · 버전:** Python 3.12 (`.python-version`, `requires-python = ">=3.12,<3.13"`) — PPS-346 계획에서 확정(Q1)
- **패키지 매니저:** uv + `pyproject.toml` + `uv.lock`(커밋). 의존성 추가는 `uv add` — PPS-346 계획에서 확정(Q2)
- **레이아웃:** src 레이아웃 단일 패키지 `src/specgraph/{indexer,mcp_server}` + 공유 모듈 `src/specgraph/*.py` + `deploy/` + `tests/` — PPS-346 계획에서 확정(Q3)
- **모듈 구조:** 평면 기능 모듈. 순수 로직(분할 · 해시 · 정규식 · 용어 사전 · diff · KG payload)과 I/O 어댑터(git · LightRAG · PostgreSQL)를 파일 단위로 분리하고, I/O 는 `typing.Protocol` 포트(`LightRagPort`, `ManifestPort`, `GitSourcePort`) 뒤에 둔다. LightRAG API 차이는 `lightrag_store.py` 한 곳에서만 흡수한다 — PPS-346 계획에서 확정(Q4)
- **의존 방향:** `indexer` · `mcp_server` → 공유 모듈(`src/specgraph/*.py`) 한 방향이다. 공유 모듈과 `mcp_server` 는 `indexer` 를 import 하지 않는다(여럿이 쓰는 타입 · 순수 함수는 공유 모듈에 둔다: `kg_model.py` · `blocks.py` · `docid.py`). `lightrag` 패키지는 `lightrag_store.py` 에서만 import 한다. 회귀 가드는 `tests/test_dependency_rules.py` — PPS-346 계획 Q3 · Q4 레이아웃의 의존 방향을 N6 리뷰(A1 · A3)에서 명문화
- **테스트:** pytest + pytest-asyncio(`asyncio_mode=auto`), `--import-mode=importlib`. 위치 `tests/<모듈>/test_<대상>.py`(소스 옆 배치 금지), 함수명 `test_<상황>_<기대결과>`, Arrange-Act-Assert. 가짜 구현은 `tests/fakes.py`. 외부 서비스가 필요한 테스트는 `tests/integration/` + `@pytest.mark.integration` 이고 기본 실행에서 `-m "not integration"` 으로 뺀다 — PPS-346 계획에서 확정(Q5)
- **린터 · 포매터:** ruff(lint + format), line-length 100, 규칙 `E,F,I,UP,B,ASYNC`, 설정은 `pyproject.toml [tool.ruff]`(별도 `ruff.toml` 금지). mypy 미도입 — PPS-346 계획에서 확정(Q6)
- **설정:** pydantic-settings 단일 `Settings`(`src/specgraph/settings.py`). 자체 변수는 `SPECGRAPH_` 접두어, LightRAG 와 공유하는 변수(`LLM_*`, `EMBEDDING_*`, `RERANK_*`, `SUMMARY_LANGUAGE`, `POSTGRES_*`, `LIGHTRAG_*_STORAGE`)는 LightRAG 규약 이름 · 의미 그대로. 모델명은 코드에 리터럴로 두지 않는다(`tests/test_no_hardcoding.py`) — PPS-346 계획에서 확정(Q7)
- **에러 처리:** `SpecGraphError` 하위 예외(`ConfigError`, `GitSourceError`(`NonUtf8FileError`), `IndexingError`, `NotFoundError`, `InvalidArgumentError`). 폴링은 브랜치 단위로 실패를 격리하고 실패 브랜치의 HEAD 는 갱신하지 않는다(다음 주기 재시도). MCP 도구는 도메인 예외를 도구 오류 응답으로 바꾼다 — PPS-346 계획에서 확정(Q8)
- **로깅:** 표준 `logging` + `specgraph.log.log_event(logger, "<event>", k=v…)` 한 줄 key=value(`SPECGRAPH_LOG_FORMAT=json` 이면 JSON), 출력은 stderr — PPS-346 계획에서 확정(Q9)
- **비동기 · LightRAG:** asyncio 전면, LightRAG 는 라이브러리(`lightrag-hku`)로 직접 사용 — PPS-346 계획에서 확정(Q10)
- **배포 파일:** `deploy/` 에 집중(`docker-compose.yml`, `app.Dockerfile`, `postgres/`, `.env.example`). 포트는 `127.0.0.1` 에만 바인딩 — PPS-346 계획에서 확정(Q11)
- **doc_id:** `<branch>:<path>#<§번호>`, 챕터 = 문서의 최상위 번호 헤딩, 머리말은 `#preamble` — PPS-346 계획에서 확정(D5)

## specGraph 실행

상세 절차 · 수동 검증은 `docs/PPS-346-runbook.md`.

```bash
uv sync                                                   # 의존성
uv run ruff check . && uv run ruff format --check .       # 린트 · 포맷
uv run pytest -m "not integration"                        # 단위 테스트(외부 서비스 불필요)

cp deploy/.env.example deploy/.env                        # 토큰 · 비밀번호 입력
docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d
uv run pytest -m integration                              # 스택이 떠 있을 때(runbook §3 export 후)

uv run specgraph-indexer --once                           # 1회 동기화(호스트)
uv run specgraph-mcp                                      # MCP stdio 서버(.mcp.json 으로 Claude Code 등록)
```
