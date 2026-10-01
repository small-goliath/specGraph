#!/usr/bin/env bash
# AI-Engineering 설치 스크립트 — Graph Engineering + JIRA Ticket.
#
#   사용법: .claude/graph/install.sh <대상 프로젝트 경로> [옵션]
#
# 이 템플릿의 산출물을 대상 프로젝트의 .claude/ 로 복사한다.
# 전부 project scope 다 — ~/.claude(user scope)는 절대 건드리지 않는다.
#
# 설계 원칙
#   1) 대상 저장소의 **기존 파일을 말없이 덮어쓰지 않는다.** 다르면 건너뛰고 알린다(--force 로 덮어씀).
#   2) 이 시스템이 소유한 파일만 건드린다
#      (ge-*, jt-*, graph-*.md, jira-*.md, skills/{graph-engineering,jira-ticket}/**, graph/**, jira/**).
#      대상 프로젝트의 다른 에이전트·커맨드·스킬은 손대지 않는다.
#   3) 원본 경로를 못 찾으면 **즉시 실패**한다. 0건 복사하고 "완료" 라고 말하지 않는다.
#   4) 설치 후 회귀 테스트를 실제로 돌려 동작을 증명한다.
set -euo pipefail

# ── 원본 위치 확정 ─────────────────────────────────────────────────────────
# 이 스크립트는 <템플릿>/.claude/graph/install.sh 에 있어야 한다.
SRC_CLAUDE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

TARGET=""; DRY=0; FORCE=0; DO_CLAUDE_MD=1; DO_TESTS=1

usage() {
  cat <<'EOF'
AI-Engineering 설치 — Graph Engineering + JIRA Ticket

  .claude/graph/install.sh <대상 프로젝트 경로> [옵션]

옵션
  --dry-run        실제로 바꾸지 않고 무엇이 일어날지만 출력
  --force          내용이 다른 기존 파일을 덮어씀 (.bak 백업을 남긴다)
                   단 config.json 두 개는 --force 여도 덮지 않는다 — 누락 키만 병합한다.
                   템플릿 값으로 초기화하려면 그 파일을 지우고 다시 설치한다.
  --no-claude-md   대상 CLAUDE.md 에 안내 블록을 추가하지 않음
  --no-tests       설치 후 회귀 테스트를 돌리지 않음
  -h, --help       이 도움말

설치되는 것 (전부 project scope)
  [Graph Engineering]
  .claude/agents/ge-*.md                  노드 서브에이전트
  .claude/commands/graph-*.md             /graph-run /graph-status /graph-resume
                                          /graph-approve /graph-reject /graph-abort
  .claude/skills/graph-engineering/**     그래프 SOP + references
  .claude/hooks/ge-*.sh, ge_hooks.py      결정론적 가드/게이트 훅
  .claude/hooks/lib/ge-hooklib.sh         훅 공통 런처
  .claude/graph/bin/ge                    상태 CLI 래퍼
  .claude/graph/lib/ge_state.py           공유 상태 런타임
  .claude/graph/config.json               팀 공용 설정 (이미 있으면 누락 키만 병합)
  .claude/graph/tests/run-tests.sh        회귀 테스트

  [JIRA Ticket]
  .claude/commands/jira-*.md              /jira-ticket /jira-approve /jira-reject
  .claude/skills/jira-ticket/**           티켓 작성 절차 + 양식 규칙
  .claude/hooks/jt-*.sh, jt_hooks.py      생성 가드 · 초안 상태 가드 · 승인 훅
  .claude/hooks/lib/jt-hooklib.sh         훅 공통 런처
  .claude/jira/bin/jt                     양식 검증 CLI
  .claude/jira/lib/jt_ticket.py           검증기 + 초안 런타임
  .claude/jira/config.json                프로젝트·이슈타입 설정 (이미 있으면 누락 키만 병합)
  .claude/jira/tests/run-tests.sh         회귀 테스트

  [공통]
  .claude/settings.json                   훅·권한 병합 (기존 설정 보존)
  .gitignore                              런타임 산출물 제외 규칙 추가

설치되지 않는 것
  .claude/graph/state|logs|tmp/  실행 상태·로그·임시 산출물 (프로젝트마다 새로 생긴다)
  .claude/jira/drafts/           티켓 초안 (논의 내용이 들어간다)
  .claude/settings.local.json    개인 설정
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)      DRY=1 ;;
    --force)        FORCE=1 ;;
    --no-claude-md) DO_CLAUDE_MD=0 ;;
    --no-tests)     DO_TESTS=0 ;;
    -h|--help)      usage; exit 0 ;;
    -*)             echo "❌ 알 수 없는 옵션: $1" >&2; usage >&2; exit 1 ;;
    *)              TARGET="$1" ;;
  esac
  shift
done

# ── 사전 검증 — 실패는 조용히 넘어가지 않는다 ────────────────────────────────
[ -n "$TARGET" ] || { usage >&2; exit 1; }

if [ ! -f "$SRC_CLAUDE/graph/lib/ge_state.py" ]; then
  echo "❌ 원본을 찾을 수 없습니다: $SRC_CLAUDE/graph/lib/ge_state.py" >&2
  echo "   이 스크립트는 <템플릿>/.claude/graph/install.sh 위치에서 실행해야 합니다." >&2
  exit 1
fi

command -v python3 >/dev/null 2>&1 || {
  echo "❌ python3 가 필요합니다. 상태 런타임과 훅이 python3 로 동작합니다." >&2; exit 1; }

[ -d "$TARGET" ] || { echo "❌ 대상 디렉터리가 없습니다: $TARGET" >&2; exit 1; }
TARGET="$(cd "$TARGET" && pwd)"
DST_CLAUDE="$TARGET/.claude"

case "$TARGET" in
  "$HOME/.claude"|"$HOME/.claude/"*)
    echo "❌ user scope($HOME/.claude)에는 설치하지 않습니다. 이 시스템은 project scope 전용입니다." >&2
    exit 1 ;;
esac

if [ "$DST_CLAUDE" = "$SRC_CLAUDE" ]; then
  echo "❌ 대상이 템플릿 자신입니다. 다른 프로젝트 경로를 지정하세요." >&2
  exit 1
fi

if [ ! -d "$TARGET/.git" ]; then
  echo "⚠️  $TARGET 은 git 저장소가 아닙니다."
  echo "   그래프는 티켓 브랜치를 만들고 커밋까지 수행하므로 git 저장소여야 합니다."
  echo "   계속하려면 대상에서 먼저 'git init' 을 실행하세요."
fi

say() { [ "$DRY" -eq 1 ] && echo "  [dry-run] $*" || echo "  $*"; }
run() { [ "$DRY" -eq 1 ] || "$@"; }

echo "Graph Engineering 설치"
echo "  원본: $SRC_CLAUDE"
echo "  대상: $DST_CLAUDE"
[ "$DRY" -eq 1 ] && echo "  (dry-run — 아무것도 바꾸지 않습니다)"
echo

# ── 1. 설치 대상 파일 목록 (이 시스템이 소유한 것만) ─────────────────────────
FILES=()
while IFS= read -r f; do
  [ -n "$f" ] && FILES+=("$f")
done < <(
  cd "$SRC_CLAUDE" && {
    # Graph Engineering
    find agents -maxdepth 1 -type f -name 'ge-*.md'
    find commands -maxdepth 1 -type f -name 'graph-*.md'
    find skills/graph-engineering -type f
    find hooks -maxdepth 1 -type f \( -name 'ge-*.sh' -o -name 'ge_hooks.py' \)
    find hooks/lib -maxdepth 1 -type f -name 'ge-*.sh'
    find graph/bin graph/lib graph/tests -type f
    echo graph/config.json
    echo graph/install.sh
    # JIRA Ticket
    find commands -maxdepth 1 -type f -name 'jira-*.md'
    find skills/jira-ticket -type f
    find hooks -maxdepth 1 -type f \( -name 'jt-*.sh' -o -name 'jt_hooks.py' \)
    find hooks/lib -maxdepth 1 -type f -name 'jt-*.sh'
    find jira/bin jira/lib jira/tests -type f
    echo jira/config.json
  } 2>/dev/null | grep -v '__pycache__' | sort -u
)

if [ "${#FILES[@]}" -lt 30 ]; then
  echo "❌ 원본에서 찾은 파일이 ${#FILES[@]}개뿐입니다 — 템플릿이 손상됐거나 경로가 잘못됐습니다." >&2
  exit 1
fi

# 두 시스템이 모두 들어왔는지 확인한다. 한쪽만 복사되면 반쪽짜리 설치가 된다.
for must in graph/lib/ge_state.py jira/lib/jt_ticket.py hooks/ge_hooks.py hooks/jt_hooks.py; do
  printf '%s\n' "${FILES[@]}" | grep -qx "$must" || {
    echo "❌ 필수 파일이 목록에 없습니다: .claude/$must" >&2; exit 1; }
done

# 프로젝트가 손대는 설정 — 덮어쓰지 않고 **누락된 키만 병합**한다.
# 새 기능이 추가한 설정 키(예: jira.transition_policy)는 채워지고,
# 대상이 바꾼 값(cloud_id, site_url, project_overrides …)은 그대로 남는다.
is_config() {
  case "$1" in
    graph/config.json|jira/config.json) return 0 ;;
    *) return 1 ;;
  esac
}

copied=0; same=0; skipped=0; merged=0; conflicts=(); to_merge=()
for rel in "${FILES[@]}"; do
  src="$SRC_CLAUDE/$rel"; dst="$DST_CLAUDE/$rel"

  if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
    same=$((same+1)); continue
  fi

  if [ -f "$dst" ]; then
    if is_config "$rel"; then
      # --force 여도 덮지 않는다. 아래 병합 단계에서 누락 키만 채운다.
      to_merge+=("$rel"); continue
    fi
    if [ "$FORCE" -eq 0 ]; then
      conflicts+=("$rel"); skipped=$((skipped+1)); continue
    fi
    say "백업 후 갱신: .claude/$rel → .claude/$rel.bak"
    run cp "$dst" "$dst.bak"
  else
    say "설치: .claude/$rel"
  fi

  run mkdir -p "$(dirname "$dst")"
  run cp "$src" "$dst"
  copied=$((copied+1))
done

[ "$same" -gt 0 ] && echo "  동일 (변경 없음): ${same}건"

if [ "${#conflicts[@]}" -gt 0 ]; then
  echo
  echo "  ⚠️  내용이 다른 기존 파일 ${#conflicts[@]}건을 건너뛰었습니다 (덮어쓰려면 --force):"
  printf '       .claude/%s\n' "${conflicts[@]}"
fi

# ── 1-1. config.json 병합 (덮어쓰지 않고 누락 키만 채운다) ──────────────────
# 대상이 바꾼 값은 한 글자도 건드리지 않는다. 새 키만 추가하고,
# `$comment*` 문서용 키만 템플릿 최신값으로 갱신한다(설명이 낡으면 오히려 해롭다).
merge_config() {  # merge_config <rel> — 결과 메시지는 MERGE_OUT 에 담긴다
  local rel="$1" tmp rc=0
  tmp="$(mktemp "${TMPDIR:-/tmp}/ge-merge.XXXXXX")"
  python3 - "$SRC_CLAUDE/$rel" "$DST_CLAUDE/$rel" "$DRY" >"$tmp" 2>&1 <<'PYMERGE' || rc=$?
import json, shutil, sys

src, dst, dry = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
try:
    s = json.load(open(src, encoding="utf-8"))
except Exception as exc:
    sys.exit("원본 설정을 읽을 수 없습니다: %s" % exc)
try:
    d = json.load(open(dst, encoding="utf-8"))
except Exception as exc:
    sys.exit("대상 설정을 읽을 수 없습니다(%s). 손으로 고친 뒤 다시 실행하세요." % exc)

added, refreshed, conflicts = [], [], []


def merge(s, d, path=""):
    """s 의 키 중 d 에 **없는 것만** 추가한다. 기존 값은 절대 바꾸지 않는다.

    예외: `$comment` 로 시작하는 문서용 키는 템플릿 값으로 갱신한다.
    배열은 통째로 하나의 값으로 본다 — 대상이 항목을 뺐을 수 있으므로 합치지 않는다.
    """
    for k, v in s.items():
        here = "%s.%s" % (path, k) if path else k
        if k not in d:
            d[k] = v
            added.append(here)
        elif isinstance(v, dict) and isinstance(d[k], dict):
            merge(v, d[k], here)
        elif isinstance(v, dict) != isinstance(d[k], dict):
            # 구조가 어긋났다 — 값을 멋대로 바꾸지 않고 사실만 알린다.
            conflicts.append(here)
        elif k.startswith("$comment") and d[k] != v:
            d[k] = v
            refreshed.append(here)


merge(s, d)
for k in conflicts:
    print("  ⚠ 구조 불일치(손대지 않음): %s — 템플릿과 타입이 다릅니다" % k)
if not added and not refreshed:
    print("동일 (병합할 키 없음)" if not conflicts else "구조 불일치만 있음")
    sys.exit(0)
if not dry:
    shutil.copyfile(dst, dst + ".bak")
    json.dump(d, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    open(dst, "a", encoding="utf-8").write("\n")
print("키 %d개 추가%s" % (len(added), ", 설명 %d개 갱신" % len(refreshed) if refreshed else ""))
for k in added:
    print("  + %s" % k)
PYMERGE
  MERGE_OUT="$(cat "$tmp")"
  rm -f "$tmp"
  return "$rc"
}

for rel in ${to_merge[@]+"${to_merge[@]}"}; do
  if ! merge_config "$rel"; then
    echo "  ❌ .claude/$rel 병합 실패:" >&2
    printf '%s\n' "$MERGE_OUT" | sed 's/^/     /' >&2
    exit 1
  fi
  case "$MERGE_OUT" in
    "동일"*)
      same=$((same+1)) ;;
    *)
      say "병합 (기존 값 보존, .bak 백업): .claude/$rel"
      printf '%s\n' "$MERGE_OUT" | sed 's/^/    /'
      merged=$((merged+1)) ;;
  esac
done

# 런타임 디렉터리는 만들어만 둔다 (내용은 복사하지 않는다)
run mkdir -p "$DST_CLAUDE/graph/state" "$DST_CLAUDE/graph/logs" "$DST_CLAUDE/graph/tmp" \
             "$DST_CLAUDE/jira/drafts"

# ── 2. 실행 권한 ──────────────────────────────────────────────────────────
say "실행 권한: hooks/*.sh, hooks/lib/*.sh, graph/bin/ge, jira/bin/jt, tests/*.sh, install.sh"
if [ "$DRY" -eq 0 ]; then
  chmod +x "$DST_CLAUDE"/hooks/ge-*.sh          2>/dev/null || true
  chmod +x "$DST_CLAUDE"/hooks/jt-*.sh          2>/dev/null || true
  chmod +x "$DST_CLAUDE"/hooks/lib/ge-*.sh      2>/dev/null || true
  chmod +x "$DST_CLAUDE"/hooks/lib/jt-*.sh      2>/dev/null || true
  chmod +x "$DST_CLAUDE"/graph/bin/ge           2>/dev/null || true
  chmod +x "$DST_CLAUDE"/graph/tests/*.sh       2>/dev/null || true
  chmod +x "$DST_CLAUDE"/graph/install.sh       2>/dev/null || true
  chmod +x "$DST_CLAUDE"/jira/bin/jt            2>/dev/null || true
  chmod +x "$DST_CLAUDE"/jira/tests/*.sh        2>/dev/null || true
fi

# ── 3. settings.json 병합 (훅 등록 + 권한) ────────────────────────────────
SRC_SETTINGS="$SRC_CLAUDE/settings.json"
DST_SETTINGS="$DST_CLAUDE/settings.json"

if [ ! -f "$SRC_SETTINGS" ]; then
  echo "  ⚠️  원본에 settings.json 이 없습니다 — 훅 등록을 건너뜁니다."
elif [ ! -f "$DST_SETTINGS" ]; then
  say "설치: .claude/settings.json"
  run mkdir -p "$DST_CLAUDE"
  run cp "$SRC_SETTINGS" "$DST_SETTINGS"
else
  say "병합: .claude/settings.json (기존 설정 보존, .bak 백업)"
  if [ "$DRY" -eq 0 ]; then
    cp "$DST_SETTINGS" "$DST_SETTINGS.bak"
    python3 - "$SRC_SETTINGS" "$DST_SETTINGS" <<'PY'
import json, sys

src, dst = sys.argv[1], sys.argv[2]
s = json.load(open(src, encoding="utf-8"))
try:
    d = json.load(open(dst, encoding="utf-8"))
except Exception as exc:
    sys.exit("    ❌ 대상 settings.json 을 읽을 수 없습니다(%s). .bak 을 확인하세요." % exc)


def cmds(entry):
    return tuple(h.get("command") for h in entry.get("hooks", []))


added_hooks = updated_hooks = 0
d.setdefault("hooks", {})
for event, entries in (s.get("hooks") or {}).items():
    existing = d["hooks"].setdefault(event, [])
    for e in entries:
        # 같은 스크립트를 등록한 항목이 있으면 matcher 를 최신값으로 맞춘다.
        # (예: PostToolUse matcher 에 Bash 가 추가된 경우)
        hit = next((x for x in existing if cmds(x) == cmds(e)), None)
        if hit is None:
            existing.append(e)
            added_hooks += 1
        elif hit.get("matcher") != e.get("matcher"):
            hit["matcher"] = e.get("matcher")
            updated_hooks += 1

perms = d.setdefault("permissions", {}).setdefault("allow", [])
added_perms = 0
for p in (s.get("permissions") or {}).get("allow", []):
    if p not in perms:
        perms.append(p)
        added_perms += 1

json.dump(d, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
open(dst, "a", encoding="utf-8").write("\n")
print("    훅 %d개 추가 / %d개 matcher 갱신, 권한 %d개 추가 (기존 설정은 그대로)"
      % (added_hooks, updated_hooks, added_perms))
PY
  fi
fi

# ── 4. .gitignore — 런타임 산출물 제외 ────────────────────────────────────
# state/logs/drafts 에는 JIRA 티켓 원문과 테스트 실패 로그가 그대로 들어간다. 커밋하면 안 된다.
# 두 시스템을 **따로** 판정한다 — 그래야 기존 설치에 덧설치할 때 빠진 쪽만 채워진다.
GI="$TARGET/.gitignore"

gi_add() {  # gi_add <검사할 규칙> <블록 본문>
  if [ -f "$GI" ] && grep -qxF "$1" "$GI" 2>/dev/null; then
    say ".gitignore: 이미 등록됨 ($1)"
    return 0
  fi
  say ".gitignore: 제외 규칙 추가 ($1)"
  [ "$DRY" -eq 1 ] && return 0
  { [ -f "$GI" ] && [ -s "$GI" ] && echo ""; printf '%s\n' "$2"; } >> "$GI"
}

gi_add ".claude/graph/state/" \
"# Graph Engineering 런타임 산출물 — 실행별·머신별이고 JIRA 티켓 원문이 들어간다.
# .claude/graph/config.json 은 팀 공용 설정이므로 반대로 반드시 커밋한다.
.claude/graph/state/
.claude/graph/logs/
.claude/graph/tmp/"

gi_add ".claude/jira/drafts/" \
"# JIRA Ticket 초안 — 논의 내용과 티켓 원문이 들어간다.
# .claude/jira/config.json 은 팀 공용 설정이므로 반대로 반드시 커밋한다.
.claude/jira/drafts/"

# ── 5. CLAUDE.md — 진입점 안내 ────────────────────────────────────────────
# 두 시스템을 **따로** 판정한다 — 기존 설치에 덧설치할 때 빠진 쪽만 추가된다.
CM="$TARGET/CLAUDE.md"
CM_BACKED_UP=0

cm_add() {  # cm_add <마커> <블록 본문>
  if [ "$DO_CLAUDE_MD" -eq 0 ]; then
    say "CLAUDE.md: --no-claude-md 로 건너뜀 ($1)"
    return 0
  fi
  if [ -f "$CM" ] && grep -qF "$1" "$CM" 2>/dev/null; then
    say "CLAUDE.md: 이미 안내 블록이 있음 ($1)"
    return 0
  fi
  say "CLAUDE.md: 안내 블록 추가 ($1)"
  [ "$DRY" -eq 1 ] && return 0
  if [ -f "$CM" ] && [ "$CM_BACKED_UP" -eq 0 ]; then
    cp "$CM" "$CM.bak"; CM_BACKED_UP=1
  fi
  { [ -f "$CM" ] && [ -s "$CM" ] && echo ""; printf '%s\n' "$2"; } >> "$CM"
}

cm_add "## Graph Engineering (opt-in)" \
'## Graph Engineering (opt-in)

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
```'

cm_add "## JIRA Ticket (상시)" \
'## JIRA Ticket (상시)

논의를 **정해진 양식**의 JIRA 티켓으로 만든다. 그래프와 무관하게 단독으로 동작한다.

- 만들기: `/jira-ticket [주제]` · 승인: `approve` / `/jira-approve` · 반려: `/jira-reject <사유>`
- 양식: `.claude/skills/jira-ticket/references/template.md`

**등록·승인된 초안 없이는 `mcp__atlassian__createJiraIssue` 가 훅에 차단된다.**
양식 검증을 통과한 티켓은 `/graph-run` 이 양식 위반으로 멈추지 않는다(같은 규격).

```bash
.claude/jira/tests/run-tests.sh
```'

# ── 6. 설치 검증 ──────────────────────────────────────────────────────────
echo
FAILED=0
if [ "$DRY" -eq 0 ]; then
  echo "설치 검증"

  # 대상에 진행 중인 그래프가 있는지 먼저 본다 — 아래 프로브들의 전제가 달라진다.
  ACTIVE_TICKET=""
  if [ -f "$DST_CLAUDE/graph/state/current" ]; then
    ACTIVE_TICKET="$(tr -d ' \n' < "$DST_CLAUDE/graph/state/current" 2>/dev/null || true)"
  fi

  # 활성/비활성 어느 쪽이든 런타임이 실제로 도는지 본다(출력은 한 줄로 짧다).
  if out=$(cd "$TARGET" && CLAUDE_PROJECT_DIR="$TARGET" "$DST_CLAUDE/graph/bin/ge" show --field ticket 2>&1); then
    if [ -n "$ACTIVE_TICKET" ]; then
      echo "  ✅ ge 런타임 동작 (진행 중인 그래프: $ACTIVE_TICKET)"
    else
      echo "  ✅ ge 런타임 동작 (현재 비활성)"
    fi
  else
    echo "  ❌ ge 실행 실패:"; printf '%s\n' "$out" | sed 's/^/     /'; FAILED=1
  fi

  if out=$(cd "$TARGET" && CLAUDE_PROJECT_DIR="$TARGET" "$DST_CLAUDE/jira/bin/jt" status 2>&1); then
    echo "  ✅ jt 런타임 동작 ($(printf '%s' "$out" | head -1))"
  else
    echo "  ❌ jt 실행 실패:"; printf '%s\n' "$out" | sed 's/^/     /'; FAILED=1
  fi

  # 비활성 상태에서 훅이 정말 무해한지 — 이 시스템의 가장 중요한 성질.
  # 단 이 프로브는 "그래프가 꺼져 있다" 를 전제로 한다. 대상에 진행 중인 그래프가 있으면
  # 훅이 차단하는 것이 **정상 동작**이므로 설치 실패로 판정하지 않는다.
  probe='{"tool_name":"Bash","tool_input":{"command":"git commit -m \"아무 메시지\""}}'
  if [ -n "$ACTIVE_TICKET" ]; then
    echo "  ⏭  비활성 훅 프로브 건너뜀 — 대상에 진행 중인 그래프가 있습니다 (티켓 ${ACTIVE_TICKET})"
    echo "     지금 훅이 차단하는 것은 정상입니다. 상태 확인: cd \"$TARGET\" && .claude/graph/bin/ge status"
  elif printf '%s' "$probe" | CLAUDE_PROJECT_DIR="$TARGET" "$DST_CLAUDE/hooks/ge-guard-bash.sh" >/dev/null 2>&1; then
    echo "  ✅ 비활성 훅 무해함 (그래프를 켜기 전엔 평소 작업에 간섭하지 않는다)"
  else
    echo "  ❌ 그래프가 꺼져 있는데 훅이 차단했습니다."
    echo "     .claude/graph/state/current 가 없는데 차단됐다면 설치가 잘못된 것입니다."
    echo "     재현: printf '%s' '$probe' | CLAUDE_PROJECT_DIR=\"$TARGET\" \\"
    echo "             \"$DST_CLAUDE/hooks/ge-guard-bash.sh\"; echo exit=\$?"
    FAILED=1
  fi

  wprobe='{"tool_name":"Write","tool_input":{"file_path":"src/App.java"}}'
  if printf '%s' "$wprobe" | CLAUDE_PROJECT_DIR="$TARGET" "$DST_CLAUDE/hooks/jt-guard-write.sh" >/dev/null 2>&1; then
    echo "  ✅ 티켓 훅도 평소 쓰기에 간섭하지 않는다"
  else
    echo "  ❌ 평소 쓰기를 티켓 훅이 차단했습니다 — 설치가 잘못됐습니다."; FAILED=1
  fi

  # 티켓 생성 가드는 **항상** 켜져 있다(초안 없이 양식 밖의 티켓을 만들 수 없게).
  # 이 프로브는 exit 2 를 기대하므로 `set -e` 에 걸리지 않게 상태를 직접 받는다.
  cprobe='{"tool_name":"mcp__atlassian__createJiraIssue","tool_input":{"summary":"probe"}}'
  crc=0
  printf '%s' "$cprobe" \
    | CLAUDE_PROJECT_DIR="$TARGET" "$DST_CLAUDE/hooks/jt-guard-create.sh" >/dev/null 2>&1 || crc=$?
  if [ "$crc" -eq 2 ]; then
    echo "  ✅ 초안 없는 티켓 생성은 차단된다 (양식 강제 동작)"
  else
    echo "  ❌ 초안 없이도 티켓이 생성됩니다 — 양식 강제가 동작하지 않습니다."; FAILED=1
  fi

  # 설정 스키마 — 템플릿에 있는 키가 대상에 다 있는지 실측한다.
  # 새 기능이 요구하는 키(예: jira.transition_policy)가 빠지면 그 기능이 **조용히** 꺼진다.
  missing_keys() {  # missing_keys <rel> — 빠진 키를 MISS 에 담는다
    local rel="$1" tmp rc=0
    tmp="$(mktemp "${TMPDIR:-/tmp}/ge-schema.XXXXXX")"
    python3 - "$SRC_CLAUDE/$rel" "$DST_CLAUDE/$rel" >"$tmp" 2>/dev/null <<'PYCHECK' || rc=$?
import json, sys

s = json.load(open(sys.argv[1], encoding="utf-8"))
d = json.load(open(sys.argv[2], encoding="utf-8"))
missing = []


def walk(s, d, path=""):
    for k, v in s.items():
        here = "%s.%s" % (path, k) if path else k
        if k not in d:
            missing.append(here)
        elif isinstance(v, dict) and isinstance(d[k], dict):
            walk(v, d[k], here)
        elif isinstance(v, dict) != isinstance(d[k], dict):
            # 키는 있지만 구조가 다르다 — 그 아래 설정은 전부 읽히지 않는다.
            missing.append(here + "(구조 불일치)")


walk(s, d)
print(" ".join(missing))
PYCHECK
    MISS="$(cat "$tmp")"
    rm -f "$tmp"
    return "$rc"
  }

  schema_ok=1
  for cf in graph/config.json jira/config.json; do
    if ! missing_keys "$cf"; then
      echo "  ❌ .claude/$cf 를 읽을 수 없습니다 — JSON 문법을 확인하세요."; FAILED=1; schema_ok=0
    elif [ -n "$MISS" ]; then
      echo "  ❌ .claude/$cf 에 템플릿 키가 빠져 있습니다: $MISS"
      echo "     → 그 키를 쓰는 기능이 조용히 꺼집니다. 다시 설치하거나 손으로 채우세요."
      FAILED=1; schema_ok=0
    fi
  done
  [ "$schema_ok" -eq 1 ] && echo "  ✅ 설정 스키마 최신 (템플릿 키가 모두 존재)"

  run_suite() {  # run_suite <이름> <스크립트>
    if [ "$DO_TESTS" -eq 0 ]; then
      echo "  ⏭  $1 회귀 테스트 건너뜀 (--no-tests). 수동 실행: $2"
    elif [ -x "$2" ]; then
      if out=$("$2" 2>&1); then
        echo "  ✅ $1 회귀 통과 — $(printf '%s' "$out" | grep -o '결과: [0-9]* PASS / [0-9]* FAIL' | tail -1)"
      else
        echo "  ❌ $1 회귀 실패:"
        printf '%s\n' "$out" | grep -E 'FAIL' | sed 's/^/     /' | head -20
        echo "     재현: $2"; FAILED=1
      fi
    else
      echo "  ⚠️  회귀 테스트 스크립트를 찾을 수 없습니다: $2"; FAILED=1
    fi
  }
  run_suite "Graph"  "$DST_CLAUDE/graph/tests/run-tests.sh"
  run_suite "Ticket" "$DST_CLAUDE/jira/tests/run-tests.sh"
fi

# ── 7. 마무리 ─────────────────────────────────────────────────────────────
JIRA_SITE=$(python3 -c '
import json,sys
try: print(json.load(open(sys.argv[1],encoding="utf-8")).get("jira",{}).get("site_url","(미설정)"))
except Exception: print("(읽기 실패)")' "$DST_CLAUDE/graph/config.json" 2>/dev/null || echo "(읽기 실패)")

JIRA_PROJ=$(python3 -c '
import json,sys
try: print(json.load(open(sys.argv[1],encoding="utf-8")).get("ticket",{}).get("default_project","(미설정)"))
except Exception: print("(읽기 실패)")' "$DST_CLAUDE/jira/config.json" 2>/dev/null || echo "(읽기 실패)")

cat <<EOF

완료 — 설치/갱신 ${copied}건 · 설정 병합 ${merged}건 · 동일 ${same}건 · 건너뜀 ${skipped}건

다음 할 일
  1. 대상 프로젝트에서 Claude Code 를 재시작한다 (훅·커맨드·에이전트 인식).
  2. .claude/graph/config.json 의 JIRA 설정을 확인한다.
       현재 site_url: ${JIRA_SITE}
       다른 사이트면 jira.cloud_id 와 site_url 을 바꾼다.
       워크플로가 다르면 jira.status_targets 와 project_overrides 의
       known_status_ids 를 그 프로젝트 값으로 바꾼다.
       N1=검토 중 · N4=개발 중 · N5=검증 중 으로 **매 진입마다** 전이하며,
       전이 기록이 없으면 ge node 가 진입을 거부한다(jira.transition_policy.enforce).
       (transition 은 이름이 아니라 목표 상태 to.name 으로 매칭한다).
  3. .claude/jira/config.json 의 티켓 설정을 확인한다.
       현재 default_project: ${JIRA_PROJ}
       ticket.default_issue_type 이 대상 프로젝트에 실제로 있는 이름인지 확인한다
       (프로젝트마다 다르다 — 생성 직전에 메타데이터로 재확인한다).
       접속 정보는 비워두면 graph 설정을 그대로 쓴다.
  4. 워킹 트리를 깨끗하게 만든 뒤 첫 티켓을 굴린다:
       /graph-run <TICKET>
  5. 논의를 티켓으로 남기려면:
       /jira-ticket

두 시스템의 성격이 다르다.
  · Graph Engineering 은 opt-in 이다. /graph-run 로 켜기 전까지 훅은 전부 즉시 통과한다.
  · JIRA Ticket 은 생성 가드만 상시 켜져 있다. mcp__atlassian__createJiraIssue 하나에만
    붙어서, 등록·승인된 초안 없이는 양식 밖의 티켓을 만들 수 없게 한다.
    그 밖의 평소 작업에는 어느 쪽도 간섭하지 않는다.
EOF

exit "$FAILED"
