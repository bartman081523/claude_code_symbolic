# Stock claude-code mechanics — map for the 3-stage symbolic docking

Reverse-engineered from `extracted/cli.pretty.js` (2.1.214, prettified, 35 MB /
797,875 lines). DEV-ONLY — must NOT leak into `gen_patch.py` / `patches.json` /
the binary. Purpose: find exact docking points to run the DECIDER/JUDGE/
TRANSLATOR pipeline with the engine room hidden (like normal thinking) and
only a clean final answer + tool calls reaching the user.

## 1. Main agentic loop (3 layers)

- **L1 `k2({...})` :386666** — per-query orchestrator. Drives `for await (let L of Mne({...}))` (:386717).
- **L2 `async function* Mne(e)` :383432** → `async function* itd(e,t)` :383499 — the real per-turn driver.
  - Outer `while(!0)` :383541 (once per query); **inner `while(gn)` :383904 = the per-turn tool_use→tool_result loop**.
  - Each turn: `for await (let Rn of iZu(f.callModel({...})))` :383980 fires the API.
  - State in `g` (:383512): `g.messages`, `g.toolUseContext`, `g.turnCount`, ...
- **L3 `fLr` :19668** — SDK `BetaToolRunner` loop (NOT used by CC main loop; CC rolls its own `itd`).

## 2. Request body construction — `aad(...)` :406849, body literal `Rs` :407208

```js
let Rs = {
  model: kte(i.model),                 // policy-resolved
  messages: Gl,                        // wSy(fad(Q,bs),...) :407206 — normalized list
  system: ce,                          // CSy(...) :406999 — ARRAY of {type:"text",text,cache_control?}
  tools: gSc(ee,i.model),              // schema array
  tool_choice: Ir,                     // {type:"auto"}, demoted if thinking
  max_tokens: Ke,                      // min(override, Oao(u))
  thinking: je,                        // §5
  metadata: yit(), betas, output_config{effort}, speed:"fast"?, ...
};
```

## 3. Where the request is sent

- Main loop uses **`Yo.beta.messages.create({...bs, stream:!0},{...}).withResponse()`** at **:407632** (`Yo` = per-model client `Ile(...)`). Returns `{data: MessageStream, response, request_id}`.
- **CC does NOT use `client.beta.messages.stream(...)`** — uses `.create({stream:true}).withResponse()` and iterates the stream. (SDK's BetaToolRunner uses `.stream()` at :19678 — different path.)
- SDK base: **`class $Je` :19799**, `create(e,t)` :19804 → `this._client.post("/v1/messages?beta=true",{...})` :19819. **This is the current patch's hook point (HOOKA_BASE / HOOKA_BETA).** Confirmed correct layer.
- `stream(e,t)` :19841 → `uLr.createMessage` (streaming class :19136); `_createMessage` :19182 → `e.create({...t,stream:!0},{signal}).withResponse()` then `for await`.

## 4. System prompt — ARRAY of text blocks

- Built by **`CSy(t,ie,{...})` :409066** from a string list `t` (:406992 = `[CGn(...), CZn(...), ...outerSystemPrompt, ...advisor?]`).
- `ivs` :405271 places cache breakpoints → final `system: [{type:"text",text},{type:"text",text,cache_control:{type:"ephemeral",ttl}}]`.
- **→ Docking: inject the converged construct as an extra `{type:"text",text:"<internal reasoning …>"}` system block. Hidden from user (system never shown), no role:"user" stage message, no stage names.**
- Per-turn `<system-reminder>` injection: **`ltd(e,t)` :405378** prepends a `user` message `{content:"<system-reminder>…</system-reminder>", isMeta:!0}`. Called from `itd` :383981. **→ Docking: a visible "cc-symbolic active" marker could ride this channel.**
- Mid-conversation effort: `Msd` :405987 injects `{type:"api_system",outputConfig:{effort}}` into messages; `wSy` :409046 promotes it.

## 5. Thinking config — one decision site

`Bn` closure :407120, block :407138-407160:
- `dt = r.type!=="disabled" && !CLAUDE_CODE_DISABLE_THINKING`
- if `dt && aDi(u)` (model supports thinking):
  - adaptive if `Y9t(u)` (claude-4.6+) → `{type:"adaptive",display?}`
  - else `{budget_tokens:clamp(budgetTokens, Z1c(u), 1024, Ke-1), type:"enabled",display?}`
- else if `r.type==="disabled" && firstParty && aDi(u)` → `{type:"disabled"}`
- else `void 0`
- `nSy` :406311 sanitizes `{type:"disabled"}` (only `type` key).
- User triggers: `MAX_THINKING_TOKENS` env, `/effort`, `ultrathink`/`ultracode` keyword, `set_max_thinking_tokens` control (:346230). `X9r(e)` :386897 = `getThinkingConfig` (per-actor overlays).

## 6. Docking-point summary (for the hidden-engine-room redesign)

| Point | Location | Use |
|---|---|---|
| **Current hook** | `$Je.create`→`post` :19819 (HOOKA_BASE/BETA) | intercept main turn, run decider+judge, return translator stream ✓ (keep) |
| Body literal | `Rs` :407208 | mutate system/messages/thinking/model per stage |
| **System injection** | `CSy` :409066 / append to `Rs.system` | **inject construct as hidden system text block** (key fix) |
| system-reminder channel | `ltd` :405378 | optional visible mode-marker |
| Streaming call | :407632 | the real HTTP exit |
| Per-turn loop | inner `while(gn)` :383904 | tool_use→tool_result iteration |

## 7. Streaming / visible-vs-hidden / tool-loop — (pending agents 2 & 3)

To be filled: SDK Stream (v7/uLr) consumer shape, text/thinking/tool_use delta
dispatch, what reaches the visible text channel vs thinking (hidden?),
whether a stream wrapper is safe (the prior Spread-crash risk), tool_use
assembly + tool_result feedback, history accumulation (is thinking preserved?).