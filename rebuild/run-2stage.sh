#!/usr/bin/env bash
# run-2stage.sh -- launch the patched claude-code (out/claude.patched) with
# the cc-ollama backend env + 2-stage-thinking config, so the in-source
# 2-stage mod can be smoke-tested against Ollama Cloud (glm-5.2 / minimax-m3).
#
# Mirrors the `cc ollama` env block from
# claude-model-einstellen/claude-code-launcher-minimax-glm/bin/cc-launcher
# (base url, auth, tier models) but:
#   * runs OUR patched binary, not the stock ~/.local/bin/claude (2.1.206);
#   * EXPORTS OPUS_MODEL/SONNET_MODEL/HAIKU_MODEL (the launcher sets
#     ANTHROPIC_DEFAULT_*_MODEL; the 2-stage helper resolves stage models from
#     process.env.OPUS_MODEL / SONNET_MODEL / HAIKU_MODEL, so we export them);
#   * sets TWO_STAGE_* (stage1=opus tier -> glm-5.2:cloud decider,
#     stage2=sonnet tier -> minimax-m3:cloud judge) + TWO_STAGE_DEBUG=1.
#
# Usage:
#   ./run-2stage.sh [claude-args...]            # interactive
#   ./run-2stage.sh -p "ultrathink: <frage>"    # one-shot print mode
#   TWO_STAGE_ENABLED=0 ./run-2stage.sh -p "..." # passthrough (stock behavior)
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_BIN="${CLAUDE_BIN:-$DIR/out/claude.patched}"
[[ -x "$CLAUDE_BIN" ]] || { echo "[!] patched binary not found: $CLAUDE_BIN (run ./build.sh build first)" >&2; exit 1; }

# --- cc-ollama backend env (tier models = glm-5.2 / minimax-m3) ---
export ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-http://127.0.0.1:11434}"
export ANTHROPIC_AUTH_TOKEN="ollama"
export ANTHROPIC_API_KEY=""
export CLAUDE_CODE_ATTRIBUTION_HEADER=0
export OPUS_MODEL="${OPUS_MODEL:-glm-5.2:cloud}"
export SONNET_MODEL="${SONNET_MODEL:-minimax-m3:cloud}"
export HAIKU_MODEL="${HAIKU_MODEL:-minimax-m3:cloud}"
export SUBAGENT_MODEL="${SUBAGENT_MODEL:-minimax-m3:cloud}"
export ANTHROPIC_DEFAULT_OPUS_MODEL="$OPUS_MODEL"
export ANTHROPIC_DEFAULT_OPUS_MODEL_NAME="$OPUS_MODEL"
export ANTHROPIC_DEFAULT_SONNET_MODEL="$SONNET_MODEL"
export ANTHROPIC_DEFAULT_SONNET_MODEL_NAME="$SONNET_MODEL"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="$HAIKU_MODEL"
export ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME="$HAIKU_MODEL"
export CLAUDE_CODE_SUBAGENT_MODEL="$SUBAGENT_MODEL"
export CLAUDE_CODE_SUBAGENT_MODEL_NAME="$SUBAGENT_MODEL"

# --- 2-stage config (stage1=opus tier decider, stage2=sonnet tier judge) ---
export TWO_STAGE_ENABLED="${TWO_STAGE_ENABLED:-1}"
export TWO_STAGE_STAGE1_TIER="${TWO_STAGE_STAGE1_TIER:-opus}"     # -> OPUS_MODEL = glm-5.2:cloud
export TWO_STAGE_STAGE2_TIER="${TWO_STAGE_STAGE2_TIER:-sonnet}"  # -> SONNET_MODEL = minimax-m3:cloud
export TWO_STAGE_DECIDER_BUDGET="${TWO_STAGE_DECIDER_BUDGET:-2000}"
export TWO_STAGE_JUDGE_BUDGET="${TWO_STAGE_JUDGE_BUDGET:-16000}"
export TWO_STAGE_DEBUG="${TWO_STAGE_DEBUG:-1}"

echo "[run-2stage] bin=$CLAUDE_BIN" >&2
echo "[run-2stage] base=$ANTHROPIC_BASE_URL opus=$OPUS_MODEL sonnet=$SONNET_MODEL" >&2
echo "[run-2stage] stage1=$TWO_STAGE_STAGE1_TIER($TWO_STAGE_DECIDER_BUDGET) stage2=$TWO_STAGE_STAGE2_TIER($TWO_STAGE_JUDGE_BUDGET) enabled=$TWO_STAGE_ENABLED" >&2

exec "$CLAUDE_BIN" "$@"