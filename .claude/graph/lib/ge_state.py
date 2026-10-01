#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Graph Engineering — 공유 상태(shared state) 런타임.

이 파일이 그래프의 유일한 상태 기록기다. 노드(서브에이전트)는 `.claude/graph/bin/ge`
래퍼를 통해서만 상태를 읽고 쓴다. 훅은 같은 모듈을 import 해서 판정에 쓴다.

설계 원칙
  - 표준 라이브러리만 사용한다(파이썬 3.8+).
  - 쓰기는 원자적(temp + os.replace)이다.
  - 승인 레코드는 UserPromptSubmit 훅만 쓸 수 있다(GE_HOOK_AUTH 게이팅).
  - 알 수 없는 상태/파일 부재는 예외가 아니라 "그래프 비활성"으로 취급한다.
"""

from __future__ import annotations

import argparse
import contextlib
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
from datetime import datetime, timezone

try:
    import fcntl
except ImportError:  # 비 POSIX — 락 없이 동작(단일 프로세스 전제)
    fcntl = None

SCHEMA_VERSION = 1
# 주 경로 N0..N10 + 곁가지 노드. NODE_ORDER 는 **소속 목록**이지 강제 순서가 아니다
# (역방향 엣지가 이미 존재한다). AMEND 는 게이트 반려에서만 진입하는 곁가지다.
NODE_ORDER = ["N0", "N1", "N2", "N3", "N4", "N5", "N6", "N7", "N8", "N9", "N10",
              "AMEND", "DONE"]
GATES = ["GATE-PLAN", "GATE-REVIEW", "GATE-COMMIT", "GATE-TICKET"]
# 게이트가 통과되어야 진입할 수 있는 노드
GATE_UNLOCKS = {"GATE-PLAN": "N4", "GATE-REVIEW": "N7", "GATE-COMMIT": "N7-commit",
                "GATE-TICKET": "AMEND-write"}


# --------------------------------------------------------------------------
# 경로
# --------------------------------------------------------------------------

def project_dir() -> str:
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env and os.path.isdir(os.path.join(env, ".claude", "graph")):
        return os.path.abspath(env)
    cur = os.path.abspath(os.getcwd())
    while True:
        if os.path.isfile(os.path.join(cur, ".claude", "graph", "config.json")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return os.path.abspath(os.getcwd())
        cur = parent


def P(*parts) -> str:
    return os.path.join(project_dir(), *parts)


def config() -> dict:
    try:
        with open(P(".claude", "graph", "config.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def state_dir() -> str:
    return P(".claude", "graph", "state")


def logs_dir() -> str:
    return P(".claude", "graph", "logs")


def pointer_path() -> str:
    return os.path.join(state_dir(), "current")


def state_path(ticket: str) -> str:
    return os.path.join(state_dir(), ticket + ".json")


# --------------------------------------------------------------------------
# 기본 유틸
# --------------------------------------------------------------------------

def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def short_code(digest: str) -> str:
    return digest.split(":")[-1][:6]


def normalize_artifact(text: str) -> str:
    """게이트 해시 계산용 정규화 — 줄바꿈/양끝 공백 차이로 승인이 깨지지 않게 한다."""
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def gate_hash(text: str) -> str:
    return sha256_text(normalize_artifact(text))


def atomic_write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".ge-", suffix=".tmp")
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
    sys.stderr.write("[ge] " + msg + "\n")
    sys.exit(code)


def norm_rel(path: str) -> str:
    """저장소 루트 기준 상대 경로로 정규화한다."""
    if not path:
        return ""
    root = os.path.normpath(project_dir())
    if os.path.isabs(path):
        ap = os.path.normpath(path)
    else:
        # cwd 기준으로 먼저 풀되, 프로젝트 밖으로 나가면 루트 기준으로 다시 푼다.
        cand = os.path.normpath(os.path.join(os.getcwd(), path))
        inside = cand == root or cand.startswith(root + os.sep)
        ap = cand if inside else os.path.normpath(os.path.join(root, path))
    try:
        rel = os.path.relpath(ap, root)
    except ValueError:
        return path
    return rel.replace(os.sep, "/")


def glob_any(rel_path: str, patterns) -> bool:
    rp = rel_path.lstrip("./")
    for pat in patterns or []:
        if fnmatch.fnmatch(rp, pat):
            return True
        # `**/x` 는 루트 바로 아래 x 도 포함해야 자연스럽다
        if pat.startswith("**/") and fnmatch.fnmatch(rp, pat[3:]):
            return True
        if fnmatch.fnmatch("/" + rp, "/" + pat):
            return True
    return False


# --------------------------------------------------------------------------
# 상태 로드/세이브
# --------------------------------------------------------------------------

def current_ticket():
    try:
        with open(pointer_path(), encoding="utf-8") as fh:
            t = fh.read().strip()
        return t or None
    except Exception:
        return None


def is_active() -> bool:
    t = current_ticket()
    return bool(t) and os.path.isfile(state_path(t))


def load(ticket=None):
    ticket = ticket or current_ticket()
    if not ticket:
        return None
    try:
        with open(state_path(ticket), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def save(st: dict) -> None:
    st["updated_at"] = now()
    atomic_write(state_path(st["ticket"]), json.dumps(st, ensure_ascii=False, indent=2) + "\n")


_LOCK_DEPTH = 0


def lock_path(ticket=None) -> str:
    t = ticket or current_ticket() or "_global"
    return os.path.join(state_dir(), ".%s.lock" % t)


@contextlib.contextmanager
def state_lock(ticket=None, timeout=30.0):
    """상태의 read-modify-write 를 프로세스 간에 직렬화한다.

    fan-out(N2 3-way, N6 4-way)에서 여러 `ge` 프로세스가 동시에 load→변형→save 하면
    나중 save 가 앞선 save 를 통째로 덮어써 필드가 유실된다(lost update).
    쓰기를 하는 모든 경로는 이 락 안에서 상태를 다시 읽어야 한다.
    """
    global _LOCK_DEPTH
    if fcntl is None or _LOCK_DEPTH > 0:   # 락 불가 또는 재진입
        _LOCK_DEPTH += 1
        try:
            yield
        finally:
            _LOCK_DEPTH -= 1
        return
    os.makedirs(state_dir(), exist_ok=True)
    fh = open(lock_path(ticket), "a+")
    try:
        deadline = time.time() + timeout
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() > deadline:
                    die("상태 잠금을 %.0f초 안에 얻지 못했다: %s\n"
                        "  → 다른 ge 프로세스가 멈춰 있는지 확인하라." % (timeout, lock_path(ticket)), 4)
                time.sleep(0.03)
        _LOCK_DEPTH = 1
        try:
            yield
        finally:
            _LOCK_DEPTH = 0
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


def dget(obj, path, default=None):
    cur = obj
    for part in path.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
                continue
            except (ValueError, IndexError):
                return default
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def dset(obj, path, value):
    parts = path.split(".")
    cur = obj
    for part in parts[:-1]:
        if part not in cur or not isinstance(cur[part], (dict, list)):
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value


def log_event(st, node, event, note=""):
    st.setdefault("history", []).append(
        {"node": node, "event": event, "at": now(), "note": note}
    )


# --------------------------------------------------------------------------
# 새 상태 스켈레톤
# --------------------------------------------------------------------------

def new_state(ticket, branch, base_branch) -> dict:
    cfg = config()
    return {
        "schema_version": SCHEMA_VERSION,
        "ticket": ticket,
        "created_at": now(),
        "updated_at": now(),
        "current_node": "N0",
        "node_status": "running",
        "halt_reason": None,
        "git": {
            "base_branch": base_branch,
            "branch": branch,
            "base_sha": None,
            "head_sha_at_start": None,
            "worktree_clean_at_start": None,
        },
        "runtime": {
            "project_state": None,
            "graphify": {"available": None, "marker": None, "detected_at": None},
            "build": {"status": "unknown"},
        },
        "jira": {
            "cloud_id": dget(cfg, "jira.cloud_id"),
            "url": None,
            "summary": None,
            "status_at_entry": None,
            "current_status": None,
            "raw_description_md": None,
            "raw_description_adf_path": None,
            "transitions_applied": [],
            "node_entries": {},
        },
        "requirements": {
            "format_valid": None,
            "format_violations": [],
            "draft_proposed": None,
            "target": {"repos": [], "modules": [], "entrypoints": []},
            "background": {"as_is": None, "to_be": None, "why": None},
            "definition_of_done_scope": None,
            "acceptance_criteria": [],
            "out_of_scope": [],
            "constraints": {"backward_compat": None, "db_schema": None, "perf_security": None},
        },
        "conventions": {
            "origin": None,
            "evidence": [],
            "architecture": {},
            "naming": {},
            "error_handling": None,
            "testing": {},
            "formatting": {},
            "decided_this_run": [],
            "confidence": None,
            "open_questions": [],
        },
        "impact": {"files": [], "modules": [], "risk_notes": []},
        "plan": {"markdown": None, "hash": None, "created_at": None},
        "gates": {"pending": None, "approvals": []},
        "tdd": {"mode": dget(cfg, "tdd.mode", "block"), "attempts": []},
        "tests": {"attempts": []},
        "api_docs": {
            "style": None, "required": None, "checked_at": None,
            "controllers_changed": [], "documented": [], "missing": [],
            "snippets_found": 0, "exemptions": [],
        },
        "review": {"rounds": []},
        "retries": {
            "n5_to_n4": 0,
            "n6_to_n4": 0,
            "gate_to_amend": 0,
            "limits": {
                "n5_to_n4": dget(cfg, "retries.n5_to_n4", 3),
                "n6_to_n4": dget(cfg, "retries.n6_to_n4", 2),
                "gate_to_amend": dget(cfg, "retries.gate_to_amend", 2),
            },
        },
        "ticket_amendments": [],
        "changed_files": [],
        "commit": {"message": None, "hash": None, "at": None},
        "jira_updates": {"acceptance_updated": False, "comment_id": None},
        "graphify_update": {"status": None, "at": None, "reason": None,
                            "before": {}, "after": {}, "changed": None},
        "history": [],
    }


# --------------------------------------------------------------------------
# git 헬퍼
# --------------------------------------------------------------------------

def git(*args, cwd=None):
    try:
        p = subprocess.run(
            ["git"] + list(args), cwd=cwd or project_dir(),
            capture_output=True, text=True, timeout=30,
        )
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as exc:  # git 없음 등
        return 127, "", str(exc)


def current_branch():
    rc, out, _ = git("rev-parse", "--abbrev-ref", "HEAD")
    return out if rc == 0 else None


# --------------------------------------------------------------------------
# 런타임 탐지
# --------------------------------------------------------------------------

def graphify_stats(root=None) -> dict:
    """graphify-out/graph.json 의 규모와 갱신 시각을 읽는다. 없으면 빈 dict."""
    root = root or project_dir()
    cfg = config()
    path = os.path.join(root, dget(cfg, "graphify.marker", "graphify-out/graph.json"))
    if not os.path.isfile(path):
        return {}
    stat = os.stat(path)
    out = {"path": norm_rel(path), "bytes": stat.st_size,
           "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc)
                            .astimezone().isoformat(timespec="seconds")}
    try:
        with open(path, encoding="utf-8") as fh:
            g = json.load(fh)
        for key in ("nodes", "edges", "links"):
            v = g.get(key)
            if isinstance(v, list):
                out["edges" if key == "links" else key] = len(v)
    except Exception:
        out["parse_error"] = True     # 규모를 못 읽어도 mtime 비교는 유효하다
    return out


def detect_graphify() -> dict:
    cfg = config()
    marker = dget(cfg, "graphify.marker", "graphify-out/graph.json")
    fallback = dget(cfg, "graphify.fallback_marker", "graphify-out")
    if os.path.isfile(P(marker)):
        return {"available": True, "marker": marker, "detected_at": now(),
                "mode": "graph.json", "stats": graphify_stats(),
                "instruction": "graphify skill 최우선 사용"}
    if os.path.isdir(P(fallback)):
        return {"available": True, "marker": fallback, "detected_at": now(),
                "mode": "dir-only", "stats": {},
                "instruction": "graphify skill 최우선 사용(그래프 재빌드 필요할 수 있음)"}
    return {"available": False, "marker": None, "detected_at": now(),
            "mode": "none", "stats": {},
            "instruction": "일반 탐색(grep/read/glob) 폴백"}


# 순서 = 우선순위. Kotest·Spock 은 JUnit Platform 위에서 돌기 때문에
# `useJUnitPlatform()` 이 함께 있는 것이 정상이다. junit5 를 먼저 매칭하면
# Kotest 프로젝트가 영원히 junit5 로 잡힌다 — 반드시 구체적인 것이 먼저 온다.
_TEST_FW_HINTS = [
    ("kotest", ["io.kotest", "kotest-runner", "kotest-assertions", "KotestExtension"]),
    ("spock", ["spock-core", "org.spockframework"]),
    ("testng", ["useTestNG", "org.testng"]),
    ("junit5", ["useJUnitPlatform", "junit-jupiter", "junit.jupiter",
                "spring-boot-starter-test"]),   # Boot 2.2+ 스타터가 JUnit 5 를 가져온다
    ("junit4", ["junit:junit", "useJUnit()"]),
]

# 테스트를 거드는 라이브러리(주 프레임워크는 아니지만 작성 방식에 영향을 준다)
_TEST_LIB_HINTS = [
    ("springmockk", ["com.ninja-squad:springmockk", "springmockk"]),
    ("mockk", ["io.mockk", "mockk"]),
    ("mockito", ["org.mockito", "mockito-core", "mockito-kotlin"]),
    ("kotest-spring", ["kotest-extensions-spring", "kotest.extensions.spring"]),
    ("testcontainers", ["org.testcontainers", "testcontainers"]),
]

# Spring REST Docs — flavor 별 의존성 좌표
_RESTDOCS_FLAVORS = [
    ("mockmvc", ["spring-restdocs-mockmvc"]),
    ("webtestclient", ["spring-restdocs-webtestclient"]),
    ("restassured", ["spring-restdocs-restassured"]),
]
_DEFAULT_SNIPPETS_DIR = "build/generated-snippets"


_LANG_EXT = {".kt": "kotlin", ".java": "java", ".groovy": "groovy", ".scala": "scala"}
# 따옴표·괄호 형태(Groovy/Kotlin DSL)에 흔들리지 않게 정규식으로 본다
_LANG_PLUGIN_RES = [
    ("kotlin", re.compile(r"""kotlin\s*\(\s*['"]jvm['"]|org\.jetbrains\.kotlin\.jvm|"""
                          r"""plugin:\s*['"]kotlin|kotlin-spring|kotlin\(['"]plugin""")),
    ("java", re.compile(r"""id\s*\(?\s*['"](?:java|java-library)['"]|"""
                        r"""apply\s+plugin:\s*['"]java['"]""")),
]
_SKIP_DIRS = {"build", "out", "target", ".git", ".gradle", "node_modules", "generated"}


def detect_language(root: str, blob: str, source_roots, test_roots) -> dict:
    """**소스 파일을 세서** 주 언어를 정한다.

    빌드 스크립트 DSL(`dsl` 필드, .kts 여부)과 혼동하지 마라 — 별개다.
    Groovy DSL 빌드에 Kotlin 소스인 조합이 흔하다.
    """
    res = {"primary": None, "counts": {}, "mixed": False, "origin": None, "evidence": []}
    counts = {}
    for rel in list(source_roots or []) + list(test_roots or []):
        base = os.path.join(root, rel)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, files in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for f in files:
                lang = _LANG_EXT.get(os.path.splitext(f)[1])
                if lang:
                    counts[lang] = counts.get(lang, 0) + 1

    counts.pop("groovy", None)          # 대개 빌드 스크립트다 — 주 언어 후보에서 뺀다
    if counts:
        res["counts"] = counts
        ordered = sorted(counts.items(), key=lambda kv: -kv[1])
        res["primary"] = ordered[0][0]
        res["origin"] = "source-files"
        total = sum(counts.values())
        minority = total - ordered[0][1]
        res["mixed"] = len(ordered) > 1 and minority / total >= 0.10
        res["evidence"].append(
            "소스 파일 표본: " + ", ".join("%s %d개" % (k, v) for k, v in ordered))
        return res

    # 소스가 하나도 없다(greenfield) → 빌드 스크립트 플러그인으로 추정
    for lang, rx in _LANG_PLUGIN_RES:
        if rx.search(blob):
            res["primary"] = lang
            res["origin"] = "build-plugin"
            res["evidence"].append("빌드 스크립트 플러그인: " + lang)
            return res

    # 그것도 없으면 소스 디렉터리 이름 (src/main/kotlin vs src/main/java)
    for rel in source_roots or []:
        for lang in ("kotlin", "java"):
            if os.path.isdir(os.path.join(root, rel, lang)):
                res["primary"] = lang
                res["origin"] = "source-dir"
                res["evidence"].append("소스 디렉터리: %s/%s" % (rel, lang))
                return res
    return res


def detect_spring(blob: str) -> dict:
    """빌드 스크립트에서 Spring Boot 여부와 웹 스택을 판정한다."""
    res = {"boot": False, "version": None, "web": None, "evidence": []}
    if "org.springframework.boot" in blob or "spring-boot-starter" in blob:
        res["boot"] = True
        res["evidence"].append("spring-boot plugin/starter")
        m = re.search(r"""org\.springframework\.boot['"]?\s*\)?\s*version\s*['"]([0-9][^'"]*)['"]""", blob)
        if not m:
            m = re.search(r"""springBootVersion\s*=\s*['"]([0-9][^'"]*)['"]""", blob)
        if m:
            res["version"] = m.group(1)
    if "spring-boot-starter-webflux" in blob:
        res["web"] = "webflux"
    elif "spring-boot-starter-web" in blob:
        res["web"] = "mvc"
    return res


def detect_restdocs(root: str, blob: str, modules) -> dict:
    """Spring REST Docs 사용 여부와 스니펫 출력 위치를 판정한다."""
    res = {"available": False, "flavor": None, "snippets_dirs": [],
           "asciidoctor": False, "openapi": False, "evidence": []}
    if "spring-restdocs" not in blob and "restdocs" not in blob:
        return res
    res["available"] = True
    res["evidence"].append("spring-restdocs 의존성")
    for flavor, hints in _RESTDOCS_FLAVORS:
        if any(h in blob for h in hints):
            res["flavor"] = flavor
            break
    if "org.asciidoctor" in blob:
        res["asciidoctor"] = True
        res["evidence"].append("asciidoctor 플러그인")
    if "restdocs-api-spec" in blob or "openapi3" in blob:
        res["openapi"] = True
        res["evidence"].append("restdocs-api-spec (OpenAPI)")

    # snippetsDir 재정의를 찾는다: `val snippetsDir = file("build/…")` 등
    dirs = []
    for m in re.finditer(r"""snippets?Dir\s*(?:=|by)\s*[^\n]*?['"]([^'"]+)['"]""", blob):
        dirs.append(m.group(1).strip("/"))
    bases = [""] + [str(mod).replace(":", os.sep) for mod in (modules or [])]
    for b in bases:
        for d in (dirs or [_DEFAULT_SNIPPETS_DIR]):
            rel = (b + "/" + d).lstrip("/") if b else d
            if rel not in res["snippets_dirs"]:
                res["snippets_dirs"].append(rel)
    return res


# 널리 쓰이는 레이어 이름 — 이 중 **둘 이상**의 디렉터리가 있어야 "레이어를 갖췄다" 고 본다.
# 하나만 있는 경우(예: 파일 하나짜리 프로젝트에 우연히 `service/` 폴더 하나)까지 강제하면
# 강제할 경계 자체가 없는 프로젝트에도 ArchUnit 을 들이미는 과잉이 된다.
_ARCH_LAYER_NAMES = {
    "controller", "service", "repository", "domain", "application",
    "infrastructure", "usecase", "dao", "adapter", "facade",
}


def detect_layered_packages(root: str, source_roots) -> set:
    """소스 루트 아래에서 레이어 이름 디렉터리를 찾는다(내용 없이 디렉터리 존재만 본다)."""
    found = set()
    for sr in source_roots or []:
        base = os.path.join(root, sr)
        if not os.path.isdir(base):
            continue
        for _dirpath, dirnames, _files in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for d in dirnames:
                if d.lower() in _ARCH_LAYER_NAMES:
                    found.add(d.lower())
    return found


def detect_archunit(blob: str) -> dict:
    """ArchUnit(`com.tngtech.archunit`) 의존성 사용 여부를 판정한다."""
    res = {"available": False, "evidence": []}
    if "archunit" not in blob.lower():
        return res
    res["available"] = True
    res["evidence"].append("archunit 의존성")
    return res


def _read(path, limit=400000):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except Exception:
        return ""


def detect_build() -> dict:
    root = project_dir()
    res = {
        "tool": "gradle", "status": "unknown", "detected_at": now(),
        "wrapper": None, "invoke": None, "dsl": None, "multi_module": False,
        "modules": [], "version_catalog": None,
        "test_task": None, "check_task": None, "extra_test_tasks": [],
        "language": {"primary": None, "counts": {}, "mixed": False},
        "test_framework": None, "test_frameworks": [], "test_libs": [],
        "test_framework_suggested": None,
        "spring": {"boot": False, "version": None, "web": None},
        "restdocs": {"available": False, "flavor": None, "snippets_dirs": []},
        "api_docs": {"style": None, "required": False},
        "archunit": {"available": False},
        "arch_rules": {"style": None, "required": False},
        "layers_detected": [],
        "source_roots": [], "test_roots": [],
        "evidence": [], "blockers": [],
    }

    has_wrapper = os.path.isfile(os.path.join(root, "gradlew"))
    settings_g = os.path.join(root, "settings.gradle")
    settings_k = os.path.join(root, "settings.gradle.kts")
    build_g = os.path.join(root, "build.gradle")
    build_k = os.path.join(root, "build.gradle.kts")

    settings_file = settings_k if os.path.isfile(settings_k) else (
        settings_g if os.path.isfile(settings_g) else None)
    build_file = build_k if os.path.isfile(build_k) else (
        build_g if os.path.isfile(build_g) else None)

    if not settings_file and not build_file and not has_wrapper:
        res["status"] = "missing"
        res["blockers"].append(
            "Gradle 빌드 파일(settings.gradle[.kts] / build.gradle[.kts])도 wrapper 도 없다. "
            "신규 프로젝트라면 N3 계획에 Gradle 세팅을 포함하고 사용자 승인을 받아야 한다.")
        return res

    if has_wrapper:
        res["wrapper"] = "./gradlew"
        res["invoke"] = "./gradlew"
        res["evidence"].append("gradlew 존재")
        if not os.access(os.path.join(root, "gradlew"), os.X_OK):
            res["blockers"].append("gradlew 에 실행 권한이 없다 (chmod +x gradlew 필요).")
    else:
        from shutil import which
        if which("gradle"):
            res["invoke"] = "gradle"
            res["evidence"].append("wrapper 없음 → PATH 의 gradle 사용")
        else:
            res["blockers"].append(
                "gradle wrapper 도 없고 PATH 에 gradle 도 없다. 실행 명령을 구성할 수 없다.")

    res["dsl"] = "kotlin" if (settings_file or "").endswith(".kts") or \
                             (build_file or "").endswith(".kts") else "groovy"

    if settings_file:
        text = _read(settings_file)
        res["evidence"].append(os.path.basename(settings_file))
        mods = []
        for m in re.finditer(r"""include\s*\(?\s*((?:['"][^'"]+['"]\s*,?\s*)+)\)?""", text):
            for q in re.finditer(r"""['"]([^'"]+)['"]""", m.group(1)):
                mods.append(q.group(1).lstrip(":"))
        seen, ordered = set(), []
        for m in mods:
            if m not in seen:
                seen.add(m)
                ordered.append(m)
        res["modules"] = ordered
        res["multi_module"] = len(ordered) > 0

    catalog = os.path.join(root, "gradle", "libs.versions.toml")
    if os.path.isfile(catalog):
        res["version_catalog"] = "gradle/libs.versions.toml"
        res["evidence"].append("version catalog")

    # 빌드 스크립트 전체를 모아 태스크/프레임워크를 판정
    scripts = [p for p in (settings_file, build_file) if p]
    for mod in res["modules"]:
        modpath = mod.replace(":", os.sep)
        for name in ("build.gradle.kts", "build.gradle"):
            cand = os.path.join(root, modpath, name)
            if os.path.isfile(cand):
                scripts.append(cand)
    blob = "\n".join(_read(p) for p in scripts)

    for fw, hints in _TEST_FW_HINTS:
        if any(h in blob for h in hints):
            res["test_frameworks"].append(fw)
    if res["test_frameworks"]:
        # 목록의 앞쪽이 더 구체적이다 — 첫 번째가 주 프레임워크
        res["test_framework"] = res["test_frameworks"][0]
        res["evidence"].append("test framework: " + ", ".join(res["test_frameworks"]))
    res["test_libs"] = [lib for lib, hints in _TEST_LIB_HINTS
                        if any(h in blob for h in hints)]

    res["spring"] = detect_spring(blob)
    res["restdocs"] = detect_restdocs(root, blob, res["modules"])
    res["archunit"] = detect_archunit(blob)
    res["evidence"].extend(res["spring"].get("evidence", []))
    res["evidence"].extend(res["restdocs"].get("evidence", []))
    res["evidence"].extend(res["archunit"].get("evidence", []))

    # API 문서화 정책 — Spring Boot 웹 프로젝트면 REST Docs 가 **항상** 필수다.
    # spring-restdocs 의존성이 아직 없다는 사실이 요구사항을 면제하지 않는다 — 도입 자체가
    # 이번 티켓의 일이 된다(§5, N3 계획에 반드시 포함, 생략 불가).
    if res["spring"]["boot"]:
        if res["restdocs"]["available"]:
            res["api_docs"] = {"style": "spring-restdocs", "required": True}
        else:
            res["api_docs"] = {"style": "spring-restdocs", "required": True,
                               "note": "Spring Boot 인데 spring-restdocs 의존성이 아직 없다. "
                                       "API 를 건드리는 티켓이면 이번 티켓에서 반드시 도입한다 "
                                       "(N3 계획에 포함 필수, GATE-PLAN 승인 후 N4 에서 세팅)."}

    extra = set()
    for m in re.finditer(r"""(?:tasks\.register|task)\s*[(<]?\s*['"]?([A-Za-z0-9_]+)['"]?[^\n]*Test""", blob):
        name = m.group(1)
        if name and name != "test":
            extra.add(name)
    for m in re.finditer(r"""register<Test>\s*\(\s*['"]([A-Za-z0-9_]+)['"]""", blob):
        extra.add(m.group(1))
    res["extra_test_tasks"] = sorted(extra)

    cfg = config()
    res["test_task"] = dget(cfg, "build.default_test_task", "test")
    res["check_task"] = dget(cfg, "build.default_check_task", "check")

    # 소스/테스트 루트
    bases = [""] + [m.replace(":", os.sep) for m in res["modules"]]
    for b in bases:
        for kind, key in (("main", "source_roots"), ("test", "test_roots")):
            d = os.path.join(root, b, "src", kind) if b else os.path.join(root, "src", kind)
            if os.path.isdir(d):
                res[key].append(norm_rel(d))
        for extra_kind in ("integrationTest", "testFixtures"):
            d = os.path.join(root, b, "src", extra_kind) if b else os.path.join(root, "src", extra_kind)
            if os.path.isdir(d):
                res["test_roots"].append(norm_rel(d))

    # 언어 판정은 source_roots/test_roots 가 채워진 **뒤에** 한다
    res["language"] = detect_language(root, blob, res["source_roots"], res["test_roots"])
    res["evidence"].extend(res["language"].get("evidence", []))

    # 아키텍처 규칙(ArchUnit) 정책 — 레이어를 이미 갖춘 기존 JVM 프로젝트면 **항상** 필수다.
    # archunit 의존성이 아직 없다는 사실이 요구사항을 면제하지 않는다 — 도입 자체가
    # 이번 티켓의 일이 된다(`references/archunit.md` §5, N3 계획에 반드시 포함, 생략 불가).
    # greenfield(소스가 아직 없음)·JVM 이 아님·레이어가 없음(디렉터리 구조로 실측) 이면
    # 강제하지 않는다 — 강제할 경계 자체가 없다.
    layers = detect_layered_packages(root, res["source_roots"])
    res["layers_detected"] = sorted(layers)
    is_layered_jvm_existing = (res["language"]["primary"] in ("kotlin", "java")) and \
        bool(res["source_roots"] or res["test_roots"]) and len(layers) >= 2
    if is_layered_jvm_existing:
        if res["archunit"]["available"]:
            res["arch_rules"] = {"style": "archunit", "required": True}
        else:
            res["arch_rules"] = {"style": "archunit", "required": True,
                                 "note": "레이어가 있는 기존 JVM 프로젝트인데 ArchUnit 의존성이 "
                                         "아직 없다. 아키텍처·패키지 구조를 건드리는 티켓이면 "
                                         "이번 티켓에서 반드시 도입한다 "
                                         "(N3 계획에 포함 필수, GATE-PLAN 승인 후 N4 에서 세팅)."}
    else:
        res["arch_rules"] = {"style": "archunit", "required": False}

    # 탐지된 프레임워크가 없을 때만(주로 greenfield) 언어 기본값을 **제안**한다.
    # `test_framework` 은 사실이고 이것은 제안이다 — 절대 섞지 않는다.
    if not res["test_framework"]:
        by_lang = dget(cfg, "tdd.default_test_framework_by_language", {}) or {}
        res["test_framework_suggested"] = by_lang.get(res["language"]["primary"])

    if res["blockers"]:
        res["status"] = "blocked"
    elif not res["source_roots"] and not res["test_roots"]:
        res["status"] = "ambiguous"
        res["blockers"].append(
            "Gradle 프로젝트는 있으나 src/main·src/test 를 찾지 못했다. 소스 레이아웃을 사용자에게 확인하라.")
    else:
        res["status"] = "detected"
    return res


# --------------------------------------------------------------------------
# TDD 판정
# --------------------------------------------------------------------------

def classify_path(rel_path: str, st=None):
    """'test' | 'prod' | 'exempt' 로 분류한다."""
    cfg = config()
    if glob_any(rel_path, dget(cfg, "tdd.exempt_globs", [])):
        return "exempt"
    if glob_any(rel_path, dget(cfg, "tdd.test_path_patterns", [])):
        return "test"
    if st:
        for tr in dget(st, "runtime.build.test_roots", []) or []:
            if rel_path.startswith(tr.rstrip("/") + "/"):
                return "test"
        roots = dget(st, "runtime.build.source_roots", []) or []
        if roots and not any(rel_path.startswith(r.rstrip("/") + "/") for r in roots):
            # 탐지된 소스 루트 밖이면 프로덕션 코드로 강제하지 않는다
            return "exempt"
    return "prod"


# --------------------------------------------------------------------------
# API 문서화 (Spring Boot + Spring REST Docs)
# --------------------------------------------------------------------------

# 컨트롤러 판정은 파일명이 아니라 **내용**으로 한다. 파일명 규칙은 프로젝트마다 다르다.
_CONTROLLER_MARKERS = (
    "@RestController", "@Controller", "@RequestMapping",
    "@GetMapping", "@PostMapping", "@PutMapping", "@DeleteMapping", "@PatchMapping",
    "RouterFunction", "coRouter(", "RouterFunctions.route",
)
# 테스트가 REST Docs 스니펫을 실제로 만들고 있는지 판정하는 표지
_RESTDOCS_MARKERS = (
    "document(", "RestDocumentation", "restDocs", "andDocument",
    "MockMvcRestDocumentation", "WebTestClientRestDocumentation",
    "RestAssuredRestDocumentation", "RestDocumentationExtension",
    "documentWithResource",           # restdocs-api-spec
)


def is_controller_source(rel_path: str, root=None) -> bool:
    """프로덕션 소스가 HTTP 엔드포인트를 정의하는지 내용으로 판정한다."""
    if not re.search(r"\.(kt|java|kts)$", rel_path or ""):
        return False
    full = os.path.join(root or project_dir(), rel_path)
    if not os.path.isfile(full):
        return False
    text = _read(full, 200000)
    return any(mk in text for mk in _CONTROLLER_MARKERS)


def test_documents_api(rel_path: str, root=None) -> bool:
    """테스트 파일이 REST Docs 스니펫을 만드는지 판정한다."""
    full = os.path.join(root or project_dir(), rel_path)
    if not os.path.isfile(full):
        return False
    text = _read(full, 200000)
    return any(mk in text for mk in _RESTDOCS_MARKERS)


def count_snippets(st, root=None) -> int:
    """생성된 REST Docs 스니펫(.adoc/.json) 개수를 센다."""
    root = root or project_dir()
    total = 0
    for d in dget(st, "runtime.build.restdocs.snippets_dirs", []) or []:
        base = os.path.join(root, d)
        if not os.path.isdir(base):
            continue
        for _dirpath, _dirnames, files in os.walk(base):
            total += sum(1 for f in files if f.endswith((".adoc", ".json")))
    return total


def api_docs_applicable(st) -> bool:
    """Spring Boot 면 항상 적용된다 — `restdocs.available` 은 더 이상 조건이 아니다.
    REST Docs 의존성이 아직 없는 것은 '아직 도입 안 한 상태'일 뿐, 요구사항을 면제하지 않는다.
    (의존성이 없으면 `cmd_apidocs` 의 실측에서 컨트롤러가 전부 `missing` 으로 잡혀
    도입 자체를 강제하는 효과를 낸다.)
    """
    cfg = config()
    if dget(cfg, "api_docs.mode", "block") == "off":
        return False
    return bool(dget(st, "runtime.build.spring.boot"))


def unresolved_api_docs(st):
    """문서화되지 않은 채 남은 변경 컨트롤러 목록."""
    exempt = {e.get("path") for e in dget(st, "api_docs.exemptions", []) or []}
    return [m for m in dget(st, "api_docs.missing", []) or [] if m not in exempt]


# --------------------------------------------------------------------------
# 아키텍처 규칙 (ArchUnit) — 레이어를 갖춘 기존 JVM 프로젝트
# --------------------------------------------------------------------------

# 테스트 파일이 실제로 ArchUnit 규칙을 정의하고 있는지 판정하는 표지
_ARCHUNIT_MARKERS = (
    "com.tngtech.archunit", "ArchRuleDefinition", "@AnalyzeClasses", "ArchRule",
    "layeredArchitecture(", "noClasses()", "classes()",
)


def test_defines_arch_rule(rel_path: str, root=None) -> bool:
    """테스트 파일이 ArchUnit 규칙(레이어·의존방향·네이밍 등)을 정의하는지 판정한다."""
    full = os.path.join(root or project_dir(), rel_path)
    if not os.path.isfile(full):
        return False
    text = _read(full, 200000)
    return any(mk in text for mk in _ARCHUNIT_MARKERS)


def archunit_rule_files(st, root=None):
    """`runtime.build.test_roots` 안에서 실제 ArchUnit 규칙 파일을 찾는다(파일시스템 실측)."""
    root = root or project_dir()
    hits = []
    for tr in dget(st, "runtime.build.test_roots", []) or []:
        base = os.path.join(root, tr)
        if not os.path.isdir(base):
            continue
        for dirpath, _dirnames, files in os.walk(base):
            for f in files:
                if not re.search(r"\.(kt|java)$", f):
                    continue
                rel = norm_rel(os.path.join(dirpath, f))
                if test_defines_arch_rule(rel, root):
                    hits.append(rel)
    return hits


def arch_rules_applicable(st) -> bool:
    """레이어를 갖춘 기존 JVM 프로젝트면 항상 적용된다 — `archunit.available` 은 조건이 아니다.
    ArchUnit 의존성이 아직 없는 것은 '아직 도입 안 한 상태'일 뿐, 요구사항을 면제하지 않는다.
    (의존성이 없으면 `cmd_archunit` 의 실측에서 이번 시도의 프로덕션 변경이 그대로 `missing` 으로
    잡혀 도입 자체를 강제하는 효과를 낸다.)
    """
    cfg = config()
    if dget(cfg, "arch_rules.mode", "block") == "off":
        return False
    return bool(dget(st, "runtime.build.arch_rules.required"))


def unresolved_arch_rules(st) -> bool:
    """이번 티켓에 아키텍처 규칙 미해소가 남아 있는지."""
    if dget(st, "arch_rules.exemptions", []):
        return False
    return bool(dget(st, "arch_rules.missing"))


def tdd_current_attempt(st, create=False):
    attempts = dget(st, "tdd.attempts", []) or []
    if not attempts:
        if not create:
            return None
        attempts.append({"attempt": 1, "started_at": now(), "test_writes": [],
                         "prod_writes": [], "violations": [], "exemptions": []})
        dset(st, "tdd.attempts", attempts)
    return attempts[-1]


def tdd_verdict(st, rel_path):
    """(decision, reason) — decision in {'allow','deny','warn'}"""
    mode = dget(st, "tdd.mode", "block")
    if mode == "off":
        return "allow", ""
    kind = classify_path(rel_path, st)
    if kind in ("test", "exempt"):
        return "allow", ""
    att = tdd_current_attempt(st)
    if att and (att.get("test_writes") or []):
        return "allow", ""
    if att:
        for ex in att.get("exemptions", []) or []:
            if ex.get("path") == rel_path:
                return "allow", ""
    reason = (
        "TDD Red-First 위반: 이번 N4 시도에서 아직 테스트 파일을 하나도 작성하지 않았는데 "
        "프로덕션 소스 '%s' 를 쓰려고 한다.\n"
        "  → 실패하는 테스트를 먼저 작성하라(Red). 테스트 파일을 한 번이라도 쓰면 이후 쓰기는 열린다.\n"
        "  → 이 파일이 테스트 대상이 아니라면: .claude/graph/bin/ge tdd exempt --path '%s' --reason '<사유>'"
        % (rel_path, rel_path)
    )
    return ("deny" if mode == "block" else "warn"), reason


# --------------------------------------------------------------------------
# 게이트
# --------------------------------------------------------------------------

def gate_approval(st, gate):
    """가장 최근의 해당 게이트 승인 레코드를 돌려준다(반려는 무시하지 않고 그대로 반환)."""
    latest = None
    for rec in dget(st, "gates.approvals", []) or []:
        if rec.get("gate") == gate:
            latest = rec
    return latest


def gate_is_approved(st, gate, artifact_hash=None, require_hash=False):
    """require_hash=True 면 대조할 해시가 **없는 것 자체**를 무효로 본다.

    티켓 수정(AMEND)으로 `plan.hash` 가 비워지면 대조 대상이 사라진다. 그때 해시 검사를
    건너뛰면 **낡은 GATE-PLAN 승인이 그대로 살아남아** 무효화된 계획으로 코드를 쓸 수 있다.
    """
    rec = gate_approval(st, gate)
    if not rec or rec.get("decision") != "approved":
        return False, "게이트 %s 에 대한 사용자 승인 기록이 없다." % gate
    if rec.get("source") != "hook:UserPromptSubmit":
        return False, "게이트 %s 의 승인 기록이 훅이 아닌 경로로 작성됐다(위조 의심). 무효." % gate
    if require_hash and not artifact_hash:
        return False, ("게이트 %s 의 승인 대상이 사라졌다(계획이 없거나 티켓 수정으로 무효화됨). "
                       "새 산출물을 제시하고 다시 승인받아야 한다." % gate)
    if artifact_hash and rec.get("artifact_hash") != artifact_hash:
        return False, (
            "게이트 %s 는 승인됐지만 승인 대상이 바뀌었다.\n  승인된 해시: %s\n  현재 해시:   %s\n"
            "  → 바뀐 내용을 다시 제시하고 재승인을 받아야 한다."
            % (gate, rec.get("artifact_hash"), artifact_hash))
    return True, ""


# --------------------------------------------------------------------------
# JIRA 상태 전이 — 노드 본 작업 **이전에**, **매 진입마다** 강제한다
# --------------------------------------------------------------------------

def norm_status(name) -> str:
    """상태 이름 비교용 정규화(NFC + 공백 제거 + 소문자). config.jira.match_strategy 와 같은 규칙."""
    return unicodedata.normalize("NFC", str(name or "")).replace(" ", "").strip().lower()


def project_key(st) -> str:
    ticket = (st or {}).get("ticket") or ""
    return (ticket.split("-")[0] if "-" in ticket else ticket).upper()


def transition_policy(cfg=None) -> dict:
    return dget(cfg or config(), "jira.transition_policy", {}) or {}


def transition_mode(cfg=None) -> str:
    """block | warn | off"""
    return str(transition_policy(cfg).get("enforce") or "block").lower()


def transition_nodes(cfg=None) -> dict:
    """{노드: status_targets 키}. 여기 실린 노드만 전이를 강제받는다."""
    return transition_policy(cfg).get("nodes") or {}


def status_target(st, node, cfg=None):
    """노드의 목표 상태 이름. 프로젝트 override 가 전역 status_targets 보다 우선한다."""
    cfg = cfg or config()
    key = transition_nodes(cfg).get(node)
    if not key:
        return None
    proj = project_key(st)
    return (dget(cfg, "jira.project_overrides.%s.status_targets.%s" % (proj, key))
            or dget(cfg, "jira.status_targets.%s" % key))


def known_status_id(st, name, cfg=None):
    """config 의 known_status_ids 에서 상태 id 를 찾는다(정규화 비교). 없으면 None."""
    cfg = cfg or config()
    ids = dget(cfg, "jira.project_overrides.%s.known_status_ids" % project_key(st), {}) or {}
    want = norm_status(name)
    for k, v in ids.items():
        if norm_status(k) == want:
            return v
    return None


def last_transition(st, node):
    for rec in reversed(dget(st, "jira.transitions_applied", []) or []):
        if rec.get("node") == node:
            return rec
    return None


def pending_transition(st, node):
    """이번 진입을 위해 기록됐고 아직 소비되지 않은 전이 레코드."""
    rec = last_transition(st, node)
    return None if (rec is None or rec.get("consumed")) else rec


def _transition_howto(st, node, target) -> str:
    sid = known_status_id(st, target)
    return (
        "  → 노드를 실행하기 **전에** 티켓을 '%s'%s 로 전이하고 기록해야 한다.\n"
        "    1. mcp__atlassian__getTransitionsForJiraIssue 로 목록을 받는다\n"
        "    2. transitions[].to.name 이 '%s' 인 것을 고른다 (transition 의 name 으로 고르지 마라)\n"
        "    3. mcp__atlassian__transitionJiraIssue 로 실제 전이한다\n"
        "    4. ge jira-transition --node %s --from \"<이전 상태>\" "
        "--transition-id <id> --transition-name \"<이름>\"\n"
        "       (조회 결과 이미 '%s' 였으면 --already 로 기록한다)"
        % (target, (" (id %s)" % sid) if sid else "", target, node, target))


def consume_transition(st, node):
    """노드 진입 시 호출. 이번 진입용 전이 기록이 없으면 진입을 거부한다.

    **첫 진입만이 아니라 매 진입마다** 필요하다 — 레코드는 진입할 때 소비되므로
    N5 → N4 루프로 되돌아오면 다시 전이해서 새로 기록해야 한다.
    """
    target = status_target(st, node)
    mode = transition_mode()
    if not target or mode == "off":
        return
    rec = pending_transition(st, node)
    if rec is None or norm_status(rec.get("to_status")) != norm_status(target):
        cur = dget(st, "jira.current_status") or "?"
        msg = ("%s 진입 거부 — 이번 진입에 대한 JIRA 상태 전이 기록이 없다 "
               "(현재 상태: %s, 목표: %s).\n%s"
               % (node, cur, target, _transition_howto(st, node, target)))
        if mode == "warn":
            sys.stderr.write("[ge] 경고: " + msg + "\n")
            return
        die(msg, 2)
    entries = dget(st, "jira.node_entries", {}) or {}
    n = int(entries.get(node) or 0) + 1
    entries[node] = n
    dset(st, "jira.node_entries", entries)
    rec["consumed"] = True
    rec["entry"] = n
    rec["consumed_at"] = now()
    dset(st, "jira.current_status", rec.get("to_status"))


def require_transitioned(st, node):
    """노드의 **현재 진입**이 목표 상태 전이를 마쳤는지 확인한다(산출물 기록용 백스톱)."""
    target = status_target(st, node)
    mode = transition_mode()
    if not target or mode == "off":
        return
    entries = dget(st, "jira.node_entries", {}) or {}
    n = int(entries.get(node) or 0)
    rec = last_transition(st, node)
    ok = (n > 0 and rec is not None and rec.get("consumed")
          and rec.get("entry") == n
          and norm_status(rec.get("to_status")) == norm_status(target))
    if ok:
        return
    msg = ("%s 산출물을 기록할 수 없다 — 이번 진입의 JIRA 상태 전이가 확인되지 않는다 "
           "(목표: %s).\n%s" % (node, target, _transition_howto(st, node, target)))
    if mode == "warn":
        sys.stderr.write("[ge] 경고: " + msg + "\n")
        return
    die(msg, 2)


# --------------------------------------------------------------------------
# 서브커맨드
# --------------------------------------------------------------------------

def cmd_init(a):
    os.makedirs(state_dir(), exist_ok=True)
    os.makedirs(logs_dir(), exist_ok=True)
    ticket = a.ticket.strip()
    if not re.match(r"^[A-Z][A-Z0-9]+-[0-9]+$", ticket):
        die("티켓 키 형식이 아니다: %r (예: PPS-283)" % ticket)
    existing = load(ticket)
    if existing and not a.force:
        die("이미 %s 상태 파일이 있다. 이어가려면 `ge resume --ticket %s`, 새로 시작하려면 --force."
            % (ticket, ticket))
    st = new_state(ticket, a.branch or ticket, a.base_branch)
    rc, out, _ = git("rev-parse", "HEAD")
    st["git"]["head_sha_at_start"] = out if rc == 0 else None
    rc, out, _ = git("rev-parse", a.base_branch)
    st["git"]["base_sha"] = out if rc == 0 else None
    rc, out, _ = git("status", "--porcelain")
    st["git"]["worktree_clean_at_start"] = (rc == 0 and out == "")
    st["runtime"]["graphify"] = detect_graphify()
    log_event(st, "N0", "enter", "graph 시작")
    save(st)
    atomic_write(pointer_path(), ticket + "\n")
    print(json.dumps({"ok": True, "ticket": ticket,
                      "state": norm_rel(state_path(ticket)),
                      "graphify": st["runtime"]["graphify"]},
                     ensure_ascii=False, indent=2))


def cmd_show(a):
    st = load(a.ticket)
    if not st:
        print(json.dumps({"active": False}, ensure_ascii=False))
        return
    if a.field:
        val = dget(st, a.field, None)
        print(val if isinstance(val, str) else json.dumps(val, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(st, ensure_ascii=False, indent=2))


def _gate_line(st, gate):
    rec = gate_approval(st, gate)
    if not rec:
        return "미승인"
    return "%s (%s)" % (rec.get("decision"), rec.get("at", "")[:19])


def cmd_status(a):
    st = load(a.ticket)
    if not st:
        print("Graph Engineering: 비활성 (상태 파일 없음). 평소 작업에 아무 영향 없음.")
        return
    b = dget(st, "runtime.build", {}) or {}
    gp = dget(st, "gates.pending")
    lines = [
        "Graph Engineering: 활성",
        "  티켓        : %s  (%s)" % (st["ticket"], dget(st, "jira.url") or "-"),
        "  브랜치      : %s  (base: %s, 현재: %s)" % (
            dget(st, "git.branch"), dget(st, "git.base_branch"), current_branch() or "?"),
        "  현재 노드   : %s [%s]" % (st.get("current_node"), st.get("node_status")),
        "  JIRA 상태   : %s  (진입 시: %s, 전이 %d회)" % (
            dget(st, "jira.current_status") or "-",
            dget(st, "jira.status_at_entry") or "-",
            len(dget(st, "jira.transitions_applied", []) or [])),
        "  graphify    : %s" % ("사용 (graphify skill 최우선)"
                                if dget(st, "runtime.graphify.available") else "미사용 (일반 탐색)"),
        "  빌드        : %s / invoke=%s / test=%s / fw=%s" % (
            b.get("status"), b.get("invoke"), b.get("test_task"), b.get("test_framework")),
        "  TDD         : mode=%s, 시도 %d회" % (
            dget(st, "tdd.mode"), len(dget(st, "tdd.attempts", []) or [])),
        "  게이트      : PLAN=%s | REVIEW=%s | COMMIT=%s%s" % (
            _gate_line(st, "GATE-PLAN"), _gate_line(st, "GATE-REVIEW"), _gate_line(st, "GATE-COMMIT"),
            (" | TICKET=%s" % _gate_line(st, "GATE-TICKET"))
            if gate_approval(st, "GATE-TICKET") else ""),
        "  재시도      : N5→N4 %d/%d, N6→N4 %d/%d" % (
            dget(st, "retries.n5_to_n4", 0), dget(st, "retries.limits.n5_to_n4", 3),
            dget(st, "retries.n6_to_n4", 0), dget(st, "retries.limits.n6_to_n4", 2)),
        "  테스트      : %d회 실행, 마지막 exit=%s" % (
            len(dget(st, "tests.attempts", []) or []),
            (dget(st, "tests.attempts", []) or [{}])[-1].get("exit_code") if dget(st, "tests.attempts") else "-"),
        "  변경 파일   : %d개" % len(st.get("changed_files") or []),
        "  커밋        : %s" % (dget(st, "commit.hash") or "-"),
    ]
    if api_docs_applicable(st):
        lines.append("  API 문서화  : 컨트롤러 %d개 변경 / 문서화 %d / 미해소 %d (스니펫 %d)" % (
            len(dget(st, "api_docs.controllers_changed", []) or []),
            len(dget(st, "api_docs.documented", []) or []),
            len(unresolved_api_docs(st)),
            dget(st, "api_docs.snippets_found", 0) or 0))
    if arch_rules_applicable(st):
        lines.append("  아키텍처규칙: ArchUnit %s / 프로덕션 변경 %d개 / 규칙 파일 %d개 / 미해소 %s" % (
            "도입됨" if dget(st, "runtime.build.archunit.available") else "미도입",
            len(dget(st, "arch_rules.prod_changed", []) or []),
            len(dget(st, "arch_rules.rule_files", []) or []),
            "예" if unresolved_arch_rules(st) else "아니오"))
    ams = dget(st, "ticket_amendments", []) or []
    if ams:
        applied = [a for a in ams if a.get("applied_at")]
        lines.append("  티켓 수정   : 제안 %d건 / 반영 %d건 (AMEND %d/%d회)" % (
            len(ams), len(applied),
            dget(st, "retries.gate_to_amend", 0),
            dget(st, "retries.limits.gate_to_amend", 2)))
        last = ams[-1]
        lines.append("     최근: [%s 반려] %s → %s" % (
            last.get("from_gate") or "?", (last.get("reject_reason") or "")[:50],
            "반영됨" if last.get("applied_at") else "승인 대기"))
    if dget(st, "runtime.graphify.available"):
        gu = dget(st, "graphify_update", {}) or {}
        lines.append("  지식 그래프 : N10 %s%s" % (
            gu.get("status") or "미실행",
            (" (변경 %s, delta=%s)" % (gu.get("changed"), json.dumps(gu.get("delta") or {})))
            if gu.get("status") else ""))
    if st.get("halt_reason"):
        lines.append("  ** 정지 사유: %s" % st["halt_reason"])
    if gp:
        lines.append("")
        lines.append("  ⏸ 승인 대기: %s  code=%s" % (gp.get("gate"), gp.get("code")))
        lines.append("     %s" % (gp.get("summary") or ""))
        lines.append("     승인: 채팅에 `approve` 또는 `승인` 입력  /  반려: `reject <사유>`")
    print("\n".join(lines))


def cmd_node(a):
    st = load(a.ticket)
    if not st:
        die("활성 그래프가 없다.")
    if a.node not in NODE_ORDER:
        die("알 수 없는 노드: %s (%s)" % (a.node, ", ".join(NODE_ORDER)))
    # N4 진입은 GATE-PLAN 승인을 요구한다 (결정론적 가드)
    if a.node == "N4" and st.get("current_node") in ("N0", "N1", "N2", "N3", "AMEND"):
        ok, why = gate_is_approved(st, "GATE-PLAN", dget(st, "plan.hash"), require_hash=True)
        if not ok:
            die("N4 진입 거부 — " + why, 2)
    if a.node == "N7":
        ok, why = gate_is_approved(st, "GATE-REVIEW")
        if not ok:
            die("N7 진입 거부 — " + why, 2)
    # DONE 으로 갈 때, graphify 가 있는데 최신화 기록이 없으면 알려준다(차단은 아니다)
    if a.node == "DONE" and dget(st, "runtime.graphify.available") \
            and not dget(st, "graphify_update.status"):
        sys.stderr.write(
            "[ge] 경고: graphify-out 이 있는데 N10(지식 그래프 최신화) 기록이 없다.\n"
            "  → `ge node N10 --status running` 후 graphify 를 `--update` 로 돌리고\n"
            "    `ge record-graphify --status ok|skipped|failed` 로 기록하라.\n")

    # 노드 본 작업 **이전에** JIRA 상태 전이가 끝나 있어야 한다 (매 진입마다).
    # 전이 기록을 여기서 소비하므로, 되돌아온 진입은 다시 전이해야 통과한다.
    if a.status == "running":
        consume_transition(st, a.node)

    prev = st.get("current_node")
    st["current_node"] = a.node
    st["node_status"] = a.status
    if a.status == "escalated":
        st["halt_reason"] = a.note or "에스컬레이션"
    log_event(st, a.node, a.status, a.note or ("from " + str(prev)))
    if a.node == "N4" and a.status == "running":
        attempts = dget(st, "tdd.attempts", []) or []
        attempts.append({"attempt": len(attempts) + 1, "started_at": now(),
                         "test_writes": [], "prod_writes": [], "violations": [], "exemptions": []})
        dset(st, "tdd.attempts", attempts)
    save(st)
    print("[ge] node=%s status=%s (prev=%s)" % (a.node, a.status, prev))


def _payload(a):
    if a.file:
        with open(a.file, encoding="utf-8") as fh:
            raw = fh.read()
        return json.loads(raw) if a.json_value else raw
    if a.value is not None:
        return json.loads(a.value) if a.json_value else a.value
    raw = sys.stdin.read()
    return json.loads(raw) if a.json_value else raw


def cmd_put(a):
    st = load(a.ticket)
    if not st:
        die("활성 그래프가 없다.")
    if a.path.startswith("gates.approvals") or a.path == "gates":
        die("승인 레코드는 CLI 로 쓸 수 없다. 사용자가 채팅에 승인을 입력해야 훅이 기록한다.", 2)
    dset(st, a.path, _payload(a))
    save(st)
    print("[ge] put %s" % a.path)


def cmd_append(a):
    st = load(a.ticket)
    if not st:
        die("활성 그래프가 없다.")
    if a.path.startswith("gates.approvals"):
        die("승인 레코드는 CLI 로 쓸 수 없다.", 2)
    cur = dget(st, a.path)
    if cur is None:
        cur = []
        dset(st, a.path, cur)
    if not isinstance(cur, list):
        die("%s 는 배열이 아니다." % a.path)
    cur.append(_payload(a))
    save(st)
    print("[ge] append %s (len=%d)" % (a.path, len(cur)))


def cmd_gate(a):
    st = load(a.ticket)
    if not st:
        die("활성 그래프가 없다.")
    if a.gate_cmd == "open":
        if a.gate not in GATES:
            die("알 수 없는 게이트: %s" % a.gate)
        if a.artifact_file:
            with open(a.artifact_file, encoding="utf-8") as fh:
                artifact = fh.read()
        elif a.artifact:
            artifact = a.artifact
        else:
            artifact = sys.stdin.read()
        if not artifact.strip():
            die("승인 대상 산출물이 비어 있다.")
        h = gate_hash(artifact)
        pending = {"gate": a.gate, "artifact_hash": h, "code": short_code(h),
                   "presented_at": now(), "summary": a.summary or "",
                   "artifact_len": len(artifact)}
        dset(st, "gates.pending", pending)
        if a.gate == "GATE-PLAN":
            dset(st, "plan.hash", h)
        if a.gate == "GATE-COMMIT":
            dset(st, "commit.message", artifact.rstrip("\n"))
        log_event(st, st.get("current_node"), "gate-open", a.gate)
        save(st)
        print(json.dumps({"gate": a.gate, "code": pending["code"], "artifact_hash": h,
                          "instruction": "사용자가 채팅에 `approve` (또는 `승인`) 를 직접 입력해야 통과한다. "
                                         "반려는 `reject <사유>`."},
                         ensure_ascii=False, indent=2))
    elif a.gate_cmd == "show":
        print(json.dumps({"pending": dget(st, "gates.pending"),
                          "approvals": dget(st, "gates.approvals", [])},
                         ensure_ascii=False, indent=2))
    elif a.gate_cmd == "check":
        h = None
        if a.artifact_file:
            with open(a.artifact_file, encoding="utf-8") as fh:
                h = gate_hash(fh.read())
        elif a.artifact:
            h = gate_hash(a.artifact)
        ok, why = gate_is_approved(st, a.gate, h)
        if ok:
            print("[ge] %s 승인 확인됨." % a.gate)
        else:
            die(why, 2)


def cmd_approve(a):
    """훅 전용. 사람의 실제 입력에서만 발화한다."""
    if os.environ.get("GE_HOOK_AUTH") != "1" or a.source != "hook:UserPromptSubmit":
        die("승인 기록은 UserPromptSubmit 훅만 쓸 수 있다. 모델이 직접 승인할 수 없다.", 2)
    st = load(a.ticket)
    if not st:
        die("활성 그래프가 없다.")
    pending = dget(st, "gates.pending")
    if not pending:
        die("승인 대기 중인 게이트가 없다.", 3)
    if a.code and a.code != pending.get("code"):
        die("승인 코드 불일치: 입력=%s, 대기 중=%s" % (a.code, pending.get("code")), 3)
    rec = {
        "gate": pending["gate"],
        "artifact_hash": pending["artifact_hash"],
        "code": pending["code"],
        "decision": a.decision,
        "at": now(),
        "by": "user",
        "comment": a.comment or "",
        "source": "hook:UserPromptSubmit",
        "raw_prompt_excerpt": (a.raw or "")[:400],
        "node_at_decision": st.get("current_node"),
    }
    st.setdefault("gates", {}).setdefault("approvals", []).append(rec)
    dset(st, "gates.pending", None)
    log_event(st, st.get("current_node"), "gate-" + a.decision, pending["gate"])
    save(st)
    print(json.dumps({"ok": True, "gate": rec["gate"], "decision": a.decision},
                     ensure_ascii=False))


def cmd_jira_target(a):
    """노드의 목표 상태와 현재 전이 상태를 알려준다(상태 이름 하드코딩 방지용)."""
    st = load(a.ticket)
    if not st:
        die("활성 그래프가 없다.")
    cfg = config()
    nodes = [a.node] if a.node else list(transition_nodes(cfg).keys())
    out = {"enforce": transition_mode(cfg), "project": project_key(st),
           "current_status": dget(st, "jira.current_status"),
           "status_at_entry": dget(st, "jira.status_at_entry"), "nodes": {}}
    for node in nodes:
        target = status_target(st, node, cfg)
        rec = last_transition(st, node)
        out["nodes"][node] = {
            "target_status": target,
            "target_status_id": known_status_id(st, target, cfg) if target else None,
            "entries": int((dget(st, "jira.node_entries", {}) or {}).get(node) or 0),
            "pending": pending_transition(st, node) is not None,
            "last": rec,
        }
    print(json.dumps(out, ensure_ascii=False, indent=2))


def cmd_jira_transition(a):
    """실제로 수행한 상태 전이를 기록한다. 이 기록이 있어야 해당 노드에 진입할 수 있다."""
    with state_lock(a.ticket):
        st = load(a.ticket)
        if not st:
            die("활성 그래프가 없다.")
        node = a.node
        target = status_target(st, node)
        if not target:
            die("노드 %s 는 전이 대상이 아니다. config.jira.transition_policy.nodes 를 보라 (%s)."
                % (node, ", ".join(sorted(transition_nodes().keys())) or "비어 있음"))
        if a.to and norm_status(a.to) != norm_status(target):
            die("%s 의 목표 상태는 '%s' 다. '%s' 로는 기록할 수 없다.\n"
                "  → 워크플로가 다르면 config.jira.project_overrides.%s.status_targets 를 고쳐라."
                % (node, target, a.to, project_key(st)), 2)
        if not a.already and not a.transition_id:
            die("--transition-id 가 필요하다(실제로 수행한 transition 의 id).\n"
                "  → 조회 결과 이미 '%s' 였다면 --already 로 기록한다. 확인 없이 쓰지 마라." % target, 2)
        rec = {
            "node": node,
            "from_status": a.from_status,
            "to_status": target,
            "to_status_id": known_status_id(st, target),
            "transition_id": a.transition_id,
            "transition_name": a.transition_name,
            "applied": not a.already,
            "already_in_target": bool(a.already),
            "at": now(),
            "consumed": False,
        }
        applied = dget(st, "jira.transitions_applied", []) or []
        applied.append(rec)
        dset(st, "jira.transitions_applied", applied)
        dset(st, "jira.current_status", target)
        if not dget(st, "jira.status_at_entry") and a.from_status:
            dset(st, "jira.status_at_entry", a.from_status)
        log_event(st, node, "jira-transition",
                  "%s → %s (%s)" % (a.from_status or "?", target,
                                    "이미 목표 상태" if a.already else
                                    "transition %s/%s" % (a.transition_id, a.transition_name or "?")))
        save(st)
    print(json.dumps({"node": node, "to_status": target, "applied": not a.already,
                      "consumed": False}, ensure_ascii=False))


def cmd_retry(a):
    st = load(a.ticket)
    if not st:
        die("활성 그래프가 없다.")
    key = a.edge
    limit = dget(st, "retries.limits." + key, 3)
    cur = dget(st, "retries." + key, 0) + 1
    dset(st, "retries." + key, cur)
    log_event(st, st.get("current_node"), "retry", "%s %d/%d" % (key, cur, limit))
    if cur > limit:
        st["node_status"] = "escalated"
        st["halt_reason"] = ("재시도 상한 초과: %s %d회 > 상한 %d회. 그래프를 정지하고 "
                             "사용자에게 에스컬레이션한다." % (key, cur, limit))
        save(st)
        sys.stderr.write("[ge] " + st["halt_reason"] + "\n")
        sys.exit(3)
    save(st)
    print("[ge] retry %s = %d/%d (남은 횟수 %d)" % (key, cur, limit, limit - cur))


def cmd_record_test(a):
    st = load(a.ticket)
    if not st:
        die("활성 그래프가 없다.")
    # N5 산출물이다. `ge node N5` 를 건너뛰고 테스트만 돌린 경로도 여기서 걸린다.
    require_transitioned(st, "N5")
    attempts = dget(st, "tests.attempts", []) or []
    n = len(attempts) + 1
    raw = ""
    if a.log and os.path.isfile(a.log):
        raw = _read(a.log, 5_000_000)
    elif not sys.stdin.isatty():
        raw = sys.stdin.read()
    logdir = os.path.join(logs_dir(), st["ticket"])
    os.makedirs(logdir, exist_ok=True)
    logpath = os.path.join(logdir, "n5-attempt-%d.log" % n)
    atomic_write(logpath, raw)
    # 원문 근거: 요약하지 않고 발췌를 그대로 남긴다
    lines = raw.splitlines()
    excerpt_lines = [ln for ln in lines
                     if re.search(r"(FAILED|FAILURE|error:|Exception|AssertionError|"
                                  r"expected:|Caused by:|BUILD FAILED|Test.*failed)", ln)]
    excerpt = "\n".join(excerpt_lines[:200]) or "\n".join(lines[-120:])
    attempts.append({
        "attempt": n, "at": now(), "command": a.command,
        "exit_code": a.exit_code, "passed": a.exit_code == 0,
        "raw_log_path": norm_rel(logpath),
        "raw_log_lines": len(lines),
        "failure_excerpt_verbatim": excerpt,
    })
    dset(st, "tests.attempts", attempts)
    log_event(st, "N5", "test-run", "%s → exit %d" % (a.command, a.exit_code))
    save(st)
    print(json.dumps({"attempt": n, "passed": a.exit_code == 0,
                      "raw_log_path": norm_rel(logpath)}, ensure_ascii=False))


def cmd_record_review(a):
    st = load(a.ticket)
    if not st:
        die("활성 그래프가 없다.")
    with open(a.file, encoding="utf-8") as fh:
        findings = json.load(fh)
    if isinstance(findings, dict):
        findings = findings.get("findings", [])
    rounds = dget(st, "review.rounds", []) or []
    blocking = [f for f in findings if f.get("severity") in ("blocking", "critical")]
    rounds.append({"round": len(rounds) + 1, "at": now(), "findings": findings,
                   "blocking_count": len(blocking)})
    dset(st, "review.rounds", rounds)
    log_event(st, "N6", "review", "round %d, blocking %d" % (len(rounds), len(blocking)))
    save(st)
    print(json.dumps({"round": len(rounds), "total": len(findings),
                      "blocking": len(blocking)}, ensure_ascii=False))


def cmd_tdd(a):
    st = load(a.ticket)
    if not st:
        die("활성 그래프가 없다.")
    if a.tdd_cmd == "record":
        att = tdd_current_attempt(st, create=True)
        rel = norm_rel(a.path)
        kind = a.kind or classify_path(rel, st)
        key = {"test": "test_writes", "prod": "prod_writes"}.get(kind)
        if key:
            att.setdefault(key, []).append({"path": rel, "at": now()})
        save(st)
        print("[ge] tdd record %s (%s)" % (rel, kind))
    elif a.tdd_cmd == "exempt":
        att = tdd_current_attempt(st, create=True)
        att.setdefault("exemptions", []).append(
            {"path": norm_rel(a.path), "reason": a.reason, "at": now()})
        save(st)
        print("[ge] tdd exempt %s — %s (리뷰 노드에 그대로 노출된다)" % (norm_rel(a.path), a.reason))
    elif a.tdd_cmd == "check":
        rel = norm_rel(a.path)
        decision, reason = tdd_verdict(st, rel)
        print(json.dumps({"path": rel, "kind": classify_path(rel, st),
                          "decision": decision, "reason": reason},
                         ensure_ascii=False, indent=2))
        if decision == "deny":
            sys.exit(2)
    elif a.tdd_cmd == "report":
        print(json.dumps(dget(st, "tdd", {}), ensure_ascii=False, indent=2))


def latest_amendment(st):
    ams = dget(st, "ticket_amendments", []) or []
    return ams[-1] if ams else None


def amend_write_allowed(st):
    """AMEND 노드에서 JIRA 티켓 본문을 쓸 수 있는가 — (ok, 사유).

    GATE-TICKET 이 **제안 본문 해시에 묶여** 승인된 경우에만 허용한다.
    승인 후 본문을 고치면 해시가 달라져 다시 막힌다.
    """
    am = latest_amendment(st)
    if not am or not am.get("proposed_hash"):
        return False, ("수정 제안이 상태에 없다. 먼저 `ge amend propose --file <제안본문>` 으로 "
                       "제안을 등록하고 GATE-TICKET 을 열어야 한다.")
    if am.get("applied_at"):
        return False, ("이 제안은 이미 반영됐다(%s). 다시 고치려면 새 제안을 등록하라."
                       % am["applied_at"])
    return gate_is_approved(st, "GATE-TICKET", am["proposed_hash"])


def cmd_amend(a):
    """AMEND — 게이트 반려가 '요구사항 불일치' 일 때 JIRA 티켓 본문을 고친다.

    propose : 원본 스냅샷 + 제안 본문을 상태에 싣는다(아직 JIRA 를 건드리지 않는다).
    check   : 지금 JIRA 를 쓸 수 있는지 판정한다(훅이 같은 함수를 쓴다).
    applied : 실제 반영 사실을 기록한다. 이후 요구사항은 낡았으므로 N2 로 되돌린다.
    report  : 현재 수정 원장.
    """
    st = load(a.ticket)
    if not st:
        die("활성 그래프가 없다.")

    if a.amend_cmd == "propose":
        if not a.reason:
            die("--reason 은 필수다. 어떤 반려 사유에서 나온 수정인지 남겨야 한다.")
        proposed = _read_arg_text(a)
        if not proposed.strip():
            die("제안 본문이 비어 있다.")
        original = a.original and _read(a.original) or dget(st, "jira.raw_description_md") or ""
        h = gate_hash(proposed)
        rec = {
            "seq": len(dget(st, "ticket_amendments", []) or []) + 1,
            "at": now(),
            "from_gate": a.from_gate,
            "reject_reason": a.reason,
            "original_md": original,
            "proposed_md": proposed,
            "proposed_hash": h,
            "code": short_code(h),
            "applied_at": None,
            "reentry_node": a.reentry or "N2",
        }
        ams = dget(st, "ticket_amendments", []) or []
        ams.append(rec)
        dset(st, "ticket_amendments", ams)
        log_event(st, "AMEND", "amend-propose",
                  "%s 반려 → 티켓 수정 제안 #%d" % (a.from_gate or "?", rec["seq"]))
        save(st)
        print(json.dumps({"seq": rec["seq"], "proposed_hash": h, "code": rec["code"],
                          "instruction": "`ge gate open --gate GATE-TICKET --artifact-file <같은 파일>` "
                                         "로 제시하고 사용자 승인을 받아야 JIRA 를 고칠 수 있다."},
                         ensure_ascii=False, indent=2))
        return

    if a.amend_cmd == "check":
        ok, why = amend_write_allowed(st)
        print(json.dumps({"allowed": ok, "reason": why}, ensure_ascii=False, indent=2))
        if not ok:
            sys.exit(2)
        return

    if a.amend_cmd == "applied":
        ok, why = amend_write_allowed(st)
        if not ok:
            die("반영 기록 거부 — " + why, 2)
        ams = dget(st, "ticket_amendments", []) or []
        am = ams[-1]
        am["applied_at"] = now()
        am["applied_note"] = a.note or ""
        dset(st, "ticket_amendments", ams)
        # 요구사항이 바뀌었다 — 기존 계획과 그 승인은 무효다.
        dset(st, "plan", {"markdown": None, "hash": None, "created_at": None})
        dset(st, "requirements.format_valid", None)
        st["node_status"] = "running"
        st["halt_reason"] = None
        log_event(st, "AMEND", "amend-applied",
                  "티켓 수정 #%d 반영. 계획 무효화 → %s 재진입" % (am["seq"], am["reentry_node"]))
        save(st)
        print(json.dumps({"seq": am["seq"], "applied_at": am["applied_at"],
                          "reentry_node": am["reentry_node"],
                          "instruction": "요구사항이 바뀌었다. 계획과 그 승인은 무효화됐다. "
                                         "`ge node %s --status running` 으로 되돌아가 "
                                         "티켓을 다시 파싱하라." % am["reentry_node"]},
                         ensure_ascii=False, indent=2))
        return

    if a.amend_cmd == "report":
        print(json.dumps(dget(st, "ticket_amendments", []) or [],
                         ensure_ascii=False, indent=2))
        return


def _read_arg_text(a):
    if getattr(a, "file", None):
        return _read(a.file)
    if getattr(a, "value", None):
        return a.value
    return sys.stdin.read()


def cmd_record_graphify(a):
    """N10 — graphify 지식 그래프 최신화 결과를 **실측**해 기록한다.

    N0 에서 찍어둔 `runtime.graphify.stats` 를 before 로, 지금의 graph.json 을 after 로 삼아
    실제로 갱신됐는지 비교한다. "돌렸다고 말했지만 안 바뀐" 경우가 드러난다.
    비차단이다 — 지식 그래프가 낡은 것은 정합성 결함이 아니다. 대신 사실을 남긴다.
    """
    st = load(a.ticket)
    if not st:
        die("활성 그래프가 없다.")
    before = dget(st, "runtime.graphify.stats") or {}
    after = graphify_stats()

    changed = None
    if after:
        changed = (before.get("mtime") != after.get("mtime")
                   or before.get("bytes") != after.get("bytes"))
    delta = {}
    for key in ("nodes", "edges"):
        if key in after and key in before:
            delta[key] = after[key] - before[key]

    rec = {"status": a.status, "at": now(), "reason": a.note or "",
           "before": before, "after": after, "changed": changed, "delta": delta}
    dset(st, "graphify_update", rec)
    # 다음 노드/다음 티켓이 최신 스냅샷을 기준으로 삼도록 갱신한다
    if after:
        dset(st, "runtime.graphify.stats", after)
    log_event(st, "N10", "graphify-update",
              "%s / changed=%s / delta=%s" % (a.status, changed, json.dumps(delta)))
    save(st)
    print(json.dumps(rec, ensure_ascii=False, indent=2))

    # 사실과 어긋나는 보고를 조용히 넘기지 않는다 (경고만, 차단하지 않는다)
    if a.status == "ok" and not after:
        sys.stderr.write("[ge] 경고: status=ok 인데 graph.json 이 없다. "
                         "최신화가 실제로 일어나지 않았다.\n")
    elif a.status == "ok" and changed is False:
        sys.stderr.write("[ge] 경고: status=ok 인데 graph.json 이 그대로다(mtime·크기 동일). "
                         "`--update` 가 실제로 재추출했는지 확인하라.\n")


def cmd_apidocs(a):
    """Spring Boot + REST Docs 프로젝트에서 API 문서화 이행 여부를 **사실로** 판정한다.

    파일명이 아니라 내용으로 컨트롤러를 찾고, 변경된 테스트 중 그 컨트롤러를 참조하면서
    REST Docs 스니펫을 만드는 것이 있는지 대조한다. 결과는 상태에 남아 N6 리뷰에 노출되고,
    미해소분이 있으면 커밋 훅이 차단한다.
    """
    st = load(a.ticket)
    if not st:
        die("활성 그래프가 없다.")
    root = project_dir()

    if a.apidocs_cmd == "exempt":
        exs = dget(st, "api_docs.exemptions", []) or []
        exs.append({"path": norm_rel(a.path), "reason": a.reason, "at": now()})
        dset(st, "api_docs.exemptions", exs)
        log_event(st, st.get("current_node"), "apidocs-exempt", "%s — %s" % (a.path, a.reason))
        save(st)
        print("[ge] apidocs exempt %s — %s (리뷰 노드에 그대로 노출된다)"
              % (norm_rel(a.path), a.reason))
        return

    if a.apidocs_cmd == "report":
        print(json.dumps(dget(st, "api_docs", {}), ensure_ascii=False, indent=2))
        return

    # --- check ---
    style = dget(st, "runtime.build.api_docs.style")
    if not api_docs_applicable(st):
        why = ("Spring Boot 프로젝트가 아니다" if not dget(st, "runtime.build.spring.boot")
               else "config.api_docs.mode 가 off 다")
        dset(st, "api_docs", dict(dget(st, "api_docs", {}) or {},
                                  style=style, required=False, checked_at=now(),
                                  controllers_changed=[], documented=[], missing=[],
                                  note="검사 생략: " + why))
        save(st)
        print(json.dumps({"applicable": False, "reason": why}, ensure_ascii=False))
        return

    # 상태의 changed_files 는 낡았을 수 있다 — 문서화 판정은 항상 지금의 사실로 한다.
    changed = changed_files(st)

    cfg = config()
    exempt_globs = dget(cfg, "api_docs.exempt_globs", []) or []
    controllers, tests = [], []
    for rel in changed:
        kind = classify_path(rel, st)
        if kind == "test":
            tests.append(rel)
        elif kind == "prod" and is_controller_source(rel, root) \
                and not glob_any(rel, exempt_globs):
            controllers.append(rel)

    documented, missing = [], []
    for c in controllers:
        cls = re.sub(r"\.[^.]+$", "", os.path.basename(c))
        covering = [t for t in tests
                    if cls in _read(os.path.join(root, t), 200000)
                    and test_documents_api(t, root)]
        (documented if covering else missing).append(c)

    snippets = count_snippets(st, root)
    dset(st, "api_docs", {
        "style": style or "spring-restdocs",
        "required": True,
        "checked_at": now(),
        "controllers_changed": controllers,
        "documented": documented,
        "missing": missing,
        "snippets_found": snippets,
        "exemptions": dget(st, "api_docs.exemptions", []) or [],
    })
    log_event(st, st.get("current_node"), "apidocs-check",
              "컨트롤러 %d개 / 문서화 %d / 누락 %d / 스니펫 %d"
              % (len(controllers), len(documented), len(missing), snippets))
    save(st)

    unresolved = unresolved_api_docs(st)
    print(json.dumps({
        "applicable": True, "style": style,
        "controllers_changed": controllers, "documented": documented,
        "missing": missing, "unresolved": unresolved, "snippets_found": snippets,
    }, ensure_ascii=False, indent=2))

    if unresolved:
        mode = dget(cfg, "api_docs.mode", "block")
        restdocs_hint = (
            "  → spring-restdocs 의존성이 아직 없다 — 먼저 도입하라: "
            "references/spring-restdocs.md §5\n"
            if not dget(st, "runtime.build.restdocs.available") else "")
        msg = ("API 문서화 누락 %d건 — 변경된 컨트롤러에 REST Docs 스니펫을 만드는 테스트가 없다:\n"
               "  %s\n"
               "%s"
               "  → 해당 엔드포인트의 %s 테스트에 document(...) 를 추가하라.\n"
               "  → 문서화 대상이 아니면: ge apidocs exempt --path <경로> --reason \"<사유>\""
               % (len(unresolved), "\n  ".join(unresolved), restdocs_hint,
                  dget(st, "runtime.build.test_framework") or "API"))
        sys.stderr.write("[ge] " + msg + "\n")
        if mode == "block":
            sys.exit(2)
    elif controllers:
        # stdout 은 JSON 만 — 노드가 파싱해서 쓴다. 사람용 한 줄은 stderr 로.
        sys.stderr.write("[ge] 변경된 컨트롤러 %d개 전부 REST Docs 로 문서화됨.\n" % len(controllers))


def cmd_archunit(a):
    """레이어를 갖춘 기존 JVM 프로젝트에서 ArchUnit 도입 여부를 **사실로** 판정한다.

    ArchUnit 이 이미 있으면 규칙 위반은 일반 테스트 실행(N5)이 그대로 잡는다 — 여기서 하는
    실측은 "아직 도입 전인데 프로덕션 코드가 바뀌었다" 는 한 가지 사실만 본다. REST Docs 와
    달리 파일 단위 1:1 대응이 아니라 **이번 티켓에 규칙 도입 흔적이 있는가**를 본다.
    """
    st = load(a.ticket)
    if not st:
        die("활성 그래프가 없다.")
    root = project_dir()

    if a.archunit_cmd == "exempt":
        exs = dget(st, "arch_rules.exemptions", []) or []
        exs.append({"reason": a.reason, "at": now()})
        dset(st, "arch_rules.exemptions", exs)
        log_event(st, st.get("current_node"), "archunit-exempt", a.reason)
        save(st)
        print("[ge] archunit exempt — %s (리뷰 노드에 그대로 노출된다)" % a.reason)
        return

    if a.archunit_cmd == "report":
        print(json.dumps(dget(st, "arch_rules", {}), ensure_ascii=False, indent=2))
        return

    # --- check ---
    style = dget(st, "runtime.build.arch_rules.style")
    if not arch_rules_applicable(st):
        why = ("레이어를 갖춘 기존 JVM 프로젝트가 아니다"
               if not dget(st, "runtime.build.arch_rules.required")
               else "config.arch_rules.mode 가 off 다")
        dset(st, "arch_rules", dict(dget(st, "arch_rules", {}) or {},
                                    style=style, required=False, checked_at=now(),
                                    prod_changed=[], rule_files=[], missing=False,
                                    note="검사 생략: " + why))
        save(st)
        print(json.dumps({"applicable": False, "reason": why}, ensure_ascii=False))
        return

    # 상태의 changed_files 는 낡았을 수 있다 — 판정은 항상 지금의 사실로 한다.
    changed = changed_files(st)
    prod_changed = [rel for rel in changed if classify_path(rel, st) == "prod"]
    rule_files = archunit_rule_files(st, root)
    available = bool(dget(st, "runtime.build.archunit.available"))
    exempted = bool(dget(st, "arch_rules.exemptions", []) or [])

    if available:
        # 이미 도입됨 — 여기서 더 판정할 사실이 없다. 위반은 N5 테스트 실행이 잡는다.
        missing = False
        note = "ArchUnit 이미 도입됨 — 규칙 위반은 일반 테스트 실행(N5)이 잡는다."
    elif not prod_changed:
        missing = False
        note = "프로덕션 코드 변경이 없다 — 도입을 강제할 대상이 없다."
    elif exempted:
        missing = False
        note = "예외 처리됨."
    else:
        missing = not rule_files
        note = ("ArchUnit 미도입 상태에서 프로덕션 코드가 바뀌었는데 이번 시도에 규칙 테스트가 없다."
                 if missing else "이번 시도에 ArchUnit 규칙 테스트를 도입했다.")

    dset(st, "arch_rules", {
        "style": style or "archunit",
        "required": True,
        "checked_at": now(),
        "prod_changed": prod_changed,
        "rule_files": rule_files,
        "missing": missing,
        "exemptions": dget(st, "arch_rules.exemptions", []) or [],
        "note": note,
    })
    log_event(st, st.get("current_node"), "archunit-check",
              "프로덕션 변경 %d개 / 규칙 파일 %d개 / missing=%s"
              % (len(prod_changed), len(rule_files), missing))
    save(st)

    print(json.dumps({
        "applicable": True, "available": available, "missing": missing,
        "prod_changed": prod_changed, "rule_files": rule_files, "note": note,
    }, ensure_ascii=False, indent=2))

    if missing:
        mode = dget(config(), "arch_rules.mode", "block")
        msg = ("아키텍처 규칙(ArchUnit) 도입 누락 — 이 프로젝트엔 레이어가 있는데 ArchUnit "
               "의존성도, 이번 시도의 규칙 테스트도 없다.\n"
               "  변경된 프로덕션 파일: %s\n"
               "  → 작성법: references/archunit.md §4\n"
               "  → 도입 대상이 아니면: ge archunit exempt --reason \"<사유>\""
               % ", ".join(prod_changed))
        sys.stderr.write("[ge] " + msg + "\n")
        if mode == "block":
            sys.exit(2)


def cmd_detect(a):
    st = load(a.ticket)
    if a.what in ("build", "all"):
        b = detect_build()
        if st:
            dset(st, "runtime.build", b)
        print(json.dumps(b, ensure_ascii=False, indent=2))
    if a.what in ("graphify", "all"):
        g = detect_graphify()
        if st:
            dset(st, "runtime.graphify", g)
        print(json.dumps(g, ensure_ascii=False, indent=2))
    if st:
        # 신규/기존 판정
        b = dget(st, "runtime.build", {}) or {}
        greenfield = b.get("status") == "missing" or (
            not b.get("source_roots") and not b.get("test_roots"))
        dset(st, "runtime.project_state", "greenfield" if greenfield else "existing")
        save(st)


def _porcelain_paths(out: str):
    """`git status --porcelain` 한 줄에서 경로를 뽑는다.

    - `--untracked-files=all` 이 없으면 새 디렉터리가 `src/` 하나로 뭉쳐 나온다.
      그러면 파일 단위 판정(TDD 분류, 컨트롤러 탐지)이 전부 무력화된다.
    - 이름 변경은 `R  old -> new` 형태다. 새 경로를 쓴다.
    """
    paths = []
    for ln in out.splitlines():
        if len(ln) < 4:
            continue
        p = ln[3:].strip()
        if " -> " in p:
            p = p.split(" -> ", 1)[1].strip()
        p = p.strip('"')
        if p:
            paths.append(p)
    return paths


def changed_files(st):
    """base 대비 변경 + 워킹트리 변경(추적되지 않은 파일 포함)을 파일 단위로 모은다."""
    base = dget(st, "git.base_branch") or "main"
    rc, out, _ = git("diff", "--name-only", base + "...HEAD")
    tracked = out.splitlines() if rc == 0 else []
    rc2, out2, _ = git("status", "--porcelain", "--untracked-files=all")
    working = _porcelain_paths(out2) if rc2 == 0 else []
    return sorted(set(f for f in tracked + working
                      if f and not f.startswith(".claude/graph/")))


def cmd_changed_files(a):
    st = load(a.ticket)
    if not st:
        die("활성 그래프가 없다.")
    files = changed_files(st)
    st["changed_files"] = files
    save(st)
    print("\n".join(files) if files else "(변경 파일 없음)")


def cmd_checkpoint(a):
    st = load(a.ticket)
    if not st:
        die("활성 그래프가 없다.")
    log_event(st, st.get("current_node"), "checkpoint", a.note or "")
    save(st)
    print("[ge] checkpoint @ %s" % st.get("current_node"))


def cmd_resume(a):
    st = load(a.ticket)
    if not st:
        die("%s 상태 파일이 없다." % (a.ticket or "(current)"))
    atomic_write(pointer_path(), st["ticket"] + "\n")
    log_event(st, st.get("current_node"), "resume", "")
    save(st)
    cmd_status(argparse.Namespace(ticket=st["ticket"]))


def cmd_abort(a):
    st = load(a.ticket)
    if not st:
        print("[ge] 활성 그래프가 없다. 정리할 것 없음.")
        return
    log_event(st, st.get("current_node"), "abort", a.note or "")
    st["node_status"] = "aborted"
    st["halt_reason"] = a.note or "사용자 중단"
    save(st)
    try:
        os.unlink(pointer_path())
    except OSError:
        pass
    print("[ge] 그래프 비활성화. 상태 파일은 %s 에 보존됨(감사용). 브랜치 %s 는 건드리지 않았다."
          % (norm_rel(state_path(st["ticket"])), dget(st, "git.branch")))


def cmd_history(a):
    st = load(a.ticket)
    if not st:
        die("활성 그래프가 없다.")
    for e in st.get("history", []):
        print("%-25s %-4s %-14s %s" % (e.get("at", ""), e.get("node", ""),
                                       e.get("event", ""), e.get("note", "")))


def cmd_active(a):
    sys.exit(0 if is_active() else 1)


# --------------------------------------------------------------------------
# 파서
# --------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(prog="ge", description="Graph Engineering 상태 런타임")
    p.add_argument("--ticket", default=None, help="대상 티켓(기본: state/current)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="그래프 시작")
    s.add_argument("--ticket", required=True, dest="ticket")
    s.add_argument("--branch")
    s.add_argument("--base-branch", default="main")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("show"); s.add_argument("--field"); s.set_defaults(func=cmd_show)
    s = sub.add_parser("status"); s.set_defaults(func=cmd_status)
    s = sub.add_parser("history"); s.set_defaults(func=cmd_history)
    s = sub.add_parser("active", help="활성이면 exit 0"); s.set_defaults(func=cmd_active)

    s = sub.add_parser("node", help="현재 노드 전이")
    s.add_argument("node")
    s.add_argument("--status", default="running",
                   choices=["running", "done", "failed", "escalated", "aborted"])
    s.add_argument("--note", default="")
    s.set_defaults(func=cmd_node)

    for name, fn in (("put", cmd_put), ("append", cmd_append)):
        s = sub.add_parser(name)
        s.add_argument("path")
        s.add_argument("--value")
        s.add_argument("--file")
        s.add_argument("--json", dest="json_value", action="store_true")
        s.set_defaults(func=fn)

    s = sub.add_parser("gate")
    s.add_argument("gate_cmd", choices=["open", "show", "check"])
    s.add_argument("--gate", choices=GATES)
    s.add_argument("--artifact")
    s.add_argument("--artifact-file")
    s.add_argument("--summary")
    s.set_defaults(func=cmd_gate)

    s = sub.add_parser("approve", help="[훅 전용] 사용자 입력에서만 발화")
    s.add_argument("--decision", choices=["approved", "rejected"], required=True)
    s.add_argument("--source", required=True)
    s.add_argument("--code")
    s.add_argument("--comment")
    s.add_argument("--raw")
    s.set_defaults(func=cmd_approve)

    s = sub.add_parser("retry")
    s.add_argument("--edge", choices=["n5_to_n4", "n6_to_n4", "gate_to_amend"],
                   required=True)
    s.set_defaults(func=cmd_retry)

    s = sub.add_parser("jira-target", help="노드의 목표 상태와 전이 기록 상태를 본다")
    s.add_argument("--node", choices=NODE_ORDER)
    s.set_defaults(func=cmd_jira_target)

    s = sub.add_parser("jira-transition", help="수행한 JIRA 상태 전이를 기록한다(노드 진입 조건)")
    s.add_argument("--node", required=True, choices=NODE_ORDER)
    s.add_argument("--to", help="목표 상태 이름(생략 가능, 넣으면 config 와 대조한다)")
    s.add_argument("--from", dest="from_status", help="전이 전 상태 이름")
    s.add_argument("--transition-id", help="실제로 수행한 transition 의 id")
    s.add_argument("--transition-name", help="transition 의 이름(감사 기록용)")
    s.add_argument("--already", action="store_true",
                   help="조회 결과 이미 목표 상태여서 전이가 필요 없었다")
    s.set_defaults(func=cmd_jira_transition)

    s = sub.add_parser("record-test")
    s.add_argument("--command", required=True)
    s.add_argument("--exit-code", type=int, required=True)
    s.add_argument("--log")
    s.set_defaults(func=cmd_record_test)

    s = sub.add_parser("record-review")
    s.add_argument("--file", required=True)
    s.set_defaults(func=cmd_record_review)

    s = sub.add_parser("tdd")
    s.add_argument("tdd_cmd", choices=["record", "exempt", "check", "report"])
    s.add_argument("--path")
    s.add_argument("--kind", choices=["test", "prod"])
    s.add_argument("--reason")
    s.set_defaults(func=cmd_tdd)

    s = sub.add_parser("amend", help="AMEND — 게이트 반려 시 JIRA 티켓 본문 수정")
    s.add_argument("amend_cmd", choices=["propose", "check", "applied", "report"])
    s.add_argument("--file", help="제안 본문 파일")
    s.add_argument("--value", help="제안 본문 문자열")
    s.add_argument("--original", help="원본 본문 파일(생략 시 jira.raw_description_md)")
    s.add_argument("--from-gate", dest="from_gate", choices=GATES,
                   help="어느 게이트 반려에서 나왔는지")
    s.add_argument("--reason", help="사용자의 반려 사유 원문")
    s.add_argument("--reentry", choices=NODE_ORDER, help="반영 후 되돌아갈 노드(기본 N2)")
    s.add_argument("--note", default="")
    s.set_defaults(func=cmd_amend)

    s = sub.add_parser("record-graphify", help="N10 graphify 최신화 결과 기록")
    s.add_argument("--status", choices=["ok", "skipped", "failed"], required=True)
    s.add_argument("--note", default="")
    s.set_defaults(func=cmd_record_graphify)

    s = sub.add_parser("apidocs", help="Spring REST Docs API 문서화 이행 검사")
    s.add_argument("apidocs_cmd", choices=["check", "exempt", "report"])
    s.add_argument("--path")
    s.add_argument("--reason")
    s.set_defaults(func=cmd_apidocs)

    s = sub.add_parser("archunit", help="ArchUnit 아키텍처 규칙 도입 이행 검사")
    s.add_argument("archunit_cmd", choices=["check", "exempt", "report"])
    s.add_argument("--reason")
    s.set_defaults(func=cmd_archunit)

    s = sub.add_parser("detect")
    s.add_argument("what", choices=["build", "graphify", "all"], default="all", nargs="?")
    s.set_defaults(func=cmd_detect)

    s = sub.add_parser("changed-files"); s.set_defaults(func=cmd_changed_files)

    s = sub.add_parser("checkpoint"); s.add_argument("--note"); s.set_defaults(func=cmd_checkpoint)
    s = sub.add_parser("resume"); s.set_defaults(func=cmd_resume)
    s = sub.add_parser("abort"); s.add_argument("--note"); s.set_defaults(func=cmd_abort)
    return p


# 상태를 변형하는 서브커맨드 — 실행 전체를 배타 락으로 감싼다.
MUTATING = {
    "init", "node", "put", "append", "gate", "approve", "retry",
    "jira-transition",
    "record-test", "record-review", "record-graphify", "amend", "tdd", "apidocs", "archunit",
    "detect", "changed-files",
    "checkpoint", "resume", "abort",
}


def main(argv=None):
    args = build_parser().parse_args(argv)
    if getattr(args, "cmd", None) in MUTATING:
        with state_lock(getattr(args, "ticket", None)):
            args.func(args)
    else:
        args.func(args)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BrokenPipeError:
        pass
    except Exception as exc:  # noqa: BLE001
        die("내부 오류: %s: %s" % (type(exc).__name__, exc), 1)
