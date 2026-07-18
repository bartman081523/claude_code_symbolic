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

  console.log(`\nRESULT: ${pass} pass, ${fail} fail`);
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error('HARNESS ERROR:', e); process.exit(2); });