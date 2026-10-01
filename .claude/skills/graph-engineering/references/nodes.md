# 노드 명세

각 노드는 **책임 / 입력 상태 / 출력 상태 / 사용 도구 / 성공 조건 / 실패 시 라우팅** 을 갖는다.
노드는 **자기 소유 필드만** 쓴다. 다른 노드의 필드를 덮어쓰지 않는다.

---

## N0 — 선행조건 (오케스트레이터가 직접 수행)

**다른 어떤 작업보다 먼저 수행한다.** 서브에이전트를 띄우지 않는다.

| 항목 | 내용 |
|---|---|
| 책임 | 워킹트리 청결 확인 → base 브랜치 최신화 → 티켓 브랜치 생성/체크아웃 → 런타임 탐지 → 상태 초기화 |
| 입력 | 사용자가 준 티켓 키 |
| 출력 | `git.*`, `runtime.graphify`, `runtime.build`, `runtime.project_state`, 상태 파일 + `state/current` |
| 도구 | Bash(git), `ge init`, `ge detect all` |
| 성공 | 티켓 브랜치에 체크아웃됨 + `runtime.build.status == "detected"` |
| 실패 | 어떤 실패든 **즉시 중단하고 사용자에게 보고**. 상태 파일을 만들지 않거나(초기 실패) `halt_reason` 을 남긴다. |

절차:

```bash
# 1. 워킹트리 청결
git status --porcelain          # 출력이 있으면 → 중단, 사용자에게 알림
# 2. base 브랜치 결정 (config.git.base_branch_candidates 순서로 실재 확인)
git rev-parse --verify main || git rev-parse --verify master || ...
#    → 후보가 하나도 없으면 중단하고 사용자에게 묻는다 (임의로 우회 금지)
# 3. base 최신화
git switch <base> && (git remote | grep -q . && git pull --ff-only || echo "리모트 없음 — 로컬 base 사용")
# 4. 티켓 브랜치
git switch -c <TICKET>          # 이미 있으면 git switch <TICKET> 후 사용자에게 알림
# 5. 상태 초기화 + 탐지
.claude/graph/bin/ge init --ticket <TICKET> --base-branch <base>
.claude/graph/bin/ge detect all
```

`runtime.build.status`:
- `detected` → 진행
- `missing` → **신규 프로젝트**. 중단하지 말고 상태를 `greenfield` 로 두고 N1 로 간다.
  Gradle 세팅은 N3 계획에 포함해 승인을 받는다.
- `blocked` / `ambiguous` → **중단하고 사용자에게 묻는다** (`blockers` 배열을 그대로 보여준다).

---

## N1 — 티켓 상태 전이 + 수신 · `ge-jira-analyst`

| 항목 | 내용 |
|---|---|
| 책임 | **본 작업 이전에** 티켓 상태를 `검토 중`(config.jira.status_targets.N1_on_receive)으로 전이한 뒤 티켓 원문 수신 |
| 입력 | `ticket`, `jira.cloud_id` |
| 출력 | `jira.summary`, `jira.raw_description_md`, `jira.raw_description_adf_path`, `jira.url`, `jira.status_at_entry`, `jira.transitions_applied[]` |
| 도구 | `mcp__atlassian__getJiraIssue`, `getTransitionsForJiraIssue`, `transitionJiraIssue`, `ge put` |
| 성공 | 목표 상태로 전이 완료(또는 이미 그 상태) + summary/description 원문이 상태에 저장됨 |
| 실패 | transition 매칭 실패 → **사용자에게 후보 목록을 제시하고 물어본다**. 티켓 조회 실패 → 중단 후 보고. |

전이는 반드시 `to.name` 으로 매칭한다. 전이 → `ge jira-transition --node N1 …` 기록 →
`ge node N1 --status running` 순서다. 기록 없이는 런타임이 진입을 거부한다(exit 2).
**재진입할 때마다 다시 전이한다.** 상세: `jira.md`.

---

## N2 — 요구사항 분석 + 저장소 컨텍스트 (fan-out 3 → join)

세 에이전트를 **한 메시지에서 동시에** 띄운다.

### N2a `ge-jira-analyst` — 티켓 파싱·양식 검증
| 항목 | 내용 |
|---|---|
| 입력 | `jira.raw_description_md`, `jira.summary` |
| 출력 | `requirements.*` (target, background, definition_of_done_scope, acceptance_criteria[], out_of_scope[], constraints, format_valid, format_violations[]) |
| 도구 | Read(상태), `ge put`, atlassian 읽기 |
| 성공 | `format_valid == true` 이고 `acceptance_criteria` 가 1개 이상 |
| 실패 | 양식 위반 → `format_violations` 를 채우고 `requirements.draft_proposed` 에 **완료 조건 초안**을 넣은 뒤 join 에서 그래프를 멈춘다 |

### N2b `ge-impact-scout` — 영향 범위 조사
| 항목 | 내용 |
|---|---|
| 입력 | `requirements`(또는 티켓 원문), `runtime.graphify` |
| 출력 | `impact.files[]`, `impact.modules[]`, `impact.risk_notes[]` |
| 도구 | **`graphify-out/` 있으면 graphify skill 최우선**, 없으면 Grep/Glob/Read. 읽기 전용. |
| 성공 | 변경이 닿을 파일/모듈 후보와 근거가 상태에 실림 |
| 실패 | 근거 없음 → 빈 배열 + `risk_notes` 에 "탐색 실패 사유" 를 남긴다. 추측으로 채우지 않는다. |

### N2c `ge-convention-scout` — 컨벤션·아키텍처·빌드
| 항목 | 내용 |
|---|---|
| 입력 | `runtime.build`, `runtime.project_state`, `runtime.graphify` |
| 출력 | `conventions.*` (origin, evidence[], architecture, naming, error_handling, testing, formatting, confidence, open_questions[]) |
| 도구 | graphify 우선 / Grep·Glob·Read. 읽기 전용. |
| 성공 | `confidence` 판정 + 근거(`evidence`) 명시 |
| 실패 | 근거 빈약·패턴 충돌 → `confidence: "low"` 와 `open_questions` 를 채운다. **추측으로 확정하지 않는다.** |

### N2-join (오케스트레이터)
- `format_valid == false` → **그래프 정지**. `format_violations` 와 `draft_proposed` 를 사용자에게
  보고하고, 사용자가 티켓을 고치거나 초안을 승인할 때까지 대기한다. (`/graph-resume` 으로 재개)
- 정상 → `ge node N3 --status running`

---

## N3 — 계획 수립 · `ge-planner` → **GATE-PLAN**

| 항목 | 내용 |
|---|---|
| 책임 | 요구사항·영향범위·컨벤션을 근거로 실행 계획 작성. greenfield 면 "이번에 정할 아키텍처·컨벤션" 포함. `conventions.open_questions` 가 있으면 계획에 질문으로 올린다. |
| 입력 | `requirements`, `impact`, `conventions`, `runtime` |
| 출력 | `.claude/graph/tmp/<TICKET>-plan.md` 파일, `plan.markdown`, `plan.hash`, `plan.created_at`, `gates.pending` |
| 도구 | Read, graphify(있으면), Write(`.claude/graph/tmp/` 만), `ge put`, `ge gate open` |
| 성공 | 사용자가 `approve` 입력 → 훅이 GATE-PLAN 승인 기록 |
| 실패 | 반려 → 사유를 반영해 계획을 고치고 게이트를 **다시 연다**. 승인 없이는 N4 진입 불가(훅 차단). |

계획에 반드시 포함할 것: 완료 조건별 구현 방법 / TDD 로 먼저 쓸 테스트 목록 / 변경 파일 목록 /
`범위 밖`·`제약` 준수 방법 / 확정이 필요한 컨벤션 질문 / 위험과 롤백.

`runtime.build.spring.boot == true` 이고 이번 티켓이 엔드포인트를 건드리면 **추가로 필수**:
문서화할 엔드포인트별 `document()` 식별자(REST Docs 가 아직 없으면 도입 자체를 계획에 포함 —
생략 불가) / 각 엔드포인트의 RESTful 설계(URI·메서드·상태코드, `references/restful-api.md`).

레이어를 갖춘 기존 JVM 프로젝트(`runtime.build.arch_rules.required == true`)이고 이번
티켓이 프로덕션 코드를 건드리면 **추가로 필수**: ArchUnit 이 아직 없으면 도입(의존성 +
레이어 의존 방향/순환 금지/네이밍 규칙)을 계획에 포함한다 — 생략 불가(`references/archunit.md`).

---

## N4 — TDD 개발 · `ge-tdd-developer`

| 항목 | 내용 |
|---|---|
| 책임 | 티켓 상태를 `개발 중` 으로 전이(**매 진입마다, 구현 착수 이전에**) → Red→Green→Refactor 로 구현 |
| 입력 | `plan`, `requirements`(특히 `out_of_scope`, `constraints`), `conventions`, `runtime.build`, 직전 `tests.attempts`/`review.rounds` |
| 출력 | 소스 변경, `tdd.attempts[]`(훅이 자동 기록), `changed_files` |
| 도구 | Read/Edit/Write/Grep/Glob, Bash(gradle), graphify(있으면), `ge` |
| 성공 | 계획한 변경이 반영되고 로컬 컴파일이 통과 |
| 실패 | 구현 불가 판단 → `ge node N4 --status failed --note "<사유>"` 후 사용자 보고 |

하드 제약:
- **진입할 때마다** 먼저 `개발 중` 으로 전이하고 기록한다. N5/N6 에서 되돌아온 진입도 예외가 아니다
  (그 사이 티켓은 `검증 중` 에 가 있다). 기록이 없으면 `ge node N4` 가 거부한다.
- `requirements.out_of_scope` 에 있는 것은 **하지 않는다**.
- `requirements.constraints`(하위호환/DB 스키마/성능·보안)를 **위반하지 않는다**.
- `conventions` 를 **위반하지 않고 새 컨벤션을 발명하지 않는다**.
- 반드시 테스트를 먼저 쓴다. 훅이 Red-First 를 강제한다.
- `runtime.build.spring.boot == true` 면 `runtime.build.api_docs.required` 는 **항상**
  `true` 다(REST Docs 의존성 유무와 무관, 조건부 아님). 컨트롤러를 건드리는 순간 **같은
  Red 단계에서** REST Docs 테스트도 쓴다 (`references/spring-restdocs.md`). 의존성이 아직
  없으면 계획대로 먼저 도입한다.
- 같은 조건에서 엔드포인트는 **RESTful 하게 설계한다** — 자원 중심 URI, 의미에 맞는 HTTP
  메서드, 표준 상태코드 (`references/restful-api.md`). REST Docs 스니펫이 그 계약을
  그대로 담아야 한다.
- `runtime.build.arch_rules.required == true`(레이어 있는 기존 JVM 프로젝트)면 ArchUnit
  도입 여부와 무관하게 **조건부 아님**. 아직 없으면 계획대로 Red 단계에서 규칙 테스트를
  함께 쓰고, 이미 있으면 기존 규칙을 위반하지 않는다(`references/archunit.md`).

---

## N5 — 테스트 · `ge-test-runner`

| 항목 | 내용 |
|---|---|
| 책임 | 티켓 상태를 `검증 중` 으로 전이(**매 진입마다, 테스트 실행 이전에**) → 탐지된 Gradle 태스크로 테스트 실행, 결과를 원문 그대로 기록 |
| 입력 | `runtime.build.invoke`, `runtime.build.test_task` |
| 출력 | `tests.attempts[]` (command, exit_code, raw_log_path, failure_excerpt_verbatim), `api_docs`(Spring Boot 면), `arch_rules`(레이어 있는 기존 JVM 프로젝트면) |
| 도구 | Bash(gradle), `ge record-test`, `ge apidocs check`, `ge archunit check`. **소스 수정 권한 없음.** |
| 성공 | `exit_code == 0` **그리고** (해당되면) `ge apidocs check`·`ge archunit check` 통과 → N6 |
| 실패 | `ge retry --edge n5_to_n4` → exit 0 이면 N4 재실행 / exit 3(상한 초과)이면 **정지·에스컬레이션** |

**테스트를 돌리기 전에** 티켓을 `검증 중`(`config.jira.status_targets.N5_on_verify`)으로 전이하고
`ge jira-transition --node N5 …` 로 기록한다. 기록 없이는 `ge node N5` 도 `ge record-test` 도
거부된다(exit 2). N4 에서 되돌아온 재진입도 **매번** 다시 전이한다.

테스트가 통과하면 **API 문서화를 실측**한다(Spring Boot 프로젝트면 항상, REST Docs 도입
여부와 무관): `ge apidocs check` 가 변경된 컨트롤러와 생성된 스니펫을 대조한다. 누락(exit 2)이면
테스트 실패와 **동일하게** N5→N4 역방향 엣지를 탄다 — N4 가 문서화 테스트를 써야 한다.

이어서 **아키텍처 규칙을 실측**한다(레이어가 있는 기존 JVM 프로젝트면 항상): `ge archunit check`
가 ArchUnit 도입 여부와 이번 시도의 프로덕션 변경을 대조한다. 누락(exit 2)이면 동일하게
N5→N4 로 돌아간다. ArchUnit 이 이미 도입돼 있으면 규칙 위반 자체는 방금 돌린 Gradle 테스트가
이미(보통의 테스트 실패로) 잡아낸다 — `ge archunit check` 는 그 경우 추가로 더 볼 게 없다.

---

## N6 — 코드리뷰 (fan-out 4 → join) → **GATE-REVIEW**

리뷰 에이전트는 **개발 에이전트와 완전히 분리**돼 있고 **읽기 전용 도구만** 갖는다.
자기 코드를 자기가 통과시키는 경로가 없다.

| 에이전트 | 관점 |
|---|---|
| `ge-review-correctness` | 정합성·버그·엣지케이스·요구사항 충족 |
| `ge-review-architecture` | 아키텍처·컨벤션 준수, `out_of_scope`/`constraints` 위반, RESTful API 설계 위반(Spring Boot), ArchUnit 미도입·위반(레이어 있는 기존 JVM) |
| `ge-review-tests` | 테스트 커버리지, TDD 준수(원장 대조), 테스트 품질, API 문서화 누락/품질(Spring Boot) |
| `ge-review-security` | 보안·예외처리·입력검증·비밀정보 |

각 리뷰어는 findings JSON 배열을 낸다:
```json
{"id":"C1","dimension":"correctness","severity":"blocking|major|minor|info",
 "file":"src/...","line":42,"summary":"한 줄","detail":"근거","suggestion":"","evidence":"인용"}
```

### N6-join `ge-review-synthesizer`
- 중복 제거, 심각도 재판정, `.claude/graph/tmp/<TICKET>-review.md` 작성, `ge record-review`
- `blocking` 이 하나라도 있으면 → `ge retry --edge n6_to_n4` → N4 (상한 초과 시 정지)
- `blocking` 이 없으면 → `ge gate open --gate GATE-REVIEW --artifact-file <리뷰파일>` 후
  사용자에게 리뷰 결과 전문을 제시하고 승인을 요청한다.

| 항목 | 내용 |
|---|---|
| 성공 | 사용자가 `approve` → N7 |
| 실패 | 반려 → 사유를 findings 에 반영하고 N4 재실행(카운터 증가) |

---

## N7 — 커밋 · `ge-commit-writer` → **GATE-COMMIT**

| 항목 | 내용 |
|---|---|
| 책임 | `git diff` 근거로 컨벤셔널 커밋 메시지 작성 → 승인 → 커밋 실행 |
| 입력 | `changed_files`, `plan`, `review`, `ticket` |
| 출력 | `.claude/graph/tmp/<TICKET>-commit.txt`, `commit.message`, `commit.hash`, `commit.at` |
| 도구 | Bash(git diff/log/status/add/commit), `ge gate open`, `ge put` |
| 성공 | 커밋 해시가 상태에 기록됨 |
| 실패 | 훅 차단 → 차단 사유를 읽고 메시지를 고쳐 게이트를 다시 연다 |

```bash
.claude/graph/bin/ge changed-files
git add -A
printf '%s\n' "feat(PPS-283): 요약" "" "본문" > .claude/graph/tmp/PPS-283-commit.txt
.claude/graph/bin/ge gate open --gate GATE-COMMIT --artifact-file .claude/graph/tmp/PPS-283-commit.txt
# ← 사용자 approve 대기
git commit -F .claude/graph/tmp/PPS-283-commit.txt   # 또는 -m "..." (본문 없을 때)
.claude/graph/bin/ge put commit.hash --value "$(git rev-parse HEAD)"
```

**푸시·PR 금지.**

---

## N8 — 완료 조건 체크 · `ge-jira-reporter`

| 항목 | 내용 |
|---|---|
| 책임 | `요구사항 > 완료 조건` 체크박스를 **실제 구현 결과에 맞춰** 갱신 |
| 입력 | `requirements.acceptance_criteria[]`, `tests.attempts`, `review.rounds`, `commit` |
| 출력 | `requirements.acceptance_criteria[].status`/`evidence`, `jira_updates.acceptance_updated` |
| 도구 | `getJiraIssue`(adf), `editJiraIssue`, `ge put` |
| 성공 | 티켓 본문의 체크박스가 갱신되고, 각 체크에 근거(커밋/테스트/파일)가 상태에 남음 |
| 실패 | ADF 구조를 안전하게 바꿀 수 없으면 **쓰지 말고** 변경안을 사용자에게 제시한다 |

**근거 없이 체크하지 않는다.** 구현되지 않은 항목은 체크하지 않고 그대로 둔다.
절차: `jira.md` 의 "완료 조건 ADF 갱신".

---

## N9 — 요약 댓글 · `ge-jira-reporter`

| 항목 | 내용 |
|---|---|
| 책임 | 무엇을 왜 어떻게 바꿨는지 + 변경 파일 + 테스트 결과 + 남은 이슈를 댓글로 |
| 입력 | 상태 전체 |
| 출력 | `jira_updates.comment_id`, `ge node DONE --status done` |
| 도구 | `mcp__atlassian__addCommentToJiraIssue`, `ge` |
| 성공 | 댓글 등록 + 그래프 종료 보고 |
| 실패 | 댓글 실패 → 본문을 사용자에게 그대로 출력하고 수동 등록을 요청 |

---

## N10 — 지식 그래프 최신화 · `ge-graphify-updater`

| 항목 | 내용 |
|---|---|
| 책임 | 커밋된 변경을 `graphify-out/` 에 **증분 반영**해 다음 티켓의 탐색 노드가 낡은 그래프를 보지 않게 한다 |
| 입력 | `runtime.graphify`(N0 스냅샷 `stats` 포함), `changed_files` |
| 출력 | `graphify_update{status,before,after,changed,delta}`, 갱신된 `graphify-out/`, (갱신됐으면) `graphify-out/` 자체 커밋 |
| 도구 | `Skill(graphify, "--update")`, Bash, `ge record-graphify`. **소스 수정 권한 없음** — `graphify-out/` 만 쓰고 커밋한다. |
| 성공 | 증분 최신화 완료 또는 해당 없음(skipped) → `ge node DONE` |
| 실패 | **비차단.** `--status failed` 로 사실을 남기고 DONE 으로 간다. 그래프를 멈추지 않는다. |

분기:
- `runtime.graphify.available == false` → **아무 것도 하지 않는다.** 전체 빌드는 비용이 크고
  티켓 범위 밖이다. `ge record-graphify --status skipped`.
- `mode == "dir-only"`(graph.json 없음) → 증분 기준이 없다. 최신화하지 않고 사용자에게 알린다.
- `available && mode == "graph.json"` → `Skill(graphify, "--update")`.

왜 여기(맨 끝)인가:
- **커밋 뒤**여야 커밋된 코드가 색인된다.
- **맨 마지막**이어야 graphify 실패가 JIRA 보고(N8·N9)를 막지 않는다 — 노드 단위 실패 격리.

`ge record-graphify` 는 N0 에 찍어둔 `runtime.graphify.stats` 를 before 로 삼아 실측 대조한다.
`--status ok` 인데 `changed == false` 면 경고가 남는다 — 성공을 지어낼 수 없다.

**자체 커밋 — GATE 없이:** `changed: true` 이고 `graphify-out/` 이 `.gitignore` 대상이
아니면, N10 은 `graphify-out/` 만 스테이징해 `feat(graphify): update` 로 직접 커밋한다.
N7 의 GATE-COMMIT/GATE-REVIEW 승인 절차를 타지 않는다 — 훅(`guard_commit`)이
(a) 현재 노드가 N10, (b) 메시지가 `feat(graphify): update` 와 정확히 일치, (c) 스테이징된
파일이 전부 `graphify-out/` 아래 세 조건을 모두 실측 확인했을 때만 예외를 통과시킨다
(`config.commit.graphify`). 하나라도 어긋나면 일반 커밋 규칙(컨벤션·GATE-REVIEW·
GATE-COMMIT)으로 떨어져 차단된다 — 이 예외가 리뷰를 우회하는 일반 통로가 될 수 없다.

---

## AMEND — 티켓 요구사항 수정 · `ge-ticket-amender` → **GATE-TICKET**

**주 경로가 아니다.** 게이트 반려 사유가 **(B) 요구사항 불일치**일 때만 진입하는 곁가지다.
(A) 산출물 품질 문제면 여기 오지 않고 해당 노드를 다시 수행한다 (`references/amend.md`).

| 항목 | 내용 |
|---|---|
| 책임 | 사용자의 반려 사유를 근거로 JIRA 티켓 본문 수정안 작성 → GATE-TICKET → 반영 |
| 입력 | `gates.approvals`(마지막 반려 사유), `jira.raw_description_md`, `requirements`, `ticket_amendments` |
| 출력 | `ticket_amendments[]`(원본 스냅샷·제안·해시·반영시각), 수정된 JIRA 티켓, **무효화된 `plan`** |
| 도구 | Read/Write(`.claude/graph/tmp/` 만), `ge amend`, `mcp__atlassian__getJiraIssue`/`editJiraIssue`. **소스 수정 권한 없음.** |
| 성공 | 사용자가 `approve` → 훅이 GATE-TICKET 승인 기록 → JIRA 반영 → `ge amend applied` → **N2 재진입** |
| 실패 | 반려 → 수정안을 고쳐 게이트를 다시 연다. 상한(`retries.gate_to_amend`, 2회) 초과 시 **정지·에스컬레이션** |

강제:
- `mcp__atlassian__editJiraIssue` 는 PreToolUse 훅이 막는다. `AMEND`/`N8`/`N9` 외의 노드에서는
  아예 차단, `AMEND` 에서는 **GATE-TICKET 승인이 제안 본문 해시에 묶여** 있어야 통과한다.
- **모델이 요구사항을 만들어낼 수 있는 경로가 없다.** 티켓에 넣는 모든 문장의 근거는
  사용자의 반려 사유 원문이다.
- `ge amend applied` 가 `plan` 을 비운다 → GATE-PLAN 승인은 계획 해시에 묶여 있으므로
  **자동으로 무효**가 된다. 새 계획을 세우고 다시 승인받아야 N4 로 갈 수 있다.
- `original_md` 를 남겨 사용자가 원본으로 되돌릴 수 있게 한다.
