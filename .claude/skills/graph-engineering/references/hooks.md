# 훅 — 결정론적 엣지와 가드

훅은 **모델의 선택에 의존하지 않는** 전이·가드만 담당한다. 모델이 SOP 를 잊어도 반드시 발화한다.

## 0. 비활성 시 무해함 (가장 중요한 성질)

모든 `ge-*.sh` 는 첫 동작으로 `.claude/graph/state/current` 존재를 확인하고,
없으면 **파이썬을 띄우지도 않고 즉시 `exit 0`** 한다. 출력도 없다.
→ 그래프를 켜지 않은 평소 작업은 이 훅들의 존재를 감지할 수 없다.

추가 안전장치:
- `python3` 이 없으면 통과, 구현 파일이 없으면 통과.
- 구현이 예기치 않게 죽으면(`exit != 0,2`) 통과.
- **차단은 exit 2 로만**, 명시적 위반이 판정됐을 때만 한다.

## 1. 등록 (`.claude/settings.json`)

| 이벤트 | matcher | 스크립트 | 역할 |
|---|---|---|---|
| PreToolUse | `Bash` | `ge-guard-bash.sh` | **셸 쓰기 판정(브랜치·GATE-PLAN·TDD)**, 커밋 가드, 푸시/PR 차단, 상태 위변조 차단, 승인 위조 차단 |
| PreToolUse | `Edit\|Write\|MultiEdit\|NotebookEdit` | `ge-guard-write.sh` | 브랜치 가드, GATE-PLAN 가드, TDD Red-First |
| PreToolUse | `mcp__atlassian__editJiraIssue` | `ge-guard-jira.sh` | **티켓 본문 쓰기 가드** — AMEND 에서는 GATE-TICKET 승인 필수 |
| PostToolUse | `Edit\|Write\|MultiEdit\|NotebookEdit\|Bash` | `ge-record-write.sh` | TDD 원장 자동 기록(셸 쓰기 포함) |
| UserPromptSubmit | — | `ge-approval.sh` | **승인/반려 기록** + 그래프 컨텍스트 주입 |
| SessionStart | — | `ge-session-context.sh` | 활성 그래프 컨텍스트 주입 |
| Stop / SubagentStop | — | `ge-checkpoint.sh` | 노드 종료 체크포인트 |

사용자 전역(`~/.claude/settings.json`)의 훅(graphify `hook-guard` 등)은 그대로 병합되어
함께 동작한다. 프로젝트 설정은 전역을 덮어쓰지 않는다.

## 1-2. 훅 밖의 결정론적 가드 — `ge` 런타임

훅만이 강제 지점은 아니다. 상태 런타임 자체가 노드 진입을 거부하는 것들이 있다.

| 지점 | 조건 | 실패 시 |
|---|---|---|
| `ge node N4` | GATE-PLAN 승인 + 계획 해시 일치 | exit 2 |
| `ge node N7` | GATE-REVIEW 승인 | exit 2 |
| `ge node N1\|N4\|N5` | **이번 진입에 대한 JIRA 상태 전이 기록**(`ge jira-transition`) | exit 2 |
| `ge record-test` | 같은 검사(N5 를 건너뛴 경로 백스톱) | exit 2 |

전이 기록은 **진입할 때 소비**된다. 그래서 `N5 → N4` 로 되돌아온 진입은 같은 기록을
재사용할 수 없고 다시 전이해야 한다 — "첫 진입에만" 이 아니라 **매 진입마다**다.
정책은 `config.jira.transition_policy`(`enforce`: block|warn|off) 가 정한다. 상세: `jira.md` §1.

## 1-1. 강제력은 도구 종류에 의존하지 않는다

**Edit/Write 로 막는 것은 Bash 로도 막는다.** 예전에는 `guard-bash` 가 소스 쓰기를 전혀 보지
않아서 `echo x > src/…/App.java` 한 줄로 GATE-PLAN 과 TDD Red-First 가 동시에 무력화됐다.
지금은 두 경로가 `check_write_targets()` 하나를 공유한다.

`guard-bash` 가 "이 명령이 쓰는 파일" 로 인식하는 것:

| 형태 | 예 |
|---|---|
| 리다이렉션 | `> f`, `>> f`, `>\| f`, `2> f`, heredoc 의 `cat > f <<EOF` |
| 파이프 저장 | `… \| tee f`, `tee -a f` |
| in-place 편집 | `sed -i`, `perl -i`, `ruby -i`, `ex` |
| 파일 조작 | `cp`, `mv`, `install`, `ln`, `touch`, `truncate`, `rm`, `dd of=` |

- 세그먼트를 순서대로 훑으며 **`cd` 로 바뀐 작업 디렉터리를 추적**한다.
  `cd .claude/graph && echo x > state/PPS-283.json` 도 상태 쓰기로 판정된다.
- **저장소 밖 경로는 판정하지 않는다.** `echo x > /tmp/scratch.txt` 는 그대로 통과한다.
- `.claude/graph/tmp/` 는 그래프 작업영역이라 게이트·TDD 판정 밖이다.
  (`./gradlew test 2>&1 | tee .claude/graph/tmp/test.log` 는 정상 통과한다.)
- `sed -i`·`cp` 류는 인자 중 **실재하거나 확장자를 가진 것만** 경로로 본다.
  `sed -i "" s/a/b/ f` 의 `s/a/b/` 를 파일로 오인하지 않는다.

**잔여 위험(의도적으로 판정하지 않음):** 인터프리터 인라인 코드로 파일을 쓰는 경우
(`python3 -c "open('src/X.java','w')…"`). 파싱으로 신뢰성 있게 잡을 수 없어 판정하지 않는다.
다만 대상이 상태/로그 경로면 `.claude/graph/state` 문자열 백스톱이 잡는다.

### git 실행 형태

git 서브커맨드는 정규식이 아니라 **토큰 파싱**으로 찾는다. `git` 자체 옵션(`-C`, `-c`,
`--git-dir`, `--work-tree` …)을 건너뛰고 첫 비옵션 토큰을 서브커맨드로 본다.
(예전 정규식 `^git\s+(-\S+\s+)*commit` 은 `git -C . commit` 에서 `.` 때문에 매칭이 깨져
커밋·푸시 가드가 통째로 우회됐다.)

한 세그먼트에 `git` 토큰과 `commit`/`push` 가 같이 있는데 실행 형태를 확신할 수 없으면
(`$(echo git) commit`, `xargs git commit` …) **fail-closed 로 차단**한다.
`grep -rn "git push" docs/` 처럼 명령 이름이 읽기 전용인 경우는 통과한다.

## 1-4. JIRA 티켓 본문 쓰기 (AMEND)

티켓 본문을 바꾸는 것은 **외부 시스템에 대한 되돌리기 어려운 쓰기**다. 승인 없이 열어두면
모델이 스스로 요구사항을 고쳐놓고 "티켓에 그렇게 적혀 있다" 고 말할 수 있다.

| `current_node` | `mcp__atlassian__editJiraIssue` |
|---|---|
| `AMEND` 외 / `N8` / `N9` 외 | **차단** — 다른 노드는 티켓을 쓸 이유가 없다 |
| `N8` / `N9` | 통과 (완료 조건 체크박스·요약 댓글) |
| `AMEND`, 제안 없음 | **차단** — `ge amend propose` 를 먼저 |
| `AMEND`, GATE-TICKET 미승인 | **차단** |
| `AMEND`, 승인 후 본문 변경 | **차단** (해시 불일치) |
| `AMEND`, 이미 반영된 제안 | **차단** |
| `AMEND` + 승인 + 해시 일치 | 통과 |

자세한 절차와 반려 사유 분류: `amend.md`.

## 1-3. API 문서화 (Spring Boot 프로젝트면 항상 — 조건부 아님)

`runtime.build.spring.boot == true` 면 발동한다. **`restdocs.available` 은 조건이 아니다** —
의존성이 아직 없어도 발동하며, 그 경우 실측(`ge apidocs check`)에서 변경된 컨트롤러가
전부 `missing` 으로 잡혀 도입 자체를 강제하는 효과를 낸다. Spring Boot 가 아닌 프로젝트에서는
아래 규칙이 존재하지 않는 것처럼 동작한다.

| 시점 | 훅/명령 | 동작 |
|---|---|---|
| N4 쓰기 전 | `guard-write`/`guard-bash` | 컨트롤러를 고치는데 이번 시도에 REST Docs 테스트가 없으면 **경고**(차단 아님 — 아직 스니펫이 없어 사실 판정 불가) |
| N5 테스트 후 | `ge apidocs check` | 변경 컨트롤러 ↔ 문서화 테스트 ↔ 생성된 스니펫을 **실측**, 상태 기록, 누락이면 exit 2 |
| 커밋 직전 | `guard-bash` | 미해소 누락이 있으면 차단. **검사를 한 번도 안 돌렸어도 차단**(건너뛰기로 통과 불가) |

컨트롤러 판정은 파일명이 아니라 **내용**으로 한다(`@RestController`, `@*Mapping`,
`RouterFunction` …). 자세한 내용은 `spring-restdocs.md`.

## 1-3b. 아키텍처 규칙(ArchUnit) (레이어 있는 기존 JVM 프로젝트면 항상 — 조건부 아님)

`runtime.build.arch_rules.required == true` 면 발동한다(레이어가 있는 기존 JVM 프로젝트).
**`archunit.available` 은 조건이 아니다** — 의존성이 아직 없어도 발동하며, 그 경우
실측(`ge archunit check`)에서 이번 시도의 프로덕션 변경이 그대로 `missing` 으로 잡혀
도입 자체를 강제하는 효과를 낸다. greenfield 나 JVM 이 아닌 프로젝트에서는 아래 규칙이
존재하지 않는 것처럼 동작한다.

| 시점 | 훅/명령 | 동작 |
|---|---|---|
| N4 쓰기 전 | `guard-write`/`guard-bash` | ArchUnit 미도입인데 프로덕션 코드를 고치는 시도에 규칙 테스트가 없으면 **경고**(차단 아님) |
| N5 테스트 후 | `ge archunit check` | ArchUnit 도입 여부 ↔ 이번 시도의 프로덕션 변경을 **실측**, 상태 기록, 누락이면 exit 2. 이미 도입돼 있으면 위반은 방금 돈 일반 테스트 실행이 잡는다 |
| 커밋 직전 | `guard-bash` | 미해소 누락이 있으면 차단. **검사를 한 번도 안 돌렸어도 차단** |

REST Docs 와 달리 파일 단위 1:1 대응이 아니라 **프로젝트 전체**·**이번 티켓 단위**로
판정한다. 자세한 내용은 `archunit.md`.

## 1-3c. N10 graphify 자동 커밋 (GATE 없이 통과)

`guard_commit` 은 일반 커밋에 GATE-REVIEW·GATE-COMMIT 승인과 컨벤셔널 커밋 형식(§ 아래
"커밋 가드")을 강제한다. N10(`ge-graphify-updater`)이 `graphify-out/` 을 갱신한 뒤 남기는
자체 커밋만 예외다 — 코드 변경이 아니라 색인 산출물이고, N7 에서 이미 리뷰·승인을 받은
코드 커밋과는 별개이기 때문이다.

예외는 세 조건을 **전부** 실측 확인했을 때만 적용된다(`config.commit.graphify`):

| 조건 | 확인 방법 |
|---|---|
| 지금 노드가 `commit.graphify.node`(기본 `N10`) | `state.current_node` |
| 커밋 메시지가 `commit.graphify.message`(기본 `feat(graphify): update`)와 **정확히** 일치 | 문자열 비교(정규화 후) |
| 스테이징된 파일이 전부 `commit.graphify.path_prefix`(기본 `graphify-out/`) 아래 | `git diff --cached --name-only` |

하나라도 어긋나면 **일반 커밋 규칙으로 떨어진다** — 예외가 아니라 컨벤션·GATE-REVIEW·
GATE-COMMIT 검사를 그대로 받고, `graphify-out/` 외 파일이 섞여 있으면 그 사실을 이유로
차단된다(리뷰 우회 통로가 되지 않도록). 메시지·경로 접두사·대상 노드는 설정으로 바꿀 수
있지만, 기본값을 바꾸지 않는 한 문구는 `feat(graphify): update` 그대로여야 한다.

## 1-2. 공유 상태의 동시 쓰기

fan-out(N2 3-way, N6 4-way)에서 여러 `ge` 프로세스가 동시에 상태를 갱신하면
나중 `save` 가 앞선 `save` 를 통째로 덮어써 필드가 유실됐다(lost update).
지금은 상태를 변형하는 모든 서브커맨드가 `.claude/graph/state/.<TICKET>.lock` 의
**배타 flock 안에서 실행**된다(`ge_state.state_lock`). 훅의 원장 기록·체크포인트·승인도
같은 락을 잡고 상태를 **다시 읽은 뒤** 쓴다.

## 2. 승인이 위조 불가능한 이유

```
사용자 키보드 → UserPromptSubmit 훅(prompt 원문) → ge approve (GE_HOOK_AUTH=1)
                                                  → gates.approvals[] 기록
```

1. `ge approve` 는 `GE_HOOK_AUTH=1` **이면서** `--source hook:UserPromptSubmit` 일 때만 동작한다.
2. Bash 가드가 명령줄의 `GE_HOOK_AUTH` 와 `ge approve` 를 **차단**한다.
3. Write 가드가 `.claude/graph/state/**` 직접 편집을 **차단**한다.
4. Bash 가드가 상태 경로에 대한 셸 변형(`>`, `tee`, `sed -i`, `jq >`, `rm`, `mv` …)을 **차단**한다.
5. `ge put`/`ge append` 는 `gates.approvals` 경로 쓰기를 **거부**한다.
6. 승인 검증 시 `source != "hook:UserPromptSubmit"` 인 레코드는 **무효** 처리한다.
7. 승인은 **산출물 해시에 묶인다.** 승인 후 계획·커밋 메시지를 고치면 해시가 달라져 다시 막힌다.

즉 "승인받은 척"은 경로 자체가 존재하지 않는다.

## 3. 차단 메시지와 대응

| 차단 | 원인 | 대응 |
|---|---|---|
| `상태/로그 파일은 직접 편집할 수 없다` | Edit/Write 로 상태 JSON 수정 시도 | `ge put/append/node/record-*` 사용 |
| `현재 브랜치가 …가 아니다` | 티켓 브랜치를 벗어나 작업 | `git switch <TICKET>` 또는 `/graph-abort` |
| `계획 승인(GATE-PLAN) 전에는 소스를 수정할 수 없다` | N3 미승인 상태에서 코드 작성 | 계획을 제시하고 사용자 `approve` 를 받는다 |
| `승인 대상이 바뀌었다` | 승인 후 산출물 수정 | `ge gate open` 으로 다시 제시하고 재승인 |
| `TDD Red-First 위반` | 테스트 없이 프로덕션 코드 작성 | 실패하는 테스트를 먼저 쓴다. 정말 예외면 `ge tdd exempt --path … --reason …` |
| `메시지가 컨벤션에 맞지 않다` | `type(TICKET): subject` 형식 위반 | 형식을 맞춘다 |
| `scope 가 … 인데 진행 중인 티켓은 …` | 다른 티켓 키를 scope 에 씀 | 현재 티켓 키로 고친다 |
| `코드리뷰 승인이 없다` | GATE-REVIEW 미승인 커밋 | N6 를 끝내고 승인을 받는다 |
| `커밋 메시지를 명령에서 찾을 수 없다` | 에디터 모드 커밋 | `git commit -m "…"` 또는 `-F <파일>` |
| `이 그래프는 푸시를 하지 않는다` | `git push` / `gh pr create` | 사용자가 직접 한다 |
| `git 명령의 실행 형태를 확신 있게 파싱할 수 없어 차단한다` | `$(…) commit`, `xargs git commit` 등 | `git commit -m "…"` 단순 형태로 실행한다 |
| `테스트를 먼저 쓰지 않고 프로덕션 코드를 쓴 기록이 … 남아 있다` | 원장에 테스트 없는 prod 쓰기가 있음 | 테스트를 쓰거나 `ge tdd exempt` 로 사유를 남긴다 |
| `변경된 컨트롤러 …개에 API 문서(REST Docs 스니펫)가 없다` | Spring Boot 인데 문서화 누락 | 테스트에 `document(...)` 추가 또는 `ge apidocs exempt` |
| `API 문서화 검사를 아직 돌리지 않았다` | `ge apidocs check` 미실행 | N5 에서 테스트 실행 뒤 `ge apidocs check` |
| `아키텍처 규칙(ArchUnit)이 아직 도입되지 않았는데 프로덕션 코드가 바뀌었다` | 레이어 있는 기존 JVM 프로젝트인데 ArchUnit 미도입 | 규칙 테스트 도입 또는 `ge archunit exempt` |
| `아키텍처 규칙 검사를 아직 돌리지 않았다` | `ge archunit check` 미실행 | N5 에서 테스트 실행 뒤 `ge archunit check` |
| `상태 잠금을 30초 안에 얻지 못했다` | 다른 `ge` 프로세스가 멈춤 | 남은 프로세스를 확인하고 다시 시도한다 |
| `JIRA 티켓 본문은 … 노드에서만 수정할 수 있다` | AMEND/N8/N9 밖에서 티켓 쓰기 시도 | 요구사항이 틀렸다면 게이트 반려 후 AMEND 로 |
| `티켓 수정 차단: …` | GATE-TICKET 미승인 또는 해시 불일치 | `ge amend propose` → `ge gate open --gate GATE-TICKET` → 사용자 `approve` |
| `ge approve 는 훅 전용` | 모델이 스스로 승인 시도 | 사용자에게 승인을 요청한다 |

**같은 차단을 두 번 이상 만나면 우회를 시도하지 말고 사용자에게 보고한다.**

## 4. 훅 테스트

```bash
.claude/graph/tests/run-tests.sh
```

비활성 통과 / 활성 차단을 합성 stdin JSON 으로 실측한다. 훅을 고치면 반드시 다시 돌린다.

## 5. 훅을 끄고 싶을 때

- 정상 종료: `/graph-abort` → `state/current` 삭제 → 즉시 전부 무해해진다.
- 강제: `rm .claude/graph/state/current` 만으로 충분하다. 상태 파일은 감사용으로 남는다.
- `.claude/settings.json` 에서 훅 등록을 지울 필요는 없다.
