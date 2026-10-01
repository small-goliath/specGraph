---
description: Graph Engineering 파이프라인을 켜고 JIRA 티켓 하나를 N0→N10 으로 끝까지 굴린다
argument-hint: <TICKET> (예 PPS-283)
---

# /graph-run $1

너는 이제 **Graph Engineering 그래프의 오케스트레이터**다.
`graph-engineering` skill 을 읽고, 그 SOP 에 정의된 노드/엣지/상태 규칙을 그대로 따른다.

**티켓: `$1`** — 인자가 비었으면 사용자에게 티켓 키를 묻고 멈춘다.

## 0단계 — 선행조건 (N0). 다른 어떤 작업보다 먼저.

```bash
git status --porcelain          # 출력이 있으면 → 중단하고 사용자에게 알린다
git rev-parse --verify main     # 없으면 master, develop 순으로 확인
```

1. **워킹 트리가 더러우면 여기서 중단한다.** 커밋/스태시는 사용자가 결정할 일이다.
2. base 브랜치를 확정한다(`config.json` 의 `git.base_branch_candidates` 순서).
   **후보가 하나도 없으면 중단하고 사용자에게 묻는다.** 임의로 다른 브랜치를 base 로 쓰지 마라.
3. base 로 이동해 최신화한다. 리모트가 없으면 로컬 base 를 그대로 쓰고 그 사실을 알린다.
   ```bash
   git switch <base> && (git remote | grep -q . && git pull --ff-only || true)
   ```
4. **티켓 키를 브랜치명으로** 브랜치를 만들고 체크아웃한다.
   ```bash
   git switch -c $1        # 이미 있으면 git switch $1 하고 사용자에게 알린다
   ```
5. 상태를 만들고 런타임을 탐지한다.
   ```bash
   .claude/graph/bin/ge init --ticket $1 --base-branch <base>
   .claude/graph/bin/ge detect all
   .claude/graph/bin/ge status
   ```
6. `runtime.build.status` 분기:
   - `detected` → 진행
   - `missing` → 신규 프로젝트. 진행하되 **Gradle 세팅을 N3 계획에 넣는다.**
   - `blocked` / `ambiguous` → **중단.** `blockers` 를 그대로 보여주고 사용자에게 묻는다.
7. `runtime.graphify.available` 을 확인하고, 이번 실행의 탐색 방식을 사용자에게 한 줄로 알린다.
   (`true` → 모든 탐색 노드가 graphify skill 최우선 / `false` → 일반 탐색)

**여기까지 성공한 뒤에만 N1 로 진입한다.**

## 1단계 이후 — 노드 실행

각 노드는 서브에이전트로 실행한다. 노드에 들어가기 전에
`.claude/skills/graph-engineering/references/nodes.md` 의 해당 절을 읽고,
끝나면 `ge node <다음노드> --status running` 으로 전이를 명시한다.

**N1 · N4 · N5 는 JIRA 상태 전이가 선행 조건이다.** 담당 에이전트가 노드 안에서
`getTransitionsForJiraIssue` → `transitionJiraIssue` → `ge jira-transition --node <노드> …`
를 먼저 수행한 뒤에 `ge node` 로 진입한다. 기록이 없으면 런타임이 exit 2 로 거부한다.
**첫 진입만이 아니라 매 진입마다** 필요하다 — `N5 → N4`, `N6 → N4` 로 되돌아갈 때도
그 노드의 목표 상태(`개발 중`/`검증 중`)로 다시 전이해야 한다. 상세: `references/jira.md` §1.

| 노드 | 실행 방식 |
|---|---|
| N1 | `ge-jira-analyst` 단일 호출 |
| N2 | **한 메시지에 3개 동시 호출**: `ge-jira-analyst`(파싱) + `ge-impact-scout` + `ge-convention-scout` → join |
| N3 | `ge-planner` → **GATE-PLAN 승인 대기** |
| N4 | `ge-tdd-developer` 단일 호출 |
| N5 | `ge-test-runner` 단일 호출 |
| N6 | diff 를 `.claude/graph/tmp/$1.diff` 로 뽑은 뒤 **한 메시지에 4개 동시 호출**: `ge-review-correctness` + `ge-review-architecture` + `ge-review-tests` + `ge-review-security` → `ge-review-synthesizer` 로 join → **GATE-REVIEW 승인 대기** |
| N7 | `ge-commit-writer` → **GATE-COMMIT 승인 대기** → 커밋 실행 |
| N8, N9 | `ge-jira-reporter` |
| N10 | `ge-graphify-updater` 단일 호출 — **비차단**. `runtime.graphify.available == false` 면 건너뛴다. 실패해도 그래프를 멈추지 말고 `graphify_update` 기록만 확인한 뒤 DONE 으로 간다. |

N6 전 준비:
```bash
git diff <base>...HEAD > .claude/graph/tmp/$1.diff   # 커밋 전이면 git diff 도 합쳐서
```

## 엣지 판정 — 네가 직접 한다

- **N2 join**: `requirements.format_valid == false` → **그래프 정지.** 위반 목록과
  `requirements.draft_proposed`(완료 조건 초안)를 사용자에게 제시하고 지시를 기다린다.
  요구사항을 지어내고 진행하지 마라.
- **N5 실패**: `ge retry --edge n5_to_n4` → exit 0 이면 N4 재실행, **exit 3 이면 즉시 정지하고 에스컬레이션**.
- **N6 blocking**: `ge retry --edge n6_to_n4` → 위와 동일.
- 정지·에스컬레이션 시 보고할 것: 남은 blocking/실패 목록, 마지막 테스트 원문 로그 경로,
  변경 파일, 제안하는 다음 수.

## 승인 게이트 3곳 — 우회 불가능

`GATE-PLAN`(N3→N4), `GATE-REVIEW`(N6→N7), `GATE-COMMIT`(커밋 직전).

- 게이트를 열면(`ge gate open`) 산출물 해시와 6자리 코드가 나온다.
- **사용자에게 산출물 전문을 제시하고 거기서 턴을 끝낸다.** 승인 없이 다음 노드로 가지 마라.
- 사용자가 `approve`/`승인` 을 입력하면 UserPromptSubmit 훅이 승인을 기록하고
  다음에 무엇을 하라고 컨텍스트로 알려준다. 그 신호를 본 뒤에 진행한다.
- **반려되면 사유를 분류한다** (`references/amend.md`):
  - **(A) 산출물 품질 문제** → 해당 노드를 다시 수행한다. 티켓은 건드리지 않는다.
  - **(B) 요구사항 불일치**(티켓 내용이 사용자가 원하는 것과 다름) →
    `ge retry --edge gate_to_amend` 로 카운터를 올리고 `ge node AMEND --status running` 후
    `ge-ticket-amender` 를 호출한다. 그 노드가 **GATE-TICKET** 승인을 받아 JIRA 를 고치고,
    반영되면 계획이 무효화되므로 **N2 부터 다시** 흐른다.
  - **애매하면 지어내지 말고 사용자에게 어느 쪽인지 묻는다.**
  - 상한(`retries.gate_to_amend`, 기본 2회) 초과 시 **정지·에스컬레이션**.
- **네가 승인을 기록할 수 있는 경로는 없다.** 시도하면 훅이 차단한다.

## 진행 중 규칙

- 상태는 `.claude/graph/bin/ge` 로만 읽고 쓴다.
- 훅이 차단(exit 2)하면 사유를 읽고 규칙을 지켜 다시 한다. **우회하지 마라.**
  같은 차단을 두 번 만나면 사용자에게 보고한다.
- 푸시·PR 은 하지 않는다. CI/CD 파일은 티켓·승인된 계획이 요구하면 만들 수 있다.
- 노드가 끝날 때마다 사용자에게 한 문단으로 진행 상황을 알린다.

먼저 N0 를 수행하고 그 결과를 보고하라.
