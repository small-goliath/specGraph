#!/usr/bin/env bash
# JIRA Ticket 훅: approval
# 초안이 없으면 즉시 exit 0 — 평소 작업에 어떤 영향도 주지 않는다.
set -u
_d="${0%/*}"; [ "$_d" = "$0" ] && _d="."
. "$_d/lib/jt-hooklib.sh"
jt_dispatch approval
