#!/usr/bin/env node
/* test_hooka_fallback.js — regression for the Hook A stock-fallback path.
 *
 * Bug (2026-07-25, surfaced after the null-upstream crash fix made the
 * fallback actually reachable):
 *   return(await fp.withResponse()).then(function(fb){...});
 * `await` resolves the withResponse() Promise FIRST, then `.then` is called
 * on the resolved plain object {response,request_id,data} — which has no
 * `.then` -> "(await fp.withResponse()).then is not a function".
 *
 * Fix: `var fb=await fp.withResponse(); return{...fb...};` (await + direct
 * return, no .then on the already-awaited value).
 *
 * This test evals the REAL hooka_repl output string (not a replica), builds
 * the Messages.create function from it, and drives the fallback path with a
 * mock client whose decider create throws (so __symRun returns {data:null})
 * and whose stream:true create returns a fake MessageStream with a Promise-
 * returning withResponse(). Verifies the result shape + no .then crash.
 */
const vm = require('vm');
const { execSync } = require('child_process');

// --- pull HELPER + hooka_repl string from gen_patch.py ---
const helperJs = execSync(
  "python3 -c 'import gen_patch,json; print(json.dumps(gen_patch.HELPER))'",
  { cwd: __dirname, encoding: 'utf8' }
).trim();
const HELPER = JSON.parse(helperJs);
const hookaBase = execSync(
  "python3 -c 'import gen_patch,json; print(json.dumps(gen_patch.hooka_repl(gen_patch.HOOKA_BETA_NEEDLE,\"e\",\"e.stream\")))'",
  { cwd: __dirname, encoding: 'utf8' }
).trim();
const HOOKA = JSON.parse(hookaBase);

// --- env: symbolic ON, trace OFF (default), so the wrapper passes fb.data through ---
delete process.env.symbolic_thinking_trace;
process.env.symbolic_thinking = '1';
process.env.symbolic_thinking_mode = 'disabled';
process.env.symbolic_thinking_max_escalate = '0';
process.env.OPUS_MODEL = 'opus-mock';
process.env.SONNET_MODEL = 'sonnet-mock';
process.env.HAIKU_MODEL = 'haiku-mock';

const sandbox = { process, require, console, Buffer, AbortController, Symbol, Promise, JSON, Math, Date };
sandbox.global = sandbox;
vm.createContext(sandbox);
vm.runInContext(HELPER + '\n', sandbox);

// ns: merge an array of header objects (the stock create uses ns([...])).
sandbox.ns = function (arr) {
  let h = {};
  for (let x of arr) { if (x && typeof x === 'object') Object.assign(h, x); }
  return h;
};

let pass = 0, fail = 0;
function ok(name, cond, extra) {
  if (cond) { pass++; console.log('  PASS ' + name); }
  else { fail++; console.log('  FAIL ' + name + (extra ? '  // ' + extra : '')); }
}

// --- build the hooked create function from the REAL hooka string (BETA) ---
// Beta messages.create signature: e (body, has .stream), t (opts), r (timeout),
// n (header). `ns` (header-merge helper) is a module-scope closure in the real
// binary, so we expose it on the sandbox global rather than as a param — that
// way the recursive __s.create(body, t) call (which only passes 1-2 args) still
// resolves ns from the global. The engine calls beta.messages.create (the beta
// hook), and __symRun recurses through client.create with just the body.
const createFn = vm.runInContext(
  '__hookaCreate = function(e,t,r,n){' + HOOKA + '};',
  sandbox
);

(async () => {
try {

// --- mock client ---
// _client.post branches on opts.stream:
//   stream:false (decider/judge) -> throw, so __symRun returns {data:null} (stage error)
//   stream:true  (fallback)      -> fake MessageStream with Promise-returning withResponse()
function fakeStream() {
  const c = new AbortController();
  return {
    controller: c,
    [Symbol.asyncIterator]() {
      return { next: async () => ({ value: undefined, done: true }), return: async () => ({ value: undefined, done: true }) };
    }
  };
}
const client = {
  // `this` inside the hooked create is the `messages` object, which has both
  // .create (the hooked create itself, for recursion in the fallback) and
  // ._client.post (the stock post, used by orig_post when __symInStage guards
  // off the symbolic path).
  create: null, // wired below after createFn is built
  _client: {
    post(url, opts) {
      if (opts && opts.stream === false) {
        throw new Error('mock decider 500 (force stage error -> fallback)');
      }
      // stream:true fallback path — returns a MessageStream-like with withResponse()
      const fs = fakeStream();
      return {
        controller: fs.controller,
        withResponse() {
          // Real SDK: withResponse() returns Promise<{response,request_id,data}>.
          return Promise.resolve({ response: { status: 200 }, request_id: 'fb-req-1', data: fs });
        },
        [Symbol.asyncIterator]: fs[Symbol.asyncIterator].bind(fs),
      };
    },
  },
};
client.create = createFn; // recursive: __s.create inside the fallback re-enters the hooked create

// --- main body: thinking enabled -> triggers symbolic path ---
const body = {
  model: 'minimax-m3:cloud',
  stream: true,
  thinking: { type: 'enabled', budget_tokens: 1000 },
  max_tokens: 100,
  messages: [{ role: 'user', content: 'describe the image' }],
};
const ac = new AbortController();
const opts = { signal: ac.signal };

console.log('TEST 1: Hook A fallback path — await withResponse (no .then crash)');
let result;
let thrown = null;
try {
  // createFn returns {withResponse: fn} (symbolic path); await its withResponse()
  const ret = createFn.call(client, body, opts, undefined, undefined);
  ok('hooked create returns a withResponse-bearing object', ret && typeof ret.withResponse === 'function');
  result = await ret.withResponse();
  ok('withResponse() resolved to {response,request_id,data} (no .then crash)', !!result && 'response' in result && 'request_id' in result && 'data' in result,
     thrown ? ('threw: ' + thrown) : ('got: ' + JSON.stringify(Object.keys(result || {}))));
} catch (e) {
  thrown = e;
  ok('withResponse() resolved to {response,request_id,data} (no .then crash)', false, 'threw: ' + e.message);
}
if (!thrown && result) {
  ok('response.status is 200 (stock fallback reached the mock)', result.response && result.response.status === 200);
  ok('request_id is the fallback request id', result.request_id === 'fb-req-1');
  ok('data is present (the wrapped fallback stream)', !!result.data);
  ok('data has .controller (engine in-check will pass)', result.data && 'controller' in result.data);
  ok('data is async-iterable (SSy(qe) will work)', result.data && typeof result.data[Symbol.asyncIterator] === 'function');
}

console.log('\nTEST 2: regression — the OLD .then form would crash here');
// Confirm the fix is actually in the generated string (defensive: if someone
// reintroduces `(await fp.withResponse()).then`, this flags it).
ok('hooka string uses "var fb=await fp.withResponse()" (the fix)', /var fb=await fp\.withResponse\(\)/.test(HOOKA));
ok('hooka string does NOT use "(await fp.withResponse()).then" (the bug)', !/\(await fp\.withResponse\(\)\)\.then/.test(HOOKA));

console.log(`\nRESULT: ${pass} pass, ${fail} fail`);
process.exit(fail ? 1 : 0);
} catch (err) {
  console.error('HARNESS ERROR:', err && err.stack || err);
  process.exit(2);
}
})();