# Spring Boot API 문서화 — Spring REST Docs (Kotlin/Kotest · Java/JUnit 5)

**문서화는 TDD 사이클 밖의 별도 단계가 아니다.** Red 단계에서 쓰는 API 테스트가 곧 문서다.
테스트가 통과하지 않으면 스니펫이 생기지 않으므로, **문서는 구현과 어긋날 수 없다.**
(Swagger 어노테이션과 결정적으로 다른 점이다 — 그쪽은 코드와 따로 놀 수 있다.)

**문서화가 맞더라도 설계가 RESTful 하지 않으면 이 문서는 절반짜리다.** 엔드포인트 자체를
RESTful 하게 만드는 규칙(자원 URI, HTTP 메서드, 상태코드)은 `references/restful-api.md` 에
있다 — N4 는 그 규칙대로 구현하고, 여기 있는 `document(...)` 패턴으로 그 계약을 스니펫에
그대로 남긴다. 결과물(스니펫 묶음)이 곧 RESTful 명세여야 한다.

## 1. 언제 발동하는가

**`spring.boot` 하나로 정해진다. `restdocs.available` 은 더 이상 조건이 아니다.**
판정은 N0 의 `ge detect build` 가 한다.

```bash
.claude/graph/bin/ge show --field runtime.build.spring          # {"boot":true,"web":"mvc"|"webflux",...}
.claude/graph/bin/ge show --field runtime.build.restdocs        # {"available":true,"flavor":"mockmvc",...}
.claude/graph/bin/ge show --field runtime.build.api_docs        # {"style":"spring-restdocs","required":true}
```

| 상황 | 동작 |
|---|---|
| `spring.boot == false` | 이 문서 전체가 **적용되지 않는다.** 평소 TDD 만 한다. |
| `spring.boot == true` (restdocs 유무 무관) | **문서화 필수.** `api_docs.required` 는 항상 `true` 다. |
| `spring.boot && restdocs.available` | 바로 아래 규칙대로 작성한다. |
| `spring.boot && !restdocs.available` | 아직 REST Docs 가 없다. API 를 바꾸는 티켓이면 **N3 계획에 도입을 반드시 넣는다** — 생략은 선택지가 아니다(§5). |

`api_docs.required` 가 `restdocs.available` 에 좌우되던 예전 동작은 없앴다. 그때는 "아직
의존성이 없다" 는 사실 하나로 문서화 의무 자체가 사라졌다 — 즉 REST Docs 를 붙이지 않은
채로 놔두면 영원히 강제되지 않는 구멍이었다. 지금은 `spring.boot` 만 보고, 의존성이 없으면
`cmd_apidocs` 실측에서 변경 컨트롤러가 전부 `missing` 으로 잡혀 **도입 자체를 강제**한다.

### 테스트 프레임워크는 **언어와 탐지값**이 정한다

REST Docs 자체는 언어와 무관하다. Kotlin이든 Java든 **같은 스니펫**이 나온다.
달라지는 건 테스트를 쓰는 문법뿐이다.

```bash
.claude/graph/bin/ge show --field runtime.build.language                   # {"primary":"kotlin"|"java",...}
.claude/graph/bin/ge show --field runtime.build.test_framework             # 실제 탐지값 (사실)
.claude/graph/bin/ge show --field runtime.build.test_framework_suggested   # greenfield 제안값
```

**우선순위 (위가 무조건 이긴다):**

1. **저장소의 지배적 패턴** — 기존 API 테스트가 있으면 그 형태를 그대로 따른다.
   `conventions.testing.api_test_style`·`restdocs` 를 보라. 베이스 클래스가 있으면 재사용한다.
2. **`test_framework`(탐지값)** — 실제 의존성에서 나온 사실이다.
3. **`test_framework_suggested`** — **greenfield 에서만** 쓰는 제안값이며,
   확정은 GATE-PLAN 승인으로 한다. 언어별 기본값은 `config.json` 의
   `tdd.default_test_framework_by_language` (`kotlin → kotest`, `java → junit5`).

| 언어 | 기본 | 이유 |
|---|---|---|
| Kotlin | Kotest (+ `kotest-extensions-spring`, `springmockk`) | Kotlin DSL 기반 Spec 스타일 |
| Java | JUnit 5 (+ `spring-boot-starter-test`, Mockito) | **Kotest 는 Kotlin DSL 이라 Java 프로젝트에 강요하지 않는다.** Kotlin 툴체인 도입은 티켓 하나가 결정할 일이 아니다. |

`language.mixed == true`(두 언어 혼재)면 **건드리는 파일이 속한 쪽의 패턴**을 따른다.
Java 모듈에 Kotest 를, Kotlin 모듈에 JUnit 5 를 새로 들이지 마라.
**컨벤션을 발명하지 않는다.**

## 2. 무엇이 문서화 대상인가

컨트롤러 판정은 **파일명이 아니라 내용**으로 한다(프로젝트마다 이름 규칙이 다르다).
아래 표지가 있는 프로덕션 소스가 대상이다:

```
@RestController  @Controller  @RequestMapping
@GetMapping  @PostMapping  @PutMapping  @DeleteMapping  @PatchMapping
RouterFunction  coRouter(  RouterFunctions.route          ← WebFlux 함수형 라우팅
```

제외: `config.json` 의 `api_docs.exempt_globs` (기본 `**/actuator/**`, `**/internal/**`,
`**/*HealthController.*`). 그 밖에 문서화 대상이 아닌 것이 있으면 사유를 남긴다:

```bash
.claude/graph/bin/ge apidocs exempt --path <경로> --reason "<사유>"
# 예외는 상태에 남아 N6 리뷰에 그대로 노출된다. 남용하지 마라.
```

## 3. TDD 사이클 안에서의 위치

```
Red      실패하는 API 테스트를 쓴다 — 이때 document("<식별자>", …) 를 **함께** 넣는다.
         (문서화를 나중에 붙이지 않는다. 나중에 붙이면 반드시 빠진다.)
Green    통과시키는 최소한의 컨트롤러/서비스 코드를 쓴다.
Refactor 필드 설명(fieldWithPath)을 다듬는다. 테스트는 초록으로 유지한다.
```

N4 에서 컨트롤러를 수정하는데 그 시도에 REST Docs 테스트가 없으면 훅이 **경고**한다
(차단은 아니다 — 이 시점엔 스니펫이 없어 사실 판정이 불가능하다).
**실측과 차단은 N5 뒤에 일어난다** (§6).

## 4. 작성 패턴

같은 엔드포인트를 Kotlin/Kotest 와 Java/JUnit 5 로 각각 쓴 예다.
**`document(...)` 부분은 완전히 동일하다** — 스니펫도 동일하게 나온다.

### 4-1. MockMvc (`restdocs.flavor == "mockmvc"`, Spring MVC)

```kotlin
@WebMvcTest(UserController::class)
@AutoConfigureRestDocs                       // 스니펫 출력 경로를 자동 설정
class UserControllerTest : FunSpec() {       // Kotest — 프로젝트의 지배적 Spec 스타일을 따른다

    override fun extensions() = listOf(SpringExtension)   // kotest-extensions-spring

    @Autowired lateinit var mockMvc: MockMvc
    @MockkBean lateinit var userService: UserService      // springmockk

    init {
        test("사용자를 조회하면 200 과 사용자 정보를 반환한다") {
            every { userService.find(1L) } returns User(1L, "김단이")

            mockMvc.perform(
                get("/api/users/{id}", 1L).accept(MediaType.APPLICATION_JSON)
            )
                .andExpect(status().isOk)
                .andExpect(jsonPath("$.name").value("김단이"))
                .andDo(
                    document(
                        "user-get",                        // ← 스니펫 식별자
                        pathParameters(
                            parameterWithName("id").description("사용자 ID")
                        ),
                        responseFields(
                            fieldWithPath("id").type(NUMBER).description("사용자 ID"),
                            fieldWithPath("name").type(STRING).description("이름")
                        )
                    )
                )
        }
    }
}
```

### 4-1-b. 같은 것을 Java + JUnit 5 로 (`language.primary == "java"`)

```java
@WebMvcTest(UserController.class)
@AutoConfigureRestDocs
class UserControllerTest {

    @Autowired MockMvc mockMvc;
    @MockBean UserService userService;          // Mockito — Java 에서는 springmockk 를 쓰지 않는다

    @Test
    void 사용자를_조회하면_200_과_사용자_정보를_반환한다() throws Exception {
        given(userService.find(1L)).willReturn(new User(1L, "김단이"));

        mockMvc.perform(get("/api/users/{id}", 1L).accept(MediaType.APPLICATION_JSON))
            .andExpect(status().isOk())
            .andExpect(jsonPath("$.name").value("김단이"))
            .andDo(document("user-get",                    // ← Kotlin 예제와 완전히 동일
                pathParameters(
                    parameterWithName("id").description("사용자 ID")
                ),
                responseFields(
                    fieldWithPath("id").type(NUMBER).description("사용자 ID"),
                    fieldWithPath("name").type(STRING).description("이름")
                )));
    }
}
```

테스트 메서드 네이밍(한글 스네이크 / `should_...` / `given_when_then`)은
**저장소의 지배적 패턴**을 따른다. 없으면 N3 계획에서 확정한다.

### 4-2. WebTestClient (`flavor == "webtestclient"`, WebFlux)

```kotlin
webTestClient.get().uri("/api/users/{id}", 1L)
    .exchange()
    .expectStatus().isOk
    .expectBody()
    .consumeWith(
        document(
            "user-get",
            pathParameters(parameterWithName("id").description("사용자 ID")),
            responseFields(
                fieldWithPath("id").type(NUMBER).description("사용자 ID"),
                fieldWithPath("name").type(STRING).description("이름")
            )
        )
    )
```

### 4-3. 규칙

- **RESTful 설계 규칙을 먼저 지킨다.** URI·HTTP 메서드·상태코드는 `references/restful-api.md`
  를 따른다 — 여기서부터 벗어나 있으면 스니펫을 아무리 잘 써도 문서가 잘못된 계약을 박제한다.
- **식별자는 `<자원>-<행위>` 형태로 안정적으로.** `"user-get"`, `"user-create"` 처럼
  엔드포인트 단위 kebab-case. 이미 있는 식별자 규칙이 보이면 그것을 따른다.
- **모든 요청·응답 필드를 기술한다.** REST Docs 는 누락된 필드가 있으면 테스트를 **실패**시킨다.
  이게 문서가 낡지 않는 이유이므로, 귀찮다고 `relaxedResponseFields` 로 도망가지 마라.
  (정말 부분 문서화가 필요한 곳에서만 쓰고, 왜인지 테스트에 주석으로 남긴다.)
- **에러 응답도 문서화한다.** 완료 조건에 4xx/5xx 동작이 있으면 그 케이스도 스니펫을 만든다.
- **`@WebMvcTest` 슬라이스를 우선**한다. 컨트롤러 계약만 검증하면 되므로 `@SpringBootTest` 는
  느리고 과하다. 슬라이스로 안 되는 통합 시나리오만 `@SpringBootTest` 를 쓴다.
- **Kotlin 프로젝트**: Kotest Spec 스타일(`FunSpec`/`DescribeSpec`/`BehaviorSpec`)은
  기존 테스트의 지배적 패턴을 따른다. 새로 정하는 경우에만 `DescribeSpec`(given-when-then
  가독성)을 기본으로 제안한다. 목킹은 `springmockk`(`@MockkBean`).
- **Java 프로젝트**: JUnit 5 + `@MockBean`(Mockito). Kotest 를 새로 도입하지 마라 —
  Kotlin 툴체인 추가는 이 그래프의 범위가 아니다. 정말 필요하면 N3 계획에 올려 승인받는다.

## 5. REST Docs 가 아직 없는 Spring Boot 프로젝트 (N3)

`spring.boot == true && restdocs.available == false` 이고 이번 티켓이 API 를 건드리면,
**도입은 선택이 아니라 이번 티켓의 일이다.** 임의로(승인 없이) 붙이지 말고 N3 계획에 넣어
GATE-PLAN 승인을 받는다 — 여기서 "임의로" 는 승인 절차를 건너뛰지 말라는 뜻이지,
**도입 여부를 고를 수 있다는 뜻이 아니다.** 계획에 포함할 것:

**Kotlin 프로젝트** (`language.primary == "kotlin"`):

```kotlin
// build.gradle.kts
plugins { id("org.asciidoctor.jvm.convert") version "3.3.2" }

val snippetsDir by extra { file("build/generated-snippets") }

dependencies {
    testImplementation("org.springframework.restdocs:spring-restdocs-mockmvc")
    testImplementation("io.kotest:kotest-runner-junit5:<버전>")
    testImplementation("io.kotest.extensions:kotest-extensions-spring:<버전>")
    testImplementation("com.ninja-squad:springmockk:<버전>")
}

tasks.test { useJUnitPlatform(); outputs.dir(snippetsDir) }
tasks.asciidoctor { inputs.dir(snippetsDir); dependsOn(tasks.test) }
```

**Java 프로젝트** (`language.primary == "java"`) — Kotest·Kotlin 툴체인을 **넣지 않는다**:

```groovy
// build.gradle
plugins { id 'org.asciidoctor.jvm.convert' version '3.3.2' }

ext { snippetsDir = file('build/generated-snippets') }

dependencies {
    testImplementation 'org.springframework.boot:spring-boot-starter-test'   // JUnit 5 + Mockito
    testImplementation 'org.springframework.restdocs:spring-restdocs-mockmvc'
}

test { useJUnitPlatform(); outputs.dir snippetsDir }
asciidoctor { inputs.dir snippetsDir; dependsOn test }
```

- 버전은 **version catalog(`gradle/libs.versions.toml`)가 있으면 거기에** 넣는다.
- **"이번엔 귀찮으니 문서화하지 않음" 같은 사유를 계획 단계에서 새로 만들어내지 않는다.**
  건너뛸 수 있는 경우는 딱 하나다 — 티켓의 `requirements.out_of_scope` 에 **이미** 그 문서화
  대상 API 자체가 범위 밖으로 명시돼 있을 때(즉 REST Docs 문제가 아니라 그 엔드포인트를
  아예 이번 티켓에서 다루지 않는 경우). 그 밖의 모든 경우, `spring.boot == true` 이고
  컨트롤러를 건드리면 REST Docs 도입과 문서화는 **강제**다.

## 6. 검증 — 실측이고, 우회할 수 없다

N5(테스트 실행) 직후 실행한다. 스니펫이 실제로 생성됐는지를 파일시스템에서 확인한다.

```bash
.claude/graph/bin/ge apidocs check      # 누락이 있으면 exit 2
.claude/graph/bin/ge apidocs report     # 현재 판정 결과
```

판정 방식:
1. 변경 파일 중 **내용상 컨트롤러**인 프로덕션 소스를 찾는다.
2. 변경된 테스트 중 그 컨트롤러 클래스명을 참조하면서 REST Docs 표지
   (`document(`, `MockMvcRestDocumentation`, `WebTestClientRestDocumentation` …)를
   가진 것이 있는지 대조한다.
3. `restdocs.snippets_dirs` 의 스니펫(`.adoc`/`.json`) 개수를 센다.
4. 결과를 상태의 `api_docs` 에 기록한다 — N6 리뷰가 그대로 읽는다.

**미해소분이 남아 있으면 커밋 훅이 차단한다.** `api_docs.mode` 가 `block`(기본)일 때다.
`warn` 이면 경고만, `off` 면 검사 자체를 하지 않는다.

검사를 한 번도 돌리지 않은 채 커밋하려 해도 차단된다 — "검사를 건너뛰어서 통과" 는 없다.

## 7. 상태 스키마

```jsonc
"api_docs": {
  "style": "spring-restdocs",
  "required": true,
  "checked_at": "2026-09-04T…",
  "controllers_changed": ["src/main/kotlin/…/UserController.kt"],
  "documented":          ["src/main/kotlin/…/UserController.kt"],
  "missing":             [],
  "snippets_found": 12,
  "exemptions": [{"path":"…","reason":"…","at":"…"}]
}
```

| 필드 | 쓰는 주체 |
|---|---|
| `api_docs.*` (exemptions 제외) | `ge apidocs check` |
| `api_docs.exemptions` | `ge apidocs exempt` (사유 필수, 리뷰에 노출) |
