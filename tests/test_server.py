"""HTTP tests: the real server on a free port, with a fake engine."""
import json
import os
import unittest
import urllib.error
import urllib.request

from helpers import CATALOG_DIR, DEMO, ROOT, FakeEngine, assert_valid

from rbllm.catalog import CatalogStore
from rbllm.server import App, start_background
from rbllm.status import StatusProvider


class TestServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cats = CatalogStore()
        cats.load_dir(CATALOG_DIR)
        cls.engine = FakeEngine()
        cls.app = App(cls.engine, cats, StatusProvider(demo_path=DEMO), os.path.join(ROOT, "web"), "fake-model")
        cls.httpd, _ = start_background(cls.app)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
                return r.status, r.headers.get("Content-Type", ""), raw
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type", ""), e.read()

    def json(self, method, path, body=None):
        code, _, raw = self.call(method, path, body)
        return code, json.loads(raw)

    def test_health_info(self):
        code, h = self.json("GET", "/v1/health")
        self.assertEqual((code, h["status"]), (200, "ready"))
        assert_valid(self, "health.schema.json#/$defs/health", h)
        code, i = self.json("GET", "/v1/info")
        assert_valid(self, "health.schema.json#/$defs/info", i)

    def test_status(self):
        code, s = self.json("GET", "/v1/status?source=demo")
        self.assertEqual(s["source"], "demo")
        self.assertTrue(any(f["key"] == "gps.position" for f in s["facts"]))
        code, raw = self.json("GET", "/v1/status?source=demo&raw=1")
        self.assertIn("/getSituation", raw["snapshot"])

    def test_web_ui(self):
        code, ctype, body = self.call("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(b"rb-llm", body)
        self.assertEqual(self.call("GET", "/web/app.js")[0], 200)
        self.assertEqual(self.call("GET", "/web/../rbllm/server.py")[0], 404)

    def test_parse_command(self):
        self.engine.outputs = [{"action": "GET", "param": "ems.cht"}]
        code, r = self.json("POST", "/v1/parse", {"protocol": "rb-llm/1", "request_id": "1", "lang": "en",
                                                 "text": "computer what are the cylinder temperatures",
                                                 "context": {"wake_word": "computer"}})
        self.assertEqual((code, r["status"], r["command"]["parameter"]), (200, "ok", "cht"))
        assert_valid(self, "parse-response.schema.json", r)

    def test_parse_examples_requests_accepted(self):
        for name in ("01-command-ems-request", "02-command-radio-station-request", "04-atc-clearance-request"):
            with open(os.path.join(ROOT, "examples", name + ".json"), encoding="utf-8") as f:
                body = json.load(f)["body"]
            body.pop("catalog_hash", None)
            self.engine.outputs = [{"action": "GET", "param": "none"}] if "atc" not in name else \
                [{"kind": "other", "callsign": None, "station": None, "instructions": []}]
            code, r = self.json("POST", "/v1/parse", body)
            self.assertIn(code, (200,), name)
            assert_valid(self, "parse-response.schema.json", r)

    def test_catalog_mismatch(self):
        code, r = self.json("POST", "/v1/parse", {"protocol": "rb-llm/1", "request_id": "1", "lang": "en",
                                                 "text": "computer cht", "task": "command", "catalog_hash": "0" * 16})
        self.assertEqual((code, r["error"]["code"]), (409, "catalog_mismatch"))

    def test_bad_request(self):
        code, r = self.json("POST", "/v1/parse", {"protocol": "rb-llm/1", "request_id": "1", "lang": "xx", "text": "hi"})
        self.assertEqual((code, r["error"]["code"]), (400, "bad_request"))

    def test_put_catalog(self):
        with open(os.path.join(CATALOG_DIR, "rbvoice-cases.en.json"), encoding="utf-8") as f:
            cf = json.load(f)
        code, r = self.json("PUT", "/v1/catalog", {"protocol": "rb-llm/1", "lang": "en", "case_file": cf})
        self.assertEqual(code, 200)
        assert_valid(self, "catalog.schema.json#/$defs/put_response", r)

    def test_ask_with_posted_status(self):
        with open(DEMO, encoding="utf-8") as f:
            snap = json.load(f)
        self.engine.outputs = [{"facts": ["gps.position"], "answer": "Latitude 43.10 north, longitude 12.25 east."}]
        code, r = self.json("POST", "/v1/ask", {"protocol": "rb-llm/1", "request_id": "2", "lang": "en",
                                               "text": "where are we?", "aircraft_status": snap})
        self.assertEqual((code, r["status"], r["status_source"]), (200, "ok", "request"))
        assert_valid(self, "ask-response.schema.json", r)
        # the model saw the position, not a refusal-prone raw JSON dump
        prompt = self.engine.calls[-1]["messages"][-1]["content"]
        self.assertIn("gps.position: latitude 43.10466 N", prompt)


if __name__ == "__main__":
    unittest.main()
