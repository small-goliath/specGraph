#!/usr/bin/env bash
# JIRA Ticket 훅: guard-write
# 초안 상태 파일(.claude/jira/drafts/) 을 jt 밖에서 쓰는 것을 막는다.
# 초안이 없어도 판정한다 — 없을 때 통과시키면 승인 레코드를 새로 만들어낼 수 있다.
set -u
_d="${0%/*}"; [ "$_d" = "$0" ] && _d="."
. "$_d/lib/jt-hooklib.sh"
jt_dispatch guard-write 0
