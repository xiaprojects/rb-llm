# Architecture

## Implementation status (v0.1)

What is built differs from the plan below in these points:

- **No rules fast path yet.** Every command, ATC and ask request goes to the model. Stage 3 is the next step ([roadmap](roadmap.md)).
- **Third task, `ask`.** The model answers questions from the aircraft status. `rbllm/status.py` turns the RB-01 snapshot into labelled facts (with distances, bearings and clock positions computed in code, and sentinels removed). `select_facts()` keeps only the question's topic, and the schema makes the model list the facts it used before it writes the answer.
- **Output form.** The model writes short-key JSON and not the single-letter compact form: `{"action":"GET","param":"ems.cht"}` for commands, and the instruction objects of the protocol (with `frequency` in place of `frequency_mhz`) for ATC. Readable keys helped the 1.7 B model more than the few tokens saved. `kind` and empty `args` were dropped, cutting command output to ~15 tokens.
- **Server.** The server uses the Python standard library (`ThreadingHTTPServer`), not FastAPI, so the Pi needs no pip packages. Request validation with `jsonschema` is used only when it is installed.
- **Engine.** `llama-server` runs with 3 slots, one per task (`id_slot`), so each static prompt stays cached. Requests are streamed, so a deadline cancels generation in the engine. Thinking is off (`--reasoning off` and `enable_thinking: false`).
- **Confidence** is a heuristic (0.9 minus penalties for warnings), not yet calibrated from log-probabilities.

## Pipeline

Every `POST /v1/parse` request goes through the same five stages:

```
request ─► 1 normalize ─► 2 classify ─► 3 rules fast path ──hit──► 5 validate + expand ─► response
                                            │ miss                        ▲
                                            ▼                             │
                                       4 constrained LLM ─────────────────┘
```

### 1. Normalize (deterministic, all languages)

Shared helpers turn STT text into a canonical form. They run before both the rules and the LLM, so the model never has to do arithmetic on spelled numbers.

- Lowercase, strip punctuation, and apply the rbvoice `aliases` and `skip_words` from the catalog.
- **Numbers:** spoken digits and compounds become digits ("one one eight decimal five" → `118.5`, "centodiciotto virgola cinque" → `118.5`, "flight level eight five" → `FL85`, "five thousand" → `5000`). Port, or share, rbvoice's `parse_frequency` and its `text2num` usage.
- **Spelling alphabet:** runs of ICAO letters become a code ("lima india romeo zulu" → `LIRZ`, "india charlie delta" → `ICD`). Use rbvoice's `SPELLING_ALPHABET` logic and its `SPELLED_CODE_MIN` rule.
- **Units and keywords:** `feet|piedi`, `hectopascal`, `QNH|cu enne acca`, `squawk`, `decimal|virgola|punto`.

The normalizer emits the normalized text **and** a span map, so every value in the output can point back to the words it came from (`evidence`). This is how the rule "the LLM never invents numbers" is enforced: the expander rejects any number in the LLM output that is not present in the normalized text.

### 2. Classify: who is speaking to whom

The intercom carries everything. rbvoice's flight recordings show that 356 of 360 utterances were not meant for the avionics. Each utterance is classified as:

| `kind` | Example | Typical next step |
|---|---|---|
| `command` | "computer dimmi la quota" | command parser |
| `atc` | "I-CD climb 5000 ft" (ground → aircraft) | ATC parser |
| `readback` | "climb 5000 ft, India Charlie Delta" (pilot → ATC) | ATC parser, flagged as a readback |
| `other` | passenger talk, other traffic's chatter | `not_addressed` |

If RB-08 already matched a wake word, or the request came from push to talk, it says so in the request (`context.wake_word`, `source: "push_to_talk"`), and classification is skipped (`kind = command`). Without those, a cheap rules classifier runs first: callsign and phraseology markers suggest `atc`, catalog keywords suggest `command`. The LLM settles only the ambiguous cases.

### 3. Rules fast path

These are deterministic parsers over the normalized text:

- **Commands:** the rbvoice matching algorithm, reimplemented or imported (`detect_action` / `detect_parameter` with the same thresholds), so that rb-llm agrees with RB-08 on easy utterances. A hit above the high threshold (for example ≥ 0.9, no tie) returns immediately with `engine.path = "rules"`.
- **ATC:** pattern rules for the common instructions (QNH, squawk, frequency change, altitude, heading, runway clearances). A full-coverage parse returns immediately. A partial one goes to the LLM, with the found slots as hints.

The fast path keeps typical latency in milliseconds, and keeps the system working when the model is not loaded.

### 4. Constrained LLM

- **Prompt layout** (cache-friendly):
  1. Static system prompt (role, rules, output format): byte-identical for every request.
  2. Static catalog section for the task and language: identical until the catalog changes.
  3. Few-shot examples: static.
  4. Dynamic tail: session context (last component and parameter), own callsign, the normalized utterance and up to 2 STT alternatives.
- **Grammar:** a GBNF grammar is generated from the catalog at startup and whenever `PUT /v1/catalog` changes it. Only valid `component/parameter` pairs and valid ATC instruction types are expressible.
- **Sampling:** temperature 0 (greedy), `n_predict` capped (for example 48 tokens for commands, 128 for ATC), and the stop sequence at the grammar's end.
- **Thinking off.** For models with a reasoning mode (Qwen3, for example), disable it. It costs hundreds of tokens.
- **Confidence:** computed from token log-probabilities of the enum choices (`n_probs` in llama-server) and combined with the rules score. This is not the model's self-reported confidence.

### Compact internal form

The LLM writes a compact form and never the protocol JSON. Fewer output tokens means proportionally lower latency.

Command (≈ 12–20 tokens):

```json
{"k":"c","a":"G","p":"ems.cht"}
{"k":"c","a":"S","p":"radio.radio","f":"118.500"}
{"k":"c","a":"S","p":"autopilot.destination","ap":"LIRZ"}
{"k":"n"}
```

ATC (≈ 30–80 tokens):

```json
{"k":"a","cs":"ICD","st":"perugia tower","i":[["alt","climb",5000,"ft"],["qnh",1013]]}
```

`k`: `c` command, `a` atc, `r` readback, `n` none. `a`: `G`/`S`/`D`. `p` is `component.parameter`. Instruction tuples follow the instruction table in [aviation-domain.md](aviation-domain.md#atc-instruction-types). The grammar defines the compact form, and the expander maps it 1:1 onto the [protocol](protocol.md).

### 5. Validate and expand

- Check the compact output against the catalog again (defence in depth).
- Every number must appear in the normalized text (evidence check). Otherwise drop that slot, add a warning and lower the confidence.
- Apply safety rules: a SET below the SET threshold becomes `requires_confirmation: true` or `no_match`, and ATC `suggested_actions` always require confirmation.
- Callsign match against `context.own_callsign`: full registration, ICAO abbreviated form (first and last two letters, "I-CD"), and the spoken variants.
- Build the readback and display text from **templates**, never with the LLM.
- Fill in `engine` timings, build the response, and validate it against `schemas/parse-response.schema.json` in debug and test builds.

## Components (planned code layout)

```
rbllm/
  server.py            FastAPI app, routes, deadlines, single-flight queue
  protocol.py          pydantic models; JSON Schema exported to schemas/ (must match)
  catalog.py           loads rbvoice-cases.<lang>.json, builds keyword tables
  normalize/           numbers.py, spelling.py, units.py, aliases.py
  classify.py          rules classifier (kind)
  rules/               command.py (rbvoice-compatible matcher), atc.py (phraseology patterns)
  llm/
    adapter.py         Engine interface: complete(prompt, grammar, deadline) -> text, logprobs
    llamacpp.py        llama-server /completion client
    ollama.py          dev/bench adapter
    prompts/           static prompt templates per task and language
    grammar.py         GBNF generation from the catalog
  expand.py            compact form -> protocol, evidence check, safety rules
  readback.py          templates for readback / display per language
tests/                 unit, golden (examples/), routing parity with rbvoice
bench/                 llama-bench wrappers, eval runner, results/ (gitignored)
```

## Concurrency and deadlines

- One LLM request at a time (`--parallel 1`). The API keeps a queue of depth 1. A new request for the same `session_id` **supersedes** a queued one, because stale voice commands are worthless.
- Each request carries `options.deadline_ms` (default 2500). The server cancels generation when the deadline passes and answers `status: "error"`, `error.code: "timeout"`, so RB-08 can fall back in time.

## Catalog synchronization

The command vocabulary lives in rbvoice's case files. rb-llm gets it in one of two ways:

1. **File:** `--catalog-dir` points to the case files. This is used when co-located, or when the files are copied at install time.
2. **Push:** RB-08 calls `PUT /v1/catalog` at startup with the active case file. The response returns `catalog_hash`, and every parse request carries `catalog_hash`. A mismatch answers `409`, and RB-08 pushes again.

rbvoice remains the single source of truth.
