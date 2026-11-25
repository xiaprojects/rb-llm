"""Shared test helpers: a fake engine (no model needed) and schema validation."""
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rbllm.llama_client import Completion, EngineError  # noqa: E402

DEMO = os.path.join(ROOT, "data", "demo-rb01.json")
CATALOG_DIR = os.path.join(ROOT, "catalog")
if not os.path.isdir(CATALOG_DIR) or not glob.glob(os.path.join(CATALOG_DIR, "rbvoice-cases.*.json")):
    CATALOG_DIR = os.path.join(ROOT, "tests", "fixtures")


class FakeEngine:
    """Returns queued outputs (dicts) or raises queued EngineErrors; records every call."""

    base_url = "http://fake"
    model_name = "fake-model"

    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []

    def health(self, timeout=1.0):
        return "ok"

    def complete_json(self, messages, schema, max_tokens, deadline, slot=-1):
        self.calls.append({"messages": messages, "schema": schema, "slot": slot})
        out = self.outputs.pop(0) if self.outputs else {}
        if isinstance(out, EngineError):
            raise out
        return Completion(data=out, raw=json.dumps(out), latency_ms=5, tokens_in=100, tokens_out=10,
                          cached_tokens=90, tok_per_s=10.0, model=self.model_name)


def validator(schema_ref):
    """jsonschema validator for e.g. 'parse-response.schema.json', or None if jsonschema is absent."""
    try:
        import jsonschema
        from referencing import Registry, Resource
    except ImportError:
        return None
    reg = Registry()
    for p in glob.glob(os.path.join(ROOT, "schemas", "*.json")):
        with open(p, encoding="utf-8") as f:
            s = json.load(f)
        reg = reg.with_resource(s["$id"], Resource.from_contents(s))
    return jsonschema.Draft202012Validator(
        {"$ref": "https://rbavionics.com/schemas/rb-llm/1/" + schema_ref}, registry=reg)


def assert_valid(test, schema_ref, obj):
    v = validator(schema_ref)
    if v is None:
        return
    errs = [f"{'/'.join(map(str, e.path))}: {e.message}" for e in v.iter_errors(obj)]
    test.assertEqual(errs, [], json.dumps(obj, indent=1)[:2000])
