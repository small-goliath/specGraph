# 아키텍처 규칙 — ArchUnit (Kotlin/Kotest · Java/JUnit 5)

**아키텍처 규칙도 TDD 사이클 밖의 별도 단계가 아니다.** ArchUnit 규칙은 그냥 테스트다 —
`gradle test` 로 같이 돈다. 한번 도입되면 레이어 역전·순환 의존·네이밍 위반은 컴파일이 아니라
**테스트 실패**로 드러난다. `conventions.architecture` 를 사람이 리뷰에서 읽고 판단하는 것과
달리, 이건 기계가 매 실행마다 재확인한다.

## 1. 언제 발동하는가

`runtime.build` 의 값들로 정해진다. 판정은 N0 의 `ge detect build` 가 한다.

```bash
.claude/graph/bin/ge show --field runtime.build.language        # {"primary":"kotlin"|"java",...}
.claude/graph/bin/ge show --field runtime.build.layers_detected # 찾은 레이어 디렉터리 이름들
.claude/graph/bin/ge show --field runtime.build.archunit        # {"available":true|false,...}
.claude/graph/bin/ge show --field runtime.build.arch_rules      # {"style":"archunit","required":true|false}
```

"레이어를 갖췄다" 는 추측이 아니라 실측이다 — 소스 루트 아래 디렉터리 이름을 훑어
`controller`/`service`/`repository`/`domain`/`application`/`infrastructure`/`usecase`/
`dao`/`adapter`/`facade` 중 **둘 이상**이 있으면 레이어가 있다고 본다. 파일 하나짜리
프로젝트에 폴더 하나 있다고 강제하지 않는다 — 강제할 경계가 최소 두 개는 있어야 한다.

| 상황 | 동작 |
|---|---|
| `language.primary` 가 kotlin/java 가 아니거나, 소스·테스트 루트가 전혀 없다(greenfield), 또는 `layers_detected` 가 2개 미만 | 이 문서 전체가 **적용되지 않는다.** 강제할 레이어가 없다. |
| 레이어(2개 이상)를 갖춘 기존 JVM 프로젝트 | `arch_rules.required` 는 **항상 `true`.** `archunit.available` 유무와 무관하다 — 조건부가 아니다. |
| `archunit.available == true` | 규칙이 이미 있다. 위반은 **일반 테스트 실행(N5)**이 그대로 잡는다. |
| `archunit.available == false` | 아직 없다. 프로덕션 코드를 건드리는 티켓이면 **N3 계획에 도입을 반드시 넣는다** — 생략 불가(§5). |

REST Docs 와 다른 점: REST Docs 는 "엔드포인트 하나에 스니펫 하나" 처럼 파일 단위 1:1 대응이지만,
ArchUnit 규칙은 **프로젝트 전체**에 적용된다. 그래서 강제 방식도 다르다 — 매 컨트롤러가 아니라
"이 프로젝트에 규칙 테스트가 존재하는가" 를 본다(§6).

greenfield(신규 프로젝트)는 아직 레이어가 없으므로 이 문서가 발동하지 않는다. N3 계획에서
아키텍처를 확정하고 N4 가 `CLAUDE.md` 에 기록한 뒤, **다음 티켓부터** `project_state` 가
`existing` 이 되면 발동한다.

## 2. 무엇을 규칙으로 만드는가

프로젝트가 이미 레이어를 나누고 있다면(예: `controller`/`service`/`repository`/`domain`
패키지) 그 경계를 규칙으로 **박제**한다. 최소 이 세 가지는 넣는다:

1. **레이어 의존 방향** — 예: `domain` 은 `controller`/`infrastructure` 를 참조하지 않는다,
   `service` 는 `controller` 를 참조하지 않는다.
2. **순환 의존 금지** — 패키지 간 사이클이 없어야 한다(`slices().assertThatSlices()...` 또는
   `noCycles()`류 API — 버전별 표기 확인).
3. **네이밍/위치 컨벤션** — `conventions.naming`이 이미 확립돼 있으면 그대로 규칙화한다.
   예: `@Service` 어노테이션이 붙은 클래스는 `..service..` 패키지에만 있어야 한다.

**규칙은 저장소의 실제 구조에서 뽑는다.** N2c(`ge-convention-scout`)가 낸
`conventions.architecture` 를 근거로 쓴다. 근거 없이 "이상적인" 레이어링을 지어내
강제하지 않는다 — 그러면 기존 코드가 전부 위반으로 잡혀 그래프가 멈춘다.

## 3. TDD 사이클 안에서의 위치

```
ArchUnit 이 이미 있는 프로젝트:
  평소처럼 구현한다. 이번 변경이 기존 규칙을 어기면 N5 의 일반 테스트 실행이 실패로 잡는다.
  새 레이어/패키지를 도입하는 티켓이면 그 레이어의 규칙도 Red 로 같이 추가한다.

ArchUnit 이 아직 없는 프로젝트 (계획에 도입이 있을 때):
  Red      §2 의 규칙 테스트를 쓴다 — 기존 코드가 이미 어기고 있으면 그 테스트는
           처음부터 실패해야 정상이다(그게 Red 다).
  Green    위반을 고치거나(가능하면), 그 시점엔 고치지 않기로 계획에 명시된 위반은
           `@ArchIgnore`/`freeze` 로 동결하고 사유를 남긴다(무단 삭제 금지).
  Refactor 규칙 설명을 다듬는다. 테스트는 초록으로 유지한다.
```

## 4. 작성 패턴

### 4-1. Kotlin/Kotest

```kotlin
@AnalyzeClasses(packages = ["com.example.app"], importOptions = [ImportOption.DoNotIncludeTests::class])
class ArchitectureTest : FunSpec() {
    init {
        test("domain 은 controller 를 참조하지 않는다") {
            val rule = noClasses()
                .that().resideInAPackage("..domain..")
                .should().dependOnClassesThat().resideInAPackage("..controller..")
            rule.check(importedClasses)
        }

        test("레이어 의존 방향을 지킨다") {
            layeredArchitecture()
                .consideringAllDependencies()
                .layer("Controller").definedBy("..controller..")
                .layer("Service").definedBy("..service..")
                .layer("Repository").definedBy("..repository..")
                .whereLayer("Controller").mayNotBeAccessedByAnyLayer()
                .whereLayer("Service").mayOnlyBeAccessedByLayers("Controller")
                .whereLayer("Repository").mayOnlyBeAccessedByLayers("Service")
                .check(importedClasses)
        }
    }

    companion object {
        val importedClasses: JavaClasses =
            ClassFileImporter().importPackages("com.example.app")
    }
}
```

### 4-2. Java/JUnit 5

```java
@AnalyzeClasses(packages = "com.example.app", importOptions = ImportOption.DoNotIncludeTests.class)
class ArchitectureTest {

    @ArchTest
    static final ArchRule domain_does_not_depend_on_controller =
        noClasses().that().resideInAPackage("..domain..")
            .should().dependOnClassesThat().resideInAPackage("..controller..");

    @ArchTest
    static final ArchRule layer_dependencies_are_respected =
        layeredArchitecture()
            .consideringAllDependencies()
            .layer("Controller").definedBy("..controller..")
            .layer("Service").definedBy("..service..")
            .layer("Repository").definedBy("..repository..")
            .whereLayer("Controller").mayNotBeAccessedByAnyLayer()
            .whereLayer("Service").mayOnlyBeAccessedByLayers("Controller")
            .whereLayer("Repository").mayOnlyBeAccessedByLayers("Service");
}
```

`@AnalyzeClasses` + `@ArchTest` 조합(ArchUnit 의 JUnit 5 확장)을 쓰면 클래스 임포트를
캐싱해서 빠르다 — 새로 규칙을 추가할 때는 기존 클래스에 필드/메서드로 얹는 것을 우선한다.

### 4-3. 규칙

- **기존 위반을 이번 티켓이 만들지 않았다면 강제로 고치지 않는다.** 새로 도입하는 규칙이
  기존 코드의 오래된 위반과 충돌하면, 그 위반들은 `freeze`(ArchUnit 의 FreezingArchRule)로
  동결하거나 명시적으로 예외 목록에 넣고 N3 계획/N6 리뷰에 노출한다. "규칙을 도입했더니
  기존 코드 절반이 깨져서 전부 고쳐야 했다" 는 이 그래프의 범위를 넘는다.
- **패키지 이름은 저장소 관례를 그대로 쓴다.** `..controller..`/`..web..` 등 실제 패키지
  이름에 맞춘다. 지어내지 않는다.
- **테스트 프레임워크 문법은 언어별 지배적 패턴**을 따른다(`references/spring-restdocs.md`
  §1 과 동일한 우선순위 규칙 — 저장소 패턴 → 탐지값 → greenfield 제안값).

## 5. ArchUnit 이 아직 없는 프로젝트 (N3)

레이어가 있는 기존 JVM 프로젝트에서 `archunit.available == false` 이고 이번 티켓이
프로덕션 코드를 건드리면, **도입은 선택이 아니라 이번 티켓의 일이다.** 임의로(승인 없이)
붙이지 말고 N3 계획에 넣어 GATE-PLAN 승인을 받는다. 계획에 포함할 것:

**Kotlin/Kotest:**
```kotlin
// build.gradle.kts
dependencies {
    testImplementation("com.tngtech.archunit:archunit-junit5:1.3.0")
}
```

**Java/JUnit 5:**
```groovy
// build.gradle
dependencies {
    testImplementation 'com.tngtech.archunit:archunit-junit5:1.3.0'
}
```

- 버전은 **version catalog(`gradle/libs.versions.toml`)가 있으면 거기에** 넣는다.
- 계획에는 §2 의 최소 규칙 세트(의존 방향/순환 금지/네이밍)를 **이 저장소의 실제 패키지
  이름으로** 채워 넣는다.
- "이번엔 귀찮으니 도입하지 않음" 같은 사유를 계획 단계에서 새로 만들어내지 않는다.
  건너뛸 수 있는 경우는 이번 티켓이 프로덕션 코드를 **전혀** 건드리지 않을 때뿐이다.

## 6. 검증 — 실측이고, 우회할 수 없다

N5(테스트 실행) 직후 실행한다.

```bash
.claude/graph/bin/ge archunit check      # 누락이 있으면 exit 2
.claude/graph/bin/ge archunit report     # 현재 판정 결과
```

판정 방식(REST Docs 와 달리 파일 단위 대응이 아니라 **"이번 티켓에 도입 흔적이 있는가"** 다):

1. `archunit.available == true` → 더 볼 것이 없다. 규칙 위반은 방금 돈 Gradle 테스트가
   이미 실패로 잡았을 것이다(ArchUnit 규칙은 평범한 테스트이므로).
2. `archunit.available == false` 인데 이번 시도에 프로덕션 코드 변경이 있고, 변경된 테스트
   중 ArchUnit 규칙을 정의하는 것(`com.tngtech.archunit`, `@AnalyzeClasses`, `ArchRule`,
   `layeredArchitecture(` 등)이 **하나도 없으면** `missing = true`.
3. 결과를 상태의 `arch_rules` 에 기록한다 — N6 리뷰가 그대로 읽는다.

**미해소분이 남아 있으면 커밋 훅이 차단한다.** `config.arch_rules.mode` 가 `block`(기본)일
때다. `warn` 이면 경고만, `off` 면 검사 자체를 하지 않는다. 검사를 한 번도 돌리지 않은 채
커밋하려 해도 차단된다.

도입 대상이 아니면(예: 이번 티켓이 프로덕션 코드를 건드리지 않는데도 잘못 판정됐다면):
```bash
.claude/graph/bin/ge archunit exempt --reason "<사유>"
# 예외는 상태에 남아 N6 리뷰에 그대로 노출된다. 남용하지 마라.
```

## 7. 상태 스키마

```jsonc
"arch_rules": {
  "style": "archunit",
  "required": true,
  "checked_at": "2026-09-17T…",
  "prod_changed": ["src/main/kotlin/…/OrderService.kt"],
  "rule_files": [],
  "missing": true,
  "exemptions": [{"reason":"…","at":"…"}],
  "note": "ArchUnit 미도입 상태에서 프로덕션 코드가 바뀌었는데 이번 시도에 규칙 테스트가 없다."
}
```

| 필드 | 쓰는 주체 |
|---|---|
| `arch_rules.*` (exemptions 제외) | `ge archunit check` |
| `arch_rules.exemptions` | `ge archunit exempt` (사유 필수, 리뷰에 노출) |
