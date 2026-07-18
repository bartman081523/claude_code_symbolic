#!/usr/bin/env python3
"""two_stage_shim -- 2-stage epistemic thinking for claude-code, via its
*normal* thinking flow (NOT a hermes plugin).

PROBLEM
    claude-code runs extended thinking on a single model (the main-loop /
    "opus" tier, ``getMainLoopModel``). All the (expensive) thinking is done by
    Opus -> too many Opus tokens.

DESIGN
    A transparent proxy on ``ANTHROPIC_BASE_URL``. claude-code keeps issuing its
    ordinary ``POST /v1/messages`` request with ``thinking:{type:"enabled",
    budget_tokens:N}``. The shim splits that one call into two:

      Stage 1 -- DECIDER (Opus).  Forward the original request with a SMALL
        thinking budget (``TWO_STAGE_DECIDER_BUDGET``). Opus drafts a response
        (text + tool_use + its short thinking). Cheap.

      Stage 2 -- JUDGE (Sonnet).  Build a new request: the SciMind 5.0 epistemic
        mandates (see ``scimind_mandates.py``, ported from hermes-agent commit
        1a7bb2983) as the system preamble + a judge instruction, the original
        messages + the decider's draft as a final user turn, the SAME tools, and
        a LARGE thinking budget (``TWO_STAGE_JUDGE_BUDGET``). Sonnet reasons
        heavily (cheap per token) and emits the AUTHORITATIVE final response,
        including tool calls. The shim returns Sonnet's response to claude-code,
        reshaped as a normal Anthropic ``/v1/messages`` reply under the model
        name claude-code originally requested.

    Tool calls are preserved (Sonnet emits them), so the claude-code agent loop
    keeps working. thinking content blocks are dropped from the reply to avoid
    Anthropic signature verification on synthesised blocks.

COMPOSITION
    The shim speaks the Anthropic ``/v1/messages`` schema on BOTH sides, so it
    composes cleanly in front of an Anthropic backend, or behind/with
    ``anthropic_openai_shim.py`` when the real backend is OpenAI-schema
    (OpenRouter / NIM). Point claude-code at this shim via ``ANTHROPIC_BASE_URL``.

CONFIG (env)
    TWO_STAGE_ENABLED            true/1/yes/on (default true)
    TWO_STAGE_PORT               9877
    TWO_STAGE_UPSTREAM_BASE_URL  https://api.anthropic.com   (Anthropic-schema)
    TWO_STAGE_OPUS_MODEL         decider model name at the upstream
    TWO_STAGE_SONNET_MODEL       judge model name at the upstream
    TWO_STAGE_API_KEY            upstream key (else forwarded from inbound)
    TWO_STAGE_DECIDER_BUDGET     small thinking budget for stage 1 (default 2000)
    TWO_STAGE_JUDGE_BUDGET       large thinking budget for stage 2 (default 16000)
    TWO_STAGE_TRIGGER_MODELS    comma-sep incoming model names that trigger 2-stage
                                 (empty = any request with thinking enabled)
    TWO_STAGE_MAX_CHARS         cap on decider draft fed to the judge (default 32000)
    TWO_STAGE_LOG               /tmp/two-stage-shim.log

USAGE
    python3 two_stage_shim.py
    ANTHROPIC_BASE_URL=http://127.0.0.1:9877 OPUS_MODEL=<opus> SONNET_MODEL=<sonnet> claude
    (OPUS_MODEL / SONNET_MODEL are read by claude-code itself for tier names; the
    shim reads TWO_STAGE_OPUS_MODEL / TWO_STAGE_SONNET_MODEL for the upstream names.)

STATUS: v0.1. Non-streaming upstream calls; synthesises SSE back to claude-code when
the inbound request asked for streaming. Stage 2 may emit tool_use; tool results from
prior turns are carried via the original messages. Streaming passthrough for
non-triggered requests. See DESIGN.md for limitations + roadmap.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from scimind_mandates import SCIMIND_5_0_PREAMBLE

LOG = logging.getLogger("two_stage")

# ---- config ----------------------------------------------------------------

def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None or raw == "" else raw.strip().lower() in {"1", "true", "yes", "on"}

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default

def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    return default if raw is None else (raw.strip() or default)

ENABLED        = _env_bool("TWO_STAGE_ENABLED", True)
PORT           = _env_int("TWO_STAGE_PORT", 9877)
UPSTREAM       = _env_str("TWO_STAGE_UPSTREAM_BASE_URL", "https://api.anthropic.com").rstrip("/")
OPUS_MODEL     = _env_str("TWO_STAGE_OPUS_MODEL", "")
SONNET_MODEL   = _env_str("TWO_STAGE_SONNET_MODEL", "")
API_KEY        = os.environ.get("TWO_STAGE_API_KEY", "")
DECIDER_BUDGET = _env_int("TWO_STAGE_DECIDER_BUDGET", 2000)
JUDGE_BUDGET   = _env_int("TWO_STAGE_JUDGE_BUDGET", 16000)
TRIGGER        = [m.strip() for m in os.environ.get("TWO_STAGE_TRIGGER_MODELS", "").split(",") if m.strip()]
MAX_CHARS      = _env_int("TWO_STAGE_MAX_CHARS", 32000)
LOG_PATH       = _env_str("TWO_STAGE_LOG", "/tmp/two-stage-shim.log")

JUDGE_INSTRUCTION = (
    "You are the JUDGE in a 2-stage reasoning pipeline. A DECIDER model has "
    "already analysed the task and produced the DRAFT below. Your job is to "
    "reason epistemically about that draft and emit the AUTHORITATIVE final "
    "response.\n"
    "Apply the Core Mandates that follow: treat the draft as an incomplete "
    "suggestion, verify its claims, actively seek evidence that it is WRONG "
    "(falsificationism), and only emit tool calls or answers you have justified. "
    "You may reuse the same tools as the decider; you may correct, reject, or "
    "confirm the draft. The user only sees YOUR final response, so it must stand "
    "alone and be correct.\n\n"
    "=== DECIDER DRAFT (verify, do not trust) ===\n"
)

# ---- logging ---------------------------------------------------------------

def _setup_log() -> None:
    fmt = "%(asctime)s %(levelname)s %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt)
    try:
        h = logging.FileHandler(LOG_PATH)
        h.setFormatter(logging.Formatter(fmt))
        logging.getLogger().addHandler(h)
    except OSError:
        pass

def _log_event(rec: dict) -> None:
    rec["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    line = json.dumps(rec, ensure_ascii=False)
    LOG.info(line)

# ---- HTTP ------------------------------------------------------------------

def _upstream_key(headers: dict) -> str:
    return API_KEY or headers.get("x-api-key") or (headers.get("authorization", "") or "").replace("Bearer ", "")

def _post_json(url: str, body: dict, headers: dict) -> dict:
    data = json.dumps(body).encode()
    h = {
        "content-type": "application/json",
        "anthropic-version": headers.get("anthropic-version", "2023-06-01"),
        "x-api-key": _upstream_key(headers),
    }
    if beta := headers.get("anthropic-beta"):
        h["anthropic-beta"] = beta
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"upstream {url} -> {e.code}: {err[:500]}")

# ---- helpers ---------------------------------------------------------------

def _thinking_budget(req: dict, budget: int) -> Optional[dict]:
    """Return a thinking spec with the given budget, or None if thinking off."""
    t = req.get("thinking")
    if not t or t.get("type") == "disabled":
        return None
    return {"type": "enabled", "budget_tokens": budget}

def _draft_text(resp: dict) -> str:
    """Concatenate text + tool_use blocks of a response into a readable draft."""
    parts = []
    for blk in resp.get("content", []):
        if blk.get("type") == "text":
            parts.append(blk.get("text", ""))
        elif blk.get("type") == "tool_use":
            parts.append(f"[tool_call: {blk.get('name')}({json.dumps(blk.get('input', {}))})]")
        elif blk.get("type") == "thinking":
            parts.append(f"[decider thinking: {blk.get('thinking','')[:1000]}...]")
    return "\n".join(p for p in parts if p)

def _final_content(resp: dict) -> list:
    """Keep text + tool_use from the judge; drop thinking blocks (signatures)."""
    out = []
    for blk in resp.get("content", []):
        if blk.get("type") in ("text", "tool_use"):
            out.append(blk)
    if not out:
        out.append({"type": "text", "text": ""})
    return out

def _trigger(req: dict) -> bool:
    if not ENABLED:
        return False
    if not OPUS_MODEL or not SONNET_MODEL:
        return False
    if not _thinking_budget(req, DECIDER_BUDGET):
        return False
    if TRIGGER and req.get("model") not in TRIGGER:
        return False
    return True

# ---- the two stages --------------------------------------------------------

def _stage1_decider(req: dict, headers: dict) -> dict:
    body = dict(req)
    body["model"] = OPUS_MODEL
    body["stream"] = False
    tb = _thinking_budget(req, DECIDER_BUDGET)
    body["thinking"] = tb if tb else {"type": "disabled"}
    return _post_json(f"{UPSTREAM}/v1/messages", body, headers)

def _stage2_judge(req: dict, draft_resp: dict, headers: dict) -> dict:
    draft = _draft_text(draft_resp)
    if len(draft) > MAX_CHARS:
        draft = draft[:MAX_CHARS] + "\n…[truncated]"
    judge_user = {"role": "user", "content": JUDGE_INSTRUCTION + draft}
    messages = list(req.get("messages", [])) + [judge_user]

    body = {
        "model": SONNET_MODEL,
        "messages": messages,
        "stream": False,
        "thinking": {"type": "enabled", "budget_tokens": JUDGE_BUDGET},
        "max_tokens": req.get("max_tokens", 4096),
    }
    if req.get("tools"):
        body["tools"] = req["tools"]
        body["tool_choice"] = req.get("tool_choice", {"type": "auto"})
    if sys := req.get("system"):
        body["system"] = sys
    sys_preamble = SCIMIND_5_0_PREAMBLE
    body["system"] = (sys if isinstance(sys, str) else json.dumps(sys)) if sys else ""
    body["system"] = (sys_preamble + "\n\n" + body["system"]).strip() if body["system"] else sys_preamble
    return _post_json(f"{UPSTREAM}/v1/messages", body, headers)

def _reshape(judge_resp: dict, requested_model: str) -> dict:
    return {
        "id": judge_resp.get("id", "msg_two_stage"),
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": _final_content(judge_resp),
        "stop_reason": judge_resp.get("stop_reason", "end_turn"),
        "stop_sequence": judge_resp.get("stop_sequence"),
        "usage": judge_resp.get("usage", {"input_tokens": 0, "output_tokens": 0}),
    }

# ---- SSE synthesis (when claude-code asked for streaming) ------------------

def _sse(resp: dict) -> bytes:
    mid = resp["id"]
    usage = resp.get("usage", {})
    out = []
    out.append(("message_start", {"type": "message_start", "message": {
        "id": mid, "type": "message", "role": "assistant", "model": resp["model"],
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": usage.get("input_tokens", 0), "output_tokens": 0}}}))
    for idx, blk in enumerate(resp["content"]):
        if blk["type"] == "text":
            out.append(("content_block_start", {"type": "content_block_start", "index": idx,
                "content_block": {"type": "text", "text": ""}}))
            text = blk.get("text", "")
            STEP = 200
            for k in range(0, len(text), STEP):
                out.append(("content_block_delta", {"type": "content_block_delta", "index": idx,
                    "delta": {"type": "text_delta", "text": text[k:k + STEP]}}))
            out.append(("content_block_stop", {"type": "content_block_stop", "index": idx}))
        elif blk["type"] == "tool_use":
            out.append(("content_block_start", {"type": "content_block_start", "index": idx,
                "content_block": {"type": "tool_use", "id": blk.get("id", "toolu_2stage"),
                "name": blk.get("name", ""), "input": {}}}))
            out.append(("content_block_delta", {"type": "content_block_delta", "index": idx,
                "delta": {"type": "input_json_delta", "partial_json": json.dumps(blk.get("input", {}))}}))
            out.append(("content_block_stop", {"type": "content_block_stop", "index": idx}))
    out.append(("message_delta", {"type": "message_delta",
        "delta": {"stop_reason": resp.get("stop_reason", "end_turn"), "stop_sequence": None},
        "usage": {"output_tokens": usage.get("output_tokens", 0)}}))
    out.append(("message_stop", {"type": "message_stop"}))
    buf = b""
    for ev, data in out:
        buf += f"event: {ev}\ndata: {json.dumps(data)}\n\n".encode()
    return buf

# ---- passthrough (non-triggered requests) ---------------------------------

def _passthrough(req: dict, headers: dict, want_stream: bool) -> tuple[int, bytes, str, bool]:
    """Forward unchanged. Streaming requests are proxied as JSON here for v0.1
    simplicity (upstream called with stream=False, returned as JSON). Returns
    (status, body_bytes, content_type, is_sse)."""
    body = dict(req)
    body["stream"] = False
    key = _upstream_key(headers)
    h = {"content-type": "application/json",
         "anthropic-version": headers.get("anthropic-version", "2023-06-01"),
         "x-api-key": key}
    if beta := headers.get("anthropic-beta"):
        h["anthropic-beta"] = beta
    data = json.dumps(body).encode()
    r = urllib.request.Request(f"{UPSTREAM}/v1/messages", data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            return resp.status, resp.read(), "application/json", False
    except urllib.error.HTTPError as e:
        return e.code, e.read(), "application/json", False

# ---- handler ---------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _read_headers(self) -> dict:
        return {k.lower(): v for k, v in self.headers.items()}

    def log_message(self, *a):  # silence default stderr noise
        pass

    def do_GET(self):
        if self.path in ("/", "/healthz"):
            body = json.dumps({"ok": True, "enabled": ENABLED,
                               "opus": OPUS_MODEL, "sonnet": SONNET_MODEL,
                               "upstream": UPSTREAM}).encode()
            self.send_response(200); self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)
        else:
            self.send_response(404); self.send_header("content-length", "0"); self.end_headers()

    def do_OPTIONS(self):
        self.send_response(204); self.send_header("content-length", "0"); self.end_headers()

    def do_POST(self):
        if not self.path.startswith("/v1/messages"):
            self.send_response(404); self.send_header("content-length", "0"); self.end_headers(); return
        length = int(self.headers.get("content-length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            self.send_response(400); self.send_header("content-length", "0"); self.end_headers(); return
        headers = self._read_headers()
        want_stream = bool(req.get("stream"))
        requested_model = req.get("model", "")

        if not _trigger(req):
            status, body, ctype, _ = _passthrough(req, headers, want_stream)
            self.send_response(status); self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)
            _log_event({"evt": "passthrough", "model": requested_model,
                        "status": status, "reason": "not triggered"})
            return

        t0 = time.time()
        try:
            d1 = _stage1_decider(req, headers)
            d2 = _stage2_judge(req, d1, headers)
        except RuntimeError as e:
            err = json.dumps({"type": "error", "error": {"type": "upstream_error",
                            "message": str(e)}}).encode()
            self.send_response(502); self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(err))); self.end_headers(); self.wfile.write(err)
            _log_event({"evt": "error", "model": requested_model, "err": str(e)})
            return

        reshaped = _reshape(d2, requested_model)
        _log_event({"evt": "two_stage", "model": requested_model,
                    "decider": OPUS_MODEL, "judge": SONNET_MODEL,
                    "decider_usage": d1.get("usage"), "judge_usage": d2.get("usage"),
                    "ms": int((time.time() - t0) * 1000),
                    "stop_reason": reshaped["stop_reason"]})

        if want_stream:
            sse = _sse(reshaped)
            self.send_response(200); self.send_header("content-type", "text/event-stream")
            self.send_header("cache-control", "no-cache"); self.send_header("connection", "keep-alive")
            self.send_header("content-length", str(len(sse))); self.end_headers(); self.wfile.write(sse)
        else:
            body = json.dumps(reshaped).encode()
            self.send_response(200); self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)

def main() -> None:
    _setup_log()
    if not ENABLED:
        LOG.warning("TWO_STAGE_ENABLED=false -- shim will only passthrough")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    LOG.info("two_stage_shim listening on 127.0.0.1:%d -> %s (opus=%s sonnet=%s)",
             PORT, UPSTREAM, OPUS_MODEL or "<unset>", SONNET_MODEL or "<unset>")
    print(f"two_stage_shim on http://127.0.0.1:{PORT}  (opus={OPUS_MODEL or '<unset>'}, "
          f"sonnet={SONNET_MODEL or '<unset>'}, upstream={UPSTREAM})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()