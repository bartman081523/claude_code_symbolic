#!/usr/bin/env python3
"""anthropic_openai_shim — übersetzt Claude-Code-Anthropic-Schema in
NVIDIA-NIM-OpenAI-Schema und zurück.

Warum: NVIDIA NIM (build.nvidia.com) akzeptiert kein Anthropic-API-Schema
unter /v1/messages, sondern nur das OpenAI-kompatible /v1/chat/completions.
Claude Code spricht aber das Anthropic-Schema. Dieser Shim lauscht auf
127.0.0.1:9876 und mappt dazwischen.

Phasen:
  Phase 1 (text-only):  system, messages (text), max_tokens, temperature,
                         top_p, stop_sequences, stream. Tools werden
                         ignoriert, Tool-Calls in Responses werden gedroppt.
  Phase 2 (Tool-Use):    folgt, sobald Phase 1 grün ist.

Konfiguration per Env-Var:
  SHIM_PORT       default 9876
  SHIM_UPSTREAM   default https://integrate.api.nvidia.com
  SHIM_API_KEY    default ""; wenn leer, wird der Wert aus
                  ANTHROPIC_AUTH_TOKEN der Anfrage gelesen (x-api-key-Header)
  SHIM_LOG_LEVEL  default info (debug|info|warn|error)

Aufruf:
  python3 tools/anthropic_openai_shim.py
  SHIM_PORT=9876 SHIM_LOG_LEVEL=debug python3 tools/anthropic_openai_shim.py
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


# ---- Globales --------------------------------------------------------------

# TCP-Nagle-Algorithmus deaktivieren: kleine SSE-Pakete (typisch 100-500 B
# pro Event) sollen sofort raus, nicht 40ms auf ein Ack warten. Das senkt
# Time-to-First-Token messbar (10-30ms pro Paket). Setzen wir ueber den
# Default-Opener, den urllib intern nutzt.
import socket as _socket_mod
_real_create_connection = _socket_mod.create_connection


def _socket_create_connection_nodelay(address, *args, **kwargs):
    sock = _real_create_connection(address, *args, **kwargs)
    try:
        sock.setsockopt(_socket_mod.IPPROTO_TCP, _socket_mod.TCP_NODELAY, 1)
    except OSError:
        pass
    return sock


_socket_mod.create_connection = _socket_create_connection_nodelay  # type: ignore[assignment]


# ---- Konfiguration ----------------------------------------------------------

SHIM_PORT = int(os.environ.get("SHIM_PORT", "9876"))
SHIM_UPSTREAM = os.environ.get("SHIM_UPSTREAM", "https://integrate.api.nvidia.com")
SHIM_API_KEY = os.environ.get("SHIM_API_KEY", "")
SHIM_LOG_LEVEL = os.environ.get("SHIM_LOG_LEVEL", "info").upper()

logging.basicConfig(
    level=getattr(logging, SHIM_LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("shim")


# ---- Mapping: Anthropic-Request → OpenAI-Request ---------------------------

def _map_messages_to_openai(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic messages[] → OpenAI messages[] inkl. tool_use / tool_result.

    Wichtige Pfade:
    - user-Text: content: str → messages[role=user, content=str]
    - user-Blocks: text/image/tool_result → entweder messages[role=user, content=...] (Text)
      oder messages[role=tool, tool_call_id, content=...] (tool_result)
    - assistant-Blocks: text → messages[role=assistant, content=str]
      tool_use → messages[role=assistant, content=None, tool_calls=[...]]
      (Text + tool_use in einer Message → Text in content, tool_calls separat)
    """
    out: list[dict[str, Any]] = []

    for msg in messages or []:
        role = msg.get("role", "user")
        content = msg.get("content")

        if role == "user":
            if isinstance(content, str):
                out.append({"role": "user", "content": content})
                continue
            if isinstance(content, list):
                # Sammle Text-Blöcke UND tool_result-Blöcke separat.
                # tool_result wird zu role=tool message, Text bleibt user-content.
                text_parts: list[str] = []
                tool_msgs: list[dict[str, Any]] = []
                for blk in content:
                    if not isinstance(blk, dict):
                        continue
                    t = blk.get("type")
                    if t == "text":
                        text_parts.append(blk.get("text", ""))
                    elif t == "tool_result":
                        tool_msgs.append({
                            "role": "tool",
                            "tool_call_id": blk.get("tool_use_id", ""),
                            # Anthropic: content ist str oder list[block].
                            # OpenAI: content ist str. Beides nach string giessen.
                            "content": _coerce_tool_result_content(blk.get("content")),
                        })
                    else:
                        log.warning("ignoring user content block: type=%s", t)
                # Tool-Results zuerst (OpenAI-Konvention: tool vor user),
                # dann user-Text. Bei nur tool_results: kein user-Message noetig.
                out.extend(tool_msgs)
                text_joined = "\n".join(p for p in text_parts if p)
                if text_joined:
                    out.append({"role": "user", "content": text_joined})
                continue
            # Fallback: content fehlt oder falscher Typ
            out.append({"role": "user", "content": ""})
            continue

        if role == "assistant":
            if isinstance(content, str):
                out.append({"role": "assistant", "content": content})
                continue
            if isinstance(content, list):
                text_parts: list[str] = []
                tool_calls: list[dict[str, Any]] = []
                for blk in content:
                    if not isinstance(blk, dict):
                        continue
                    t = blk.get("type")
                    if t == "text":
                        text_parts.append(blk.get("text", ""))
                    elif t == "tool_use":
                        inp = blk.get("input", {})
                        # OpenAI verlangt arguments als JSON-String, nicht Objekt.
                        if not isinstance(inp, str):
                            try:
                                inp = json.dumps(inp, ensure_ascii=False)
                            except (TypeError, ValueError):
                                inp = "{}"
                        tool_calls.append({
                            "id": blk.get("id") or _gen_block_id(),
                            "type": "function",
                            "function": {
                                "name": blk.get("name", ""),
                                "arguments": inp,
                            },
                        })
                    else:
                        log.warning("ignoring assistant content block: type=%s", t)
                asm: dict[str, Any] = {"role": "assistant"}
                text_joined = "\n".join(p for p in text_parts if p)
                if text_joined:
                    asm["content"] = text_joined
                else:
                    asm["content"] = None
                if tool_calls:
                    asm["tool_calls"] = tool_calls
                out.append(asm)
                continue
            out.append({"role": "assistant", "content": ""})
            continue

        if role == "tool":
            # Bereits OpenAI-Schema-konform (Claude Code schickt das so nicht,
            # aber wir tolerieren es).
            out.append(msg)
            continue

        if role == "system":
            # Tolerieren: manche Clients schicken System-Messages in messages[]
            # statt im system-Feld. Wir reichen sie als OpenAI-system durch.
            c = msg.get("content")
            if isinstance(c, str):
                out.append({"role": "system", "content": c})
            else:
                log.warning("ignoring system message with non-string content")
            continue

        log.warning("ignoring message with unknown role: %s", role)

    return out


def _coerce_tool_result_content(content: Any) -> str:
    """Anthropic tool_result.content → str.

    Anthropic erlaubt: string | list[block] (Text + ggf. Image).
    OpenAI will: string. Bei Blocks: text konkatenieren, Bilder verwerfen
    (NVIDIA NIM akzeptiert keine Bilder in tool-Messages — wir geben
    einen kurzen Hinweistext aus, damit das Modell weiss, was passiert ist).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for blk in content:
            if not isinstance(blk, dict):
                continue
            t = blk.get("type")
            if t == "text":
                parts.append(blk.get("text", ""))
            elif t == "image":
                parts.append("[image omitted: tool-result images not supported via OpenAI schema]")
        return "\n".join(parts)
    if content is None:
        return ""
    return str(content)


def anthropic_to_openai_request(body: dict[str, Any]) -> dict[str, Any]:
    """Mappt einen Anthropic-/v1/messages-Body in OpenAI-/v1/chat/completions.

    Phase 2: tool_use, tool_result und tools[] werden unterstuetzt.
    """
    out: dict[str, Any] = {
        "model": body.get("model", ""),
        "messages": [],
    }

    # system: string oder list[block] → erstes OpenAI-message
    system = body.get("system")
    if system is not None:
        if isinstance(system, str):
            sys_text = system
        elif isinstance(system, list):
            parts = []
            for blk in system:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    parts.append(blk.get("text", ""))
                elif isinstance(blk, str):
                    parts.append(blk)
            sys_text = "\n".join(p for p in parts if p)
        else:
            sys_text = str(system)
        if sys_text:
            out["messages"].append({"role": "system", "content": sys_text})

    # messages inkl. tool_use/tool_result
    out["messages"].extend(_map_messages_to_openai(body.get("messages") or []))

    # Skalare
    if "max_tokens" in body:
        out["max_tokens"] = int(body["max_tokens"])
    if "temperature" in body:
        out["temperature"] = body["temperature"]
    if "top_p" in body:
        out["top_p"] = body["top_p"]
    if "stop_sequences" in body:
        out["stop"] = body["stop_sequences"]

    # tools[]: Anthropic flat → OpenAI function-wrapped
    if body.get("tools"):
        oai_tools: list[dict[str, Any]] = []
        for tool in body["tools"]:
            if not isinstance(tool, dict):
                continue
            oai_tools.append({
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                },
            })
        out["tools"] = oai_tools

    # tool_choice: {type:"auto"|"any"|"tool", name?} → string oder object
    tc = body.get("tool_choice")
    if isinstance(tc, dict):
        kind = tc.get("type", "auto")
        if kind == "auto":
            out["tool_choice"] = "auto"
        elif kind == "any":
            out["tool_choice"] = "required"
        elif kind == "tool" and tc.get("name"):
            out["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}
        else:
            out["tool_choice"] = "auto"
    elif isinstance(tc, str):
        out["tool_choice"] = tc

    # Stream-Flag
    out["stream"] = bool(body.get("stream", False))

    # Ignoriert: thinking, metadata, context_management, output_config.
    if body.get("thinking"):
        log.info("ignoring thinking field (not supported by OpenAI schema)")

    return out


# ---- Mapping: OpenAI-Response (non-streaming) → Anthropic-Message ----------

def _gen_id() -> str:
    # Anthropic-Format msg_<24-hex> — wir generieren deterministisch genug
    return "msg_" + secrets.token_hex(12)


def _gen_block_id() -> str:
    return "toolu_" + secrets.token_hex(12)


def openai_to_anthropic_response(oai: dict[str, Any], msg_id: str) -> dict[str, Any]:
    """Mappt eine OpenAI-non-streaming-Response in Anthropic-Message-Format."""
    choice = (oai.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    finish = choice.get("finish_reason") or "stop"

    content: list[dict[str, Any]] = []
    text = msg.get("content")
    if text:
        content.append({"type": "text", "text": text})

    # Phase 2: tool_calls werden zu tool_use-Blöcken.
    for tc in (msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        raw_args = fn.get("arguments", "")
        if isinstance(raw_args, dict):
            inp = raw_args
        elif isinstance(raw_args, str):
            try:
                inp = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                log.warning("tool_use arguments not valid JSON, wrapping as raw: %r", raw_args[:200])
                inp = {"_raw": raw_args}
        else:
            inp = {}
        content.append({
            "type": "tool_use",
            "id": tc.get("id") or _gen_block_id(),
            "name": fn.get("name", ""),
            "input": inp,
        })

    # finish_reason → stop_reason
    stop_reason_map = {
        "stop": "end_turn",
        "tool_calls": "tool_use",
        "length": "max_tokens",
        "content_filter": "end_turn",
    }
    stop_reason = stop_reason_map.get(finish, "end_turn")

    usage = oai.get("usage") or {}
    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "content": content or [{"type": "text", "text": ""}],
        "model": oai.get("model", ""),
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens", 0)),
            "output_tokens": int(usage.get("completion_tokens", 0)),
        },
    }


# ---- Mapping: OpenAI-SSE → Anthropic-SSE (Phase 1, text-only) -------------

def _sse(event: str, data: dict[str, Any]) -> bytes:
    """Formatiert ein Anthropic-konformes SSE-Event."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


def openai_sse_to_anthropic_sse(oai_lines: list[str], msg_id: str, model: str) -> bytes:
    """Konvertiert OpenAI-SSE-Chunks in einen Anthropic-SSE-Stream.

    oai_lines: rohe SSE-Textzeilen (ohne 'data: '-Prefix wird hier erwartet).
    Gibt den vollständigen Anthropic-Stream als bytes zurück (für Tests/Buffer).
    """
    out: list[bytes] = []
    text_block_open = False
    text_block_index = 0
    tool_buf: dict[int, dict[str, Any]] = {}
    next_anthropic_index = 0
    input_tokens = 0
    output_tokens = 0
    final_stop_reason: str | None = None

    # message_start immer zuerst
    out.append(_sse("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": model,
            "stop_reason": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    }))

    block_index = 0

    for raw in oai_lines:
        raw = raw.strip()
        if not raw or not raw.startswith("data:"):
            continue
        payload = raw[5:].strip()
        if payload == "[DONE]":
            break
        try:
            evt = json.loads(payload)
        except json.JSONDecodeError:
            log.warning("bad SSE chunk: %r", payload[:200])
            continue

        choice = (evt.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}

        # usage-Deltas (manche Upstreams senden die am Ende)
        if "usage" in evt and evt["usage"]:
            u = evt["usage"]
            if u.get("prompt_tokens"):
                input_tokens = int(u["prompt_tokens"])
            if u.get("completion_tokens"):
                output_tokens = int(u["completion_tokens"])

        # Text
        if "content" in delta and delta["content"]:
            if not text_block_open:
                text_block_index = next_anthropic_index
                next_anthropic_index += 1
                out.append(_sse("content_block_start", {
                    "type": "content_block_start",
                    "index": text_block_index,
                    "content_block": {"type": "text", "text": ""},
                }))
                text_block_open = True
            out.append(_sse("content_block_delta", {
                "type": "content_block_delta",
                "index": text_block_index,
                "delta": {"type": "text_delta", "text": delta["content"]},
            }))

        # tool_calls in Stream puffern und am Ende emittieren
        for tc_delta in (delta.get("tool_calls") or []):
            tc_idx = tc_delta.get("index", 0)
            if tc_idx not in tool_buf:
                tool_buf[tc_idx] = {
                    "id": tc_delta.get("id") or _gen_block_id(),
                    "name": (tc_delta.get("function") or {}).get("name", ""),
                    "args": "",
                    "closed": False,
                    "anthropic_index": next_anthropic_index,
                }
                next_anthropic_index += 1
            blk = tool_buf[tc_idx]
            fn = tc_delta.get("function") or {}
            if fn.get("name"):
                blk["name"] = fn["name"]
            if fn.get("arguments"):
                blk["args"] += fn["arguments"]

        # finish_reason
        fr = choice.get("finish_reason")
        if fr:
            final_stop_reason = {
                "stop": "end_turn",
                "tool_calls": "tool_use",
                "length": "max_tokens",
                "content_filter": "end_turn",
            }.get(fr, "end_turn")

    if text_block_open:
        out.append(_sse("content_block_stop", {
            "type": "content_block_stop", "index": text_block_index,
        }))
        text_block_open = False
    for tc_idx in sorted(tool_buf.keys()):
        if not tool_buf[tc_idx]["closed"]:
            blk = tool_buf[tc_idx]
            ai = blk["anthropic_index"]
            out.append(_sse("content_block_start", {
                "type": "content_block_start", "index": ai,
                "content_block": {"type": "tool_use", "id": blk["id"], "name": blk["name"], "input": {}},
            }))
            if blk["args"]:
                out.append(_sse("content_block_delta", {
                    "type": "content_block_delta", "index": ai,
                    "delta": {"type": "input_json_delta", "partial_json": blk["args"]},
                }))
            out.append(_sse("content_block_stop", {
                "type": "content_block_stop", "index": ai,
            }))
            blk["closed"] = True

    if text_block_open:
        out.append(_sse("content_block_stop", {
            "type": "content_block_stop",
            "index": block_index,
        }))

    # message_delta mit stop_reason + final usage
    out.append(_sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": final_stop_reason or "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": output_tokens},
    }))

    # message_stop
    out.append(_sse("message_stop", {"type": "message_stop"}))

    return b"".join(out)


# ---- HTTP-Server ------------------------------------------------------------

class ShimHandler(BaseHTTPRequestHandler):
    server_version = "anthropic_openai_shim/0.1"

    # Logniveau leiser, damit jeder Request eine Zeile produziert
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        log.debug(fmt, *args)

    def _send_json(self, status: int, payload: dict[str, Any] | list[Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: int, msg: str, err_type: str = "api_error") -> None:
        self._send_json(status, {"type": "error", "error": {"type": err_type, "message": msg}})

    def _path(self) -> str:
        """Liefert den Pfad ohne Query-String. /v1/messages?beta=true → /v1/messages."""
        return self.path.split("?", 1)[0]

    def _bearer(self) -> str:
        """Extrahiert Token aus Authorization: Bearer ...; '' wenn fehlt oder anders."""
        h = self.headers.get("Authorization", "") or self.headers.get("authorization", "")
        if h.lower().startswith("bearer "):
            return h[7:].strip()
        return ""

    def do_GET(self) -> None:  # noqa: N802
        if self._path() == "/v1/models":
            # Forward an Upstream, damit die Modell-Liste aktuell bleibt.
            try:
                upstream = self._call_upstream("/v1/models", method="GET", body=None, stream=False)
            except urllib.error.URLError as e:
                self._send_error_json(502, f"upstream unreachable: {e}")
                return
            self._send_json(upstream["status"], upstream["json"])
            return
        if self._path() in ("/healthz", "/health"):
            self._send_json(200, {"status": "ok"})
            return
        self._send_error_json(404, f"unknown endpoint: {self.path}", err_type="not_found_error")

    def do_POST(self) -> None:  # noqa: N802
        if self._path() != "/v1/messages":
            self._send_error_json(404, f"unknown endpoint: {self.path}", err_type="not_found_error")
            return

        # Body lesen
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._send_error_json(400, "missing request body", err_type="invalid_request_error")
            return
        raw = self.rfile.read(length)
        try:
            ant_body = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            self._send_error_json(400, f"invalid json: {e}", err_type="invalid_request_error")
            return

        # API-Key: mehrere Quellen, in Reihenfolge der Spezifität:
        #   1. x-api-key-Header (Anthropic-Konvention)
        #   2. Authorization: Bearer ... (OpenAI-Konvention, schickt Claude Code scheinbar auch)
        #   3. Env SHIM_API_KEY (Fallback für Tests)
        api_key = (
            self.headers.get("x-api-key")
            or self._bearer()
            or SHIM_API_KEY
        )
        if not api_key:
            self._send_error_json(401, "missing x-api-key or Authorization Bearer", err_type="authentication_error")
            return

        stream = bool(ant_body.get("stream", False))
        oai_body = anthropic_to_openai_request(ant_body)
        n_tools = len(ant_body.get("tools") or [])
        n_tool_msgs = sum(1 for m in (ant_body.get("messages") or [])
                          if m.get("role") == "user" and isinstance(m.get("content"), list)
                          and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in m["content"]))
        log.info("POST /v1/messages model=%s stream=%s tools=%d tool_result_msgs=%d",
                 oai_body.get("model"), stream, n_tools, n_tool_msgs)

        if stream:
            self._handle_stream(oai_body, api_key, ant_body.get("model", ""))
        else:
            self._handle_non_stream(oai_body, api_key)

    def _call_upstream(self, path: str, method: str, body: dict[str, Any] | None,
                       stream: bool, api_key: str = "") -> dict[str, Any]:
        """Non-streaming-Call mit Retry-Loop gegen Free-Tier-Instabilität.

        Retry nur bei 5xx und transienten Verbindungsfehlern.
        4xx wird nicht wiederholt (Anfrage selbst war falsch).
        """
        url = SHIM_UPSTREAM.rstrip("/") + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        body_bytes = len(data) if data else 0
        log.debug("upstream %s %s body=%dB", method, path, body_bytes)

        backoffs = [0.5, 1.0, 2.0]  # 3 Versuche nach initialem Call
        attempt = 0
        last_error: str | None = None
        t_start = time.monotonic()

        while True:
            req = urllib.request.Request(
                url, data=data, method=method,
                headers={
                    "Authorization": f"Bearer {api_key or SHIM_API_KEY}",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream" if stream else "application/json",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    status = resp.status
                    ctype = resp.headers.get("Content-Type", "")
                    raw = resp.read()
                # Erfolg — Latenz loggen
                log.info("upstream %s %s -> %d body=%dB in %.2fs (attempt %d)",
                         method, path, status, body_bytes,
                         time.monotonic() - t_start, attempt + 1)
                if "json" in ctype:
                    try:
                        return {"status": status, "json": json.loads(raw.decode("utf-8"))}
                    except Exception:
                        return {"status": status, "json": {"raw": raw.decode("utf-8", errors="replace")}}
                return {"status": status, "json": {"raw": raw.decode("utf-8", errors="replace")}}
            except urllib.error.HTTPError as e:
                status = e.code
                raw = e.read() or b""
                # 4xx (außer 408/429) → kein Retry
                if 400 <= status < 500 and status not in (408, 429):
                    try:
                        return {"status": status, "json": json.loads(raw.decode("utf-8"))}
                    except Exception:
                        return {"status": status, "json": {"raw": raw.decode("utf-8", errors="replace")}}
                # 5xx oder 408/429 → retry wenn Versuche übrig
                last_error = f"HTTP {status}: {raw[:200].decode('utf-8', errors='replace')}"
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last_error = f"connection: {e}"

            # Retry-Entscheidung
            if attempt < len(backoffs):
                wait = backoffs[attempt]
                log.warning("upstream call failed (attempt %d/%d): %s — retry in %.1fs",
                            attempt + 1, len(backoffs) + 1, last_error, wait)
                time.sleep(wait)
                attempt += 1
                continue

            # Versuche aufgebraucht → 502 zurückgeben
            log.error("upstream call failed after %d attempts: %s", len(backoffs) + 1, last_error)
            return {"status": 502, "json": {"raw": last_error or "upstream unreachable"}}

    def _handle_non_stream(self, oai_body: dict[str, Any], api_key: str) -> None:
        # stream:false erzwingen für non-stream-Pfad
        oai_body = {**oai_body, "stream": False}
        try:
            up = self._call_upstream("/v1/chat/completions", "POST", oai_body, stream=False,
                                     api_key=api_key)
        except urllib.error.URLError as e:
            self._send_error_json(502, f"upstream unreachable: {e}")
            return
        if up["status"] >= 400:
            # Body in Anthropic-Error wrappen
            self._send_json(up["status"], {
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": json.dumps(up["json"])[:1000],
                },
            })
            return
        ant_msg = openai_to_anthropic_response(up["json"], _gen_id())
        self._send_json(200, ant_msg)

    def _handle_stream(self, oai_body: dict[str, Any], api_key: str, model: str) -> None:
        oai_body = {**oai_body, "stream": True}
        url = SHIM_UPSTREAM.rstrip("/") + "/v1/chat/completions"
        data = json.dumps(oai_body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={
                "Authorization": f"Bearer {api_key or SHIM_API_KEY}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
        )
        # Connect-Phase Retry: 1× bei 5xx/URLError, bevor der erste Chunk
        # an den Client geschickt wird. Sobald message_start raus ist, kein
        # Retry mehr — sonst duplizieren sich SSE-Events beim Client.
        t_connect_start = time.monotonic()
        t_connected: float | None = None
        upstream_resp = None
        last_err: str | None = None
        for attempt in range(2):  # 0, 1 → max 2 Connect-Versuche
            try:
                upstream_resp = urllib.request.urlopen(req, timeout=300)
                t_connected = time.monotonic()
                log.debug("upstream stream connected in %.2fs", t_connected - t_connect_start)
                last_err = None
                break
            except urllib.error.HTTPError as e:
                err_body = e.read() or b""
                last_err = f"HTTP {e.code}: {err_body[:200].decode('utf-8', errors='replace')}"
                if 400 <= e.code < 500 and e.code not in (408, 429):
                    # 4xx (außer 408/429) → direkt an Client durchreichen
                    self.send_response(e.code)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "type": "error",
                        "error": {"type": "api_error", "message": err_body.decode("utf-8", errors="replace")[:1000]},
                    }).encode("utf-8"))
                    return
                if attempt == 0:
                    log.warning("upstream stream connect failed (attempt 1/2): %s — retry in 1.0s", last_err)
                    time.sleep(1.0)
                    continue
            except urllib.error.URLError as e:
                last_err = f"connection: {e}"
                if attempt == 0:
                    log.warning("upstream stream connect failed (attempt 1/2): %s — retry in 1.0s", last_err)
                    time.sleep(1.0)
                    continue
        if upstream_resp is None:
            log.error("upstream stream connect failed after 2 attempts: %s", last_err)
            self._send_error_json(502, last_err or "upstream unreachable")
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        msg_id = _gen_id()
        # OpenAI SSE konsumieren, Zeile für Zeile.
        # Da OpenAI-SSE zeilenbasiert ist und wir sofort flushen wollen,
        # lesen wir raw und parsen on-the-fly.
        # Tool-Call-Puffer: pro OpenAI-tool-call-index sammeln wir
        # {id, name, arguments_acc} bis zum Streamende, dann emittieren
        # wir content_block_start/-delta/-stop fuer jeden.
        tool_buf: dict[int, dict[str, Any]] = {}
        next_anthropic_index = 0
        text_block_open = False
        text_block_index = 0
        output_tokens = 0
        final_stop_reason: str | None = None
        leftover = b""

        def _flush_tool_block(tc_idx: int) -> None:
            """Emittiert content_block_start/-delta/-stop fuer einen gepufferten Tool-Call."""
            nonlocal text_block_open
            blk = tool_buf.get(tc_idx)
            if not blk:
                return
            # Offenen Text-Block (falls existent) zuerst schliessen.
            if text_block_open:
                self.wfile.write(_sse("content_block_stop", {
                    "type": "content_block_stop", "index": text_block_index,
                }))
                self.wfile.flush()
                text_block_open = False
            ai = blk["anthropic_index"]
            self.wfile.write(_sse("content_block_start", {
                "type": "content_block_start",
                "index": ai,
                "content_block": {
                    "type": "tool_use",
                    "id": blk["id"],
                    "name": blk["name"],
                    "input": {},
                },
            }))
            self.wfile.flush()
            if blk["args"]:
                self.wfile.write(_sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": ai,
                    "delta": {"type": "input_json_delta", "partial_json": blk["args"]},
                }))
                self.wfile.flush()
            self.wfile.write(_sse("content_block_stop", {
                "type": "content_block_stop", "index": ai,
            }))
            self.wfile.flush()
            blk["closed"] = True

        # message_start
        t_first_event = time.monotonic() if t_connected else None
        self.wfile.write(_sse("message_start", {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": model,
                "stop_reason": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        }))
        self.wfile.flush()

        try:
            n_chunks = 0
            n_bytes = 0
            n_events = 0
            t_ttft: float | None = None
            # Hard-Limit fuer leftover: schuetzt vor Runaway-Memory, falls der
            # Upstream nie ein \n liefert (z. B. abgestuerzter Stream).
            LEFTOVER_MAX = 1 * 1024 * 1024  # 1 MiB
            while True:
                chunk = upstream_resp.read(4096)
                if not chunk:
                    break
                n_chunks += 1
                n_bytes += len(chunk)
                leftover += chunk
                if len(leftover) > LEFTOVER_MAX:
                    log.error("upstream stream leftover exceeded %d bytes without newline — aborting", LEFTOVER_MAX)
                    raise ValueError("leftover overflow")
                # SSE-Trennung ist \n\n zwischen Events; ein Event kann über Chunk-Grenzen gehen.
                while b"\n" in leftover:
                    line, leftover = leftover.split(b"\n", 1)
                    line = line.decode("utf-8", errors="replace").rstrip("\r")
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        raise StopIteration
                    try:
                        evt = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    n_events += 1
                    if t_ttft is None:
                        t_ttft = time.monotonic() - (t_first_event or t_connected or time.monotonic())
                    choice = (evt.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    # Text-Deltas
                    if "content" in delta and delta["content"]:
                        if not text_block_open:
                            text_block_index = next_anthropic_index
                            next_anthropic_index += 1
                            self.wfile.write(_sse("content_block_start", {
                                "type": "content_block_start",
                                "index": text_block_index,
                                "content_block": {"type": "text", "text": ""},
                            }))
                            self.wfile.flush()
                            text_block_open = True
                        self.wfile.write(_sse("content_block_delta", {
                            "type": "content_block_delta",
                            "index": text_block_index,
                            "delta": {"type": "text_delta", "text": delta["content"]},
                        }))
                        self.wfile.flush()
                    # Tool-Call-Deltas puffern
                    for tc_delta in (delta.get("tool_calls") or []):
                        tc_idx = tc_delta.get("index", 0)
                        if tc_idx not in tool_buf:
                            tool_buf[tc_idx] = {
                                "id": tc_delta.get("id") or _gen_block_id(),
                                "name": (tc_delta.get("function") or {}).get("name", ""),
                                "args": "",
                                "closed": False,
                                "anthropic_index": next_anthropic_index,
                            }
                            next_anthropic_index += 1
                        blk = tool_buf[tc_idx]
                        fn = tc_delta.get("function") or {}
                        if fn.get("name"):
                            blk["name"] = fn["name"]
                        if fn.get("arguments"):
                            blk["args"] += fn["arguments"]
                    fr = choice.get("finish_reason")
                    if fr:
                        final_stop_reason = {
                            "stop": "end_turn",
                            "tool_calls": "tool_use",
                            "length": "max_tokens",
                            "content_filter": "end_turn",
                        }.get(fr, "end_turn")
                    u = evt.get("usage")
                    if u and u.get("completion_tokens"):
                        output_tokens = int(u["completion_tokens"])
        except StopIteration:
            pass
        finally:
            t_stream_end = time.monotonic()
            t_total = (t_stream_end - t_connected) if t_connected else 0.0
            # Stream-Summary auf INFO — wichtigste Performance-Metrik.
            # body_bytes (Request) fehlt hier; chunks+events sind die Antwortseite.
            log.info("stream done: chunks=%d events=%d ttft=%.2fs total=%.2fs tool_blocks=%d",
                     n_chunks, n_events,
                     t_ttft or 0.0, t_total, len(tool_buf))
            upstream_resp.close()

        # Offene Text-Blöcke schliessen, dann Tool-Calls in OpenAI-Index-Reihenfolge.
        if text_block_open:
            self.wfile.write(_sse("content_block_stop", {
                "type": "content_block_stop", "index": text_block_index,
            }))
            text_block_open = False
        for tc_idx in sorted(tool_buf.keys()):
            if not tool_buf[tc_idx]["closed"]:
                _flush_tool_block(tc_idx)
        self.wfile.write(_sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": final_stop_reason or "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": output_tokens},
        }))
        self.wfile.write(_sse("message_stop", {"type": "message_stop"}))
        self.wfile.flush()


# ---- main -------------------------------------------------------------------

def main() -> int:
    httpd = ThreadingHTTPServer(("127.0.0.1", SHIM_PORT), ShimHandler)
    log.info("shim listening on 127.0.0.1:%d, upstream=%s, log_level=%s",
             SHIM_PORT, SHIM_UPSTREAM, SHIM_LOG_LEVEL)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("shim shutting down")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
