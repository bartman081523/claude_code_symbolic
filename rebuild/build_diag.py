#!/usr/bin/env python3
"""Append diagnostic patches to patches.json to locate the typeless transcript
event that crashes the TUI render (bKy default -> bare object -> spread crash).

Patch A: QT loop neighbor logger (context of the bad event).
Patch B: bKy `default` -> return [e] (safe wrap) so the turn renders instead of
         crashing, letting us observe behavior + downstream creation.
Patch C: wrap x9f.mutableMessages in a logging Proxy -- any array method call
         that receives an argument with `type===undefined` logs the method name,
         the item, and `new Error().stack` to /tmp/mm.log.  This catches the
         CREATION/push site of the typeless event with a real stack.

Written as a file (no inline shell) so backslashes survive.  Newline = chr(10).
"""
import json, base64, subprocess

subprocess.run(["python3", "gen_patch.py"], check=True)
patches = json.load(open("patches.json"))
assert len(patches) == 5, f"expected 5 base patches, got {len(patches)}"

NL = chr(10)

# --- Patch A: QT loop neighbor logger ---
qt_needle = 'for(let i of e){let s=n,a=_Ky(i)?s:!1;if(r){'
qt_repl = (
    'for(let i of e){'
    'if(i&&i.type===void 0){try{require("fs").appendFileSync("/tmp/qt.log",'
    '"QTTYPELESS self="+JSON.stringify(i)+" ctx="+JSON.stringify(e.map(function(x){'
    'if(!x)return null;'
    'var o={type:x.type};'
    'if(x.attachment)o.att=x.attachment.type;'
    'if(x.message&&x.message.content)o.ct=Array.isArray(x.message.content)?x.message.content.map(function(c){return c&&c.type}):typeof x.message.content;'
    'o.keys=Object.keys(x);'
    'return o;}))'
    '+' + repr(NL) + ')}catch(_){}}'
    'let s=n,a=_Ky(i)?s:!1;if(r){'
)

# --- Patch B: bKy default safe wrap (no crash) ---
bky_needle = 'origin:e.origin}),uuid:r?qur(e.uuid,i):e.uuid}})}default:return e}}'
bky_repl = 'origin:e.origin}),uuid:r?qur(e.uuid,i):e.uuid}})}default:{try{require("fs").appendFileSync("/tmp/bky.log","BKYDEFAULT "+JSON.stringify(e)+' + repr(NL) + ')}catch(_){}return [e];}}}'
assert bky_repl.count('{') == 3 and bky_repl.count('}') == 9

# --- Patch C: logging Proxy on mutableMessages ---
mm_needle = 'this.mutableMessages=e.initialMessages??[]'
mm_repl = (
    'this.mutableMessages = new Proxy(e.initialMessages ?? [], {get:function(t,k){'
    'var v=t[k];'
    'if(typeof v!=="function")return v;'
    'return function(){var a=arguments;'
    'for(var i=0;i<a.length;i++){var x=a[i];'
    'if(x&&x.type===void 0){try{require("fs").appendFileSync("/tmp/mm.log",'
    '"MM "+String(k)+" "+JSON.stringify(x)+' + repr(NL) + '+new Error().stack+' + repr(NL) + ')'
    '}catch(_){}}}'
    'return v.apply(t,a);};}})'
)

patches.append({
    "needle": base64.b64encode(qt_needle.encode()).decode(),
    "replacement": base64.b64encode(qt_repl.encode()).decode(),
    "note": "DIAG QT loop typeless-neighbor logger",
})
patches.append({
    "needle": base64.b64encode(bky_needle.encode()).decode(),
    "replacement": base64.b64encode(bky_repl.encode()).decode(),
    "note": "DIAG bKy default safe wrap",
})
patches.append({
    "needle": base64.b64encode(mm_needle.encode()).decode(),
    "replacement": base64.b64encode(mm_repl.encode()).decode(),
    "note": "DIAG mutableMessages logging Proxy (typeless push + stack)",
})

# --- Patch D: log every Lt event flowing from Mne -> the 766852 consumer, +
#     stack for typeless ones.  Preserves t.value (return value) via manual
#     async-iterator walk instead of `yield* e`.
mvs_needle = 'async function*MvS(e,t){t.value=yield*e}'
mvs_repl = (
    'async function*MvS(e,t){'
    'var it=e[Symbol.asyncIterator](),r;'
    'try{while(true){var n=await it.next();'
    'if(n.done){r=n.value;break}'
    'var v=n.value;'
    'try{require("fs").appendFileSync("/tmp/lt.log","LT "+String(v&&v.type||"UNDEF")+" keys="+JSON.stringify(v&&Object.keys(v))+' + repr(NL) + ')}catch(_){};'
    'if(v&&v.type===void 0){try{require("fs").appendFileSync("/tmp/lt.log","LTTYPELESS "+JSON.stringify(v)+' + repr(NL) + '+new Error().stack+' + repr(NL) + ')}catch(_){}}'
    'yield v}}catch(x){try{require("fs").appendFileSync("/tmp/lt.log","LTERR "+x+' + repr(NL) + ')}catch(_){}}'
    'finally{t.value=r}}'
)
assert mvs_repl.count('{') == mvs_repl.count('}'), (mvs_repl.count('{'), mvs_repl.count('}'))
assert mvs_repl.count('(') == mvs_repl.count(')'), (mvs_repl.count('('), mvs_repl.count(')'))
patches.append({
    "needle": base64.b64encode(mvs_needle.encode()).decode(),
    "replacement": base64.b64encode(mvs_repl.encode()).decode(),
    "note": "DIAG MvS Lt-event logger (type+keys per event, stack on typeless)",
})
json.dump(patches, open("patches.json", "w"), indent=2)
print("entries", len(patches))
# sanity: braces balanced for mm
print("mm needle braces:", mm_needle.count('{'), mm_needle.count('}'))
print("mm repl braces:", mm_repl.count('{'), mm_repl.count('}'))