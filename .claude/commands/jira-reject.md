---
description: 대기 중인 JIRA 티켓 초안을 반려한다
argument-hint: <사유>
---

# /jira-reject $1

이 입력 자체가 `UserPromptSubmit` 훅에 잡혀 반려로 기록된다.
사유($1)를 반영해 초안을 고치고 `jt draft` 로 **다시 등록**한 뒤 재승인을 요청하라.
해시가 바뀌므로 이전 승인은 자동으로 무효다.
