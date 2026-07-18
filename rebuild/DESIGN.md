# 2-Stage Epistemic Thinking for claude-code — Design

Goal: **Opus = decider (stage 1, cheap/short), Sonnet = judge (stage 2, heavy
thinking, cheaper per token)** — applied through claude-code's *normal*
thinking flow, **not** as a hermes plugin. Saves Opus tokens: the expensive long
thinking moves to Sonnet.

## 1. Reference patch (hermes)

`hermes-agent` commit `1a7bb2983` — *"scimind: add SciMind 5.0 epistemic
mandates + worker-decoder plugin + resume-all-sources"*:

- `agent/scimind_mandates.py` — SciMind 5.0 mandates (Incomplete Suggestion,
  Empirical Verification, Falsificationism, Pipeline Integrity, Zero-Trust,
  Atomic & Secured, Anti-Embedding, Credential Protection, Source Control).
  Ported from Gemini-CLI `snippets.ts`, model-agnostic, injected into the
  system prompt.
- `plugins/worker-decoder/` — **2-stage epistemic plugin**: worker LLM
  produces output, a decoder LLM (default `glm-5.2:cloud`) reviews it with
  verdicts `OK | CORRECT | REJECT`, wired via the `transform_llm_output` hook.

The hermes design is a **post-hoc output transform plugin**. We do **not**
copy that shape: the user wants the 2-stage to ride claude-code's own thinking,
not a bolt-on hook. We reuse only the *epistemic mandates* (the
`SCIMIND_5_0_PREAMBLE` constant — see `scimind_mandates.py`, copied verbatim).

## 2. How claude-code does thinking (evidence from the extracted bundle)

Reverse-engineered from the official binary's embedded `cli.js` (see
`extracted/`, obtained via `unbun` from the `.bun` ELF section; method in
`patch.py` / `build.sh`).

- **Single main model.** `getMainLoopModel` (266 refs) is the main-loop model;
  `mainLoopModel` is the user-facing setting. Extended thinking runs on that
  same model — there is **no separate "thinking model"**.
- **Thinking request shape.** Requests carry `thinking:{type:"enabled",
  budget_tokens:N}` (or `{type:"disabled"}`). The beta header
  `interleaved-thinking-2025-05-14` enables interleaved thinking+tool-use.
  Responses contain `thinking` content blocks carrying a `signature`.
- **Tier system.** `ANTHROPIC_DEFAULT_OPUS/SONNET/HAIKU_MODEL` + env overrides
  `OPUS_MODEL` / `SONNET_MODEL` / `HAIKU_MODEL` / `SUBAGENT_MODEL` (read 51 /
  40 / 37 / 8 times). `ANTHROPIC_BASE_URL` (49 refs) redirects the API
  endpoint — **the hook point for a transparent proxy.** The user's
  `cc-launcher-or` already sets the tier env vars per invocation.

## 3. Design: the 2-stage shim (`two_stage_shim.py`)

A transparent proxy on `ANTHROPIC_BASE_URL`. claude-code keeps issuing its
ordinary `POST /v1/messages` with thinking enabled; the shim splits that call:

```
claude-code  --POST /v1/messages (thinking on, model=Opus)-->  two_stage_shim
                                                                |
                              Stage 1 DECIDER (Opus, small budget) ---- upstream
                                draft = text + tool_use + short thinking
                                                                |
                              Stage 2 JUDGE (Sonnet, large budget + SciMind)
                                system = SciMind mandates + judge instruction
                                messages = original + [draft as final user turn]
                                tools = original tools   (Sonnet emits final calls)
                                                                |
                              reshape (drop thinking blocks, keep model name)
                                                                v
claude-code  <-- normal Anthropic /v1/messages response (text + tool_use) --
```

Why this shape:

- **Tool calls survive.** Sonnet emits the authoritative `tool_use` blocks,
  so claude-code's agent loop keeps executing tools. (If the judge only
  produced text, the agent loop would break.)
- **Thinking blocks are dropped** from the reply. Anthropic verifies
  `thinking` block `signature`s; synthesised blocks would fail that check.
  The thinking happened *inside* the shim; claude-code does not need the
  blocks back to render the answer.
- **Model name is preserved.** The returned `model` field is the name
  claude-code originally requested, so its tier logic is unaffected.
- **Composition.** The shim speaks Anthropic `/v1/messages` on both sides,
  so it composes with `anthropic_openai_shim.py` when the real backend is
  OpenAI-schema (OpenRouter / NIM). Point claude-code at the shim via
  `ANTHROPIC_BASE_URL=http://127.0.0.1:9877`.

### Triggering

2-stage applies when: `TWO_STAGE_ENABLED` and both `TWO_STAGE_OPUS_MODEL` /
`TWO_STAGE_SONNET_MODEL` are set, the request has thinking enabled, and the
incoming model is in `TWO_STAGE_TRIGGER_MODELS` (empty = any thinking request).
Non-triggered requests pass through unchanged.

### Config (env)

| var | default | meaning |
|---|---|---|
| `TWO_STAGE_PORT` | 9877 | listen port |
| `TWO_STAGE_UPSTREAM_BASE_URL` | https://api.anthropic.com | Anthropic-schema upstream |
| `TWO_STAGE_OPUS_MODEL` | — | decider model name |
| `TWO_STAGE_SONNET_MODEL` | — | judge model name |
| `TWO_STAGE_API_KEY` | (inbound) | upstream key |
| `TWO_STAGE_DECIDER_BUDGET` | 2000 | small thinking budget, stage 1 |
| `TWO_STAGE_JUDGE_BUDGET` | 16000 | large thinking budget, stage 2 |
| `TWO_STAGE_TRIGGER_MODELS` | (all) | comma-sep models that trigger 2-stage |

### Run

```bash
python3 rebuild/two_stage_shim.py
ANTHROPIC_BASE_URL=http://127.0.0.1:9877 \
TWO_STAGE_OPUS_MODEL=<opus-id> TWO_STAGE_SONNET_MODEL=<sonnet-id> \
OPUS_MODEL=<opus-id> SONNET_MODEL=<sonnet-id> claude
```

## 4. Status & roadmap (v0.1)

Done:
- 2-stage for thinking requests; non-streaming upstream calls; SSE synthesis
  back to claude-code for streaming inbound requests; streaming-free passthrough
  for non-triggered requests; per-request token logging to
  `/tmp/two-stage-shim.log`; SciMind mandates as the judge system preamble.

Limitations / next:
- **Tool results across turns** are carried via the original `messages`, so
  multi-turn tool-use works, but the judge's per-turn tool *execution* (one
  round) is not yet simulated inside the shim — claude-code executes whatever
  tool calls Sonnet returns and calls back, which re-enters the shim (good).
- **True streaming** end-to-end (pipe SSE deltas rather than synthesise from a
  completed non-stream call) for lower TTFT.
- **Tool Choice / parallel tool_use** edge cases need real traffic testing.
- **Upstream protocol** is Anthropic-schema only; for OpenAI-schema backends,
  chain behind `anthropic_openai_shim.py` (one per stage), or add an OpenAI
  translator here.
- **Cost telemetry**: aggregate Opus-vs-Sonnet token spend from the log to
  prove the Opus-token reduction.

## 5. Relationship to `patch.py` / the binary-patch tooling

`patch.py` + `build.sh` are a *separate* mechanism: surgical same-length byte
patches of the official binary (for fixing bugs in the bundle). The 2-stage
thinking feature is deliberately implemented as an external proxy instead of
a bundle patch, because it requires new orchestration code (two HTTP round
trips + request rewriting) that cannot be expressed as a same-length
find/replace in minified JS. Both live under `rebuild/` for convenience.