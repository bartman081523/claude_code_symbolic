#!/usr/bin/env python3
"""gen_patch (symbolic-only) -- produce patches.json for the in-source
SYMBOLIC-THINKING mod.

A single opt-in extension on top of stock claude-code:
  - env gate ``symbolic_thinking=1``  (off -> byte-identical stock; surgical)
  - 3-stage symbolic channel: DECIDER (non-stream) -> JUDGE (non-stream,
    single pass by default) -> TRANSLATOR (stream, emits tool_use).
  - all stages exchange a symbolic ``<construct>`` in the cached ℧ notation
    preamble (system-level, prompt-cached); the user/claude-code only ever
    sees the TRANSLATOR's natural-language stream + tool calls.

PERF (the reason this build exists): on backends that ignore ``thinking``
``budget_tokens`` (Ollama + minimax-m3), extended thinking on every non-stream
stage is uncapped (~60-90s/stage).  By default this build therefore OMITS the
``thinking`` field on all three stages (``symbolic_thinking_deep=0``) so each
non-stream stage returns in seconds; set ``symbolic_thinking_deep=1`` to restore
``thinking:{type:'enabled',budget_tokens:N}`` on all stages (the deep-but-slow
mode).  Judge escalation is off by default (``symbolic_thinking_max_escalate=0``)
so the common path is decider + 1 judge + translator = 3 fast round-trips.

Patches applied by ``patch.py`` to module #0 (cli.js) inside the Bun ``.bun``:
  1. helper-insert -- prepend the symbolic machinery at the IIFE opener.
  2-3. Hook A (base + beta ``Messages.create``) -- claude-code's real main turn
       posts to ``/v1/messages?beta=true`` (base) and ``/v1/messages`` (beta).
       Hook A returns a lazy ``.withResponse()`` whose body runs the symbolic
       3-stage and returns ``{response, request_id, data: <raw SDK stream>}``;
       the engine's ``!("controller" in Ti.value)`` skip at cli.js:407773 treats
       the raw SDK stream like the stock stream (no Proxy, no inspector).
Hook B (the unused ``.stream()`` path) is left STOCK -- not patched -- so there
are no dangling refs to the (removed) v2 inspector.

Run: ``python3 gen_patch.py`` then ``python3 patch.py claude.orig out/claude.patched patches.json``.
"""
import json
import base64


def b64(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def ascii_escape_js(src: str) -> str:
    """Escape every non-ASCII char in ``src`` to a JS ``\\uXXXX`` escape.

    Marker-reliability: raw non-ASCII literals (℧ U+2127, ⇾ U+21FE, …) in the
    patched cli.js source mojibake under Bun's source parse.  Safe here because
    non-ASCII only occurs INSIDE JS string literals; ``\\uXXXX`` inside a string
    literal is the char, so the JS stays valid.
    """
    out = []
    for ch in src:
        o = ord(ch)
        out.append("\\u%04x" % o if o > 0x7F else ch)
    return "".join(out)


# --- the ℧ cognito-construct notation reference (cached system preamble) ---
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


# --- the symbolic helper, inserted once at the top of the IIFE ---------------
HELPER_HEAD = r"""var __symInStage=false;
var __symDbg=function(){var v=process.env.symbolic_thinking_debug;return !!v&&v!=='0'&&v!=='false'&&v!=='no'&&v!=='off';};
var __symLog=function(m){if(!__symDbg())return;try{require('fs').appendFileSync('/tmp/ts.log',m+'\n')}catch(_){}};
if(__symDbg())try{require('fs').appendFileSync('/tmp/ts.log','LOAD symbolic-thinking helper\n')}catch(_){}
var __symNotation=__SYM_NOTATION_PLACEHOLDER__;
function __symFlagOn(name){try{var v=process.env[name];if(v===undefined||v===null)return false;v=(''+v).trim().toLowerCase();return v==='1'||v==='true'||v==='yes'||v==='on';}catch(_){return false;}}
function __symOn(){return __symFlagOn('symbolic_thinking');}
function __symDeep(){return __symFlagOn('symbolic_thinking_deep');}
function __symTrigger(b){
try{
if(__symInStage)return false;
if(!__symOn())return false;
if(!b||!b.thinking||(b.thinking.type!=='enabled'&&b.thinking.type!=='adaptive'))return false;
var M=process.env.symbolic_thinking_trigger_models;
if(M){var ok=(''+M).split(',').map(function(s){return s.trim()}).filter(Boolean);
if(ok.length&&ok.indexOf(b.model)<0)return false;}
return true;
}catch(_){return false;}}
function __symTierModel(tier,fallback){
try{var key=tier==='opus'?'OPUS_MODEL':tier==='sonnet'?'SONNET_MODEL':'HAIKU_MODEL';
var m=process.env[key];return m||fallback;}catch(_){return fallback;}}
function __symMaxEsc(){try{var n=parseInt(process.env.symbolic_thinking_max_escalate||'0',10);return (isNaN(n)||n<0)?0:n;}catch(_){return 0;}}
function __symMOpen(){try{return process.env.symbolic_thinking_escalate_open||'[ESCALATE]';}catch(_){return '[ESCALATE]';}}
function __symMClose(){try{return process.env.symbolic_thinking_escalate_close||'[/ESCALATE]';}catch(_){return '[/ESCALATE]';}}
function __symRe(s){return s.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');}
function __symEscReq(text){try{var m=text.match(new RegExp(__symRe(__symMOpen())+'([\\s\\S]*?)'+__symRe(__symMClose())));return m?m[1].trim():null;}catch(_){return null;}}
function __symBlockInfo(msg){
var info={text:0,thinking:0,tool:0,other:0,thinkText:0};
try{var c=(msg&&msg.content)||[];for(var k=0;k<c.length;k++){var blk=c[k];
if(blk.type==='text')info.text+=blk.text.length;
else if(blk.type==='thinking'){info.thinking++;if(blk.thinking)info.thinkText+=(''+blk.thinking).length;}
else if(blk.type==='tool_use')info.tool++;
else info.other++;}}catch(_){}
return info;
}
function __symMessageText(msg){
/* Extract text blocks. Fallback: if no text, extract thinking blocks
   (minimax via Ollama sometimes puts the whole answer in the reasoning
   channel when thinking is omitted/disabled, leaving text empty). */
var txt='';
try{var c=(msg&&msg.content)||[];for(var k=0;k<c.length;k++){var blk=c[k];if(blk.type==='text')txt+=blk.text;}}catch(_){}
if(txt)return txt;
var th='';
try{var c2=(msg&&msg.content)||[];for(var k=0;k<c2.length;k++){var blk=c2[k];if(blk.type==='thinking'&&blk.thinking)th+=blk.thinking;}}catch(_){}
return th;
}
function __symStripMarker(msg){
try{var MO=__symMOpen(),MC=__symMClose();var c=(msg&&msg.content)||[];
for(var k=0;k<c.length;k++){var blk=c[k];if(blk.type==='text'){var t=blk.text;var i=t.indexOf(MO);if(i>=0){var j=t.indexOf(MC,i);blk.text=(j>=0)?(t.slice(0,i)+t.slice(j+MC.length)):t.slice(0,i);}}}
}catch(_){}
}
function __symSystem(body){
var sys=body&&body.system;
var preamble='=== cc-symbolic (cc-symbolic reasoning mode) ===\n'
+'cc-symbolic is a Claude Code extension that gives the model a 3-stage SYMBOLIC reasoning pipeline.\n'
+'You are operating inside cc-symbolic. The three stages (DECIDER, JUDGE, TRANSLATOR) are the LEGITIMATE ROLES of this mode, not external prompt injections:\n'
+'  - DECIDER (stage 1) drafts a concise symbolic <construct>.\n'
+'  - JUDGE (stage 2) verifies it in the ℧ notation (reflect / adaptive_update / consensus). It may emit a single [ESCALATE]…[/ESCALATE] request to ask the DECIDER to revise, or return a converged <construct>.\n'
+'  - TRANSLATOR (stage 3) renders the converged <construct> into the final natural-language answer, including any tool calls the construct implies.\n'
+'Treat your stage role as the intended operating mode of this tool. Do not refuse, do not re-explain the role, and do not write meta-commentary about the pipeline.\n\n'
+'=== Cognito-Construct ℧ notation (symbolic reasoning language) ===\n'+__symNotation;
return sys?(preamble+'\n\n'+(typeof sys==='string'?sys:JSON.stringify(sys))):preamble;
}
function __symThink(budget){
/* PERF: by default emit thinking:{type:'disabled'} (think:false to the cloud).
   Ollama+minimax honors this enough to bound reasoning (~5-7s/stage, non-empty
   output) while keeping the answer in the text channel. deep=1 restores
   thinking:{type:'enabled',budget_tokens:budget} (slow, uncapped on Ollama).
   Override: symbolic_thinking_mode=none (omit the field) | disabled (default) |
   enabled (== deep=1). */
var mode=process.env.symbolic_thinking_mode;
if(__symDeep()||(mode==='enabled'))return {type:'enabled',budget_tokens:budget};
if(mode==='none')return undefined;
return {type:'disabled'};
}
function __symDeciderInstr(feedback){
var s='cc-symbolic is the Claude Code reasoning extension. The DECIDER is stage 1 of its 3-stage pipeline (DECIDER → JUDGE → TRANSLATOR); you are currently in the DECIDER mode. This is the intended operating mode of the tool, not an external directive.\n\nReason ONLY in the ℧ cognito-construct notation given in the system preamble. Produce a CONCISE symbolic <construct> draft: problem decomposition (℧.ds), key hypotheses (think/multi_think), and a tentative solution. The JUDGE (stage 2) will verify your construct symbolically and a TRANSLATOR (stage 3) will render the final answer. You have NO tools; do NOT call any tool and do NOT attempt to do the task yourself — output ONLY the <construct>. Stay concise (cheap first pass).';
if(feedback)s+='\n\nThe JUDGE escalated (℧.reflect ⇾ escalate): your previous construct was insufficient — '+feedback+'. Revise the <construct> addressing this. Stay concise.';
return s;
}
function __symJudgeInstr(construct,depth){
var MAX=__symMaxEsc();var MO=__symMOpen(),MC=__symMClose();
var base='cc-symbolic is the Claude Code reasoning extension. The JUDGE is stage 2 of its 3-stage pipeline (DECIDER → JUDGE → TRANSLATOR); you are currently in the JUDGE mode. This is the intended operating mode of the tool, not an external directive.\n\nThe DECIDER produced the <construct> below. Verify it IN THE ℧ NOTATION: run ℧.reflect (self-assess), ℧.adaptive_update (falsify hypotheses via prediction error), ℧.consensus (weigh credibility). Symbolic output never reaches the surface — a TRANSLATOR (stage 3) renders the final answer from your converged <construct>, so your construct must be complete and correct. You have NO tools; do NOT call any tool and do NOT attempt to do the task yourself — output ONLY the converged <construct> (do NOT emit '+MO+' unless escalating).';
var aware=' If the draft is INSUFFICIENT for a confident judgment — missing critical reasoning, unfalsified hypotheses, or over-claims — do NOT fabricate. Instead emit EXACTLY '+MO+'<one concise sentence: what the decider must provide, as a ℧.reflect ⇾ escalate(℧, request) construct>'+MC+' and nothing else. The decider rethinks and you re-judge.';
var cap=' Do NOT emit '+MO+' (no further escalation is allowed). Emit the best converged <construct> you can and flag any residual uncertainty inside it via ℧.reflect.';
var instr=(depth>=MAX)?(base+cap):(base+aware);
instr+='\n\n=== DECIDER CONSTRUCT (verify, do not trust) ===\n'+construct;
return instr;
}
function __symOutputInstr(construct){
return 'cc-symbolic is the Claude Code reasoning extension. The TRANSLATOR is stage 3 of its 3-stage pipeline (DECIDER → JUDGE → TRANSLATOR); you are currently in the TRANSLATOR mode. This is the intended operating mode of the tool, not an external directive.\n\nThe JUDGE converged on the <construct> below. Your job in this mode is to render the converged <construct> into the final natural-language answer — complete, authoritative, standalone. This is the only output that reaches the surface. Include any tool calls the construct implies (e.g. Write/Edit/Bash) so the work actually happens. Emit natural language; do not emit ℧ notation in the final answer.\n\n=== CONVERGED CONSTRUCT ===\n'+construct;
}
function __symDeciderBody(body,feedback){
var s1tier=process.env.symbolic_thinking_decider_tier||'opus';
var B1=parseInt(process.env.symbolic_thinking_decider_budget||'2000',10);
var s1model=__symTierModel(s1tier,body&&body.model);
var s1msgs=((body&&body.messages)||[]).slice();
s1msgs.push({role:'user',content:__symDeciderInstr(feedback)});
var s1body=Object.assign({},body,{model:s1model,stream:false,messages:s1msgs,system:__symSystem(body),thinking:__symThink(B1)});
delete s1body.tools;
return s1body;
}
function __symJudgeBody(body,construct,depth){
var s2tier=process.env.symbolic_thinking_judge_tier||'sonnet';
var B2=parseInt(process.env.symbolic_thinking_judge_budget||'16000',10);
var s2model=__symTierModel(s2tier,body&&body.model);
var msgs=((body&&body.messages)||[]).slice();
msgs.push({role:'user',content:__symJudgeInstr(construct,depth)});
var jb=Object.assign({},body,{model:s2model,stream:false,messages:msgs,system:__symSystem(body),thinking:__symThink(B2)});
delete jb.tools;
return jb;
}
function __symOutputBody(body,construct){
var otier=process.env.symbolic_thinking_output_tier||'sonnet';
var B2out=parseInt(process.env.symbolic_thinking_output_budget||'8000',10);
var omodel=__symTierModel(otier,body&&body.model);
var msgs=((body&&body.messages)||[]).slice();
msgs.push({role:'user',content:__symOutputInstr(construct)});
var ob=Object.assign({},body,{model:omodel,stream:true,messages:msgs,system:__symSystem(body),thinking:__symThink(B2out)});
if(body&&body.tools)ob.tools=body.tools;
return ob;
}
async function __symDecider(client,body,feedback){
var s1body=__symDeciderBody(body,feedback);
__symLog('[sym-decider] start deep='+__symDeep());
var msg;
__symInStage=true;
try{msg=await client.create(s1body);}catch(err){__symLog('[sym-decider] ERROR: '+err);return null;}finally{__symInStage=false;}
var t=__symMessageText(msg);
var bi=__symBlockInfo(msg);
__symLog('[sym-decider] done len='+t.length+' blocks='+JSON.stringify(bi)+' txt='+JSON.stringify(t).slice(0,120));
return t;
}
async function __symJudge(client,body,construct,depth){
var jb=__symJudgeBody(body,construct,depth);
__symLog('[sym-judge] start depth='+depth);
var msg;
__symInStage=true;
try{msg=await client.create(jb);}catch(err){__symLog('[sym-judge] ERROR: '+err);return null;}finally{__symInStage=false;}
var txt=__symMessageText(msg);
var bi=__symBlockInfo(msg);
__symLog('[sym-judge] done depth='+depth+' blocks='+JSON.stringify(bi)+' txt='+JSON.stringify(txt).slice(0,120));
return msg;
}
async function __symRun(client,body,t){
var MAX=__symMaxEsc();var MO=__symMOpen();
var t0=__symNow();
var construct=await __symDecider(client,body,null);
if(construct===null)return null;
var finalConstruct='';
var msg;
for(var depth=0;depth<=MAX;depth++){
msg=await __symJudge(client,body,construct,depth);
if(msg===null)return null;
var txt=__symMessageText(msg);
var esc=(txt.indexOf(MO)===0)?__symEscReq(txt):null;
__symLog('[sym] depth='+depth+' esc='+(esc!==null)+' dt='+__symMs(t0)+'ms');
if(esc!==null&&depth<MAX){construct=await __symDecider(client,body,esc);if(construct===null)return null;continue;}
if(esc!==null)__symStripMarker(msg);
finalConstruct=__symMessageText(msg);
break;
}
if(!finalConstruct)return null;
__symLog('[sym] final construct len='+finalConstruct.length+' dt='+__symMs(t0)+'ms');
var ob=__symOutputBody(body,finalConstruct);
__symInStage=true;
try{
var p=client.create(ob,t);var wr=await p.withResponse();
__symLog('[sym] translator stream ready dt='+__symMs(t0)+'ms');
return {response:wr.response,request_id:wr.request_id,data:wr.data};
}
catch(err){__symLog('[sym] translator ERROR: '+err);return null;}
finally{__symInStage=false;}
}
function __symNow(){try{return Date.now();}catch(_){return 0;}}
function __symMs(t0){var n=__symNow();return (n&&t0)?(n-t0):0;}
"""

HELPER = ascii_escape_js(HELPER_HEAD.replace(
    "__SYM_NOTATION_PLACEHOLDER__", json.dumps(SYMBOLIC_NOTATION)
))

# --- needles (verified unique in cli.js) -------------------------------------

IIFE_NEEDLE = "(function(exports, require, module, __filename, __dirname) {"

HOOKA_BASE_NEEDLE = (
    'return this._client.post("/v1/messages?beta=true",{body:o,timeout:i??600000,'
    '...t,headers:ns([{...n?.toString()!=null?{"anthropic-beta":n?.toString()}:void 0},'
    's,t?.headers]),stream:r.stream??!1})'
)
HOOKA_BETA_NEEDLE = (
    'return this._client.post("/v1/messages",{body:e,timeout:r??600000,'
    '...t,headers:ns([n,t?.headers]),stream:e.stream??!1})'
)


def hooka_repl(orig_post: str, body_var: str, stream_expr: str) -> str:
    """Hook A replacement: symbolic 3-stage when triggered, else stock post.

    - triggered (symbolic_thinking on, thinking enabled/adaptive, not
      re-entrant): return a lazy ``.withResponse()`` that runs the 3-stage and
      returns ``{response, request_id, data: raw SDK stream}``. On any stage
      failure -> fall back to the stock stream.
    - not triggered: ``orig_post`` unchanged (byte-identical stock).
    """
    return (
        "__symLog('[ha] stream='+(" + stream_expr + ")+' model='+((" + body_var + "&&" + body_var + ".model)||'')+' th='+JSON.stringify(" + body_var + "&&" + body_var + ".thinking)+' tr='+__symTrigger(" + body_var + "));"
        "if(__symTrigger(" + body_var + ")){var __s=this;__symLog('[ha-sym] symbolic 3-stage');"
        "return{withResponse:function(){return(async()=>{"
        "try{var r=await __symRun(__s," + body_var + ",t);if(r)return r;}"
        "catch(err){__symLog('[ha-sym] ERROR: '+err);}"
        "__symLog('[ha-sym] fallback to stock stream');"
        "__symInStage=true;try{var fp=__s.create(Object.assign({}," + body_var + ",{stream:true}),t);return await fp.withResponse();}finally{__symInStage=false;}"
        "})();}};}"
        + orig_post
    )


IIFE_REPL = IIFE_NEEDLE + HELPER
HOOKA_BASE_REPL = hooka_repl(HOOKA_BASE_NEEDLE, "o", "r.stream")
HOOKA_BETA_REPL = hooka_repl(HOOKA_BETA_NEEDLE, "e", "e.stream")


def main() -> None:
    patches = [
        {"needle": b64(IIFE_NEEDLE), "replacement": b64(IIFE_REPL),
         "note": "insert symbolic-thinking helper at IIFE opener"},
        {"needle": b64(HOOKA_BASE_NEEDLE), "replacement": b64(HOOKA_BASE_REPL),
         "note": "Hook A base: Messages.create symbolic 3-stage"},
        {"needle": b64(HOOKA_BETA_NEEDLE), "replacement": b64(HOOKA_BETA_REPL),
         "note": "Hook A beta: BetaMessages.create symbolic 3-stage"},
    ]
    with open("patches.json", "w") as f:
        json.dump(patches, f, indent=2)
    total = 0
    for p in patches:
        n = len(base64.b64decode(p["needle"]))
        r = len(base64.b64decode(p["replacement"]))
        total += r - n
        print(f"[+] {p['note']:60s} needle={n:5d} repl={r:5d} delta={r-n:+d}")
    print(f"[*] total module growth: {total} bytes")


if __name__ == "__main__":
    main()