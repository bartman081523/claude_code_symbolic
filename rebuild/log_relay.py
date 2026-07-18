#!/usr/bin/env python3
"""log_relay -- tiny HTTP relay: log the request body claude-code sends, then
forward to Ollama (127.0.0.1:11434) and stream the response back unchanged.
Used to diagnose what `thinking` claude-code actually puts on the wire."""
import json, sys, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = "http://127.0.0.1:11434"

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def _proxy(self, raw, stream_hint):
        rec = {"method": self.command, "path": self.path}
        try: rec["body"] = json.loads(raw)
        except Exception: rec["body_raw"] = raw.decode("utf-8","replace")[:2000]
        bd = rec.get("body", {}) or {}
        sys.stderr.write(">>> "+self.path+" model="+str(bd.get("model"))+
                         " stream="+str(bd.get("stream"))+
                         " thinking="+json.dumps(bd.get("thinking"))+"\n")
        sys.stderr.write("    tools="+str(len(bd.get("tools") or []))+
                         " msgs="+str(len(bd.get("messages") or []))+"\n")
        sys.stderr.flush()
        # forward
        h = {k: v for k, v in self.headers.items() if k.lower() not in ("host","content-length")}
        req = urllib.request.Request(UPSTREAM+self.path, data=raw, headers=h, method=self.command)
        try:
            r = urllib.request.urlopen(req, timeout=180)
        except urllib.error.HTTPError as e:
            self.send_response(e.code); self.send_header("content-type","application/json")
            b=e.read(); self.send_header("content-length",str(len(b))); self.end_headers(); self.wfile.write(b); return
        body = r.read()
        self.send_response(r.status)
        ct = r.headers.get("content-type","application/json")
        self.send_header("content-type", ct)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        raw = self.rfile.read(n) if n else b""
        self._proxy(raw, True)
    def do_GET(self):
        req = urllib.request.Request(UPSTREAM+self.path, method="GET")
        r = urllib.request.urlopen(req, timeout=30)
        b = r.read(); self.send_response(r.status); self.send_header("content-type",r.headers.get("content-type","application/json"))
        self.send_header("content-length",str(len(b))); self.end_headers(); self.wfile.write(b)

if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 11499), H).serve_forever()