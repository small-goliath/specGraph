## Graph Engineering (opt-in)

JIRA 티켓 하나로 개발을 끝까지 굴리는 **Graph Engineering** 파이프라인이 설치돼 있다.
**기본은 꺼져 있고, 켜지 않으면 평소 작업과 100% 동일하게 동작한다.**

- 켜기: `/graph-run <TICKET>` (예: `/graph-run PPS-283`)
- 조회/재개/중단: `/graph-status`, `/graph-resume <TICKET>`, `/graph-abort`
- 승인: 채팅에 `approve`(또는 `승인`) / 반려: `reject <사유>`
- 그래프 SOP: `.claude/skills/graph-engineering/SKILL.md`

활성 판정은 `.claude/graph/state/current` 파일의 존재로만 한다.
이 파일이 없으면 `.claude/hooks/ge-*.sh` 는 전부 즉시 통과한다.

훅이나 상태 런타임을 고쳤으면 반드시 회귀 테스트를 돌린다:

```bash
.claude/graph/tests/run-tests.sh
```

## JIRA Ticket (상시)

논의를 **정해진 양식**의 JIRA 티켓으로 만든다. 그래프와 무관하게 단독으로 동작한다.

- 만들기: `/jira-ticket [주제]` · 승인: `approve` / `/jira-approve` · 반려: `/jira-reject <사유>`
- 양식: `.claude/skills/jira-ticket/references/template.md`

**등록·승인된 초안 없이는 `mcp__atlassian__createJiraIssue` 가 훅에 차단된다.**
양식 검증을 통과한 티켓은 `/graph-run` 이 양식 위반으로 멈추지 않는다(같은 규격).

```bash
.claude/jira/tests/run-tests.sh
```
