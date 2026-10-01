#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""JIRA Ticket — 티켓 양식 검증기와 초안 런타임.

이 파일이 티켓 양식의 **유일한 판정자**다. 모델의 "양식대로 썼습니다" 를 믿지 않고
본문을 실제로 파싱해서 빠진 것·안 정한 것·플레이스홀더가 남은 것을 찾아낸다.

설계 원칙
  - 표준 라이브러리만 사용한다(파이썬 3.8+).
  - 쓰기는 원자적(temp + os.replace)이다.
  - 승인 레코드는 UserPromptSubmit 훅만 쓸 수 있다(JT_HOOK_AUTH 게이팅).
  - 이 양식은 Graph Engineering 의 N2a 파서가 소비하는 것과 **같은 양식**이다.
    여기서 통과한 티켓은 그래프가 양식 위반으로 멈추지 않는다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone

SCHEMA_VERSION = 1


# --------------------------------------------------------------------------
# 경로 · 설정
# --------------------------------------------------------------------------

def project_dir() -> str:
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env and os.path.isdir(os.path.join(env, ".claude", "jira")):
        return os.path.abspath(env)
    cur = os.path.abspath(os.getcwd())
    while True:
        if os.path.isfile(os.path.join(cur, ".claude", "jira", "config.json")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return os.path.abspath(os.getcwd())
        cur = parent


def P(*parts) -> str:
    return os.path.join(project_dir(), *parts)


def _load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default if default is not None else {}


def config() -> dict:
    cfg = _load_json(P(".claude", "jira", "config.json"))
    # 접속 정보는 Graph Engineering 설정과 하나로 유지한다(중복 정의 금지).
    j = cfg.setdefault("jira", {})
    if not j.get("cloud_id") or not j.get("site_url"):
        g = _load_json(P(".claude", "graph", "config.json"))
        for key in ("cloud_id", "site_url"):
            if not j.get(key):          # setdefault 는 값이 None 인 기존 키를 못 덮는다
                v = dget(g, "jira." + key)
                if v:
                    j[key] = v
    return cfg


def drafts_dir() -> str:
    return P(*dget(config(), "state.dir", ".claude/jira/drafts").split("/"))


def draft_path() -> str:
    return os.path.join(drafts_dir(), "current.json")


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def dget(obj, path, default=None):
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize(text: str) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def body_hash(text: str) -> str:
    return sha256_text(normalize(text))


def short_code(digest: str) -> str:
    return digest.split(":")[-1][:6]


def atomic_write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".jt-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def die(msg: str, code: int = 1):
    sys.stderr.write("[jt] " + msg + "\n")
    sys.exit(code)


def read_text(path, limit=400000):
    with open(path, encoding="utf-8") as fh:
        return fh.read(limit)


# --------------------------------------------------------------------------
# 파싱 — 티켓 본문을 섹션 트리로
# --------------------------------------------------------------------------

# 값 자리에 남아 있으면 "안 채운 것" 으로 보는 템플릿 흔적들
_PLACEHOLDER_PATTERNS = [
    r"^\(?검증 가능한 문장",
    r"^\(?아는 만큼\)?$",
    r"^\(?없으면\s*[\"“]?없음",
    r"^무엇을 어떻게$",
    r"^\.\.\.$",
    r"^…$",
    r"^<[^>]*>$",          # <여기에 작성>
    r"^TODO$", r"^TBD$",
    r"^예:\s",
]
_PLACEHOLDER_RE = [re.compile(p, re.I) for p in _PLACEHOLDER_PATTERNS]

# 본문 어디에 남아 있어도 안 되는 템플릿 예시 줄
_TEMPLATE_ECHOES = [
    "[cutting-tuna] Swagger 문서를 springdoc-openapi 로 대체",
    "(검증 가능한 문장 하나에 하나씩)",
]


def is_placeholder(value: str) -> bool:
    v = (value or "").strip().strip("`")
    if not v:
        return True
    return any(rx.match(v) for rx in _PLACEHOLDER_RE)


def split_sections(md: str):
    """`# `/`## ` 헤딩 기준으로 (레벨, 제목, 본문) 목록을 만든다."""
    lines = normalize(md).split("\n")
    out, cur = [], None
    for ln in lines:
        m = re.match(r"^(#{1,3})\s+(.*\S)\s*$", ln)
        if m:
            if cur:
                out.append(cur)
            cur = {"level": len(m.group(1)), "title": m.group(2).strip(), "lines": []}
        elif cur is not None:
            cur["lines"].append(ln)
    if cur:
        out.append(cur)
    for s in out:
        s["body"] = "\n".join(s["lines"]).strip()
    return out


def _norm_title(t: str) -> str:
    """`## 범위 밖 — 이번에 안 하는 것` 처럼 꼬리가 붙어도 매칭되게 앞부분만 본다."""
    t = re.sub(r"\s*[—\-–:].*$", "", t or "").strip()
    return re.sub(r"\s+", "", t)


def find_section(sections, title, level=None):
    want = _norm_title(title)
    for s in sections:
        if _norm_title(s["title"]) == want and (level is None or s["level"] == level):
            return s
    return None


def parse_kv_bullets(body: str) -> dict:
    """`- 키: 값` 형태의 불릿을 dict 로. 값이 없으면 빈 문자열."""
    out = {}
    for ln in (body or "").split("\n"):
        m = re.match(r"^\s*[-*]\s*([^:：]+)[:：]\s*(.*)$", ln)
        if m:
            out[re.sub(r"\s+", "", m.group(1))] = m.group(2).strip()
    return out


def parse_bullets(body: str):
    out = []
    for ln in (body or "").split("\n"):
        m = re.match(r"^\s*[-*]\s+(?!\[)(.*\S)\s*$", ln)
        if m:
            out.append(m.group(1).strip())
    return out


def parse_checkboxes(body: str):
    """`- [ ] 문장` / `- [x] 문장` (마크다운 이스케이프 `\\[ \\]` 도 인식)."""
    out = []
    for ln in (body or "").split("\n"):
        m = re.match(r"^\s*[-*]\s*\\?\[\s*([ xXoOvV])\s*\\?\]\s*(.*\S)\s*$", ln)
        if m:
            out.append({"checked": m.group(1).strip() != "", "text": m.group(2).strip()})
    return out


def parse(md: str) -> dict:
    """티켓 본문을 구조화한다. 검증은 하지 않는다."""
    sections = split_sections(md)
    res = {"summary": None, "sections_found": [s["title"] for s in sections]}

    s = find_section(sections, "요약", 1)
    if s:
        # 예시 줄(`예: ...`)과 빈 줄을 걷어내고 첫 실질 줄을 요약으로 본다
        for ln in s["body"].split("\n"):
            t = ln.strip()
            if t and not t.startswith("예:") and not t.startswith("예)"):
                res["summary"] = t
                break

    tgt = find_section(sections, "대상", 2)
    kv = parse_kv_bullets(tgt["body"]) if tgt else {}
    res["target"] = {"repo": kv.get("저장소", ""), "module": kv.get("모듈/패키지", ""),
                     "entrypoint": kv.get("진입점", "") or kv.get("진입점(아는만큼)", "")}

    bg = find_section(sections, "배경", 2)
    kv = parse_kv_bullets(bg["body"]) if bg else {}
    res["background"] = {"as_is": kv.get("현재(As-Is)", "") or kv.get("현재", ""),
                         "to_be": kv.get("원하는상태(To-Be)", "") or kv.get("원하는상태", ""),
                         "why": kv.get("왜해야하는가", "") or kv.get("왜", "")}

    dod = find_section(sections, "완료 정의", 2)
    res["definition_of_done"] = parse_dod(dod["body"] if dod else "")

    ref = find_section(sections, "참고", 2)
    kv = parse_kv_bullets(ref["body"]) if ref else {}
    res["references"] = kv

    ac = find_section(sections, "완료 조건", 2)
    res["acceptance_criteria"] = parse_checkboxes(ac["body"] if ac else "")

    oos = find_section(sections, "범위 밖", 2)
    res["out_of_scope"] = parse_bullets(oos["body"] if oos else "")

    con = find_section(sections, "제약", 2)
    kv = parse_kv_bullets(con["body"] if con else "")
    res["constraints"] = {
        "backward_compat": kv.get("하위호환", ""),
        "db_schema": kv.get("DB스키마변경", ""),
        "perf_security": kv.get("성능,보안", "") or kv.get("성능/보안", "") or kv.get("성능·보안", ""),
    }
    res["_sections"] = {_norm_title(s["title"]): s for s in sections}
    return res


def parse_dod(body: str) -> dict:
    """`( ) 조사, 보고까지 (O) 배포까지` 에서 선택된 쪽을 찾는다."""
    opts = []
    for m in re.finditer(r"\(\s*([^)\s]?)\s*\)\s*([^()\n]+)", body or ""):
        mark, label = m.group(1), m.group(2).strip()
        if label:
            opts.append({"label": label, "marked": mark != ""})
    chosen = [o["label"] for o in opts if o["marked"]]
    return {"options": opts, "chosen": chosen}


# --------------------------------------------------------------------------
# 검증 — 무엇이 위반인가
# --------------------------------------------------------------------------

REQUIRED_SECTIONS = [
    ("요약", 1), ("설명", 1), ("대상", 2), ("배경", 2), ("완료 정의", 2),
    ("참고", 2), ("요구사항", 1), ("완료 조건", 2), ("범위 밖", 2), ("제약", 2),
]

_ONE_OF = {
    "backward_compat": (["유지 필요", "유지필요"], ["깨도 됨", "깨도됨"]),
    "db_schema": (["허용"], ["불가"]),
}
_SUMMARY_RE = re.compile(r"^\[([^\[\]]+)\]\s*(\S.*)$")


def _v(code, where, msg, fix):
    return {"code": code, "where": where, "message": msg, "fix": fix}


def validate(md: str, cfg=None) -> dict:
    """(violations, warnings) 을 담은 결과를 돌려준다. 판정은 전부 여기서 한다."""
    cfg = cfg or config()
    p = parse(md)
    vio, warn = [], []
    secs = p.get("_sections", {})

    # 1) 필수 섹션
    for title, level in REQUIRED_SECTIONS:
        if _norm_title(title) not in secs:
            vio.append(_v("SECTION_MISSING", title,
                          "필수 섹션 `%s%s` 가 없다." % ("#" * level + " ", title),
                          "양식(references/template.md)의 섹션을 그대로 넣어라."))

    # 2) 템플릿 예시가 그대로 남아 있는가
    body_norm = normalize(md)
    for echo in _TEMPLATE_ECHOES:
        if echo in body_norm:
            vio.append(_v("TEMPLATE_ECHO", "본문",
                          "템플릿 예시 문구가 그대로 남아 있다: %r" % echo,
                          "예시 줄을 지우고 실제 내용으로 바꿔라."))

    # 3) 요약 — `[repo] 무엇을 어떻게`
    summary = (p.get("summary") or "").strip()
    maxlen = dget(cfg, "ticket.summary_max_len", 255)
    if not summary:
        vio.append(_v("SUMMARY_EMPTY", "# 요약", "요약이 비어 있다.",
                      "`[저장소] 무엇을 어떻게` 한 줄을 써라."))
    else:
        m = _SUMMARY_RE.match(summary)
        if not m:
            vio.append(_v("SUMMARY_FORMAT", "# 요약",
                          "요약이 `[repo] 무엇을 어떻게` 형식이 아니다: %r" % summary,
                          "대괄호로 저장소를 먼저 적어라. 예: `[cutting-tuna] …`"))
        else:
            if is_placeholder(m.group(2)):
                vio.append(_v("SUMMARY_PLACEHOLDER", "# 요약",
                              "요약 내용이 플레이스홀더다: %r" % m.group(2),
                              "실제로 무엇을 어떻게 할지 써라."))
            if len(summary) > maxlen:
                vio.append(_v("SUMMARY_TOO_LONG", "# 요약",
                              "요약이 %d자를 넘는다(%d자)." % (maxlen, len(summary)),
                              "한 줄로 줄여라."))

    # 4) 대상 — 저장소는 필수
    if _norm_title("대상") in secs:
        if is_placeholder(p["target"]["repo"]):
            vio.append(_v("TARGET_REPO_EMPTY", "## 대상",
                          "`- 저장소:` 가 비어 있다.",
                          "저장소 이름을 적어라. 요약의 `[repo]` 와 같아야 한다."))
        elif summary:
            m = _SUMMARY_RE.match(summary)
            if m and _norm_title(m.group(1)) != _norm_title(p["target"]["repo"]):
                warn.append(_v("TARGET_REPO_MISMATCH", "## 대상",
                               "요약의 `[%s]` 와 `- 저장소: %s` 가 다르다."
                               % (m.group(1), p["target"]["repo"]),
                               "의도한 것이 아니면 하나로 맞춰라."))
        if is_placeholder(p["target"]["module"]):
            warn.append(_v("TARGET_MODULE_EMPTY", "## 대상",
                           "`- 모듈/패키지:` 가 비어 있다.",
                           "아는 범위까지만 적어도 된다. 영향 범위 조사 비용이 줄어든다."))

    # 5) 배경 — 셋 다 필요
    if _norm_title("배경") in secs:
        for key, label in (("as_is", "현재(As-Is)"), ("to_be", "원하는 상태(To-Be)"),
                           ("why", "왜 해야하는가")):
            if is_placeholder(p["background"][key]):
                vio.append(_v("BACKGROUND_EMPTY", "## 배경",
                              "`- %s:` 가 비어 있다." % label,
                              "한 문장이라도 채워라. 이게 없으면 리뷰가 옳고 그름을 판정할 수 없다."))

    # 6) 완료 정의 — 정확히 하나 선택
    if _norm_title("완료정의") in secs or _norm_title("완료 정의") in secs:
        chosen = p["definition_of_done"]["chosen"]
        if len(chosen) == 0:
            vio.append(_v("DOD_NOT_CHOSEN", "## 완료 정의",
                          "어디까지가 완료인지 표시되지 않았다.",
                          "`(O) 조사, 보고까지` 처럼 한쪽 괄호 안에 표시하라."))
        elif len(chosen) > 1:
            vio.append(_v("DOD_MULTIPLE", "## 완료 정의",
                          "완료 정의가 %d개 선택됐다: %s" % (len(chosen), ", ".join(chosen)),
                          "하나만 선택하라."))

    # 7) 완료 조건 — 최소 1개, 검증 가능한 문장
    if _norm_title("완료조건") in secs:
        acs = p["acceptance_criteria"]
        if not acs:
            vio.append(_v("AC_EMPTY", "## 완료 조건",
                          "완료 조건이 하나도 없다.",
                          "`- [ ] <검증 가능한 문장>` 형태로 최소 1개를 써라. "
                          "이게 비면 그래프도 여기서 멈춘다."))
        for i, ac in enumerate(acs, 1):
            if is_placeholder(ac["text"]):
                vio.append(_v("AC_PLACEHOLDER", "## 완료 조건 #%d" % i,
                              "완료 조건이 플레이스홀더다: %r" % ac["text"],
                              "무엇이 되면 끝인지 검증 가능한 문장으로 써라."))
            elif len(ac["text"]) < dget(cfg, "ticket.ac_min_len", 6):
                warn.append(_v("AC_TOO_SHORT", "## 완료 조건 #%d" % i,
                               "완료 조건이 너무 짧아 검증 가능한지 의심스럽다: %r" % ac["text"],
                               "'무엇이 어떤 상태가 되면 통과' 인지 드러나게 써라."))

    # 8) 범위 밖 — 비워두지 말고 "없음" 이라도 적는다
    if _norm_title("범위밖") in secs:
        oos = [x for x in p["out_of_scope"] if not is_placeholder(x)]
        if not oos:
            vio.append(_v("OOS_EMPTY", "## 범위 밖",
                          "범위 밖이 비어 있다.",
                          "이번에 안 할 것을 적거나, 정말 없으면 `- 없음` 이라고 명시하라. "
                          "이건 개발·리뷰에서 하드 제약으로 쓰인다."))

    # 9) 제약 — 셋 다 결정돼 있어야 한다
    if _norm_title("제약") in secs:
        c = p["constraints"]
        for key, label in (("backward_compat", "하위호환"), ("db_schema", "DB 스키마 변경")):
            val = c[key]
            if is_placeholder(val):
                vio.append(_v("CONSTRAINT_EMPTY", "## 제약",
                              "`- %s:` 가 비어 있다." % label, "선택지 중 하나로 결정하라."))
                continue
            a, b = _ONE_OF[key]
            hit_a = any(x in val for x in a)
            hit_b = any(x in val for x in b)
            if hit_a and hit_b:
                vio.append(_v("CONSTRAINT_UNDECIDED", "## 제약",
                              "`- %s: %s` — 선택지가 둘 다 남아 있다." % (label, val),
                              "고르지 않은 쪽을 지워라. 예: `- %s: %s`" % (label, a[0])))
            elif not (hit_a or hit_b):
                warn.append(_v("CONSTRAINT_FREEFORM", "## 제약",
                               "`- %s: %s` — 정해진 선택지가 아니다." % (label, val),
                               "가능하면 `%s` / `%s` 중 하나로 적어라." % (a[0], b[0])))
        if is_placeholder(c["perf_security"]):
            vio.append(_v("CONSTRAINT_EMPTY", "## 제약",
                          "`- 성능, 보안:` 이 비어 있다.",
                          "제약이 없으면 `없음` 이라고 명시하라."))

    ok = not vio
    return {"ok": ok, "violations": vio, "warnings": warn, "parsed": _strip_private(p)}


def _strip_private(p):
    return {k: v for k, v in p.items() if not k.startswith("_")}


def split_summary_description(md: str):
    """summary 필드로 갈 부분과 description 으로 갈 부분을 나눈다."""
    text = normalize(md)
    p = parse(text)
    summary = p.get("summary") or ""
    m = re.search(r"^#\s+설명\s*$", text, re.M)
    description = text[m.start():] if m else text
    return summary, description.strip()


# --------------------------------------------------------------------------
# 초안 상태
# --------------------------------------------------------------------------

def load_draft():
    return _load_json(draft_path(), None)


def save_draft(d):
    d["updated_at"] = now()
    atomic_write(draft_path(), json.dumps(d, ensure_ascii=False, indent=2) + "\n")


def is_pending() -> bool:
    d = load_draft()
    return bool(d) and not dget(d, "created.key")


def create_allowed(d=None):
    """지금 JIRA 티켓을 만들어도 되는가 — (ok, 사유). 훅이 이 함수를 쓴다."""
    d = d or load_draft()
    if not d:
        return False, ("등록된 티켓 초안이 없다. `jt draft --file <초안.md>` 로 "
                       "양식 검증을 통과한 초안을 먼저 등록해야 한다.")
    if dget(d, "created.key"):
        return False, ("이 초안은 이미 %s 로 생성됐다(%s). 새 티켓은 새 초안으로 만들어라."
                       % (d["created"]["key"], d["created"]["at"]))
    if not dget(d, "validation.ok"):
        return False, "초안이 양식 검증을 통과하지 못했다. `jt validate` 로 위반을 확인하라."
    ap = d.get("approval") or {}
    if ap.get("decision") != "approved":
        return False, "사용자 승인이 없다. 초안을 제시하고 `approve` 를 받아야 한다."
    if ap.get("source") != "hook:UserPromptSubmit":
        return False, "승인 기록이 훅이 아닌 경로로 작성됐다(위조 의심). 무효."
    if ap.get("hash") != d.get("hash"):
        return False, ("승인 후 초안이 바뀌었다.\n  승인된 해시: %s\n  현재 해시:   %s\n"
                       "  → 바뀐 내용을 다시 제시하고 재승인을 받아라."
                       % (ap.get("hash"), d.get("hash")))
    return True, ""


# --------------------------------------------------------------------------
# 서브커맨드
# --------------------------------------------------------------------------

def _read_arg_text(a):
    if getattr(a, "file", None):
        return read_text(a.file)
    if getattr(a, "value", None):
        return a.value
    return sys.stdin.read()


def _render(res, verbose=True):
    lines = []
    if res["ok"]:
        lines.append("✅ 양식 검증 통과")
    else:
        lines.append("❌ 양식 위반 %d건 — 이대로는 티켓을 만들 수 없다" % len(res["violations"]))
    for v in res["violations"]:
        lines.append("  [%s] %s" % (v["where"], v["message"]))
        lines.append("      → %s" % v["fix"])
    if verbose and res["warnings"]:
        lines.append("  ── 경고(차단하지 않음) ──")
        for w in res["warnings"]:
            lines.append("  [%s] %s" % (w["where"], w["message"]))
            lines.append("      → %s" % w["fix"])
    return "\n".join(lines)


def cmd_template(a):
    path = P(".claude", "skills", "jira-ticket", "references", "template.md")
    text = read_text(path) if os.path.isfile(path) else ""
    m = re.search(r"```+\n(# 요약.*?)\n```+", text, re.S)
    print(m.group(1) if m else text)


def cmd_validate(a):
    md = _read_arg_text(a)
    res = validate(md)
    if a.json_out:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(_render(res))
    sys.exit(0 if res["ok"] else 2)


def cmd_draft(a):
    md = _read_arg_text(a)
    res = validate(md)
    if not res["ok"]:
        sys.stderr.write(_render(res) + "\n")
        die("양식 위반이 있어 초안을 등록하지 않았다. 고친 뒤 다시 실행하라.", 2)

    summary, description = split_summary_description(md)
    h = body_hash(md)
    cfg = config()
    prev = load_draft() or {}
    d = {
        "schema_version": SCHEMA_VERSION,
        "created_at": prev.get("created_at") or now(),
        "source_file": os.path.relpath(a.file, project_dir()) if a.file else None,
        "project_key": a.project or dget(cfg, "ticket.default_project"),
        "issue_type": a.issue_type or dget(cfg, "ticket.default_issue_type"),
        "summary": summary,
        "description_md": description,
        "body_md": normalize(md),
        "hash": h,
        "code": short_code(h),
        "validation": {"ok": True, "warnings": res["warnings"], "at": now()},
        "approval": None,
        "created": None,
        "history": (prev.get("history") or []) + [{"at": now(), "event": "draft", "hash": h}],
    }
    save_draft(d)
    print(json.dumps({
        "ok": True, "hash": h, "code": d["code"],
        "project_key": d["project_key"], "issue_type": d["issue_type"],
        "summary": summary,
        "warnings": len(res["warnings"]),
        "instruction": "초안 전문과 코드를 사용자에게 제시하고 `approve` 를 받아야 "
                       "티켓이 생성된다. 승인은 초안 해시에 묶인다.",
    }, ensure_ascii=False, indent=2))
    if res["warnings"]:
        sys.stderr.write(_render(res) + "\n")


def cmd_check(a):
    ok, why = create_allowed()
    print(json.dumps({"allowed": ok, "reason": why}, ensure_ascii=False, indent=2))
    if not ok:
        sys.exit(2)


def cmd_show(a):
    d = load_draft()
    if not d:
        print(json.dumps({"pending": False}, ensure_ascii=False))
        return
    if a.field:
        print(json.dumps(dget(d, a.field), ensure_ascii=False, indent=2))
        return
    print(json.dumps(d, ensure_ascii=False, indent=2))


def cmd_status(a):
    d = load_draft()
    if not d:
        print("JIRA Ticket: 등록된 초안 없음.")
        return
    ap = d.get("approval") or {}
    cr = d.get("created") or {}
    print("\n".join([
        "JIRA Ticket 초안",
        "  요약      : %s" % d.get("summary"),
        "  프로젝트  : %s / %s" % (d.get("project_key"), d.get("issue_type")),
        "  양식 검증 : %s (경고 %d건)" % (
            "통과" if dget(d, "validation.ok") else "실패",
            len(dget(d, "validation.warnings", []) or [])),
        "  승인      : %s%s" % (ap.get("decision") or "대기 중",
                                (" (%s)" % ap.get("at", "")[:19]) if ap.get("at") else ""),
        "  생성      : %s" % (("%s  %s" % (cr.get("key"), cr.get("url"))) if cr else "아직 안 함"),
        "  코드      : %s" % d.get("code"),
    ]))


def cmd_approve(a):
    """훅 전용. 사람의 실제 입력에서만 발화한다."""
    if os.environ.get("JT_HOOK_AUTH") != "1" or a.source != "hook:UserPromptSubmit":
        die("승인 기록은 UserPromptSubmit 훅만 쓸 수 있다. 모델이 직접 승인할 수 없다.", 2)
    d = load_draft()
    if not d:
        die("등록된 초안이 없다.", 3)
    if dget(d, "created.key"):
        die("이미 생성된 초안이다.", 3)
    if a.code and a.code != d.get("code"):
        die("승인 코드 불일치: 입력=%s, 대기 중=%s" % (a.code, d.get("code")), 3)
    d["approval"] = {
        "decision": a.decision, "at": now(), "by": "user",
        "hash": d.get("hash"), "code": d.get("code"),
        "source": "hook:UserPromptSubmit",
        "comment": (a.comment or "")[:500],
        "raw_prompt_excerpt": (a.raw or "")[:400],
    }
    d.setdefault("history", []).append(
        {"at": now(), "event": "approval:" + a.decision, "hash": d.get("hash")})
    save_draft(d)
    print(json.dumps({"ok": True, "decision": a.decision}, ensure_ascii=False))


def cmd_created(a):
    ok, why = create_allowed()
    if not ok:
        die("생성 기록 거부 — " + why, 2)
    d = load_draft()
    d["created"] = {"key": a.key, "url": a.url or _issue_url(a.key), "at": now()}
    d.setdefault("history", []).append({"at": now(), "event": "created", "key": a.key})
    save_draft(d)
    archive = os.path.join(drafts_dir(), "%s.json" % a.key)
    atomic_write(archive, json.dumps(d, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"ok": True, "key": a.key, "url": d["created"]["url"],
                      "archived": os.path.relpath(archive, project_dir())},
                     ensure_ascii=False, indent=2))


def _issue_url(key):
    site = (dget(config(), "jira.site_url") or "").rstrip("/")
    return "%s/browse/%s" % (site, key) if site and key else None


def cmd_clear(a):
    d = load_draft()
    if not d:
        print("[jt] 초안이 없다. 정리할 것 없음.")
        return
    try:
        os.unlink(draft_path())
    except OSError:
        pass
    print("[jt] 초안을 비웠다. (생성된 티켓은 %s)"
          % (dget(d, "created.key") or "없음"))


# --------------------------------------------------------------------------
# 파서
# --------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(prog="jt", description="JIRA 티켓 양식 검증·초안 런타임")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("template", help="빈 양식을 출력한다")
    s.set_defaults(func=cmd_template)

    s = sub.add_parser("validate", help="본문의 양식을 검증한다 (위반 시 exit 2)")
    s.add_argument("--file"); s.add_argument("--value")
    s.add_argument("--json", dest="json_out", action="store_true")
    s.set_defaults(func=cmd_validate)

    s = sub.add_parser("draft", help="검증 통과한 본문을 초안으로 등록한다")
    s.add_argument("--file"); s.add_argument("--value")
    s.add_argument("--project"); s.add_argument("--issue-type", dest="issue_type")
    s.set_defaults(func=cmd_draft)

    s = sub.add_parser("check", help="지금 티켓을 만들어도 되는지 판정한다")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("show"); s.add_argument("--field"); s.set_defaults(func=cmd_show)
    s = sub.add_parser("status"); s.set_defaults(func=cmd_status)

    s = sub.add_parser("approve", help="[훅 전용] 사용자 입력에서만 발화")
    s.add_argument("--decision", choices=["approved", "rejected"], required=True)
    s.add_argument("--source", required=True)
    s.add_argument("--code"); s.add_argument("--comment"); s.add_argument("--raw")
    s.set_defaults(func=cmd_approve)

    s = sub.add_parser("created", help="생성 결과를 기록한다")
    s.add_argument("--key", required=True); s.add_argument("--url")
    s.set_defaults(func=cmd_created)

    s = sub.add_parser("clear", help="초안을 비운다")
    s.set_defaults(func=cmd_clear)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
