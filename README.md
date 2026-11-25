# rb-llm

Offline language engine for **RB-08 (RB-Voice)**, the voice service of the RB-Avionics family.

rb-llm runs on a dedicated **Raspberry Pi 5 (8 GB)**, next to the avionics. It turns recognized speech into structured JSON through a small REST API:

- **Aircraft status:** "where are we?" becomes "Latitude 43.10 north, longitude 12.25 east, 0.2 miles from Montemelino Airfield", read from the aircraft's own GPS.
- **Voice commands:** "computer, what are the cylinder temperatures?" becomes `{"action":"GET","component":"ems","parameter":"cht"}`.
- **ATC phraseology:** "India Charlie Delta, climb altitude five thousand feet, QNH one zero one three" becomes a typed clearance (`altitude: climb 5000 ft`, `qnh: 1013 hPa`) with a readback.

It uses small open-weight language models (1–4 B parameters) through `llama.cpp`, with grammar-constrained decoding, so the output is always valid JSON. Numbers, distances, safety rules and readbacks are handled in code, not by the model. Nothing leaves the aircraft.

> **Status:** first working version. llama.cpp build, model download, the REST API and a web console run end to end on Qwen3-1.7B. See [docs/roadmap.md](docs/roadmap.md) for what comes next.

## Quick start (Raspberry Pi 5 or any Linux box)

```bash
make deps        # build tools (sudo apt)
make llama       # build llama.cpp v1.0.0 (~10 min on a Pi 5)
make model       # download Qwen3-1.7B Q4_K_M (1.1 GB, SHA-256 verified)
make catalog     # copy the rbvoice case files (expects ../rbvoice)
make run         # engine + API; open http://<host>:8091
```

Use `RB_URL=http://<rb01-address>` with `make run` to read live aircraft data. Without it, the console uses a recorded RB-01 snapshot. `make install` sets it up as systemd services on the Pi.

What it can do today:

- **Ask the aircraft** (`POST /v1/ask`): "where are we?", "how are the engine temperatures?", "any traffic?" The answer is grounded in live RB-01 data. Distances, bearings and clock positions are computed in code, and the model only reads them.
- **Voice commands** (`POST /v1/parse`, `task: command`): rbvoice-compatible intents in 5 languages.
- **ATC** (`POST /v1/parse`, `task: atc`): typed instructions, callsign check, template readback.

> **Not certified avionics.** rb-llm is experimental software for situational awareness. It never replaces the pilot's own listening, readback and judgement.

## How it fits

```
 microphone / intercom
        │
   ┌────▼─────────────── RB-08 (rbvoice) ────────────────┐
   │ Vosk STT ─► text ─► keyword NLU ──────────┐         │
   │                 └──► rb-llm client ─┐     │         │
   │                                     │     ▼         │
   │                   RB-01 API ◄── execute intent      │
   │                                     │     │         │
   │                       Piper TTS ◄───┴─────┘         │
   └─────────────────────────────────────┼───────────────┘
                                         │ HTTP/JSON  (rb-llm/1)
                         ┌───────────────▼──────────────┐
                         │ rb-llm (Raspberry Pi 5 8 GB) │
                         │ normalize ► rules ► LLM ►    │
                         │ validate ► expand            │
                         └──────────────────────────────┘
```

## Documentation

- [Architecture](docs/architecture.md)
- [JSON protocol (rb-llm/1)](docs/protocol.md), with [schemas](schemas/) and [examples](examples/)
- [Aviation domain](docs/aviation-domain.md)
- [Raspberry Pi 5 hardware guide](docs/hardware-raspberry-pi5.md)
- [LLM engines and models](docs/llm-engines.md)
- [Integration with rbvoice](docs/integration-rbvoice.md)
- [Roadmap](docs/roadmap.md)

## License

GPL-3.0, the same license as rbvoice. Model weights are not distributed with this repository. Each model keeps its own license (see [docs/llm-engines.md](docs/llm-engines.md)).
