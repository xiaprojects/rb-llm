"""rb-llm HTTP API + web UI (Python standard library only, so the Pi needs no pip packages).

Routes (protocol rb-llm/1, see docs/protocol.md):
  GET  /                    web UI (web/index.html)
  GET  /v1/health           readiness, engine state, SoC temperature, throttling
  GET  /v1/info             versions, tasks, languages, model
  GET  /v1/status           aircraft status facts as the model sees them (?source=auto|rb|demo[&raw=1])
  POST /v1/status           facts for a posted RB-01 snapshot (web UI preview)
  GET  /v1/catalog/{lang}   catalog hash and size
  PUT  /v1/catalog          upload an rbvoice case file
  POST /v1/parse            command / atc / auto
  POST /v1/ask              question about the aircraft status
"""
from __future__ import annotations

import json
import mimetypes
import os
import subprocess
import sys
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

from . import PROTOCOL, __version__, tasks
from .catalog import Catalog, CatalogStore
from .llama_client import LlamaClient
from .status import StatusProvider, build_status

LANGS = ("it", "en", "fr", "es", "de")
ERROR_HTTP = {"timeout": 504, "busy": 503, "loading": 503, "engine_failure": 500, "internal": 500,
              "bad_request": 400, "catalog_mismatch": 409, "unsupported_lang": 422, "unsupported_task": 422}
MAX_BODY = 2 * 1024 * 1024


class App:
    def __init__(self, engine: LlamaClient, catalogs: CatalogStore, status: StatusProvider,
                 web_dir: str, model_id: str = "", llama_version: str = "") -> None:
        self.engine = engine
        self.catalogs = catalogs
        self.status = status
        self.web_dir = web_dir
        self.model_id = model_id
        self.llama_version = llama_version
        self.started = time.time()
        self._validators = self._load_validators()

    # optional request validation with jsonschema (dev machines); the Pi runs without it
    def _load_validators(self) -> Dict[str, Any]:
        try:
            import glob
            import jsonschema
            from referencing import Registry, Resource
        except ImportError:
            return {}
        root = os.path.join(os.path.dirname(self.web_dir), "schemas")
        reg = Registry()
        for p in glob.glob(os.path.join(root, "*.json")):
            with open(p, encoding="utf-8") as f:
                s = json.load(f)
            reg = reg.with_resource(s["$id"], Resource.from_contents(s))
        base = "https://rbavionics.com/schemas/rb-llm/1/"
        out = {}
        for name in ("parse-request", "ask-request"):
            try:
                out[name] = jsonschema.Draft202012Validator({"$ref": f"{base}{name}.schema.json"}, registry=reg)
            except Exception:  # noqa: BLE001
                pass
        return out

    def warmup(self, lang: str) -> None:
        """Fill each task's slot with its static prompt (cold prefill is the slowest step).

        Runs in a thread after start-up. Sends a 1-token request per task: the output is
        truncated on purpose, only the prompt processing matters.
        """
        from . import prompts
        from .llama_client import EngineError

        for _ in range(600):
            if self.engine.health() == "ok":
                break
            time.sleep(0.5)
        t0 = time.monotonic()
        jobs = [("ask", prompts.ask_messages(lang, "gps.fix: warming up", "warm up"), prompts.ask_schema(["gps.fix"]))]
        cat = self.catalogs.get(lang)
        if cat:
            jobs.append(("command", prompts.command_messages(cat.lang, cat.prompt_section(), "warm up", None,
                                                             cat.param_keys()), prompts.command_schema(cat.param_keys())))
        jobs.append(("atc", prompts.atc_messages("warm up", None), prompts.atc_schema()))
        for task, msgs, schema in jobs:
            try:
                self.engine.complete_json(msgs, schema, 1, time.monotonic() + 300, tasks.SLOT[task])
            except EngineError:
                pass  # truncated JSON is expected
        sys.stderr.write(f"warm-up ({lang}: {', '.join(j[0] for j in jobs)}) done in {time.monotonic() - t0:.1f}s\n")

    def validate(self, name: str, body: Dict[str, Any]) -> Optional[str]:
        v = self._validators.get(name)
        if v is None:
            return None
        errs = sorted(v.iter_errors(body), key=lambda e: list(e.path))
        if not errs:
            return None
        return "; ".join(f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errs[:3])

    # --- handlers ---------------------------------------------------------------------------
    def health(self) -> Tuple[int, Dict[str, Any]]:
        eng = self.engine.health()
        state = {"ok": "ready", "loading": "loading"}.get(eng, "degraded")
        out: Dict[str, Any] = {"status": state, "uptime_s": int(time.time() - self.started),
                               "models_loaded": [self.model_id] if eng == "ok" and self.model_id else [],
                               "catalogs": self.catalogs.hashes(), "queue_depth": 0}
        temp = soc_temperature()
        if temp is not None:
            out["soc_temp_c"] = temp
        thr = throttled()
        if thr is not None:
            out["throttled"] = thr
        return 200, out

    def info(self) -> Tuple[int, Dict[str, Any]]:
        cats = sorted(self.catalogs.hashes())
        return 200, {"name": "rb-llm", "version": __version__, "protocols": [PROTOCOL],
                     "tasks": ["command", "atc", "ask"],
                     "langs": {"command": cats, "atc": ["en", "it"], "ask": list(LANGS)},
                     "engine": self.llama_version or "llama.cpp",
                     "models": [{"id": self.model_id or "unknown", "task": "all"}]}

    def get_status(self, source: str, raw: bool = False) -> Tuple[int, Dict[str, Any]]:
        try:
            snap, src = self.status.snapshot(source)
        except ConnectionError as e:
            return 502, {"error": {"code": "engine_failure", "message": str(e)}}
        if raw:
            return 200, {"source": src, "snapshot": snap}
        return 200, build_status(snap, src).to_json()

    def put_catalog(self, body: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        cf = body.get("case_file")
        if not isinstance(cf, dict) or "components" not in cf:
            return 400, {"error": {"code": "bad_request", "message": "case_file with components required"}}
        cf = dict(cf)
        cf.setdefault("language", body.get("lang", ""))
        cat = Catalog.from_case_file(cf, source=body.get("source", "PUT"))
        if cat.lang not in LANGS:
            return 422, {"error": {"code": "unsupported_lang", "message": f"language {cat.lang!r}"}}
        self.catalogs.put(cat)
        return 200, {"protocol": PROTOCOL, "warnings": [], **cat.stats()}

    def parse(self, req: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        req.setdefault("request_id", str(uuid.uuid4()))
        t0 = time.monotonic()
        bad = self.validate("parse-request", req) or _basic_check(req)
        if bad:
            return 400, tasks._error(req, t0, "bad_request", bad)
        lang = req["lang"]
        catalog = self.catalogs.get(lang)
        task = req.get("task", "auto")
        if task == "auto":
            task = tasks.classify(req, catalog)
        if task == "command":
            if catalog is None:
                return 422, tasks._error(req, t0, "unsupported_lang", f"no catalog for {lang}: PUT /v1/catalog")
            if req.get("catalog_hash") and req["catalog_hash"] != catalog.hash:
                return 409, tasks._error(req, t0, "catalog_mismatch", f"server has {catalog.hash}")
            resp = tasks.run_command(req, self.engine, catalog)
        elif task == "atc":
            resp = tasks.run_atc(req, self.engine)
        else:
            return 422, tasks._error(req, t0, "unsupported_task", task)
        return _http_for(resp), resp

    def ask(self, req: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        req.setdefault("request_id", str(uuid.uuid4()))
        t0 = time.monotonic()
        bad = self.validate("ask-request", req) or _basic_check(req)
        if bad:
            return 400, tasks._error(req, t0, "bad_request", bad)
        if isinstance(req.get("aircraft_status"), dict):
            status = build_status(req["aircraft_status"], "request")
        else:
            try:
                snap, src = self.status.snapshot((req.get("options") or {}).get("status_source", "auto"))
            except ConnectionError as e:
                return 502, tasks._error(req, t0, "engine_failure", str(e))
            status = build_status(snap, src)
        resp = tasks.run_ask(req, self.engine, status)
        return _http_for(resp), resp


def _basic_check(req: Dict[str, Any]) -> Optional[str]:
    if not isinstance(req.get("text"), str) or not req["text"].strip():
        return "text: required"
    if len(req["text"]) > 500:
        return "text: longer than 500 characters"
    if req.get("lang") not in LANGS:
        return f"lang: one of {', '.join(LANGS)}"
    return None


def _http_for(resp: Dict[str, Any]) -> int:
    if resp.get("status") == "error":
        return ERROR_HTTP.get((resp.get("error") or {}).get("code", ""), 500)
    return 200


def soc_temperature() -> Optional[float]:
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read().strip()) / 1000.0, 1)
    except (OSError, ValueError):
        return None


def throttled() -> Optional[bool]:
    """Pi only: any *current* under-voltage / throttling / frequency-cap flag (bits 0-3)."""
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=1).stdout
        return (int(out.strip().split("=")[1], 16) & 0xF) != 0
    except (OSError, IndexError, ValueError, subprocess.SubprocessError):
        return None


class Handler(BaseHTTPRequestHandler):
    app: App  # set by serve()
    server_version = f"rb-llm/{__version__}"

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter default log
        pass

    def _send(self, code: int, body: Any, ctype: str = "application/json; charset=utf-8") -> None:
        data = body if isinstance(body, bytes) else tasks.dumps(body)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> Optional[Dict[str, Any]]:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_BODY:
            return None
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return body if isinstance(body, dict) else None

    def _log(self, code: int, t0: float, extra: str = "") -> None:
        ms = int((time.monotonic() - t0) * 1000)
        sys.stderr.write(f"{time.strftime('%H:%M:%S')} {self.command} {self.path} {code} {ms}ms {extra}\n")

    def do_GET(self) -> None:  # noqa: N802
        t0 = time.monotonic()
        path, _, query = self.path.partition("?")
        app = self.app
        if path == "/v1/health":
            code, body = app.health()
        elif path == "/v1/info":
            code, body = app.info()
        elif path == "/v1/status":
            q = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
            code, body = app.get_status(q.get("source", "auto"), q.get("raw") == "1")
        elif path.startswith("/v1/catalog/"):
            cat = app.catalogs.get(path.rsplit("/", 1)[1])
            code, body = (200, {"protocol": PROTOCOL, **cat.stats()}) if cat else \
                (404, {"error": {"code": "unsupported_lang", "message": "no catalog"}})
        else:
            self._static(path)
            return
        self._send(code, body)
        if not path.endswith("/health"):
            self._log(code, t0)

    def _static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
        if rel.startswith("web/"):
            rel = rel[4:]
        full = os.path.realpath(os.path.join(self.app.web_dir, rel))
        if not full.startswith(os.path.realpath(self.app.web_dir) + os.sep) or not os.path.isfile(full):
            self._send(404, {"error": {"code": "bad_request", "message": "not found"}})
            return
        with open(full, "rb") as f:
            data = f.read()
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self._send(200, data, ctype)

    def do_PUT(self) -> None:  # noqa: N802
        t0 = time.monotonic()
        body = self._body()
        if self.path != "/v1/catalog":
            code, resp = 404, {"error": {"code": "bad_request", "message": "not found"}}
        elif body is None:
            code, resp = 400, {"error": {"code": "bad_request", "message": "JSON object body required"}}
        else:
            code, resp = self.app.put_catalog(body)
        self._send(code, resp)
        self._log(code, t0, resp.get("catalog_hash", ""))

    def do_POST(self) -> None:  # noqa: N802
        t0 = time.monotonic()
        body = self._body()
        if body is None:
            self._send(400, {"error": {"code": "bad_request", "message": "JSON object body required"}})
            return
        try:
            if self.path == "/v1/parse":
                code, resp = self.app.parse(body)
            elif self.path == "/v1/status":  # preview facts for a custom snapshot (web UI)
                code, resp = 200, build_status(body, "request").to_json()
            elif self.path == "/v1/ask":
                code, resp = self.app.ask(body)
            else:
                self._send(404, {"error": {"code": "bad_request", "message": "not found"}})
                return
        except Exception as e:  # noqa: BLE001 - never crash the server on one request
            code, resp = 500, tasks._error(body, t0, "internal", f"{type(e).__name__}: {e}")
        self._send(code, resp)
        self._log(code, t0, f"{resp.get('kind', '')} {resp.get('status', '')} "
                            f"{(resp.get('engine') or {}).get('latency_ms', '')}ms-engine")


def serve(app: App, host: str, port: int) -> None:
    Handler.app = app
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    sys.stderr.write(f"rb-llm {__version__} listening on http://{host}:{port}  (engine {app.engine.base_url}, "
                     f"catalogs {sorted(app.catalogs.hashes()) or 'none'}, model {app.model_id or '?'})\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def start_background(app: App, host: str = "127.0.0.1", port: int = 0) -> Tuple[ThreadingHTTPServer, threading.Thread]:
    """For tests: run the server in a thread, return (server, thread). port=0 picks a free port."""
    Handler.app = app
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    return httpd, th
