# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**rb-llm** is the open-source language engine for **RB-08** (RB-Voice, the voice service in `../rbvoice`). It runs on a dedicated Raspberry Pi 5 (8 GB) and exposes a REST API. RB-08 sends it the text its speech recognizer heard, and rb-llm returns structured JSON. There are three tasks:

1. **Aircraft-status questions** (`/v1/ask`). "Where are we?", "how are the engine temperatures?": rb-llm reads the RB-01 (GPS, AHRS, baro, EMS, radio, traffic, airfields), turns it into short labelled facts, and the model answers from those facts only.
2. **Voice commands.** The pilot talks to the avionics ("computer, what are the cylinder temperatures", "set radio Perugia tower"). rb-llm returns the same `action / component / parameter / args` that rbvoice's keyword matcher produces today, so RB-08 can execute it against the RB-01 API.
3. **ATC sentences.** It parses radio phraseology heard on the intercom ("India Charlie Delta, climb altitude 5000 feet, QNH 1013") into a typed clearance, with a deterministic readback.

Everything runs offline, on the device. No cloud.

**Status: first working version (LLM path).** llama.cpp build, model download, the API service and the web console run end to end with Qwen3-1.7B. The rules fast path from the architecture doc is not built yet: every request goes to the model. The plan is in [docs/roadmap.md](docs/roadmap.md).

## Documentation map

| File | Contents |
|---|---|
| [README.md](README.md) | Public overview |
| [docs/architecture.md](docs/architecture.md) | Pipeline: normalize → rules fast path → constrained LLM → validate → expand. Internal compact LLM output |
| [docs/protocol.md](docs/protocol.md) | **The RB-08 ↔ rb-llm JSON protocol** (`rb-llm/1`): endpoints, request and response, errors, versioning |
| [schemas/](schemas/) | JSON Schema (draft 2020-12) for the protocol. Source of truth when prose and schema disagree |
| [examples/](examples/) | Request/response pairs; must validate against `schemas/` |
| [docs/aviation-domain.md](docs/aviation-domain.md) | The "shrunk" aviation context: command catalog, ATC instruction types, units, callsigns |
| [docs/hardware-raspberry-pi5.md](docs/hardware-raspberry-pi5.md) | What fits on a Pi 5 8 GB: OS, cooling, memory and latency budget, deployment |
| [docs/llm-engines.md](docs/llm-engines.md) | Runtimes (llama.cpp, Ollama, LiteRT-LM, Hailo…) and candidate models |
| [docs/integration-rbvoice.md](docs/integration-rbvoice.md) | What changes on the RB-08 side and how fallback works |
| [docs/roadmap.md](docs/roadmap.md) | Phased plan, evaluation method, open questions |
| [docs/website/](docs/website/) | Website copy and HTML section for RB-08 AI Voice. Publish only phrases that pass `scripts/verify_phrases.py` |

## Ground rules (apply to all future code)

- **The LLM never produces flight data.** For commands and ATC it only maps words to intents and slots. For `/v1/ask` it reads facts that `rbllm/status.py` computed from the RB-01 snapshot. Every number in an output must be traceable to the input text or to the facts shown to the model (evidence checks in `tasks.py`).
- **Compute in code, not in the model.** Distances, bearings, clock positions, unit conversions and sentinel filtering all happen in `status.py`. Small models get arithmetic wrong.
- **Constrained output only.** Every LLM call uses a grammar (GBNF or JSON schema) generated from the catalog, so the output is always parseable and every enum value is valid. Invalid output is a bug, not something to retry around.
- **Fail closed and fast.** On timeout, low confidence or validation failure, return `no_match` or `error` within the deadline. RB-08 then falls back to its own keyword matcher. Never guess a SET.
- **Keep rbvoice's safety rules.** These are explicit intent for autopilot direct-to, a clear-margin match for airfields, and standby-only radio writes (see `../rbvoice/CLAUDE.md`). ATC parsing is advisory: suggested actions always carry `requires_confirmation: true`.
- **Catalog comes from rbvoice.** Component, parameter and endpoint names are the ones in `../rbvoice/rbvoice-cases.<lang>.json`. Do not invent parallel names. Their keyword *values* are the parameter IDs.
- **Units in the protocol:** frequencies are strings with three decimals (`"118.500"`, the rbvoice format), altitudes are integer feet or integer flight levels, headings are integers 1–360, QNH is integer hPa, durations are integer seconds.
- **Languages:** it (reference, like rbvoice), en, fr, es, de for commands. ATC: en first, it second.
- **License hygiene:** the repo is GPL-3.0 (same as rbvoice). Never commit model weights. Download scripts fetch them, and each model's license is recorded in `docs/llm-engines.md`.
- **Target Python 3.11** (Raspberry Pi OS Bookworm), like rbvoice.

## Related repositories (siblings in `~/`)

| Path | Role |
|---|---|
| `../rbvoice` | RB-08 voice service: Vosk STT, keyword NLU, Piper TTS. The client of this API. Its `CLAUDE.md` documents the matcher, case files and RB-01 API |
| `../roastbeef` | RB-01 server (Stratux fork) and HMI. Routes in `main/managementinterface.go` |
| `../rb-manuals` | Product manuals. `docs/tech-requests/rbvoice.md` holds open questions about RB-08 (Q-08A-9d808 asks whether an LLM option is planned) |

## Commands

```bash
make deps                 # apt packages (sudo): build-essential cmake git curl python3 ccache
make llama                # clone + build llama.cpp at LLAMA_TAG (v1.0.0) -> bin/llama-server, llama-bench, llama-cli
make models               # list models/registry.tsv;   make model MODEL=<id>  downloads + verifies SHA-256
make catalog              # copy ../rbvoice/rbvoice-cases.*.json into catalog/ (RBVOICE_DIR=...)
make run                  # engine (background, engine.log) + API/web UI on :8091; WARM_LANG=it|en|... ; RB_URL=http://<rb01>
make test                 # stdlib unittest, fake engine, no model needed (~1 s); validates responses against schemas if jsonschema is installed
make validate             # examples/*.json against schemas/
make check-refusal        # GPS/position questions through /v1/ask; add --baseline via: python3 scripts/refusal_check.py --baseline
make bench                # llama-bench on the active model
python3 scripts/verify_phrases.py --lang en   # website phrases (data/website-phrases.json) -> docs/website/phrase-results.md; re-run after model/prompt/tasks changes
make install              # Pi: /opt/rb-llm, user rbllm, systemd rb-llm-engine + rb-llm, config /etc/default/rb-llm
python3 -m rbllm ask "where are we?" --source demo      # CLI clients
python3 -m rbllm parse "india charlie delta climb five thousand feet" --task atc --callsign I-ABCD
```

The active model is `models/current.gguf` (a symlink) plus `models/current.id`. `download-model.sh <id>` switches it.

## Code map

| File | Role |
|---|---|
| `scripts/build-llama.sh` | Builds llama.cpp. The embedded llama.cpp web UI is disabled (`LLAMA_BUILD_UI=OFF`, `LLAMA_USE_PREBUILT_UI=OFF`): its prebuilt-asset download breaks shallow-clone builds and needs network |
| `scripts/run-engine.sh` | Starts llama-server: 3 slots × 4096 ctx, `--reasoning off`, `--no-webui`, physical-core threads |
| `rbllm/server.py` | stdlib `ThreadingHTTPServer`: API routes + static `web/`. No pip packages needed on the Pi |
| `rbllm/llama_client.py` | Streaming chat completion with `response_format: json_schema`, per-task `id_slot`, deadline cancellation |
| `rbllm/tasks.py` | ask / command / atc logic: prompts → model → validation, evidence checks, callsign match, readback templates, refusal detection |
| `rbllm/prompts.py` | Static system prompts + few-shot examples + JSON schemas per task |
| `rbllm/status.py` | RB-01 snapshot → facts; `select_facts()` keeps only the topic of the question |
| `rbllm/catalog.py` | rbvoice case file → parameter list, prompt section, hash |
| `rbllm/normalize.py` | Spoken numbers → digits, ICAO alphabet, aliases |
| `web/` | Console: Ask / Command / ATC / Auto tabs, live facts panel, timings, raw JSON |
| `data/demo-rb01.json` | Real RB-01 capture (from rbvoice `tests/fixtures/rb01.json`), used when no RB-01 is reachable |

## Hard-won details (read before changing the LLM path)

- **Schema property order is enforced.** llama.cpp compiles the JSON schema into a grammar that allows properties only in the declared order. With `value` declared before `turn`, the model could not write `{"type":"heading","turn":"left","value":270}` and lost the heading. Declare properties in the order they are spoken (qualifiers first), and keep few-shot examples in the same order.
- **Always stream.** A non-streaming request keeps running inside llama-server after the client times out, and abandoned requests queue up in front of new ones (seen: 36 s of queue). Closing a stream cancels the task.
- **One slot per task** (`tasks.SLOT`, `--parallel 3`). With one shared slot, switching task evicts the cached static prompt (~1.5k tokens). A language switch also re-processes the prompt, so set `WARM_LANG` / `--warm-lang` to RB-08's language.
- **Output tokens are the latency.** Command output was cut from ~25 to ~15 tokens by dropping `kind` and empty `args`. Keep schemas and answers short.
- **Live status is re-processed every request** (its values change), so `select_facts()` sends only the facts the question is about.
- **Stratux sentinels:** 3276.7 / 32767 / 99999 mean "no data" (`status.SENTINELS`). EMS zeros mean "sensor not connected". Elevations in `db.airfields.json` are meters.
- **Evidence pool = facts shown to the model.** Matching against all facts let an invented "34.6 NM" pass as a 34 °C cylinder temperature.
- **Refusals:** neither Qwen3-1.7B nor Gemma-3-1B refused on the demo data, even with a generic prompt. The ask prompt framing, the facts-first schema, refusal detection with one retry and `status: "refused"` stay as defences. Use `make check-refusal` when trying a new model.
- **Altitude verbs come from the transmission.** The model wrote "maintain" for Italian "salite" (climb). `ALTITUDE_VERBS` in `tasks.py` overrides the model's verb when the words say otherwise, and an altitude instruction is dropped when the verb is unclear. Never add a default.
- **rbvoice gap found:** the English case file has `ems` endpoints `fuel1`/`fuel2` without keywords. `catalog.py` adds keyword-less endpoints as parameters so the model can still pick them.

## Protocol files

Example files are `{"schema": "<file>[#/$defs/...]", "description": "...", "body": {...}}`. When you change the protocol, update `docs/protocol.md`, `schemas/`, `examples/` and the tests together. `make test` checks live responses against the schemas.
