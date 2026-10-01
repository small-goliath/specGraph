---
description: 승인 대기 중인 Graph Engineering 게이트를 반려한다 (채팅에 reject <사유> 를 입력해도 동일하다)
argument-hint: <사유>
allowed-tools: Bash(.claude/graph/bin/ge:*)
---

이 명령을 제출하는 순간 **UserPromptSubmit 훅이 이미 반려를 기록했다.**

사유: `$ARGUMENTS`

```bash
.claude/graph/bin/ge gate show
```

1. 반려된 게이트를 확인한다.
2. 반려 사유를 반영해 **해당 노드를 다시 수행한다.**
   - `GATE-PLAN` 반려 → N3 계획을 고쳐 `ge gate open --gate GATE-PLAN` 으로 **다시 제시**
   - `GATE-REVIEW` 반려 → `ge retry --edge n6_to_n4` 후 N4 재실행 (상한 초과면 정지·에스컬레이션)
   - `GATE-COMMIT` 반려 → N7 커밋 메시지를 고쳐 게이트를 다시 연다
3. 사유가 비어 있으면 무엇이 문제였는지 사용자에게 묻는다.
4. **승인 없이 다음 노드로 넘어갈 수 없다.** 훅이 차단한다.
