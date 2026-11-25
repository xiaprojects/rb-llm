# LLM engines and candidate models

This document compares the runtimes and models that run on a Raspberry Pi 5 (8 GB) and suit our task. The task is short multilingual utterances in, small structured JSON out. Whatever we pick here must be confirmed by the Phase 0 benchmark ([roadmap](roadmap.md)).

## What the task needs

| Requirement | Why |
|---|---|
| Constrained decoding (grammar / JSON schema) | Output must always parse and every enum must be valid. Never rely on "please answer in JSON". |
| KV prompt caching | The system prompt and catalog are ~1–2k tokens. Reprocessing them per request costs seconds. |
| Fast prefill and short decode on ARM CPU | See the [latency budget](hardware-raspberry-pi5.md#latency-budget-for-one-voice-command). |
| Multilingual: it, en, fr, es, de | Commands come in all five languages. ATC is mostly English, plus Italian. |
| Permissive license | The repo is GPL-3.0 and public. Users download the weights themselves, but we recommend models whose licenses allow redistribution and fine-tuning. |
| Fine-tunable (LoRA) | Phase 3 may fine-tune on synthetic aviation data. |

## Runtimes

| Runtime | Constrained output | Prompt cache | Pi 5 notes | Verdict |
|---|---|---|---|---|
| **llama.cpp `llama-server`** | GBNF grammars and `json_schema`, per request | yes (`cache_prompt`, `--cache-reuse`, slot save/restore) | Best-tuned ARM CPU kernels (NEON, dotprod, i8mm on A76). Exposes an OpenAI-compatible and a native `/completion` API | **Baseline.** Maximum control, small footprint |
| Ollama | `format` with a JSON schema (built on llama.cpp grammars) | automatic, less control | Simple to install and swap models. Adds a daemon and model-store layer, and pins its own llama.cpp version | Good for quick model comparisons in Phase 0. Not the production engine |
| Google LiteRT-LM / MediaPipe LLM | limited | yes | Raspberry Pi showcases it with Gemma. Optimized for Gemma `.litertlm` models | Benchmark Gemma here as a comparison point |
| ONNX Runtime GenAI | guidance / JSON support varies by version | yes | Works on ARM64. Fewer ready-made small models | Fallback option |
| Hailo-10H (AI HAT+ 2) | via Hailo's own GenAI API | — | 1–1.5 B models only (Qwen2/2.5, Llama 3.2 1B, DeepSeek-R1-Distill 1.5B at launch). CPU measured faster | Watch, don't build on it |
| llama.cpp Vulkan (Pi GPU) | same as llama.cpp | same | Runs on VideoCore VII, not faster than the CPU in reports | Benchmark once, out of curiosity |
| vLLM, TGI, MLC | — | — | Built for GPUs or not practical on Pi 5 | No |

**Decision:** rb-llm talks to an **engine adapter** interface, with adapters for `llamacpp` (production), `ollama` (dev/bench) and `rules-only` (no LLM, for tests and degraded mode). Swapping the runtime must not touch the protocol.

## Candidate models

All are instruction-tuned and available as GGUF. Sizes are total parameters. Pick the **smallest model that passes the evaluation set**, and do not pick the most "capable" model on paper.

| Model | Size | License | Multilingual (it/fr/es/de) | Notes |
|---|---|---|---|---|
| Qwen3 0.6B / 1.7B | 0.6 B / 1.7 B | Apache-2.0 | good | Thinking can be disabled (`/no_think`, or the template flag). Must be off for us. 1.7 B is the prime **command-model** candidate |
| Qwen3 4B (Instruct-2507) / Qwen3.5 small variants | ~4 B | Apache-2.0 | good | Native tool-calling. **ATC-model** candidate |
| Gemma 3 1B / 4B | 1 B / 4 B | Gemma Terms of Use (custom) | very good | Strong multilingual quality. The custom license is fine to use, but needs a notice |
| Gemma 4 E2B / E4B | ~2 B / ~4 B effective, more total | Gemma license (check the version) | very good | Check total RAM use, not "effective" size. Raspberry Pi published LiteRT numbers for E2B |
| Llama 3.2 1B / 3B | 1 B / 3 B | Llama 3.2 Community License | ok (de, fr, it, es officially listed) | Custom license with an attribution requirement |
| Phi-4-mini | 3.8 B | MIT | ok | Good at structured output. Weaker in Italian |
| SmolLM3 | 3 B | Apache-2.0 | en, fr, es, de, it, pt | Fully open training data. Good for fine-tuning experiments |
| Ministral 3 (3B) | 3 B | check | good | Check the license before recommending it |

Quantization: start with **Q4_K_M**. Also test **Q4_0**, because llama.cpp repacks it at load time for the faster ARM kernels, and **Q8_0** for the 0.6 B tier, where quality loss at 4-bit hurts more.

### Non-LLM alternatives (keep them in the comparison)

- **Rules / grammar parser.** Most ATC instructions follow ICAO patterns (Doc 9432), and a deterministic parser over normalized text handles a large share of them in under 1 ms. rb-llm uses one as its fast path in any case ([architecture](architecture.md)).
- **Encoder intent + slot model.** A small multilingual encoder (for example, distilled multilingual BERT or XLM-R) fine-tuned for joint intent classification and slot tagging, run with ONNX Runtime, takes ~10–50 ms on a Pi 5 CPU. It needs labelled data, which the generators from Phase 2 produce anyway. If it matches the LLM's accuracy, it is the better engine for **commands**. The LLM remains better for open ATC phrasing.

## Why constrained decoding plus a compact form

A 1–2 B model in free generation produces malformed JSON or invented parameter names on maybe several percent of inputs. A GBNF grammar built from the live catalog makes both impossible, because the sampler can only pick tokens the grammar allows. Keeping the grammar's output **short** (short keys, enum codes) is the single largest lever on latency. See [architecture.md](architecture.md#compact-internal-form).

## Sources

Consulted November 2026. Re-check before final model selection.

- Raspberry Pi: [Mastering edge AI on Raspberry Pi with LiteRT and Gemma](https://raspberrypi.com/news/mastering-edge-ai-on-raspberry-pi-with-litert-and-gemma)
- Raspberry Pi: [Introducing the AI HAT+ 2](https://www.raspberrypi.com/news/introducing-the-raspberry-pi-ai-hat-plus-2-generative-ai-on-raspberry-pi-5/)
- Jeff Geerling: [Raspberry Pi's new AI HAT adds 8GB of RAM for local LLMs](https://jeffgeerling.com/blog/2026/raspberry-pi-ai-hat-2) (CPU faster than the Hailo-10H in most tests)
- CNX Software: [AI HAT+ 2 review](https://www.cnx-software.com/2026/01/20/raspberry-pi-ai-hat-2-review-a-40-tops-ai-accelerator-tested-with-computer-vision-llm-and-vlm-workloads/)
- Community Pi 5 benchmarks (numbers inconsistent, order of magnitude only): [Gemma 3 on Pi 5](https://www.kunalganglani.com/blog/gemma-3-raspberry-pi-5-benchmark.md), [SpecPicks llama.cpp tok/s](https://specpicks.com/reviews/raspberry-pi-5-local-llm-llama-cpp-tokens-per-second-2026), [Stratosphere Pi 5 LLM research](https://stratospherelinuxips.readthedocs.io/en/develop/immune/research_rpi_llm_performance.html)
- Small-model tool calling: [TinyLLM paper (arXiv 2511.22138)](https://arxiv.org/html/2511.22138v1). Check the [Berkeley Function-Calling Leaderboard](https://gorilla.cs.berkeley.edu/leaderboard.html) for current rankings
- llama.cpp grammars: `grammars/README.md` in the llama.cpp repository
