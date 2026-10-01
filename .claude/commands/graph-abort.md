---
description: Graph Engineering 그래프를 중단하고 상태 포인터를 정리한다 (브랜치는 사용자 확인 후에만)
argument-hint: [사유]
---

# /graph-abort

1. 먼저 지금 무엇이 진행 중인지 보여준다.
   ```bash
   .claude/graph/bin/ge status
   git status --short
   git log --oneline -5
   ```
2. 상태를 비활성화한다. **상태 파일 자체는 감사용으로 남는다.**
   ```bash
   .claude/graph/bin/ge abort --note "$ARGUMENTS"
   ```
   → `.claude/graph/state/current` 가 지워지고, **그 즉시 모든 훅이 무해해진다.**
   평소 작업이 다시 100% 정상으로 돌아간다.

3. **브랜치와 변경사항은 자동으로 건드리지 않는다.** 상태를 보여준 뒤 사용자에게 묻는다.
   - 티켓 브랜치를 그대로 둘지
   - base 브랜치로 돌아갈지 (`git switch <base>`)
   - 변경사항을 어떻게 할지 (그대로 / stash / 되돌리기)

   **사용자가 명시적으로 지시하기 전에는 브랜치 삭제도, 변경 되돌리기도 하지 마라.**

4. 마지막으로 알려준다: 이어서 하려면 `/graph-resume <TICKET>`,
   상태 파일은 `.claude/graph/state/<TICKET>.json` 에 남아 있다.
