#!/usr/bin/env bash
# Graph Engineering 훅: checkpoint
# 그래프 비활성 시 즉시 exit 0 — 평소 작업에 어떤 영향도 주지 않는다.
set -u
_d="${0%/*}"; [ "$_d" = "$0" ] && _d="."
. "$_d/lib/ge-hooklib.sh"
ge_dispatch checkpoint
