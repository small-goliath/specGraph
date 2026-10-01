#!/usr/bin/env bash
# Graph Engineering 훅/런타임 회귀 테스트.
#
# 실제 저장소를 건드리지 않는다: .claude/graph 와 .claude/hooks 를 임시 샌드박스로 복사하고
# 거기에 git 저장소를 만들어 훅을 합성 stdin JSON 으로 구동한다.
#
#   사용: .claude/graph/tests/run-tests.sh
set -u

SRC_ROOT=$(cd -- "$(dirname -- "$0")/../../.." && pwd)
SANDBOX=$(mktemp -d "${TMPDIR:-/tmp}/ge-test.XXXXXX")
PASS=0; FAIL=0
TICKET="TEST-1"

cleanup() { rm -rf "$SANDBOX"; }
trap cleanup EXIT

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$*"; }
bad()  { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$*"; }

# hook <스크립트명> <기대종료코드> <설명> <<< stdin
hook() {
  script="$1"; want="$2"; desc="$3"; payload="$4"
  out=$(printf '%s' "$payload" | "$SANDBOX/.claude/hooks/ge-$script.sh" 2>"$SANDBOX/.stderr")
  rc=$?
  if [ "$rc" -eq "$want" ]; then
    ok "$desc (exit $rc)"
  else
    bad "$desc — 기대 exit $want, 실제 $rc"
    sed 's/^/       /' "$SANDBOX/.stderr" | head -6
  fi
  LAST_OUT="$out"
}

GE() { "$SANDBOX/.claude/graph/bin/ge" "$@"; }

# 전이 강제 노드(N1/N4/N5)에 진입하기 전에 JIRA 상태 전이를 기록한다.
# 실제 그래프에서는 MCP 로 진짜 전이를 수행한 **뒤** 같은 명령을 부른다.
XN() { GE jira-transition --node "$1" --transition-id "${2:-1}" --transition-name t >/dev/null; }

# --------------------------------------------------------------------------
say "샌드박스 준비: $SANDBOX"
mkdir -p "$SANDBOX/.claude"
cp -R "$SRC_ROOT/.claude/graph" "$SANDBOX/.claude/graph"
cp -R "$SRC_ROOT/.claude/hooks" "$SANDBOX/.claude/hooks"
rm -rf "$SANDBOX/.claude/graph/state" "$SANDBOX/.claude/graph/logs" "$SANDBOX/.claude/graph/tmp"
mkdir -p "$SANDBOX/.claude/graph/state" "$SANDBOX/.claude/graph/logs" "$SANDBOX/.claude/graph/tmp"
mkdir -p "$SANDBOX/src/main/java" "$SANDBOX/src/test/java"
: > "$SANDBOX/settings.gradle"; : > "$SANDBOX/build.gradle"
printf '#!/bin/sh\nexit 0\n' > "$SANDBOX/gradlew"; chmod +x "$SANDBOX/gradlew"
printf 'x\n' > "$SANDBOX/src/main/java/App.java"
( cd "$SANDBOX" && git init -q -b main && git config user.email t@t && git config user.name t \
  && git add -A >/dev/null && git commit -qm init && git switch -q -c "$TICKET" ) || exit 1
export CLAUDE_PROJECT_DIR="$SANDBOX"

# ==========================================================================
say "1. 그래프 비활성 — 모든 훅이 즉시 통과해야 한다 (평소 작업 무간섭)"
[ -f "$SANDBOX/.claude/graph/state/current" ] && bad "상태 포인터가 남아 있다" || ok "state/current 없음"

hook guard-bash      0 "비활성: 컨벤션 무시한 git commit 통과" \
  '{"tool_name":"Bash","tool_input":{"command":"git commit -m \"아무 메시지나\""}}'
hook guard-bash      0 "비활성: git push 통과" \
  '{"tool_name":"Bash","tool_input":{"command":"git push origin main"}}'
hook guard-write     0 "비활성: 소스 파일 편집 통과" \
  '{"tool_name":"Write","tool_input":{"file_path":"src/main/java/App.java"}}'
hook guard-write     0 "비활성: 상태 파일 편집조차 통과" \
  '{"tool_name":"Edit","tool_input":{"file_path":".claude/graph/state/X.json"}}'
hook record-write    0 "비활성: PostToolUse 통과" \
  '{"tool_name":"Write","tool_input":{"file_path":"src/main/java/App.java"}}'
hook approval        0 "비활성: UserPromptSubmit 통과" '{"prompt":"approve"}'
hook session-context 0 "비활성: SessionStart 통과" '{"hook_event_name":"SessionStart"}'
hook checkpoint      0 "비활성: Stop 통과" '{"hook_event_name":"Stop"}'
[ -z "$LAST_OUT" ] && ok "비활성 훅은 출력이 없다(컨텍스트 오염 없음)" || bad "비활성인데 출력이 있다: $LAST_OUT"

say "1b. python3 가 없어도 통과해야 한다"
PATH_BAK="$PATH"; export PATH="/nonexistent"
out=$(printf '{}' | /bin/bash "$SANDBOX/.claude/hooks/ge-guard-bash.sh" 2>&1); rc=$?
export PATH="$PATH_BAK"
[ "$rc" -eq 0 ] && ok "python3 부재 시 통과 (exit 0)" || bad "python3 부재 시 exit $rc"

say "1c. 실행 권한"
missing=0
for f in "$SRC_ROOT"/.claude/hooks/ge-*.sh "$SRC_ROOT/.claude/graph/bin/ge"; do
  [ -x "$f" ] || { bad "실행 권한 없음: $f"; missing=1; }
done
[ "$missing" -eq 0 ] && ok "모든 훅/CLI 에 실행 권한 있음"

# ==========================================================================
say "2. 그래프 활성화 (ge init)"
GE init --ticket "$TICKET" --base-branch main >/dev/null || bad "ge init 실패"
GE detect all >/dev/null
[ -f "$SANDBOX/.claude/graph/state/current" ] && ok "state/current 생성됨" || bad "포인터 미생성"
[ "$(GE show --field runtime.build.status)" = "detected" ] \
  && ok "Gradle 탐지: detected" || bad "Gradle 탐지 실패: $(GE show --field runtime.build.status)"
[ "$(GE show --field runtime.graphify.available)" = "false" ] \
  && ok "graphify 미탐지 → 일반 탐색 폴백" || bad "graphify 탐지 결과 이상"

say "2b. graphify 분기 — graphify-out/ 이 있으면 available=true"
mkdir -p "$SANDBOX/graphify-out"; printf '{}' > "$SANDBOX/graphify-out/graph.json"
GE detect graphify >/dev/null
[ "$(GE show --field runtime.graphify.available)" = "true" ] \
  && ok "graphify-out 존재 → available=true (graphify skill 최우선)" || bad "graphify 탐지 실패"
GE show --field runtime.graphify.instruction | grep -q "graphify" \
  && ok "탐색 지시문이 상태에 실림" || bad "지시문 없음"
rm -rf "$SANDBOX/graphify-out"; GE detect graphify >/dev/null
[ "$(GE show --field runtime.graphify.available)" = "false" ] \
  && ok "graphify-out 제거 → 일반 탐색 폴백" || bad "폴백 실패"

say "3. 상태 위변조 차단"
hook guard-write 2 "상태 JSON 직접 편집 차단" \
  "{\"tool_name\":\"Edit\",\"tool_input\":{\"file_path\":\".claude/graph/state/$TICKET.json\"}}"
hook guard-bash 2 "셸로 상태 파일 덮어쓰기 차단" \
  '{"tool_name":"Bash","tool_input":{"command":"echo hacked > .claude/graph/state/current"}}'
hook guard-bash 2 "sed -i 로 상태 변조 차단" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"sed -i '' s/a/b/ .claude/graph/state/$TICKET.json\"}}"
hook guard-bash 2 "GE_HOOK_AUTH 사칭 차단" \
  '{"tool_name":"Bash","tool_input":{"command":"GE_HOOK_AUTH=1 .claude/graph/bin/ge approve --decision approved --source hook:UserPromptSubmit"}}'
hook guard-bash 2 "ge approve 직접 호출 차단" \
  '{"tool_name":"Bash","tool_input":{"command":".claude/graph/bin/ge approve --decision approved --source x"}}'
hook guard-bash 0 "상태 파일 읽기(cat|jq)는 오탐 없이 통과" \
  '{"tool_name":"Bash","tool_input":{"command":"cat .claude/graph/state/TEST-1.json | jq .current_node"}}'
hook guard-bash 0 "무관한 일반 명령은 통과" \
  '{"tool_name":"Bash","tool_input":{"command":"ls -la src && git status --short"}}'
GE put gates.approvals --json --value '[]' >/dev/null 2>&1 \
  && bad "ge put 으로 승인 배열 조작이 됐다" || ok "ge put 으로 승인 경로 쓰기 거부"

say "4. 푸시/PR 차단"
hook guard-bash 2 "git push 차단" '{"tool_name":"Bash","tool_input":{"command":"git push origin HEAD"}}'
hook guard-bash 2 "gh pr create 차단" '{"tool_name":"Bash","tool_input":{"command":"gh pr create --fill"}}'

say "5. GATE-PLAN — 승인 전에는 소스를 쓸 수 없다"
hook guard-write 2 "계획 미승인 상태에서 소스 쓰기 차단" \
  '{"tool_name":"Write","tool_input":{"file_path":"src/main/java/App.java"}}'
hook guard-write 0 "게이트 산출물 작업영역(.claude/graph/tmp)은 허용" \
  '{"tool_name":"Write","tool_input":{"file_path":".claude/graph/tmp/plan.md"}}'

say "6. 승인은 사용자의 실제 입력에서만 발화한다"
printf '# 계획\n- 하나\n' > "$SANDBOX/.claude/graph/tmp/plan.md"
CODE=$(GE gate open --gate GATE-PLAN --artifact-file "$SANDBOX/.claude/graph/tmp/plan.md" \
        --summary "계획" | python3 -c 'import json,sys;print(json.load(sys.stdin)["code"])')
[ -n "$CODE" ] && ok "게이트 열림 (code=$CODE)" || bad "게이트 열기 실패"

hook approval 0 "틀린 코드로 승인 시도 → 기록되지 않음" '{"prompt":"approve ffffff"}'
[ "$(GE show --field gates.pending.gate)" = "GATE-PLAN" ] \
  && ok "틀린 코드 후에도 pending 유지" || bad "틀린 코드인데 승인됨"
hook guard-write 2 "여전히 소스 쓰기 차단" \
  '{"tool_name":"Write","tool_input":{"file_path":"src/main/java/App.java"}}'

hook approval 0 "사용자가 approve 입력 → 훅이 승인 기록" "{\"prompt\":\"approve $CODE\"}"
[ "$(GE show --field gates.approvals.0.decision)" = "approved" ] \
  && ok "승인 레코드 생성됨" || bad "승인 레코드 없음"
[ "$(GE show --field gates.approvals.0.source)" = "hook:UserPromptSubmit" ] \
  && ok "승인 출처가 훅으로 기록됨" || bad "승인 출처 이상"

say "7. TDD Red-First"
XN N4
GE node N4 --status running >/dev/null
hook guard-write 2 "테스트 없이 프로덕션 코드 쓰기 차단" \
  '{"tool_name":"Write","tool_input":{"file_path":"src/main/java/App.java"}}'
hook guard-write 0 "빌드 스크립트는 예외 glob 으로 통과" \
  '{"tool_name":"Write","tool_input":{"file_path":"build.gradle"}}'
hook guard-write 0 "테스트 파일 쓰기는 허용" \
  '{"tool_name":"Write","tool_input":{"file_path":"src/test/java/AppTest.java"}}'
hook record-write 0 "PostToolUse 가 테스트 쓰기를 원장에 기록" \
  '{"tool_name":"Write","tool_input":{"file_path":"src/test/java/AppTest.java"}}'
[ "$(GE show --field tdd.attempts.0.test_writes.0.path)" = "src/test/java/AppTest.java" ] \
  && ok "TDD 원장에 테스트 쓰기 기록됨" || bad "TDD 원장 미기록"
hook guard-write 0 "Red 이후 프로덕션 코드 쓰기 허용" \
  '{"tool_name":"Write","tool_input":{"file_path":"src/main/java/App.java"}}'

say "8. 커밋 가드"
printf 'feat(%s): 테스트 커밋\n' "$TICKET" > "$SANDBOX/.claude/graph/tmp/commit.txt"
hook guard-bash 2 "컨벤션 위반 메시지 차단" \
  '{"tool_name":"Bash","tool_input":{"command":"git commit -m \"그냥 커밋\""}}'
hook guard-bash 2 "다른 티켓 scope 차단" \
  '{"tool_name":"Bash","tool_input":{"command":"git commit -m \"feat(OTHER-9): x\""}}'
hook guard-bash 2 "에디터 모드 커밋 차단" \
  '{"tool_name":"Bash","tool_input":{"command":"git commit"}}'
hook guard-bash 2 "형식은 맞지만 GATE-REVIEW 미승인 → 차단" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git commit -m \\\"feat($TICKET): 테스트 커밋\\\"\"}}"

GE node N6 --status running >/dev/null
printf '리뷰 결과: blocking 없음\n' > "$SANDBOX/.claude/graph/tmp/review.md"
RCODE=$(GE gate open --gate GATE-REVIEW --artifact-file "$SANDBOX/.claude/graph/tmp/review.md" \
        | python3 -c 'import json,sys;print(json.load(sys.stdin)["code"])')
hook approval 0 "리뷰 승인" "{\"prompt\":\"승인 $RCODE\"}"
hook guard-bash 2 "리뷰만 승인 → GATE-COMMIT 없어 여전히 차단" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git commit -m \\\"feat($TICKET): 테스트 커밋\\\"\"}}"

CCODE=$(GE gate open --gate GATE-COMMIT --artifact-file "$SANDBOX/.claude/graph/tmp/commit.txt" \
        | python3 -c 'import json,sys;print(json.load(sys.stdin)["code"])')
hook approval 0 "커밋 승인" "{\"prompt\":\"approve $CCODE\"}"
hook guard-bash 0 "승인된 메시지 그대로면 커밋 허용" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git commit -m \\\"feat($TICKET): 테스트 커밋\\\"\"}}"
hook guard-bash 2 "승인 후 메시지를 바꾸면 해시 불일치로 차단" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git commit -m \\\"feat($TICKET): 몰래 바꾼 메시지\\\"\"}}"

say "9. 브랜치 가드"
( cd "$SANDBOX" && git switch -q main )
hook guard-write 2 "티켓 브랜치가 아니면 쓰기 차단" \
  '{"tool_name":"Write","tool_input":{"file_path":"src/main/java/App.java"}}'
hook guard-bash 2 "티켓 브랜치가 아니면 커밋 차단" \
  "{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"git commit -m \\\"feat($TICKET): 테스트 커밋\\\"\"}}"
( cd "$SANDBOX" && git switch -q "$TICKET" )

say "10. 재시도 상한 (N5 → N4)"
r1=$(GE retry --edge n5_to_n4 >/dev/null 2>&1; echo $?)
r2=$(GE retry --edge n5_to_n4 >/dev/null 2>&1; echo $?)
r3=$(GE retry --edge n5_to_n4 >/dev/null 2>&1; echo $?)
r4=$(GE retry --edge n5_to_n4 >/dev/null 2>&1; echo $?)
[ "$r1$r2$r3" = "000" ] && ok "상한(3) 이내 재시도는 통과" || bad "재시도 1~3 결과 $r1$r2$r3"
[ "$r4" = "3" ] && ok "상한 초과 시 exit 3 (에스컬레이션)" || bad "상한 초과인데 exit $r4"
[ "$(GE show --field node_status)" = "escalated" ] \
  && ok "상태가 escalated 로 전환됨" || bad "escalated 미전환"
GE show --field halt_reason | grep -q "상한" && ok "halt_reason 기록됨" || bad "halt_reason 없음"

say "11. 테스트 결과 원문 보존"
XN N5
GE node N5 --status running >/dev/null
printf 'AppTest > case FAILED\n    expected: 1 but was: 2\n' > "$SANDBOX/.claude/graph/tmp/n5.log"
GE record-test --command "./gradlew test" --exit-code 1 --log "$SANDBOX/.claude/graph/tmp/n5.log" >/dev/null
LOGP=$(GE show --field tests.attempts.0.raw_log_path)
[ -f "$SANDBOX/$LOGP" ] && ok "원문 로그 보존: $LOGP" || bad "원문 로그 없음"
GE show --field tests.attempts.0.failure_excerpt_verbatim | grep -q "expected: 1 but was: 2" \
  && ok "실패 원문이 요약 없이 상태에 남음" || bad "실패 원문 유실"

say "12. /graph-abort 후 즉시 무해해진다"
GE abort --note "테스트 종료" >/dev/null
[ -f "$SANDBOX/.claude/graph/state/current" ] && bad "포인터가 남았다" || ok "state/current 제거됨"
hook guard-bash  0 "중단 후: 아무 커밋이나 통과" \
  '{"tool_name":"Bash","tool_input":{"command":"git commit -m \"아무거나\""}}'
hook guard-write 0 "중단 후: 소스 편집 통과" \
  '{"tool_name":"Write","tool_input":{"file_path":"src/main/java/App.java"}}'
[ -f "$SANDBOX/.claude/graph/state/$TICKET.json" ] \
  && ok "상태 파일은 감사용으로 보존됨" || bad "상태 파일이 사라졌다"

# ==========================================================================
# 13~17: 셸(Bash) 경로가 Edit/Write 와 동일한 강제력을 갖는지 검증한다.
#        (예전에는 guard-bash 가 소스 쓰기를 전혀 보지 않아 GATE-PLAN·TDD 가
#         `echo x > src/…` 한 줄로 무력화됐다.)
# ==========================================================================
GE init --ticket "$TICKET" --branch "$TICKET" --base main --force >/dev/null
GE detect all >/dev/null 2>&1
XN N4
GE node N4 --status running >/dev/null

# bhook <기대종료코드> <설명> <bash 명령> — 명령을 JSON 으로 안전하게 감싼다
bhook() {
  payload=$(python3 -c 'import json,sys; print(json.dumps({"tool_name":"Bash","tool_input":{"command":sys.argv[1]}}))' "$3")
  hook guard-bash "$1" "$2" "$payload"
}

say "13. 셸로 소스를 쓰는 모든 경로가 GATE-PLAN 에 걸린다"
bhook 2 "리다이렉션 > 소스"        'echo hacked > src/main/java/App.java'
bhook 2 "추가 >> 소스"             'printf x >> src/main/java/App.java'
bhook 2 "heredoc cat > 소스"       'cat > src/main/java/App.java <<EOF'
bhook 2 "tee 소스"                 'echo x | tee src/main/java/App.java'
bhook 2 "sed -i 소스"              'sed -i "" s/a/b/ src/main/java/App.java'
bhook 2 "cp 로 소스 덮어쓰기"       'cp /etc/hosts src/main/java/App.java'
bhook 2 "mv 로 새 소스 생성"        'mv /tmp/x src/main/java/New.java'
bhook 2 "cd 로 우회한 소스 쓰기"    'cd src/main && echo x > java/App.java'
hook guard-write 2 "  ↳ Edit/Write 도 동일 판정" \
  '{"tool_name":"Write","tool_input":{"file_path":"src/main/java/App.java"}}'

say "14. cd 로 우회한 상태/로그 파일 쓰기도 막힌다"
bhook 2 "직접 경로 상태 쓰기"       'echo x > .claude/graph/state/TEST-1.json'
bhook 2 "cd 후 상태 쓰기"           'cd .claude/graph && echo x > state/TEST-1.json'
bhook 2 "cd 후 로그 tee"            'cd .claude/graph/logs && echo x | tee y.log'

say "15. 오탐 방지 — 정상 작업 명령은 그대로 통과한다"
bhook 0 "gradlew 출력 tee → tmp"    './gradlew test 2>&1 | tee .claude/graph/tmp/test.log'
bhook 0 "ge show (읽기)"            '.claude/graph/bin/ge show --field runtime.build'
bhook 0 "git status"                'git status --porcelain'
bhook 0 "git diff → tmp"            'git diff main...HEAD > .claude/graph/tmp/TEST-1.diff'
bhook 0 "저장소 밖 쓰기는 무관"      'echo x > /tmp/ge-scratch.txt'
bhook 0 "grep 결과에 든 git push"    'grep -rn "git push" docs/'
bhook 0 "echo 안의 git commit"       'echo "run git commit later"'
bhook 0 "sed 읽기(-i 없음)"          'sed -n 1,5p src/main/java/App.java'

say "16. git 실행 형태를 바꾼 우회가 막힌다"
printf 'plan\n' > "$SANDBOX/.claude/graph/tmp/plan.md"
GE gate open --gate GATE-PLAN --artifact-file "$SANDBOX/.claude/graph/tmp/plan.md" --summary s >/dev/null
hook approval 0 "GATE-PLAN 승인" '{"prompt":"approve"}'
bhook 2 "git commit (기본형)"        'git commit -m "junk"'
bhook 2 "git -C . commit"            'git -C . commit -m "junk"'
bhook 2 "git -c user.name=x commit"  'git -c user.name=x commit -m "junk"'
bhook 2 "git --work-tree=. commit"   'git --work-tree=. --git-dir=.git commit -m "junk"'
bhook 2 "/usr/bin/git commit"        '/usr/bin/git commit -m "junk"'
bhook 2 "sudo git -C . commit"       'sudo git -C . commit -m "junk"'
bhook 2 "\$(echo git) commit"         '$(echo git) commit -m "junk"'
bhook 2 "xargs git commit"           'echo junk | xargs -I{} git commit -m {}'
bhook 2 "git -C . push"              'git -C . push origin main'
bhook 2 "git -c a=b push"            'git -c a=b push'
bhook 2 "xargs git push"             'echo x | xargs git push'

say "17. 테스트 없이 쓴 프로덕션 코드가 커밋을 막는다 (원장 기반)"
GE put tdd.mode --value warn >/dev/null
GE tdd record --path src/main/java/App.java --kind prod >/dev/null
printf 'review\n' > "$SANDBOX/.claude/graph/tmp/rv.md"
GE gate open --gate GATE-REVIEW --artifact-file "$SANDBOX/.claude/graph/tmp/rv.md" --summary s >/dev/null
hook approval 0 "GATE-REVIEW 승인" '{"prompt":"approve"}'
printf 'feat(%s): x\n' "$TICKET" > "$SANDBOX/.claude/graph/tmp/cm.txt"
GE gate open --gate GATE-COMMIT --artifact-file "$SANDBOX/.claude/graph/tmp/cm.txt" --summary s >/dev/null
hook approval 0 "GATE-COMMIT 승인" '{"prompt":"approve"}'
bhook 2 "미해소 TDD 위반 → 커밋 차단" "git commit -m \"feat($TICKET): x\""
GE tdd exempt --path src/main/java/App.java --reason "테스트 대상 아님" >/dev/null
bhook 0 "면제 기록 후 커밋 허용"       "git commit -m \"feat($TICKET): x\""

say "18. fan-out 동시 상태 쓰기에서 필드가 유실되지 않는다 (배타 락)"
LOST=0
for i in 1 2 3 4 5 6 7 8; do
  GE put impact --json --value '{"r":0}' >/dev/null
  ( GE put impact      --json --value '{"r":1}' >/dev/null & \
    GE put conventions --json --value '{"r":1}' >/dev/null & \
    GE put review_hint --json --value '{"r":1}' >/dev/null & wait )
  got="$(GE show --field impact | tr -d ' \n')$(GE show --field conventions | tr -d ' \n')$(GE show --field review_hint | tr -d ' \n')"
  [ "$got" = '{"r":1}{"r":1}{"r":1}' ] || LOST=$((LOST+1))
done
[ "$LOST" -eq 0 ] && ok "3-way 동시 쓰기 8회 — 유실 0건" || bad "동시 쓰기 유실 ${LOST}/8회"

say "19. 중단 후에는 셸 경로도 완전히 무해하다"
GE abort --note "테스트 종료" >/dev/null
bhook 0 "비활성: 셸 소스 쓰기 통과"  'echo x > src/main/java/App.java'
bhook 0 "비활성: git -C . push 통과" 'git -C . push origin main'
bhook 0 "비활성: 상태 파일 쓰기 통과" 'echo x > .claude/graph/state/TEST-1.json'

# ==========================================================================
# 20~22: Spring Boot + Kotest + Spring REST Docs API 문서화
#        별도 샌드박스에서 돈다 — 위 테스트들은 Spring 이 아닌 프로젝트를 전제한다.
# ==========================================================================
SB=$(mktemp -d "${TMPDIR:-/tmp}/ge-springboot.XXXXXX")
trap 'rm -rf "$SANDBOX" "$SB"' EXIT
cp -R "$SRC_ROOT/.claude/graph" "$SB/.claude-graph-tmp" 2>/dev/null || true
mkdir -p "$SB/.claude"
cp -R "$SRC_ROOT/.claude/graph" "$SB/.claude/graph"
cp -R "$SRC_ROOT/.claude/hooks" "$SB/.claude/hooks"
rm -rf "$SB/.claude-graph-tmp" "$SB/.claude/graph/state" "$SB/.claude/graph/logs" "$SB/.claude/graph/tmp"
mkdir -p "$SB/.claude/graph/state" "$SB/.claude/graph/logs" "$SB/.claude/graph/tmp"
mkdir -p "$SB/src/main/kotlin/api" "$SB/src/test/kotlin/api"
cat > "$SB/build.gradle.kts" <<'GRADLE'
plugins {
  id("org.springframework.boot") version "3.2.0"
  id("org.asciidoctor.jvm.convert") version "3.3.2"
}
dependencies {
  implementation("org.springframework.boot:spring-boot-starter-web")
  testImplementation("io.kotest:kotest-runner-junit5:5.8.0")
  testImplementation("io.kotest.extensions:kotest-extensions-spring:1.1.3")
  testImplementation("org.springframework.restdocs:spring-restdocs-mockmvc")
  testImplementation("com.ninja-squad:springmockk:4.0.2")
}
tasks.test { useJUnitPlatform() }
GRADLE
: > "$SB/settings.gradle.kts"
printf '#!/bin/sh\nexit 0\n' > "$SB/gradlew"; chmod +x "$SB/gradlew"
( cd "$SB" && git init -q -b main && git config user.email t@t && git config user.name t \
  && git add -A >/dev/null && git commit -qm init && git switch -q -c PPS-900 ) || exit 1

SBGE() { CLAUDE_PROJECT_DIR="$SB" "$SB/.claude/graph/bin/ge" "$@"; }
sbhook() {  # sbhook <스크립트> <기대exit> <설명> <payload>
  out=$(printf '%s' "$4" | CLAUDE_PROJECT_DIR="$SB" "$SB/.claude/hooks/ge-$1.sh" 2>"$SB/.err")
  rc=$?
  if [ "$rc" -eq "$2" ]; then ok "$3 (exit $rc)"; else bad "$3 — 기대 $2, 실제 $rc"; sed 's/^/       /' "$SB/.err" | head -4; fi
}
sbfield() { SBGE apidocs report | python3 -c "import json,sys;print(json.dumps(json.load(sys.stdin).get('$1'),ensure_ascii=False))"; }

SBGE init --ticket PPS-900 --branch PPS-900 --base main >/dev/null
SBGE detect all >/dev/null 2>&1

say "20. Spring Boot / Kotest / REST Docs 탐지"
[ "$(SBGE show --field runtime.build.test_framework)" = "kotest" ] \
  && ok "Kotest 가 junit5 보다 우선 매칭된다 (Kotest 는 JUnit Platform 위에서 돈다)" \
  || bad "test_framework=$(SBGE show --field runtime.build.test_framework) (kotest 여야 함)"
[ "$(SBGE show --field runtime.build.spring.boot)" = "true" ] \
  && ok "Spring Boot 탐지" || bad "spring.boot 미탐지"
[ "$(SBGE show --field runtime.build.spring.web)" = "mvc" ] \
  && ok "웹 스택 mvc 탐지" || bad "spring.web 미탐지"
[ "$(SBGE show --field runtime.build.restdocs.flavor)" = "mockmvc" ] \
  && ok "REST Docs flavor=mockmvc 탐지" || bad "restdocs.flavor 미탐지"
[ "$(SBGE show --field runtime.build.api_docs.required)" = "true" ] \
  && ok "api_docs.required=true (문서화 강제 발동)" || bad "api_docs.required 미설정"

say "21. changed-files 가 새 파일을 파일 단위로 잡는다"
printf '@RestController\nclass UserController { @GetMapping("/api/users/{id}") fun g() = 1 }\n' \
  > "$SB/src/main/kotlin/api/UserController.kt"
printf 'class UserControllerTest : FunSpec({ test("x") { } })\n' \
  > "$SB/src/test/kotlin/api/UserControllerTest.kt"
SBGE changed-files >/dev/null
SBGE show --field changed_files | grep -q 'src/main/kotlin/api/UserController.kt' \
  && ok "추적되지 않은 새 파일이 'src/' 로 뭉치지 않는다 (--untracked-files=all)" \
  || bad "changed_files 가 파일 단위가 아니다: $(SBGE show --field changed_files | tr -d ' \n')"

say "22. API 문서화 강제 (Spring Boot + REST Docs)"
SBGE apidocs check >/dev/null 2>&1; rc=$?
[ "$rc" -eq 2 ] && ok "문서화 없는 컨트롤러 → exit 2" || bad "문서화 누락인데 exit $rc"
sbfield missing | grep -q 'UserController.kt' && ok "누락 목록에 컨트롤러가 기록됨" || bad "missing 미기록"

# 게이트 3개를 승인해 커밋 직전 상태로 만든다
printf 'plan\n' > "$SB/.claude/graph/tmp/a.md"
for G in GATE-PLAN GATE-REVIEW; do
  SBGE gate open --gate "$G" --artifact-file "$SB/.claude/graph/tmp/a.md" --summary s >/dev/null
  printf '{"prompt":"approve"}' | CLAUDE_PROJECT_DIR="$SB" "$SB/.claude/hooks/ge-approval.sh" >/dev/null 2>&1
done
printf 'feat(PPS-900): 사용자 조회 API\n' > "$SB/.claude/graph/tmp/c.txt"
SBGE gate open --gate GATE-COMMIT --artifact-file "$SB/.claude/graph/tmp/c.txt" --summary s >/dev/null
printf '{"prompt":"approve"}' | CLAUDE_PROJECT_DIR="$SB" "$SB/.claude/hooks/ge-approval.sh" >/dev/null 2>&1
SBCOMMIT='{"tool_name":"Bash","tool_input":{"command":"git commit -m \"feat(PPS-900): 사용자 조회 API\""}}'
sbhook guard-bash 2 "게이트 3개 다 승인돼도 문서화 누락이면 커밋 차단" "$SBCOMMIT"

# REST Docs 테스트로 교체 + 스니펫 생성
cat > "$SB/src/test/kotlin/api/UserControllerTest.kt" <<'KT'
@WebMvcTest(UserController::class)
@AutoConfigureRestDocs
class UserControllerTest : FunSpec({
  test("사용자를 조회하면 200") {
    mockMvc.perform(get("/api/users/{id}", 1L)).andExpect(status().isOk)
      .andDo(document("user-get", responseFields(fieldWithPath("id").description("ID"))))
  }
})
KT
mkdir -p "$SB/build/generated-snippets/user-get"
echo snippet > "$SB/build/generated-snippets/user-get/response-fields.adoc"
SBGE changed-files >/dev/null
SBGE apidocs check >/dev/null 2>&1; rc=$?
[ "$rc" -eq 0 ] && ok "REST Docs 테스트 + 스니펫 → 통과" || bad "문서화했는데 exit $rc"
sbhook guard-bash 0 "문서화 후 커밋 허용" "$SBCOMMIT"

# 자동 예외 glob
printf '@RestController\nclass HealthController { @GetMapping("/health") fun h() = 1 }\n' \
  > "$SB/src/main/kotlin/api/HealthController.kt"
SBGE changed-files >/dev/null
SBGE apidocs check >/dev/null 2>&1
sbfield missing | grep -q 'HealthController' \
  && bad "HealthController 가 exempt_globs 로 제외되지 않았다" \
  || ok "exempt_globs 가 HealthController 를 자동 제외"

# 명시적 예외
printf '@RestController\nclass AdminController { @GetMapping("/admin") fun a() = 1 }\n' \
  > "$SB/src/main/kotlin/api/AdminController.kt"
SBGE changed-files >/dev/null
SBGE apidocs check >/dev/null 2>&1; rc=$?
[ "$rc" -eq 2 ] && ok "새 미문서화 컨트롤러 → 다시 차단" || bad "차단되지 않음 (exit $rc)"
SBGE apidocs exempt --path src/main/kotlin/api/AdminController.kt --reason "사내 전용" >/dev/null
SBGE apidocs check >/dev/null 2>&1; rc=$?
[ "$rc" -eq 0 ] && ok "ge apidocs exempt 로 해소" || bad "exempt 후에도 exit $rc"
sbfield exemptions | grep -q '사내 전용' \
  && ok "예외 사유가 상태에 남아 N6 리뷰에 노출된다" || bad "예외 사유 미기록"

# Spring Boot 가 아니면 발동하지 않는다
CLAUDE_PROJECT_DIR="$SB" python3 -c '
import sys; sys.path.insert(0, "'"$SB"'/.claude/graph/lib"); import ge_state as G
st = G.load(); G.dset(st, "runtime.build.spring.boot", False); G.save(st)'
SBGE apidocs check 2>/dev/null | grep -q '"applicable": false' \
  && ok "Spring Boot 가 아니면 검사 자체를 하지 않는다" || bad "비 Spring 프로젝트에서 발동했다"
sbhook guard-bash 0 "  ↳ 그 상태에선 커밋도 막지 않는다" "$SBCOMMIT"

# ==========================================================================
# 22b: REST Docs 의존성이 아직 없는 Spring Boot 프로젝트 — 그래도 강제된다 (조건부 아님)
# ==========================================================================
say "22b. REST Docs 미도입 Spring Boot — 그래도 문서화가 강제된다"
SB2=$(mktemp -d "${TMPDIR:-/tmp}/ge-springboot-nodocs.XXXXXX")
trap 'rm -rf "$SANDBOX" "$SB" "$SB2"' EXIT
mkdir -p "$SB2/.claude"
cp -R "$SRC_ROOT/.claude/graph" "$SB2/.claude/graph"
cp -R "$SRC_ROOT/.claude/hooks" "$SB2/.claude/hooks"
rm -rf "$SB2/.claude/graph/state" "$SB2/.claude/graph/logs" "$SB2/.claude/graph/tmp"
mkdir -p "$SB2/.claude/graph/state" "$SB2/.claude/graph/logs" "$SB2/.claude/graph/tmp"
mkdir -p "$SB2/src/main/kotlin/api" "$SB2/src/test/kotlin/api"
cat > "$SB2/build.gradle.kts" <<'GRADLE'
plugins {
  id("org.springframework.boot") version "3.2.0"
}
dependencies {
  implementation("org.springframework.boot:spring-boot-starter-web")
  testImplementation("io.kotest:kotest-runner-junit5:5.8.0")
}
tasks.test { useJUnitPlatform() }
GRADLE
: > "$SB2/settings.gradle.kts"
printf '#!/bin/sh\nexit 0\n' > "$SB2/gradlew"; chmod +x "$SB2/gradlew"
( cd "$SB2" && git init -q -b main && git config user.email t@t && git config user.name t \
  && git add -A >/dev/null && git commit -qm init && git switch -q -c PPS-901 ) || exit 1

SB2GE() { CLAUDE_PROJECT_DIR="$SB2" "$SB2/.claude/graph/bin/ge" "$@"; }
SB2GE init --ticket PPS-901 --branch PPS-901 --base main >/dev/null
SB2GE detect all >/dev/null 2>&1

[ "$(SB2GE show --field runtime.build.restdocs.available)" = "false" ] \
  && ok "restdocs 의존성 없음 (전제 확인)" || bad "테스트 전제가 깨졌다: restdocs 가 이미 있다"
[ "$(SB2GE show --field runtime.build.api_docs.required)" = "true" ] \
  && ok "api_docs.required=true — restdocs 미도입이어도 강제된다 (조건부 아님)" \
  || bad "restdocs 가 없으면 required 가 false 로 빠진다 — 강제가 조건부로 되돌아갔다"

printf '@RestController\nclass UserController { @GetMapping("/api/users/{id}") fun g() = 1 }\n' \
  > "$SB2/src/main/kotlin/api/UserController.kt"
SB2GE changed-files >/dev/null
SB2GE apidocs check >/dev/null 2>&1; rc=$?
[ "$rc" -eq 2 ] && ok "REST Docs 미도입 상태에서도 apidocs check 가 실측하고 차단한다" \
  || bad "restdocs 미도입이면 검사 자체를 건너뛴다 (exit $rc — applicable 이 조건부로 작동)"
SB2GE apidocs report 2>/dev/null | grep -q '"required": true' \
  && ok "report 에도 required=true 로 기록된다" || bad "report 의 required 가 false 다"

# ==========================================================================
# 22c: 아키텍처 규칙(ArchUnit) 강제 — Spring Boot 여부와 무관, 조건부 아님
# ==========================================================================
say "22c. ArchUnit 강제 — 레이어 있는 기존 JVM 프로젝트면 항상"
SB3=$(mktemp -d "${TMPDIR:-/tmp}/ge-archunit.XXXXXX")
trap 'rm -rf "$SANDBOX" "$SB" "$SB2" "$SB3"' EXIT
mkdir -p "$SB3/.claude"
cp -R "$SRC_ROOT/.claude/graph" "$SB3/.claude/graph"
cp -R "$SRC_ROOT/.claude/hooks" "$SB3/.claude/hooks"
rm -rf "$SB3/.claude/graph/state" "$SB3/.claude/graph/logs" "$SB3/.claude/graph/tmp"
mkdir -p "$SB3/.claude/graph/state" "$SB3/.claude/graph/logs" "$SB3/.claude/graph/tmp"
mkdir -p "$SB3/src/main/kotlin/app/service" "$SB3/src/main/kotlin/app/repository" "$SB3/src/test/kotlin/app"
cat > "$SB3/build.gradle.kts" <<'GRADLE'
plugins { kotlin("jvm") version "1.9.22" }
dependencies {
  testImplementation("io.kotest:kotest-runner-junit5:5.8.0")
}
tasks.test { useJUnitPlatform() }
GRADLE
: > "$SB3/settings.gradle.kts"
printf '#!/bin/sh\nexit 0\n' > "$SB3/gradlew"; chmod +x "$SB3/gradlew"
( cd "$SB3" && git init -q -b main && git config user.email t@t && git config user.name t \
  && git add -A >/dev/null && git commit -qm init && git switch -q -c PPS-902 ) || exit 1

SB3GE() { CLAUDE_PROJECT_DIR="$SB3" "$SB3/.claude/graph/bin/ge" "$@"; }
sb3hook() {  # sb3hook <스크립트> <기대exit> <설명> <payload>
  out=$(printf '%s' "$4" | CLAUDE_PROJECT_DIR="$SB3" "$SB3/.claude/hooks/ge-$1.sh" 2>"$SB3/.err")
  rc=$?
  if [ "$rc" -eq "$2" ]; then ok "$3 (exit $rc)"; else bad "$3 — 기대 $2, 실제 $rc"; sed 's/^/       /' "$SB3/.err" | head -4; fi
}
SB3GE init --ticket PPS-902 --branch PPS-902 --base main >/dev/null
SB3GE detect all >/dev/null 2>&1

[ "$(SB3GE show --field runtime.build.language.primary)" = "kotlin" ] \
  && ok "소스 디렉터리로 Kotlin 판정(src/main/kotlin)" || bad "language.primary 미탐지"
[ "$(SB3GE show --field runtime.build.spring.boot)" = "false" ] \
  && ok "Spring Boot 아님 (전제 확인)" || bad "테스트 전제가 깨졌다"
[ "$(SB3GE show --field runtime.build.archunit.available)" = "false" ] \
  && ok "archunit 의존성 없음 (전제 확인)" || bad "테스트 전제가 깨졌다: archunit 이 이미 있다"
layers="$(SB3GE show --field runtime.build.layers_detected | tr -d ' \n')"
case "$layers" in
  *service*repository*|*repository*service*)
    ok "layers_detected 가 service/repository 를 실측한다" ;;
  *) bad "layers_detected=$layers" ;;
esac
[ "$(SB3GE show --field runtime.build.arch_rules.required)" = "true" ] \
  && ok "arch_rules.required=true — Spring Boot 가 아니어도 강제된다 (조건부 아님)" \
  || bad "JVM 기존 프로젝트인데 arch_rules.required 가 false 다"

printf 'class OrderService { fun place() = 1 }\n' > "$SB3/src/main/kotlin/app/service/OrderService.kt"
SB3GE changed-files >/dev/null
SB3GE archunit check >/dev/null 2>&1; rc=$?
[ "$rc" -eq 2 ] && ok "ArchUnit 미도입 상태에서 프로덕션 변경 → exit 2" \
  || bad "미도입인데 프로덕션이 바뀌었는데도 차단하지 않는다 (exit $rc)"
SB3GE archunit report 2>/dev/null | grep -q '"missing": true' \
  && ok "report 에 missing=true 로 기록된다" || bad "missing 이 기록되지 않았다"

# 게이트 3개를 승인해 커밋 직전 상태로 만든다
printf 'plan\n' > "$SB3/.claude/graph/tmp/a.md"
for G in GATE-PLAN GATE-REVIEW; do
  SB3GE gate open --gate "$G" --artifact-file "$SB3/.claude/graph/tmp/a.md" --summary s >/dev/null
  printf '{"prompt":"approve"}' | CLAUDE_PROJECT_DIR="$SB3" "$SB3/.claude/hooks/ge-approval.sh" >/dev/null 2>&1
done
printf 'feat(PPS-902): 주문 서비스 추가\n' > "$SB3/.claude/graph/tmp/c.txt"
SB3GE gate open --gate GATE-COMMIT --artifact-file "$SB3/.claude/graph/tmp/c.txt" --summary s >/dev/null
printf '{"prompt":"approve"}' | CLAUDE_PROJECT_DIR="$SB3" "$SB3/.claude/hooks/ge-approval.sh" >/dev/null 2>&1
SB3COMMIT='{"tool_name":"Bash","tool_input":{"command":"git commit -m \"feat(PPS-902): 주문 서비스 추가\""}}'
sb3hook guard-bash 2 "게이트 3개 다 승인돼도 ArchUnit 미도입이면 커밋 차단" "$SB3COMMIT"

# 규칙 테스트 도입 → 해소
cat > "$SB3/src/test/kotlin/app/ArchitectureTest.kt" <<'KT'
import com.tngtech.archunit.junit5.AnalyzeClasses
import com.tngtech.archunit.junit5.ArchTest

@AnalyzeClasses(packages = ["app"])
class ArchitectureTest {
  @ArchTest
  val serviceNotDependOnController = noClasses().that().resideInAPackage("..service..")
    .should().dependOnClassesThat().resideInAPackage("..controller..")
}
KT
SB3GE changed-files >/dev/null
SB3GE archunit check >/dev/null 2>&1; rc=$?
[ "$rc" -eq 0 ] && ok "규칙 테스트 도입 후 통과" || bad "도입했는데 exit $rc"
sb3hook guard-bash 0 "도입 후 커밋 허용" "$SB3COMMIT"

# 예외 처리
rm -f "$SB3/src/test/kotlin/app/ArchitectureTest.kt"
printf 'class PaymentService { fun pay() = 1 }\n' > "$SB3/src/main/kotlin/app/service/PaymentService.kt"
SB3GE changed-files >/dev/null
SB3GE archunit check >/dev/null 2>&1; rc=$?
[ "$rc" -eq 2 ] && ok "규칙 테스트가 없어지면 다시 차단" || bad "차단되지 않음 (exit $rc)"
SB3GE archunit exempt --reason "실험용 브랜치 — 다음 티켓에서 도입" >/dev/null
SB3GE archunit check >/dev/null 2>&1; rc=$?
[ "$rc" -eq 0 ] && ok "ge archunit exempt 로 해소" || bad "exempt 후에도 exit $rc"
SB3GE archunit report 2>/dev/null | grep -q '실험용 브랜치' \
  && ok "예외 사유가 상태에 남아 N6 리뷰에 노출된다" || bad "예외 사유 미기록"

# ArchUnit 이 이미 도입된 프로젝트 — 여기서는 파일 단위로 강제하지 않는다
SB3B=$(mktemp -d "${TMPDIR:-/tmp}/ge-archunit-have.XXXXXX")
trap 'rm -rf "$SANDBOX" "$SB" "$SB2" "$SB3" "$SB3B"' EXIT
mkdir -p "$SB3B/.claude"
cp -R "$SRC_ROOT/.claude/graph" "$SB3B/.claude/graph"
cp -R "$SRC_ROOT/.claude/hooks" "$SB3B/.claude/hooks"
rm -rf "$SB3B/.claude/graph/state" "$SB3B/.claude/graph/logs" "$SB3B/.claude/graph/tmp"
mkdir -p "$SB3B/.claude/graph/state" "$SB3B/.claude/graph/logs" "$SB3B/.claude/graph/tmp"
mkdir -p "$SB3B/src/main/kotlin/app/service" "$SB3B/src/main/kotlin/app/repository" "$SB3B/src/test/kotlin/app"
cat > "$SB3B/build.gradle.kts" <<'GRADLE'
plugins { kotlin("jvm") version "1.9.22" }
dependencies {
  testImplementation("com.tngtech.archunit:archunit-junit5:1.3.0")
}
tasks.test { useJUnitPlatform() }
GRADLE
: > "$SB3B/settings.gradle.kts"
printf '#!/bin/sh\nexit 0\n' > "$SB3B/gradlew"; chmod +x "$SB3B/gradlew"
( cd "$SB3B" && git init -q -b main && git config user.email t@t && git config user.name t \
  && git add -A >/dev/null && git commit -qm init && git switch -q -c PPS-903 ) || exit 1
SB3BGE() { CLAUDE_PROJECT_DIR="$SB3B" "$SB3B/.claude/graph/bin/ge" "$@"; }
SB3BGE init --ticket PPS-903 --branch PPS-903 --base main >/dev/null
SB3BGE detect all >/dev/null 2>&1
[ "$(SB3BGE show --field runtime.build.archunit.available)" = "true" ] \
  && ok "archunit 의존성 있음 (전제 확인)" || bad "테스트 전제가 깨졌다"
printf 'class ReportService { fun run() = 1 }\n' > "$SB3B/src/main/kotlin/app/ReportService.kt"
SB3BGE changed-files >/dev/null
SB3BGE archunit check >/dev/null 2>&1; rc=$?
[ "$rc" -eq 0 ] && ok "ArchUnit 이미 도입됐으면 규칙 파일 없이 바꿔도 차단하지 않는다 (위반은 N5 테스트가 잡는다)" \
  || bad "이미 도입됐는데도 파일 단위로 차단한다 (exit $rc)"

# ==========================================================================
# 23: 소스 언어 탐지와 언어별 기본 테스트 프레임워크
#     Kotest 는 Kotlin DSL 이라 Java 프로젝트에 강요하면 안 된다.
# ==========================================================================
say "23. 언어 탐지 → 언어별 테스트 프레임워크"

# lang_case <이름> <기대language> <기대탐지fw> <기대제안fw> <셋업함수>
lang_case() {
  name="$1"; want_lang="$2"; want_fw="$3"; want_sug="$4"; setup="$5"
  LD=$(mktemp -d "${TMPDIR:-/tmp}/ge-lang.XXXXXX")
  mkdir -p "$LD/.claude"
  cp -R "$SRC_ROOT/.claude/graph" "$LD/.claude/graph"
  rm -rf "$LD/.claude/graph/state"; mkdir -p "$LD/.claude/graph/state"
  printf '#!/bin/sh\nexit 0\n' > "$LD/gradlew"; chmod +x "$LD/gradlew"
  ( cd "$LD" && $setup )
  out=$(CLAUDE_PROJECT_DIR="$LD" python3 "$LD/.claude/graph/lib/ge_state.py" detect build 2>/dev/null)
  got=$(printf '%s' "$out" | python3 -c '
import json,sys; d=json.load(sys.stdin)
print("%s|%s|%s" % (d.get("language",{}).get("primary"),
                    d.get("test_framework"), d.get("test_framework_suggested")))')
  if [ "$got" = "$want_lang|$want_fw|$want_sug" ]; then
    ok "$name → language=$want_lang 탐지=$want_fw 제안=$want_sug"
  else
    bad "$name — 기대 $want_lang|$want_fw|$want_sug, 실제 $got"
  fi
  rm -rf "$LD"
}

setup_kotlin() {
  mkdir -p src/main/kotlin src/test/kotlin src/main/java
  for i in 1 2 3 4 5; do echo c > "src/main/kotlin/A$i.kt"; done
  echo c > src/main/java/Legacy.java            # 소수 Java 가 섞여도 주 언어는 kotlin
  : > settings.gradle
  cat > build.gradle <<'G'
plugins { id "org.springframework.boot" version "3.2.0" }
dependencies {
  testImplementation "io.kotest:kotest-runner-junit5"
  testImplementation "org.springframework.restdocs:spring-restdocs-mockmvc"
}
G
}
setup_java() {
  mkdir -p src/main/java src/test/java
  for i in 1 2 3 4 5; do echo c > "src/main/java/A$i.java"; done
  : > settings.gradle
  cat > build.gradle <<'G'
plugins { id "org.springframework.boot" version "3.2.0"; id "java" }
dependencies {
  testImplementation "org.springframework.boot:spring-boot-starter-test"
  testImplementation "org.springframework.restdocs:spring-restdocs-mockmvc"
}
G
}
setup_green_java()   { mkdir -p src/main/java src/test/java;     : > settings.gradle;     echo 'plugins { id "java" }' > build.gradle; }
setup_green_kotlin() { mkdir -p src/main/kotlin src/test/kotlin; : > settings.gradle.kts; echo 'plugins { kotlin("jvm") version "1.9.0" }' > build.gradle.kts; }

lang_case "Kotlin 프로젝트(Java 소수 혼재)" kotlin kotest None setup_kotlin
lang_case "Java 프로젝트"                   java   junit5 None setup_java
lang_case "greenfield Java"                 java   None   junit5 setup_green_java
lang_case "greenfield Kotlin"               kotlin None   kotest setup_green_kotlin

# Kotest 를 Java 프로젝트에 제안하지 않는다 — 위 케이스가 곧 그 증명이다.
grep -q '"java": "junit5"' "$SRC_ROOT/.claude/graph/config.json" \
  && ok "config 의 언어별 기본값에 java→junit5 가 있다" \
  || bad "java 기본 테스트 프레임워크가 설정되지 않았다"
grep -q 'preferred_test_framework' "$SRC_ROOT/.claude/graph/config.json" \
  && bad "죽은 설정 preferred_test_framework 가 남아 있다" \
  || ok "언어를 무시하던 preferred_test_framework 가 제거됨"

# ==========================================================================
# 24: N10 — graphify 지식 그래프 최신화 (비차단, 실측 대조)
# ==========================================================================
say "24. N10 지식 그래프 최신화"

GD=$(mktemp -d "${TMPDIR:-/tmp}/ge-graphify.XXXXXX")
mkdir -p "$GD/.claude" "$GD/src/main/java"
cp -R "$SRC_ROOT/.claude/graph" "$GD/.claude/graph"
cp -R "$SRC_ROOT/.claude/hooks" "$GD/.claude/hooks"
rm -rf "$GD/.claude/graph/state" "$GD/.claude/graph/logs" "$GD/.claude/graph/tmp"
mkdir -p "$GD/.claude/graph/state" "$GD/.claude/graph/logs" "$GD/.claude/graph/tmp"
: > "$GD/settings.gradle"; : > "$GD/build.gradle"
printf '#!/bin/sh\nexit 0\n' > "$GD/gradlew"; chmod +x "$GD/gradlew"
( cd "$GD" && git init -q -b main && git config user.email t@t && git config user.name t \
  && git add -A >/dev/null && git commit -qm init && git switch -q -c PPS-901 ) || exit 1
GDGE() { CLAUDE_PROJECT_DIR="$GD" "$GD/.claude/graph/bin/ge" "$@"; }
gjson() { python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('$1'))"; }
gwrite() { python3 -c "
import json,sys
json.dump({'nodes':[{'id':i} for i in range(int(sys.argv[1]))],
           'edges':[{'a':1} for _ in range(int(sys.argv[2]))]},
          open('$GD/graphify-out/graph.json','w'))" "$1" "$2"; }

# N10 이 노드 순서에 있는가
GDGE init --ticket PPS-901 --branch PPS-901 --base main >/dev/null
GDGE node N10 --status running >/dev/null 2>&1 \
  && ok "N10 이 NODE_ORDER 에 등록됨" || bad "N10 노드 전이 실패"

# (1) graphify-out 이 없으면 건너뛴다
GDGE detect all >/dev/null 2>&1
[ "$(GDGE show --field runtime.graphify.available)" = "false" ] \
  && ok "graphify-out 없음 → available=false" || bad "available 판정 오류"
GDGE record-graphify --status skipped --note "graphify-out 없음" 2>/dev/null | gjson status \
  | grep -q skipped && ok "해당 없음 → status=skipped 기록" || bad "skipped 기록 실패"
[ -z "$(GDGE node DONE --status done 2>&1 >/dev/null)" ] \
  && ok "graphify 미사용 프로젝트는 DONE 경고 없음" || bad "불필요한 DONE 경고"

# (2) graphify-out 이 있으면 N0 가 규모 스냅샷을 찍는다
mkdir -p "$GD/graphify-out"; gwrite 10 20
GDGE init --ticket PPS-901 --branch PPS-901 --base main --force >/dev/null
GDGE detect all >/dev/null 2>&1
[ "$(GDGE show --field runtime.graphify.stats | gjson nodes)" = "10" ] \
  && ok "N0 가 graph.json 규모를 스냅샷(before)으로 기록" || bad "graphify stats 미기록"

# (3) 안 바뀌었는데 ok 로 보고하면 경고 — 성공을 지어낼 수 없다
GDGE record-graphify --status ok --note "돌렸다고 주장" 2>"$GD/.err" >/dev/null
grep -q "그대로다" "$GD/.err" \
  && ok "변화 없는데 status=ok → 경고(실측 대조)" || bad "거짓 성공이 경고 없이 통과"

# (4) 실제로 갱신되면 delta 가 잡힌다
sleep 1; gwrite 14 31
GDGE record-graphify --status ok --note "증분 재추출" 2>/dev/null > "$GD/.out"
[ "$(gjson changed < "$GD/.out")" = "True" ] \
  && ok "실제 갱신 → changed=true" || bad "갱신 감지 실패"
python3 -c "
import json;d=json.load(open('$GD/.out'))
assert d['delta']=={'nodes':4,'edges':11}, d['delta']" \
  && ok "delta 계산 정확 (nodes +4, edges +11)" || bad "delta 계산 오류"

# (5) N10 을 건너뛰고 DONE 으로 가면 경고한다 (차단은 아니다)
GDGE init --ticket PPS-901 --branch PPS-901 --base main --force >/dev/null
GDGE detect all >/dev/null 2>&1
GDGE node DONE --status done 2>"$GD/.err2" >/dev/null; rc=$?
grep -q "N10(지식 그래프 최신화) 기록이 없다" "$GD/.err2" \
  && ok "N10 누락 시 DONE 에서 경고" || bad "N10 누락 경고 없음"
[ "$rc" -eq 0 ] && ok "  ↳ 경고일 뿐 차단하지 않는다 (비차단 노드)" || bad "N10 누락이 DONE 을 막았다"

# (6) failed 로 기록해도 그래프는 진행된다
GDGE record-graphify --status failed --note "graphify 설치 실패" 2>/dev/null | gjson status \
  | grep -q failed && ok "실패를 사실대로 기록 (failed)" || bad "failed 기록 실패"
GDGE node DONE --status done >/dev/null 2>&1 \
  && ok "  ↳ 실패해도 DONE 으로 진행 (실패 격리)" || bad "failed 가 그래프를 멈췄다"

# (7) ge status 에 노출된다
GDGE status | grep -q "지식 그래프" \
  && ok "ge status 에 지식 그래프 상태 표시" || bad "status 미표시"

# (8) N10 graphify 자체 커밋 — GATE 없이 통과 (노드·메시지·스테이징 범위가 전부 맞을 때만)
ghook() { # ghook <script> <want> <desc> <payload>
  s="$1"; w="$2"; d="$3"; p="$4"
  o=$(printf '%s' "$p" | CLAUDE_PROJECT_DIR="$GD" "$GD/.claude/hooks/ge-$s.sh" 2>"$GD/.gherr")
  rc=$?
  if [ "$rc" -eq "$w" ]; then ok "$d (exit $rc)"; else bad "$d — 기대 exit $w, 실제 $rc"; sed 's/^/       /' "$GD/.gherr" | head -6; fi
}
GDGE node N10 --status running >/dev/null 2>&1
( cd "$GD" && printf '{"nodes":[]}' > graphify-out/note.json && git add graphify-out/note.json )

ghook guard-bash 0 "N10: graphify-out 만 스테이징 + 정확한 메시지 → GATE 없이 커밋 허용" \
  '{"tool_name":"Bash","tool_input":{"command":"git commit -m \"feat(graphify): update\""}}'

ghook guard-bash 2 "N10: 메시지가 다르면 예외 미적용 → 일반 컨벤션 검사로 차단" \
  '{"tool_name":"Bash","tool_input":{"command":"git commit -m \"feat(graphify): 다른 메시지\""}}'

( cd "$GD" && printf 'x' > src/main/java/App.java && git add src/main/java/App.java )
ghook guard-bash 2 "N10: graphify-out 외 파일이 섞이면 예외가 적용되지 않고 차단된다" \
  '{"tool_name":"Bash","tool_input":{"command":"git commit -m \"feat(graphify): update\""}}'
( cd "$GD" && git reset -q src/main/java/App.java )

GDGE node N9 --status running >/dev/null 2>&1
ghook guard-bash 2 "N10 이 아닌 노드에서는 같은 메시지라도 예외가 적용되지 않는다" \
  '{"tool_name":"Bash","tool_input":{"command":"git commit -m \"feat(graphify): update\""}}'

rm -rf "$GD"

# ==========================================================================
# 25: AMEND — 게이트 반려가 '요구사항 불일치' 일 때 티켓을 고치고 되돌아간다
#     JIRA 티켓 본문 쓰기는 GATE-TICKET 승인(본문 해시 바인딩) 없이는 불가능하다.
# ==========================================================================
say "25. AMEND 티켓 수정 + GATE-TICKET"

AD=$(mktemp -d "${TMPDIR:-/tmp}/ge-amend.XXXXXX")
mkdir -p "$AD/.claude" "$AD/src/main/java" "$AD/src/test/java"
cp -R "$SRC_ROOT/.claude/graph" "$AD/.claude/graph"
cp -R "$SRC_ROOT/.claude/hooks" "$AD/.claude/hooks"
rm -rf "$AD/.claude/graph/state" "$AD/.claude/graph/logs" "$AD/.claude/graph/tmp"
mkdir -p "$AD/.claude/graph/state" "$AD/.claude/graph/logs" "$AD/.claude/graph/tmp"
: > "$AD/settings.gradle"; : > "$AD/build.gradle"
printf '#!/bin/sh\nexit 0\n' > "$AD/gradlew"; chmod +x "$AD/gradlew"
( cd "$AD" && git init -q -b main && git config user.email t@t && git config user.name t \
  && git add -A >/dev/null && git commit -qm init && git switch -q -c PPS-902 ) || exit 1
ADGE() { CLAUDE_PROJECT_DIR="$AD" "$AD/.claude/graph/bin/ge" "$@"; }
adhook() {  # adhook <스크립트> <기대exit> <설명> <payload>
  printf '%s' "$4" | CLAUDE_PROJECT_DIR="$AD" "$AD/.claude/hooks/ge-$1.sh" >/dev/null 2>"$AD/.err"
  rc=$?
  if [ "$rc" -eq "$2" ]; then ok "$3 (exit $rc)"; else bad "$3 — 기대 $2, 실제 $rc"; head -3 "$AD/.err" | sed 's/^/       /'; fi
}
adapprove() { printf '{"prompt":"approve"}' | CLAUDE_PROJECT_DIR="$AD" "$AD/.claude/hooks/ge-approval.sh" >/dev/null 2>&1; }
JEDIT='{"tool_name":"mcp__atlassian__editJiraIssue","tool_input":{"issueIdOrKey":"PPS-902"}}'
WSRC='{"tool_name":"Write","tool_input":{"file_path":"src/test/java/T.java"}}'
WPROD='{"tool_name":"Write","tool_input":{"file_path":"src/main/java/A.java"}}'

ADGE init --ticket PPS-902 --branch PPS-902 --base main >/dev/null
ADGE detect all >/dev/null 2>&1
ADGE put jira.raw_description_md --value "원본 티켓 본문" >/dev/null

# (1) AMEND/N8/N9 밖에서는 티켓을 쓸 수 없다
ADGE node N3 --status running >/dev/null
adhook guard-jira 2 "N3 에서 티켓 본문 쓰기 차단 (모델이 요구사항을 만들 수 없다)" "$JEDIT"
ADGE node N8 --status running >/dev/null
adhook guard-jira 0 "N8 의 완료조건 갱신은 통과 (기존 동작)" "$JEDIT"

# (2) 계획 승인까지 진행
ADGE node N3 --status running >/dev/null
printf 'plan v1\n' > "$AD/.claude/graph/tmp/plan.md"
ADGE gate open --gate GATE-PLAN --artifact-file "$AD/.claude/graph/tmp/plan.md" --summary s >/dev/null
adapprove
adhook guard-write 0 "GATE-PLAN 승인 후 테스트 쓰기 허용" "$WSRC"

# (3) 반려 안내가 (A)/(B) 분류를 요구한다
ADGE gate open --gate GATE-PLAN --artifact-file "$AD/.claude/graph/tmp/plan.md" --summary s >/dev/null
out=$(printf '{"prompt":"reject 완료 조건에 B 가 빠졌다"}' | CLAUDE_PROJECT_DIR="$AD" "$AD/.claude/hooks/ge-approval.sh" 2>/dev/null)
printf '%s' "$out" | grep -q "요구사항 불일치" \
  && ok "반려 시 (A)산출물 / (B)요구사항 분류를 지시한다" || bad "반려 안내에 분류 지시 없음"
printf '%s' "$out" | grep -q "애매하면" \
  && ok "  ↳ 애매하면 사용자에게 물으라고 명시" || bad "애매할 때 지침 없음"

# (4) AMEND 진입 — 단계별로 막힌다
ADGE retry --edge gate_to_amend >/dev/null
ADGE node AMEND --status running >/dev/null
adhook guard-jira 2 "AMEND 인데 제안 없이 티켓 쓰기 차단" "$JEDIT"

printf '# 완료 조건\n- [ ] A\n- [ ] B\n' > "$AD/.claude/graph/tmp/ticket.md"
ADGE amend propose --file "$AD/.claude/graph/tmp/ticket.md" --from-gate GATE-PLAN \
  --reason "완료 조건에 B 가 빠졌다" --reentry N2 >/dev/null
adhook guard-jira 2 "제안은 있지만 GATE-TICKET 미승인 → 차단" "$JEDIT"

ADGE gate open --gate GATE-TICKET --artifact-file "$AD/.claude/graph/tmp/ticket.md" --summary s >/dev/null
adapprove
adhook guard-jira 0 "GATE-TICKET 승인 후 티켓 쓰기 허용" "$JEDIT"

# (5) 승인 후 본문을 바꾸면 다시 막힌다
printf '# 완료 조건\n- [ ] 몰래 C 추가\n' > "$AD/.claude/graph/tmp/ticket2.md"
ADGE amend propose --file "$AD/.claude/graph/tmp/ticket2.md" --from-gate GATE-PLAN --reason "몰래" >/dev/null
adhook guard-jira 2 "승인 후 제안 본문을 바꾸면 해시 불일치로 차단" "$JEDIT"

# (6) 반영 → 계획과 그 승인이 무효화된다
ADGE amend report | grep -q "원본 티켓 본문" \
  && ok "원본 티켓 본문이 스냅샷으로 보존됨 (롤백 근거)" || bad "original_md 미보존"
# 승인된 첫 제안으로 되돌려 반영
ADGE amend propose --file "$AD/.claude/graph/tmp/ticket.md" --from-gate GATE-PLAN --reason "재제안" >/dev/null
ADGE gate open --gate GATE-TICKET --artifact-file "$AD/.claude/graph/tmp/ticket.md" --summary s >/dev/null
adapprove
ADGE amend applied --note "완료조건 B 추가" >/dev/null
[ "$(ADGE show --field plan.hash)" = "null" ] \
  && ok "반영 후 계획이 무효화된다" || bad "plan.hash 가 남아 있다"
adhook guard-write 2 "  ↳ 낡은 GATE-PLAN 승인으로 소스를 쓸 수 없다 (require_hash)" "$WSRC"
adhook guard-write 2 "  ↳ 프로덕션 코드도 차단" "$WPROD"
ADGE node N4 --status running >/dev/null 2>&1 \
  && bad "AMEND 에서 N4 로 바로 진입됐다" || ok "  ↳ AMEND → N4 직행 거부 (N2 부터 다시)"
adhook guard-jira 2 "이미 반영된 제안으로 또 쓰려 하면 차단" "$JEDIT"

# (7) 재진입 후 새 계획을 승인하면 다시 열린다
ADGE node N2 --status running >/dev/null
ADGE node N3 --status running >/dev/null
printf 'plan v2 (완료조건 B 반영)\n' > "$AD/.claude/graph/tmp/plan2.md"
ADGE gate open --gate GATE-PLAN --artifact-file "$AD/.claude/graph/tmp/plan2.md" --summary s >/dev/null
adapprove
adhook guard-write 0 "새 계획 승인 후 다시 쓰기 허용 (사이클 완주)" "$WSRC"

# (8) 수정 상한
ADGE retry --edge gate_to_amend >/dev/null 2>&1
ADGE retry --edge gate_to_amend >/dev/null 2>&1; rc=$?
[ "$rc" -eq 3 ] && ok "gate_to_amend 상한(2) 초과 → exit 3" || bad "상한 초과인데 exit $rc"
[ "$(ADGE show --field node_status)" = "escalated" ] \
  && ok "  ↳ 상태가 escalated 로 전환 (무한 수정 루프 방지)" || bad "escalated 미전환"

# (9) 비활성이면 티켓 가드도 무해하다
ADGE abort --note done >/dev/null
adhook guard-jira 0 "비활성: 티켓 쓰기 통과" "$JEDIT"

rm -rf "$AD"

# ==========================================================================
say "26. JIRA 상태 전이 강제 (N1/N4/N5 · 매 진입)"
TD=$(mktemp -d "${TMPDIR:-/tmp}/ge-trans.XXXXXX")
mkdir -p "$TD/.claude"
cp -R "$SRC_ROOT/.claude/graph" "$TD/.claude/graph"
cp -R "$SRC_ROOT/.claude/hooks" "$TD/.claude/hooks"
rm -rf "$TD/.claude/graph/state" "$TD/.claude/graph/logs"
mkdir -p "$TD/.claude/graph/state" "$TD/.claude/graph/logs" "$TD/.claude/graph/tmp"
: > "$TD/build.gradle"
( cd "$TD" && git init -q -b main && git config user.email t@t && git config user.name t \
  && git add -A >/dev/null && git commit -qm init && git switch -q -c PPS-777 ) || bad "전이 샌드박스 준비 실패"
TGE() { CLAUDE_PROJECT_DIR="$TD" "$TD/.claude/graph/bin/ge" "$@"; }
TXN() { TGE jira-transition --node "$1" "${@:2}" >/dev/null; }
TGE init --ticket PPS-777 --base-branch main >/dev/null

# (1) 목표 상태는 config 에서 온다 — 노드별로 다르다
[ "$(TGE jira-target --node N5 | python3 -c 'import json,sys;print(json.load(sys.stdin)["nodes"]["N5"]["target_status"])')" = "검증 중" ] \
  && ok "N5 목표 상태 = 검증 중 (config.jira.transition_policy)" || bad "N5 목표 상태가 검증 중이 아니다"
[ "$(TGE jira-target --node N1 | python3 -c 'import json,sys;print(json.load(sys.stdin)["nodes"]["N1"]["target_status_id"])')" = "10060" ] \
  && ok "  ↳ known_status_ids 에서 상태 id 를 찾는다 (검토 중=10060)" || bad "상태 id 매칭 실패"

# (2) 전이 기록 없이는 진입할 수 없다 (N1/N4/N5 전부)
for n in N1 N5; do
  TGE node "$n" --status running >/dev/null 2>&1 \
    && bad "$n: 전이 기록 없이 진입됐다" || ok "$n: 전이 기록 없으면 진입 거부 (exit 2)"
done

# (3) 목표 상태가 아닌 전이는 기록 자체가 거부된다
TGE jira-transition --node N5 --to "완료" --transition-id 9 >/dev/null 2>&1 \
  && bad "목표 상태가 아닌데 기록됐다" || ok "목표 상태와 다른 전이는 기록 거부"
TGE jira-transition --node N5 >/dev/null 2>&1 \
  && bad "transition id 없이 기록됐다" || ok "  ↳ --transition-id 도 --already 도 없으면 거부"

# (4) 기록 → 진입 → 상태에 남는다
TXN N1 --from "해야 할 일" --transition-id 8 --transition-name "착수"
TGE node N1 --status running >/dev/null || bad "전이 기록 후에도 N1 진입 실패"
[ "$(TGE show --field jira.current_status)" = "검토 중" ] \
  && ok "N1 진입 후 jira.current_status = 검토 중" || bad "current_status 미기록"
[ "$(TGE show --field jira.transitions_applied.0.consumed)" = "true" ] \
  && ok "  ↳ 전이 기록이 진입에서 소비된다" || bad "consumed 미표시"

# (5) **매 진입마다** 다시 전이해야 한다 (첫 진입만이 아니다)
TGE node N1 --status running >/dev/null 2>&1 \
  && bad "소비된 기록으로 재진입됐다" || ok "재진입에는 새 전이 기록이 필요하다"

# (6) N5 → N4 루프: 되돌아온 N4 도 다시 '개발 중' 으로 전이해야 한다
TXN N5 --from "개발 중" --transition-id 5 --transition-name "검증요청"
TGE node N5 --status running >/dev/null || bad "N5 진입 실패"
printf 'AppTest > case FAILED\n' > "$TD/.claude/graph/tmp/n5.log"
TGE record-test --command "./gradlew test" --exit-code 1 --log "$TD/.claude/graph/tmp/n5.log" >/dev/null \
  && ok "전이된 N5 에서는 record-test 가 통과" || bad "record-test 가 막혔다"
TGE node N4 --status running >/dev/null 2>&1 \
  && bad "N5 실패 복귀 N4 가 전이 없이 진입됐다" || ok "N5→N4 복귀도 '개발 중' 재전이를 요구"

# (7) 이미 목표 상태면 --already 로 기록한다(확인은 실제 조회로)
TXN N5 --already --from "검증 중"
TGE node N5 --status running >/dev/null && ok "--already 기록으로 재진입 허용" || bad "--already 재진입 실패"
[ "$(TGE show --field jira.node_entries.N5)" = "2" ] \
  && ok "  ↳ 진입 횟수가 노드별로 집계된다 (N5=2)" || bad "node_entries 집계 오류"

# (8) ge node N5 를 건너뛴 경로도 record-test 에서 걸린다
TGE node N6 --status running >/dev/null
TGE record-test --command "./gradlew test" --exit-code 0 --log "$TD/.claude/graph/tmp/n5.log" >/dev/null 2>&1 \
  && ok "이미 소비된 N5 진입 안에서는 record-test 가 계속 통과" || bad "같은 진입인데 record-test 가 막혔다"

# (9) 전이 대상이 아닌 노드는 아무 영향이 없다
TGE node N2 --status running >/dev/null && ok "전이 대상 아닌 노드(N2)는 그대로 진입" || bad "N2 가 막혔다"

# (10) 그래프가 꺼지면 전이 강제도 사라진다
TGE abort --note done >/dev/null
TGE node N5 --status running >/dev/null 2>&1 \
  && bad "비활성인데 node 명령이 성공했다" || ok "비활성에서는 상태 명령 자체가 무해하다"
rm -rf "$TD"

# ==========================================================================
say "27. install.sh — 이미 설치된 프로젝트에 재설치 (config 는 병합, 덮어쓰지 않는다)"
IT=$(mktemp -d "${TMPDIR:-/tmp}/ge-install.XXXXXX")
( cd "$IT" && git init -q -b main && git config user.email t@t && git config user.name t \
  && printf '# t\n' > README.md && git add -A >/dev/null && git commit -qm init ) || bad "설치 대상 준비 실패"

INSTALL="$SRC_ROOT/.claude/graph/install.sh"
"$INSTALL" "$IT" --no-tests --no-claude-md >/dev/null 2>&1 \
  && ok "새 프로젝트에 설치 성공" || bad "새 설치 실패"

# 대상이 자기 값으로 바꾼 설정 + 템플릿의 새 키를 지운 상태를 만든다(구버전 흉내)
python3 - "$IT/.claude/graph/config.json" <<'PYSET'
import json, sys
p = sys.argv[1]
c = json.load(open(p, encoding="utf-8"))
c["jira"]["site_url"] = "https://our-team.atlassian.net"
c["jira"]["cloud_id"] = "OUR-CLOUD"
c["jira"]["project_overrides"]["ABC"] = {"status_targets": {"N1_on_receive": "Review"}}
c["git"]["allow_push"] = True
c["jira"].pop("transition_policy", None)                    # 새 기능 키가 없는 구버전
c["jira"]["status_targets"].pop("N5_on_verify", None)
json.dump(c, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PYSET

out=$("$INSTALL" "$IT" --no-tests --no-claude-md 2>&1); irc=$?
[ "$irc" -eq 0 ] && ok "재설치 성공 (exit 0)" || bad "재설치 exit $irc"
printf '%s' "$out" | grep -q "jira.transition_policy" \
  && ok "누락된 새 키를 병합했다고 보고한다" || bad "병합 보고가 없다"
printf '%s' "$out" | grep -q "설정 스키마 최신" \
  && ok "  ↳ 설치 검증이 설정 스키마를 실측한다" || bad "스키마 검증이 없다"

python3 - "$IT/.claude/graph/config.json" <<'PYCHK'
import json, sys
c = json.load(open(sys.argv[1], encoding="utf-8"))
j = c["jira"]
assert j["site_url"] == "https://our-team.atlassian.net", "site_url 이 덮였다"
assert j["cloud_id"] == "OUR-CLOUD", "cloud_id 가 덮였다"
assert c["git"]["allow_push"] is True, "git 설정이 덮였다"
assert "ABC" in j["project_overrides"], "대상 프로젝트 override 가 사라졌다"
assert j["transition_policy"]["nodes"]["N5"] == "N5_on_verify", "새 키가 안 들어왔다"
assert j["status_targets"]["N5_on_verify"] == "검증 중", "중첩된 새 키가 안 들어왔다"
PYCHK
[ $? -eq 0 ] && ok "커스터마이징은 보존되고 새 키만 추가됐다" || bad "병합이 값을 훼손했다"
[ -f "$IT/.claude/graph/config.json.bak" ] && ok "  ↳ 병합 전 원본을 .bak 으로 남긴다" || bad ".bak 백업이 없다"

# 멱등성 — 두 번째 재설치는 아무 것도 바꾸지 않는다
cp "$IT/.claude/graph/config.json" "$IT/.before"
out2=$("$INSTALL" "$IT" --no-tests --no-claude-md 2>&1)
cmp -s "$IT/.before" "$IT/.claude/graph/config.json" \
  && ok "다시 돌려도 config 가 그대로다 (멱등)" || bad "멱등이 아니다"
printf '%s' "$out2" | grep -q "설정 병합 0건" \
  && ok "  ↳ 병합할 것이 없으면 0건으로 보고한다" || bad "병합 건수 보고가 틀렸다"

# 구조가 어긋난 config 는 조용히 넘어가지 않는다 (그 기능이 통째로 꺼지므로)
python3 - "$IT/.claude/graph/config.json" <<'PYBREAK'
import json, sys
p = sys.argv[1]
c = json.load(open(p, encoding="utf-8"))
c["jira"]["transition_policy"] = "block"        # dict 여야 하는 자리에 문자열
json.dump(c, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PYBREAK
out3=$("$INSTALL" "$IT" --no-tests --no-claude-md 2>&1); brc=$?
[ "$brc" -ne 0 ] && ok "구조가 깨진 설정이면 설치가 실패로 끝난다 (exit $brc)" || bad "깨진 설정인데 exit 0"
printf '%s' "$out3" | grep -q "구조 불일치" \
  && ok "  ↳ 어느 키가 어긋났는지 알려준다" || bad "구조 불일치 보고가 없다"
printf '%s' "$out3" | grep -q '"block"' && bad "깨진 값을 그대로 덮어썼다" \
  || ok "  ↳ 값을 멋대로 고치지 않는다"

# 진행 중인 그래프가 있는 대상에 덧설치 — 훅이 차단하는 게 정상이므로 실패로 보면 안 된다
python3 - "$IT/.claude/graph/config.json" <<'PYFIX'
import json, sys
p = sys.argv[1]
c = json.load(open(p, encoding="utf-8"))
c["jira"]["transition_policy"] = {"enforce": "block",
                                  "nodes": {"N1": "N1_on_receive", "N4": "N4_on_develop",
                                            "N5": "N5_on_verify"}}
json.dump(c, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PYFIX
( cd "$IT" && git switch -q -c ABC-1 ) >/dev/null 2>&1
CLAUDE_PROJECT_DIR="$IT" "$IT/.claude/graph/bin/ge" init --ticket ABC-1 --base-branch main >/dev/null 2>&1 \
  && ok "대상에서 그래프를 켰다 (덧설치 시나리오 준비)" || bad "대상 그래프 시작 실패"

out4=$("$INSTALL" "$IT" --no-tests --no-claude-md 2>&1); arc=$?
[ "$arc" -eq 0 ] && ok "진행 중인 그래프가 있어도 설치는 성공한다 (exit 0)" \
  || bad "활성 그래프 때문에 설치가 실패했다 (exit $arc)"
printf '%s' "$out4" | grep -q "설치가 잘못됐습니다" \
  && bad "정상 차단을 설치 오류로 보고한다" || ok "  ↳ 정상 차단을 오류로 오인하지 않는다"
printf '%s' "$out4" | grep -q "비활성 훅 프로브 건너뜀 — 대상에 진행 중인 그래프가 있습니다 (티켓 ABC-1)" \
  && ok "  ↳ 어떤 티켓이 돌고 있어서 건너뛰는지 알려준다" || bad "건너뛴 이유 보고가 없다"
printf '%s' "$out4" | grep -q "진행 중인 그래프: ABC-1" \
  && ok "  ↳ 활성인데 '비활성' 이라고 말하지 않는다" || bad "활성 그래프를 비활성으로 보고한다"
printf '%s' "$out4" | grep -q '"current_node"' \
  && bad "검증 줄에 상태 JSON 전체를 쏟는다" || ok "  ↳ 상태 전문을 출력에 쏟지 않는다"

CLAUDE_PROJECT_DIR="$IT" "$IT/.claude/graph/bin/ge" abort --note done >/dev/null 2>&1
out5=$("$INSTALL" "$IT" --no-tests --no-claude-md 2>&1)
printf '%s' "$out5" | grep -q "비활성 훅 무해함" \
  && ok "그래프를 끄면 비활성 프로브가 다시 실행된다" || bad "비활성 프로브가 돌지 않았다"

rm -rf "$IT"

# ==========================================================================
printf '\n\033[1m======== 결과: %d PASS / %d FAIL ========\033[0m\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
