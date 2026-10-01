# 빌드 환경 — 런타임 탐지와 명령 구성

빌드 도구는 **Gradle** 이다. 그 외 사실(wrapper, 멀티모듈, DSL, version catalog,
테스트 태스크, 테스트 프레임워크)은 **실행 시점에 탐지**해서 상태에 싣고, 명령은 그 값으로 만든다.
**태스크 이름을 하드코딩하지 않는다.**

> Spring Boot 프로젝트(`runtime.build.spring.boot == true`)는 `api_docs.required` 가
> **항상** `true` 다 — REST Docs 의존성이 아직 없어도 마찬가지다(조건부 아님, 도입 자체가
> 강제된다). API 문서화는 TDD 사이클의 일부로 강제되며, 규칙과 작성 패턴은
> `spring-restdocs.md`, RESTful 설계 규칙은 `restful-api.md` 를 보라.
>
> 레이어를 갖춘 기존 JVM 프로젝트는 `arch_rules.required` 도 **항상** `true` 다 — ArchUnit
> 의존성 유무와 무관하다(조건부 아님). 작성 패턴은 `archunit.md` 를 보라.

## 1. 탐지

```bash
.claude/graph/bin/ge detect build     # runtime.build 를 채운다
```

탐지 항목과 근거:

| 항목 | 근거 |
|---|---|
| `wrapper` / `invoke` | 루트의 `gradlew` 존재 → `./gradlew`. 없으면 PATH 의 `gradle`. |
| `dsl` | `settings.gradle.kts`/`build.gradle.kts` → `kotlin`, 아니면 `groovy` |
| `multi_module`, `modules` | `settings.gradle[.kts]` 의 `include(...)` 파싱 |
| `version_catalog` | `gradle/libs.versions.toml` |
| `test_framework` | 빌드 스크립트 힌트. **우선순위 = kotest > spock > testng > junit5 > junit4.** Kotest·Spock 은 JUnit Platform 위에서 돌아 `useJUnitPlatform()` 이 함께 있으므로, junit5 를 먼저 매칭하면 Kotest 프로젝트가 영원히 junit5 로 잡힌다. |
| `test_frameworks` | 탐지된 것 **전부** (예: `["kotest","junit5"]`) |
| `language` | **소스 파일을 세서** 판정 → `{primary:"kotlin"\|"java", counts, mixed, origin}`. `dsl`(빌드 스크립트가 `.kts` 인지)과 **다르다** — Groovy DSL + Kotlin 소스 조합이 흔하다. 소스가 없으면(greenfield) 빌드 플러그인 → 소스 디렉터리 이름 순으로 추정 |
| `test_framework_suggested` | `test_framework` 이 `null` 일 때만 채워지는 **제안값**(`kotlin→kotest`, `java→junit5`). 사실이 아니라 제안이며 greenfield 에서 GATE-PLAN 승인으로 확정한다 |
| `test_libs` | `mockk`, `springmockk`, `mockito`, `kotest-spring`, `testcontainers` |
| `spring` | `org.springframework.boot` 플러그인/스타터 → `{boot, version, web:"mvc"\|"webflux"}` |
| `restdocs` | `spring-restdocs-*` 의존성 → `{available, flavor:"mockmvc"\|"webtestclient"\|"restassured", snippets_dirs, asciidoctor, openapi}` |
| `api_docs` | `spring.boot` → `{style:"spring-restdocs", required:true}` (`restdocs.available` 은 조건이 아니다 — 없어도 `required:true`) |
| `archunit` | `archunit` 의존성 → `{available, evidence}` |
| `layers_detected` | 소스 루트 아래 `controller`/`service`/`repository`/`domain`/`application`/`infrastructure`/`usecase`/`dao`/`adapter`/`facade` 디렉터리 중 실제로 존재하는 것들 |
| `arch_rules` | JVM(`language.primary` kotlin/java) + 기존 프로젝트(소스 있음) + `layers_detected` **2개 이상** → `{style:"archunit", required:true}` (`archunit.available` 은 조건이 아니다) |
| `extra_test_tasks` | `tasks.register<Test>("integrationTest")` 등 커스텀 Test 태스크 |
| `source_roots`, `test_roots` | 루트와 각 모듈의 `src/main`, `src/test`, `src/integrationTest`, `src/testFixtures` |
| `test_task`, `check_task` | 기본값 `test` / `check` (config 로 변경 가능) |

## 2. `status` 별 분기 — 탐지 실패하면 멈추고 묻는다

| status | 의미 | 행동 |
|---|---|---|
| `detected` | 정상 | 진행 |
| `missing` | Gradle 파일도 wrapper 도 없음 → **신규 프로젝트** | 중단하지 않는다. `project_state = greenfield`. **Gradle 세팅을 N3 계획에 포함**해 승인받고 N4 에서 만든다. |
| `blocked` | wrapper 도 없고 PATH 에 `gradle` 도 없음 / `gradlew` 실행 권한 없음 | **그래프 정지.** `blockers` 를 그대로 보여주고 사용자에게 묻는다. 임의로 우회하지 않는다. |
| `ambiguous` | Gradle 은 있는데 소스 레이아웃을 못 찾음 | **그래프 정지.** 소스/테스트 루트를 사용자에게 확인한다. |

`extra_test_tasks` 가 있으면 "기본 `test` 만 돌릴지, `integrationTest` 도 돌릴지" 를
**N3 계획에 넣어 사용자 승인을 받는다.** 임의로 정하지 않는다.

## 3. 명령 구성 (N5)

```bash
GE=.claude/graph/bin/ge
INVOKE=$($GE show --field runtime.build.invoke)      # ./gradlew | gradle
TASK=$($GE show --field runtime.build.test_task)     # test | …
LOG=.claude/graph/tmp/n5.log

$INVOKE $TASK 2>&1 | tee "$LOG"
RC=${PIPESTATUS[0]}
$GE record-test --command "$INVOKE $TASK" --exit-code "$RC" --log "$LOG"
```

- 멀티모듈에서 특정 모듈만 돌릴 때도 태스크 경로는 탐지된 `modules` 로 만든다: `:core:test`.
- **실패 출력을 요약하지 않는다.** `record-test` 가 원문 전체를
  `.claude/graph/logs/<TICKET>/n5-attempt-<n>.log` 에 저장하고, 상태에는 그 경로와
  원문 발췌(`failure_excerpt_verbatim`)를 넣는다. N4 는 발췌가 아니라 **로그 원문**을 읽고 고친다.
- `--offline`, `--no-daemon`, `--tests <필터>` 같은 옵션은 필요할 때만 붙이고, 붙였으면
  `record-test --command` 에 실제 실행한 명령을 그대로 남긴다.

## 4. greenfield 에서 Gradle 세팅하기 (N4)

계획에서 승인된 대로만 만든다. 최소 구성:

- `gradle wrapper` 생성 (버전은 계획에서 확정)
- `settings.gradle[.kts]` — 루트 프로젝트명, 멀티모듈이면 `include`
- `build.gradle[.kts]` — 플러그인, 의존성, `test { useJUnitPlatform() }` 등
- 소스 레이아웃 `src/main/...`, `src/test/...`

세팅 후 반드시 `ge detect build` 를 다시 돌려 `runtime.build` 를 갱신한다.
이 파일들은 TDD 예외(`tdd.exempt_globs`)라 Red-First 에 걸리지 않는다.
