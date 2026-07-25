#!/usr/bin/env node
/* test_escalation.js — Logic harness for the v3 3-stage symbolic pipeline
 * (Decider<->Judge escalation + Translator).
 *
 * Loads the HELPER string from gen_patch.py (via subprocess + JSON.dumps),
 * exposes process/require/AbortController in a vm sandbox, and drives
 * __symRun / __symDecider / __symJudge / __symFormatTrace with a mocked
 * client whose judge returns an ESCALATE marker on the first call and a
 * converged <construct> on the second.
 *
 * Replaces the v2-era test_escalation.js (which used __tsTwoStage /
 * __tsInspect / __tsSymbolicRun from a HELPER build that no longer ships).
 *
 * Run:  node test_escalation.js
 * Exit: 0 = all pass, 1 = some fail, 2 = harness error.
 */
const fs = require('fs');
const vm = require('vm');
const { execSync } = require('child_process');

// --- pull the HELPER JS out of gen_patch.py ---
const helperJs = execSync(
  "python3 -c 'import gen_patch,json; print(json.dumps(gen_patch.HELPER))'",
  { cwd: __dirname, encoding: 'utf8' }
).trim();
const HELPER = JSON.parse(helperJs);  // the JS source string

// Clean env: helper off by default; tests flip symbolic_thinking_* as needed.
delete process.env.symbolic_thinking_debug;
delete process.env.symbolic_thinking_trace;
process.env.symbolic_thinking = '1';
process.env.symbolic_thinking_mode = 'disabled';
process.env.symbolic_thinking_max_escalate = '2';
process.env.symbolic_thinking_decider_tier = 'opus';
process.env.symbolic_thinking_judge_tier = 'sonnet';
process.env.symbolic_thinking_output_tier = 'sonnet';
process.env.OPUS_MODEL = 'opus-mock';
process.env.SONNET_MODEL = 'sonnet-mock';
process.env.HAIKU_MODEL = 'haiku-mock';

const sandbox = { process, require, console, Buffer, AbortController, Date, Math, Symbol, JSON };
sandbox.global = sandbox;
vm.createContext(sandbox);
vm.runInContext(HELPER + '\n', sandbox);
const H = sandbox;  // holds the helper functions

let pass = 0, fail = 0;
function ok(name, cond, extra) {
  if (cond) { pass++; console.log('  PASS ' + name); }
  else      { fail++; console.log('  FAIL ' + name + (extra ? '  // ' + extra : '')); }
}

// --- SSE event builders (Anthropic /v1/messages shape) ---
const ev = (type, extra) => Object.assign({ type }, extra);
const msgStart = () => ev('message_start', { message: { id: 'msg_x', type: 'message', role: 'assistant', model: 'sonnet-mock', content: [], stop_reason: null, stop_sequence: null, usage: { input_tokens: 0, output_tokens: 0 } } });
const msgDelta = (o) => ev('message_delta', { delta: { stop_reason: 'end_turn' }, usage: { output_tokens: o } });
const msgStop = () => ev('message_stop', {});
const blkStart = (index, cbtype, extra) => ev('content_block_start', { index, content_block: Object.assign({ type: cbtype }, extra || {}) });
const blkStop = (index) => ev('content_block_stop', { index });
const thinkingDelta = (i, t) => ev('content_block_delta', { index: i, delta: { type: 'thinking_delta', thinking: t } });
const textDelta = (i, t) => ev('content_block_delta', { index: i, delta: { type: 'text_delta', text: t } });

function asyncIter(events) {
  return {
    [Symbol.asyncIterator]() {
      let k = 0;
      return {
        next: async () => (k < events.length ? { value: events[k++], done: false } : { value: undefined, done: true }),
        return: async () => ({ value: undefined, done: true }),
      };
    },
  };
}

(async () => {
try {

// =========================================================================
// TEST 1: __symFlagOn opt-in semantics
// =========================================================================
console.log('TEST 1: __symFlagOn opt-in semantics');
delete process.env.symbolic_thinking;
ok('unset -> off',  H.__symFlagOn('symbolic_thinking') === false);
process.env.symbolic_thinking = '0';    ok('0 -> off',    H.__symFlagOn('symbolic_thinking') === false);
process.env.symbolic_thinking = 'no';   ok('no -> off',   H.__symFlagOn('symbolic_thinking') === false);
process.env.symbolic_thinking = '1';    ok('1 -> on',     H.__symFlagOn('symbolic_thinking') === true);
process.env.symbolic_thinking = 'true'; ok('true -> on',  H.__symFlagOn('symbolic_thinking') === true);
process.env.symbolic_thinking = 'ON';   ok('ON -> on (case-insensitive)', H.__symFlagOn('symbolic_thinking') === true);
process.env.symbolic_thinking = '  yes  '; ok('whitespace-tolerant', H.__symFlagOn('symbolic_thinking') === true);
process.env.symbolic_thinking = '1';

// =========================================================================
// TEST 2: __symTrigger (env gate + body shape)
// =========================================================================
console.log('\nTEST 2: __symTrigger — opt-in truth table (thinking enabled body)');
function setFlags(on, deep) {
  if (on)  process.env.symbolic_thinking = '1'; else delete process.env.symbolic_thinking;
  if (deep) process.env.symbolic_thinking_deep = '1'; else delete process.env.symbolic_thinking_deep;
}
const thinkBody = { model: 'opus-mock', thinking: { type: 'enabled', budget_tokens: 2000 }, messages: [{ role: 'user', content: 'q' }] };
setFlags(false, false); ok('off+no_deep -> off',    H.__symTrigger(thinkBody) === false);
setFlags(true,  false); ok('on+no_deep -> triggers (mode=disabled default)', H.__symTrigger(thinkBody) === true);
setFlags(false, true);  ok('off+deep -> off (deep alone does not enable)', H.__symTrigger(thinkBody) === false);
setFlags(true,  true);  ok('on+deep -> on',         H.__symTrigger(thinkBody) === true);
// No-thinking body never triggers regardless of flags
ok('no thinking body -> off', H.__symTrigger({ model: 'x' }) === false);
ok('thinking.disabled body -> off', H.__symTrigger({ model: 'x', thinking: { type: 'disabled' } }) === false);
ok('thinking.adaptive body -> triggers', H.__symTrigger({ model: 'x', thinking: { type: 'adaptive' } }) === true);
// symbolic_thinking_trigger_models filter
process.env.symbolic_thinking_trigger_models = 'glm-5.2:cloud,opus-mock';
ok('trigger_models filter excludes other models', H.__symTrigger(thinkBody) === true);   // opus-mock is in the list
ok('trigger_models filter blocks unlisted',       H.__symTrigger({ model: 'm-x', thinking: { type: 'enabled' } }) === false);
delete process.env.symbolic_thinking_trigger_models;
setFlags(false, false);
process.env.symbolic_thinking = '1';

// =========================================================================
// TEST 3: __symMaxEsc / __symMOpen / __symMClose / __symRe / __symEscReq
// =========================================================================
console.log('\nTEST 3: escalate-marker helpers');
process.env.symbolic_thinking_max_escalate = '0';
ok('__symMaxEsc default 0 (after explicit 0)', H.__symMaxEsc() === 0);
process.env.symbolic_thinking_max_escalate = '3';
ok('__symMaxEsc reads 3', H.__symMaxEsc() === 3);
process.env.symbolic_thinking_max_escalate = '-1';
ok('__symMaxEsc clamps negative to 0', H.__symMaxEsc() === 0);
process.env.symbolic_thinking_max_escalate = 'not-a-number';
ok('__symMaxEsc clamps NaN to 0', H.__symMaxEsc() === 0);
process.env.symbolic_thinking_max_escalate = '2';
ok('__symMOpen default [ESCALATE]', H.__symMOpen() === '[ESCALATE]');
ok('__symMClose default [/ESCALATE]', H.__symMClose() === '[/ESCALATE]');
ok('__symRe escapes metachars', H.__symRe('[ESC]') === '\\[ESC\\]');
ok('__symRe escapes parens',    H.__symRe('(a)') === '\\(a\\)');
ok('__symEscReq extracts request', H.__symEscReq('[ESCALATE]give me X[/ESCALATE]') === 'give me X');
ok('__symEscReq no marker -> null', H.__symEscReq('just an answer') === null);
ok('__symEscReq multiline body', H.__symEscReq('[ESCALATE]line1\nline2[/ESCALATE]') === 'line1\nline2');
ok('__symEscReq extra after close is ignored', H.__symEscReq('[ESCALATE]X[/ESCALATE] trailing') === 'X');
process.env.symbolic_thinking_escalate_open = '[ESC]';
process.env.symbolic_thinking_escalate_close = '[/ESC]';
ok('custom markers respected by __symEscReq', H.__symEscReq('[ESC]need Y[/ESC]') === 'need Y');
ok('custom markers respected by __symMOpen', H.__symMOpen() === '[ESC]');
ok('custom markers respected by __symMClose', H.__symMClose() === '[/ESC]');
delete process.env.symbolic_thinking_escalate_open;
delete process.env.symbolic_thinking_escalate_close;

// =========================================================================
// TEST 4: body builders (decider / judge / output)
// =========================================================================
console.log('\nTEST 4: stage body builders (system + messages + model + tools + thinking)');
const sbody = {
  model: 'opus-mock',
  thinking: { type: 'enabled', budget_tokens: 2000 },
  messages: [{ role: 'user', content: 'solve' }],
  tools: [{ name: 'Read' }],
  system: 'SYS',
};

// Decider body
const db = H.__symDeciderBody(sbody, null);
ok('decider body model is opus (tier=opus)', db.model === 'opus-mock');
ok('decider body non-stream', db.stream === false);
ok('decider body stripped tools (per spec: tools stripped from decider)', !db.tools);
ok('decider body has thinking field', !!db.thinking);
ok('decider system has ℧ notation', /℧/.test(db.system));
ok('decider system has DECIDER framing', /DECIDER/.test(db.system));
ok('decider system preserves original', /SYS/.test(db.system));
ok('decider instruction in last user msg', /DECIDER/.test(db.messages[db.messages.length - 1].content));
ok('decider instruction no tools mention (or allowed)', !/Use tools|Read tool/.test(db.messages[db.messages.length - 1].content));

// Decider body with feedback (escalation round 2)
const db2 = H.__symDeciderBody(sbody, 'the constraint values are missing');
ok('decider feedback round 2 contains feedback', /constraint values are missing/.test(db2.messages[db2.messages.length - 1].content));
ok('decider feedback round 2 still has DECIDER role', /DECIDER/.test(db2.messages[db2.messages.length - 1].content));

// Judge body
const jb = H.__symJudgeBody(sbody, '<construct>draft</construct>', 0);
ok('judge body model is sonnet (tier=sonnet)', jb.model === 'sonnet-mock');
ok('judge body non-stream', jb.stream === false);
ok('judge body stripped tools', !jb.tools);
ok('judge system has ℧ notation', /℧/.test(jb.system));
ok('judge system has JUDGE framing', /JUDGE/.test(jb.system));
ok('judge instruction at depth 0 carries ESCALATE directive', /\[ESCALATE\]/.test(jb.messages[jb.messages.length - 1].content));
ok('judge instruction embeds the decider construct', jb.messages[jb.messages.length - 1].content.includes('<construct>draft</construct>'));

// Judge cap (depth >= MAX)
const jbCap = H.__symJudgeBody(sbody, 'draft', 2);  // MAX=2
ok('judge cap (depth=MAX) instruction forbids ESCALATE directive', !/emit EXACTLY \[ESCALATE\]/.test(jbCap.messages[jbCap.messages.length - 1].content));
ok('judge cap instruction says "no further escalation"', /no further escalation is allowed/.test(jbCap.messages[jbCap.messages.length - 1].content));

// Judge aware (depth < MAX) must keep ESCALATE option
const jbAware = H.__symJudgeBody(sbody, 'draft', 1);
ok('judge aware (depth<MAX) keeps ESCALATE directive', /\[ESCALATE\]/.test(jbAware.messages[jbAware.messages.length - 1].content));

// Output body
const ob = H.__symOutputBody(sbody, '<construct>final</construct>');
ok('output body model is sonnet', ob.model === 'sonnet-mock');
ok('output body is stream', ob.stream === true);
ok('output body PRESERVES tools (translator needs them)', ob.tools && ob.tools.length === 1);
ok('output system carries the construct', /<construct>final<\/construct>/.test(JSON.stringify(ob.system)) || (Array.isArray(ob.system) && ob.system.some(x => /<construct>final<\/construct>/.test(x.text))));
ok('output system has hidden reasoning (do-not-surface note)', /Do NOT mention, quote, summarize/.test(JSON.stringify(ob.system)));
ok('output system has the construct as internal reasoning', /<construct>final<\/construct>/.test(JSON.stringify(ob.system)));

// system-thinking guards
process.env.symbolic_thinking_mode = 'disabled';
const dbDis = H.__symDeciderBody(sbody, null);
ok('mode=disabled emits thinking:{type:disabled}', dbDis.thinking && dbDis.thinking.type === 'disabled');
process.env.symbolic_thinking_mode = 'none';
const dbNone = H.__symDeciderBody(sbody, null);
ok('mode=none omits thinking field', dbNone.thinking === undefined);
process.env.symbolic_thinking_mode = 'enabled';
const dbEn = H.__symDeciderBody(sbody, null);
ok('mode=enabled emits thinking:{type:enabled,budget_tokens:N}', dbEn.thinking && dbEn.thinking.type === 'enabled' && dbEn.thinking.budget_tokens > 0);
process.env.symbolic_thinking_deep = '1';
const dbDeep = H.__symDeciderBody(sbody, null);
ok('symbolic_thinking_deep=1 forces thinking:enabled regardless of mode', dbDeep.thinking && dbDeep.thinking.type === 'enabled');
delete process.env.symbolic_thinking_deep;
process.env.symbolic_thinking_mode = 'disabled';

// =========================================================================
// TEST 5: __symTrimMsgs (token-saving: only first + last user msgs)
// =========================================================================
console.log('\nTEST 5: __symTrimMsgs');
const msgs = [
  { role: 'user', content: 'first' },
  { role: 'assistant', content: 'a' },
  { role: 'user', content: 'second' },
  { role: 'assistant', content: 'b' },
  { role: 'user', content: 'last' },
];
const trimmed = H.__symTrimMsgs(msgs);
ok('trimmed length is 2 (first + last)', trimmed.length === 2);
ok('first is the first user msg', trimmed[0].content === 'first');
ok('last is the most-recent msg', trimmed[1].content === 'last');
ok('short array (≤2) returned as-is', H.__symTrimMsgs([{ role: 'user', content: 'a' }]).length === 1);
ok('empty array -> empty', H.__symTrimMsgs([]).length === 0);

// =========================================================================
// TEST 6: __symBlockInfo (block counts)
// =========================================================================
console.log('\nTEST 6: __symBlockInfo');
const bi = H.__symBlockInfo({ content: [
  { type: 'text', text: 'hi' },
  { type: 'text', text: 'world' },
  { type: 'thinking', thinking: 'longer thought' },
  { type: 'tool_use', id: 't1', name: 'Read', input: {} },
  { type: 'redacted_thinking' },
]});
ok('text count = 7 chars', bi.text === 7);
ok('thinking count = 1 block', bi.thinking === 1);
ok('thinking text count = 14 chars', bi.thinkText === 14);
ok('tool count = 1', bi.tool === 1);
ok('other count = 1 (redacted_thinking)', bi.other === 1);
ok('null msg -> zero counts', H.__symBlockInfo(null).text === 0);
ok('empty content -> zero counts', H.__symBlockInfo({ content: [] }).text === 0);

// =========================================================================
// TEST 7: __symIsCloud + __symStageMaxTokens (cloud-fallback budget cap)
// =========================================================================
console.log('\nTEST 7: __symIsCloud + __symStageMaxTokens');
delete process.env.ANTHROPIC_BASE_URL;
ok('minimax is cloud', H.__symIsCloud('minimax-m3:cloud') === true);
ok('any :cloud suffix is cloud', H.__symIsCloud('whatever:cloud') === true);
ok('plain name is not cloud (no base URL)', H.__symIsCloud('opus-4-8') === false);
process.env.ANTHROPIC_BASE_URL = 'http://127.0.0.1:11434';
ok('ollama base URL is cloud-flagged', H.__symIsCloud('whatever') === true);
delete process.env.ANTHROPIC_BASE_URL;
ok('anthropic base URL is not cloud', H.__symIsCloud('opus-4-8') === false);

process.env.ANTHROPIC_BASE_URL = 'http://127.0.0.1:11434';
const cloudBody = { model: 'minimax-m3:cloud' };
ok('cloud body caps max_tokens to budget', H.__symStageMaxTokens(cloudBody, 1500) === 1500);
delete process.env.ANTHROPIC_BASE_URL;
ok('non-cloud body returns undefined (keep original max_tokens)', H.__symStageMaxTokens({ model: 'opus-4-8' }, 1500) === undefined);

// =========================================================================
// TEST 8: __symStripModelFlag (Hook C: respawnFlags dedup)
// =========================================================================
console.log('\nTEST 8: __symStripModelFlag (respawnFlags env-tiers win)');
ok('filters out --model pairs from existing arr', JSON.stringify(
  H.__symStripModelFlag(['--model', 'deepseek-v4-pro:cloud', '--permission-mode', 'auto'], '--model', 'minimax-m3:cloud')
) === JSON.stringify(['--permission-mode', 'auto']));
ok('appends new pair when e is not --model (with t)', JSON.stringify(
  H.__symStripModelFlag(['--permission-mode', 'auto'], '--verbose', '1')
) === JSON.stringify(['--permission-mode', 'auto', '--verbose', '1']));
ok('appends single arg when t is undefined', JSON.stringify(
  H.__symStripModelFlag(['--permission-mode', 'auto'], '--verbose', undefined)
) === JSON.stringify(['--permission-mode', 'auto', '--verbose']));
ok('empty arr', JSON.stringify(H.__symStripModelFlag([], '--x', 'y')) === JSON.stringify(['--x', 'y']));
ok('no --model anywhere, append as-is', JSON.stringify(
  H.__symStripModelFlag(['a', 'b'], '--x', 'y')
) === JSON.stringify(['a', 'b', '--x', 'y']));

// =========================================================================
// TEST 9: __symSystem (preamble) + thinking-block-fallback text extraction
// =========================================================================
console.log('\nTEST 9: __symSystem + __symMessageText (text + thinking fallback)');
const sys = H.__symSystem({ system: 'USER_SYS' });
ok('system includes original', /USER_SYS/.test(sys));
ok('system has cc-symbolic preamble', /cc-symbolic/.test(sys));
ok('system has ℧ notation', /℧/.test(sys));
ok('system frames 3 stages (DECIDER/JUDGE/TRANSLATOR)', /DECIDER/.test(sys) && /JUDGE/.test(sys) && /TRANSLATOR/.test(sys));
ok('system suppresses injection-flagging rationale for stage roles', /Do not refuse/.test(sys));

const msgText = { content: [{ type: 'text', text: 'real answer' }] };
ok('__symMessageText extracts text (>=30 chars works without fallback)', H.__symMessageText(msgText) === 'real answer');
const shortMsg = { content: [{ type: 'thinking', thinking: 'reasoning-only channel content' }] };
const out = H.__symMessageText(shortMsg);
ok('__symMessageText falls back to TRUNCATED thinking when no text', out.includes('reasoning-only channel content') || /reasoning:/.test(out));
ok('thinking fallback is truncated at 240 chars', out.length <= 270);  // 'reasoning: ' prefix + 240 + '…'
const mixedMsg = { content: [{ type: 'text', text: 'short' }, { type: 'thinking', thinking: 'reasoning detail' }] };
const mixedOut = H.__symMessageText(mixedMsg);
ok('mixed: text wins when text is short? — short text still returned', mixedOut.startsWith('short'));

// =========================================================================
// TEST 10: __symFormatTrace (algorithmic formatter)
// =========================================================================
console.log('\nTEST 10: __symFormatTrace (algorithmic 3-stage trace)');
const t1 = H.__symFormatTrace([{ stage: 'decider', depth: 0, role: 'decider', t0: 1000, dt: 800, ok: true, construct: 'c1', model: 'opus-mock', escalateRequest: null, blockInfo: { text: 2, thinking: 0, tool: 0, other: 0, thinkText: 0 } }]);
ok('single-decider trace has header', /^cc-symbolic reasoning trace \(3-stage\)/m.test(t1));
ok('single-decider trace has DECIDER line', /\[\+0\.00s\] DECIDER/.test(t1));
ok('single-decider trace has model', /model=opus-mock/.test(t1));
ok('single-decider trace has dt', /\b800ms\b/.test(t1));

const t2 = H.__symFormatTrace([
  { stage: 'decider', depth: 0, role: 'decider', t0: 1000, dt: 800, ok: true, construct: 'c1', model: 'opus-mock', escalateRequest: null, blockInfo: { text: 2, thinking: 0, tool: 0, other: 0, thinkText: 0 } },
  { stage: 'judge', depth: 0, role: 'judge', t0: 2000, dt: 1500, ok: true, construct: 'c2', model: 'sonnet-mock', escalateRequest: 'need X', blockInfo: { text: 2, thinking: 0, tool: 0, other: 0, thinkText: 0 } },
]);
ok('judge escalation shows ESCALATE prefix', /ESCALATE: need X/.test(t2));

const tErr = H.__symFormatTrace([{ stage: 'decider', depth: 0, role: 'decider', t0: 1000, dt: 0, ok: false, construct: '', model: 'm', escalateRequest: null, blockInfo: { text: 0, thinking: 0, tool: 0, other: 0, thinkText: 0 }, error: 'connection refused' }]);
ok('error trace has failure header', /cc-symbolic reasoning failed/.test(tErr));
ok('error trace has error line', /error: connection refused/.test(tErr));

// =========================================================================
// TEST 11: __symRun — normal (no escalation) → 1 decider, 1 judge, 1 translator
// =========================================================================
console.log('\nTEST 11: __symRun normal — no escalation');
let deciderCalls = 0, judgeCalls = 0, translatorCalls = 0;
let deciderFeedbacks = [], judgeDepths = [];
let translatorBody = null;

function makeClient({ escalateFirst = false } = {}) {
  return {
    create(b, opts) {
      const lastUser = [...(b.messages || [])].reverse().find(m => m.role === 'user');
      const instr = (lastUser && lastUser.content) || '';
      if (b.stream) {
        // TRANSLATOR
        translatorCalls++;
        translatorBody = b;
        const events = [
          msgStart(),
          blkStart(0, 'thinking', { thinking: '' }),
          thinkingDelta(0, 'rendering…'),
          blkStop(0),
          blkStart(1, 'text', { text: '' }),
          textDelta(1, 'Answer: 42'),
          blkStop(1),
          msgDelta(5),
          msgStop(),
        ];
        const data = asyncIter(events);
        // SDK v7: data has .controller (used by __symBuildThinkingWrapper terminal)
        data.controller = { signal: new AbortController().signal, abort: () => {} };
        return { withResponse: async () => ({ response: { status: 200 }, request_id: 'rid-tr', data }) };
      }
      if (/you are currently in the DECIDER mode/.test(instr)) {
        deciderCalls++;
        if (/JUDGE escalated/.test(instr)) deciderFeedbacks.push(instr);
        return Promise.resolve({ content: [{ type: 'text', text: '<construct>℧.think hypotheses answer=42 draft</construct>' }] });
      }
      if (/you are currently in the JUDGE mode/.test(instr)) {
        judgeCalls++;
        const isEsc = (judgeCalls === 1 && escalateFirst);
        judgeDepths.push(isEsc ? 'esc' : 'conv');
        if (isEsc) {
          return Promise.resolve({ content: [{ type: 'text', text: '[ESCALATE]℧.reflect ⇾ escalate(℧, need exact constraint values)[/ESCALATE]' }] });
        }
        return Promise.resolve({ content: [{ type: 'text', text: '<construct>℧.consensus chosen=42 verified</construct>' }] });
      }
      // Default: empty text
      return Promise.resolve({ content: [{ type: 'text', text: '' }] });
    },
  };
}

async function collectText(stream) {
  const out = [];
  for await (const e of stream) out.push(e);
  return out.filter(e => e.type === 'content_block_delta' && e.delta && e.delta.type === 'text_delta').map(e => e.delta.text).join('');
}

process.env.symbolic_thinking_max_escalate = '2';
deciderCalls = 0; judgeCalls = 0; translatorCalls = 0; deciderFeedbacks = []; judgeDepths = [];
const cN = makeClient({ escalateFirst: false });
const rN = await H.__symRun(cN, sbody, {});
ok('normal: returns result shape', rN && rN.response && rN.request_id && rN.data);
ok('normal: data is raw stream (has controller)', !!rN.data && ('controller' in rN.data));
ok('normal: trace attached (>= 3 records: decider + judge + translator-init)', Array.isArray(rN.__symTrace) && rN.__symTrace.length >= 3);
ok('normal: trace records are decider + judge + translator-init',
  rN.__symTrace[0].stage === 'decider' &&
  rN.__symTrace[1].stage === 'judge' &&
  rN.__symTrace[2].stage === 'translator-init');
const tN = await collectText(rN.data);
ok('normal: decider called once', deciderCalls === 1);
ok('normal: judge called once', judgeCalls === 1);
ok('normal: translator called once', translatorCalls === 1);
ok('normal: judge round 1 was conv', judgeDepths[0] === 'conv');
ok('normal: no decider feedback', deciderFeedbacks.length === 0);
ok('normal: translator output is "Answer: 42"', /Answer: 42/.test(tN));
ok('normal: no ESCALATE marker leaked to user', !/\[ESCALATE\]/.test(tN));
ok('normal: no ℧ notation leaked to user', !/℧\.|<construct>/.test(tN));
ok('normal: translator system has ℧ notation', translatorBody && /℧/.test(JSON.stringify(translatorBody.system)));
ok('normal: translator system has do-not-surface note', translatorBody && /Do NOT mention, quote, summarize/.test(JSON.stringify(translatorBody.system)));

// =========================================================================
// TEST 12: __symRun — escalation round (d0 escalates, d1 converges)
// =========================================================================
console.log('\nTEST 12: __symRun — escalation round');
deciderCalls = 0; judgeCalls = 0; translatorCalls = 0; deciderFeedbacks = []; judgeDepths = [];
const cE = makeClient({ escalateFirst: true });
const rE = await H.__symRun(cE, sbody, {});
const tE = await collectText(rE.data);
ok('esc: decider called twice (r1 + r2)', deciderCalls === 2);
ok('esc: decider r2 got feedback', deciderFeedbacks.length === 1 && /JUDGE escalated/.test(deciderFeedbacks[0]));
ok('esc: judge called twice (escalate then converge)', judgeCalls === 2);
ok('esc: translator called once', translatorCalls === 1);
ok('esc: judge r1 was esc, r2 was conv', judgeDepths[0] === 'esc' && judgeDepths[1] === 'conv');
ok('esc: trace has 5 records (decider + judge + decider + judge + translator-init)', rE.__symTrace.length === 5);
ok('esc: trace records alternate roles correctly',
  rE.__symTrace[0].stage === 'decider' &&
  rE.__symTrace[1].stage === 'judge' &&
  rE.__symTrace[2].stage === 'decider' &&
  rE.__symTrace[3].stage === 'judge' &&
  rE.__symTrace[4].stage === 'translator-init');
ok('esc: r1 judge has escalateRequest, r2 judge has none',
  rE.__symTrace[1].escalateRequest !== null &&
  rE.__symTrace[3].escalateRequest === null);
ok('esc: no marker in translator output', !/\[ESCALATE\]/.test(tE));
ok('esc: translator output is the answer', /Answer: 42/.test(tE));

// =========================================================================
// TEST 13: __symRun — cap (MAX=1) — exactly one escalation, then cap converges
// =========================================================================
console.log('\nTEST 13: __symRun — escalation cap (MAX=1)');
process.env.symbolic_thinking_max_escalate = '1';
deciderCalls = 0; judgeCalls = 0; translatorCalls = 0; deciderFeedbacks = []; judgeDepths = [];
const cC = makeClient({ escalateFirst: true });  // d0 escalates; d1 (cap) converges
const rC = await H.__symRun(cC, sbody, {});
const tC = await collectText(rC.data);
ok('cap: decider called twice', deciderCalls === 2);
ok('cap: judge called twice', judgeCalls === 2);
ok('cap: translator called once', translatorCalls === 1);
ok('cap: no marker in translator output', !/\[ESCALATE\]/.test(tC));
ok('cap: translator output is the answer', /Answer: 42/.test(tC));
process.env.symbolic_thinking_max_escalate = '2';

// =========================================================================
// TEST 14: __symRun — translator stream has .controller (required by wrapper)
// =========================================================================
console.log('\nTEST 14: __symRun return shape (raw SDK v7 MessageStream contract)');
deciderCalls = 0; judgeCalls = 0; translatorCalls = 0;
const cR = makeClient({ escalateFirst: false });
const rR = await H.__symRun(cR, sbody, {});
ok('result.response has status 200', rR.response && rR.response.status === 200);
ok('result.request_id is a string', typeof rR.request_id === 'string');
ok('result.data is async-iterable', typeof rR.data[Symbol.asyncIterator] === 'function');
ok('result.data has .controller (raw SDK v7 contract)', 'controller' in rR.data);
ok('result.data.controller has .signal', rR.data.controller && 'signal' in rR.data.controller);

// =========================================================================
// TEST 15: __symRun — decider error returns {data:null, __symTrace: [err record]}
// =========================================================================
console.log('\nTEST 15: __symRun — decider error path');
const cErr = {
  create(b) { return Promise.reject(new Error('decider 500')); },
};
const rErr = await H.__symRun(cErr, sbody, {});
ok('decider error: response is null', rErr.response === null);
ok('decider error: data is null', rErr.data === null);
ok('decider error: trace has 1 record (the decider error)', rErr.__symTrace.length === 1);
ok('decider error: trace record is the decider', rErr.__symTrace[0].stage === 'decider');
ok('decider error: trace record has error', /decider 500/.test(rErr.__symTrace[0].error));
ok('decider error: trace record ok=false', rErr.__symTrace[0].ok === false);

// =========================================================================
// TEST 16: __symTierModel — env tier lookup with fallback
// =========================================================================
console.log('\nTEST 16: __symTierModel — env tier lookup with fallback');
delete process.env.OPUS_MODEL; delete process.env.SONNET_MODEL; delete process.env.HAIKU_MODEL;
ok('unknown tier -> fallback',      H.__symTierModel('foo', 'fb') === 'fb');
ok('opus tier, no env -> fallback',H.__symTierModel('opus', 'fb-opus') === 'fb-opus');
ok('sonnet tier, no env -> fallback',H.__symTierModel('sonnet', 'fb-sonnet') === 'fb-sonnet');
ok('haiku tier, no env -> fallback',H.__symTierModel('haiku', 'fb-haiku') === 'fb-haiku');
process.env.OPUS_MODEL = 'o'; process.env.SONNET_MODEL = 's'; process.env.HAIKU_MODEL = 'h';
ok('opus tier with env',  H.__symTierModel('opus', 'x') === 'o');
ok('sonnet tier with env',H.__symTierModel('sonnet', 'x') === 's');
ok('haiku tier with env', H.__symTierModel('haiku', 'x') === 'h');
delete process.env.OPUS_MODEL; delete process.env.SONNET_MODEL; delete process.env.HAIKU_MODEL;
process.env.OPUS_MODEL = 'opus-mock'; process.env.SONNET_MODEL = 'sonnet-mock'; process.env.HAIKU_MODEL = 'haiku-mock';  // restore for later tests

// =========================================================================
// TEST 17: __symStripModelFlag — drop --model pairs (respawnFlags dedup)
// =========================================================================
console.log('\nTEST 17: __symStripModelFlag — drops --model pairs');
ok('empty arr + non-model pair -> [foo,bar]',       JSON.stringify(H.__symStripModelFlag([], 'foo', 'bar')) === '["foo","bar"]');
ok('arr with --model x + e==--model -> x dropped, NO new --model appended',JSON.stringify(H.__symStripModelFlag(['a','b'], '--model', 'claude-opus')) === '["a","b"]');
ok('arr with --model x -> x dropped',                JSON.stringify(H.__symStripModelFlag(['--model','claude-opus','rest'], 'whatever','val')) === '["rest","whatever","val"]');
ok('multiple --model dropped',                       JSON.stringify(H.__symStripModelFlag(['--model','a','--model','b','keep'], '--debug','on')) === '["keep","--debug","on"]');
ok('arr with --model + non-model pair -> model dropped, pair appended',JSON.stringify(H.__symStripModelFlag(['--model','old'], '--debug', true)) === '["--debug",true]');
ok('appending --model directly via (e,t) is suppressed (e==--model -> no push)',JSON.stringify(H.__symStripModelFlag(['keep'], '--model', 'new')) === '["keep"]');
ok('appending --model with no t is suppressed',      JSON.stringify(H.__symStripModelFlag(['keep'], '--model', undefined)) === '["keep"]');
ok('non-model pair without t -> just e pushed',     JSON.stringify(H.__symStripModelFlag(['keep'], '--verbose', undefined)) === '["keep","--verbose"]');

// =========================================================================
// TEST 18: __symIsCloud — backend detection for thinking-ignore behavior
// =========================================================================
console.log('\nTEST 18: __symIsCloud — backend detection');
ok('empty model -> false',           H.__symIsCloud('') === false);
ok('null model -> false',            H.__symIsCloud(null) === false);
ok('undefined model -> false',       H.__symIsCloud(undefined) === false);
ok('real anthropic model -> false',  H.__symIsCloud('claude-opus-4-8') === false);
ok('minimax-m3:cloud -> true',       H.__symIsCloud('minimax-m3:cloud') === true);
ok('glm-5.2:cloud -> true',          H.__symIsCloud('glm-5.2:cloud') === true);
delete process.env.ANTHROPIC_BASE_URL;
ok('plain model with no base URL -> false', H.__symIsCloud('claude-haiku') === false);
process.env.ANTHROPIC_BASE_URL = 'http://localhost:11434/ollama';
ok('ollama base URL triggers cloud path', H.__symIsCloud('claude-haiku') === true);
process.env.ANTHROPIC_BASE_URL = 'https://api.anthropic.com';
ok('official base URL -> not cloud (for claude-haiku)', H.__symIsCloud('claude-haiku') === false);
delete process.env.ANTHROPIC_BASE_URL;

// =========================================================================
// TEST 19: __symStageMaxTokens — cap cloud-fallback max_tokens
// =========================================================================
console.log('\nTEST 19: __symStageMaxTokens — cloud fallback budget cap');
ok('real anthropic model -> undefined (no cap)',H.__symStageMaxTokens({model:'claude-opus-4-8'}, 1500) === undefined);
ok('minimax cloud model with budget -> returns budget',H.__symStageMaxTokens({model:'minimax-m3:cloud'}, 1500) === 1500);
ok('glm cloud model with budget -> returns budget',H.__symStageMaxTokens({model:'glm-5.2:cloud'}, '3000') === 3000);
ok('cloud model, zero budget -> undefined (no cap applied)',H.__symStageMaxTokens({model:'minimax-m3:cloud'}, 0) === undefined);
ok('cloud model, NaN budget -> undefined', H.__symStageMaxTokens({model:'minimax-m3:cloud'}, 'abc') === undefined);
ok('null body -> undefined', H.__symStageMaxTokens(null, 1500) === undefined);

// =========================================================================
// TEST 20: __symBuildThinkingWrapper — deep-mode passthrough (no summary
// if trace already has rich thinking blocks, e.g. upstream emitted them
// directly because symbolic_thinking_deep=1).
// =========================================================================
console.log('\nTEST 20: __symBuildThinkingWrapper — deep-mode passthrough');
process.env.symbolic_thinking_trace = '1';
// trace with a stage that has thinking>0 and thinkText>0 (simulating deep mode)
const TRACE_DEEP = [
  { stage:'decider', depth:0, role:'decider', t0:0, dt:100, ok:true,
    construct:'x', model:'minimax-m3:cloud', escalateRequest:null,
    blockInfo:{text:0, thinking:1, tool:0, other:0, thinkText:50} },
];
const upstreamDeep = {
  [Symbol.asyncIterator]() { return { next: async () => ({value:undefined,done:true}), return: async () => ({value:undefined,done:true}) }; },
  controller: { signal: new AbortController().signal, abort: () => {} },
};
const wrapDeep = H.__symBuildThinkingWrapper(TRACE_DEEP, upstreamDeep, null);
ok('deep-mode trace: wrapper returns upstream unchanged (=== upstream)', wrapDeep === upstreamDeep);
// and trace=0 -> off
process.env.symbolic_thinking_trace = '0';
const wrapOff = H.__symBuildThinkingWrapper([], upstreamDeep, null);
ok('trace=0: wrapper returns upstream unchanged', wrapOff === upstreamDeep);
process.env.symbolic_thinking_trace = '1';
// trace without thinking blocks -> wrapper DOES wrap (not passthrough)
const TRACE_FLAT = [
  { stage:'decider', depth:0, role:'decider', t0:0, dt:100, ok:true,
    construct:'x', model:'opus-mock', escalateRequest:null,
    blockInfo:{text:1, thinking:0, tool:0, other:0, thinkText:0} },
];
const upstreamFlat = {
  [Symbol.asyncIterator]() { return { next: async () => ({value:msgStart(),done:false}), return: async () => ({value:undefined,done:true}) }; },
  controller: { signal: new AbortController().signal, abort: () => {} },
};
const wrapFlat = H.__symBuildThinkingWrapper(TRACE_FLAT, upstreamFlat, null);
ok('trace=1, no upstream thinking yet: wrapper wraps (returns a new iterator)', wrapFlat !== upstreamFlat && typeof wrapFlat[Symbol.asyncIterator] === 'function');

console.log(`\nRESULT: ${pass} pass, ${fail} fail`);
process.exit(fail ? 1 : 0);
} catch (err) {
  console.error('HARNESS ERROR:', err && err.stack || err);
  process.exit(2);
}
})();
