#!/usr/bin/env bash
# Graph Engineering 훅 공통 런처.
#
# 절대 원칙
#   1) 상태 포인터(.claude/graph/state/current)가 없으면 파이썬을 띄우지도 않고 즉시 exit 0.
#      → 그래프를 켜지 않은 평소 작업은 이 훅들의 존재를 감지할 수 없다.
#   2) python3 가 없거나 구현 파일이 없으면 통과(exit 0).
#   3) 차단은 구현이 exit 2 를 돌려줬을 때만 전파한다.

ge_project_dir() {
  if [ -n "${CLAUDE_PROJECT_DIR:-}" ] && [ -d "$CLAUDE_PROJECT_DIR/.claude/graph" ]; then
    printf '%s\n' "$CLAUDE_PROJECT_DIR"
    return 0
  fi
  # 훅 스크립트 위치에서 역산 (.claude/hooks/lib → 루트)
  _self="${BASH_SOURCE[0]}"; _dir="${_self%/*}"
  [ "$_dir" = "$_self" ] && _dir="."
  d=$(cd -- "$_dir/../../.." 2>/dev/null && pwd)
  printf '%s\n' "${d:-$PWD}"
}

# ge_dispatch <subcommand>
ge_dispatch() {
  sub="$1"
  root=$(ge_project_dir)

  # (1) 비활성 즉시 통과 — 가장 싼 경로
  [ -f "$root/.claude/graph/state/current" ] || exit 0

  impl="$root/.claude/hooks/ge_hooks.py"
  [ -f "$impl" ] || exit 0

  py=""
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then py="$c"; break; fi
  done
  [ -n "$py" ] || exit 0

  CLAUDE_PROJECT_DIR="$root" "$py" "$impl" "$sub"
  rc=$?
  # 구현이 알 수 없는 코드로 죽으면 통과시킨다 (2 = 의도된 차단만 전파)
  if [ "$rc" -ne 0 ] && [ "$rc" -ne 2 ]; then
    exit 0
  fi
  exit $rc
}
