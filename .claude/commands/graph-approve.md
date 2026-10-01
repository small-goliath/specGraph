---
description: 승인 대기 중인 Graph Engineering 게이트를 승인한다 (채팅에 approve/승인 을 입력해도 동일하다)
argument-hint: [코드] [코멘트]
allowed-tools: Bash(.claude/graph/bin/ge:*)
---

이 명령을 제출하는 순간 **UserPromptSubmit 훅이 이미 승인을 기록했다.**
승인은 사용자의 실제 입력에서만 발화하며, 모델은 이 기록을 만들 수 없다.

```bash
.claude/graph/bin/ge gate show
```

1. 위 출력에서 방금 승인된 게이트를 확인한다.
   - `pending` 이 `null` 이고 `approvals` 마지막 항목이 `approved` → 정상 승인됨.
   - 아직 `pending` 이 남아 있으면 승인이 기록되지 않은 것이다
     (대기 중인 게이트가 없거나 코드가 틀림). 그 사실을 알리고 멈춘다.
2. 승인된 게이트에 따라 다음 노드로 진행한다.
   - `GATE-PLAN` → N4 (`ge-tdd-developer`)
   - `GATE-REVIEW` → N7 (`ge-commit-writer`)
   - `GATE-COMMIT` → 커밋 실행
3. `graph-engineering` skill 의 해당 노드 절을 읽고 진행한다.
