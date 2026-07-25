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

## 7. Streaming + visible-vs-hidden (agent 2)

- **Stream consumer**: `aad` async generator :406849, loop `for await (let zo of SSy(qe, Rn))` :407849. `qe` = SDK `MessageStream` (class `v7` :17610). `SSy` :407748 is a watchdog wrapper (`yield* e` + ping). Discriminator raw-vs-wrapper: `!("controller" in Ti.value)` :407773.
- **Delta dispatch** :408010-408058 into `Qe[zo.index]`: `text_delta`→`lr.text`, `thinking_delta`→`lr.thinking`, `input_json_delta`→`lr.input` (string), `signature_delta`→`lr.signature`.
- **Per-block yield** :408097: on `content_block_stop`, accumulated block → `{type:"assistant", message:{...content:[lr]}}` → `yield nn`. This per-block `assistant` event is the unit of UI delivery.
- **VISIBLE = `text` blocks only.** `Out(e)` :489768 dispatches: `assistant`→`displayTransform.entryLanded(e)` (visible Ink text) + `onStreamingText` (live text deltas via `Qx.apply`). `ols` :489855: `text_delta`→`onStreamingText` (LIVE to terminal); `thinking_delta`→only `onApiMetrics("thinking_progress")` (token pill, NOT visible text).
- **Thinking IS shown** briefly: post-stream snapshot into React state `ef` :749641, auto-cleared after 30s. To make thinking FULLY invisible: `thinking:{type:"enabled",display:"omitted"}` (API returns `redacted_thinking` only) OR strip thinking text from the yielded `assistant.message.content` before `Out`.
- **Stream identity**: raw SDK `v7` (has `.controller`, `[Symbol.asyncIterator]`). Wrapping is RISKY (the prior Spread-crash + has-trap came from this). **→ Avoid wrapping; prevent leakage at the source (clean system, no notation preamble) instead of filtering the stream.** This is the basis of the translator reframe.
- **Tool_use assembly**: `input_json_delta` fragments → string `lr.input` → parsed at consumer. Final tool_use list = filter `message.content` for `type==="tool_use"`.

## 8. Tool-use loop + history (agent 3)

- **Turn termination**: `itd` :383499, single `while(!0)` :383541. `Lr = Er?.message.stop_reason ?? Dt` :385068. `tool_use`→execute+continue; `end_turn`→done (with a "thinking-only nudge" :385069-385094 if no text — injects meta "produce a user-visible response" + continues). Terminal `return {reason:"completed"}` :385161.
- **Tool extraction** :384554-384561: `z.message.content.filter(type==="tool_use")` → `Fe` → `Ft.addTool(Ke,z)` (`P6e` executor :378550, instantiated :383711). `P6e.executeTool` :378744 → `F9r` :354270 → `xny` :354521 (switch: hooks, validation, **permission pipeline**, `e.handler(...)`).
- **Tool_result** = `Ur({content:[{type:"tool_result",content,is_error,tool_use_id}]})` user message (factory `Ur` :488081). Collected into `at` via `Ft.getRemainingResults()` :385167-385184.
- **Permission gating**: `dYr` :331100 (decider) → `i6e` :372692 (read-only bypass) → `Lat` :373150 (rules) → `canUseTool` callback; `MVt` :60591 maps mode→behavior; `--dangerously-skip-permissions` → `"bypassPermissions"` → unconditional allow.
- **History** :385433-385448: `g.messages = [...Re, ...ot, ...at]` — assistant turn (`ot`, **thinking + tool_use blocks PRESERVED**, only stripped on server rejection :407750) + tool_result user msgs (`at`). Invariant: assistant-msg → tool_result-user-msg 1:1 on `tool_use_id`.
- **→ Docking confirmed**: the translator's streamed response IS the assistant turn; its `text` blocks → visible, its `tool_use` blocks → stock `P6e` executor (native). So a clean-system translator that emits normal text + tool_use works with zero tool-loop changes.