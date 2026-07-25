#!/usr/bin/env bash
# build.sh — end-to-end build of the cc-symbolic patched binary.
#
# Phasen (alle idempotent, jede überspringt was schon passt):
#   1. fetch_base    : holt claude.orig von npm (überspringt wenn vorhanden)
#   2. gen_patches   : python3 gen_patch.py -> patches.json + ascii-escape check
#   3. syntax_check  : node --check auf dem Helper
#   4. apply_patches : python3 patch.py claude.orig out/claude.patched patches.json
#   5. smoke         : --version + stage-trace dry-run (decider/judge/translator
#                      müssen ohne Decider-Misbehavior durchlaufen)
#   6. tests         : test_thinking_wrapper + test_escalation + test_hooka_fallback
#                      (unit, HELPER via vm sandbox) + test_e2e (binary + mock)
#
# Run:
#   ./build.sh                # build + tests (default)
#   ./build.sh tests          # nur tests (binary muss existieren)
#   ./build.sh rebuild        # erzwingt alle Phasen (clean+fetch_base+...) + tests
#   ./build.sh clean          # räumt out/, extracted/, patches.json
#   ./build.sh inspect        # extrahiert cli.js (für Debug, kein Build)
#   ./build.sh preflight      # nur Phase 1-3 (Source-Validierung, kein Binary)
#   ./build.sh phase NAME     # einzelne Phase (fetch_base|gen_patches|syntax_check
#                              |apply_patches|smoke)
#
# Stand-alone von außen aufrufbar (kein env nötig).  Der v2-era `cc2stage`-
# Launcher ist nicht betroffen — der nutzt andere Env-Namen (TWO_STAGE_*); der
# cc-symbolic-Pfad nutzt symbolic_thinking_*.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

VERSION="${CLAUDE_VERSION:-2.1.214}"
PLATFORM="${PLATFORM:-linux-x64}"
BASE="claude.orig"
OUT="out/claude.patched"
EXTRACTED="extracted/src/entrypoints/cli.js"
LOG="/tmp/build-cc-symbolic.log"

# -------- helpers ----------------------------------------------------------
say() { printf '\033[1;36m[build]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[build]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[build]\033[0m %s\n' "$*" >&2; }
ok() { printf '\033[1;32m[build]\033[0m %s\n' "$*"; }

require() {
    command -v "$1" >/dev/null 2>&1 || { err "missing: $1"; exit 127; }
}
require python3
require node
require npm

# Wenn die OUT-Datei läuft, bricht mmap/write fehl ("text file busy") oder
# die laufende cc-symbolic-Session würde korruptiert.  Finde Prozesse, die
# die Datei WIRKLICH offen halten (mmap'd executable / open fd), NICHT
# Prozesse, deren Command-Line den Pfad nur erwähnt — sonst matcht pgrep
# build.sh selbst, patch.py (hat $OUT in den args) und die aufrufende Shell
# (self-match), und der Build kann nie schreiben.  fuser prüft echte
# File-Deskriptoren; Fallback auf /proc/*/exe wenn fuser fehlt.
release_out_lock() {
    [[ -f "$OUT" ]] || return 0
    local pids=""
    if command -v fuser >/dev/null 2>&1; then
        # fuser prints PIDs to stdout, filename to stderr. PIDs may carry a
        # one-letter usage code (e/f/r/c) in default mode — strip non-digits.
        # `|| true` because grep exits 1 (and pipefail + set -e would abort
        # the whole build) when there are NO holders — which is the normal case.
        pids="$(fuser "$OUT" 2>/dev/null | tr -cs '0-9' '\n' | grep -E '^[0-9]+$' | sort -u | tr '\n' ' ' || true)"
    else
        local abs pid exe
        abs="$(readlink -f "$OUT")"
        for pid in /proc/[0-9]*; do
            exe="$(readlink -f "$pid/exe" 2>/dev/null)" || continue
            [[ "$exe" == "$abs" ]] && pids="$pids ${pid#/proc/}"
        done
        pids="$(echo $pids | tr -s ' ' '\n' | grep -E '^[0-9]+$' | sort -u | tr '\n' ' ' || true)"
    fi
    if [[ -n "${pids// /}" ]]; then
        warn "$OUT is held open by: $(echo $pids | tr '\n' ' ')"
        warn "  (a running cc-symbolic session has the binary mmap'd — writing now"
        warn "   would fail with 'text file busy' or corrupt that session.  We will"
        warn "   NOT kill it automatically.  Run 'pkill -f claude.patched' or close"
        warn "   the cc-symbolic session, then re-run build.sh.)  Build aborted."
        exit 2
    fi
}

# -------- phases -----------------------------------------------------------

phase_fetch_base() {
    [[ "${REBUILD:-0}" -eq 1 ]] && rm -f "$BASE"
    if [[ -f "$BASE" ]]; then say "base present: $BASE ($(stat -c%s "$BASE") bytes)"; return 0; fi
    say "fetching @anthropic-ai/claude-code-$PLATFORM@$VERSION"
    npm pack "@anthropic-ai/claude-code-$PLATFORM@$VERSION" >/dev/null
    local tgz
    tgz=$(ls anthropic-ai-claude-code-"$PLATFORM"-*.tgz | tail -1)
    mkdir -p _pkg && tar xzf "$tgz" -C _pkg --strip-components=1
    cp _pkg/claude "$BASE"; chmod +x "$BASE"
    rm -rf _pkg "$tgz"
    ok "base: $BASE ($(stat -c%s "$BASE") bytes)"
}

phase_gen_patches() {
    [[ "${REBUILD:-0}" -eq 1 ]] && rm -f patches.json
    [[ -f patches.json ]] && [[ "${REBUILD:-0}" -eq 0 ]] && \
        { say "patches.json present ($(stat -c%s patches.json) bytes)"; return 0; }
    say "running gen_patch.py"
    python3 gen_patch.py 2>&1 | tee -a "$LOG"
    [[ -f patches.json ]] || { err "gen_patch.py did not produce patches.json"; exit 3; }
    ok "patches.json ($(stat -c%s patches.json) bytes)"
}

phase_syntax_check() {
    say "node --check on the helper"
    python3 - <<'PY' >/tmp/sym_helper.js
import json, base64
patches = json.load(open("patches.json"))
iife = next(p for p in patches if "helper" in p.get("note", ""))
repl = base64.b64decode(iife["replacement"]).decode("utf-8")
helper = repl.split("(function(exports, require, module, __filename, __dirname) {", 1)[1]
print(helper, end="")
PY
    node --check /tmp/sym_helper.js || { err "helper JS invalid"; exit 4; }
    ok "helper JS OK"

    say "ascii-escape check (no raw non-ASCII in patched src)"
    if LC_ALL=C grep -P '[^\x00-\x7F]' /tmp/sym_helper.js >/dev/null 2>&1; then
        err "non-ASCII chars in helper (Bun source-parse risk)"; exit 5
    fi
    ok "helper is pure-ASCII"
}

phase_apply_patches() {
    release_out_lock
    [[ "${REBUILD:-0}" -eq 1 ]] && rm -f "$OUT"
    say "applying patches.json -> $OUT"
    python3 patch.py "$BASE" "$OUT" patches.json 2>&1 | tee -a "$LOG"
    [[ -x "$OUT" ]] || { err "patch.py did not produce $OUT"; exit 6; }
    ok "wrote: $OUT ($(stat -c%s "$OUT") bytes)"
}

phase_smoke() {
    say "smoke: $OUT --version"
    local ver
    ver="$("$OUT" --version 2>&1 | head -1)"
    echo "  -> $ver"
    case "$ver" in
        *"${VERSION}"*) ok "version matches ($VERSION)" ;;
        *) warn "version mismatch (expected $VERSION, got: $ver)" ;;
    esac
    # The 3-stage mock canary (binary vs mock_anthropic_api, 'text file busy' /
    # 'Spread syntax' / Ti.value / .then regressions) is covered by the
    # 'tests' phase via test_e2e.sh, which starts its own mock on 18765 and
    # sets ANTHROPIC_BASE_URL correctly. The old in-smoke canary did NOT set
    # ANTHROPIC_BASE_URL, so the binary never hit the mock and the empty
    # `hits=$(grep ...)` aborted the build under `set -euo pipefail`.
    say "smoke: 3-stage canary deferred to 'tests' phase (test_e2e.sh)"
}

phase_tests() {
    say "running test suites"
    local fails=0 t
    # Unit/logic suites (no network, no tokens): HELPER via vm sandbox.
    for t in test_thinking_wrapper.js test_escalation.js test_hooka_fallback.js; do
        if [[ -f "$t" ]]; then
            if node "$t" >/tmp/build-test-${t%.js}.log 2>&1; then
                ok "$t passed"
            else
                err "$t FAILED (see /tmp/build-test-${t%.js}.log)"
                fails=$((fails+1))
            fi
        else
            warn "$t missing — skipping"
        fi
    done
    # E2E: starts a mock Anthropic API + runs the freshly-built binary through
    # the 3-stage symbolic pipeline. Guards against the original Ti.value /
    # .then / Unexpected-event-order crashes. ~10s.
    if [[ -x test_e2e.sh ]]; then
        if ./test_e2e.sh >/tmp/build-test-e2e.log 2>&1; then
            ok "test_e2e.sh passed"
        else
            err "test_e2e.sh FAILED (see /tmp/build-test-e2e.log)"
            fails=$((fails+1))
        fi
    else
        warn "test_e2e.sh missing — skipping E2E"
    fi
    if [[ $fails -gt 0 ]]; then
        err "$fails test suite(s) failed — build aborted"
        exit 7
    fi
    ok "all test suites passed"
}

# -------- dispatch ---------------------------------------------------------

PHASES=(fetch_base gen_patches syntax_check apply_patches smoke tests)

run_phase() {
    case "$1" in
        fetch_base)    phase_fetch_base ;;
        gen_patches)   phase_gen_patches ;;
        syntax_check)  phase_syntax_check ;;
        apply_patches) phase_apply_patches ;;
        smoke)         phase_smoke ;;
        tests)         phase_tests ;;
        *) err "unknown phase: $1"; exit 1 ;;
    esac
}

# Start-log
mkdir -p "$(dirname "$LOG")"
: > "$LOG"
say "build.sh start  (CLAUDE_VERSION=$VERSION  PLATFORM=$PLATFORM  REBUILD=${REBUILD:-0})"

case "${1:-build}" in
    clean)
        rm -rf out _pkg extracted anthropic-ai-claude-code-*.tgz patches.json
        ok "cleaned out/ extracted/ patches.json"
        ;;
    inspect)
        phase_fetch_base
        say "extracting modules (bytecode omitted)"
        npx -y unbunjs extract "$BASE" ./extracted >/dev/null
        rm -f extracted/src/entrypoints/cli.js.bytecode
        ok "cli.js: $EXTRACTED ($(stat -c%s "$EXTRACTED") bytes)"
        ;;
    preflight)
        for p in fetch_base gen_patches syntax_check; do run_phase "$p"; done
        ok "preflight OK (no binary built)"
        ;;
    rebuild)
        REBUILD=1
        for p in "${PHASES[@]}"; do run_phase "$p"; done
        ok "rebuild complete"
        ;;
    phase)
        [[ $# -ge 2 ]] || { err "usage: $0 phase NAME"; exit 1; }
        run_phase "$2"
        ;;
    tests)
        # Run only the test suites (no build). Requires the binary to exist
        # for test_e2e.sh; the unit suites load the HELPER from gen_patch.py.
        [[ -x "$OUT" ]] || { err "tests: $OUT not built — run ./build.sh first"; exit 6; }
        phase_tests
        ok "tests complete"
        ;;
    build|"")
        for p in "${PHASES[@]}"; do run_phase "$p"; done
        ok "build complete"
        ;;
    *)
        cat <<EOF
build.sh — end-to-end build for the cc-symbolic patched binary.

Usage:
  ./build.sh                build + test (idempotent — skips what is current)
  ./build.sh rebuild        wipe + rebuild all phases + test
  ./build.sh tests          run only the test suites (build must exist)
  ./build.sh preflight      source-only: gen_patch + syntax check (no binary)
  ./build.sh phase NAME     one phase: fetch_base|gen_patches|syntax_check
                            |apply_patches|smoke|tests
  ./build.sh inspect        extract cli.js (for debugging — no build)
  ./build.sh clean          remove out/ extracted/ patches.json

Env:
  CLAUDE_VERSION=2.1.214    base binary version (default $VERSION)
  PLATFORM=linux-x64        npm platform package (default $PLATFORM)
  REBUILD=1                 force every phase (used by 'rebuild' target)
EOF
        exit 1
        ;;
esac