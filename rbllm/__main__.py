"""rb-llm command line.

  python3 -m rbllm serve [--port 8091] [--llama-url ...] [--rb-url http://<rb01>] [--demo-status FILE]
  python3 -m rbllm ask "where are we?" [--lang en] [--url http://127.0.0.1:8091]
  python3 -m rbllm parse "computer what are the cylinder temperatures" [--task auto|command|atc]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

import threading

from . import PROTOCOL, tasks
from .catalog import CatalogStore
from .llama_client import LlamaClient
from .server import App, serve
from .status import StatusProvider

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def cmd_serve(a: argparse.Namespace) -> int:
    catalogs = CatalogStore()
    for d in a.catalog_dir:
        if os.path.isdir(d):
            catalogs.load_dir(d)
    model_id = a.model_id or _read(os.path.join(ROOT, "models", "current.id"))
    llama_version = _read(os.path.join(ROOT, "bin", "llama.version")).replace("\n", " ")
    app = App(engine=LlamaClient(a.llama_url, model_id),
              catalogs=catalogs,
              status=StatusProvider(rb_url=a.rb_url, demo_path=a.demo_status, airfields_path=a.airfields),
              web_dir=os.path.join(ROOT, "web"), model_id=model_id, llama_version=llama_version)
    if a.deadline_ms:
        for k in tasks.DEFAULT_DEADLINE_MS:
            tasks.DEFAULT_DEADLINE_MS[k] = a.deadline_ms
    if a.warm_lang != "none":
        threading.Thread(target=app.warmup, args=(a.warm_lang,), daemon=True).start()
    serve(app, a.host, a.port)
    return 0


def _post(url: str, body: dict) -> int:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            out = json.load(r)
    except urllib.error.HTTPError as e:
        out = json.loads(e.read() or b"{}")
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0 if out.get("status") in ("ok", "not_addressed", "no_match") else 1


def cmd_ask(a: argparse.Namespace) -> int:
    return _post(a.url.rstrip("/") + "/v1/ask", {"protocol": PROTOCOL, "request_id": str(uuid.uuid4()),
                                                   "lang": a.lang, "text": a.text,
                                                   "options": {"debug": a.debug, "status_source": a.source}})


def cmd_parse(a: argparse.Namespace) -> int:
    ctx = {}
    if a.callsign:
        ctx["own_callsign"] = {"registration": a.callsign}
    return _post(a.url.rstrip("/") + "/v1/parse", {"protocol": PROTOCOL, "request_id": str(uuid.uuid4()),
                                                     "lang": a.lang, "text": a.text, "task": a.task,
                                                     "context": ctx, "options": {"debug": a.debug}})


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="rbllm")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the API + web UI")
    s.add_argument("--host", default=os.environ.get("RBLLM_HOST", "0.0.0.0"))
    s.add_argument("--port", type=int, default=int(os.environ.get("RBLLM_PORT", "8091")))
    s.add_argument("--llama-url", default=os.environ.get("RBLLM_LLAMA_URL", "http://127.0.0.1:8080"))
    s.add_argument("--catalog-dir", action="append",
                   default=[d for d in os.environ.get("RBLLM_CATALOG_DIR", "").split(":") if d]
                   or [os.path.join(ROOT, "catalog"), "/boot/firmware/rb"])
    s.add_argument("--rb-url", default=os.environ.get("RBLLM_RB_URL", ""),
                   help="RB-01 base URL for live aircraft status, e.g. http://192.168.10.1")
    s.add_argument("--demo-status", default=os.environ.get("RBLLM_DEMO_STATUS", ""),
                   help="RB-01 snapshot JSON used when no live RB-01 (data/demo-rb01.json)")
    s.add_argument("--airfields", default=os.environ.get("RBLLM_AIRFIELDS", ""),
                   help="db.airfields.json (default: fetched from --rb-url)")
    s.add_argument("--model-id", default=os.environ.get("RBLLM_MODEL_ID", ""))
    s.add_argument("--warm-lang", default=os.environ.get("RBLLM_WARM_LANG", "it"),
                   help="language whose prompts are pre-loaded in the KV cache at start-up ('none' to skip)")
    s.add_argument("--deadline-ms", type=int, default=int(os.environ.get("RBLLM_DEADLINE_MS", "0")),
                   help="default deadline for requests without options.deadline_ms (0 = per-task defaults)")
    s.set_defaults(fn=cmd_serve)

    for name, fn in (("ask", cmd_ask), ("parse", cmd_parse)):
        c = sub.add_parser(name)
        c.add_argument("text")
        c.add_argument("--lang", default="en")
        c.add_argument("--url", default="http://127.0.0.1:8091")
        c.add_argument("--debug", action="store_true")
        if name == "ask":
            c.add_argument("--source", default="auto", choices=["auto", "rb", "demo"])
        else:
            c.add_argument("--task", default="auto", choices=["auto", "command", "atc"])
            c.add_argument("--callsign", default="", help="own registration, e.g. I-ABCD")
        c.set_defaults(fn=fn)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
