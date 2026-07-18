#!/usr/bin/env python3
"""usage_relay -- HTTP relay that logs per-request TOKEN USAGE, then forwards
to Ollama (127.0.0.1:11434) and streams the response back unchanged.

Extracts usage from:
  * non-stream JSON responses  -> resp["usage"]
  * SSE stream responses      -> message_start.message.usage (input) +
                                  message_delta.usage (output)

Logs one line per request to stderr:
  model=... stream=... think=<budget>  in=... out=...  (stage tag from x-ts header)

Used for the stock-vs-2stage cost comparison."""
import json, sys, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = "http://127.0.0.1:11434"


def _sse_usage(body: bytes):
    """Parse SSE event stream for input/output token usage.

    Each SSE block is ``event: <type>\\ndata: <json>\\n\\n`` -- scan every line
    for ``data:`` (don't require the block to start with it)."""
    inp = out = None
    try:
        text = body.decode("utf-8", "replace")
    except Exception:
        return None, None
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            ev = json.loads(payload)
        except Exception:
            continue
        t = ev.get("type")
        if t == "message_start":
            u = (ev.get("message") or {}).get("usage") or {}
            if inp is None: inp = u.get("input_tokens")
        elif t == "message_delta":
            u = ev.get("usage") or {}
            if u.get("input_tokens") is not None: inp = u.get("input_tokens")
            if u.get("output_tokens") is not None: out = u.get("output_tokens")
    return inp, out


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass

    def _proxy(self, raw):
        try:
            req_body = json.loads(raw)
        except Exception:
            req_body = {}
        model = req_body.get("model", "?")
        stream = bool(req_body.get("stream"))
        th = req_body.get("thinking") or {}
        budget = th.get("budget_tokens") if isinstance(th, dict) else None
        tag = self.headers.get("x-ts-stage", "-")
        h = {k: v for k, v in self.headers.items()
             if k.lower() not in ("host", "content-length", "x-ts-stage")}
        req = urllib.request.Request(UPSTREAM + self.path, data=raw, headers=h, method=self.command)
        try:
            r = urllib.request.urlopen(req, timeout=300)
        except urllib.error.HTTPError as e:
            b = e.read()
            self.send_response(e.code); self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(b))); self.end_headers(); self.wfile.write(b)
            return
        body = r.read()
        ct = r.headers.get("content-type", "application/json")
        # extract usage
        in_tok = out_tok = None
        if stream and "event-stream" in ct:
            in_tok, out_tok = _sse_usage(body)
        else:
            try:
                in_tok = json.loads(body).get("usage", {}).get("input_tokens")
                out_tok = json.loads(body).get("usage", {}).get("output_tokens")
            except Exception:
                pass
        sys.stderr.write(f"[{tag}] model={model} stream={stream} think_budget={budget} "
                          f"in={in_tok} out={out_tok}\n")
        sys.stderr.flush()
        self.send_response(r.status)
        self.send_header("content-type", ct)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        raw = self.rfile.read(n) if n else b""
        self._proxy(raw)
    def do_GET(self):
        req = urllib.request.Request(UPSTREAM + self.path, method="GET")
        r = urllib.request.urlopen(req, timeout=30)
        b = r.read()
        self.send_response(r.status); self.send_header("content-type", r.headers.get("content-type", "application/json"))
        self.send_header("content-length", str(len(b))); self.end_headers(); self.wfile.write(b)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 11500), H).serve_forever()