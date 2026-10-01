---
name: graph-engineering
description: JIRA 티켓 하나로 끝까지 굴러가는 개발 파이프라인의 그래프 SOP. 노드(N0~N10)·엣지·공유 상태·승인 게이트·재시도 정책·TDD 규칙·커밋 컨벤션을 정의한다. /graph-run, /graph-status, /graph-resume, /graph-abort 로 진입하며, 그래프를 켜지 않으면 아무 것도 하지 않는다.
---

# Graph Engineering — 그래프 SOP

개발 작업을 **노드(일하는 주체) · 엣지(라우팅) · 공유 상태(노드 사이를 흐르는 데이터)** 로
표현한 명시적 방향 그래프다. 오케스트레이터(= 이 스킬을 읽은 메인 세션)는 노드를 순서대로
호출하고, 상태를 갱신하고, 엣지 조건을 판정한다. **노드의 실제 작업은 전부 서브에이전트가 한다.**

## 0. 절대 규칙

1. **상태는 `.claude/graph/bin/ge` 로만 읽고 쓴다.** 상태 JSON 을 Edit/Write/셸로 직접
   건드리면 훅이 차단한다.
2. **승인 게이트는 우회 불가능하다.** 상시 3곳(GATE-PLAN, GATE-REVIEW, GATE-COMMIT)
   \+ 조건부 1곳(**GATE-TICKET** — AMEND 노드에서 JIRA 티켓 본문을 고칠 때).
   승인 레코드는 사용자가 채팅에 직접 입력한 `approve`/`승인` 을 `UserPromptSubmit` 훅이
   받아 기록할 때만 생긴다. 오케스트레이터도 서브에이전트도 승인을 발화할 수 없다.
   "승인받았다고 가정하고 진행"은 존재하지 않는 경로다 — 훅이 물리적으로 막는다.
3. **푸시·PR 은 이 그래프의 범위가 아니다.** 사용자가 명시적으로 요청하지 않는 한 하지 않는다.
4. **CI/CD 파일은 티켓·승인된 계획이 요구하면 만들 수 있다.** 단, 그래프의 검증 기준(N5 테스트
   통과 판정)은 CI 결과가 아니라 로컬 Gradle 실행과 훅이다.
5. 실패한 노드는 상태에 실패 사유를 남기고 **정의된 엣지로만** 빠져나간다. 실패를 안고
   다음 노드로 흘려보내지 않는다.
6. 재시도 상한을 넘기면 **멈추고 사용자에게 에스컬레이션**한다. 무한 루프 금지.

## 1. 런타임 탐지 — 매 실행마다 다시 판정한다

저장소 사실을 SOP 에 박아두지 않는다. N0 에서 탐지해 상태에 싣고, 이후 모든 노드가 그 값을 쓴다.

```bash
.claude/graph/bin/ge detect all     # runtime.graphify, runtime.build, runtime.project_state 를 채운다
```

- **`runtime.graphify.available == true`** → 그 실행에서 코드베이스를 탐색하는 **모든** 노드는
  `grep`/`read`/`glob` 보다 **`graphify` skill 을 최우선으로 적극 사용**한다.
  `false` → 일반 탐색으로 폴백한다. 이 분기는 모든 탐색 에이전트 프롬프트에 명시돼 있다.
- **`runtime.build`** → Gradle wrapper 유무, 멀티모듈, DSL, version catalog, 테스트 태스크,
  테스트 프레임워크, 소스/테스트 루트. `status` 가 `detected` 가 아니면 **멈추고 사용자에게 묻는다**
  (`references/gradle.md`).
- **`runtime.build.spring` / `runtime.build.restdocs` / `runtime.build.api_docs`** →
  Spring Boot 여부·웹 스택(mvc/webflux), REST Docs 사용 여부와 flavor·스니펫 경로.
  **`spring.boot == true` 면 `api_docs.required` 는 항상 `true` 다.** REST Docs 의존성이
  아직 없어도 마찬가지다 — 그 경우 도입 자체가 이번 티켓의 일이 된다. **조건부가 아니다.**
  API 문서화는 TDD 사이클의 일부로 강제된다(`references/spring-restdocs.md`).
  같은 조건에서 **API 구현은 RESTful 설계 규칙도 강제**된다(`references/restful-api.md`).
- **`runtime.build.archunit` / `runtime.build.arch_rules`** → ArchUnit 사용 여부.
  **레이어를 갖춘 기존 JVM 프로젝트면 `arch_rules.required` 는 항상 `true` 다.** ArchUnit
  의존성이 아직 없어도 마찬가지다 — **조건부가 아니다.** 아키텍처 규칙도 TDD 사이클의
  일부로 강제된다(`references/archunit.md`).
- **`runtime.build.test_framework`** → 주 테스트 프레임워크. Kotest·Spock 은 JUnit Platform
  위에서 돌기 때문에 구체적인 것이 먼저 매칭된다(Kotest 프로젝트가 junit5 로 잡히지 않는다).
- **`runtime.build.language`** → 소스 파일을 **세서** 정한 주 언어(kotlin/java)와 혼재 여부.
  빌드 스크립트 DSL 과 별개다. 테스트 문법은 **① 지배적 패턴 → ② 탐지된 `test_framework`
  → ③ `test_framework_suggested`(greenfield 전용)** 순으로 정하고,
  **언어를 바꾸거나 새 프레임워크를 들이지 않는다**(Java 에 Kotest 강요 금지).
- **`runtime.project_state`** → `existing`(기존 코드 있음) / `greenfield`(신규·빈 프로젝트).
  컨벤션 경로가 갈린다 (`references/conventions.md`).

## 2. 노드

| 노드 | 책임 | 담당 에이전트 |
|---|---|---|
| N0 | 선행조건: clean tree → base 최신화 → 티켓 브랜치 생성/체크아웃 → 런타임 탐지 | 오케스트레이터 |
| N1 | 티켓 상태를 `검토 중` 으로 전이(**진입할 때마다, 먼저**) 후 티켓 수신 | `ge-jira-analyst` |
| N2 | 요구사항 분석 + 저장소 컨텍스트 수집 (**3-way 병렬 fan-out → join**) | `ge-jira-analyst` / `ge-impact-scout` / `ge-convention-scout` |
| N3 | 계획 작성 → **GATE-PLAN** | `ge-planner` |
| N4 | 티켓 상태를 `개발 중` 으로 전이(**진입할 때마다, 먼저**) 후 **TDD** 개발 (Red→Green→Refactor) | `ge-tdd-developer` |
| N5 | 티켓 상태를 `검증 중` 으로 전이(**진입할 때마다, 먼저**) 후 Gradle 테스트 수행 | `ge-test-runner` |
| N6 | 코드리뷰 (**4-way 병렬 fan-out → join**) → **GATE-REVIEW** | `ge-review-*` 4종 + `ge-review-synthesizer` |
| N7 | 컨벤셔널 커밋 메시지 작성 → **GATE-COMMIT** → 커밋 실행 | `ge-commit-writer` |
| N8 | JIRA `요구사항 > 완료 조건` 체크박스 갱신 | `ge-jira-reporter` |
| N9 | 개발 요약을 티켓 댓글로 작성 | `ge-jira-reporter` |
| N10 | **graphify 지식 그래프 증분 최신화** (비차단) | `ge-graphify-updater` |
| AMEND | 게이트 반려가 **요구사항 불일치**일 때 JIRA 티켓 본문 수정 → **GATE-TICKET** → N2 재진입 | `ge-ticket-amender` |

각 노드의 **입력 상태 / 출력 상태 / 사용 도구 / 성공 조건 / 실패 시 라우팅**은
`references/nodes.md` 에 전부 정의돼 있다. 노드를 실행하기 전에 그 노드 절을 읽어라.

### 병렬화 판단 (agent team)
- **fan-out 하는 곳**: N2(티켓 파싱 / 영향 범위 / 컨벤션·빌드) — 서로 독립적이고 컨텍스트가
  섞이면 오히려 오염된다. N6(정합성·버그 / 아키텍처·컨벤션 / 테스트 커버리지 / 보안·예외처리)
  — 관점이 다르면 놓치는 것이 다르다.
- **접는 곳**: N1·N3·N4·N5·N7·N8·N9 는 단일 에이전트. 나누면 상태 왕복 비용만 늘고 얻는 게 없다.
- fan-out 은 **한 메시지에 여러 Agent 호출**을 넣어 동시에 띄우고, 전원 종료 후 join 노드가
  결과를 하나로 합친다.

## 3. 엣지

```
N0 → N1 → N2 → N3 ==GATE-PLAN==> N4 → N5 → N6 ==GATE-REVIEW==> N7 ==GATE-COMMIT==> N8 → N9 → N10
                                   ↑          ↓                  ↑
                                   └──────────┘ (테스트 실패 / API 문서화 누락 / 아키텍처 규칙 누락, 상한 3회)
                                   └─────────────────────────────┘ (blocking 지적, 상한 2회)
```

N10 은 **비차단**이다. 실패해도 그래프를 멈추지 않고 사실만 기록한 뒤 DONE 으로 간다.

- 역방향 엣지 진입 시 반드시 카운터를 올린다:
  `ge retry --edge n5_to_n4` / `ge retry --edge n6_to_n4`.
  상한 초과 시 CLI 가 exit 3 을 내고 상태를 `escalated` 로 바꾼다 → **즉시 멈추고 보고**한다.
- 전체 엣지 조건표와 mermaid 다이어그램: `references/edges.md`.

## 4. 승인 게이트 운용

```bash
# 1) 승인 대상 산출물을 파일로 만든다 (게이트 전에는 소스 쓰기가 막혀 있으므로 tmp 를 쓴다)
#    계획:      .claude/graph/tmp/<TICKET>-plan.md
#    리뷰결과:  .claude/graph/tmp/<TICKET>-review.md
#    커밋메시지: .claude/graph/tmp/<TICKET>-commit.txt
# 2) 게이트를 연다 — 산출물 해시와 6자리 코드가 나온다
.claude/graph/bin/ge gate open --gate GATE-PLAN \
  --artifact-file .claude/graph/tmp/PPS-283-plan.md \
  --summary "계획 승인 요청"
# 3) 사용자에게 산출물 전문과 코드를 제시하고, 다음 문장으로 승인을 요청한다:
#    "승인하시려면 `approve` (또는 `승인`) 를, 반려하시려면 `reject <사유>` 를 입력해 주세요. (code: ab12cd)"
# 4) 사용자가 입력하면 훅이 승인을 기록하고 다음 지시를 컨텍스트로 주입한다. 그 전에는 진행 금지.
```

- 승인은 **산출물 해시에 묶인다.** 승인 후 계획이나 커밋 메시지를 고치면 해시가 달라져
  훅이 다시 막는다. 고쳤으면 게이트를 다시 열어라.
- 사용자가 반려하면 **사유를 분류**한다:
  - **(A) 산출물 품질 문제** → 해당 노드를 다시 수행한다. 티켓은 건드리지 않는다.
  - **(B) 요구사항 불일치**(티켓 내용이 사용자가 원하는 것과 다름) → **AMEND** 로 간다.
    티켓 수정안을 만들어 **GATE-TICKET** 승인을 받고, 반영 후 N2 부터 다시 흐른다.
    상한 `retries.gate_to_amend`(기본 2회). 자세한 절차: `references/amend.md`.
  - **애매하면 지어내지 말고 사용자에게 어느 쪽인지 묻는다.**

## 5. TDD (N4)

`Red → Green → Refactor`. 훅이 **Red-First** 를 강제한다: 그 N4 시도에서 테스트 파일 쓰기가
한 번도 없으면 프로덕션 소스 쓰기가 차단된다(`tdd.mode = block`).

- 테스트 대상이 아닌 파일(빌드 스크립트, 문서, 리소스 등)은 `config.json` 의 `tdd.exempt_globs`
  로 이미 통과한다. 그 밖의 예외가 필요하면:
  `ge tdd exempt --path <경로> --reason "<사유>"` — **예외는 상태에 남아 N6 리뷰에 그대로 노출된다.**
- 각 N4 시도의 테스트/프로덕션 쓰기 순서는 PostToolUse 훅이 자동으로 원장에 기록한다.

### API 문서화는 TDD 안에 있다 (Spring Boot — 조건부 아님, 항상 강제)

`runtime.build.spring.boot == true` 면 그 자체로 **문서화가 필수**다.
`restdocs.available == false`(아직 spring-restdocs 의존성이 없음)는 **면제 사유가 아니다** —
도입 자체가 이번 티켓의 일이 된다. 별도 단계가 아니라 **Red 단계에서 API 테스트에
`document(...)` 를 함께 넣는다.** 테스트가 통과해야 스니펫이 생기므로 문서가 구현과
어긋날 수 없다.

| 노드 | 하는 일 |
|---|---|
| N3 | 문서화할 엔드포인트와 `document()` 식별자를 **테스트 목록에 함께** 계획한다. REST Docs 가 없으면 **도입을 계획에 넣는다(생략 불가 — "이번엔 문서화하지 않음" 은 더 이상 유효한 선택지가 아니다).** |
| N4 | Kotest(또는 지배적 프레임워크) + REST Docs 로 Red 를 쓴다. 컨트롤러만 고치고 문서화가 없으면 훅이 **경고**한다. |
| N5 | 테스트 통과 후 `ge apidocs check` — 스니펫과 테스트를 **실측**해 상태에 기록한다. REST Docs 가 아직 없으면 변경 컨트롤러 전부가 `missing` 으로 잡혀 도입을 강제하는 효과를 낸다. |
| N6 | `ge-review-tests` 가 `api_docs.missing`·`exemptions`·문서 품질을, `ge-review-architecture` 가 RESTful 설계 위반을 판정한다. |
| 커밋 | 미해소 누락이 있거나 **검사를 돌리지 않았으면 훅이 차단**한다. |

- 컨트롤러 판정은 파일명이 아니라 **내용**(`@RestController`, `@*Mapping`, `RouterFunction` …)으로 한다.
- 문서화 "대상이 아닌" 것(actuator/health 등)만 `ge apidocs exempt --path <경로> --reason "<사유>"` —
  **리뷰에 그대로 노출된다.** 귀찮음·시간 부족을 사유로 쓰지 않는다.
- Spring Boot 가 아니면 이 절 전체가 발동하지 않는다(REST Docs 자체가 Spring 전용이다).
- 실제 구현은 **RESTful 설계 규칙**도 함께 강제된다(자원·메서드·상태코드) — `references/restful-api.md`.
  REST Docs 스니펫은 이 규칙을 지킨 **RESTful 명세**로 나와야 한다.
- 작성 패턴·검증 방식: `references/spring-restdocs.md`.

### 아키텍처 규칙(ArchUnit)도 TDD 안에 있다 (조건부 아님, 항상 강제)

레이어를 갖춘 기존 JVM 프로젝트(`language.primary` kotlin/java + 소스가 이미 있음)면
그 자체로 **아키텍처 규칙 도입이 필수**다. `archunit.available == false`(아직 의존성이
없음)는 **면제 사유가 아니다** — 도입 자체가 이번 티켓의 일이 된다.

| 노드 | 하는 일 |
|---|---|
| N3 | ArchUnit 이 없으면 도입(의존성 + 레이어 의존 방향/순환 금지/네이밍 규칙)을 계획에 넣는다(생략 불가). |
| N4 | ArchUnit 이 없으면 Red 단계에서 규칙 테스트를 함께 쓴다. 이미 있으면 평소처럼 구현하되 새 레이어를 도입하면 그 규칙도 추가한다. |
| N5 | 테스트 통과 후 `ge archunit check` — 도입 흔적을 실측한다. 이미 도입돼 있으면 위반은 방금 돈 일반 테스트 실행이 이미 잡는다. |
| N6 | `ge-review-architecture` 가 규칙 미도입·기존 규칙 위반을 판정한다. |
| 커밋 | 미해소 누락이 있거나 **검사를 돌리지 않았으면 훅이 차단**한다. |

- REST Docs 와 달리 파일 단위 1:1 대응이 아니라 **프로젝트 전체**에 적용된다. 강제 방식도
  "이번 티켓에 도입 흔적이 있는가" 를 본다 — `references/archunit.md` §6.
- greenfield(소스가 아직 없는 프로젝트)나 JVM 이 아니면 이 절 전체가 발동하지 않는다 —
  강제할 레이어가 없다.
- 작성 패턴·검증 방식: `references/archunit.md`.

## 6. Gradle

명령은 **탐지된 값으로 구성**한다. 하드코딩 금지.

```bash
INVOKE=$(.claude/graph/bin/ge show --field runtime.build.invoke)   # ./gradlew 또는 gradle
TASK=$(.claude/graph/bin/ge show --field runtime.build.test_task)
$INVOKE $TASK 2>&1 | tee .claude/graph/tmp/test.log
.claude/graph/bin/ge record-test --command "$INVOKE $TASK" --exit-code ${PIPESTATUS[0]} --log .claude/graph/tmp/test.log
```

실패 출력은 **요약하지 않는다.** 원문 전체가 `.claude/graph/logs/<TICKET>/n5-attempt-N.log` 에
저장되고, 상태에는 그 경로와 원문 발췌(`failure_excerpt_verbatim`)가 들어간다.
자세한 규칙: `references/gradle.md`.

## 7. 커밋 (N7)

```
type(scope:JIRA ticket):subject
본문(선택)
```
- scope 는 **반드시 현재 티켓 키**. 예: `feat(PPS-283): 코드 일관성 수정`
- 형식 위반·GATE-REVIEW 미승인·GATE-COMMIT 미승인·승인 후 메시지 변경은 모두 훅이 차단한다.
- `git commit -m "..."` 형태로만 실행한다(에디터 모드는 메시지를 대조할 수 없어 차단된다).

## 8. JIRA

### 상태 전이는 노드 실행 **이전에**, **매 진입마다** (강제)

| 노드 | 목표 상태 |
|---|---|
| N1 | `검토 중` |
| N4 | `개발 중` |
| N5 | `검증 중` |

- **첫 진입만이 아니다.** N5 실패로 N4 에 돌아오면 다시 `개발 중` 으로, 다시 N5 에 가면
  다시 `검증 중` 으로 전이한다. 티켓 상태는 언제나 "지금 실제로 돌고 있는 노드" 를 가리킨다.
- 순서는 **전이 → 기록 → 노드 실행**이다. 일을 다 하고 상태를 바꾸지 않는다.
- 런타임이 결정론적으로 막는다: 이번 진입의 전이 기록이 없으면
  `ge node N1|N4|N5 --status running` 이 **exit 2** 로 거부되고, N5 는 `ge record-test` 도 거부된다.
  기록은 진입 시 소비되므로 같은 기록으로 두 번 들어갈 수 없다.
  ```bash
  ge jira-target --node N5          # 목표 상태·id 를 config 에서 (하드코딩 금지)
  # getTransitionsForJiraIssue → to.name 매칭 → transitionJiraIssue
  ge jira-transition --node N5 --from "개발 중" --transition-id 5 --transition-name "검증요청"
  ge node N5 --status running
  ```
- 목표 상태와 전이 대상 노드는 `config.jira.transition_policy` 가 정한다(`enforce`: block|warn|off).
- 상태 전이는 **transition 이름이 아니라 목표 상태(`to.name`)로 매칭**한다.
  (실측: PPS 에서 `검토 중` 으로 가는 transition 의 이름은 `착수` 다. 이름 매칭은 반드시 실패한다.)
- 티켓 본문은 정해진 양식을 따라야 한다. `완료 조건` 이 비었거나 없으면 **그래프를 멈추고**
  초안을 제안한 뒤 사용자 승인을 받는다. 요구사항을 지어내지 않는다.
- `범위 밖` 과 `제약` 은 N4·N6 에서 **하드 제약**이다.
- 양식 전문·파싱 규칙·ADF 체크박스 갱신 절차: `references/jira.md`.

## 9. 컨벤션

- `existing` 프로젝트: N2 가 실제 코드에서 추출한다. 근거 우선순위
  ① `CLAUDE.md`·컨벤션 문서·ADR ② 린터/포매터 설정 ③ 코드의 지배적 패턴.
  **N4 는 이를 위반하지 않고 새 컨벤션을 발명하지 않는다. N6 는 위반을 차단 사유로 본다.**
- `greenfield` 프로젝트: 티켓의 `대상`·`제약` 을 컨벤션으로 삼고, 부족하면 N3 계획에
  "이번에 정할 아키텍처·컨벤션" 을 넣어 **승인 후** 확정하고 `CLAUDE.md` 에 기록한다.
- 근거가 빈약하거나 패턴이 엇갈리면 추측하지 말고 N3 에서 사용자에게 확인한다.
- 상태 스키마와 강제 방법: `references/conventions.md`.

## 9-1. 지식 그래프 최신화 (N10)

`runtime.graphify.available == true` 면 N2b·N2c·N4·N6 이 전부 graphify 를 최우선으로 쓴다.
갱신하지 않으면 **다음 티켓이 이번 변경을 모르는 그래프로 판단**하게 된다.

- **커밋 뒤에** 돈다 — 그래야 커밋된 코드가 색인된다.
- **맨 마지막**이다 — graphify 실패가 JIRA 보고(N8·N9)를 막으면 안 된다(실패 격리).
- **증분(`--update`)만** 한다. 전체 재빌드는 비싸고 티켓 범위 밖이다.
- `graphify-out/` 이 없으면 **새로 만들지 않고 건너뛴다**(`status: skipped`).
- `ge record-graphify` 가 `graph.json` 의 mtime·크기·노드 수를 **N0 스냅샷과 실측 대조**한다.
  "돌렸다고 했는데 안 바뀐" 경우가 경고로 드러난다.
- 실제로 갱신됐고(`changed: true`) `graphify-out/` 이 `.gitignore` 대상이 아니면, N10 은
  `graphify-out/` 만 스테이징해 `feat(graphify): update` 로 **자체 커밋**한다. 이 커밋은
  N7 의 GATE-COMMIT 대상이 아니다 — 메시지·노드·스테이징 범위가 정확히 일치할 때만
  훅이 GATE-REVIEW·GATE-COMMIT·컨벤션 검사 없이 통과시킨다(`config.commit.graphify`,
  `hooks/ge_hooks.py guard_commit`). 조건이 하나라도 어긋나면 일반 커밋 규칙으로 떨어진다.

## 10. 참조 문서

| 파일 | 내용 |
|---|---|
| `references/nodes.md` | 노드별 책임/입력/출력/도구/성공조건/실패 라우팅 |
| `references/edges.md` | 엣지 조건표, mermaid, 재시도·실패 격리 정책 |
| `references/state-schema.md` | 공유 상태 전체 스키마와 필드 소유권 |
| `references/jira.md` | 티켓 양식, transition 매칭, 완료 조건 ADF 갱신 |
| `references/conventions.md` | 컨벤션 컨텍스트 산출 규약(existing/greenfield) |
| `references/gradle.md` | 빌드 환경 탐지와 명령 구성 |
| `references/spring-restdocs.md` | Spring Boot API 문서화(Kotest + REST Docs)와 강제 방식 |
| `references/restful-api.md` | RESTful API 설계 규칙(URI·메서드·상태코드)과 강제 지점 |
| `references/archunit.md` | 아키텍처 규칙(ArchUnit) 작성 패턴과 강제 방식 |
| `references/amend.md` | 반려 사유 분류와 티켓 수정(AMEND) 경로, GATE-TICKET 강제 |
| `references/hooks.md` | 훅이 강제하는 것과 차단 메시지 대응법 |
