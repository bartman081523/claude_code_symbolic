#!/usr/bin/env bash
# Build a patched claude-code binary from the official prebuilt platform package.
#
#   ./build.sh                # fetch base (if missing) + apply patches.json -> out/claude.patched
#   ./build.sh inspect         # extract cli.js (no bytecode) to extracted/ for bug hunting
#   ./build.sh clean           # remove outputs (keeps base + extracted)
#
# Env:
#   CLAUDE_VERSION=2.1.214    # version to fetch
#   PLATFORM=linux-x64        # platform package suffix
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

VERSION="${CLAUDE_VERSION:-2.1.214}"
PLATFORM="${PLATFORM:-linux-x64}"
BASE="claude.orig"
OUT="out/claude.patched"

fetch_base() {
  if [ -f "$BASE" ]; then echo "[*] base present: $BASE"; return; fi
  echo "[*] fetching @anthropic-ai/claude-code-$PLATFORM@$VERSION"
  npm pack "@anthropic-ai/claude-code-$PLATFORM@$VERSION" >/dev/null
  local tgz
  tgz=$(ls anthropic-ai-claude-code-"$PLATFORM"-*.tgz | tail -1)
  mkdir -p _pkg && tar xzf "$tgz" -C _pkg --strip-components=1
  cp _pkg/claude "$BASE"; chmod +x "$BASE"
  rm -rf _pkg "$tgz"
  echo "[+] base binary: $BASE ($(stat -c%s "$BASE") bytes)"
}

case "${1:-build}" in
  inspect)
    fetch_base
    echo "[*] extracting modules (bytecode omitted to save space)"
    npx -y unbunjs extract "$BASE" ./extracted >/dev/null
    rm -f extracted/src/entrypoints/cli.js.bytecode
    echo "[+] cli.js at: extracted/src/entrypoints/cli.js ($(stat -c%s extracted/src/entrypoints/cli.js) bytes)"
    echo "    search it: grep -ao 'pattern' extracted/src/entrypoints/cli.js"
    ;;
  clean)
    rm -rf out _pkg anthropic-ai-claude-code-*.tgz
    echo "[+] cleaned outputs"
    ;;
  build|"")
    fetch_base
    if [ ! -f patches.json ]; then echo "[!] patches.json missing"; exit 1; fi
    echo "[*] applying patches.json"
    python3 patch.py "$BASE" "$OUT" patches.json
    echo "[*] verifying --version"
    "$OUT" --version
    echo "[+] built: $OUT"
    ;;
  *)
    echo "usage: $0 [build|inspect|clean]"; exit 1;;
esac