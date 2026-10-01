#!/usr/bin/env bash
# JIRA Ticket 훅 공통 런처.
#
# 절대 원칙
#   1) 초안 파일(.claude/jira/drafts/current.json)이 없으면 파이썬을 띄우지도 않고 즉시 exit 0.
#      → 이 스킬을 쓰지 않는 평소 작업은 이 훅들의 존재를 감지할 수 없다.
#   2) python3 가 없거나 구현 파일이 없으면 통과(exit 0).
#   3) 차단은 구현이 exit 2 를 돌려줬을 때만 전파한다.

jt_project_dir() {
  if [ -n "${CLAUDE_PROJECT_DIR:-}" ] && [ -d "$CLAUDE_PROJECT_DIR/.claude/jira" ]; then
    printf '%s\n' "$CLAUDE_PROJECT_DIR"
    return 0
  fi
  _self="${BASH_SOURCE[0]}"; _dir="${_self%/*}"
  [ "$_dir" = "$_self" ] && _dir="."
  d=$(cd -- "$_dir/../../.." 2>/dev/null && pwd)
  printf '%s\n' "${d:-$PWD}"
}

# jt_dispatch <subcommand> [require_draft]
#   require_draft=1 (기본) : 초안이 없으면 즉시 통과 — 평소 작업 무간섭
#   require_draft=0        : 초안이 없어도 판정한다. 티켓 "생성" 가드가 여기 해당한다.
#     초안이 없을 때 통과시키면 스킬을 건너뛰고 양식 밖의 티켓을 만들 수 있어
#     "양식 강제" 자체가 무의미해진다.
jt_dispatch() {
  sub="$1"
  require_draft="${2:-1}"
  root=$(jt_project_dir)

  if [ "$require_draft" = "1" ]; then
    [ -f "$root/.claude/jira/drafts/current.json" ] || exit 0
  fi

  impl="$root/.claude/hooks/jt_hooks.py"
  [ -f "$impl" ] || exit 0

  py=""
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then py="$c"; break; fi
  done
  [ -n "$py" ] || exit 0

  CLAUDE_PROJECT_DIR="$root" "$py" "$impl" "$sub"
  rc=$?
  if [ "$rc" -ne 0 ] && [ "$rc" -ne 2 ]; then exit 0; fi
  exit $rc
}
