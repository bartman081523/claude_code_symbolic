#!/usr/bin/env python3
"""gen_patch -- produce patches.json for the in-source 2-stage-thinking mod.

SOURCE patch (not bytecode).  Emits FIVE entries applied by ``patch.py`` to
module #0 (cli.js) inside the Bun ``.bun`` section:

  1. helper-insert   -- prepend ``__twoStage`` / ``__tsTrigger`` machinery.
  2-3. Hook A (base + beta ``Messages.create``)  -- the real main turn:
       claude-code calls ``client.beta.messages.create({stream:true}).withResponse()``,
       so Hook A handles BOTH stream (lazy ``.withResponse()`` proxy -> stage1
       then forwards stage 2's real SSE stream) and non-stream thinking turns.
  4-5. Hook B (base + beta ``MessageStream._createMessage``) -- the ``.stream()``
       path (safety net; claude-code does not currently use it).

claude-code's main streaming turn posts to ``/v1/messages?beta=true`` (the BASE
``Messages`` path), so BOTH base and beta hooks are patched to cover every
thinking turn.  All replacements are longer than their needles -> ``patch.py``
repoints module #0's contents into the freed bytecode region.

Recursion guard: a module-level ``__tsInStage1`` flag set around stage 1's
``create`` call (NOT a WeakSet-by-reference -- the base path's ``Hal(e)``
clones the body, so identity guards fail).  Stage models resolve by tier
(opus/sonnet/haiku) from OPUS_MODEL/SONNET_MODEL/HAIKU_MODEL.  SciMind 5.0
mandates inlined as the judge system preamble.  Run: ``python3 gen_patch.py``.
"""
import json
import base64

from scimind_mandates import SCIMIND_5_0_PREAMBLE


def b64(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


# --- the 2-stage helper, inserted once at the top of the IIFE ---------------

HELPER = (
    "var __tsInStage1=false;"
    "var __tsDbg=function(){var v=process.env.TWO_STAGE_DEBUG;return !!v&&v!=='0'&&v!=='false'&&v!=='no'&&v!=='off';};"
    "var __tsLog=function(m){if(!__tsDbg())return;try{require('fs').appendFileSync('/tmp/ts.log',m+'\\n')}catch(_){}};"
    "if(__tsDbg())try{require('fs').appendFileSync('/tmp/ts.log','LOAD two-stage helper\\n')}catch(_){}"
    "var __SCIMIND_PREAMBLE__=" + json.dumps(SCIMIND_5_0_PREAMBLE) + ";"
    "function __tsTrigger(b){"
    "try{"
    "var E=process.env.TWO_STAGE_ENABLED;"
    "if(E!==undefined&&E!==null&&(E==='0'||E==='false'||E==='no'||E==='off'))return false;"
    "if(__tsInStage1)return false;"
    "if(!b||!b.thinking||(b.thinking.type!=='enabled'&&b.thinking.type!=='adaptive'))return false;"
    "var M=process.env.TWO_STAGE_TRIGGER_MODELS;"
    "if(M){var ok=(''+M).split(',').map(function(s){return s.trim()}).filter(Boolean);"
    "if(ok.length&&ok.indexOf(b.model)<0)return false;}"
    "return true;"
    "}catch(_){return false;}}"
    "function __tsTierModel(tier,fallback){"
    "try{var key=tier==='opus'?'OPUS_MODEL':tier==='sonnet'?'SONNET_MODEL':'HAIKU_MODEL';"
    "var m=process.env[key];return m||fallback;}catch(_){return fallback;}}"
    "async function __twoStage(client,body){"
    "var s1tier=process.env.TWO_STAGE_STAGE1_TIER||'opus';"
    "var s2tier=process.env.TWO_STAGE_STAGE2_TIER||'sonnet';"
    "var B1=parseInt(process.env.TWO_STAGE_DECIDER_BUDGET||'2000',10);"
    "var B2=parseInt(process.env.TWO_STAGE_JUDGE_BUDGET||'16000',10);"
    "var s1model=__tsTierModel(s1tier,body&&body.model);"
    "var s2model=__tsTierModel(s2tier,body&&body.model);"
    "var s1msgs=((body&&body.messages)||[]).slice();"
    "var concise=process.env.TWO_STAGE_DECIDER_CONCISE;concise=(concise===undefined||concise===null||concise===''||concise==='1'||concise==='true'||concise==='yes'||concise==='on');"
    "if(concise)s1msgs.push({role:'user',content:'You are the DECIDER in a 2-stage pipeline. Think BRIEFLY (cheap first pass). Produce a CONCISE draft: key approach + tentative answer only. Do NOT write a full polished response or elaborate. A JUDGE will independently verify and write the final authoritative response.'});"
    "var s1body=Object.assign({},body,{model:s1model,stream:false,thinking:{type:'enabled',budget_tokens:B1},messages:s1msgs});"
    "var draft;"
    "__tsInStage1=true;"
    "try{draft=await client.create(s1body);}catch(err){return body;}finally{__tsInStage1=false;}"
    "var draftText='';"
    "try{var c=(draft&&draft.content)||[];"
    "for(var k=0;k<c.length;k++){var blk=c[k];"
    "if(blk.type==='text')draftText+=blk.text+'\\n';"
    "else if(blk.type==='tool_use')draftText+='[tool_call: '+blk.name+'('+JSON.stringify(blk.input)+')]\\n';"
    "else if(blk.type==='thinking')draftText+='[draft thinking: '+(blk.thinking||'').slice(0,800)+']\\n';}"
    "}catch(_){}"
    "if(draftText.length>32000)draftText=draftText.slice(0,32000)+'\\n...[truncated]';"
    "var instr='You are the JUDGE in a 2-stage reasoning pipeline. A DECIDER model has produced the DRAFT below. "
    "Apply epistemic humility: treat the draft as an incomplete suggestion, verify its claims, actively seek evidence "
    "it is WRONG (falsificationism), and emit the AUTHORITATIVE final response, including any tool calls. "
    "The user only sees YOUR response, so it must stand alone.\\n\\n=== DECIDER DRAFT (verify, do not trust) ===\\n'+draftText;"
    "var msgs=((body&&body.messages)||[]).slice();"
    "msgs.push({role:'user',content:instr});"
    "var sys=body&&body.system;"
    "var judgeSystem=sys?(__SCIMIND_PREAMBLE__+'\\n\\n'+(typeof sys==='string'?sys:JSON.stringify(sys))):__SCIMIND_PREAMBLE__;"
    "var judge=Object.assign({},body,{model:s2model,thinking:{type:'enabled',budget_tokens:B2},messages:msgs,system:judgeSystem});"
    "if(body&&body.tools)judge.tools=body.tools;"
    "if(process.env.TWO_STAGE_DEBUG)try{console.error('[two-stage] s1='+s1model+' s2='+s2model+' draftLen='+draftText.length);}catch(_){}"
    "return judge;}"
)

# --- needles (all verified unique, count=1) --------------------------------

IIFE_NEEDLE = "(function(exports, require, module, __filename, __dirname) {"

# Hook A -- non-streaming create post line.
HOOKA_BASE_NEEDLE = (
    'return this._client.post("/v1/messages?beta=true",{body:o,timeout:i??600000,'
    '...t,headers:ns([{...n?.toString()!=null?{"anthropic-beta":n?.toString()}:void 0},'
    's,t?.headers]),stream:r.stream??!1})'
)
HOOKA_BETA_NEEDLE = (
    'return this._client.post("/v1/messages",{body:e,timeout:r??600000,'
    '...t,headers:ns([n,t?.headers]),stream:e.stream??!1})'
)

# Hook B -- streaming _createMessage (the existing await e.create line).
HOOKB_BASE_NEEDLE = (
    'Ao(this,gme,"m",Eui).call(this);let{response:i,data:s}=await e.create('
    '{...t,stream:!0},{...r,signal:this.controller.signal}).withResponse()'
)
HOOKB_BETA_NEEDLE = (
    'Ao(this,yme,"m",Uui).call(this);let{response:i,data:s}=await e.create('
    '{...t,stream:!0},{...r,signal:this.controller.signal}).withResponse()'
)


def hookb_repl(field_call: str) -> str:
    """Hook B replacement: insert stage1 before the existing await e.create."""
    return (
        field_call + ';'
        "__tsLog('[hb] th='+JSON.stringify(t&&t.thinking)+' tr='+__tsTrigger(t)+' model='+((t&&t.model)||''));"
        "if(__tsTrigger(t)){t=await __twoStage(e,t)}"
        "let{response:i,data:s}=await e.create("
        "{...t,stream:!0},{...r,signal:this.controller.signal}).withResponse()"
    )


def hooka_repl(orig_post: str, body_var: str, stream_expr: str) -> str:
    """Hook A replacement: if triggered, run stage1 then stage2.

    claude-code consumes create({stream:true}) via ``.withResponse()`` ->
    ``{response,data}`` then ``for await of data``.  So the stream+triggered
    branch returns a LAZY PROXY whose ``.withResponse()`` first awaits stage 1,
    builds the judge body, then re-enters ``this.create(judgeBody)`` under the
    ``__tsInStage1`` guard (so the re-entered create does the original post and
    does NOT re-trigger) and delegates ``.withResponse()`` to that real stream.
    The !stream+triggered branch returns an async IIFE that awaits stage 1 and
    returns stage 2's Promise.  Non-triggered -> original post unchanged."""
    return (
        "__tsLog('[ha] stream='+(" + stream_expr + ")+' model='+((" + body_var + "&&" + body_var + ".model)||'')+' th='+JSON.stringify(" + body_var + "&&" + body_var + ".thinking)+' tr='+__tsTrigger(" + body_var + "));"
        "if(__tsTrigger(" + body_var + ")){var __s=this;"
        "if(" + stream_expr + "){return{withResponse:function(){return(async()=>{try{var b=await __twoStage(__s," + body_var + ");b.stream=true;__tsInStage1=true;try{var p=__s.create(b,t);var wr=await p.withResponse();return wr;}finally{__tsInStage1=false;}}catch(err){__tsLog('[ha] withResponse ERROR: '+err);throw err;}})();}};}"
        "return(async()=>{var b=await __twoStage(__s," + body_var + ");b.stream=false;__tsInStage1=true;try{return __s.create(b,t);}finally{__tsInStage1=false;}})();"
        "}" + orig_post
    )


IIFE_REPL = IIFE_NEEDLE + HELPER

HOOKA_BASE_REPL = hooka_repl(HOOKA_BASE_NEEDLE, "o", "r.stream")
HOOKA_BETA_REPL = hooka_repl(HOOKA_BETA_NEEDLE, "e", "e.stream")
HOOKB_BASE_REPL = hookb_repl('Ao(this,gme,"m",Eui).call(this)')
HOOKB_BETA_REPL = hookb_repl('Ao(this,yme,"m",Uui).call(this)')


def main() -> None:
    patches = [
        {"needle": b64(IIFE_NEEDLE), "replacement": b64(IIFE_REPL),
         "note": "insert __twoStage helper at IIFE opener"},
        {"needle": b64(HOOKA_BASE_NEEDLE), "replacement": b64(HOOKA_BASE_REPL),
         "note": "Hook A base: Messages.create 2-stage (stream+non-stream)"},
        {"needle": b64(HOOKA_BETA_NEEDLE), "replacement": b64(HOOKA_BETA_REPL),
         "note": "Hook A beta: BetaMessages.create 2-stage (stream+non-stream)"},
        {"needle": b64(HOOKB_BASE_NEEDLE), "replacement": b64(HOOKB_BASE_REPL),
         "note": "Hook B base: Messages._createMessage streaming 2-stage"},
        {"needle": b64(HOOKB_BETA_NEEDLE), "replacement": b64(HOOKB_BETA_REPL),
         "note": "Hook B beta: BetaMessages._createMessage streaming 2-stage"},
    ]
    with open("patches.json", "w") as f:
        json.dump(patches, f, indent=2)
    total = 0
    for p in patches:
        n = len(base64.b64decode(p["needle"]))
        r = len(base64.b64decode(p["replacement"]))
        total += r - n
        print(f"[+] {p['note']:50s} needle={n:5d} repl={r:5d} delta={r-n:+d}")
    print(f"[*] total module growth: {total} bytes")


if __name__ == "__main__":
    main()