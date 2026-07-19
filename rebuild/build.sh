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
#
# Run:
#   ./build.sh                # alles, was nicht idempotent passt
#   ./build.sh rebuild        # erzwingt alle Phasen (clean+fetch_base+...)
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

# Wenn die OUT-Datei läuft, bricht mmap/write fehl.  Suche den Halter-Prozess
# und beende ihn sauber, bevor wir schreiben.  Idempotent: ohne Lock passiert
# nichts.
release_out_lock() {
    [[ -f "$OUT" ]] || return 0
    local pids
    pids="$(pgrep -fa "$OUT" 2>/dev/null | awk '{print $1}' | sort -u || true)"
    if [[ -n "$pids" ]]; then
        warn "out/claude.patched is held by: $(echo $pids | tr '\n' ' ')"
        warn "  (we will NOT kill other users' sessions; please run"
        warn "   'pkill -f out/claude.patched' in a separate shell, then"
        warn "   re-run build.sh).  Build aborted."
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

    say "smoke: 'text file busy' / 'Spread syntax' canary check (with mock)"
    # Mini-canary gegen mock — bestätigt, dass der symbolic-Pfad überhaupt
    # triggerbar ist und die Stages durchlaufen, ohne den Mock zu starten.
    # Wenn MOCK_BIN / Mock-Server fehlt, überspringen wir (kein Hard-Fail).
    if [[ -x tools/mock_anthropic_api.py ]]; then
        MOCK_PORT=8765 python3 tools/mock_anthropic_api.py >/tmp/build-mock.log 2>&1 &
        local mock_pid=$!
        trap "kill $mock_pid 2>/dev/null || true" EXIT
        for _ in 1 2 3 4 5 6 7 8; do
            curl -sS -o /dev/null --max-time 1 "http://127.0.0.1:8765/v1/messages" \
                -X POST -H 'content-type: application/json' \
                --data '{"model":"mock-model","max_tokens":1,"messages":[{"role":"user","content":"x"}]}' \
                2>/dev/null && break
            sleep 0.3
        done
        local tmp; tmp="$(mktemp -d)"
        if ( cd "$tmp" && MOCK_PORT=8765 symbolic_thinking_debug=1 \
              timeout 60 "$DIR/out/claude.patched" \
              --allowedTools 'Read Write Edit Bash' \
              -p 'Schreibe 42 in answer.txt und bestätige.' \
              >/tmp/build-run.log 2>&1 ); then
            local hits
            hits=$(grep -oE 'stage=(decider|judge|translator)' /tmp/build-mock.log | sort | uniq -c)
            echo "$hits" | sed 's/^/    /'
            if echo "$hits" | grep -q 'translator'; then
                ok "3-stage pipeline hit the mock (decider + judge + translator)"
            else
                warn "translator stage did not hit the mock — see /tmp/build-mock.log"
            fi
        else
            warn "canary run failed (rc=$?) — see /tmp/build-run.log"
        fi
        rm -rf "$tmp"
        kill $mock_pid 2>/dev/null || true; trap - EXIT
    else
        warn "tools/mock_anthropic_api.py missing — skipping mock canary"
    fi
}

# -------- dispatch ---------------------------------------------------------

PHASES=(fetch_base gen_patches syntax_check apply_patches smoke)

run_phase() {
    case "$1" in
        fetch_base)    phase_fetch_base ;;
        gen_patches)   phase_gen_patches ;;
        syntax_check)  phase_syntax_check ;;
        apply_patches) phase_apply_patches ;;
        smoke)         phase_smoke ;;
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
    build|"")
        for p in "${PHASES[@]}"; do run_phase "$p"; done
        ok "build complete"
        ;;
    *)
        cat <<EOF
build.sh — end-to-end build for the cc-symbolic patched binary.

Usage:
  ./build.sh                build (idempotent — skips what is already current)
  ./build.sh rebuild        wipe + rebuild all phases
  ./build.sh preflight      source-only: gen_patch + syntax check (no binary)
  ./build.sh phase NAME     one phase: fetch_base|gen_patches|syntax_check
                            |apply_patches|smoke
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