#!/usr/bin/env node
/* Logic harness for the v2 2-stage helper: inspector + judge->decider escalation.
 * Loads the HELPER string from gen_patch.py (via a generated JS snippet), exposes
 * process/require, and drives __tsInspect + __tsEscalate with a mocked client
 * whose judge stream emits an escalate marker on the first call and a normal
 * answer on the second.
 */
const fs = require('fs');
const vm = require('vm');

// --- pull the HELPER JS out of gen_patch.py ---
const { execSync } = require('child_process');
const helperJs = execSync(
  "python3 -c 'import gen_patch,json; print(json.dumps(gen_patch.HELPER))'",
  { cwd: __dirname, encoding: 'utf8' }
).trim();
const HELPER = JSON.parse(helperJs);  // the JS source string

// Clean env so __tsDbg is off (no /tmp/ts.log writes).
delete process.env.TWO_STAGE_DEBUG;
process.env.TWO_STAGE_ENABLED = '1';
process.env.TWO_STAGE_SCIMIND = '1';   // canonical v2 config: judge carries SciMind (opt-in now)
process.env.TWO_STAGE_MAX_ESCALATE = '2';
process.env.TWO_STAGE_DECIDER_CONCISE = '1';
process.env.TWO_STAGE_STAGE1_TIER = 'opus';
process.env.TWO_STAGE_STAGE2_TIER = 'sonnet';
process.env.OPUS_MODEL = 'opus-mock';
process.env.SONNET_MODEL = 'sonnet-mock';

const sandbox = { process, require, console, Buffer };
sandbox.global = sandbox;
vm.createContext(sandbox);
vm.runInContext(HELPER + '\n', sandbox);
const H = sandbox;  // holds the helper functions

let pass = 0, fail = 0;
function ok(name, cond) { (cond ? (pass++, console.log('  PASS', name)) : (fail++, console.log('  FAIL', name))); }

// --- helpers to build SSE-style parsed events ---
const ev = (type, extra) => Object.assign({ type }, extra);
const msgStart = () => ev('message_start', { message: { usage: { input_tokens: 1, output_tokens: 0 } } });
const msgDelta = (o) => ev('message_delta', { usage: { input_tokens: 1, output_tokens: o } });
const msgStop = () => ev('message_stop', {});
const blkStart = (index, cbtype) => ev('content_block_start', { index, content_block: { type: cbtype } });
const blkStop = (index) => ev('content_block_stop', { index });
const thinkingDelta = (i, t) => ev('content_block_delta', { index: i, delta: { type: 'thinking_delta', thinking: t } });
const textDelta = (i, t) => ev('content_block_delta', { index: i, delta: { type: 'text_delta', text: t } });

function asyncIter(events) {
  return {
    [Symbol.asyncIterator]() {
      let k = 0;
      return {
        next: async () => (k < events.length ? { value: events[k++], done: false } : { value: undefined, done: true }),
        return: async () => ({ value: undefined, done: true }),  // mimic v7 auto-abort finally
      };
    },
  };
}

// --- mock client ---
let deciderCalls = 0;
let deciderFeedbacks = [];
let judgeStreamCalls = 0;
let judgeBodies = [];

function makeDraftMessage(body) {
  // record feedback (round 2 has an extra user msg about insufficiency)
  const lastUser = [...(body.messages || [])].reverse().find(m => m.role === 'user');
  deciderCalls++;
  if (lastUser && /JUDGE found your previous draft insufficient/.test(lastUser.content)) {
    deciderFeedbacks.push(lastUser.content);
  }
  return { content: [{ type: 'text', text: 'draft: approach = brute force; answer = 42' }] };
}

function makeJudgeStream(body) {
  judgeStreamCalls++;
  judgeBodies.push({ model: body.model, depth: body._testDepth });
  const idx = judgeStreamCalls;
  const events = [msgStart()];
  if (idx === 1) {
    // first judge: ESCALATE
    events.push(blkStart(0, 'thinking'), thinkingDelta(0, 'hmm...'), blkStop(0));
    events.push(blkStart(1, 'text'), textDelta(1, '[ESCALATE]need the exact constraint values to verify[/ESCALATE]'), blkStop(1));
  } else {
    // second judge: normal answer
    events.push(blkStart(0, 'thinking'), thinkingDelta(0, 'verifying...'), blkStop(0));
    events.push(blkStart(1, 'text'), textDelta(1, 'Answer: 42'), blkStop(1));
  }
  events.push(msgDelta(10), msgStop());
  return asyncIter(events);
}

const client = {
  create(body, opts) {
    if (body.stream) {
      // judge stream -> APIPromise-like with .withResponse()
      return { withResponse: async () => ({ response: { status: 200 }, request_id: 'rid-' + judgeStreamCalls, data: makeJudgeStream(body) }) };
    }
    return Promise.resolve(makeDraftMessage(body));
  },
};

// --- TEST 1: escalation fires, decider runs twice, final answer has no marker ---
console.log('TEST 1: escalation (MAX=2, first judge escalates, second answers)');
deciderCalls = 0; deciderFeedbacks = []; judgeStreamCalls = 0; judgeBodies = [];
const body = { model: 'opus-mock', thinking: { type: 'enabled', budget_tokens: 2000 }, messages: [{ role: 'user', content: 'solve' }], tools: [{ name: 'Read' }] };
// build first judge body via __twoStage
(async () => {
  const jb = await H.__twoStage(client, body);
  ok('judge body model is sonnet', jb.model === 'sonnet-mock');
  ok('judge body preserves tools', jb.tools && jb.tools.length === 1);
  ok('judge body has SciMind preamble', typeof jb.system === 'string' && jb.system.length > 100);
  ok('judge body has escalate-aware instruction (depth 0)', /\[ESCALATE\]/.test(jb.messages[jb.messages.length - 1].content));

  // run the inspector on the first judge stream
  const ctx = { client, body, t: {} };
  // mimic the Hook A proxy: create the (first) judge, get wr.data, wrap in inspector
  const p = client.create(Object.assign({}, jb, { stream: true }), {});
  const wr = await p.withResponse();
  const out = [];
  for await (const e of H.__tsInspect(wr.data, ctx, 0)) out.push(e);

  // collect text deltas from output
  const text = out.filter(e => e.type === 'content_block_delta' && e.delta && e.delta.type === 'text_delta').map(e => e.delta.text).join('');
  console.log('   output text:', JSON.stringify(text));
  ok('decider called twice (round1 + round2)', deciderCalls === 2);
  ok('decider round2 got feedback', deciderFeedbacks.length === 1 && /insufficient/.test(deciderFeedbacks[0]));
  ok('judge streamed twice (escalate then answer)', judgeStreamCalls === 2);
  ok('final output has NO escalate marker', !/\[ESCALATE\]/.test(text));
  ok('final output is the answer', /Answer: 42/.test(text));
  ok('output preserved message_start', out.some(e => e.type === 'message_start'));
  ok('output preserved thinking block (from 2nd judge)', out.some(e => e.type === 'content_block_delta' && e.delta && e.delta.type === 'thinking_delta'));

  // --- TEST 2: depth cap (MAX=1) -> exactly one escalation, then answer ---
  console.log('TEST 2: depth cap (MAX=1)');
  process.env.TWO_STAGE_MAX_ESCALATE = '1';
  deciderCalls = 0; deciderFeedbacks = []; judgeStreamCalls = 0;
  // judge at depth 1 should get the CAP instruction (no escalate option)
  const jb0 = await H.__twoStage(client, body);
  ok('depth0 instruction still has escalate option', /\[ESCALATE\]/.test(jb0.messages[jb0.messages.length - 1].content));
  // simulate the escalation: run inspector at depth 0; 2nd judge (depth1) answers normally
  const p2 = client.create(Object.assign({}, jb0, { stream: true }), {});
  const wr2 = await p2.withResponse();
  const out2 = [];
  for await (const e of H.__tsInspect(wr2.data, { client, body, t: {} }, 0)) out2.push(e);
  const text2 = out2.filter(e => e.type === 'content_block_delta' && e.delta && e.delta.type === 'text_delta').map(e => e.delta.text).join('');
  console.log('   output text:', JSON.stringify(text2));
  ok('cap: decider called twice', deciderCalls === 2);
  ok('cap: judge streamed twice', judgeStreamCalls === 2);
  ok('cap: no marker in final output', !/\[ESCALATE\]/.test(text2));
  // verify __tsJudgeBody at depth>=MAX drops the escalate option
  const jbCap = H.__tsJudgeBody(body, 'draft', 1);
  // cap branch must NOT carry the escalation directive ("begin your response with EXACTLY ...")
  ok('cap instruction has NO escalate directive at depth>=MAX', !/begin your response with EXACTLY/.test(jbCap.messages[jbCap.messages.length - 1].content));
  ok('cap instruction forbids further escalation', /no further escalation is allowed/.test(jbCap.messages[jbCap.messages.length - 1].content));
  // aware branch (depth < MAX) MUST carry the directive
  const jbAware = H.__tsJudgeBody(body, 'draft', 0);
  ok('aware instruction HAS escalate directive at depth<MAX', /begin your response with EXACTLY/.test(jbAware.messages[jbAware.messages.length - 1].content));

  // --- TEST 3: normal turn (no marker) -> 1x judge, real SSE passthrough ---
  console.log('TEST 3: normal turn (judge answers first time, no escalation)');
  process.env.TWO_STAGE_MAX_ESCALATE = '2';
  deciderCalls = 0; judgeStreamCalls = 0;
  // override mock to answer on first call
  const oldMakeStream = makeJudgeStream;
  judgeStreamCalls = 0;
  // monkeypatch: first judge answers normally
  let firstAnswered = false;
  const client2 = {
    create(b, o) {
      if (b.stream) {
        return { withResponse: async () => {
          judgeStreamCalls++;
          const events = [msgStart(), blkStart(0,'thinking'), thinkingDelta(0,'t'), blkStop(0), blkStart(1,'text'), textDelta(1,'hi there'), blkStop(1), msgDelta(3), msgStop()];
          return { response: {status:200}, request_id: 'r', data: asyncIter(events) };
        }};
      }
      return Promise.resolve(makeDraftMessage(b));
    }
  };
  const jb3 = await H.__twoStage(client2, body);
  const p3 = client2.create(Object.assign({}, jb3, { stream: true }), {});
  const wr3 = await p3.withResponse();
  const out3 = [];
  for await (const e of H.__tsInspect(wr3.data, { client: client2, body, t: {} }, 0)) out3.push(e);
  const text3 = out3.filter(e => e.type==='content_block_delta' && e.delta && e.delta.type==='text_delta').map(e=>e.delta.text).join('');
  ok('normal: judge streamed exactly once', judgeStreamCalls === 1);
  ok('normal: decider called once', deciderCalls === 1);
  ok('normal: answer text intact', text3 === 'hi there');
  ok('normal: all 9 events passed through', out3.length === 9);

  // --- TEST 4: __tsEscReq regex + __tsRe escaping ---
  console.log('TEST 4: regex helpers');
  ok('__tsEscReq extracts request', H.__tsEscReq('[ESCALATE]give me X[/ESCALATE]') === 'give me X');
  ok('__tsEscReq returns null when no marker', H.__tsEscReq('just an answer') === null);
  ok('__tsRe escapes metachars', H.__tsRe('[ESC]') === '\\[ESC\\]');
  // custom marker
  process.env.TWO_STAGE_ESCALATE_OPEN = '[ESC]'; process.env.TWO_STAGE_ESCALATE_CLOSE = '[/ESC]';
  ok('custom marker respected by __tsEscReq', H.__tsEscReq('[ESC]need Y[/ESC]') === 'need Y');
  delete process.env.TWO_STAGE_ESCALATE_OPEN; delete process.env.TWO_STAGE_ESCALATE_CLOSE;

  // === SYMBOLIC 3-STAGE TESTS =============================================
  console.log('\nTEST 5: __tsSymEnabled env read');
  delete process.env.TWO_STAGE_SYMBOLIC;
  ok('sym disabled by default', H.__tsSymEnabled() === false);
  process.env.TWO_STAGE_SYMBOLIC = '0'; ok('sym disabled on 0', H.__tsSymEnabled() === false);
  process.env.TWO_STAGE_SYMBOLIC = 'false'; ok('sym disabled on false', H.__tsSymEnabled() === false);
  process.env.TWO_STAGE_SYMBOLIC = '1'; ok('sym enabled on 1', H.__tsSymEnabled() === true);

  console.log('TEST 6: symbolic body builders');
  const sbody = { model: 'opus-mock', thinking: { type: 'enabled', budget_tokens: 2000 },
    messages: [{ role: 'user', content: 'solve' }], tools: [{ name: 'Read' }], system: 'SYS' };
  const jbSym = H.__tsSymJudgeBody(sbody, '<construct>draft</construct>', 0);
  ok('sym judge body model is sonnet', jbSym.model === 'sonnet-mock');
  ok('sym judge body preserves tools', jbSym.tools && jbSym.tools.length === 1);
  ok('sym judge body non-stream', jbSym.stream === false);
  ok('sym judge system has ℧ notation', /℧/.test(jbSym.system));
  ok('sym judge system has SciMind', /epistemic|humility|falsif/i.test(jbSym.system) || jbSym.system.length > 200);
  ok('sym judge instruction has ESCALATE directive (depth 0)', /\[ESCALATE\]/.test(jbSym.messages[jbSym.messages.length - 1].content));
  const jbSymCap = H.__tsSymJudgeBody(sbody, 'draft', 2);
  ok('sym cap instruction forbids ESCALATE', !/emit EXACTLY \[ESCALATE\]/.test(jbSymCap.messages[jbSymCap.messages.length - 1].content));
  ok('sym cap instruction says no further escalation', /no further escalation is allowed/.test(jbSymCap.messages[jbSymCap.messages.length - 1].content));
  const obSym = H.__tsSymOutputBody(sbody, '<construct>final</construct>');
  ok('sym output body model is sonnet', obSym.model === 'sonnet-mock');
  ok('sym output body is stream', obSym.stream === true);
  ok('sym output body preserves tools', obSym.tools && obSym.tools.length === 1);
  ok('sym output instruction says TRANSLATOR', /TRANSLATOR/.test(obSym.messages[obSym.messages.length - 1].content));

  // --- symbolic mock client ---
  let symDeciderCalls = 0, symDeciderFeedbacks = [], symJudgeCalls = 0, symTranslatorCalls = 0;
  let symJudgeDepths = [], symTranslatorBody = null;
  function makeSymClient({ escalateFirst = true } = {}) {
    return {
      create(b, o) {
        const lastUser = [...(b.messages || [])].reverse().find(m => m.role === 'user');
        const instr = (lastUser && lastUser.content) || '';
        if (b.stream) {
          // TRANSLATOR
          symTranslatorCalls++; symTranslatorBody = b;
          const events = [msgStart(), blkStart(0, 'thinking'), thinkingDelta(0, 'rendering'), blkStop(0),
            blkStart(1, 'text'), textDelta(1, 'Answer: 42'), blkStop(1), msgDelta(5), msgStop()];
          const data = asyncIter(events); data.controller = {};  // mimic SDK v7 stream
          return { withResponse: async () => ({ response: { status: 200 }, request_id: 'rid-tr', data }) };
        }
        if (instr.startsWith('You are the DECIDER')) {
          symDeciderCalls++;
          if (/JUDGE escalated/.test(instr)) symDeciderFeedbacks.push(instr);
          return Promise.resolve({ content: [{ type: 'text', text: '<construct>℧.think hypotheses answer=42 draft</construct>' }] });
        }
        if (instr.startsWith('You are the JUDGE')) {
          symJudgeCalls++;
          const depth = (symJudgeCalls === 1 && escalateFirst);
          symJudgeDepths.push(depth ? 'esc' : 'conv');
          if (depth) {
            return Promise.resolve({ content: [{ type: 'text', text: '[ESCALATE]℧.reflect ⇾ escalate(℧, need exact constraint values)[/ESCALATE]' }] });
          }
          return Promise.resolve({ content: [{ type: 'text', text: '<construct>℧.consensus chosen=42 verified</construct>' }] });
        }
        return Promise.resolve({ content: [{ type: 'text', text: '' }] });
      },
    };
  }
  async function collectText(stream) {
    const out = [];
    for await (const e of stream) out.push(e);
    return out.filter(e => e.type === 'content_block_delta' && e.delta && e.delta.type === 'text_delta').map(e => e.delta.text).join('');
  }

  console.log('TEST 7: symbolic normal (no escalation) -> 1 decider, 1 judge, 1 translator, NL output');
  process.env.TWO_STAGE_MAX_ESCALATE = '2';
  symDeciderCalls = 0; symDeciderFeedbacks = []; symJudgeCalls = 0; symTranslatorCalls = 0; symJudgeDepths = [];
  const c7 = makeSymClient({ escalateFirst: false });
  const r7 = await H.__tsSymbolicRun(c7, sbody, {});
  ok('sym normal: returns a result object', r7 && r7.response && r7.request_id && r7.data);
  ok('sym normal: data is raw stream (controller in data)', !!r7.data && ('controller' in r7.data));
  const t7 = await collectText(r7.data);
  console.log('   translator text:', JSON.stringify(t7));
  ok('sym normal: decider called once', symDeciderCalls === 1);
  ok('sym normal: judge called once', symJudgeCalls === 1);
  ok('sym normal: translator called once', symTranslatorCalls === 1);
  ok('sym normal: translator output is NL "Answer: 42"', /Answer: 42/.test(t7));
  ok('sym normal: no ESCALATE marker leaked to user', !/\[ESCALATE\]/.test(t7));
  ok('sym normal: no ℧ notation leaked to user', !/℧\.|<construct>/.test(t7));
  ok('sym normal: translator body used ℧ system', symTranslatorBody && /℧/.test(symTranslatorBody.system));

  console.log('TEST 8: symbolic escalation (d0 escalate, d1 converge, translator)');
  symDeciderCalls = 0; symDeciderFeedbacks = []; symJudgeCalls = 0; symTranslatorCalls = 0; symJudgeDepths = [];
  const c8 = makeSymClient({ escalateFirst: true });
  const r8 = await H.__tsSymbolicRun(c8, sbody, {});
  const t8 = await collectText(r8.data);
  console.log('   translator text:', JSON.stringify(t8));
  ok('sym esc: decider called twice (r1 + r2)', symDeciderCalls === 2);
  ok('sym esc: decider r2 got feedback', symDeciderFeedbacks.length === 1 && /JUDGE escalated/.test(symDeciderFeedbacks[0]));
  ok('sym esc: judge called twice (escalate then converge)', symJudgeCalls === 2);
  ok('sym esc: translator called once', symTranslatorCalls === 1);
  ok('sym esc: judge round 1 was escalate', symJudgeDepths[0] === 'esc');
  ok('sym esc: judge round 2 converged', symJudgeDepths[1] === 'conv');
  ok('sym esc: no marker in translator output', !/\[ESCALATE\]/.test(t8));
  ok('sym esc: translator output is the answer', /Answer: 42/.test(t8));

  console.log('TEST 9: symbolic cap (MAX=1) -> exactly one escalation, then cap converges');
  process.env.TWO_STAGE_MAX_ESCALATE = '1';
  symDeciderCalls = 0; symDeciderFeedbacks = []; symJudgeCalls = 0; symTranslatorCalls = 0; symJudgeDepths = [];
  const c9 = makeSymClient({ escalateFirst: true });  // d0 judge escalates; d1 (cap) converges
  const r9 = await H.__tsSymbolicRun(c9, sbody, {});
  const t9 = await collectText(r9.data);
  ok('sym cap: decider called twice', symDeciderCalls === 2);
  ok('sym cap: judge called twice', symJudgeCalls === 2);
  ok('sym cap: translator called once', symTranslatorCalls === 1);
  ok('sym cap: no marker in translator output', !/\[ESCALATE\]/.test(t9));
  ok('sym cap: translator output is the answer', /Answer: 42/.test(t9));
  // cap instruction (depth >= MAX=1) forbids ESCALATE
  const jbCap1 = H.__tsSymJudgeBody(sbody, 'draft', 1);
  ok('sym cap d1 instruction forbids ESCALATE directive', !/emit EXACTLY \[ESCALATE\]/.test(jbCap1.messages[jbCap1.messages.length - 1].content));
  const jbAware0 = H.__tsSymJudgeBody(sbody, 'draft', 0);
  ok('sym aware d0 instruction has ESCALATE directive', /emit EXACTLY \[ESCALATE\]/.test(jbAware0.messages[jbAware0.messages.length - 1].content));
  process.env.TWO_STAGE_MAX_ESCALATE = '2';

  console.log('TEST 10: __tsSymbolicRun returns {response,request_id,data} shape (raw stream)');
  const c10 = makeSymClient({ escalateFirst: false });
  const r10 = await H.__tsSymbolicRun(c10, sbody, {});
  ok('sym shape: has response', r10.response && r10.response.status === 200);
  ok('sym shape: has request_id', typeof r10.request_id === 'string');
  ok('sym shape: data is the raw SDK stream (not a Proxy wrapper)', 'controller' in r10.data && typeof r10.data[Symbol.asyncIterator] === 'function');

  // === 3-OPT-IN TRUTH TABLE ===============================================
  console.log('\nTEST 11: __tsFlagOn opt-in semantics');
  delete process.env.TWO_STAGE_ENABLED; delete process.env.TWO_STAGE_SYMBOLIC; delete process.env.TWO_STAGE_SCIMIND;
  ok('flag unset -> off', H.__tsFlagOn('TWO_STAGE_SCIMIND') === false);
  process.env.TWO_STAGE_SCIMIND = '0'; ok('flag 0 -> off', H.__tsFlagOn('TWO_STAGE_SCIMIND') === false);
  process.env.TWO_STAGE_SCIMIND = 'no'; ok('flag no -> off', H.__tsFlagOn('TWO_STAGE_SCIMIND') === false);
  process.env.TWO_STAGE_SCIMIND = '1'; ok('flag 1 -> on', H.__tsFlagOn('TWO_STAGE_SCIMIND') === true);
  process.env.TWO_STAGE_SCIMIND = 'true'; ok('flag true -> on', H.__tsFlagOn('TWO_STAGE_SCIMIND') === true);
  process.env.TWO_STAGE_SCIMIND = 'ON'; ok('flag ON (case-insensitive) -> on', H.__tsFlagOn('TWO_STAGE_SCIMIND') === true);

  console.log('TEST 12: __tsTrigger opt-in truth table (thinking enabled body)');
  const thinkBody = { model: 'opus-mock', thinking: { type: 'enabled', budget_tokens: 2000 }, messages: [{ role: 'user', content: 'q' }] };
  function setFlags(en, sym) { if (en) process.env.TWO_STAGE_ENABLED = '1'; else delete process.env.TWO_STAGE_ENABLED; if (sym) process.env.TWO_STAGE_SYMBOLIC = '1'; else delete process.env.TWO_STAGE_SYMBOLIC; }
  setFlags(false, false); ok('000 trigger false (stock)', H.__tsTrigger(thinkBody) === false);
  setFlags(true, false);  ok('010 trigger true (2-stage)', H.__tsTrigger(thinkBody) === true);
  setFlags(false, true);  ok('100 trigger true (symbolic forces)', H.__tsTrigger(thinkBody) === true);
  setFlags(true, true);   ok('110 trigger true', H.__tsTrigger(thinkBody) === true);
  // no thinking -> never triggers even with flags
  ok('no-thinking body never triggers', H.__tsTrigger({ model: 'x' }) === false);
  setFlags(false, false);
  ok('non-thinking body with scimind only -> no 2-stage trigger', H.__tsTrigger({ model: 'x', thinking: { type: 'disabled' } }) === false);

  console.log('TEST 13: SciMind preamble is opt-in (conditional in judge + symbolic system)');
  delete process.env.TWO_STAGE_SCIMIND;
  const jbNoSci = H.__tsJudgeBody(sbody, 'draft', 0);
  ok('scimind OFF: judge system has NO SciMind preamble', !/epistemic|humility|falsif/i.test(jbNoSci.system) || jbNoSci.system.length < 300);
  ok('scimind OFF: judge system is original sys', /SYS/.test(jbNoSci.system));
  const symSysNoSci = H.__tsSymSystem(sbody);
  ok('scimind OFF: symbolic system still has ℧ notation', /℧/.test(symSysNoSci));
  ok('scimind OFF: symbolic system has NO SciMind', !/SciMind|Epistemic Humility|Incomplete Suggestion Protocol/i.test(symSysNoSci));
  process.env.TWO_STAGE_SCIMIND = '1';
  const jbSci = H.__tsJudgeBody(sbody, 'draft', 0);
  ok('scimind ON: judge system HAS SciMind preamble', jbSci.system.length > jbNoSci.system.length && /SYS/.test(jbSci.system));
  const symSysSci = H.__tsSymSystem(sbody);
  ok('scimind ON: symbolic system has BOTH SciMind + ℧', /℧/.test(symSysSci) && symSysSci.length > symSysNoSci.length);
  ok('scimind ON: __tsScimindSys(sys) prepends preamble', /SYS/.test(H.__tsScimindSys('SYS')));
  delete process.env.TWO_STAGE_SCIMIND;
  ok('scimind OFF: __tsScimindSys(sys) returns sys unchanged', H.__tsScimindSys('SYS') === 'SYS');
  ok('scimind OFF: __tsScimindSys(undefined) returns undefined', H.__tsScimindSys(undefined) === undefined);

  console.log('TEST 14: symbolic run respects scimind (translator system conditional)');
  // symbolic normal run with scimind ON -> translator system has SciMind
  process.env.TWO_STAGE_MAX_ESCALATE = '2';
  process.env.TWO_STAGE_SCIMIND = '1';
  symTranslatorBody = null; symDeciderCalls = 0; symJudgeCalls = 0; symTranslatorCalls = 0;
  const c14 = makeSymClient({ escalateFirst: false });
  await H.__tsSymbolicRun(c14, sbody, {});
  ok('scimind ON: translator body system has SciMind', symTranslatorBody && /epistemic|humility|falsif/i.test(symTranslatorBody.system));
  delete process.env.TWO_STAGE_SCIMIND;
  symTranslatorBody = null; symDeciderCalls = 0; symJudgeCalls = 0; symTranslatorCalls = 0;
  const c14b = makeSymClient({ escalateFirst: false });
  await H.__tsSymbolicRun(c14b, sbody, {});
  ok('scimind OFF: translator body system has NO SciMind', symTranslatorBody && !/SciMind|Epistemic Humility|Incomplete Suggestion Protocol/i.test(symTranslatorBody.system));

  console.log(`\nRESULT: ${pass} pass, ${fail} fail`);
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error('HARNESS ERROR:', e); process.exit(2); });