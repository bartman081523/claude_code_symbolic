#!/usr/bin/env python3
"""gen_patch -- produce patches.json for the in-source 2-stage-thinking mod.

SOURCE patch (not bytecode).  Emits FIVE entries applied by ``patch.py`` to
module #0 (cli.js) inside the Bun ``.bun`` section:

  1. helper-insert   -- prepend the 2-stage machinery (decider/judge/inspector).
  2-3. Hook A (base + beta ``Messages.create``)  -- the real main turn:
       claude-code calls ``client.beta.messages.create({stream:true}).withResponse()``,
       so Hook A handles BOTH stream (lazy ``.withResponse()`` proxy -> stage1
       then forwards stage 2's real SSE stream THROUGH the inspector) and
       non-stream thinking turns.
  4-5. Hook B (base + beta ``MessageStream._createMessage``) -- the ``.stream()``
       path (safety net; claude-code does not currently use it).

v2 adds JUDGE->DECIDER ESCALATION with a Stream-Inspector: the judge streams real
SSE; the inspector buffers the first text deltas and (a) if the judge begins with
``<ESCALATE>...</ESCALATE>`` aborts the stream, runs a 2nd decider pass with the
judge's request, and re-streams the judge (depth-capped); (b) else releases the
buffered events and pipes the rest through 1:1 -- real SSE preserved, only 1x
judge in the common case.  Both roles are role-aware (the judge is self-aware
about its own constraints and may escalate when it cannot confidently judge).

claude-code's main streaming turn posts to ``/v1/messages?beta=true`` (the BASE
``Messages`` path), so BOTH base and beta hooks are patched to cover every
thinking turn.  All replacements are longer than their needles -> ``patch.py``
repoints module #0's contents into the freed bytecode region.

Recursion guard: a module-level ``__tsInStage1`` flag set around each re-entrant
``create`` (decider pass + judge pass + inspector re-create) -- NOT a
WeakSet-by-reference (the base path's ``Hal(e)`` clones the body, so identity
guards fail).  Stage models resolve by tier (opus/sonnet/haiku) from
OPUS_MODEL/SONNET_MODEL/HAIKU_MODEL.  SciMind 5.0 mandates inlined as the judge
system preamble.  Run: ``python3 gen_patch.py``.
"""
import json
import base64

from scimind_mandates import SCIMIND_5_0_PREAMBLE


def b64(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


# --- the 2-stage helper, inserted once at the top of the IIFE ---------------
# Written as a raw string so backslashes survive verbatim into the JS source
# (regex char classes, escape sequences).  Preamble spliced in via concat.

HELPER_HEAD = r"""var __tsInStage1=false;
var __tsDbg=function(){var v=process.env.TWO_STAGE_DEBUG;return !!v&&v!=='0'&&v!=='false'&&v!=='no'&&v!=='off';};
var __tsLog=function(m){if(!__tsDbg())return;try{require('fs').appendFileSync('/tmp/ts.log',m+'\n')}catch(_){}};
if(__tsDbg())try{require('fs').appendFileSync('/tmp/ts.log','LOAD two-stage helper v2 (escalation)\n')}catch(_){}
var __SCIMIND_PREAMBLE__=__PREAMBLE_PLACEHOLDER__;
function __tsTrigger(b){
try{
var E=process.env.TWO_STAGE_ENABLED;
if(E!==undefined&&E!==null&&(E==='0'||E==='false'||E==='no'||E==='off'))return false;
if(__tsInStage1)return false;
if(!b||!b.thinking||(b.thinking.type!=='enabled'&&b.thinking.type!=='adaptive'))return false;
var M=process.env.TWO_STAGE_TRIGGER_MODELS;
if(M){var ok=(''+M).split(',').map(function(s){return s.trim()}).filter(Boolean);
if(ok.length&&ok.indexOf(b.model)<0)return false;}
return true;
}catch(_){return false;}}
function __tsTierModel(tier,fallback){
try{var key=tier==='opus'?'OPUS_MODEL':tier==='sonnet'?'SONNET_MODEL':'HAIKU_MODEL';
var m=process.env[key];return m||fallback;}catch(_){return fallback;}}
function __tsMaxEsc(){try{var n=parseInt(process.env.TWO_STAGE_MAX_ESCALATE||'2',10);return (isNaN(n)||n<0)?2:n;}catch(_){return 2;}}
function __tsMOpen(){try{return process.env.TWO_STAGE_ESCALATE_OPEN||'[ESCALATE]';}catch(_){return '[ESCALATE]';}}
function __tsMClose(){try{return process.env.TWO_STAGE_ESCALATE_CLOSE||'[/ESCALATE]';}catch(_){return '[/ESCALATE]';}}
function __tsRe(s){return s.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');}
function __tsEscReq(text){try{var m=text.match(new RegExp(__tsRe(__tsMOpen())+'([\\s\\S]*?)'+__tsRe(__tsMClose())));return m?m[1].trim():null;}catch(_){return null;}}
async function __tsDecider(client,body,feedback){
var s1tier=process.env.TWO_STAGE_STAGE1_TIER||'opus';
var B1=parseInt(process.env.TWO_STAGE_DECIDER_BUDGET||'2000',10);
var s1model=__tsTierModel(s1tier,body&&body.model);
var s1msgs=((body&&body.messages)||[]).slice();
var concise=process.env.TWO_STAGE_DECIDER_CONCISE;
concise=(concise===undefined||concise===null||concise===''||concise==='1'||concise==='true'||concise==='yes'||concise==='on');
if(concise)s1msgs.push({role:'user',content:'You are the DECIDER in a 2-stage pipeline. Think BRIEFLY (cheap first pass). Produce a CONCISE draft: key approach + tentative answer only. Do NOT write a full polished response or elaborate. A JUDGE will independently verify and write the final authoritative response.'});
if(feedback)s1msgs.push({role:'user',content:'The JUDGE found your previous draft insufficient: '+feedback+'. Provide a revised, more complete draft addressing this. Stay concise.'});
var s1body=Object.assign({},body,{model:s1model,stream:false,thinking:{type:'enabled',budget_tokens:B1},messages:s1msgs});
var draft;
__tsInStage1=true;
try{draft=await client.create(s1body);}catch(err){__tsLog('[decider] ERROR: '+err);return null;}finally{__tsInStage1=false;}
var draftText='';
try{var c=(draft&&draft.content)||[];
for(var k=0;k<c.length;k++){var blk=c[k];
if(blk.type==='text')draftText+=blk.text+'\n';
else if(blk.type==='tool_use')draftText+='[tool_call: '+blk.name+'('+JSON.stringify(blk.input)+')]\n';
else if(blk.type==='thinking')draftText+='[draft thinking: '+(blk.thinking||'').slice(0,800)+']\n';}
}catch(_){}
if(draftText.length>32000)draftText=draftText.slice(0,32000)+'\n...[truncated]';
return draftText;
}
function __tsJudgeInstr(draftText,depth){
var MAX=__tsMaxEsc();
var MO=__tsMOpen(),MC=__tsMClose();
var base='You are the JUDGE in a 2-stage reasoning pipeline. A DECIDER model has produced the DRAFT below. Apply epistemic humility: treat the draft as an unverified suggestion, verify its claims, and actively seek evidence it is WRONG (falsificationism). Emit the AUTHORITATIVE final response, including any tool calls. The user only sees YOUR response, so it must stand alone.';
var aware=' Be self-aware about your own constraints: if the draft is INSUFFICIENT for a confident judgment — too little detail, missing critical information, OR it over-claims beyond what is justified — do NOT fabricate or guess. Instead, begin your response with EXACTLY '+MO+'<one concise sentence: what specific info/reasoning the decider must provide>'+MC+' and nothing else. The decider will rethink and you will re-judge.';
var cap=' Do NOT emit '+MO+' (no further escalation is allowed). Give the best possible answer you can and explicitly flag any residual uncertainty, including what information would be needed to judge confidently.';
var instr=(depth>=MAX)?(base+cap):(base+aware);
instr+='\n\n=== DECIDER DRAFT (verify, do not trust) ===\n'+draftText;
return instr;
}
function __tsJudgeBody(body,draftText,depth){
var s2tier=process.env.TWO_STAGE_STAGE2_TIER||'sonnet';
var B2=parseInt(process.env.TWO_STAGE_JUDGE_BUDGET||'16000',10);
var s2model=__tsTierModel(s2tier,body&&body.model);
var instr=__tsJudgeInstr(draftText||'',depth);
var msgs=((body&&body.messages)||[]).slice();
msgs.push({role:'user',content:instr});
var sys=body&&body.system;
var judgeSystem=sys?(__SCIMIND_PREAMBLE__+'\n\n'+(typeof sys==='string'?sys:JSON.stringify(sys))):__SCIMIND_PREAMBLE__;
var judge=Object.assign({},body,{model:s2model,thinking:{type:'enabled',budget_tokens:B2},messages:msgs,system:judgeSystem});
if(body&&body.tools)judge.tools=body.tools;
return judge;
}
async function __twoStage(client,body){
var draftText=await __tsDecider(client,body,null);
if(draftText===null)return body;
return __tsJudgeBody(body,draftText,0);
}
async function __tsEscalate(client,body,request,depth){
var draftText=await __tsDecider(client,body,request);
if(draftText===null)return body;
return __tsJudgeBody(body,draftText,depth);
}
function __tsMessageText(msg){
var txt='';
try{var c=(msg&&msg.content)||[];for(var k=0;k<c.length;k++){var blk=c[k];if(blk.type==='text')txt+=blk.text;}}catch(_){}
return txt;
}
function __tsStripMarker(msg){
try{var MO=__tsMOpen(),MC=__tsMClose();var c=(msg&&msg.content)||[];
for(var k=0;k<c.length;k++){var blk=c[k];if(blk.type==='text'){var t=blk.text;var i=t.indexOf(MO);if(i>=0){var j=t.indexOf(MC,i);blk.text=(j>=0)?(t.slice(0,i)+t.slice(j+MC.length)):t.slice(0,i);}}}
}catch(_){}
}
function __tsInspect(stream,ctx,depth){
var __g=(async function*(){
var buf=[],text='',mode=0;
var MO=__tsMOpen(),MC=__tsMClose(),MAX=__tsMaxEsc();
try{
for await(var ev of stream){
__tsLog('[esc-in] depth='+depth+' mode='+mode+' ev='+JSON.stringify(ev).slice(0,160));
if(mode===1){yield ev;continue;}
var isTD=ev&&ev.type==='content_block_delta'&&ev.delta&&ev.delta.type==='text_delta';
if(isTD){
text+=ev.delta.text;
__tsLog('[esc-d] depth='+depth+' MO='+JSON.stringify(MO)+'(len'+MO.length+') MC='+JSON.stringify(MC)+' td='+JSON.stringify(ev.delta.text).slice(0,50)+' | textsofar='+JSON.stringify(text.slice(0,40))+' len='+text.length+' pref='+(MO.indexOf(text)===0)+' starts='+(text.indexOf(MO)===0)+' hasclose='+(text.indexOf(MC)>=0)+' mode='+mode);
if(text.length<MO.length&&MO.indexOf(text)===0){continue;}
if(text.indexOf(MO)===0){if(text.indexOf(MC)>=0){mode=2;break;}continue;}
mode=1;for(var b=0;b<buf.length;b++)yield buf[b];yield ev;continue;
}
if(ev&&ev.type==='content_block_start'&&ev.content_block&&ev.content_block.type!=='text'&&ev.content_block.type!=='thinking'){
mode=1;for(var b=0;b<buf.length;b++)yield buf[b];yield ev;continue;
}
buf.push(ev);
}
__tsLog('[esc-end] depth='+depth+' mode='+mode+' buf='+buf.length+' textlen='+text.length);
}catch(e){__tsLog('[esc] inspector iter error: '+e);}
if(mode===1)return;
if(mode===0){for(var b=0;b<buf.length;b++)yield buf[b];return;}
if(mode===2&&depth<MAX){
var req=__tsEscReq(text);if(req===null)req='';
__tsLog('[esc] depth='+depth+' ESCALATED req='+req.slice(0,120));
var nb=await __tsEscalate(ctx.client,ctx.body,req,depth+1);
if(nb===ctx.body){__tsLog('[esc] decider r2 failed; passthrough buffered');for(var b2=0;b2<buf.length;b2++)yield buf[b2];yield {type:'content_block_delta',index:0,delta:{type:'text_delta',text:text}};return;}
nb.stream=true;
__tsInStage1=true;
try{var p2=ctx.client.create(nb,ctx.t);var wr2=await p2.withResponse();yield* __tsInspect(wr2.data,ctx,depth+1);}finally{__tsInStage1=false;}
return;
}
if(mode===2){for(var b3=0;b3<buf.length;b3++)yield buf[b3];yield {type:'content_block_delta',index:0,delta:{type:'text_delta',text:text}};}
})();
return new Proxy(__g,{has:function(t,k){return (k in t)||(k in stream);},get:function(t,k){
if(k===Symbol.asyncIterator)return t[Symbol.asyncIterator].bind(t);
var v=t[k];if(v!==undefined)return v;
try{var sv=stream[k];return sv;}catch(e){return undefined;}
}});
}
"""

HELPER = HELPER_HEAD.replace(
    "__PREAMBLE_PLACEHOLDER__", json.dumps(SCIMIND_5_0_PREAMBLE)
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
    """Hook B replacement: insert stage1 before the existing await e.create.

    NOTE: Hook B is the unused ``.stream()`` path (claude-code uses Hook A).
    Escalation is NOT wired here -- only the judge-body rewrite (v1 behaviour).
    Adding the inspector to Hook B's ``for await`` is invasive for no payoff
    since the path is dead for claude-code.  Documented limitation.
    """
    return (
        field_call + ';'
        "__tsLog('[hb] th='+JSON.stringify(t&&t.thinking)+' tr='+__tsTrigger(t)+' model='+((t&&t.model)||''));"
        "if(__tsTrigger(t)){t=await __twoStage(e,t)}"
        "let{response:i,data:s}=await e.create("
        "{...t,stream:!0},{...r,signal:this.controller.signal}).withResponse()"
    )


def hooka_repl(orig_post: str, body_var: str, stream_expr: str) -> str:
    """Hook A replacement: if triggered, run stage1 then stage2 (with escalation).

    claude-code consumes create({stream:true}) via ``.withResponse()`` ->
    ``{response,data}`` then ``for await of data``.

    - stream + triggered: returns a LAZY PROXY whose ``.withResponse()`` first
      awaits stage 1 (builds the judge body), re-enters ``this.create(judgeBody)``
      under ``__tsInStage1`` (so the re-entered create does the original post and
      does NOT re-trigger), and returns ``{response, request_id, data}`` where
      ``data`` is the judge stream wrapped in ``__tsInspect`` -- the inspector
      buffers the first text deltas, escalates on ``<ESCALATE>...</ESCALATE>``
      (abort + decider r2 + re-stream, depth-capped), else pipes the real SSE
      through 1:1.
    - non-stream + triggered: async IIFE that loops on the stage-2 ``Message```;
      if its text starts with the escalate marker and depth < MAX, run
      ``__tsEscalate`` and retry; at the cap, strip the marker and return.
    - non-triggered: original post unchanged.
    """
    return (
        "__tsLog('[ha] stream='+(" + stream_expr + ")+' model='+((" + body_var + "&&" + body_var + ".model)||'')+' th='+JSON.stringify(" + body_var + "&&" + body_var + ".thinking)+' tr='+__tsTrigger(" + body_var + "));"
        "if(__tsTrigger(" + body_var + ")){var __s=this;"
        # --- stream + triggered: lazy .withResponse() proxy, data wrapped in inspector
        "if(" + stream_expr + "){return{withResponse:function(){return(async()=>{try{"
        "var b=await __twoStage(__s," + body_var + ");b.stream=true;__tsInStage1=true;"
        "try{var p=__s.create(b,t);var wr=await p.withResponse();"
        "return{response:wr.response,request_id:wr.request_id,data:__tsInspect(wr.data,{client:__s,body:" + body_var + ",t:t},0)};"
        "}finally{__tsInStage1=false;}"
        "}catch(err){__tsLog('[ha] withResponse ERROR: '+err);throw err;}})();}};}"
        # --- non-stream + triggered: loop on the Message with escalation
        "return(async()=>{var b=await __twoStage(__s," + body_var + ");b.stream=false;"
        "var MAX=__tsMaxEsc();"
        "for(var depth=0;depth<=MAX;depth++){"
        "__tsInStage1=true;var msg;try{msg=await __s.create(b,t);}finally{__tsInStage1=false;}"
        "var txt=__tsMessageText(msg);var MO=__tsMOpen();"
        "var esc=(txt.indexOf(MO)===0)?__tsEscReq(txt):null;"
        "if(esc!==null&&depth<MAX){b=await __tsEscalate(__s," + body_var + ",esc,depth+1);b.stream=false;continue;}"
        "if(esc!==null)__tsStripMarker(msg);"
        "return msg;"
        "}})();"
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
         "note": "insert 2-stage helper v2 (decider/judge/inspector) at IIFE opener"},
        {"needle": b64(HOOKA_BASE_NEEDLE), "replacement": b64(HOOKA_BASE_REPL),
         "note": "Hook A base: Messages.create 2-stage + escalation inspector (stream+non-stream)"},
        {"needle": b64(HOOKA_BETA_NEEDLE), "replacement": b64(HOOKA_BETA_REPL),
         "note": "Hook A beta: BetaMessages.create 2-stage + escalation inspector (stream+non-stream)"},
        {"needle": b64(HOOKB_BASE_NEEDLE), "replacement": b64(HOOKB_BASE_REPL),
         "note": "Hook B base: Messages._createMessage streaming 2-stage (no escalation; dead path)"},
        {"needle": b64(HOOKB_BETA_NEEDLE), "replacement": b64(HOOKB_BETA_REPL),
         "note": "Hook B beta: BetaMessages._createMessage streaming 2-stage (no escalation; dead path)"},
    ]
    with open("patches.json", "w") as f:
        json.dump(patches, f, indent=2)
    total = 0
    for p in patches:
        n = len(base64.b64decode(p["needle"]))
        r = len(base64.b64decode(p["replacement"]))
        total += r - n
        print(f"[+] {p['note']:70s} needle={n:5d} repl={r:5d} delta={r-n:+d}")
    print(f"[*] total module growth: {total} bytes")


if __name__ == "__main__":
    main()