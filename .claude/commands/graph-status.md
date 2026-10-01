---
description: Graph Engineering 그래프의 현재 상태(노드, 브랜치, 게이트, 재시도, 테스트)를 보여준다
argument-hint: [TICKET] (생략 시 활성 그래프)
allowed-tools: Bash(.claude/graph/bin/ge:*), Bash(git status:*), Bash(git branch:*)
---

```bash
.claude/graph/bin/ge status ${1:+--ticket $1}
```

위 출력을 사용자에게 그대로 보여주고, 다음을 덧붙여라.

- 비활성이면: "Graph Engineering 은 꺼져 있습니다. 평소 작업에 아무 영향이 없습니다.
  시작하려면 `/graph-run <TICKET>`." 이라고만 답하고 끝낸다.
- 활성이면:
  - 현재 노드에서 **다음에 무엇을 해야 하는지** 한 줄
  - 승인 대기 중인 게이트가 있으면 무엇을 승인해야 하는지와 입력 방법(`approve` / `reject <사유>`)
  - `halt_reason` 이 있으면 그 사유와 복구 방법
  - 필요하면 `.claude/graph/bin/ge history` 로 이력을 함께 보여준다
