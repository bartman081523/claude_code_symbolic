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


def ascii_escape_js(src: str) -> str:
    """Escape every non-ASCII char in ``src`` to a JS ``\\uXXXX`` escape.

    Marker-reliability: raw non-ASCII literals (℧ U+2127, ⇾ U+21FE, …) in the
    patched cli.js source mojibake (double-encode) under Bun's source parse.
    Safe here because in this helper non-ASCII only ever occurs INSIDE JS
    string literals (all identifiers/keywords are ASCII); a ``\\uXXXX`` inside
    a string literal is the char, so the JS stays valid.  Pre-escaped
    ``\\uXXXX`` runs produced by ``json.dumps`` are ASCII text and pass through
    untouched (no double-escape).  Applied to the final ``HELPER``.
    """
    out = []
    for ch in src:
        o = ord(ch)
        out.append("\\u%04x" % o if o > 0x7F else ch)
    return "".join(out)


# --- the ℧ cognito-construct notation reference (cached system preamble) ---
# Compact-but-faithful summary of cognito-constructs/2.6/construct-template.txt.
# Embedded via json.dumps(ensure_ascii=True) -> pure-ASCII \u escapes in cli.js
# (marker-reliability lesson: raw non-ASCII literals mojibake under Bun parse).
SYMBOLIC_NOTATION = (
    "Communicate reasoning ONLY in this notation.\n"
    "℧ = brain object. ℧.ds = problem dataspace. "
    "℧.modules = [think, query, add_module, output, reflect, adaptive_update, consensus]. "
    "℧.state = quantum-like state (|I⟩ initial, |1⟩, ⊥ contradiction, ∅ empty, 0 null).\n"
    "Operators: ⇾ assign/flow, ↦ module def, ≔ define, ∘ compose, ∧ and, ¬ not, → implies, "
    "⨁ weighted combine. State kets: |…⟩.\n"
    "Modules:\n"
    ":: construct(℧, ds) ↦ { ℧.ds ⇾ ds, ℧.modules ⇾ [...], ℧.state ⇾ |1⟩ }  // initialize\n"
    ":: think(℧, q) ↦ { μₜ ≔ decode(q), ρ₊ ≔ retrieve(μₜ, ℧.ds), α₊ ≔ apply_logic(ρ₊), "
    "context_anchoring ≔ GCM(q), hypotheses ⇾ multi_think(context_anchoring), output ⇾ refine(hypotheses) }  // main reasoning\n"
    ":: query(℧, cn) ↦ { υₖ ≔ identify(cn), ρₑ ≔ process_query(υₖ), ℧ ⇾ update(℧, ρₑ) }  // inquiry\n"
    ":: add_module(℧, m) ↦ { validate(m), ℧.modules ⇾ append(℧.modules, m) }  // extend\n"
    ":: output(℧) ↦ { info ≔ gather(℧), formatted ⇾ format(info), deliver(formatted) }  // finalize/deliver\n"
    ":: reflect(℧) ↦ { diagnosis ⇾ self_assess(℧.ds, ℧.modules, ℧.state), "
    "tips ⇾ propose_refinements(diagnosis), ℧.ds ⇾ incorporate(℧.ds, tips) }  // self-assess\n"
    ":: adaptive_update(℧) ↦ { δₚ ≔ monitor_prediction_error(℧), "
    "if δₚ > θ then ℧ ⇾ restructure(hypotheses) }  // falsify on prediction error\n"
    ":: consensus(℧) ↦ { weights ⇔ credibility(hypotheses), "
    "chosen ⇾ argmax(⨁[weights ∘ hypotheses]) }  // weighted decision\n"
    "Wrap a full construct in <construct>…</construct>. Use <symbolic_reason>…</symbolic_reason> "
    "for reasoning steps. ESCALATION (judge → decider, when the draft is insufficient): "
    "emit exactly [ESCALATE]<one concise sentence: what the decider must provide, "
    "as ℧.reflect ⇾ escalate(℧, request)>[/ESCALATE] and nothing else."
)


# --- the 2-stage helper, inserted once at the top of the IIFE ---------------
# Written as a raw string so backslashes survive verbatim into the JS source
# (regex char classes, escape sequences).  Preamble spliced in via concat.

HELPER_HEAD = r"""var __tsInStage1=false;
var __tsDbg=function(){var v=process.env.TWO_STAGE_DEBUG;return !!v&&v!=='0'&&v!=='false'&&v!=='no'&&v!=='off';};
var __tsLog=function(m){if(!__tsDbg())return;try{require('fs').appendFileSync('/tmp/ts.log',m+'\n')}catch(_){}};
if(__tsDbg())try{require('fs').appendFileSync('/tmp/ts.log','LOAD two-stage helper v2 (escalation)\n')}catch(_){}
var __SCIMIND_PREAMBLE__=__PREAMBLE_PLACEHOLDER__;
var __tsSymNotation=__SYM_NOTATION_PLACEHOLDER__;
function __tsFlagOn(name){try{var v=process.env[name];if(v===undefined||v===null)return false;v=(''+v).trim().toLowerCase();return v==='1'||v==='true'||v==='yes'||v==='on';}catch(_){return false;}}
function __tsScimindOn(){return __tsFlagOn('TWO_STAGE_SCIMIND');}
function __tsScimindSys(sys){
if(!__tsScimindOn())return sys;
var p=__SCIMIND_PREAMBLE__;
if(!sys)return p;
return p+'\n\n'+(typeof sys==='string'?sys:JSON.stringify(sys));
}
function __tsTrigger(b){
try{
if(__tsInStage1)return false;
if(!b||!b.thinking||(b.thinking.type!=='enabled'&&b.thinking.type!=='adaptive'))return false;
if(!(__tsFlagOn('TWO_STAGE_ENABLED')||__tsFlagOn('TWO_STAGE_SYMBOLIC')))return false;
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
var judgeSystem=__tsScimindSys(sys);
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
function __tsSymEnabled(){try{var E=process.env.TWO_STAGE_SYMBOLIC;if(E===undefined||E===null||E==='0'||E==='false'||E==='no'||E==='off')return false;return true;}catch(_){return false;}}
function __tsOutputTier(){try{return process.env.TWO_STAGE_OUTPUT_TIER||'sonnet';}catch(_){return 'sonnet';}}
function __tsSymSystem(body){
var sys=body&&body.system;
var base=(__tsScimindOn()?(__SCIMIND_PREAMBLE__+'\n\n'):'')+'=== Cognito-Construct ℧ notation (symbolic reasoning language) ===\n'+__tsSymNotation;
return sys?(base+'\n\n'+(typeof sys==='string'?sys:JSON.stringify(sys))):base;
}
function __tsSymDeciderInstr(feedback){
var s='You are the DECIDER (stage 1) in a 3-stage SYMBOLIC reasoning pipeline. Reason ONLY in the ℧ cognito-construct notation given in the system preamble. Produce a CONCISE symbolic <construct> draft: problem decomposition (℧.ds), key hypotheses (think/multi_think), and a tentative solution. Do NOT write natural-language prose for the user — the JUDGE (stage 2) will verify your construct symbolically and a TRANSLATOR (stage 3) will render the final answer. Stay concise (cheap first pass).';
if(feedback)s+='\n\nThe JUDGE escalated (℧.reflect ⇾ escalate): your previous construct was insufficient — '+feedback+'. Revise the <construct> addressing this. Stay concise.';
return s;
}
function __tsSymJudgeInstr(construct,depth){
var MAX=__tsMaxEsc();var MO=__tsMOpen(),MC=__tsMClose();
var base='You are the JUDGE (stage 2) in a 3-stage SYMBOLIC reasoning pipeline. The DECIDER produced the <construct> below. Verify it IN THE ℧ NOTATION: run ℧.reflect (self-assess), ℧.adaptive_update (falsify hypotheses via prediction error), ℧.consensus (weigh credibility). The user never sees symbolic output — a TRANSLATOR renders the final answer from your converged <construct>, so your construct must be complete and correct. Emit the converged <construct> (do NOT emit '+MO+' unless escalating).';
var aware=' If the draft is INSUFFICIENT for a confident judgment — missing critical reasoning, unfalsified hypotheses, or over-claims — do NOT fabricate. Instead emit EXACTLY '+MO+'<one concise sentence: what the decider must provide, as a ℧.reflect ⇾ escalate(℧, request) construct>'+MC+' and nothing else. The decider rethinks and you re-judge.';
var cap=' Do NOT emit '+MO+' (no further escalation is allowed). Emit the best converged <construct> you can and flag any residual uncertainty inside it via ℧.reflect.';
var instr=(depth>=MAX)?(base+cap):(base+aware);
instr+='\n\n=== DECIDER CONSTRUCT (verify, do not trust) ===\n'+construct;
return instr;
}
function __tsSymOutputInstr(construct){
return 'You are the TRANSLATOR (stage 3, final output). The JUDGE converged on the <construct> below. Render it into the final natural-language answer for the user — complete, authoritative, standalone. The user sees ONLY your output. Include any tool calls the construct implies. Do NOT emit ℧ symbolic notation; emit the user-facing answer only.\n\n=== CONVERGED CONSTRUCT ===\n'+construct;
}
async function __tsSymDecider(client,body,feedback){
var s1tier=process.env.TWO_STAGE_STAGE1_TIER||'opus';
var B1=parseInt(process.env.TWO_STAGE_DECIDER_BUDGET||'2000',10);
var s1model=__tsTierModel(s1tier,body&&body.model);
var s1msgs=((body&&body.messages)||[]).slice();
s1msgs.push({role:'user',content:__tsSymDeciderInstr(feedback)});
var s1body=Object.assign({},body,{model:s1model,stream:false,thinking:{type:'enabled',budget_tokens:B1},messages:s1msgs,system:__tsSymSystem(body)});
var msg;
__tsInStage1=true;
try{msg=await client.create(s1body);}catch(err){__tsLog('[sym-decider] ERROR: '+err);return null;}finally{__tsInStage1=false;}
return __tsMessageText(msg);
}
function __tsSymJudgeBody(body,construct,depth){
var s2tier=process.env.TWO_STAGE_STAGE2_TIER||'sonnet';
var B2=parseInt(process.env.TWO_STAGE_JUDGE_BUDGET||'16000',10);
var s2model=__tsTierModel(s2tier,body&&body.model);
var msgs=((body&&body.messages)||[]).slice();
msgs.push({role:'user',content:__tsSymJudgeInstr(construct,depth)});
var jb=Object.assign({},body,{model:s2model,stream:false,thinking:{type:'enabled',budget_tokens:B2},messages:msgs,system:__tsSymSystem(body)});
if(body&&body.tools)jb.tools=body.tools;
return jb;
}
function __tsSymOutputBody(body,construct){
var otier=__tsOutputTier();
var B2out=parseInt(process.env.TWO_STAGE_OUTPUT_BUDGET||'8000',10);
var omodel=__tsTierModel(otier,body&&body.model);
var msgs=((body&&body.messages)||[]).slice();
msgs.push({role:'user',content:__tsSymOutputInstr(construct)});
var ob=Object.assign({},body,{model:omodel,stream:true,thinking:{type:'enabled',budget_tokens:B2out},messages:msgs,system:__tsSymSystem(body)});
if(body&&body.tools)ob.tools=body.tools;
return ob;
}
async function __tsSymbolicRun(client,body,t){
var MAX=__tsMaxEsc();var MO=__tsMOpen();
var construct=await __tsSymDecider(client,body,null);
if(construct===null)return null;
var finalConstruct='';
for(var depth=0;depth<=MAX;depth++){
var jb=__tsSymJudgeBody(body,construct,depth);
__tsInStage1=true;var msg;
try{msg=await client.create(jb);}catch(err){__tsLog('[sym-judge] ERROR: '+err);return null;}finally{__tsInStage1=false;}
var txt=__tsMessageText(msg);
var esc=(txt.indexOf(MO)===0)?__tsEscReq(txt):null;
__tsLog('[sym] depth='+depth+' esc='+(esc!==null)+' txt='+JSON.stringify(txt).slice(0,120));
if(esc!==null&&depth<MAX){construct=await __tsSymDecider(client,body,esc);if(construct===null)return null;continue;}
if(esc!==null)__tsStripMarker(msg);
finalConstruct=__tsMessageText(msg);
break;
}
if(!finalConstruct)return null;
__tsLog('[sym] final construct len='+finalConstruct.length);
var ob=__tsSymOutputBody(body,finalConstruct);
__tsInStage1=true;
try{var p=client.create(ob,t);var wr=await p.withResponse();__tsLog('[sym] translator stream ready');return {response:wr.response,request_id:wr.request_id,data:wr.data};}
catch(err){__tsLog('[sym] translator ERROR: '+err);return null;}
finally{__tsInStage1=false;}
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

HELPER = ascii_escape_js(HELPER_HEAD.replace(
    "__PREAMBLE_PLACEHOLDER__", json.dumps(SCIMIND_5_0_PREAMBLE)
).replace(
    "__SYM_NOTATION_PLACEHOLDER__", json.dumps(SYMBOLIC_NOTATION)
))

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
        # --- symbolic 3-stage: opt-in. decider+judge non-stream loop, translator
        #     streams; returns {response,request_id,data: raw SDK stream} so the
        #     engine's `!("controller" in Ti.value)` skip at cli.js:407773 treats it
        #     like the stock stream. On any stage failure -> stock stream fallback.
        "if(__tsSymEnabled()){__tsLog('[ha-sym] symbolic 3-stage');return{withResponse:function(){return(async()=>{"
        "try{var r=await __tsSymbolicRun(__s," + body_var + ",t);if(r)return r;}"
        "catch(err){__tsLog('[ha-sym] ERROR: '+err);}"
        "__tsLog('[ha-sym] fallback to stock stream');"
        "__tsInStage1=true;try{var fp=__s.create(Object.assign({}," + body_var + ",{stream:true}),t);return await fp.withResponse();}finally{__tsInStage1=false;}"
        "})();}};}"
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
        "}"
        # --- stock (non-triggered) path: apply SciMind preamble if opt-in.
        #     Surgical: when TWO_STAGE_SCIMIND is off, this guard is false and
        #     orig_post runs unchanged (byte-identical to untouched stock).
        "if(__tsScimindOn()){" + body_var + "=Object.assign({}," + body_var + ",{system:__tsScimindSys(" + body_var + "&&" + body_var + ".system)});}"
        + orig_post
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