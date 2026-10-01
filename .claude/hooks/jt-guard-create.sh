#!/usr/bin/env bash
# JIRA Ticket 훅: guard-create
# 티켓 '생성' 만 가로챈다. 초안이 없어도 판정한다 — 그래야 양식 강제가 뚫리지 않는다.
# 다른 어떤 도구에도 붙지 않으므로 평소 작업에는 영향이 없다.
set -u
_d="${0%/*}"; [ "$_d" = "$0" ] && _d="."
. "$_d/lib/jt-hooklib.sh"
jt_dispatch guard-create 0
