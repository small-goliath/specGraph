---
description: 지금까지의 논의를 정해진 양식의 JIRA 티켓 초안으로 만들고 승인 후 생성한다
argument-hint: [주제 힌트] (생략하면 최근 논의 전체에서 뽑는다)
---

# /jira-ticket $1

`jira-ticket` skill 을 읽고 그 절차를 그대로 따른다.

- **재료는 지금까지의 대화다.** $1 이 주어졌으면 그 주제로 범위를 좁힌다.
- 대화에 근거가 없는 항목은 **추측하지 말고 사용자에게 묻는다** —
  특히 `완료 정의`(조사까지/배포까지)와 `제약`(하위호환·DB 스키마).
- 초안을 `jt validate` 로 검증하고, `jt draft` 로 등록한 뒤
  **본문 전문과 승인 코드를 제시하고 거기서 턴을 끝낸다.**
- 승인 없이 `mcp__atlassian__createJiraIssue` 를 호출하지 마라 — 훅이 차단한다.

먼저 `.claude/skills/jira-ticket/SKILL.md` 와
`.claude/skills/jira-ticket/references/template.md` 를 읽어라.
