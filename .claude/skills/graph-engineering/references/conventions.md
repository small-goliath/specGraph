# 컨벤션 컨텍스트 산출 규약

**무엇을 지켜야 하는지는 저장소 상태에 따라 달라진다. 매 실행 시점에 판정한다.**
어떤 특정 저장소의 컨벤션도 이 문서나 에이전트 프롬프트에 하드코딩하지 않는다.

## 0. 두 경로

`runtime.project_state` 로 갈린다 (`ge detect all` 이 판정).

| | `existing` | `greenfield` |
|---|---|---|
| 판정 | Gradle 소스/테스트 루트가 존재 | `build.status == "missing"` 이거나 소스/테스트 루트 없음 |
| 컨벤션 출처 | **실제 코드에서 추출** | 티켓의 `대상`·`제약` → 부족하면 N3 계획에서 확정 |
| N3 | 추출 결과를 계획에 근거로 인용 | "이번에 정할 아키텍처·컨벤션" 절을 계획에 **필수 포함** |
| 승인 | 컨벤션 자체는 승인 대상 아님(코드가 근거) | **GATE-PLAN 승인이 곧 컨벤션 확정** |
| 확정 후 | — | `CLAUDE.md` 에 기록 → 다음 티켓부터 `existing` 취급 |

## 1. `existing` — N2c 가 코드에서 추출한다

근거 우선순위 (충돌 시 위가 이긴다):

1. **`CLAUDE.md`, 컨벤션 문서, ADR** (`docs/adr/**`, `CONTRIBUTING.md`, `docs/convention*`)
2. **린터/포매터 설정** — `.editorconfig`, ktlint, spotless, checkstyle, detekt, PMD,
   `build.gradle` 의 관련 플러그인 블록
3. **실제 코드의 지배적 패턴** — 표본을 세어 다수를 택한다

추출 대상과 상태 필드:

| 관찰 대상 | 상태 필드 | 추출 방법 |
|---|---|---|
| 레이어링/패키지 구조 | `conventions.architecture.{layers,package_root,notes}` | 패키지 트리에서 반복되는 세그먼트(`controller/service/repository`, `domain/application/infrastructure` 등) |
| 클래스·메서드·상수·패키지 네이밍 | `conventions.naming.*` | 접미사 빈도(`*Service`, `*Repository`, `*Dto`), 테스트 메서드 표기 |
| 에러 처리 | `conventions.error_handling` | 커스텀 예외 계층, `@ControllerAdvice`, Result 타입, 로깅 규칙 |
| 테스트 작성 패턴 | `conventions.testing.{framework,location,naming,style}` | 테스트 루트 위치, `*Test`/`*Spec`, given-when-then 주석, 픽스처·목킹 라이브러리 |
| 포맷팅 | `conventions.formatting.{tool,config}` | 위 2번 설정 파일 |

기록 형식:

```bash
.claude/graph/bin/ge put conventions --json --file conventions.json
# conventions.json 예
{
  "origin": "existing-code",
  "evidence": [
    {"kind":"CLAUDE.md","path":"CLAUDE.md","note":"레이어 규칙 명시"},
    {"kind":"formatter","path":".editorconfig","note":"indent 4, LF"},
    {"kind":"dominant-pattern","path":"src/main/java/**/service","note":"27/30 클래스가 *Service"}
  ],
  "architecture": {"layers":["controller","service","repository"],"package_root":"com.x.y","notes":"…"},
  "naming": {"class":"PascalCase + 역할 접미사","method":"camelCase","test":"메서드명_상황_기대결과"},
  "error_handling": "도메인 예외 → @RestControllerAdvice 에서 ErrorResponse 로 변환",
  "testing": {"framework":"junit5","location":"src/test/java","naming":"*Test","style":"given-when-then"},
  "formatting": {"tool":"spotless","config":".editorconfig"},
  "confidence": "high",
  "open_questions": []
}
```

**`confidence` 판정 기준**
- `high` — 1·2번 근거가 있고 3번과 일치
- `medium` — 3번만 있으나 지배적(≥70%)
- `low` — 근거가 빈약하거나 패턴이 엇갈림 → **추측으로 정하지 말고** `open_questions` 에 넣어
  **N3 계획 단계에서 사용자에게 확인**한다

## 2. `greenfield` — 확정 절차

1. 티켓 `## 대상`(저장소/모듈/진입점) 과 `## 제약` 에서 끌어낼 수 있는 것을 먼저 채운다
   (`origin: "ticket"`).
2. 그래도 비는 항목은 N3 계획에 **"이번에 정할 아키텍처·컨벤션"** 절로 올린다. 각 항목은
   선택지와 추천안을 함께 제시한다. 예: 패키지 구조, 레이어링, 테스트 프레임워크·위치·네이밍,
   포매터, 에러 처리 방식, Gradle 세팅(wrapper 버전, DSL, 멀티모듈 여부).
3. **GATE-PLAN 승인이 컨벤션 확정이다.** 승인된 항목을 `conventions.decided_this_run[]` 과
   해당 필드에 쓰고 `origin: "decided-in-plan"` 으로 표시한다.
4. N4 가 끝나기 전에 **`CLAUDE.md` 에 기록**한다.
   - `CLAUDE.md` 가 없으면 새로 만들고, 있으면 `## 코드 컨벤션` 절을 **추가/갱신**한다
     (기존 내용을 덮어쓰지 않는다).
   - 각 항목에 "PPS-283 계획에서 확정" 처럼 출처를 남긴다.
   - 이 파일 변경은 TDD 예외 대상(`*.md`)이라 Red-First 에 걸리지 않는다.
5. 다음 티켓부터 이 `CLAUDE.md` 가 **1순위 근거**가 되어 자동으로 `existing` 경로를 탄다.

## 3. N4 는 어떻게 참조하고 지키는가

- 코드를 쓰기 전에 `ge show --field conventions` 를 읽는다.
- **위반 금지 / 발명 금지.** 컨벤션에 없는 판단이 필요하면:
  ① 인접 코드의 패턴을 따른다 → ② 그래도 모호하면 `conventions.open_questions` 에 추가하고
  사용자에게 묻는다. 임의로 새 규칙을 만들지 않는다.
- greenfield 에서 승인된 컨벤션은 **승인된 그대로** 적용한다. 구현하며 바꾸고 싶으면
  계획을 고치고 게이트를 다시 열어야 한다(해시가 달라져 훅이 막는다).

## 4. N6 는 어떻게 강제하는가

`ge-review-architecture` 는 다음을 **차단 사유(`severity: blocking`)** 로 판정한다.

- `conventions.architecture` 위반 (레이어 역방향 의존, 패키지 규칙 위반)
- `conventions.naming` 위반
- `conventions.error_handling` 과 다른 예외 처리 방식 도입
- `conventions.testing` 과 다른 테스트 위치·네이밍·스타일
- `conventions` 에 없는 **새 컨벤션을 발명**한 흔적 (기존 코드와 다른 새 패턴 도입)
- `requirements.out_of_scope` 침범, `requirements.constraints` 위반

단, `conventions.confidence == "low"` 인 항목은 blocking 이 아니라 `major` 로 낮추고
"컨벤션 근거 부족 — 사용자 확인 필요" 로 표기한다. 근거 없는 규칙으로 개발을 막지 않는다.
