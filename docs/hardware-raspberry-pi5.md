# Raspberry Pi 5 (8 GB): what can run on it

This guide covers a Pi 5 that does nothing but run rb-llm. It sets out what the board can realistically run for our tasks, and how to set it up.

## Summary

| Question | Answer |
|---|---|
| Is RAM the limit? | **No.** 8 GB holds an 8 B model at 4-bit (~5 GB). The limit is **generation speed** (memory bandwidth, ~17 GB/s theoretical on LPDDR4X-4267). |
| Usable model size for voice | **0.5 B – 2 B** for commands (sub-second to ~1.5 s). **Up to ~4 B** for ATC if the output is kept short. 7–8 B models fit but generate ~1–2 tokens/s: too slow for a cockpit. |
| Best runtime | `llama.cpp` (`llama-server`) on the CPU with 4 threads, GGUF Q4_K_M or Q4_0 weights, ARM NEON/dotprod kernels. |
| GPU (VideoCore VII) | `llama.cpp` has a Vulkan backend that runs on the Pi 5 GPU, but reports show it is not faster than the CPU. Treat it as experimental. |
| NPU | The AI HAT+ (Hailo-8/8L) is for vision, not LLMs. The **AI HAT+ 2** (Hailo-10H, 8 GB on board) runs 1–1.5 B LLMs from Hailo's model zoo, but independent tests found the Pi 5 CPU generally *faster* (the HAT is somewhat more power-efficient). Not part of the baseline. Revisit when Hailo's model list grows. |
| What decides latency | The **number of output tokens**. This is why rb-llm makes the LLM emit a compact internal form, and expands it to the protocol JSON in code (see [architecture](architecture.md)). |

## Expected performance (to be measured in Phase 0)

Published Pi 5 numbers disagree (different runtimes, quantizations, prompt lengths, cooling). Treat this table as an order of magnitude only. The real figures will come from `llama-bench` on our board ([roadmap](roadmap.md), Phase 0).

| Model tier | Weights at Q4 | Generation (tok/s) | Prompt processing | Fits voice budget? |
|---|---|---|---|---|
| 0.5–0.6 B | ~0.4 GB | ~20–40 | fast | yes, easily |
| 1–1.7 B | ~0.8–1.2 GB | ~8–20 | ~50–150 tok/s | yes |
| 3–4 B | ~2–2.5 GB | ~3–10 | ~15–40 tok/s | only with a short output and a cached prompt |
| 7–8 B | ~4.5–5 GB | ~1–2 | slow | no |

Sources consulted (Nov 2026): Raspberry Pi's LiteRT/Gemma article, Jeff Geerling's AI HAT+ 2 tests, community llama.cpp/Ollama benchmarks. They are listed in [llm-engines.md](llm-engines.md#sources).

### Latency budget for one voice command

Target: **p95 ≤ 1.5 s** from the arrival of the request to the response, for commands. ATC parsing is less time-critical: **p95 ≤ 3 s**.

| Step | Budget |
|---|---|
| HTTP + JSON + normalization | < 20 ms |
| Rules fast path (when it matches) | < 10 ms, LLM skipped |
| Prompt processing of the utterance (system prompt KV-cached) | 100–300 ms (≈ 20–40 new tokens) |
| Generation of compact output (≈ 10–20 tokens) | 0.5–1.2 s on a 1–2 B model |
| Validation + expansion | < 10 ms |

The large static part of the prompt (instructions, catalog, few-shot examples) must stay **byte-identical between requests**, so that `llama-server` reuses its KV cache (`cache_prompt: true`). Only the utterance and the small dynamic context go at the end.

## Memory budget (dedicated board)

| Consumer | RAM |
|---|---|
| Raspberry Pi OS Lite 64-bit, idle | ~0.3 GB |
| rb-llm service (Python, FastAPI) | ~0.1 GB |
| `llama-server` with a 1.7 B Q4_K_M model, 4k context | ~1.5 GB |
| Optional second model (for example a 4 B model for ATC) | ~3 GB |
| Free for page cache / headroom | ≥ 3 GB |

Both a command model and an ATC model can stay resident at the same time, which avoids load latency. Lock them in RAM (`--mlock`) so they are never paged out.

### Co-located option (rb-llm on the RB-08 Pi)

This is possible, but the processes compete for 4 cores: Vosk's large model (~2–3 GB of RAM for the big Italian model) and Piper run at the same moments as the LLM. In that setup, use a ≤ 1 B model and `--threads 3`. The **dedicated board is the reference setup**.

## Hardware checklist

- **Raspberry Pi 5, 8 GB.** A 16 GB board brings no speed gain, only room for larger (too slow) models.
- **Active Cooler (or an equivalent fan heatsink): mandatory.** LLM inference holds all 4 cores at 100 %. Without a fan the SoC reaches 85 °C and throttles within a minute or two, and the tokens/s rate drops sharply. rb-llm reports throttling in `/v1/health` (from `vcgencmd get_throttled`).
- **Official 27 W USB-C PSU**, or a regulated 5 V 5 A supply from aircraft power. Undervoltage also throttles (bit 0 of `get_throttled`).
- **Storage:** an NVMe SSD (M.2 HAT+) is recommended, or an A2 microSD card. Storage speed affects only model load time at boot (a 1 GB model loads in ~1 s from NVMe and several seconds from SD), not inference.
- **Network to RB-08:** wired Ethernet when the boards are next to each other. Otherwise the Stratux/RB Wi-Fi. Use a fixed IP or mDNS (`rb-llm.local`).
- **Vibration and temperature:** this is the same concern as the other RB units. Use a closed case with airflow for the fan.

## Software baseline

- **Raspberry Pi OS Lite 64-bit** (Bookworm, or Trixie when RB images move to it). No desktop.
- `llama.cpp` built from source on the Pi with the native flags (`cmake -B build -DGGML_NATIVE=ON && cmake --build build -j4`), or a pinned release. Pin the commit in the Makefile, because llama.cpp changes quickly.
- Python 3.11 venv in `/opt/rb-llm` for the API service.
- Two systemd units: `rb-llm-engine.service` (llama-server on `127.0.0.1:8080`) and `rb-llm.service` (the public API on `:8091`). The API service waits for the engine's `/health` before it reports `ready`.
- Models in `/opt/rb-llm/models/`, downloaded with a pinned SHA-256 by `make models`.

Suggested `llama-server` flags (to tune in Phase 0):

```bash
llama-server -m /opt/rb-llm/models/<model>.gguf \
  --host 127.0.0.1 --port 8080 \
  --threads 4 --ctx-size 4096 --parallel 1 \
  --mlock --no-webui --cache-reuse 256
```

One parallel slot is enough. RB-08 sends one utterance at a time, and a second slot would halve the speed of each.

## Power and boot

- Cold boot to `ready` should take under 30 s, so rb-llm is ready before the engine is started. RB-08 must work without rb-llm (keyword NLU only) until `/v1/health` reports `ready`.
- Power draw under load is roughly 8–12 W including the fan (to be measured).
