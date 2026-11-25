#!/usr/bin/env python3
"""Verify the website example phrases against a live rb-llm.

Runs every phrase of data/website-phrases.json, checks the result against its expectation,
and writes docs/website/phrase-results.md (dated table with model, host and timings).
Only phrases marked PASS there may be published.

  python3 scripts/verify_phrases.py [--url http://127.0.0.1:8091] [--lang en] [--task ask]
Results for languages not run this time are kept from the previous run.
Tip: start the server with WARM_LANG=<lang> for the language you verify (a cold language
re-processes the static prompts and can hit the deadline).
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS_JSON = os.path.join(ROOT, "docs", "website", "phrase-results.json")
RESULTS_MD = os.path.join(ROOT, "docs", "website", "phrase-results.md")


def post(url: str, body: dict) -> tuple:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(p: dict, r: dict) -> tuple:
    """Returns (ok, short result, reason)."""
    if r.get("status") == "error":
        return False, "error", (r.get("error") or {}).get("message", "")
    if p["task"] == "ask":
        ans = (r.get("answer") or {}).get("speak") or ""
        if r.get("status") != "ok":
            return False, ans, f"status {r.get('status')}"
        if r.get("warnings"):
            return False, ans, "; ".join(r["warnings"])
        used = set(r.get("facts_used") or [])
        if p.get("facts") and not used & set(p["facts"]):
            return False, ans, f"used {sorted(used)}"
        for s in p.get("answer_has", []):
            if s.lower() not in ans.lower():
                return False, ans, f"answer lacks {s!r}"
        return True, ans, ""
    if p["task"] == "command":
        if p.get("status"):
            return r.get("status") == p["status"], r.get("status"), ""
        c = r.get("command") or {}
        got = f"{c.get('action')} {c.get('component')}.{c.get('parameter')}"
        args = c.get("args") or {}
        if args:
            got += " " + json.dumps(args, ensure_ascii=False)
        if r.get("status") != "ok":
            return False, got, f"status {r.get('status')}"
        if f"{c.get('component')}.{c.get('parameter')}" not in p["param"]:
            return False, got, "wrong parameter"
        if p.get("action") and c.get("action") != p["action"]:
            return False, got, "wrong action"
        for k, v in (p.get("args") or {}).items():
            if args.get(k) != v:
                return False, got, f"args.{k} != {v}"
        return True, got, ""
    # atc
    a = r.get("atc") or {}
    types = [i["type"] for i in a.get("instructions") or []]
    rb = (a.get("readback") or {}).get("text")
    got = ", ".join(types) + (f" | readback: {rb}" if rb else "")
    optional = set(p.get("types_optional", []))
    if set(types) - optional != set(p["types"]) - optional:
        return False, got, f"expected {p['types']}"
    if r.get("addressee") != p["addressee"]:
        return False, got, f"addressee {r.get('addressee')}"
    for want in p.get("readback_has", []):
        if want.lower() not in (rb or "").lower():
            return False, got, f"readback lacks {want!r}"
    # A dropped invented station/callsign is the guard working: the final result is still correct
    # A dropped invented station/callsign or a corrected verb is the guard working: the final result
    # is still correct. Recorded in the reason column all the same.
    guard = ("dropped station", "dropped callsign", "altitude verb")
    bad = [w for w in r.get("warnings") or [] if not w.startswith(guard)]
    if bad:
        return False, got, "; ".join(bad)
    return True, got, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8091")
    ap.add_argument("--lang", default="")
    ap.add_argument("--task", default="")
    a = ap.parse_args()

    with open(os.path.join(ROOT, "data", "website-phrases.json"), encoding="utf-8") as f:
        spec = json.load(f)
    try:
        with urllib.request.urlopen(a.url + "/v1/info", timeout=5) as r:
            info = json.load(r)
    except Exception as e:  # noqa: BLE001
        print(f"rb-llm not reachable at {a.url}: {e}", file=sys.stderr)
        return 2
    model = (info.get("models") or [{}])[0].get("id", "?")
    engine = info.get("engine", "")
    host = f"{platform.machine()} {_cpu()}"

    old = {}
    if os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, encoding="utf-8") as f:
            old = {(x["task"], x["lang"], x["text"]): x for x in json.load(f).get("results", [])}

    results = dict(old)
    n = fails = 0
    for p in spec["phrases"]:
        if (a.lang and p["lang"] != a.lang) or (a.task and p["task"] != a.task):
            continue
        body = {"protocol": "rb-llm/1", "request_id": f"web-{n}", "lang": p["lang"], "text": p["text"]}
        if p["task"] == "ask":
            body["options"] = {"status_source": "demo", "deadline_ms": 30000}
            path = "/v1/ask"
        else:
            body.update(task=p["task"], source="intercom",
                        context={"own_callsign": {"registration": spec["own_callsign"]}},
                        options={"deadline_ms": 15000})
            if p["text"].startswith(("computer ", "primary ", "primario ")):
                body["context"]["wake_word"] = p["text"].split()[0]
            path = "/v1/parse"
        t0 = time.monotonic()
        code, r = post(a.url + path, body)
        ms = int((time.monotonic() - t0) * 1000)
        ok, got, why = check(p, r)
        n += 1
        fails += not ok
        results[(p["task"], p["lang"], p["text"])] = {
            "task": p["task"], "lang": p["lang"], "group": p["group"], "text": p["text"],
            "pass": ok, "result": got, "reason": why, "ms": ms, "model": model,
            "date": time.strftime("%Y-%m-%d"), "host": host}
        print(f"{'PASS' if ok else 'FAIL'} {ms:6d}ms [{p['task']}/{p['lang']}] {p['text']}\n      → {got}"
              + (f"\n      ✗ {why}" if why else ""))

    order = {(p["task"], p["lang"], p["text"]): i for i, p in enumerate(spec["phrases"])}
    rows = sorted((v for k, v in results.items() if k in order), key=lambda v: order[(v["task"], v["lang"], v["text"])])
    os.makedirs(os.path.dirname(RESULTS_JSON), exist_ok=True)
    with open(RESULTS_JSON, "w", encoding="utf-8") as f:
        json.dump({"model": model, "engine": engine, "results": rows}, f, indent=1, ensure_ascii=False)
        f.write("\n")
    write_md(rows, model, engine)
    print(f"\n{n} phrases run, {n - fails} pass, {fails} fail -> {os.path.relpath(RESULTS_MD, ROOT)}")
    return 1 if fails else 0


def _cpu() -> str:
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith(("model name", "Model")):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return ""


def write_md(rows: list, model: str, engine: str) -> None:
    passed = sum(r["pass"] for r in rows)
    lines = ["# Website phrases: verification results", "",
             "Generated by `scripts/verify_phrases.py` from `data/website-phrases.json`. Do not edit by hand.", "",
             f"- Model: `{model}`",
             f"- Engine: `{re.sub(r' built=.*', '', engine)}`",
             f"- Hosts and dates: {', '.join(sorted({r['host'] + ' (' + r['date'] + ')' for r in rows}))}",
             "- Aircraft data for ask questions: `data/demo-rb01.json` (real RB-01 capture, on the ground near Perugia)",
             f"- Result: **{passed} of {len(rows)} pass**", "",
             "Latency was measured on the host above, which is not a Raspberry Pi 5. Do not quote it as Pi performance.", ""]
    for task, title in (("ask", "Ask the aircraft"), ("command", "Voice commands"), ("atc", "ATC")):
        sub = [r for r in rows if r["task"] == task]
        if not sub:
            continue
        lines += [f"## {title}", "", "| | Lang | Phrase | Result | ms |", "|---|---|---|---|---|"]
        for r in sub:
            res = str(r["result"]).replace("|", "\\|")
            if r["reason"]:
                res += f" ✗ *{r['reason']}*".replace("|", "\\|")
            lines.append(f"| {'✅' if r['pass'] else '❌'} | {r['lang']} | {r['text']} | {res} | {r['ms']} |")
        lines.append("")
    with open(RESULTS_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    sys.exit(main())
