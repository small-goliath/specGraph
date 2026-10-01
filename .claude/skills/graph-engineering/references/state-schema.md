# 공유 상태 스키마

- 파일: `.claude/graph/state/<TICKET>.json` (예: `PPS-283.json`)
- 활성 포인터: `.claude/graph/state/current` (티켓 키 한 줄)
- 테스트 원문 로그: `.claude/graph/logs/<TICKET>/n5-attempt-<n>.log`
- 게이트 산출물 임시 파일: `.claude/graph/tmp/<TICKET>-{plan,review,commit}.*`
- **활성 판정은 `state/current` 파일의 존재로만 한다.** 없으면 모든 훅은 즉시 exit 0.

## 버전관리 방침

`.claude/graph/state/`, `logs/`, `tmp/` 는 **`.gitignore` 대상**이다.
근거: ① 실행별·머신별 산출물이라 다른 사람이 재사용할 수 없다 ② 노드마다 갱신되어 diff 노이즈가 크다
③ JIRA 티켓 원문과 테스트 로그가 저장소에 박힌다.
반면 `.claude/graph/config.json` 은 팀 공용 설정이므로 **커밋한다**.

## 필드 소유권

노드는 **자기 소유 필드만 쓴다.** 다른 노드의 필드는 읽기만 한다.

| 필드 | 쓰는 주체 | 읽는 주체 |
|---|---|---|
| `ticket`, `created_at`, `git.*` | N0 | 전 노드, 훅 |
| `runtime.graphify`, `runtime.build`, `runtime.project_state` | N0 (`ge detect`) | 전 노드 |
| `jira.*` | N1 | N2, N8, N9 |
| `requirements.*` | N2a | N3, N4, N6, N8 |
| `impact.*` | N2b | N3, N4, N6 |
| `conventions.*` | N2c (+ N3 이 `decided_this_run` 추가) | N4, N6 |
| `plan.*` | N3 | N4, N6, N7 |
| `gates.pending` | `ge gate open` (노드) | 훅, 사용자 |
| **`gates.approvals[]`** | **UserPromptSubmit 훅만** | 훅, 전 노드 |
| `tdd.attempts[]` | PostToolUse 훅(자동) + `ge tdd` | 훅, N6 |
| `tests.attempts[]` | N5 (`ge record-test`) | N4, N6, N8, N9 |
| `review.rounds[]` | N6 join (`ge record-review`) | N4, N7, N9 |
| `retries.*` | `ge retry` | 오케스트레이터 |
| `changed_files` | `ge changed-files` | N6, N7, N9 |
| `commit.*` | N7 | N8, N9 |
| `jira_updates.*` | N8, N9 | — |
| `current_node`, `node_status`, `halt_reason`, `history[]` | `ge node` / 훅 | 전부 |
| `api_docs.*` (exemptions 제외) | `ge apidocs check` (N5) | N6, 커밋 훅 |
| `graphify_update.*` | `ge record-graphify` (N10) | 사용자 보고, 다음 티켓 |
| `ticket_amendments[]` | `ge amend propose/applied` (AMEND) | JIRA 쓰기 훅, N6 리뷰, N9 요약 |
| `retries.gate_to_amend` | `ge retry --edge gate_to_amend` | 상한 초과 시 정지 |
| `runtime.graphify.stats` | `ge detect` (N0) / `ge record-graphify` (N10) | N10 의 before 기준 |
| `api_docs.exemptions[]` | `ge apidocs exempt` (사유 필수) | N6 리뷰에 그대로 노출 |

## 스키마

```jsonc
{
  "schema_version": 1,
  "ticket": "PPS-283",
  "created_at": "2026-09-04T13:00:00+09:00",
  "updated_at": "...",

  "current_node": "N4",                  // N0..N10 | DONE
  "node_status": "running",              // running | done | failed | escalated | aborted
  "halt_reason": null,

  "git": {
    "base_branch": "main",
    "branch": "PPS-283",
    "base_sha": "…", "head_sha_at_start": "…",
    "worktree_clean_at_start": true
  },

  "runtime": {
    "project_state": "existing",         // existing | greenfield
    "graphify": {
      "available": true,                 // graphify-out/graph.json 존재 여부
      "marker": "graphify-out/graph.json",
      "mode": "graph.json",              // graph.json | dir-only | none
      "instruction": "graphify skill 최우선 사용",
      "detected_at": "…"
    },
    "build": {
      "tool": "gradle", "status": "detected",   // detected | missing | blocked | ambiguous
      "wrapper": "./gradlew", "invoke": "./gradlew",
      "dsl": "groovy",                    // groovy | kotlin
      "multi_module": true, "modules": ["core","api"],
      "version_catalog": "gradle/libs.versions.toml",
      "test_task": "test", "check_task": "check", "extra_test_tasks": ["integrationTest"],
      "test_framework": "junit5",         // junit5 | junit4 | spock | kotest | testng | null
      "source_roots": ["core/src/main"], "test_roots": ["core/src/test"],
      "evidence": ["gradlew 존재","settings.gradle"], "blockers": []
    }
  },

  "jira": {
    "cloud_id": "…", "url": "https://…/browse/PPS-283",
    "summary": "[repo] 무엇을 어떻게",
    "status_at_entry": "해야 할 일",       // 그래프 진입 시점의 티켓 상태
    "current_status": "검증 중",           // 마지막 전이 결과 (= 지금 티켓 상태)
    "raw_description_md": "…원문…",
    "raw_description_adf_path": ".claude/graph/logs/PPS-283/ticket.adf.json",
    // 전이 원장. `ge jira-transition` 만 쓴다. 노드 진입 시 consumed=true 로 소비된다.
    "transitions_applied": [
      {"node":"N1","from_status":"해야 할 일","to_status":"검토 중","to_status_id":"10060",
       "transition_id":"8","transition_name":"착수","applied":true,"already_in_target":false,
       "at":"…","consumed":true,"entry":1,"consumed_at":"…"}
    ],
    "node_entries": {"N1":1,"N4":2,"N5":2}   // 노드별 진입 횟수(= 소비된 전이 수)
  },

  "requirements": {
    "format_valid": true,
    "format_violations": [],
    "draft_proposed": null,               // 양식 위반 시 제안한 완료 조건 초안
    "target": {"repos":["cutting-tuna"], "modules":[], "entrypoints":[]},
    "background": {"as_is":"…","to_be":"…","why":"…"},
    "definition_of_done_scope": "배포까지",     // "조사, 보고까지" | "배포까지"
    "acceptance_criteria": [
      {"id":"AC1","group":"1. …","text":"검증 가능한 문장","checked_in_ticket":false,
       "status":"pending",                // pending | done | not-done | out-of-scope
       "evidence":[]}                     // 커밋/테스트/파일 근거. 근거 없이 done 금지.
    ],
    "out_of_scope": ["…"],                // ★ N4·N6 하드 제약
    "constraints": {                      // ★ N4·N6 하드 제약
      "backward_compat":"유지 필요(호출처: …)","db_schema":"불가","perf_security":"없음"
    }
  },

  "conventions": {
    "origin": "existing-code",            // existing-code | ticket | decided-in-plan
    "evidence": [{"kind":"CLAUDE.md","path":"CLAUDE.md","note":"…"}],
    "architecture": {"layers":[],"package_root":"","notes":""},
    "naming": {"class":"","method":"","test":"","package":""},
    "error_handling": "…",
    "testing": {"framework":"junit5","location":"src/test/java","naming":"*Test","style":"given-when-then"},
    "formatting": {"tool":"spotless","config":".editorconfig"},
    "decided_this_run": [],               // greenfield 에서 이번에 확정한 컨벤션
    "confidence": "high",                 // high | medium | low
    "open_questions": []                  // low 면 N3 계획에서 사용자 확인
  },

  "impact": {"files":[], "modules":[], "risk_notes":[]},

  "plan": {"markdown":"…", "hash":"sha256:…", "created_at":"…"},

  "gates": {
    "pending": {"gate":"GATE-PLAN","artifact_hash":"sha256:…","code":"ab12cd",
                "presented_at":"…","summary":"…","artifact_len":1234},
    "approvals": [
      {"gate":"GATE-PLAN","artifact_hash":"sha256:…","code":"ab12cd",
       "decision":"approved","at":"…","by":"user","comment":"…",
       "source":"hook:UserPromptSubmit",      // ★ 이 값이 아니면 무효 처리
       "raw_prompt_excerpt":"approve","node_at_decision":"N3"}
    ]
  },

  "tdd": {
    "mode": "block",                      // block | warn | off
    "attempts": [
      {"attempt":1,"started_at":"…",
       "test_writes":[{"path":"…Test.java","at":"…"}],
       "prod_writes":[{"path":"…Service.java","at":"…"}],
       "violations":[], "exemptions":[{"path":"…","reason":"…","at":"…"}]}
    ]
  },

  "tests": {
    "attempts": [
      {"attempt":1,"at":"…","command":"./gradlew test","exit_code":1,"passed":false,
       "raw_log_path":".claude/graph/logs/PPS-283/n5-attempt-1.log",
       "raw_log_lines":842,
       "failure_excerpt_verbatim":"…요약하지 않은 원문 발췌…"}
    ]
  },

  "review": {
    "rounds": [
      {"round":1,"at":"…","blocking_count":2,
       "findings":[{"id":"C1","dimension":"correctness","severity":"blocking",
                    "file":"…","line":42,"summary":"…","detail":"…",
                    "suggestion":"…","evidence":"…","status":"open"}]}
    ]
  },

  "retries": {"n5_to_n4":0,"n6_to_n4":0,"limits":{"n5_to_n4":3,"n6_to_n4":2}},
  "changed_files": [],
  "commit": {"message":"feat(PPS-283): …","hash":"…","at":"…"},
  "jira_updates": {"acceptance_updated":false,"comment_id":null},
  "history": [{"node":"N3","event":"gate-approved","at":"…","note":"GATE-PLAN"}]
}
```

## 동시 쓰기 — 상태는 락으로 직렬화된다

N2(3-way)·N6(4-way) fan-out 에서는 여러 노드가 동시에 상태를 갱신한다.
`ge` 의 상태 변형 서브커맨드는 전부 `.claude/graph/state/.<TICKET>.lock` 의 **배타 flock**
안에서 실행되므로, 동시에 다른 필드를 써도 서로를 덮어쓰지 않는다.

- 노드는 **자기 소유 필드만** 쓴다(위 소유권 표). 락은 경합을 막을 뿐 소유권을 대신하지 않는다.
- 훅(TDD 원장 기록, 체크포인트, 승인)도 같은 락을 잡고 상태를 **다시 읽은 뒤** 쓴다.
- 상태 JSON 을 셸이나 Edit/Write 로 직접 고치면 이 직렬화 밖으로 나가므로 훅이 차단한다.

## 체크포인트와 재개

- `Stop`/`SubagentStop` 훅이 노드 종료 시점마다 `history` 에 체크포인트를 자동으로 남긴다.
- 노드가 끝날 때 오케스트레이터도 `ge node <다음노드> --status running` 으로 전이를 명시한다.
- 세션이 끊기면 `/graph-resume <TICKET>` → 포인터 복구 + `ge status` 로 어디서 끊겼는지 확인 →
  `current_node` 부터 이어간다. 승인/재시도/테스트 이력은 전부 보존된다.

## CLI 요약

```bash
ge init --ticket PPS-283 --base-branch main   # 상태 생성 + 포인터
ge status | ge show [--field a.b.c] | ge history
ge detect all|build|graphify
ge node N4 --status running --note "…"
ge put <path> --value "…" | --file f | --json     # 상태 필드 쓰기
ge append <path> --json --value '{"…":1}'
ge gate open --gate GATE-PLAN --artifact-file f   # 게이트 열기(해시+코드)
ge gate show | ge gate check --gate G --artifact-file f
ge retry --edge n5_to_n4                          # 상한 초과 시 exit 3
ge record-test --command "…" --exit-code N --log f
ge record-review --file findings.json
ge tdd record|exempt|check|report
ge apidocs check|exempt|report                    # Spring Boot + REST Docs 문서화 실측
ge record-graphify --status ok|skipped|failed     # N10 지식 그래프 최신화 결과(실측 대조)
ge amend propose|check|applied|report             # AMEND 티켓 수정 (GATE-TICKET 필요)
ge changed-files | ge checkpoint | ge resume | ge abort
ge active                                          # 활성이면 exit 0
# ge approve 는 훅 전용 — 모델이 호출하면 차단된다
```
