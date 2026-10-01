# JIRA 연동

접근은 이 환경에 연결된 **Atlassian MCP 도구**로만 한다. `cloudId` 는 `config.json` 의
`jira.cloud_id` 를 쓰고, 없으면 `mcp__atlassian__getAccessibleAtlassianResources` 로 조회한다.

## 1. 상태 전이 — 노드 실행 **이전에**, **매 진입마다**

전이 대상 노드와 목표 상태는 `config.json` 의 `jira.transition_policy.nodes` 가 정한다.

| 노드 | 목표 상태 | status_targets 키 |
|---|---|---|
| N1 (티켓 수신) | `검토 중` | `N1_on_receive` |
| N4 (TDD 개발) | `개발 중` | `N4_on_develop` |
| N5 (테스트 수행) | `검증 중` | `N5_on_verify` |

두 가지가 **강제**된다.

1. **순서** — 전이가 노드의 본 작업보다 **먼저** 끝나야 한다. 티켓 상태는 "지금 무엇이
   진행 중인가" 를 나타내므로, 일을 다 하고 나서 상태를 바꾸면 그 사이 구간이 거짓말이 된다.
2. **매 진입** — 첫 진입만이 아니다. `N5 → N4` 복귀, `N6 → N4` 복귀처럼 되돌아온 진입도
   각각 다시 전이한다. 그래서 재시도 루프에서는 `개발 중` ↔ `검증 중` 이 오간다.

`ge` 런타임이 이것을 결정론적으로 막는다. 전이 기록은 노드에 진입할 때 **소비**되므로,
같은 기록으로 두 번 진입할 수 없다.

```
ge node N5 --status running
  → [ge] N5 진입 거부 — 이번 진입에 대한 JIRA 상태 전이 기록이 없다 (exit 2)
```

`ge record-test`(N5 산출물)에도 같은 검사가 걸려 있어, `ge node N5` 를 건너뛰고
테스트만 돌린 경로도 막힌다.

> **실측 (payprotocol / PPS 워크플로, 2026-09-04)**
> `검토 중`(status 10060) 으로 가는 transition 의 **이름은 `착수`(id 8)**,
> `개발 중`(status 10065) 으로 가는 transition 의 **이름은 `개발착수`(id 3)** 다.
> 즉 **transition 이름으로 매칭하면 반드시 실패한다.**

### 절차 (전이 대상 노드에 진입할 때마다 그대로 반복)

```
0. 목표 상태를 config 에서 확인한다 — 이름을 외우거나 하드코딩하지 않는다
   .claude/graph/bin/ge jira-target --node N5
     → {"target_status": "검증 중", "target_status_id": "10063", ...}

1. mcp__atlassian__getTransitionsForJiraIssue(cloudId, issueIdOrKey)

2. 매칭 순서 (config.jira.match_strategy):
   a. transitions[].to.name 정확 일치
   b. to.name 정규화 일치 (공백 제거·소문자·NFC)
   c. to.id 가 known_status_ids 의 목표 상태 id 와 일치
   d. 전부 실패 → 사용자에게 **전체 transition 목록(id, name, to.name)** 을 보여주고 묻는다

3. mcp__atlassian__transitionJiraIssue(cloudId, issueIdOrKey, transition={"id": <id>})

4. 수행한 전이를 기록한다 — 이 기록이 있어야 노드에 진입할 수 있다
   ge jira-transition --node N5 --from "개발 중" \
        --transition-id 5 --transition-name "검증요청"

5. 이제 노드에 진입한다
   ge node N5 --status running
```

- **이미 목표 상태인 경우**: 전이할 것이 없다. 다만 **조회로 실제 상태를 확인한 뒤**
  `ge jira-transition --node <노드> --already --from "<현재 상태>"` 로 기록한다.
  확인 없이 `--already` 를 쓰지 마라 — 그 순간 이 강제는 무의미해진다.
- 목표 상태가 아닌 값으로는 기록 자체가 거부된다(`--to` 를 넣으면 config 와 대조한다).
- **상태 이름을 코드나 프롬프트에 하드코딩하지 않는다.** 프로젝트별 매핑은 `config.json` 의
  `jira.project_overrides.<PROJECT>` 로 뺀다. 새 프로젝트를 만나면 매칭 결과를 사용자에게
  확인받은 뒤 override 에 추가할 것을 제안한다.
- 전이 강제를 끄거나 경고로 낮추려면 `jira.transition_policy.enforce` 를 `off` / `warn`
  으로 바꾼다(기본 `block`). 노드를 추가·삭제하는 것도 `nodes` 맵 하나로 끝난다.

## 2. 티켓 본문 양식 (강제)

`# 요약` 은 **summary 필드**에, 나머지는 **description** 에 있다. 파싱할 때 둘을 합쳐서 본다.

````
# 요약

[repo] 무엇을 어떻게
예: `[cutting-tuna] Swagger 문서를 springdoc-openapi 로 대체`

# 설명

## 대상
- 저장소:
- 모듈/패키지:
- 진입점(아는 만큼):

## 배경
- 현재(As-Is):
- 원하는 상태(To-Be):
- 왜 해야하는가:

## 완료 정의
( ) 조사, 보고까지 ( ) 배포까지

## 참고
- 관련 PR/이슈:
- 관련 티켓:
- 로그, 재현 데이터:

# 요구사항

## 완료 조건
- [ ] (검증 가능한 문장 하나에 하나씩)
- [ ] ...

## 범위 밖 — 이번에 안 하는 것
-

## 제약
- 하위호환: 유지 필요(호출처: ) / 깨도 됨
- DB 스키마 변경: 허용 / 불가
- 성능, 보안: (없으면 "없음")
````

### 파싱 규칙 (N2a)

| 티켓 섹션 | 상태 필드 |
|---|---|
| summary | `jira.summary` |
| `## 대상` | `requirements.target.{repos,modules,entrypoints}` |
| `## 배경` | `requirements.background.{as_is,to_be,why}` |
| `## 완료 정의` | `requirements.definition_of_done_scope` — `(O)`/`(x)`/`(v)` 로 표시된 쪽 |
| `# 요구사항 > ## 완료 조건` | `requirements.acceptance_criteria[]` |
| `## 범위 밖 …` | `requirements.out_of_scope[]` |
| `## 제약` | `requirements.constraints.{backward_compat,db_schema,perf_security}` |

- 완료 조건은 `### 1. …` 같은 **하위 그룹 헤딩**을 가질 수 있다 → `group` 필드에 넣는다.
- 마크다운 변환기가 체크박스를 `\[ \]` / `\[x\]` 로 이스케이프해서 준다. 둘 다 인식한다.
  이미 `[o]` 인 항목은 `checked_in_ticket: true` 로 표시하고, N8 에서 함부로 되돌리지 않는다.
- 항목 뒤에 `— **확인 완료: …**` 같은 주석이 붙어 있을 수 있다. 본문에서 분리해 보존한다.

### 양식 위반 처리 (N2 → 정지)

필수 섹션(`## 대상`, `## 배경`, `# 요구사항 > ## 완료 조건`, `## 제약`)이 없거나
**`완료 조건` 이 비어 있으면 그래프를 멈춘다.**

1. `requirements.format_valid = false`, `format_violations[]` 에 빠진 섹션을 기록.
2. 티켓 본문에서 **유추한 `완료 조건` 초안**을 `requirements.draft_proposed` 에 넣는다.
   (검증 가능한 문장 단위, 각 항목에 유추 근거를 붙인다)
3. 사용자에게 위반 목록 + 초안을 제시하고 다음 중 하나를 요청한다:
   - 티켓을 직접 수정 → `/graph-resume <TICKET>` 으로 재개
   - 초안 승인 → 승인받은 초안을 티켓에 반영한 뒤 진행
4. **요구사항을 임의로 지어내고 진행하지 않는다.**

## 3. `범위 밖` · `제약` 은 하드 제약

- N4: `out_of_scope` 항목은 **건드리지 않는다**. `constraints` 위반 구현을 하지 않는다.
  (하위호환 유지 필요인데 시그니처 변경, DB 스키마 변경 불가인데 마이그레이션 추가 등)
- N6: 위 위반은 **`severity: blocking`** 으로 판정한다. 논의 대상이 아니다.

## 4. 완료 조건 ADF 갱신 (N8)

마크다운으로 왕복시키면 smartlink(`<custom data-type="smartlink">`) 등 ADF 전용 노드가 깨진다.
**반드시 ADF 를 받아 최소 치환한다.**

```
1. getJiraIssue(cloudId, key, fields=["description"], responseContentFormat="adf")
   → 원본을 .claude/graph/logs/<TICKET>/ticket.adf.json 에 그대로 저장(롤백 근거)
2. `# 요구사항` → `## 완료 조건` 하위의 listItem 들을 순회하며, 첫 text 노드가
   "[ ] " 로 시작하는 항목을 acceptance_criteria 와 매칭한다(텍스트 앞부분 비교).
3. status == "done" 인 항목만 그 text 노드의 "[ ] " → "[o] " 로 바꾼다.
   - 그 외 문자는 한 글자도 바꾸지 않는다.
   - 이미 "[o]" 인 항목은 그대로 둔다.
   - 근거(evidence)가 비어 있으면 절대 체크하지 않는다.
4. editJiraIssue 로 description(ADF)을 통째로 쓴다.
5. 다시 조회해 **바뀐 문자가 의도한 체크박스뿐인지** 확인한다(diff 검증). 아니면 원본으로 되돌린다.
6. ge put jira_updates.acceptance_updated --json --value true
```

ADF 구조를 안전하게 바꿀 수 없다고 판단되면(예: 체크박스가 taskList 노드로 되어 있거나
매칭이 모호함) **쓰지 말고** 변경안을 사용자에게 제시하고 지시를 기다린다.

## 5. 요약 댓글 (N9)

`mcp__atlassian__addCommentToJiraIssue` 로 다음을 남긴다.

```
## 개발 요약 (Graph Engineering)

**무엇을** — …
**왜** — …
**어떻게** — …

### 변경 파일
- path — 한 줄 설명

### 테스트
- `./gradlew test` → 통과 (시도 N회, 마지막 exit 0)
- 추가/수정한 테스트: …

### 완료 조건
- [o] AC1 — 근거: 커밋 abc1234, XxxTest#case
- [ ] AC3 — 미완료 사유: …

### 남은 이슈 / 후속
- …

커밋: `abc1234 feat(PPS-283): …`
```

- 숫자와 결과는 상태에서 그대로 가져온다. 지어내지 않는다.
- 미완료 항목은 숨기지 않고 사유와 함께 적는다.

## 티켓 본문 수정 (AMEND 전용)

티켓 **본문**을 고치는 것은 `AMEND` 노드에서만, **GATE-TICKET 승인 후에만** 가능하다.
`mcp__atlassian__editJiraIssue` 에 PreToolUse 훅이 붙어 있어 그 밖의 경로는 차단된다.

- N8 의 `완료 조건` 체크박스 갱신과 N9 의 댓글은 **여기 해당하지 않는다**(기존 동작 그대로).
- 수정본도 위 §2 의 본문 양식을 그대로 지켜야 한다.
- 승인된 본문과 **다르게** 쓰지 마라 — 승인은 본문 해시에 묶여 있다.
- 원본은 `ticket_amendments[].original_md` 에 보존된다(롤백 근거).

절차와 반려 사유 분류: `amend.md`.
