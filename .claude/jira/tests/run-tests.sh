#!/usr/bin/env bash
# JIRA Ticket 양식 검증기·훅 회귀 테스트.
#
# 실제 저장소를 건드리지 않는다: .claude/jira 와 .claude/hooks 를 임시 샌드박스로 복사하고
# 거기서 CLI 와 훅을 합성 stdin JSON 으로 구동한다.
#
#   사용: .claude/jira/tests/run-tests.sh
set -u

SRC_ROOT=$(cd -- "$(dirname -- "$0")/../../.." && pwd)
SB=$(mktemp -d "${TMPDIR:-/tmp}/jt-test.XXXXXX")
PASS=0; FAIL=0

cleanup() { rm -rf "$SB"; }
trap cleanup EXIT

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()  { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$*"; }
bad() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$*"; }

mkdir -p "$SB/.claude"
cp -R "$SRC_ROOT/.claude/jira"  "$SB/.claude/jira"
cp -R "$SRC_ROOT/.claude/hooks" "$SB/.claude/hooks"
cp -R "$SRC_ROOT/.claude/graph" "$SB/.claude/graph"
cp -R "$SRC_ROOT/.claude/skills" "$SB/.claude/skills"
rm -rf "$SB/.claude/jira/drafts" "$SB/.claude/graph/state"
mkdir -p "$SB/.claude/jira/drafts" "$SB/.claude/graph/state" "$SB/work"
export CLAUDE_PROJECT_DIR="$SB"

JT() { "$SB/.claude/jira/bin/jt" "$@"; }
hook() {  # hook <스크립트> <기대exit> <설명> <payload>
  printf '%s' "$4" | "$SB/.claude/hooks/jt-$1.sh" >/dev/null 2>"$SB/.err"
  rc=$?
  if [ "$rc" -eq "$2" ]; then ok "$3 (exit $rc)"
  else bad "$3 — 기대 $2, 실제 $rc"; head -3 "$SB/.err" | sed 's/^/       /'; fi
}
approve() { printf '{"prompt":"%s"}' "${1:-approve}" \
  | "$SB/.claude/hooks/jt-approval.sh" 2>/dev/null; }
vio() { JT validate --file "$1" --json 2>/dev/null \
  | python3 -c "import json,sys;print(' '.join(v['code'] for v in json.load(sys.stdin)['violations']))"; }

GOOD="$SB/work/good.md"
cat > "$GOOD" <<'MD'
# 요약

[cutting-tuna] 사용자 조회 API 에 404 응답 추가

# 설명

## 대상
- 저장소: cutting-tuna
- 모듈/패키지: api/user
- 진입점(아는 만큼): UserController.get

## 배경
- 현재(As-Is): 없는 id 로 조회하면 500 이 나간다.
- 원하는 상태(To-Be): 404 와 USER_NOT_FOUND 를 반환한다.
- 왜 해야하는가: 클라이언트가 장애와 정상 흐름을 구분하지 못한다.

## 완료 정의
( ) 조사, 보고까지 (O) 배포까지

## 참고
- 관련 PR/이슈: #412
- 관련 티켓: PPS-201
- 로그, 재현 데이터: api-prod 2026-09-08

# 요구사항

## 완료 조건
- [ ] 없는 id 로 GET /api/users/{id} 호출 시 404 와 USER_NOT_FOUND 를 반환한다
- [ ] 기존 200 응답의 필드 구성이 바뀌지 않는다

## 범위 밖 — 이번에 안 하는 것
- 다른 엔드포인트의 에러 응답 표준화

## 제약
- 하위호환: 유지 필요(호출처: order-service)
- DB 스키마 변경: 불가
- 성능, 보안: 없음
MD

CREATE='{"tool_name":"mcp__atlassian__createJiraIssue","tool_input":{"summary":"[cutting-tuna] 사용자 조회 API 에 404 응답 추가"}}'

# ==========================================================================
say "1. 양식 검증 — 빈 양식은 통과할 수 없다"
JT template > "$SB/work/empty.md"
JT validate --file "$SB/work/empty.md" >/dev/null 2>&1
[ $? -eq 2 ] && ok "빈 양식 → exit 2" || bad "빈 양식이 통과했다"
for code in TEMPLATE_ECHO SUMMARY_PLACEHOLDER TARGET_REPO_EMPTY BACKGROUND_EMPTY \
            DOD_NOT_CHOSEN AC_PLACEHOLDER OOS_EMPTY CONSTRAINT_UNDECIDED; do
  vio "$SB/work/empty.md" | grep -q "$code" \
    && ok "  $code 를 잡는다" || bad "  $code 를 놓쳤다"
done

say "2. 제대로 채운 초안은 통과한다"
JT validate --file "$GOOD" >/dev/null 2>&1
[ $? -eq 0 ] && ok "완성된 초안 → exit 0" || { bad "완성된 초안이 막혔다"; JT validate --file "$GOOD"; }

say "3. 항목별 위반을 정확히 집어낸다"
mk() { sed "$1" "$GOOD" > "$SB/work/x.md"; }
mk 's/^\[cutting-tuna\] 사용자.*/괄호 없는 요약/'
vio "$SB/work/x.md" | grep -q SUMMARY_FORMAT && ok "요약에 [repo] 없음" || bad "SUMMARY_FORMAT 미검출"
mk 's/^- 저장소: cutting-tuna/- 저장소:/'
vio "$SB/work/x.md" | grep -q TARGET_REPO_EMPTY && ok "저장소 빈칸" || bad "TARGET_REPO_EMPTY 미검출"
mk 's/^- 왜 해야하는가:.*/- 왜 해야하는가:/'
vio "$SB/work/x.md" | grep -q BACKGROUND_EMPTY && ok "배경 빈칸" || bad "BACKGROUND_EMPTY 미검출"
mk 's/^( ) 조사, 보고까지 (O) 배포까지/(O) 조사, 보고까지 (O) 배포까지/'
vio "$SB/work/x.md" | grep -q DOD_MULTIPLE && ok "완료 정의 둘 다 선택" || bad "DOD_MULTIPLE 미검출"
mk '/^- \[ \] /d'
vio "$SB/work/x.md" | grep -q AC_EMPTY && ok "완료 조건 없음" || bad "AC_EMPTY 미검출"
mk 's/^- 다른 엔드포인트의 에러 응답 표준화//'
vio "$SB/work/x.md" | grep -q OOS_EMPTY && ok "범위 밖 빈칸 (없음 이라도 써야 한다)" || bad "OOS_EMPTY 미검출"
mk 's|^- DB 스키마 변경: 불가|- DB 스키마 변경: 허용 / 불가|'
vio "$SB/work/x.md" | grep -q CONSTRAINT_UNDECIDED && ok "제약 선택지 미결정" || bad "CONSTRAINT_UNDECIDED 미검출"
sed 's/^- 저장소: cutting-tuna/- 저장소: other-repo/' "$GOOD" > "$SB/work/x.md"
JT validate --file "$SB/work/x.md" --json 2>/dev/null \
  | python3 -c "import json,sys;d=json.load(sys.stdin);sys.exit(0 if any(w['code']=='TARGET_REPO_MISMATCH' for w in d['warnings']) and d['ok'] else 1)" \
  && ok "요약/저장소 불일치는 경고(차단 아님)" || bad "TARGET_REPO_MISMATCH 처리 오류"

say "4. 양식 위반이면 초안 등록 자체가 안 된다"
JT draft --file "$SB/work/empty.md" >/dev/null 2>&1
[ $? -ne 0 ] && ok "검증 실패 초안은 등록 거부" || bad "위반 초안이 등록됐다"
[ -f "$SB/.claude/jira/drafts/current.json" ] && bad "거부됐는데 초안이 생겼다" || ok "  ↳ 상태를 남기지 않는다"

say "5. 승인 없이는 티켓을 만들 수 없다"
hook guard-create 2 "초안이 아예 없으면 생성 차단 (스킬 우회 방지)" "$CREATE"
JT draft --file "$GOOD" --project PPS >/dev/null 2>&1
[ -f "$SB/.claude/jira/drafts/current.json" ] && ok "초안 등록됨" || bad "초안 등록 실패"
hook guard-create 2 "등록만 하고 승인 전 → 생성 차단" "$CREATE"
JT approve --decision approved --source hook:UserPromptSubmit >/dev/null 2>&1
[ $? -ne 0 ] && ok "모델이 직접 승인 시도 → 거부 (JT_HOOK_AUTH)" || bad "모델이 승인에 성공했다"

say "6. 사용자 입력에서만 승인이 발화한다"
approve >/dev/null
[ "$(JT show --field approval.decision | tr -d '\"')" = "approved" ] \
  && ok "채팅 approve → 승인 기록" || bad "승인이 기록되지 않았다"
[ "$(JT show --field approval.source | tr -d '\"')" = "hook:UserPromptSubmit" ] \
  && ok "  ↳ 출처가 훅으로 기록됨" || bad "승인 출처 오류"
hook guard-create 0 "승인 후 생성 허용" "$CREATE"

say "7. 승인은 초안 해시에 묶인다"
hook guard-create 2 "승인된 것과 다른 요약으로 생성 시도 → 차단" \
  '{"tool_name":"mcp__atlassian__createJiraIssue","tool_input":{"summary":"몰래 다른 티켓"}}'
sed 's/404 응답 추가/500 그대로 두기/' "$GOOD" > "$SB/work/tampered.md"
JT draft --file "$SB/work/tampered.md" >/dev/null 2>&1
hook guard-create 2 "초안을 고치면 이전 승인이 무효" "$CREATE"

say "8. 초안 상태 파일은 jt 로만 쓴다"
hook guard-write 2 "초안 파일 Edit/Write 차단" \
  '{"tool_name":"Write","tool_input":{"file_path":".claude/jira/drafts/current.json"}}'
hook guard-write 2 "셸 리다이렉션으로 초안 조작 차단" \
  '{"tool_name":"Bash","tool_input":{"command":"echo x > .claude/jira/drafts/current.json"}}'
hook guard-write 0 "일반 소스 쓰기는 통과" \
  '{"tool_name":"Write","tool_input":{"file_path":"src/App.java"}}'
hook guard-write 0 "초안 읽기(cat)는 통과" \
  '{"tool_name":"Bash","tool_input":{"command":"cat .claude/jira/drafts/current.json"}}'

say "9. 생성 기록과 재사용 방지"
JT draft --file "$GOOD" >/dev/null 2>&1
approve >/dev/null
JT created --key PPS-901 >/dev/null 2>&1
[ "$(JT show --field created.key | tr -d '\"')" = "PPS-901" ] \
  && ok "생성 결과 기록" || bad "생성 기록 실패"
JT show --field created.url | grep -q "browse/PPS-901" \
  && ok "  ↳ URL 은 graph 설정의 site_url 을 따른다" || bad "URL 폴백 실패"
[ -f "$SB/.claude/jira/drafts/PPS-901.json" ] \
  && ok "  ↳ 초안이 티켓 키로 아카이브됨" || bad "아카이브 안 됨"
hook guard-create 2 "이미 생성된 초안 재사용 차단" "$CREATE"

say "10. Graph 게이트와 승인어가 충돌하지 않는다"
JT draft --file "$GOOD" >/dev/null 2>&1
printf 'PPS-283\n' > "$SB/.claude/graph/state/current"
python3 -c "
import json,sys
json.dump({'ticket':'PPS-283','gates':{'pending':{'gate':'GATE-PLAN','code':'aaaaaa'}}},
          open('$SB/.claude/graph/state/PPS-283.json','w'))"
out=$(approve)
printf '%s' "$out" | grep -q "그래프 몫" \
  && ok "그래프 게이트 대기 중이면 일반 approve 를 가로채지 않는다" || bad "그래프 승인어를 가로챘다"
[ "$(JT show --field approval | tr -d ' \n')" = "null" ] \
  && ok "  ↳ 티켓 승인은 기록되지 않았다" || bad "티켓이 승인돼버렸다"
approve "/jira-approve" >/dev/null
[ "$(JT show --field approval.decision | tr -d '\"')" = "approved" ] \
  && ok "  ↳ /jira-approve 는 언제나 티켓에만 적용된다" || bad "/jira-approve 가 동작하지 않았다"
rm -f "$SB/.claude/graph/state/current"

say "11. 반려"
JT draft --file "$GOOD" >/dev/null 2>&1
approve "reject 완료 조건이 부족하다" >/dev/null
[ "$(JT show --field approval.decision | tr -d '\"')" = "rejected" ] \
  && ok "reject 기록" || bad "반려가 기록되지 않았다"
hook guard-create 2 "  ↳ 반려 상태에서는 생성 차단" "$CREATE"

say "12. 초안이 없으면 완전히 무해하다"
JT clear >/dev/null
hook approval 0 "비활성: UserPromptSubmit 통과" '{"prompt":"approve"}'
hook guard-write 0 "비활성: 일반 쓰기 통과" \
  '{"tool_name":"Write","tool_input":{"file_path":"src/App.java"}}'
out=$(printf '{"prompt":"approve"}' | "$SB/.claude/hooks/jt-approval.sh" 2>/dev/null)
[ -z "$out" ] && ok "  ↳ 출력이 없다(컨텍스트 오염 없음)" || bad "비활성인데 출력이 있다: $out"

say "13. 그래프가 소비하는 양식과 같은 규격이다"
python3 - "$GOOD" <<'PY'
import re, sys
md = open(sys.argv[1], encoding="utf-8").read()
need = ["## 대상", "## 배경", "## 완료 정의", "## 참고",
        "## 완료 조건", "## 범위 밖", "## 제약"]
missing = [s for s in need if s not in md]
sys.exit(1 if missing else 0)
PY
[ $? -eq 0 ] && ok "N2a 필수 섹션을 모두 포함한다" || bad "그래프가 요구하는 섹션이 빠졌다"
grep -q "완료 조건" "$SRC_ROOT/.claude/skills/graph-engineering/references/jira.md" \
  && ok "  ↳ graph 의 jira.md 와 같은 섹션 이름을 쓴다" || bad "양식 규격이 어긋났다"

# ==========================================================================
printf '\n\033[1m======== 결과: %d PASS / %d FAIL ========\033[0m\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
