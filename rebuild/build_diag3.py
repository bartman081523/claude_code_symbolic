#!/usr/bin/env python3
"""Focused diag: base 5 (Proxy inspector) + bky safe-wrap + We/elseq loggers
to find the typeless event sourced from Mne/itd via the engine generator."""
import json, base64, subprocess

subprocess.run(["python3", "gen_patch.py"], check=True)
patches = json.load(open("patches.json"))
assert len(patches) == 5, f"expected 5 base patches, got {len(patches)}"
NL = chr(10)

# bky safe-wrap (prevent spread crash so we can observe downstream)
bky_needle = 'origin:e.origin}),uuid:r?qur(e.uuid,i):e.uuid}})}default:return e}}'
bky_repl = 'origin:e.origin}),uuid:r?qur(e.uuid,i):e.uuid}})}default:{try{require("fs").appendFileSync("/tmp/bky.log","BKYDEFAULT "+JSON.stringify(e)+' + repr(NL) + ')}catch(_){}return [e];}}}'
patches.append({"needle": base64.b64encode(bky_needle.encode()).decode(),
    "replacement": base64.b64encode(bky_repl.encode()).decode(),
    "note": "DIAG bKy default safe wrap"})

# We logger: log every event received from the query generator (Pt.next())
we_needle = 'let We=Ee.value;'
we_repl = ('let We=Ee.value;try{var __wt=We===undefined?"UNDEF_VAL":(We.type===void 0?"NOTYPE":"OK");'
    'require("fs").appendFileSync("/tmp/we.log","WE "+__wt+" type="+String(We&&We.type)+'
    '" keys="+JSON.stringify(We&&Object.keys(We)).slice(0,160)+' + repr(NL) + ')}catch(_){};')
patches.append({"needle": base64.b64encode(we_needle.encode()).decode(),
    "replacement": base64.b64encode(we_repl.encode()).decode(),
    "note": "DIAG engine We-from-Pt logger"})

# elseq logger: log when the else fallback yields q(We) with a typeless/undefined We
elseq_needle = 'else yield q(We);'
elseq_repl = ('else {if(We===undefined||!We||We.type===void 0){try{require("fs").appendFileSync("/tmp/elseq.log",'
    '"ELSEQ We="+JSON.stringify(We)+" stack="+new Error().stack+' + repr(NL) + ')}catch(_){}} yield q(We);}')
patches.append({"needle": base64.b64encode(elseq_needle.encode()).decode(),
    "replacement": base64.b64encode(elseq_repl.encode()).decode(),
    "note": "DIAG else-yield-q(We) typeless logger"})

json.dump(patches, open("patches.json", "w"), indent=2)
for p in patches:
    n = len(base64.b64decode(p["needle"]))
    r = len(base64.b64decode(p["replacement"]))
    print(f"  {p['note'][:60]:60s} {n}->{r}")
print("entries", len(patches))