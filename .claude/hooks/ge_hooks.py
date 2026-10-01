#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Graph Engineering — 결정론적 엣지/가드 훅 구현.

계약
  stdin  : Claude Code 훅 JSON
  exit 0 : 통과 (stdout 은 UserPromptSubmit/SessionStart 에서만 컨텍스트로 주입된다)
  exit 2 : 차단 (stderr 이 모델에게 전달된다)

절대 원칙
  1. `.claude/graph/state/current` 가 없으면 **무조건 즉시 통과**한다.
     그래프를 켜지 않은 평소 작업은 이 훅들의 존재를 알 수 없어야 한다.
  2. 내부 오류는 통과(exit 0)로 처리한다. 훅이 세션을 망가뜨리지 않는다.
  3. 차단은 명시적으로 위반이 판정됐을 때만 한다.
  4. **강제력은 도구 종류에 의존하지 않는다.** Edit/Write 로 막는 것은 Bash 로도 막는다.
     (셸 리다이렉션·tee·sed -i·cp/mv 로 소스를 쓰는 경로가 예전에 열려 있었다.)
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "graph", "lib"))
try:
    import ge_state as G
except Exception:  # 런타임이 없으면 훅은 존재하지 않는 것처럼 행동한다
    sys.exit(0)


# --------------------------------------------------------------------------
# 공통
# --------------------------------------------------------------------------

def read_input():
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def allow():
    sys.exit(0)


def deny(reason: str):
    sys.stderr.write("[Graph Engineering · 차단]\n" + reason.rstrip() + "\n")
    sys.exit(2)


def warn(reason: str):
    """차단하지 않고 모델에게 경고만 전달한다(비차단 오류 채널)."""
    sys.stderr.write("[Graph Engineering · 경고]\n" + reason.rstrip() + "\n")
    sys.exit(0)


def require_active():
    if not G.is_active():
        sys.exit(0)
    st = G.load()
    if not st:
        sys.exit(0)
    return st


GRAPH_INTERNAL_WRITABLE = (".claude/graph/tmp/",)
GRAPH_PROTECTED = (".claude/graph/state/", ".claude/graph/logs/")


def tool_paths(data):
    ti = data.get("tool_input") or {}
    out = []
    for key in ("file_path", "notebook_path", "path"):
        v = ti.get(key)
        if isinstance(v, str) and v:
            out.append(v)
    for e in ti.get("edits") or []:
        if isinstance(e, dict) and isinstance(e.get("file_path"), str):
            out.append(e["file_path"])
    return [G.norm_rel(p) for p in out]


def header(st, extra=""):
    b = G.dget(st, "runtime.build", {}) or {}
    gy = "있음 → graphify skill 최우선" if G.dget(st, "runtime.graphify.available") else "없음 → 일반 탐색"
    pend = G.dget(st, "gates.pending")
    lines = [
        "<graph-engineering>",
        "활성 그래프: %s | 노드 %s [%s] | 브랜치 %s (base %s)"
        % (st["ticket"], st.get("current_node"), st.get("node_status"),
           G.dget(st, "git.branch"), G.dget(st, "git.base_branch")),
        "graphify-out: %s" % gy,
        "빌드: status=%s invoke=%s test=%s fw=%s"
        % (b.get("status"), b.get("invoke"), b.get("test_task"), b.get("test_framework")),
        "재시도: N5→N4 %s/%s, N6→N4 %s/%s"
        % (G.dget(st, "retries.n5_to_n4", 0), G.dget(st, "retries.limits.n5_to_n4", 3),
           G.dget(st, "retries.n6_to_n4", 0), G.dget(st, "retries.limits.n6_to_n4", 2)),
    ]
    if pend:
        lines.append("⏸ 승인 대기: %s (code %s) — 사용자가 `approve`/`승인` 을 입력해야 통과한다. "
                     "모델은 이 게이트를 스스로 통과시킬 수 없다." % (pend.get("gate"), pend.get("code")))
    if st.get("halt_reason"):
        lines.append("■ 정지 사유: %s" % st["halt_reason"])
    if extra:
        lines.append(extra)
    lines.append("상태 조작은 반드시 `.claude/graph/bin/ge ...` 로만 한다.")
    lines.append("</graph-engineering>")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 쓰기 판정 — Edit/Write 경로와 Bash 경로가 **같은 규칙**을 쓴다
# --------------------------------------------------------------------------

def check_protected(paths):
    for rel in paths:
        if any(rel.startswith(p) for p in GRAPH_PROTECTED):
            deny("상태/로그 파일은 직접 편집할 수 없다: %s\n"
                 "  → `.claude/graph/bin/ge put|append|node|record-*` 를 사용하라.\n"
                 "  → 승인 레코드는 어떤 경로로도 모델이 쓸 수 없다. 사용자가 채팅에 입력해야 한다." % rel)


def check_write_targets(st, workable, via="Edit/Write"):
    """브랜치 · GATE-PLAN · TDD Red-First 를 한 곳에서 판정한다.

    workable 은 그래프 내부 작업영역(.claude/graph/tmp)을 제외한 저장소 상대경로 목록이다.
    """
    if not workable:
        return

    # (1) 브랜치 가드 — 티켓 브랜치가 아니면 쓰기 금지
    want = G.dget(st, "git.branch")
    cur = G.current_branch()
    if want and cur and cur != want:
        deny("현재 브랜치가 '%s' 인데 그래프의 티켓 브랜치는 '%s' 다.\n"
             "  → `git switch %s` 로 이동하거나, 그래프를 끝내려면 `/graph-abort`."
             % (cur, want, want))

    # (2) 승인 게이트 — GATE-PLAN 없이는 어떤 소스도 못 쓴다 (N3→N4 엣지)
    ok, why = G.gate_is_approved(st, "GATE-PLAN", G.dget(st, "plan.hash"), require_hash=True)
    if not ok:
        deny("계획 승인(GATE-PLAN) 전에는 소스를 수정할 수 없다. (%s)\n"
             "  경로: %s (%s)\n"
             "  → N3 에서 계획을 `.claude/graph/tmp/` 에 쓰고 `ge gate open --gate GATE-PLAN "
             "--artifact-file <계획파일>` 로 제시한 뒤,\n"
             "    사용자가 채팅에 `approve` 를 입력해야 N4 로 진입한다."
             % (why, ", ".join(workable), via))

    # (3) TDD Red-First
    denied, warned = [], []
    for rel in workable:
        decision, reason = G.tdd_verdict(st, rel)
        if decision == "deny":
            denied.append(reason)
        elif decision == "warn":
            warned.append(reason)
    if denied:
        deny("\n".join(denied))

    # (4) API 문서화 넛지 — 차단하지 않는다. 판정은 스니펫을 실측하는 N5 의 몫이다.
    warned.extend(api_docs_nudges(st, workable))
    # (5) ArchUnit 넛지 — 차단하지 않는다. 판정은 N5 의 몫이다.
    warned.extend(arch_rules_nudges(st, workable))
    if warned:
        warn("\n".join(warned))


def api_docs_nudges(st, workable):
    """컨트롤러를 고치는데 이번 N4 시도에 REST Docs 테스트가 없으면 알려준다.

    여기서 차단하지 않는 이유: 이 시점에는 스니펫이 아직 생성되지 않아 사실 판정이 불가능하다.
    실측은 테스트를 돌린 뒤 `ge apidocs check` 가 하고, 미해소분은 커밋 훅이 막는다.
    """
    if not G.api_docs_applicable(st) or G.dget(st, "current_node") != "N4":
        return []
    att = G.tdd_current_attempt(st) or {}
    if any(G.test_documents_api(w.get("path", "")) for w in att.get("test_writes", []) or []):
        return []
    hits = [r for r in workable if G.is_controller_source(r)]
    if not hits:
        return []
    fw = G.dget(st, "runtime.build.test_framework") or "API"
    if not G.dget(st, "runtime.build.restdocs.available"):
        return ["API 문서화: 컨트롤러 %s 를 수정하는데 이 프로젝트에는 아직 spring-restdocs "
                "의존성이 없다.\n"
                "  → REST Docs 는 조건부가 아니다 — Spring Boot 면 항상 필수다. 이번 티켓에서 "
                "도입부터 한다.\n"
                "  → 도입 방법: .claude/skills/graph-engineering/references/spring-restdocs.md §5\n"
                "  → 도입 후 `ge detect build` 를 다시 돌려 runtime.build 를 갱신하라.\n"
                "  → 커밋 전에 `ge apidocs check` 가 실측하고, 누락이면 커밋이 차단된다."
                % ", ".join(hits)]
    flavor = G.dget(st, "runtime.build.restdocs.flavor") or "mockmvc"
    return ["API 문서화: 컨트롤러 %s 를 수정하는데 이번 시도에 REST Docs 스니펫을 만드는 "
            "테스트가 아직 없다.\n"
            "  → %s 테스트에 document(\"<식별자>\", …) 를 넣어 스니펫을 만들어라 (flavor: %s).\n"
            "  → 엔드포인트는 RESTful 하게 설계한다(자원 명사+복수형 경로, 의미에 맞는 HTTP "
            "메서드, 표준 상태코드) — references/restful-api.md.\n"
            "  → 작성법: .claude/skills/graph-engineering/references/spring-restdocs.md\n"
            "  → 커밋 전에 `ge apidocs check` 가 실측하고, 누락이면 커밋이 차단된다."
            % (", ".join(hits), fw, flavor)]


def arch_rules_nudges(st, workable):
    """레이어를 갖춘 기존 JVM 프로젝트인데 ArchUnit 이 없고 프로덕션 코드가 바뀌면 알려준다.

    ArchUnit 이 이미 도입돼 있으면 여기서 더 할 일이 없다 — 규칙 위반은 일반 테스트
    실행(N5)이 그대로 잡는다. 넛지는 "아직 도입 전인데 프로덕션 코드가 바뀌는" 경우만 본다.
    """
    if not G.arch_rules_applicable(st) or G.dget(st, "current_node") != "N4":
        return []
    if G.dget(st, "runtime.build.archunit.available"):
        return []
    hits = [r for r in workable if G.classify_path(r, st) == "prod"]
    if not hits:
        return []
    att = G.tdd_current_attempt(st) or {}
    if any(G.test_defines_arch_rule(w.get("path", "")) for w in att.get("test_writes", []) or []):
        return []
    return ["아키텍처 규칙: 프로덕션 코드 %s 를 바꾸는데 이 프로젝트에는 아직 ArchUnit "
            "의존성이 없다.\n"
            "  → ArchUnit 도 조건부가 아니다 — 레이어를 갖춘 기존 JVM 프로젝트면 항상 필수다. "
            "이번 티켓에서 도입부터 한다.\n"
            "  → 도입 방법: .claude/skills/graph-engineering/references/archunit.md §4\n"
            "  → 도입 후 `ge detect build` 를 다시 돌려 runtime.build 를 갱신하라.\n"
            "  → 커밋 전에 `ge archunit check` 가 실측하고, 누락이면 커밋이 차단된다."
            % ", ".join(hits)]


# --------------------------------------------------------------------------
# PreToolUse : Edit / Write / MultiEdit / NotebookEdit
# --------------------------------------------------------------------------

def guard_write(data):
    st = require_active()
    paths = tool_paths(data)
    if not paths:
        allow()
    check_protected(paths)
    workable = [p for p in paths
                if not any(p.startswith(w) for w in GRAPH_INTERNAL_WRITABLE)]
    check_write_targets(st, workable, via="Edit/Write")
    allow()


# --------------------------------------------------------------------------
# PostToolUse : 쓰기 사실을 TDD 원장에 기록
# --------------------------------------------------------------------------

def record_write(data):
    st = require_active()
    if G.dget(st, "current_node") not in ("N4",):
        allow()

    if (data.get("tool_name") or "") == "Bash":
        paths = bash_write_targets((data.get("tool_input") or {}).get("command") or "",
                                   data.get("cwd"))
    else:
        paths = tool_paths(data)
    paths = [p for p in paths
             if not any(p.startswith(x) for x in GRAPH_PROTECTED + GRAPH_INTERNAL_WRITABLE)]
    if not paths:
        allow()

    # 원장 갱신은 다른 ge 프로세스와 경합할 수 있다 — 락 안에서 다시 읽는다
    with G.state_lock(st.get("ticket")):
        st = G.load(st["ticket"]) or st
        changed = False
        att = G.tdd_current_attempt(st, create=True)
        for rel in paths:
            kind = G.classify_path(rel, st)
            key = {"test": "test_writes", "prod": "prod_writes"}.get(kind)
            if not key:
                continue
            att.setdefault(key, []).append({"path": rel, "at": G.now()})
            changed = True
        if changed:
            G.save(st)
    allow()


# --------------------------------------------------------------------------
# Bash 명령 파싱 — 셸로도 규칙을 우회할 수 없게 한다
# --------------------------------------------------------------------------

_SEGMENT_SPLIT = re.compile(r"(?:&&|\|\||;|\n|\|)")
_MUTATORS = (">", "tee ", "sed -i", "rm ", "mv ", "cp ", "truncate", "dd ", "chmod ",
             "python", "perl")

# 소스 파일을 만들어내는 흔한 셸 경로
_INPLACE_EDITORS = ("sed", "perl", "ruby", "gawk", "ex")
_FILE_MOVERS = ("cp", "mv", "install", "ln", "touch", "truncate", "rm", "shred")
# git 자체 옵션 중 값을 따로 받는 것들 (`git -C <dir> commit` 우회를 막는다)
_GIT_VALUE_OPTS = {"-C", "-c", "--exec-path", "--git-dir", "--work-tree",
                   "--namespace", "--super-prefix", "--config-env", "--attr-source"}
# git 토큰이 섞여 있어도 실제 git 실행이 아닌 것이 분명한 명령들
_READONLY_HEADS = {"grep", "rg", "egrep", "fgrep", "cat", "echo", "printf", "ls", "find",
                   "head", "tail", "awk", "jq", "wc", "less", "more", "diff", "column",
                   "sort", "uniq", "basename", "dirname", "test", "["}
_SRC_EXT_RE = re.compile(r"\.[A-Za-z0-9_+-]{1,8}$")


def looks_mutating(cmd: str) -> bool:
    """상태 경로를 건드리는 명령이 '쓰기'인지 판정한다.
    `cat ... | jq .` 같은 순수 읽기는 통과시키고, 리다이렉션/치환/삭제만 잡는다."""
    probe = cmd
    for noise in ("2>&1", "2>/dev/null", "2>&-", "&>/dev/null", "1>&2"):
        probe = probe.replace(noise, " ")
    return any(m in probe for m in _MUTATORS)


def bash_segments(cmd):
    return [s.strip() for s in _SEGMENT_SPLIT.split(cmd or "") if s.strip()]


def tokenize(seg):
    try:
        return shlex.split(seg, posix=True)
    except ValueError:
        return seg.split()


def strip_prefix_tokens(toks):
    """`sudo` / `env FOO=1` / `xargs -I{}` 같은 접두를 걷어내고 실제 명령 토큰을 돌려준다."""
    i = 0
    while i < len(toks):
        t = toks[i]
        if t in ("sudo", "command", "nohup", "time", "exec", "stdbuf", "setsid"):
            i += 1
        elif t == "env":
            i += 1
            while i < len(toks) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[i]):
                i += 1
        elif re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t):
            i += 1
        elif t in ("xargs", "timeout"):
            i += 1
            while i < len(toks) and toks[i].startswith("-"):
                i += 1
            if t == "timeout" and i < len(toks) and re.match(r"^[0-9.]+[smhd]?$", toks[i]):
                i += 1
        else:
            break
    return toks[i:]


def is_git_token(t) -> bool:
    t = t.strip("()\"'`$;{}")
    return t == "git" or t.endswith("/git")


def git_invocation(toks):
    """확신 있게 파싱된 git 실행이면 (subcommand, tokens) 를, 아니면 None 을 돌려준다.

    `git -C . commit` 처럼 git 자체 옵션이 앞에 붙어도 서브커맨드를 정확히 찾는다.
    (예전 정규식 `^git\\s+(-\\S+\\s+)*commit` 은 `-C .` 의 `.` 에서 매칭이 깨졌다.)
    """
    if not toks or not is_git_token(toks[0]):
        return None
    i = 1
    while i < len(toks):
        t = toks[i]
        if t.startswith("-"):
            base = t.split("=", 1)[0]
            i += 2 if (base in _GIT_VALUE_OPTS and "=" not in t) else 1
            continue
        return t, toks
    return None


# --- 셸이 실제로 쓰는 파일 경로 추출 -------------------------------------------

def _resolve(root, vcwd, raw):
    """셸 토큰 하나를 저장소 상대경로로 푼다. 저장소 밖이거나 못 풀면 None."""
    if not raw:
        return None
    raw = raw.strip()
    if not raw or raw.startswith("&") or raw.startswith("-"):
        return None
    if raw.startswith("~") or "$" in raw or "*" in raw or "?" in raw:
        return None            # 확장 결과를 알 수 없다 — 판정하지 않는다
    p = raw if os.path.isabs(raw) else os.path.join(vcwd, raw)
    p = os.path.normpath(p)
    if p != root and not p.startswith(root + os.sep):
        return None            # 저장소 밖 — 그래프의 관심사가 아니다
    rel = os.path.relpath(p, root).replace(os.sep, "/")
    return None if rel in (".", "..") else rel


def _pathlike(root, vcwd, raw):
    """cp/mv/sed -i 등의 인자 중 '파일로 보이는 것'만 고른다(정규식 스크립트 오탐 방지)."""
    rel = _resolve(root, vcwd, raw)
    if rel is None:
        return None
    if os.path.exists(os.path.join(root, rel)):
        return rel
    return rel if _SRC_EXT_RE.search(os.path.basename(rel)) else None


def bash_write_targets(cmd: str, cwd=None):
    """bash 명령이 **쓰는** 저장소 내 파일 경로를 뽑는다.

    커버: 리다이렉션(`>`, `>>`, `>|`, `N>`), `tee`, in-place 편집(`sed -i` 등),
          `cp`/`mv`/`install`/`ln`/`touch`/`truncate`/`rm`, `dd of=`.
    세그먼트를 순서대로 훑으며 `cd` 로 바뀐 작업 디렉터리를 추적한다
    (`cd .claude/graph && echo x > state/…` 우회를 막는다).

    미커버(잔여 위험): 인터프리터 인라인 코드(`python3 -c "open(...,'w')"`).
    파싱으로 잡을 수 없어 판정하지 않는다 — `references/hooks.md` 에 명시돼 있다.
    """
    root = os.path.normpath(G.project_dir())
    base = os.path.normpath(cwd) if cwd and os.path.isabs(str(cwd)) else root
    if base != root and not base.startswith(root + os.sep):
        base = root
    vcwd = base
    found = []

    def add(rel):
        if rel and rel not in found:
            found.append(rel)

    for seg in bash_segments(cmd):
        toks = tokenize(seg)
        if not toks:
            continue

        # `cd X` 는 이후 세그먼트의 상대경로 기준을 바꾼다
        if toks[0] == "cd":
            if len(toks) == 1 or toks[1] == "-":
                vcwd = base
            else:
                nxt = toks[1]
                if "$" not in nxt and "*" not in nxt:
                    cand = os.path.normpath(
                        nxt if os.path.isabs(nxt) else os.path.join(vcwd, nxt))
                    vcwd = cand if (cand == root or cand.startswith(root + os.sep)) else cand
            continue

        # (1) 리다이렉션 — 새 파일도 대상이다
        i = 0
        while i < len(toks):
            t = toks[i]
            m = re.match(r"^\d?>{1,2}\|?$", t)
            if m and i + 1 < len(toks):
                add(_resolve(root, vcwd, toks[i + 1]))
                i += 2
                continue
            m = re.match(r"^\d?>{1,2}\|?(?P<t>[^>].*)$", t)
            if m:
                add(_resolve(root, vcwd, m.group("t")))
            i += 1

        real = strip_prefix_tokens(toks)
        if not real:
            continue
        head = os.path.basename(real[0])
        args = [a for a in real[1:] if not re.match(r"^\d?>{1,2}", a)]

        # (2) tee — 파이프의 오른쪽에서 파일을 쓴다
        if head == "tee":
            for a in args:
                if not a.startswith("-"):
                    add(_resolve(root, vcwd, a))

        # (3) in-place 편집 — 인자 중 '파일로 보이는 것'만
        elif head in _INPLACE_EDITORS and any(
                a == "-i" or a.startswith("-i") or a == "--in-place" or
                (a.startswith("-") and not a.startswith("--") and "i" in a[1:])
                for a in args):
            for a in args:
                if not a.startswith("-"):
                    add(_pathlike(root, vcwd, a))

        # (4) 파일 생성/이동/삭제
        elif head in _FILE_MOVERS:
            for a in args:
                if not a.startswith("-"):
                    add(_pathlike(root, vcwd, a))

        # (5) dd of=
        elif head == "dd":
            for a in args:
                if a.startswith("of="):
                    add(_resolve(root, vcwd, a[3:]))

    return found


# --------------------------------------------------------------------------
# PreToolUse : Bash
# --------------------------------------------------------------------------

def extract_commit_message(toks):
    """git commit 토큰에서 최종 커밋 메시지를 뽑는다. (None, 사유) 도 가능."""
    msgs, i = [], 0
    while i < len(toks):
        t = toks[i]
        if t == "-m" or t == "--message":
            if i + 1 < len(toks):
                msgs.append(toks[i + 1])
                i += 2
                continue
        elif t.startswith("--message="):
            msgs.append(t.split("=", 1)[1])
        elif t.startswith("-m") and len(t) > 2:
            msgs.append(t[2:])
        elif t in ("-F", "--file"):
            if i + 1 < len(toks):
                try:
                    with open(toks[i + 1], encoding="utf-8") as fh:
                        msgs.append(fh.read())
                except Exception:
                    return None, "커밋 메시지 파일을 읽을 수 없다: %s" % toks[i + 1]
                i += 2
                continue
        elif t.startswith("--file="):
            try:
                with open(t.split("=", 1)[1], encoding="utf-8") as fh:
                    msgs.append(fh.read())
            except Exception:
                return None, "커밋 메시지 파일을 읽을 수 없다."
        i += 1
    if not msgs:
        if "--amend" in toks and "--no-edit" in toks:
            return None, "amend --no-edit 는 승인된 메시지와 대조할 수 없다. `-m` 으로 메시지를 명시하라."
        return None, ("커밋 메시지를 명령에서 찾을 수 없다(에디터 모드로 보인다).\n"
                      "  → 승인된 메시지와 해시를 대조해야 하므로 반드시 `git commit -m \"...\"` 형태로 실행하라.")
    return "\n\n".join(m for m in msgs), None


# --------------------------------------------------------------------------
# PreToolUse : JIRA 티켓 본문 쓰기 (mcp__atlassian__editJiraIssue)
# --------------------------------------------------------------------------

# 티켓 본문을 고쳐도 되는 노드. 그 밖의 노드에서는 티켓을 쓸 이유가 없다.
JIRA_WRITE_NODES = {
    "AMEND": "요구사항 수정 — GATE-TICKET 승인 필요",
    "N8": "완료 조건 체크박스 갱신",
    "N9": "요약 댓글",
}


def guard_jira(data):
    """티켓 본문을 바꾸는 것은 **외부 시스템에 대한 되돌리기 어려운 쓰기**다.

    AMEND 에서는 GATE-TICKET 승인이 제안 본문 해시에 묶여 있어야만 통과한다.
    모델이 스스로 요구사항을 고쳐놓고 "티켓에 그렇게 적혀 있다" 고 말하는 경로를 없앤다.
    """
    st = require_active()
    node = G.dget(st, "current_node")

    if node not in JIRA_WRITE_NODES:
        deny("JIRA 티켓 본문은 %s 노드에서만 수정할 수 있다. 현재 노드: %s\n"
             "  → 요구사항이 틀렸다면 게이트에서 사용자가 반려한 뒤 AMEND 로 들어가야 한다.\n"
             "  → 모델이 임의로 티켓을 고쳐 요구사항을 만들어낼 수 없다."
             % ("/".join(sorted(JIRA_WRITE_NODES)), node))

    if node != "AMEND":
        allow()          # N8/N9 의 체크박스·댓글 갱신은 기존 동작 그대로

    ok, why = G.amend_write_allowed(st)
    if not ok:
        deny("티켓 수정 차단: %s\n"
             "  → 절차: `ge amend propose --file <제안본문> --from-gate <게이트> --reason \"<반려사유>\"`\n"
             "          `ge gate open --gate GATE-TICKET --artifact-file <같은 파일>`\n"
             "          사용자가 채팅에 `approve` 를 입력해야 JIRA 를 고칠 수 있다.\n"
             "  → 승인은 **제안 본문 해시에 묶인다.** 승인 후 본문을 고치면 다시 막힌다." % why)
    allow()


def guard_bash(data):
    st = require_active()
    cmd = (data.get("tool_input") or {}).get("command") or ""
    if not cmd.strip():
        allow()

    low = cmd

    # (1) 승인 위조 경로 차단
    if "GE_HOOK_AUTH" in low:
        deny("GE_HOOK_AUTH 는 UserPromptSubmit 훅 전용이다. 모델이 승인을 발화할 수 없다.")
    if re.search(r"\b(ge|ge_state\.py)\b[^\n]*\bapprove\b", low) or "ge approve" in low:
        deny("`ge approve` 는 훅 전용 서브커맨드다. 승인은 사용자가 채팅에 `approve` 를 입력할 때만 기록된다.")

    # (2) 상태 파일 조작 차단 — 문자열 백스톱(인터프리터 인라인 코드까지 잡는다)
    if ".claude/graph/state" in low or ".claude/graph/logs" in low:
        if looks_mutating(low):
            deny("상태/로그 파일을 셸로 변경할 수 없다.\n"
                 "  → 읽기는 `ge show`, 쓰기는 `ge put|append|node|record-*` 만 허용된다.")

    # (3) 셸이 실제로 쓰는 파일에 Edit/Write 와 **동일한** 규칙을 적용한다
    targets = bash_write_targets(cmd, data.get("cwd"))
    check_protected(targets)          # cd 로 우회한 상태 파일 쓰기도 여기서 걸린다
    workable = [p for p in targets
                if not any(p.startswith(w) for w in GRAPH_INTERNAL_WRITABLE)]
    check_write_targets(st, workable, via="Bash")

    # (4) git 실행 판정 — 토큰 기반
    cfg = G.config()
    want = G.dget(st, "git.branch")
    for seg in bash_segments(cmd):
        toks = strip_prefix_tokens(tokenize(seg))
        if not toks:
            continue
        inv = git_invocation(toks)
        if inv is None:
            # fail-closed: git 토큰과 commit/push 가 같은 세그먼트에 있는데
            # 실행 형태를 확신할 수 없으면(`$(echo git) commit`, `xargs git commit` 등) 막는다.
            # 명령 이름 자체가 치환/서브셸이면 읽기 전용으로 신뢰하지 않는다
            # (`$(echo git) commit` 의 head 를 'echo' 로 오인해 통과시키던 구멍)
            head_raw = toks[0]
            clean_head = not any(c in head_raw for c in "$`()")
            head = os.path.basename(head_raw) if clean_head else ""
            if (not clean_head or head not in _READONLY_HEADS) \
                    and any(is_git_token(t) for t in toks) \
                    and ({"commit", "push"} & set(toks)):
                deny("git 명령의 실행 형태를 확신 있게 파싱할 수 없어 차단한다: %r\n"
                     "  → 커밋은 반드시 `git commit -m \"...\"` 단순 형태로 실행하라.\n"
                     "    (승인된 메시지 해시와 대조해야 하므로 우회 형태는 허용되지 않는다.)" % seg[:200])
            continue

        sub, gtoks = inv
        if sub == "push" and not G.dget(cfg, "git.allow_push", False):
            deny("이 그래프는 푸시를 하지 않는다. 사용자가 명시적으로 요청할 때만 사람이 직접 수행한다.")
        if sub == "branch" and want and any(f in gtoks for f in ("-D", "-d", "--delete")) \
                and want in gtoks:
            deny("진행 중인 티켓 브랜치 '%s' 는 삭제할 수 없다. 먼저 `/graph-abort`." % want)
        if sub == "commit":
            return guard_commit(st, gtoks)

    # (5) gh pr create 금지
    for seg in bash_segments(cmd):
        toks = strip_prefix_tokens(tokenize(seg))
        if toks and os.path.basename(toks[0]) == "gh" and not G.dget(cfg, "git.allow_pr", False):
            rest = [t for t in toks[1:] if not t.startswith("-")]
            if rest[:2] == ["pr", "create"]:
                deny("이 그래프는 PR 생성을 하지 않는다.")

    allow()


def unresolved_tdd_violations(st):
    """원장에서 **실제로 일어난** Red-First 위반을 계산한다.

    예전 구현은 `tdd.attempts[].violations` 배열을 읽었는데 그 배열을 채우는 코드가
    저장소 어디에도 없어서 검사가 항상 통과했다. 원장 자체로부터 유도한다:
    테스트를 한 번도 쓰지 않은 시도에서 프로덕션 코드가 쓰였고 면제도 없으면 위반이다.
    """
    out = []
    for att in G.dget(st, "tdd.attempts", []) or []:
        if att.get("test_writes"):
            continue
        exempt = {e.get("path") for e in att.get("exemptions", []) or []}
        for w in att.get("prod_writes", []) or []:
            if w.get("path") not in exempt:
                out.append({"attempt": att.get("attempt"), "path": w.get("path"),
                            "at": w.get("at")})
        for v in att.get("violations", []) or []:   # 레거시 필드도 존중한다
            if v.get("path") not in exempt:
                out.append(v)
    return out


def guard_commit(st, gtoks):
    cfg = G.config()
    ticket = st["ticket"]

    cur = G.current_branch()
    want = G.dget(st, "git.branch")
    if want and cur and cur != want:
        deny("커밋 차단: 현재 브랜치 '%s' 가 티켓 브랜치 '%s' 가 아니다." % (cur, want))

    msg, err = extract_commit_message(gtoks)
    if err:
        deny("커밋 차단: " + err)

    # 5-0. N10 graphify 자동 커밋 — GATE 없이 통과한다.
    # (a) 지금 노드가 설정된 노드(기본 N10)이고, (b) 메시지가 설정된 고정 문구와
    # **정확히** 같고, (c) 스테이징된 파일이 전부 설정된 경로 접두사 아래일 때만 적용된다.
    # 셋 중 하나라도 어긋나면 일반 커밋 규칙(컨벤션·GATE-REVIEW·GATE-COMMIT)으로 떨어진다 —
    # 이 예외가 리뷰·승인을 우회하는 일반 통로가 되지 않게 하기 위해서다.
    gcfg = G.dget(cfg, "commit.graphify", {}) or {}
    gmsg = gcfg.get("message")
    gnode = gcfg.get("node", "N10")
    gprefix = gcfg.get("path_prefix", "graphify-out/")
    if gmsg and G.dget(st, "current_node") == gnode and G.normalize_artifact(msg) == gmsg:
        rc, out, _ = G.git("diff", "--cached", "--name-only")
        staged = [l for l in out.splitlines() if l.strip()]
        if rc == 0 and staged and all(p.startswith(gprefix) for p in staged):
            allow()
        deny("커밋 차단: %s 의 graphify 자동 커밋 예외는 스테이징된 변경이 전부 '%s' "
             "아래일 때만 적용된다.\n  스테이징된 파일: %s"
             % (gnode, gprefix, ", ".join(staged[:10]) or "(없음)"))

    # 5-1. 컨벤셔널 커밋 형식
    pattern = G.dget(cfg, "commit.pattern")
    subject = G.normalize_artifact(msg).split("\n", 1)[0]
    m = re.match(pattern, subject) if pattern else None
    if not m:
        deny("커밋 차단: 메시지가 컨벤션에 맞지 않다.\n"
             "  형식: type(scope:JIRA ticket):subject\n"
             "  허용 type: %s\n"
             "  예: feat(%s): 코드 일관성 수정\n"
             "  받은 제목: %r"
             % (", ".join(G.dget(cfg, "commit.allowed_types", [])), ticket, subject))
    scope = m.group(2)
    if scope != ticket:
        deny("커밋 차단: scope 가 '%s' 인데 진행 중인 티켓은 '%s' 다. scope 에는 티켓 키가 들어가야 한다."
             % (scope, ticket))
    maxlen = G.dget(cfg, "commit.subject_max_len", 100)
    if len(subject) > maxlen:
        deny("커밋 차단: 제목이 %d자를 넘는다(%d자)." % (maxlen, len(subject)))

    # 5-2. 리뷰 게이트
    ok, why = G.gate_is_approved(st, "GATE-REVIEW")
    if not ok:
        deny("커밋 차단: 코드리뷰 승인이 없다. (%s)" % why)

    # 5-3. 커밋 게이트 — 실제 메시지를 재해싱해 승인 대상과 대조
    ok, why = G.gate_is_approved(st, "GATE-COMMIT", G.gate_hash(msg))
    if not ok:
        deny("커밋 차단: %s\n"
             "  → 커밋할 메시지를 `.claude/graph/tmp/%s-commit.txt` 에 쓰고\n"
             "    `ge gate open --gate GATE-COMMIT --artifact-file <그 파일>` 로 제시한 뒤\n"
             "    사용자가 `approve` 를 입력해야 커밋할 수 있다." % (why, ticket))

    # 5-4. API 문서화 미해소 (Spring Boot 면 항상 발동 — restdocs 도입 여부와 무관, 조건부 아님)
    if G.api_docs_applicable(st):
        checked = G.dget(st, "api_docs.checked_at")
        undoc = G.unresolved_api_docs(st)
        if undoc and G.dget(G.config(), "api_docs.mode", "block") == "block":
            deny("커밋 차단: 변경된 컨트롤러 %d개에 API 문서(REST Docs 스니펫)가 없다.\n"
                 "  %s\n"
                 "  → 해당 엔드포인트의 테스트에 document(...) 를 추가해 스니펫을 만들어라.\n"
                 "  → 문서화 대상이 아니면: ge apidocs exempt --path <경로> --reason \"<사유>\""
                 % (len(undoc), "\n  ".join(undoc)))
        if not checked:
            deny("커밋 차단: 이 프로젝트는 Spring Boot 인데(REST Docs 문서화 필수) API 문서화 검사를 "
                 "아직 돌리지 않았다.\n"
                 "  → `.claude/graph/bin/ge apidocs check` 를 먼저 실행하라 (N5 에서 테스트 실행 뒤).\n"
                 "  → spring-restdocs 의존성이 아직 없다면 먼저 도입하라: "
                 "references/spring-restdocs.md §5.")

    # 5-4b. 아키텍처 규칙(ArchUnit) 미해소 (레이어를 갖춘 기존 JVM 프로젝트면 항상 발동)
    if G.arch_rules_applicable(st):
        checked = G.dget(st, "arch_rules.checked_at")
        if G.unresolved_arch_rules(st) and G.dget(G.config(), "arch_rules.mode", "block") == "block":
            deny("커밋 차단: 아키텍처 규칙(ArchUnit)이 아직 도입되지 않았는데 프로덕션 코드가 "
                 "바뀌었다.\n"
                 "  변경된 프로덕션 파일: %s\n"
                 "  → 규칙 테스트를 추가해 ArchUnit 을 도입하라.\n"
                 "  → 도입 대상이 아니면: ge archunit exempt --reason \"<사유>\""
                 % ", ".join(G.dget(st, "arch_rules.prod_changed", []) or []))
        if not checked:
            deny("커밋 차단: 이 프로젝트는 레이어를 갖춘 기존 JVM 프로젝트인데(ArchUnit 필수) "
                 "아키텍처 규칙 검사를 아직 돌리지 않았다.\n"
                 "  → `.claude/graph/bin/ge archunit check` 를 먼저 실행하라 (N5 에서 테스트 실행 뒤).\n"
                 "  → ArchUnit 의존성이 아직 없다면 먼저 도입하라: references/archunit.md §4.")

    # 5-5. TDD 미해소 위반
    unresolved = unresolved_tdd_violations(st)
    if unresolved:
        deny("커밋 차단: 테스트를 먼저 쓰지 않고 프로덕션 코드를 쓴 기록이 %d건 남아 있다.\n"
             "  %s\n"
             "  → 해당 코드를 덮는 테스트를 쓰거나, 테스트 대상이 아니면\n"
             "    `ge tdd exempt --path <경로> --reason \"<사유>\"` 로 사유를 남겨라(리뷰에 노출된다)."
             % (len(unresolved), json.dumps(unresolved, ensure_ascii=False)[:500]))

    allow()


# --------------------------------------------------------------------------
# UserPromptSubmit : 사용자의 실제 입력에서만 승인이 발화한다
# --------------------------------------------------------------------------

_APPROVE_RE = re.compile(
    r"^\s*(?:/graph-approve|approved?|승인(?:함|합니다|해|할게|요)?|ok[,\s]+approve|lgtm)\b\s*(?P<code>[0-9a-f]{6})?\s*(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL)
_REJECT_RE = re.compile(
    r"^\s*(?:/graph-reject|reject(?:ed)?|반려|거부|nack)\b\s*(?P<code>[0-9a-f]{6})?\s*(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL)


def approval(data):
    st = require_active()
    prompt = data.get("prompt") or ""
    pending = G.dget(st, "gates.pending")

    decision = None
    mo = _APPROVE_RE.match(prompt)
    if mo:
        decision = "approved"
    else:
        mo = _REJECT_RE.match(prompt)
        if mo:
            decision = "rejected"

    note = ""
    if decision and pending:
        code = (mo.group("code") or "").lower() or None
        if code and code != pending.get("code"):
            note = ("사용자가 코드 %s 로 %s 를 시도했으나 대기 중인 게이트 코드는 %s 다. "
                    "승인은 기록되지 않았다. 올바른 코드를 다시 제시하라."
                    % (code, decision, pending.get("code")))
        else:
            os.environ["GE_HOOK_AUTH"] = "1"
            import argparse as _ap
            try:
                with G.state_lock(st["ticket"]):
                    G.cmd_approve(_ap.Namespace(
                        ticket=st["ticket"], decision=decision, source="hook:UserPromptSubmit",
                        code=code, comment=(mo.group("rest") or "").strip()[:500], raw=prompt))
            except SystemExit as e:
                if e.code not in (0, None):
                    note = "승인 기록 실패(코드 %s)." % e.code
            if not note:
                st = G.load(st["ticket"]) or st
                if decision == "approved":
                    nxt = {"GATE-PLAN": "N4(개발) 로 진입하라.",
                           "GATE-REVIEW": "N7(커밋 메시지 작성) 으로 진행하라.",
                           "GATE-COMMIT": "커밋을 실행하라.",
                           "GATE-TICKET": "승인된 본문 그대로 JIRA 티켓을 수정하고 "
                                          "`ge amend applied` 로 기록한 뒤, 요구사항이 바뀌었으므로 "
                                          "N2 로 되돌아가 티켓을 다시 파싱하라."}.get(pending["gate"], "")
                    note = "✅ %s 승인됨 (사용자 직접 입력, code %s). %s" % (
                        pending["gate"], pending["code"], nxt)
                else:
                    note = ("⛔ %s 반려됨. 사유: %s\n"
                            "반려 사유를 **분류**하라:\n"
                            "  (A) 산출물 품질 문제(계획/리뷰/커밋메시지가 부실·오류) → "
                            "해당 노드를 다시 수행한다. 티켓은 건드리지 않는다.\n"
                            "  (B) **요구사항 불일치**(완료 조건 누락·오기, 범위/제약이 실제와 다름, "
                            "티켓이 잘못 적혀 있음) → AMEND 노드로 간다. "
                            "`references/amend.md` 를 읽고 `ge-ticket-amender` 를 호출하라.\n"
                            "  애매하면 **지어내지 말고 사용자에게 어느 쪽인지 물어라.**\n"
                            "승인 없이 다음 노드로 넘어갈 수 없다."
                            % (pending["gate"], (mo.group("rest") or "(사유 없음)").strip()))
    elif decision and not pending:
        note = "승인/반려 표현이 감지됐지만 대기 중인 게이트가 없다. 아무 것도 기록하지 않았다."

    sys.stdout.write(header(st, note) + "\n")
    sys.exit(0)


# --------------------------------------------------------------------------
# SessionStart : 활성 그래프 컨텍스트 주입
# --------------------------------------------------------------------------

def session_context(data):
    st = require_active()
    sys.stdout.write(header(
        st, "세션이 새로 시작됐다. 이어가려면 `/graph-resume %s`, 끝내려면 `/graph-abort`." % st["ticket"]) + "\n")
    sys.exit(0)


# --------------------------------------------------------------------------
# Stop / SubagentStop : 결정론적 체크포인트
# --------------------------------------------------------------------------

def checkpoint(data):
    st = require_active()
    try:
        with G.state_lock(st.get("ticket")):
            st = G.load(st["ticket"]) or st
            G.log_event(st, st.get("current_node"), "checkpoint",
                        data.get("hook_event_name") or "stop")
            G.save(st)
    except Exception:
        pass
    sys.exit(0)


DISPATCH = {
    "guard-write": guard_write,
    "record-write": record_write,
    "guard-bash": guard_bash,
    "guard-jira": guard_jira,
    "approval": approval,
    "session-context": session_context,
    "checkpoint": checkpoint,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in DISPATCH:
        sys.exit(0)
    data = read_input()
    DISPATCH[sys.argv[1]](data)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — 훅은 절대 세션을 깨지 않는다
        sys.stderr.write("[ge-hooks] 내부 오류(통과 처리): %s: %s\n" % (type(exc).__name__, exc))
        sys.exit(0)
