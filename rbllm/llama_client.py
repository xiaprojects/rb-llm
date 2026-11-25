"""Client for llama.cpp's llama-server (OpenAI-compatible chat endpoint + JSON-schema constraint).

Every call:
- sends a JSON schema, which llama-server compiles into a grammar: the output always parses
  and every enum value is valid (no "please answer in JSON" hoping);
- disables "thinking" for hybrid models (Qwen3): thinking costs hundreds of tokens;
- uses greedy sampling and `cache_prompt`, so the static system prompt stays in the KV cache.
"""
from __future__ import annotations

import json
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


class EngineError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code  # timeout | busy | loading | engine_failure


@dataclass
class Completion:
    data: Dict[str, Any]
    raw: str
    latency_ms: int
    queue_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cached_tokens: int = 0
    tok_per_s: float = 0.0
    model: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


class LlamaClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8080", model_name: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.model_name = model_name
        self._lock = threading.Lock()  # llama-server runs with --parallel 1: one request at a time

    # --- health -----------------------------------------------------------------------------
    def health(self, timeout: float = 1.0) -> str:
        """'ok' | 'loading' | 'down'"""
        try:
            with urllib.request.urlopen(self.base_url + "/health", timeout=timeout) as r:
                return "ok" if r.status == 200 else "loading"
        except urllib.error.HTTPError as e:
            return "loading" if e.code == 503 else "down"
        except Exception:  # noqa: BLE001
            return "down"

    def props(self, timeout: float = 1.0) -> Dict[str, Any]:
        try:
            with urllib.request.urlopen(self.base_url + "/props", timeout=timeout) as r:
                return json.load(r)
        except Exception:  # noqa: BLE001
            return {}

    # --- completion ---------------------------------------------------------------------------
    def complete_json(self, messages: List[Dict[str, str]], schema: Dict[str, Any],
                      max_tokens: int, deadline: float, slot: int = -1) -> Completion:
        """Run one constrained chat completion. `deadline` is a time.monotonic() value.

        `slot` pins the request to a llama-server slot (one per task), so each task's static
        prompt stays in that slot's KV cache instead of being evicted by the other tasks.
        """
        t0 = time.monotonic()
        if not self._lock.acquire(timeout=max(0.0, deadline - t0)):
            raise EngineError("busy", "engine busy until the deadline")
        try:
            queue_ms = int((time.monotonic() - t0) * 1000)
            remaining = deadline - time.monotonic()
            if remaining <= 0.05:
                raise EngineError("timeout", "deadline expired while queued")
            payload = {
                "model": self.model_name or "rb-llm",
                "messages": messages,
                "temperature": 0.0,
                "top_k": 1,
                "max_tokens": max_tokens,
                "cache_prompt": True,
                "response_format": {"type": "json_schema", "json_schema": {"name": "rb", "schema": schema}},
                "chat_template_kwargs": {"enable_thinking": False},
                # llama-server stops generating after this many ms (keeps the slot free for the next one)
                "t_max_predict_ms": int(remaining * 1000),
            }
            if slot >= 0:
                payload["id_slot"] = slot
            # Streaming on purpose: when we close the stream at the deadline, llama-server cancels the
            # task. A plain request keeps running server-side after a client timeout, and abandoned
            # requests then queue up in front of every new one.
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
            req = urllib.request.Request(self.base_url + "/v1/chat/completions",
                                         data=json.dumps(payload).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            t1 = time.monotonic()
            parts: List[str] = []
            body: Dict[str, Any] = {}
            try:
                with urllib.request.urlopen(req, timeout=remaining) as r:
                    for line in r:
                        if time.monotonic() > deadline:
                            raise EngineError("timeout", "deadline expired during generation")
                        line = line.strip()
                        if not line.startswith(b"data:"):
                            continue
                        chunk = line[5:].strip()
                        if chunk == b"[DONE]":
                            break
                        ev = json.loads(chunk)
                        for ch in ev.get("choices") or []:
                            parts.append((ch.get("delta") or {}).get("content") or "")
                        for k in ("timings", "usage", "model"):
                            if ev.get(k):
                                body[k] = ev[k]
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                if e.code == 503:
                    raise EngineError("loading", f"llama-server loading: {detail}") from e
                raise EngineError("engine_failure", f"llama-server HTTP {e.code}: {detail}") from e
            except (socket.timeout, TimeoutError) as e:
                raise EngineError("timeout", "deadline expired during generation") from e
            except urllib.error.URLError as e:
                if isinstance(e.reason, (socket.timeout, TimeoutError)):
                    raise EngineError("timeout", "deadline expired during generation") from e
                raise EngineError("engine_failure", f"llama-server unreachable: {e.reason}") from e
            latency_ms = int((time.monotonic() - t1) * 1000)
        finally:
            self._lock.release()

        raw = _THINK.sub("", "".join(parts)).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            # Should be impossible with a grammar; only a truncated output (max_tokens / t_max) gets here
            raise EngineError("engine_failure", f"model output is not JSON (truncated?): {raw[:200]!r}") from e
        timings = body.get("timings") or {}
        usage = body.get("usage") or {}
        return Completion(
            data=data, raw=raw, latency_ms=latency_ms, queue_ms=queue_ms,
            tokens_in=int(usage.get("prompt_tokens") or timings.get("prompt_n") or 0),
            tokens_out=int(usage.get("completion_tokens") or timings.get("predicted_n") or 0),
            cached_tokens=int(timings.get("cache_n") or 0),
            tok_per_s=round(float(timings.get("predicted_per_second") or 0.0), 1),
            model=body.get("model") or self.model_name,
            extra={"prompt_ms": round(float(timings.get("prompt_ms") or 0)),
                   "predicted_ms": round(float(timings.get("predicted_ms") or 0))},
        )
