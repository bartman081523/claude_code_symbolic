#!/usr/bin/env node
/* test_thinking_wrapper.js — TDD harness for __symBuildThinkingWrapper.
 *
 * Covers the surgical fixes from 2026-07-25:
 *   1) The async generator's terminal return value MUST carry .controller, so
 *      the engine's "controller" in Ti.value check (cli.pretty.js:407773) does
 *      not throw "Ti.value is not an Object (evaluating 'controller' in Ti.value)".
 *   2) Upstream content_* events MUST have their index remapped so the engine's
 *      reducer (cli.pretty.js:19391, r.content.at(t.index)) does not update our
 *      synthetic thinking block (index 0) with the upstream's text deltas
 *      (also index 0).
 *   3) The upstream's first event (its message_start) MUST be dropped so the
 *      engine's reducer (cli.pretty.js:19374) does not throw "Unexpected event
 *      order, got message_start before message_stop".
 *   4) Translator-emitted thinking blocks (deep mode) MUST be suppressed by
 *      index (the trace is already a summary; don't double-emit).
 *   5) Non-content events (message_delta, message_stop) pass through verbatim.
 *   6) Abort path MUST also return a controller-bearing value.
 *   7) __symBuildThinkingWrapper is OFF by default (returns upstream unchanged
 *      unless symbolic_thinking_trace=1).
 *   8) Trace formatter, chunker, firstLine, blockInfo are well-formed.
 *
 * Loads the HELPER from gen_patch.py via subprocess (same trick as
 * test_escalation.js), exposes process/require in a vm sandbox, and drives
 * the helper with mocked SDK streams.
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

// Minimal env: helper off by default; we'll flip symbolic_thinking_trace per test.
delete process.env.symbolic_thinking_debug;
delete process.env.symbolic_thinking_trace;
delete process.env.symbolic_thinking;
process.env.OPUS_MODEL = 'opus-mock';
process.env.SONNET_MODEL = 'sonnet-mock';
process.env.HAIKU_MODEL = 'haiku-mock';

const sandbox = { process, require, console, Buffer, AbortController };
sandbox.global = sandbox;
vm.createContext(sandbox);
vm.runInContext(HELPER + '\n', sandbox);
const H = sandbox;

let pass = 0, fail = 0;
function ok(name, cond, extra) {
  if (cond) {
    pass++;
    console.log('  PASS ' + name);
  } else {
    fail++;
    console.log('  FAIL ' + name + (extra ? '  // ' + extra : ''));
  }
}

// --- SSE event builders (Anthropic /v1/messages shape) ---
const ev = (type, extra) => Object.assign({ type }, extra);
const msgStart = () => ev('message_start', { message: { id: 'msg_upstream', type: 'message', role: 'assistant', model: 'opus-mock', content: [], stop_reason: null, stop_sequence: null, usage: { input_tokens: 0, output_tokens: 0 } } });
const msgDelta = (o) => ev('message_delta', { delta: { stop_reason: 'end_turn' }, usage: { output_tokens: o } });
const msgStop = () => ev('message_stop', {});
const blkStart = (index, cbtype, extra) => ev('content_block_start', { index, content_block: Object.assign({ type: cbtype }, extra || {}) });
const blkStop = (index) => ev('content_block_stop', { index });
const thinkingDelta = (i, t) => ev('content_block_delta', { index: i, delta: { type: 'thinking_delta', thinking: t } });
const textDelta = (i, t) => ev('content_block_delta', { index: i, delta: { type: 'text_delta', text: t } });
const inputJsonDelta = (i, t) => ev('content_block_delta', { index: i, delta: { type: 'input_json_delta', partial_json: t } });

// --- Mock SDK v7 MessageStream: a fake async iterator with .controller ---
function mockStream(events, opts = {}) {
  const it = {
    [Symbol.asyncIterator]() {
      let k = 0;
      return {
        next: async () => {
          if (opts.throwAt != null && k === opts.throwAt) throw new Error('upstream mid-stream error');
          if (k < events.length) return { value: events[k++], done: false };
          return { value: undefined, done: true };
        },
        return: async () => ({ value: undefined, done: true }),
      };
    },
  };
  // mimic SDK v7: a .controller field with signal/abort
  it.controller = { signal: opts.signal || new AbortController().signal, abort: () => { opts.aborted = true; } };
  return it;
}

// --- Drive the wrapper, collect all yielded events, and capture the terminal return value ---
async function drive(wrapper) {
  const events = [];
  let terminal;
  const it = wrapper[Symbol.asyncIterator]();
  while (true) {
    const r = await it.next();
    if (r.done) { terminal = r.value; break; }
    events.push(r.value);
  }
  return { events, terminal };
}

// --- Build a minimal trace record ---
function mkDeciderRecord(extra = {}) {
  return Object.assign({
    stage: 'decider', depth: 0, role: 'decider',
    t0: 1700000000000, dt: 1200, ok: true,
    construct: '<construct>answer=42</construct>',
    model: 'opus-mock',
    escalateRequest: null,
    blockInfo: { text: 24, thinking: 0, tool: 0, other: 0, thinkText: 0 },
  }, extra);
}
function mkJudgeRecord(extra = {}) {
  return Object.assign({
    stage: 'judge', depth: 0, role: 'judge',
    t0: 1700000001500, dt: 1500, ok: true,
    construct: '<construct>verified=42</construct>',
    model: 'sonnet-mock',
    escalateRequest: null,
    blockInfo: { text: 28, thinking: 0, tool: 0, other: 0, thinkText: 0 },
  }, extra);
}
function mkTranslatorRecord(extra = {}) {
  return Object.assign({
    stage: 'translator-init', depth: 0, role: 'translator',
    t0: 1700000003000, dt: 0, ok: true,
    construct: '', model: 'sonnet-mock',
    escalateRequest: null,
    blockInfo: { text: 0, thinking: 0, tool: 0, other: 0, thinkText: 0 },
  }, extra);
}
const TRACE = [mkDeciderRecord(), mkJudgeRecord(), mkTranslatorRecord()];

// =================================================================
// THE PRIMARY REGRESSION: terminal value must carry .controller
// =================================================================
// Drive everything inside a single async IIFE so top-level await is
// not required and node treats this as CJS.
(async () => {
try {
console.log('TEST 1: __symBuildThinkingWrapper terminal value has .controller (regression for "Ti.value is not an Object")');
process.env.symbolic_thinking_trace = '1';
{
  const upstream = mockStream([
    msgStart(),
    blkStart(0, 'text', { text: '' }),
    textDelta(0, 'hello'),
    blkStop(0),
    msgDelta(5),
    msgStop(),
  ]);
  const wrap = H.__symBuildThinkingWrapper(TRACE, upstream, null);
  ok('wrapper is not the same object as upstream', wrap !== upstream);
  const { events, terminal } = await drive(wrap);
  ok('terminal is defined (was undefined before fix -> crash)', terminal !== undefined, 'terminal was: ' + JSON.stringify(terminal));
  ok('terminal is the upstream stream (has .controller)', terminal === upstream, 'terminal is: ' + typeof terminal);
  ok('terminal has .controller', terminal && 'controller' in terminal, 'terminal.controller=' + JSON.stringify(terminal && terminal.controller));
  ok('terminal.controller has .signal', terminal && terminal.controller && 'signal' in terminal.controller);
  ok('terminal.controller has .abort()', terminal && terminal.controller && typeof terminal.controller.abort === 'function');
}

// =================================================================
// THE OTHER PRIMARY REGRESSION: index remap so upstream index 0
// (text) does not collide with our injected thinking at index 0
// =================================================================
console.log('\nTEST 2: upstream content_block_start index 0 is remapped to local 1 (no collision with our thinking)');
{
  const upstream = mockStream([
    msgStart(),
    blkStart(0, 'text', { text: '' }),       // upstream's text at index 0 -> MUST become local 1
    textDelta(0, 'world'),                    // -> remapped to 1
    blkStop(0),                               // -> remapped to 1
    msgDelta(2),
    msgStop(),
  ]);
  const wrap = H.__symBuildThinkingWrapper(TRACE, upstream, null);
  const { events } = await drive(wrap);
  // Find upstream text block's content_block_start (the one after our synthetic thinking)
  const ourThinking = events.filter(e => e.type === 'content_block_start' && e.content_block && e.content_block.type === 'thinking');
  ok('exactly one thinking block injected', ourThinking.length === 1);
  ok('thinking block uses index 0', ourThinking[0] && ourThinking[0].index === 0);
  // Upstream's text start is the second content_block_start; it should be remapped to 1
  const allStarts = events.filter(e => e.type === 'content_block_start');
  ok('two content_block_start events total (thinking + text)', allStarts.length === 2);
  const textStart = allStarts[1];
  ok('upstream text start remapped to index 1', textStart.index === 1, 'got index=' + textStart.index);
  // Verify all upstream events for that block use the remapped index 1
  const textDeltas = events.filter(e => e.type === 'content_block_delta' && e.delta.type === 'text_delta');
  ok('upstream text_delta uses remapped index 1', textDeltas.every(e => e.index === 1), 'indices: ' + textDeltas.map(e => e.index).join(','));
  ok('all upstream text_deltas/stops use remapped index 1',
     textDeltas.every(e => e.index === 1) &&
     events.filter(e => e.type === 'content_block_stop').filter(e => e.index === 1).length === 1,
     'stops by index: ' + JSON.stringify(events.filter(e => e.type === 'content_block_stop').map(e => e.index)));
}

// =================================================================
// Drop upstream message_start so the engine reducer is happy
// =================================================================
console.log('\nTEST 3: upstream message_start is dropped (reducer expects exactly one)');
{
  const upstream = mockStream([
    msgStart(),
    blkStart(0, 'text', { text: '' }),
    textDelta(0, 'ok'),
    blkStop(0),
    msgStop(),
  ]);
  const wrap = H.__symBuildThinkingWrapper(TRACE, upstream, null);
  const { events } = await drive(wrap);
  const msgStarts = events.filter(e => e.type === 'message_start');
  ok('exactly one message_start yielded (our synthetic only)', msgStarts.length === 1);
  ok('the message_start is ours (id starts with msg_sym_)', msgStarts[0].message.id.startsWith('msg_sym_'));
}

// =================================================================
// Deep mode: upstream emits its OWN thinking block; we suppress by index
// =================================================================
console.log('\nTEST 4: deep mode upstream thinking block is suppressed (no duplicate trace)');
{
  const upstream = mockStream([
    msgStart(),
    blkStart(0, 'thinking', { thinking: '' }),   // upstream's own thinking
    thinkingDelta(0, 'lots of upstream thinking here'),
    blkStop(0),
    blkStart(1, 'text', { text: '' }),
    textDelta(1, 'final'),
    blkStop(1),
    msgStop(),
  ]);
  const wrap = H.__symBuildThinkingWrapper(TRACE, upstream, null);
  const { events } = await drive(wrap);
  // After remap: our thinking=0, upstream thinking should be suppressed,
  // upstream text=1 should be remapped to 1 (since 0 was the suppressed index)
  // Actually: our thinking takes slot 0; upstream's first start (thinking) goes
  // to local 1 BUT it's a thinking block so it's suppressed; then upstream's
  // text start goes to local 2.
  const thinkingBlocks = events.filter(e => e.type === 'content_block_start' && e.content_block && e.content_block.type === 'thinking');
  ok('only OUR thinking block is in the stream (upstream one suppressed)', thinkingBlocks.length === 1);
  // Verify no thinking_delta leaked through for the upstream's thinking
  const thinkingTexts = events.filter(e => e.type === 'content_block_delta' && e.delta.type === 'thinking_delta');
  ok('all thinking_deltas are OURS (from the trace)', thinkingTexts.every(e => e.index === 0));
}

// =================================================================
// Pass-through: message_delta, message_stop, ping
// =================================================================
console.log('\nTEST 5: non-content events pass through verbatim (no remap needed)');
{
  const upstream = mockStream([
    msgStart(),
    blkStart(0, 'text', { text: '' }),
    textDelta(0, 'a'),
    blkStop(0),
    msgDelta(7),
    msgStop(),
  ]);
  const wrap = H.__symBuildThinkingWrapper(TRACE, upstream, null);
  const { events } = await drive(wrap);
  const md = events.find(e => e.type === 'message_delta');
  ok('message_delta passed through', md && md.delta && md.delta.stop_reason === 'end_turn');
  const ms = events.find(e => e.type === 'message_stop');
  ok('message_stop passed through', !!ms);
}

// =================================================================
// Abort path: aborted during thinking_delta yields must also return
// a controller-bearing terminal value
// =================================================================
console.log('\nTEST 6: abort during thinking chunk returns controller-bearing terminal value');
{
  const ac = new AbortController();
  const upstream = mockStream([
    msgStart(),
    blkStart(0, 'text', { text: '' }),
    textDelta(0, 'never reached'),
    blkStop(0),
    msgStop(),
  ], { signal: ac.signal });
  const wrap = H.__symBuildThinkingWrapper(TRACE, upstream, ac.signal);
  const it = wrap[Symbol.asyncIterator]();
  // Get a few events then abort
  await it.next();  // message_start
  await it.next();  // content_block_start (thinking)
  // The chunks loop checks `if(aborted)return upstream;` so we need to call
  // controller.abort() before iterating further. Our wrapper exposes
  // wrap.controller.abort.
  ok('wrap.controller.abort is a function', typeof wrap.controller.abort === 'function');
  wrap.controller.abort();
  let terminal;
  while (true) {
    const r = await it.next();
    if (r.done) { terminal = r.value; break; }
  }
  ok('abort terminal value is defined', terminal !== undefined);
  ok('abort terminal value has .controller (or is upstream)', terminal && 'controller' in terminal);
}

// =================================================================
// OFF by default
// =================================================================
console.log('\nTEST 7: __symBuildThinkingWrapper is OFF unless symbolic_thinking_trace=1');
delete process.env.symbolic_thinking_trace;
{
  const upstream = mockStream([msgStart(), msgStop()]);
  const wrap = H.__symBuildThinkingWrapper(TRACE, upstream, null);
  ok('returns upstream unchanged when trace flag off', wrap === upstream);
}
// Even with a trace, returns upstream if trace is empty
process.env.symbolic_thinking_trace = '1';
{
  const upstream = mockStream([msgStart(), msgStop()]);
  const wrap = H.__symBuildThinkingWrapper([], upstream, null);
  ok('returns upstream unchanged when trace is empty', wrap === upstream);
}
// Returns upstream if any stage had rich thinking (deep mode duplicate avoidance)
{
  const deepTrace = [mkDeciderRecord({ blockInfo: { text: 0, thinking: 1, tool: 0, other: 0, thinkText: 50 } })];
  const upstream = mockStream([msgStart(), msgStop()]);
  const wrap = H.__symBuildThinkingWrapper(deepTrace, upstream, null);
  ok('returns upstream when deep-mode already has thinking', wrap === upstream);
}

// =================================================================
// Trace formatter, chunker, firstLine, blockInfo
// =================================================================
console.log('\nTEST 8: __symFormatTrace produces expected shape');
{
  const out = H.__symFormatTrace(TRACE);
  ok('header line present', /^cc-symbolic reasoning trace \(3-stage\)/m.test(out));
  ok('DECIDER section present', /\[\+0\.00s\] DECIDER/.test(out));
  ok('JUDGE section present', /\[\+/.test(out) && /JUDGE depth=0/.test(out));
  ok('TRANSLATOR section present', /TRANSLATOR/.test(out));
  ok('model name printed for decider', /model=opus-mock/.test(out));
}

console.log('\nTEST 9: __symChunkText splits at whitespace');
{
  const chunks = H.__symChunkText('hello world this is a long string that needs to be chunked into pieces', 20);
  ok('chunks is an array', Array.isArray(chunks));
  ok('all chunks <= 20 chars (except trailing)', chunks.every(c => c.length <= 20));
  ok('reassembled text equals input (modulo whitespace)', chunks.join(' ').replace(/\s+/g, ' ').trim() === 'hello world this is a long string that needs to be chunked into pieces');
  ok('empty input -> empty array', H.__symChunkText('', 10).length === 0);
}

console.log('\nTEST 10: __symFirstLine collapses whitespace and truncates');
{
  ok('first line of multi-line (whitespace-collapsed)', H.__symFirstLine('line1\nline2\nline3', 100) === 'line1 line2 line3');
  ok('truncates to max-1 + ellipsis', H.__symFirstLine('a'.repeat(200), 10).length === 10 && H.__symFirstLine('a'.repeat(200), 10).endsWith('…'));
  ok('empty -> (empty)', H.__symFirstLine('', 100) === '(empty)');
  ok('whitespace collapsed', H.__symFirstLine('a   b\n\tc', 100) === 'a b c');
}

console.log('\nTEST 11: __symBlockInfo counts blocks correctly');
{
  const bi = H.__symBlockInfo({ content: [
    { type: 'text', text: 'hi' },
    { type: 'text', text: 'world' },
    { type: 'thinking', thinking: 'thought' },
    { type: 'tool_use', id: 't1', name: 'Read', input: {} },
    { type: 'redacted_thinking' },
  ]});
  ok('text count is 7 chars (hi=2 + world=5)', bi.text === 7);
  ok('thinking count is 1 block', bi.thinking === 1);
  ok('thinking text count is 7 chars', bi.thinkText === 7);
  ok('tool count is 1', bi.tool === 1);
  ok('other count is 1 (redacted_thinking)', bi.other === 1);
  ok('null msg -> zero counts', H.__symBlockInfo(null).text === 0);
  ok('empty content -> zero counts', H.__symBlockInfo({ content: [] }).text === 0);
}

// =================================================================
// Hard regression: simulate the engine's loop at cli.pretty.js:407773
// and verify it does NOT throw "Ti.value is not an Object"
// =================================================================
console.log('\nTEST 12: HARD regression — engine-loop simulation does not throw');
process.env.symbolic_thinking_trace = '1';
{
  const upstream = mockStream([
    msgStart(),
    blkStart(0, 'text', { text: '' }),
    textDelta(0, 'answer'),
    blkStop(0),
    msgDelta(2),
    msgStop(),
  ]);
  const wrap = H.__symBuildThinkingWrapper(TRACE, upstream, null);
  // Re-implement the engine's consumer loop shape exactly:
  //   do { if (Ti = await rn.next(), !("controller" in Ti.value)) yield Ti.value; } while (!Ti.done);
  async function engineLoop() {
    const it = wrap[Symbol.asyncIterator]();
    const out = [];
    let Ti;
    do {
      Ti = await it.next();
      if (!('controller' in Ti.value)) out.push(Ti.value);  // <-- this is the line that was throwing
    } while (!Ti.done);
    return out;
  }
  let thrown = null;
  let collected = [];
  try {
    collected = await engineLoop();
    ok('engine loop completed without throwing', true);
  } catch (e) {
    thrown = e;
    ok('engine loop completed without throwing', false, 'threw: ' + e.message);
  }
  ok('engine loop yielded at least one event', collected.length > 0);
  ok('engine loop yielded the upstream text_delta', collected.some(e => e.type === 'content_block_delta' && e.delta && e.delta.type === 'text_delta'));
}

// =================================================================
// Hard regression: index collision would corrupt the engine's
// r.content array (thinking would absorb text_delta updates).
// We simulate the reducer at cli.pretty.js:19371-19456 and verify
// the final message content is [thinking, text] in that order with
// text actually containing the text.
// =================================================================
console.log('\nTEST 13: HARD regression — engine reducer simulation preserves text content');
{
  const upstream = mockStream([
    msgStart(),
    blkStart(0, 'text', { text: '' }),
    textDelta(0, 'A'),
    textDelta(0, 'B'),
    blkStop(0),
    msgStop(),
  ]);
  const wrap = H.__symBuildThinkingWrapper(TRACE, upstream, null);
  // Mimic the reducer: r is the message object built from message_start
  // then content_block_start pushes (arrival order), content_block_delta
  // updates r.content.at(t.index).
  let r = null;
  const it = wrap[Symbol.asyncIterator]();
  let Ti;
  do {
    Ti = await it.next();
    const ev0 = Ti.value;
    if (!ev0) continue;
    if (ev0.type === 'message_start') r = ev0.message;
    else if (ev0.type === 'content_block_start') r.content.push(ev0.content_block);
    else if (ev0.type === 'content_block_delta' && ev0.delta.type === 'text_delta') {
      const n = r.content.at(ev0.index);
      r.content[ev0.index] = Object.assign({}, n, { text: (n.text || '') + ev0.delta.text });
    }
  } while (!Ti.done);
  ok('reducer sees 2 content blocks (thinking + text)', r.content.length === 2);
  ok('block 0 is thinking', r.content[0] && r.content[0].type === 'thinking');
  ok('block 1 is text', r.content[1] && r.content[1].type === 'text');
  ok('text content is "AB" (not corrupted by index collision)', r.content[1] && r.content[1].text === 'AB',
     'actual: ' + JSON.stringify(r.content.map(b => ({ type: b.type, text: b.text, thinking: b.thinking }))));
}

console.log(`\nRESULT: ${pass} pass, ${fail} fail`);
process.exit(fail ? 1 : 0);
} catch (err) {
  console.error('HARNESS ERROR:', err);
  process.exit(2);
}
})();
