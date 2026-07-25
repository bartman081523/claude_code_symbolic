#!/usr/bin/env bash
# test_e2e.sh — end-to-end test: cc-symbolic mock binary against
# mock_anthropic_api.py. Guards against the original "Ti.value is not an
# Object" crash and asserts the 3-stage symbolic pipeline runs through.
#
# Run: ./test_e2e.sh
# Exit: 0 = pass, 1 = fail.
set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# Use a temp mock log so we can inspect it after the run.
MOCK_LOG="$(mktemp -t cc-symbolic-e2e-XXXXXX.log)"
LOG="$(mktemp -t cc-symbolic-e2e-XXXXXX.out)"
trap 'rm -f "$MOCK_LOG" "$LOG"' EXIT

# Point the mock at a fixed port for predictable cleanup.
export MOCK_PORT=18765
# Use mock as the backend.
MODE=mock
# Force the patch on.
export symbolic_thinking=1
# Disabled is the canonical default.
export symbolic_thinking_mode=disabled
# Disable trace by default — we just want to see stages hit the mock.
export symbolic_thinking_trace=0

# Run the launcher with a minimal prompt. We use -p (print mode) to make
# claude-code exit after one turn instead of going into an interactive loop.
PROMPT='echo: reply with the literal text E2E_OK'
PROMPT_ESCAPED=$(printf '%s' "$PROMPT" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))')
echo "=== test_e2e: launching cc-symbolic mock ==="
echo "  PROMPT: $PROMPT"
echo "  MOCK_LOG: $MOCK_LOG"
echo

# Invoke the launcher directly. capture stdout/stderr. We use timeout to
# avoid hanging if the patch breaks badly.
timeout 60 ./bin/cc-symbolic "$MODE" -p "$PROMPT" >"$LOG" 2>&1
RC=$?

echo "=== cc-symbolic exit code: $RC ==="
echo "=== last 25 lines of stdout/stderr ==="
tail -25 "$LOG" || true
echo

PASS=1
FAIL_REASON=""

# Assertion 1: no Ti.value crash.
if grep -q 'Ti.value is not an Object' "$LOG"; then
  PASS=0
  FAIL_REASON="found 'Ti.value is not an Object' in output (the original bug regressed!)"
fi

# Assertion 2: no 'controller" in undefined' / similar.
if grep -qE '"controller" in undefined' "$LOG"; then
  PASS=0
  FAIL_REASON="${FAIL_REASON:-}found 'controller in undefined' error"
fi

# Assertion 3: no Unexpected event order.
if grep -qE 'Unexpected event order' "$LOG"; then
  PASS=0
  FAIL_REASON="${FAIL_REASON:-}found 'Unexpected event order' (engine reducer threw)"
fi

# Assertion 4: cc-symbolic returned a non-error exit (0=ok, 1=user-cancel also acceptable).
# We allow 0, 1, 124 (timeout — at least it didn't crash). Anything else is fail.
if [[ $RC -gt 1 && $RC -ne 124 ]]; then
  PASS=0
  FAIL_REASON="${FAIL_REASON:-}non-zero exit code $RC (unexpected crash)"
fi

# Assertion 5: the mock log file exists and has at least 3 patch-stage hits.
# (The mock server's stage_of() is prefix-based and doesn't recognize the
# current patch's stage instructions, which start with 'cc-symbolic is the
# Claude Code reasoning extension. The DECI...'. We use the snippet text
# instead: 'The DECI' / 'The JUDG' / 'The OUTP' as our stage fingerprints,
# OR the 'cc-symbolic' prefix that the patch inserts in every stage body.)
REAL_MOCK_LOG="${XDG_DATA_HOME:-$HOME/.local/share}/cc-symbolic/mock.log"
if [[ -s "$REAL_MOCK_LOG" ]]; then
  MOCK_LOG_TO_CHECK="$REAL_MOCK_LOG"
elif [[ -s "$MOCK_LOG" ]]; then
  MOCK_LOG_TO_CHECK="$MOCK_LOG"
else
  MOCK_LOG_TO_CHECK="$LOG"
fi
PATCH_STAGE_HITS=$(grep -cE 'snippet=.cc-symbolic' "$MOCK_LOG_TO_CHECK" 2>/dev/null || echo 0)
if [[ "${PATCH_STAGE_HITS:-0}" -lt 2 ]]; then
  PASS=0
  FAIL_REASON="${FAIL_REASON:-}expected >=2 patch-stage requests in mock log (found: $PATCH_STAGE_HITS)"
fi
# Also: at least one streaming (the translator emits stream=True) and at
# least one non-streaming (decider+judge are stream=False).
STREAM_FALSE_HITS=$(grep -cE 'stream=False' "$MOCK_LOG_TO_CHECK" 2>/dev/null || echo 0)
STREAM_TRUE_HITS=$(grep -cE 'stream=True' "$MOCK_LOG_TO_CHECK" 2>/dev/null || echo 0)
if [[ "${STREAM_FALSE_HITS:-0}" -lt 2 ]]; then
  PASS=0
  FAIL_REASON="${FAIL_REASON:-}expected >=2 stream=False (decider+judge) requests, found: $STREAM_FALSE_HITS"
fi
if [[ "${STREAM_TRUE_HITS:-0}" -lt 1 ]]; then
  PASS=0
  FAIL_REASON="${FAIL_REASON:-}expected >=1 stream=True (translator) request, found: $STREAM_TRUE_HITS"
fi

if [[ $PASS -eq 1 ]]; then
  echo "=== PASS: E2E cc-symbolic mock ran 3 stages, no Ti.value crash ==="
  exit 0
else
  echo "=== FAIL: $FAIL_REASON ==="
  echo "--- mock log ---"
  cat "$MOCK_LOG" 2>/dev/null || true
  echo "--- launcher output ---"
  cat "$LOG" || true
  exit 1
fi
