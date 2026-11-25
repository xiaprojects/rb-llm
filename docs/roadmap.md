# Roadmap and implementation plan

## Progress (2026-11-25)

Done, ahead of the original phase order (the LLM path first, as decided by the project owner):

- `make llama`: llama.cpp v1.0.0 build script (Pi 5 and x86). `make model`: SHA-256-verified model registry (Qwen3 0.6B/1.7B/4B-2507, Gemma 3 1B, Llama 3.2 1B).
- API service (`/v1/parse`, `/v1/ask`, `/v1/catalog`, `/v1/health`, `/v1/info`, `/v1/status`), web console, systemd units, `make install`.
- The `ask` task over live RB-01 status, with a refusal check (`make check-refusal`, `--baseline`).
- 49 unit and HTTP tests with a fake engine.

Measured on the **x86 dev box** (i7-5557U, 2 cores, loaded), *not on a Pi*, with Qwen3-1.7B Q4_K_M:

| Task | Output tokens | Latency (warm) |
|---|---|---|
| command | 14–32 | 2.4–5 s |
| ATC | 57–95 | 6–9.5 s |
| ask | 15–70 | 4.6–17.6 s |
| cold start (static prompts, 3 slots) | — | ~75 s, once at boot |

Generation ran at ~7.5 tok/s here. Phase 0 on a real Pi 5 decides the model tier. Results so far:

- 0/17 refusals for Qwen3-1.7B and Gemma-3-1B, with rb-llm's prompt and with a generic one.
- Gemma-3-1B often picked the wrong fact.
- Without the grounded prompt, the generic-prompt answers mislabelled directions (bearing 135° called "southwest").

Next: Phase 0 on the Pi, the rules fast path (Phase 1), and logprob confidence.

Each phase ends with a measurable gate. Do not start the next phase until the gate passes.

## Phase 0: bench the board (≈ 1 week)

Goal: replace the guesses in [hardware-raspberry-pi5.md](hardware-raspberry-pi5.md) with measured numbers.

- [ ] Prepare a Pi 5 8 GB with Active Cooler, NVMe, Raspberry Pi OS Lite 64-bit.
- [ ] Build llama.cpp at a pinned commit (`GGML_NATIVE=ON`). Also install Ollama for quick comparisons.
- [ ] `bench/run_llama_bench.sh`: for each candidate model ([llm-engines.md](llm-engines.md#candidate-models)) at Q4_K_M, Q4_0 and Q8_0 (≤ 1 B only), record prefill tok/s at 64 and 512 tokens, decode tok/s at 32 tokens, RSS, SoC temperature, throttle flags, and a 10-minute soak.
- [ ] Measure the effect of `cache_prompt` with a 1,500-token static prefix plus a 30-token tail: time to first token, cold vs warm.
- [ ] One-off: llama.cpp Vulkan backend, and LiteRT-LM with Gemma.
- [ ] Results go to `bench/results/<date>-<model>.json` (gitignored), with a summary table committed in `docs/benchmarks.md`.

**Gate:** at least one model gives **warm time-to-first-token < 300 ms** and **decode ≥ 10 tok/s**, with no throttling over a 10-minute soak.

## Phase 1: skeleton service, rules only (≈ 2 weeks)

- [ ] `rbllm/` package layout as in [architecture.md](architecture.md#components-planned-code-layout). FastAPI + uvicorn + pydantic v2, Python 3.11.
- [ ] Pydantic models that **export** to `schemas/`. A CI test fails if the export differs from the committed schemas.
- [ ] `catalog.py`: loads rbvoice case files from `--catalog-dir`, and implements `PUT /v1/catalog` and the hash.
- [ ] Normalizer: numbers (it/en first), spelling alphabet, aliases, skip words. Unit tests are ported from rbvoice `tests/test_unit.py` cases.
- [ ] Rules command matcher with routing parity against rbvoice. It must route every `rbvoice-test.<lang>.txt` phrase like rbvoice does.
- [ ] Rules ATC parser for `qnh`, `squawk`, `frequency_change`, `altitude`, `heading`, `runway`, `cleared_takeoff`, `cleared_land` (English).
- [ ] `/v1/health`, `/v1/info`, `/v1/parse` with `allow_llm=false`. The `rules-only` adapter.
- [ ] `tools/validate_examples.py` in CI, and the golden tests: each request example, run through the server, must give its response example (ignoring `engine` timings).
- [ ] systemd units and `make install` for the Pi.

**Gate:** 100 % routing parity with rbvoice on its test phrases, all examples golden, p95 latency < 20 ms on the Pi.

## Phase 2: LLM path for commands (≈ 3 weeks)

- [ ] `llm/llamacpp.py` adapter (`/completion` with `grammar`, `cache_prompt`, `n_probs`, deadline cancellation).
- [ ] `grammar.py`: GBNF for the compact command form, generated from the catalog.
- [ ] Static prompts per language with few-shot examples.
- [ ] **Datasets** (`data/`, committed, CC-BY-4.0 or GPL):
  - `commands.<lang>.jsonl` seeded from rbvoice `rbvoice-test.<lang>.txt` (already labelled `phrase | component | parameter | value`), plus paraphrases. These are generated, then **hand-reviewed**.
  - A negative set: passenger talk and ATC lines that must give `not_addressed` (target: the 356/360 ratio seen in rbvoice flight recordings).
  - STT-noise variants: run phrases through Piper → Vosk, as rbvoice's `voicetest.py` already does, to get realistic misrecognitions.
- [ ] Eval runner `bench/eval.py`: intent exact match, slot F1, false-acceptance rate on negatives, JSON validity (must be 100 %), latency p50/p95 on the Pi.
- [ ] Confidence calibration from logprobs, and choice of thresholds.
- [ ] Model selection: the smallest model that passes the gate.
- [ ] Compare with an encoder intent+slot model (ONNX). If it matches, it becomes the command engine.

**Gate (commands):** intent accuracy ≥ rbvoice keyword matcher + 5 points on the paraphrase set, **no regression** on rbvoice test phrases, false acceptance ≤ 1 % on negatives, p95 ≤ 1.5 s on the Pi.

## Phase 3: ATC parsing (≈ 4 weeks)

- [ ] Compact ATC grammar covering every instruction type in [aviation-domain.md](aviation-domain.md#atc-instruction-types).
- [ ] Synthetic ATC corpus from templates (Doc 9432 / SERA phraseology, Italian airspace names), in English and Italian. Include readbacks and other-aircraft calls.
- [ ] Optional real-transcript test set: check the licenses of public ATC corpora (for example ATCO2) before use. LiveATC recordings are not licensed for redistribution.
- [ ] Callsign matcher (full, abbreviated, type-prefixed) and the `addressee` logic.
- [ ] Readback and display templates (en, it).
- [ ] Optional LoRA fine-tune of the chosen small model on the synthetic corpus (off-device, on a GPU), exported to GGUF.

**Gate (ATC):** instruction-level F1 ≥ 0.9 on the synthetic test set, 0 invented numbers (evidence check), callsign `own`/`other` accuracy ≥ 0.98, p95 ≤ 3 s.

## Phase 4: rbvoice integration and field test

- [ ] `rbllm_client.py` and `--llm-mode` in rbvoice ([integration-rbvoice.md](integration-rbvoice.md)).
- [ ] `shadow` mode in flight. Collect disagreements, and label them into the datasets.
- [ ] Readback check (`readback_check` in the protocol).
- [ ] Switch the default to `fallback`, then to `first`, based on data.

## Evaluation method (all phases)

| Metric | Definition | Where |
|---|---|---|
| Intent exact match | action + component + parameter all correct | commands |
| Slot F1 | per arg (frequency, station, airfield, duration) | commands |
| Instruction F1 | instruction type + all required fields correct | ATC |
| Evidence violations | numbers not present in the normalized input | all; must be 0 |
| False acceptance | negatives returned as `ok` | classification |
| JSON validity | responses valid against `schemas/` | all; must be 100 % |
| Latency | p50 / p95 / max, warm, on a Pi 5 8 GB | all |
| Thermal | tok/s after a 10-minute soak vs the first minute | Phase 0, every model change |

Each evaluation run records the model, its SHA-256, the quantization, the llama.cpp commit, the prompt version and the catalog hash, so results stay reproducible.

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| **ATC audio through general Vosk models is poor** (radio band-limiting, speed, accents, English phraseology on an Italian model) | ATC parsing gets garbage text | Out of scope for rb-llm, but blocking for the ATC feature. Evaluate an ATC-tuned STT (for example a Whisper model fine-tuned on ATC speech) in a separate track. Use an English Vosk model on the radio feed |
| Pi 5 too slow for 3–4 B models | ATC latency | Compact output, rules fast path, 1.7 B model with a fine-tune |
| Small models hallucinate values | Safety | Grammar, evidence check, values only from RB-01, confirmation for SET |
| Catalog drift between rbvoice and rb-llm | Wrong routing | Catalog push and hash, parity test |
| Model license changes | Distribution | Prefer Apache-2.0/MIT models, never bundle weights |
| Thermal throttling in a hot cockpit | Latency spikes | Active cooling, health reports `throttled`, deadline-based fallback |

## Open questions

1. **Q-08A-9d808 (rb-manuals):** confirm that an *optional, offline* LLM engine is acceptable for the released RB-08, and how the manual and disclaimer should describe it.
2. **Deployment:** a dedicated second Pi 5 (reference), or rb-llm co-located on the RB-08 Pi for cheaper installs?
3. **ATC display:** where the RB-01 shows parsed clearances (a new bridge key, a `/remote/voice` extension, or a new roastbeef endpoint).
4. **Audio source for ATC:** is a separate COM audio feed available (`source: "radio"`), or only the mixed intercom?
5. **Own callsign:** read from the RB-01 settings (is there a field?) or from `rbvoice.sh`?
6. **Speaking ATC readbacks:** should the RB ever *speak* a readback hint, or only display it? (Default: display only.)
7. **License of datasets:** AGPL-3.0 like the code, or CC-BY-4.0 so others can reuse them?
