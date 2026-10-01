---
description: 끊긴 Graph Engineering 실행을 체크포인트에서 이어서 진행한다
argument-hint: <TICKET>
---

# /graph-resume $1

```bash
.claude/graph/bin/ge resume --ticket $1
```

1. 위 명령으로 활성 포인터를 복구하고 상태를 출력한다.
   상태 파일이 없으면 그 사실을 알리고 `/graph-run $1` 로 새로 시작할지 묻는다.
2. **브랜치를 먼저 맞춘다.**
   ```bash
   git status --porcelain
   git rev-parse --abbrev-ref HEAD
   ```
   현재 브랜치가 `git.branch` 와 다르면 `git switch <티켓 브랜치>` 로 이동한다.
   워킹 트리가 더러우면 무엇이 남아 있는지 보여주고 사용자에게 판단을 맡긴다.
3. 런타임을 다시 탐지한다(그 사이에 저장소가 바뀌었을 수 있다).
   ```bash
   .claude/graph/bin/ge detect all
   ```
4. `graph-engineering` skill 을 읽고, `current_node` 와 `node_status` 로 재개 지점을 정한다.
   - `node_status == "escalated"` → 재개하지 말고 `halt_reason` 과 남은 문제를 보고한다.
     사용자가 어떻게 할지 정한 뒤에 움직인다.
   - `gates.pending` 이 있으면 **그 게이트를 다시 제시하고 승인을 기다린다.** 이미 승인된 것처럼 굴지 마라.
   - 그 외에는 `current_node` 부터 이어간다.
5. 재개 지점이 **N1 · N4 · N5** 면 그 노드에 **다시 진입**하는 것이므로,
   담당 에이전트가 목표 상태(`검토 중`/`개발 중`/`검증 중`)로 **다시 전이하고 기록**해야 한다.
   그 사이 사람이 티켓 상태를 바꿔놨을 수 있으므로 실제 상태를 조회해서 확인한다
   (`ge jira-target --node <노드>` 로 목표 상태를 받는다). 상세: `references/jira.md` §1.
6. 지금까지 무엇이 됐고 무엇이 남았는지 요약해 보고한 뒤 진행한다.
