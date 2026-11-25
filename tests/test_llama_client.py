"""LlamaClient against a fake llama-server (stdlib HTTP, SSE streaming). No model needed."""
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import helpers  # noqa: F401  (puts the repo root on sys.path)

from rbllm.llama_client import EngineError, LlamaClient


def sse(*events):
    """Server-sent-event body lines, as llama-server streams them."""
    out = [b"data: " + json.dumps(e).encode() + b"\n\n" for e in events]
    return out + [b"data: [DONE]\n\n"]


def deltas(text, step=4):
    return [{"choices": [{"delta": {"content": text[i:i + step]}}]} for i in range(0, len(text), step)]


class FakeLlama(BaseHTTPRequestHandler):
    # set per test: (status, list of body chunks, delay between chunks in s)
    reply = (200, [], 0.0)
    health_status = 200
    requests = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/health":
            self.send_response(self.health_status)
            self.end_headers()
            self.wfile.write(b"{}")
        elif self.path == "/props":
            body = json.dumps({"total_slots": 3}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        FakeLlama.requests.append(json.loads(self.rfile.read(n)))
        status, chunks, delay = FakeLlama.reply
        self.send_response(status)
        self.send_header("Content-Type", "text/event-stream" if status == 200 else "application/json")
        self.end_headers()
        try:
            for c in chunks:
                if delay:
                    time.sleep(delay)
                self.wfile.write(c)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # client closed the stream (deadline): that is the point


class TestLlamaClient(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeLlama)
        cls.httpd.daemon_threads = True
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        FakeLlama.requests = []
        FakeLlama.health_status = 200
        self.client = LlamaClient(self.url + "/", model_name="test-model")

    def complete(self, deadline_s=5.0, slot=-1):
        return self.client.complete_json([{"role": "user", "content": "hi"}], {"type": "object"},
                                         max_tokens=32, deadline=time.monotonic() + deadline_s, slot=slot)

    # --- completion -------------------------------------------------------------------------
    def test_streamed_json_and_usage(self):
        text = '{"action":"GET","component":"ems"}'
        usage = {"usage": {"prompt_tokens": 120, "completion_tokens": 9},
                 "timings": {"cache_n": 100, "predicted_per_second": 21.37, "prompt_ms": 40.4,
                             "predicted_ms": 410.6},
                 "model": "qwen3"}
        FakeLlama.reply = (200, sse(*deltas(text), usage), 0.0)
        c = self.complete(slot=1)
        self.assertEqual(c.data, {"action": "GET", "component": "ems"})
        self.assertEqual(c.raw, text)
        self.assertEqual((c.tokens_in, c.tokens_out, c.cached_tokens), (120, 9, 100))
        self.assertEqual(c.tok_per_s, 21.4)
        self.assertEqual(c.model, "qwen3")
        self.assertEqual(c.extra, {"prompt_ms": 40, "predicted_ms": 411})

    def test_payload(self):
        FakeLlama.reply = (200, sse(*deltas("{}")), 0.0)
        self.complete(slot=2)
        p = FakeLlama.requests[-1]
        self.assertTrue(p["stream"])  # streaming is what lets a closed stream cancel the task
        self.assertEqual(p["id_slot"], 2)
        self.assertEqual(p["temperature"], 0.0)
        self.assertEqual(p["response_format"]["type"], "json_schema")
        self.assertEqual(p["response_format"]["json_schema"]["schema"], {"type": "object"})
        self.assertEqual(p["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(p["model"], "test-model")
        self.assertGreater(p["t_max_predict_ms"], 0)
        self.assertLessEqual(p["t_max_predict_ms"], 5000)

    def test_no_slot_by_default(self):
        FakeLlama.reply = (200, sse(*deltas("{}")), 0.0)
        self.complete()
        self.assertNotIn("id_slot", FakeLlama.requests[-1])

    def test_timings_fallback_when_no_usage(self):
        FakeLlama.reply = (200, sse(*deltas('{"a":1}'), {"timings": {"prompt_n": 50, "predicted_n": 4}}), 0.0)
        c = self.complete()
        self.assertEqual((c.tokens_in, c.tokens_out), (50, 4))
        self.assertEqual(c.model, "test-model")

    def test_think_block_stripped(self):
        FakeLlama.reply = (200, sse(*deltas('<think>\nhmm\n</think>\n{"ok":true}')), 0.0)
        self.assertEqual(self.complete().data, {"ok": True})

    def test_ignores_non_data_lines(self):
        FakeLlama.reply = (200, [b": keep-alive\n\n"] + sse(*deltas('{"x":2}')), 0.0)
        self.assertEqual(self.complete().data, {"x": 2})

    # --- failures ---------------------------------------------------------------------------
    def test_truncated_output(self):
        FakeLlama.reply = (200, sse(*deltas('{"action":"GE')), 0.0)
        with self.assertRaises(EngineError) as cm:
            self.complete()
        self.assertEqual(cm.exception.code, "engine_failure")

    def test_loading_503(self):
        FakeLlama.reply = (503, [b'{"error":"loading model"}'], 0.0)
        with self.assertRaises(EngineError) as cm:
            self.complete()
        self.assertEqual(cm.exception.code, "loading")
        self.assertIn("loading model", str(cm.exception))

    def test_http_500(self):
        FakeLlama.reply = (500, [b'{"error":"boom"}'], 0.0)
        with self.assertRaises(EngineError) as cm:
            self.complete()
        self.assertEqual(cm.exception.code, "engine_failure")

    def test_deadline_during_generation(self):
        FakeLlama.reply = (200, sse(*deltas('{"a":"' + "x" * 40 + '"}', step=2)), 0.05)
        t0 = time.monotonic()
        with self.assertRaises(EngineError) as cm:
            self.complete(deadline_s=0.3)
        self.assertEqual(cm.exception.code, "timeout")
        self.assertLess(time.monotonic() - t0, 1.0)  # fail fast: no waiting for the full answer

    def test_expired_deadline(self):
        with self.assertRaises(EngineError) as cm:
            self.complete(deadline_s=0.0)
        self.assertIn(cm.exception.code, ("busy", "timeout"))
        self.assertEqual(FakeLlama.requests, [])

    def test_busy_while_other_request_runs(self):
        self.client._lock.acquire()
        try:
            with self.assertRaises(EngineError) as cm:
                self.complete(deadline_s=0.1)
            self.assertEqual(cm.exception.code, "busy")
        finally:
            self.client._lock.release()

    def test_unreachable(self):
        port = self.httpd.server_address[1]
        dead = ThreadingHTTPServer(("127.0.0.1", 0), FakeLlama)  # grab a free port, then close it
        dead_port = dead.server_address[1]
        dead.server_close()
        self.assertNotEqual(port, dead_port)
        client = LlamaClient(f"http://127.0.0.1:{dead_port}")
        with self.assertRaises(EngineError) as cm:
            client.complete_json([], {}, 8, time.monotonic() + 2)
        self.assertEqual(cm.exception.code, "engine_failure")
        self.assertEqual(client.health(timeout=0.5), "down")
        self.assertEqual(client.props(timeout=0.5), {})

    # --- health / props ---------------------------------------------------------------------
    def test_health(self):
        self.assertEqual(self.client.health(), "ok")
        FakeLlama.health_status = 503
        self.assertEqual(self.client.health(), "loading")
        FakeLlama.health_status = 500
        self.assertEqual(self.client.health(), "down")

    def test_props(self):
        self.assertEqual(self.client.props(), {"total_slots": 3})


if __name__ == "__main__":
    unittest.main()
