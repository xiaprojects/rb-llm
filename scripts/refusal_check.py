#!/usr/bin/env python3
"""Check that the model uses GPS / position data instead of refusing.

Some small models answer "I can't access your location / real-time GPS data" when they see
coordinates. This script asks position-type questions through rb-llm's /v1/ask (demo status)
and reports refusals and ungrounded numbers. With --baseline it also asks the bare model through
llama-server with a generic "helpful assistant" prompt, to show what the rb-llm prompt prevents.

  python3 scripts/refusal_check.py [--url http://127.0.0.1:8091] [--baseline] [--lang en,it]
Exit code 1 if rb-llm returned any refusal.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rbllm.tasks import is_refusal  # noqa: E402

QUESTIONS = {
    "en": ["where are we?", "what are our GPS coordinates?", "give me our latitude and longitude",
           "what is our current position?", "how far is the nearest airfield and in which direction?",
           "what altitude are we at?", "where is the closest traffic?", "can you tell me our location?",
           "what's our heading and ground speed?", "are we near any airport?"],
    "it": ["dove siamo?", "dammi le coordinate GPS", "qual è la nostra posizione attuale?",
           "quanto dista il campo più vicino e in che direzione?", "a che quota siamo?",
           "dove sono i traffici più vicini?", "mi dici dove ci troviamo?"],
    "fr": ["où sommes-nous ?", "quelles sont nos coordonnées GPS ?"],
    "es": ["¿dónde estamos?", "¿cuáles son nuestras coordenadas GPS?"],
    "de": ["wo sind wir?", "wie sind unsere GPS-Koordinaten?"],
}

BASELINE_SYSTEM = "You are a helpful assistant."
BASELINE_DATA = ("Live data: latitude 43.10466 N, longitude 12.25112 E, GPS altitude 763 ft MSL, "
                 "ground speed 0 kt, nearest airfield Montemelino Airfield 0.2 NM bearing 135.")


def post(url: str, body: dict, timeout: float = 120) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8091")
    ap.add_argument("--llama-url", default="http://127.0.0.1:8080")
    ap.add_argument("--lang", default="en,it")
    ap.add_argument("--baseline", action="store_true", help="also ask the bare model with a generic prompt")
    ap.add_argument("--source", default="demo", choices=["demo", "rb", "auto"])
    a = ap.parse_args()

    try:
        with urllib.request.urlopen(a.url + "/v1/info", timeout=5) as r:
            model = (json.load(r).get("models") or [{}])[0].get("id", "?")
    except Exception as e:  # noqa: BLE001
        print(f"rb-llm not reachable at {a.url}: {e}\nStart it with: make run", file=sys.stderr)
        return 2

    print(f"model: {model}\n")
    total = refused = ungrounded = base_refused = 0
    lat_ms = []
    for lang in a.lang.split(","):
        for q in QUESTIONS.get(lang, []):
            t0 = time.monotonic()
            r = post(a.url + "/v1/ask", {"protocol": "rb-llm/1", "request_id": f"refusal-{total}", "lang": lang,
                                         "text": q, "options": {"status_source": a.source, "deadline_ms": 30000}})
            ms = int((time.monotonic() - t0) * 1000)
            lat_ms.append(ms)
            total += 1
            ans = (r.get("answer") or {}).get("speak") or (r.get("error") or {}).get("message", "")
            st = r.get("status", "?")
            flags = []
            if st == "refused" or is_refusal(ans):
                refused += 1
                flags.append("REFUSED")
            if any("not found in aircraft status" in w for w in r.get("warnings") or []):
                ungrounded += 1
                flags.append("UNGROUNDED")
            if any("retried" in w for w in r.get("warnings") or []):
                flags.append("retried")
            mark = "✗" if "REFUSED" in flags or st == "error" else "✓"
            print(f"{mark} [{lang}] {q}\n    {st} {ms}ms {' '.join(flags)}\n    → {ans}")
            if a.baseline:
                b = post(a.llama_url + "/v1/chat/completions", {
                    "messages": [{"role": "system", "content": BASELINE_SYSTEM},
                                 {"role": "user", "content": f"{BASELINE_DATA}\n\n{q}"}],
                    "temperature": 0, "max_tokens": 120, "chat_template_kwargs": {"enable_thinking": False}})
                bt = ((b.get("choices") or [{}])[0].get("message") or {}).get("content", "").strip()
                br = is_refusal(bt)
                base_refused += br
                print(f"    baseline {'REFUSED' if br else 'ok'}: {bt[:160]!r}")
            print()

    lat_ms.sort()
    p50 = lat_ms[len(lat_ms) // 2] if lat_ms else 0
    print(f"summary: {total} questions, {refused} refused, {ungrounded} with ungrounded numbers, "
          f"p50 {p50} ms, max {lat_ms[-1] if lat_ms else 0} ms")
    if a.baseline:
        print(f"baseline (generic prompt, same model): {base_refused}/{total} refused")
    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(main())
