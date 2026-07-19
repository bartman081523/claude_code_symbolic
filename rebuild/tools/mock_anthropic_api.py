#!/usr/bin/env python3
"""mock_anthropic_api.py — Mock-Server der ANTHROPIC ORIGINAL-API.

Zweck: den 3-stage SYMBOLIC-reasoning-Patch in der gepatchten Binärdatei
(rebuild/out/claude.patched) END-TO-END testen, OHNE echten Anthropic-Key.
Spricht das Messages-API-Protokoll so, dass claude-code's SDK es akzeptiert:

  POST /v1/messages            (Hook A beta)  und
  POST /v1/messages?beta=true  (Hook A base)

Der symbolic Hook leitet jeden Main-Turn in 3 Stage-Requests um:
  DECIDER    (stream:false) -> wir geben einen knappen <construct>-Draft als text
  JUDGE      (stream:false) -> wir geben den konvergierten <construct> (kein ESCALATE)
  TRANSLATOR(stream:true)   -> wir streamen die finale NL-Antwort als SSE

Andere Requests (Titel-Erzeugung, nicht-symbolische Turns) bekommen eine kurze
Generik-Antwort.  Jeder Request wird nach stdout geloggt (Stage, stream,
Snippet) — so kann der Test verifizieren, dass alle 3 Stages den Mock getroffen
haben.

Run:  MOCK_PORT=8765 python3 mock_anthropic_api.py
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("MOCK_PORT", "8765"))
LOG = sys.stdout

def log(msg):
    LOG.write(msg + "\n"); LOG.flush()

def last_user_text(body):
    msgs = body.get("messages") or []
    for m in reversed(msgs):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str): return c
            if isinstance(c, list):
                for b in reversed(c):
                    if isinstance(b, dict) and b.get("type") == "text":
                        return b.get("text", "")
                    if isinstance(b, str):
                        return b
            return ""
    return ""

def stage_of(text):
    # Die Stage-Instruktionen beginnen jeweils mit "You are the <STAGE> (stage N
    # ...".  Prefix-Match statt Substring, weil die Judge-Instr "The DECIDER
    # produced" und die Translator-Instr "The JUDGE converged" enthalten — ein
    # Substring-Match würde die falsche Stage wählen.
    t = (text or "").lstrip()
    if t.startswith("You are the DECIDER"): return "decider"
    if t.startswith("You are the JUDGE"):   return "judge"
    if t.startswith("You are the TRANSLATOR"): return "translator"
    return "other"

DECIDER_CONSTRUCT = (
    "<construct>\n"
    "<symbolic_reason>\n"
    "℧.ds ⇾ { task ≔ \"mock task\", constraints ≔ [] }\n"
    "℧.modules ⇾ [think, reflect, consensus, output]\n"
    "℧.state ⇾ |I⟩\n"
    "</symbolic_reason>\n"
    ":: think(℧) ↦ { μₜ ≔ decode(task), hypotheses ⇾ [\"plan: solve directly\"] }\n"
    ":: consensus(℧) ↦ { chosen ⇾ \"plan: solve directly\" }\n"
    "</construct>"
)

JUDGE_CONSTRUCT = (
    "<construct>\n"
    "<symbolic_reason>\n"
    "℧.reflect ⇾ { diagnosis ≔ \"plan verified\", confidence ≔ high }\n"
    "℧.adaptive_update ⇾ { δₚ ≔ 0 }\n"
    "℧.consensus ⇾ { chosen ⇾ \"solve directly: produce the requested output\" }\n"
    "</symbolic_reason>\n"
    ":: output(℧) ↦ { deliver(formatted) }\n"
    "</construct>"
)

TRANSLATOR_TEXT = (
    "[MOCK TRANSLATOR] Symbolic-Patch gegen Original-API OK: 3 Stages "
    "durchlaufen (DECIDER → JUDGE → TRANSLATOR). Fertige Antwort gerendert."
)

GENERIC_TEXT = "[mock] generic response"

def text_for_stage(stage):
    if stage == "decider": return DECIDER_CONSTRUCT
    if stage == "judge":   return JUDGE_CONSTRUCT
    if stage == "translator": return TRANSLATOR_TEXT
    return GENERIC_TEXT

def msg_json(model, text, stop="end_turn"):
    return {
        "id": "msg_mock",
        "type": "message",
        "role": "assistant",
        "model": model or "mock-model",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": max(1, len(text) // 4)},
    }

def sse_bytes(model, text):
    """Standard Anthropic SSE-Eventfolge für einen Text-Block."""
    ev = []
    ev.append(("message_start", {
        "type": "message_start",
        "message": {
            "id": "msg_mock", "type": "message", "role": "assistant",
            "model": model or "mock-model", "content": [],
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 0},
        },
    }))
    ev.append(("content_block_start", {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "text", "text": ""},
    }))
    # in Stücken deltaen, damit der Stream-Parser echte Deltas sieht
    chunk = 40
    for i in range(0, len(text), chunk):
        ev.append(("content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": text[i:i + chunk]},
        }))
    ev.append(("content_block_stop", {"type": "content_block_stop", "index": 0}))
    ev.append(("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": max(1, len(text) // 4)},
    }))
    ev.append(("message_stop", {"type": "message_stop"}))
    out = []
    for name, data in ev:
        out.append(f"event: {name}\ndata: {json.dumps(data)}\n\n")
    return "".join(out).encode("utf-8")

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body_bytes, ctype, extra=None):
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body_bytes)))
        self.send_header("request-id", "mock-req")
        self.send_header("anthropic-organization-id", "mock-org")
        if extra:
            for k, v in extra.items(): self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body_bytes); self.wfile.flush()

    def do_POST(self):
        length = int(self.headers.get("content-length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception as e:
            log(f"POST {self.path} body-parse-err: {e}")
            self._send(400, b'{"type":"error","error":{"type":"invalid_request","message":"bad json"}}',
                       "application/json")
            return
        model = body.get("model", "mock-model")
        stream = bool(body.get("stream", False))
        txt = last_user_text(body)
        stage = stage_of(txt)
        log(f"POST {self.path} stage={stage:10s} stream={stream} model={model} "
            f"snippet={txt[:60]!r}")
        if stream:
            out_text = text_for_stage(stage)
            self._send(200, sse_bytes(model, out_text), "text/event-stream")
        else:
            out_text = text_for_stage(stage)
            self._send(200, json.dumps(msg_json(model, out_text)).encode("utf-8"),
                       "application/json")

    def do_GET(self):
        # /v1/models etc. — minimales OK
        if self.path.startswith("/v1/models"):
            self._send(200, json.dumps({
                "data": [{"id": "mock-model", "object": "model"}]
            }).encode("utf-8"), "application/json")
        else:
            self._send(200, b'{"ok":true}', "application/json")

    def log_message(self, *a):  # still — wir loggen selbst
        pass

def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    log(f"mock_anthropic_api listening on 127.0.0.1:{PORT}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()