#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""JIRA Ticket — 티켓 생성 가드와 승인 훅.

절대 원칙 (Graph Engineering 훅과 동일한 규약)
  1. 초안이 없으면 **무조건 즉시 통과**한다. 이 스킬을 쓰지 않는 평소 작업은 영향이 없다.
  2. 내부 오류는 통과(exit 0)로 처리한다. 훅이 세션을 망가뜨리지 않는다.
  3. 차단은 명시적으로 위반이 판정됐을 때만 한다.
"""

from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "jira", "lib"))
try:
    import jt_ticket as T
except Exception:
    sys.exit(0)


def read_input():
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def deny(reason: str):
    sys.stderr.write("[JIRA Ticket · 차단]\n" + reason.rstrip() + "\n")
    sys.exit(2)


def allow():
    sys.exit(0)


# --------------------------------------------------------------------------
# PreToolUse : mcp__atlassian__createJiraIssue
# --------------------------------------------------------------------------

def guard_create(data):
    """티켓 생성은 되돌리기 어려운 외부 쓰기다.

    양식 검증을 통과한 초안이 있고, 그 초안 해시에 묶인 사용자 승인이 있을 때만 통과한다.
    모델이 양식을 건너뛰고 임의의 티켓을 만들어내는 경로를 없앤다.

    **초안이 없어도 이 훅은 돈다.** 초안이 없을 때 통과시키면 스킬을 건너뛰는 것만으로
    양식 강제를 무력화할 수 있다.
    """
    if not T.dget(T.config(), "ticket.enforce_on_create", True):
        allow()
    d = T.load_draft()
    if not d:
        deny("등록된 티켓 초안이 없다. 임의로 티켓을 만들 수 없다.\n"
             "  → 절차: 초안을 `.claude/jira/drafts/` 밖의 파일에 쓰고\n"
             "          `.claude/jira/bin/jt draft --file <초안.md>` 로 양식 검증 + 등록,\n"
             "          사용자에게 전문을 제시하고 `approve` 를 받아야 생성할 수 있다.\n"
             "  → 양식: `.claude/jira/bin/jt template`")

    ok, why = T.create_allowed(d)
    if not ok:
        deny("티켓 생성 차단: %s" % why)

    # 승인된 요약과 실제로 만들려는 요약이 같은지 대조한다
    ti = data.get("tool_input") or {}
    got = (ti.get("summary") or "").strip()
    want = (d.get("summary") or "").strip()
    if got and T.normalize(got) != T.normalize(want):
        deny("티켓 생성 차단: 승인된 요약과 다른 요약으로 만들려 한다.\n"
             "  승인된: %r\n  요청한: %r\n"
             "  → 승인된 초안 그대로 만들거나, 초안을 고쳐 다시 승인받아라." % (want, got))
    allow()


# --------------------------------------------------------------------------
# PreToolUse : 초안 상태 파일 보호
# --------------------------------------------------------------------------

DRAFTS_REL = ".claude/jira/drafts/"

# 셸 쓰기 대상 추출은 Graph Engineering 훅이 이미 구현해 뒀다(159개 회귀로 검증됨).
# 문자열이 명령 어딘가에 나타나는지로 판정하면 문서·주석에 경로가 언급되기만 해도 막힌다.
try:
    import ge_hooks as _GH             # noqa: E402
except SystemExit:                     # ge_state 가 없으면 모듈이 sys.exit(0) 한다
    _GH = None
except Exception:
    _GH = None


def _rel(path, root):
    """저장소 루트 기준 상대경로. cwd 로 풀어 밖으로 나가면 루트 기준으로 다시 푼다."""
    if not path:
        return ""
    root = os.path.normpath(root)
    if os.path.isabs(path):
        p = os.path.normpath(path)
    else:
        cand = os.path.normpath(os.path.join(os.getcwd(), path))
        inside = cand == root or cand.startswith(root + os.sep)
        p = cand if inside else os.path.normpath(os.path.join(root, path))
    try:
        return os.path.relpath(p, root).replace(os.sep, "/")
    except ValueError:
        return path


def guard_write(data):
    """`.claude/jira/drafts/` 는 `jt` 로만 쓴다.

    이걸 열어두면 승인 레코드를 파일에 직접 써넣어 "사용자만 승인할 수 있다" 가 무너진다.
    잔여 위험: 인터프리터 인라인 코드는 문자열 백스톱으로만 잡는다(Graph 훅과 동일한 한계).
    """
    root = T.project_dir()
    ti = data.get("tool_input") or {}
    tool = data.get("tool_name") or ""

    if tool == "Bash":
        cmd = ti.get("command") or ""
        if _GH is None:
            allow()                    # 파서가 없으면 오탐을 내느니 통과시킨다
        try:
            hits = [t for t in _GH.bash_write_targets(cmd, data.get("cwd"))
                    if t.startswith(DRAFTS_REL)]
        except Exception:
            hits = []
        if hits:
            deny("티켓 초안 상태는 셸로 변경할 수 없다: %s\n"
                 "  → 읽기는 `jt show`, 쓰기는 `jt draft|created|clear` 만 허용된다.\n"
                 "  → 승인 레코드는 어떤 경로로도 모델이 쓸 수 없다. 사용자가 채팅에 입력해야 한다."
                 % ", ".join(hits))
        allow()

    paths = []
    for key in ("file_path", "notebook_path", "path"):
        v = ti.get(key)
        if isinstance(v, str) and v:
            paths.append(v)
    for e in ti.get("edits") or []:
        if isinstance(e, dict) and isinstance(e.get("file_path"), str):
            paths.append(e["file_path"])
    for raw in paths:
        if _rel(raw, root).startswith(DRAFTS_REL):
            deny("티켓 초안 상태 파일은 직접 편집할 수 없다: %s\n"
                 "  → `jt draft|created|clear` 를 사용하라.\n"
                 "  → 승인 레코드는 사용자가 채팅에 `approve` 를 입력해야만 생긴다." % _rel(raw, root))
    allow()


# --------------------------------------------------------------------------
# UserPromptSubmit : 사용자의 실제 입력에서만 승인이 발화한다
# --------------------------------------------------------------------------

_EXPLICIT_APPROVE = re.compile(r"^\s*/jira-approve\b\s*(?P<code>[0-9a-f]{6})?\s*(?P<rest>.*)$",
                               re.IGNORECASE | re.DOTALL)
_EXPLICIT_REJECT = re.compile(r"^\s*/jira-reject\b\s*(?P<code>[0-9a-f]{6})?\s*(?P<rest>.*)$",
                              re.IGNORECASE | re.DOTALL)
_APPROVE = re.compile(
    r"^\s*(?:approved?|승인(?:함|합니다|해|할게|요)?|ok[,\s]+approve|lgtm)\b"
    r"\s*(?P<code>[0-9a-f]{6})?\s*(?P<rest>.*)$", re.IGNORECASE | re.DOTALL)
_REJECT = re.compile(
    r"^\s*(?:reject(?:ed)?|반려|거부|nack)\b\s*(?P<code>[0-9a-f]{6})?\s*(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL)


def graph_gate_pending() -> bool:
    """Graph Engineering 이 승인을 기다리는 중인가.

    같은 `approve` 한 마디를 두 시스템이 동시에 소비하면 안 된다. 그래프가 대기 중이면
    일반 승인어는 그래프 몫으로 두고, 티켓은 `/jira-approve` 로만 승인받는다.
    """
    try:
        root = T.project_dir()
        ptr = os.path.join(root, ".claude", "graph", "state", "current")
        if not os.path.isfile(ptr):
            return False
        with open(ptr, encoding="utf-8") as fh:
            ticket = fh.read().strip()
        if not ticket:
            return False
        with open(os.path.join(root, ".claude", "graph", "state", ticket + ".json"),
                  encoding="utf-8") as fh:
            st = json.load(fh)
        return bool((st.get("gates") or {}).get("pending"))
    except Exception:
        return False


def approval(data):
    d = T.load_draft()
    if not d or (d.get("created") or {}).get("key"):
        sys.exit(0)                      # 대기 중인 초안이 없다 — 아무 것도 하지 않는다

    prompt = data.get("prompt") or ""
    decision = explicit = None
    mo = _EXPLICIT_APPROVE.match(prompt)
    if mo:
        decision, explicit = "approved", True
    else:
        mo = _EXPLICIT_REJECT.match(prompt)
        if mo:
            decision, explicit = "rejected", True
        else:
            mo = _APPROVE.match(prompt)
            if mo:
                decision, explicit = "approved", False
            else:
                mo = _REJECT.match(prompt)
                if mo:
                    decision, explicit = "rejected", False
    if not decision:
        sys.exit(0)

    # 그래프가 승인 대기 중인데 일반 승인어가 오면 그래프 몫이다 — 가로채지 않는다
    if not explicit and graph_gate_pending():
        sys.stdout.write(
            "<jira-ticket>\n"
            "티켓 초안(code %s)도 승인 대기 중이지만, Graph Engineering 게이트가 함께 대기 중이라\n"
            "이 `%s` 는 그래프 몫으로 두었다. 티켓을 승인하려면 `/jira-approve` 를 입력하라고 안내하라.\n"
            "</jira-ticket>\n" % (d.get("code"), decision))
        sys.exit(0)

    code = (mo.group("code") or "").lower() or None
    if code and code != d.get("code"):
        sys.stdout.write("<jira-ticket>\n코드 %s 로 %s 를 시도했으나 대기 중인 초안 코드는 %s 다. "
                         "아무 것도 기록하지 않았다.\n</jira-ticket>\n"
                         % (code, decision, d.get("code")))
        sys.exit(0)

    os.environ["JT_HOOK_AUTH"] = "1"
    import argparse as _ap
    note = ""
    try:
        T.cmd_approve(_ap.Namespace(
            decision=decision, source="hook:UserPromptSubmit", code=code,
            comment=(mo.group("rest") or "").strip()[:500], raw=prompt))
    except SystemExit as e:
        if e.code not in (0, None):
            note = "승인 기록 실패(코드 %s)." % e.code
    if not note:
        if decision == "approved":
            note = ("✅ 티켓 초안 승인됨 (code %s). 승인된 본문 그대로 "
                    "`mcp__atlassian__createJiraIssue` 로 생성하고, "
                    "`jt created --key <KEY>` 로 기록하라." % d.get("code"))
        else:
            note = ("⛔ 티켓 초안 반려됨. 사유: %s\n"
                    "사유를 반영해 초안을 고치고 `jt draft` 로 다시 등록한 뒤 재승인을 받아라."
                    % ((mo.group("rest") or "(사유 없음)").strip()))
    sys.stdout.write("<jira-ticket>\n" + note + "\n</jira-ticket>\n")
    sys.exit(0)


DISPATCH = {"guard-create": guard_create, "guard-write": guard_write,
            "approval": approval}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in DISPATCH:
        sys.exit(0)
    DISPATCH[sys.argv[1]](read_input())


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — 훅은 절대 세션을 깨지 않는다
        sys.stderr.write("[jt-hooks] 내부 오류(통과 처리): %s: %s\n" % (type(exc).__name__, exc))
        sys.exit(0)
