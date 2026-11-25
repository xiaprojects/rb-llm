# Integration with RB-08 (rbvoice)

This document describes what has to change in `../rbvoice` so that it can use rb-llm. These changes belong to the rbvoice repository and will be made there. This page is the design they follow.

## Principle: rb-llm can only make RB-08 better

- rbvoice keeps its Vosk → keyword matcher → handlers → Piper pipeline unchanged as the **baseline and fallback**.
- If rb-llm is absent, slow, degraded or unsure, the pilot gets exactly today's behaviour.
- The rbvoice safety rules stay in the rbvoice handlers. These are explicit intent for direct-to, the airfield clear-match rule, and standby-only valid COM frequencies. rb-llm output goes through the same handlers.

## Where it hooks in

Today, in `rbvoice.py`, the audio loop calls:

```python
reply = handle_utterance(text, ctx, CONFIG, require_wake_word=not triggered)
#if(reply == False or reply == ""):
#    reply = ollama_generate(text,ctx.Context,history)
```

That commented `ollama_generate` line is a free-text chat fallback, which this design replaces. rb-llm returns an **intent**, not a reply, so the hook sits *inside* `assistant.handle_utterance`, between the wake-word step and the dispatch:

```
handle_utterance(raw_text)
  1 aliases / skip words / wake word        (unchanged)
  2 keyword matcher: detect_action + detect_parameter → (action, component, parameter, score)
  3 NEW  rbllm.decide(...)  → maybe replace (action, component, parameter, args)
  4 dispatch to handler                      (unchanged, plus optional args)
```

## Modes (`--llm-mode` in `rbvoice.sh`)

| Mode | Behaviour | Use |
|---|---|---|
| `off` (default) | rb-llm never called | Current product |
| `shadow` | Keyword result is used. rb-llm is called asynchronously, and both results plus the timing are logged to `--rec` | **First field deployment.** Collects real disagreements with no risk |
| `fallback` | rb-llm is called only when the keyword matcher has no parameter or scores below 0.6, or when the utterance has no wake word but `source` could be ATC | Second step |
| `first` | rb-llm is called first. The keyword matcher is used on `no_match` / `error` / timeout | After evaluation shows that rb-llm is better |

New `rbvoice.py` options: `--llm-url http://rb-llm.local:8091`, `--llm-mode`, `--llm-deadline-ms 2500`, `--own-callsign I-ABCD`.

## Passing rb-llm arguments to the handlers

The special handlers (`handle_radio`, `handle_autopilot`, `handle_airfields`, `handle_timers`, `handle_display`) re-read the raw `text` to find frequencies, names and spelled codes. rb-llm returns those already extracted (`command.args`). The proposed change is an optional `args: dict | None = None` parameter on each special handler:

| `args` key | Handler use |
|---|---|
| `frequency_mhz` | skip `parse_frequency`, still check `is_com_frequency` |
| `station` | match against the merged station list with the existing word-by-word rule |
| `airfield.code` / `airfield.name` | feed `find_airfields` with the code or name; the clear-match rule still applies |
| `duration_s` | timer countdown |
| `page` | display page |

When `args` is `None`, the handlers behave as today. This keeps every existing test valid.

## ATC flow (later phase)

Today, utterances without a wake word are dropped (`handle_utterance` returns `""`). With `--llm-mode` other than `off` and `--own-callsign` set, RB-08 can send them with `task: "auto"`:

1. `kind: "atc"`, `addressee: "own_aircraft"`: show `answer.display` on the RB-01 and keep `atc` as `last_clearance` for a readback check. **Do not speak over the radio audio** by default.
2. `suggested_actions` (for example a new standby frequency): offer it on the display, and execute it only on pilot confirmation (button, or "computer, conferma").
3. Anything else: ignore.

This needs an RB-01 display surface for clearances. It is an open question for roastbeef (see [roadmap](roadmap.md#open-questions)).

## Startup sequence

1. rbvoice starts as today (startup phrase, bridge keys).
2. A background thread polls `GET /v1/health`. When it reports `ready` or `degraded`, rbvoice sends `PUT /v1/catalog` with the active case file and stores `catalog_hash`.
3. On `409`, it re-sends the catalog once. If rb-llm is unreachable, it retries every 30 s and does nothing else.
4. Optional: post `voice_llm: 1/0` to `/bridge/float`, so the RB-01 voice page can show whether the LLM is available.

## Client code size

The client is about 150 lines in a new `rbllm_client.py`, using only `requests`, which rbvoice already depends on. It holds the health thread, the catalog push, `parse()` with the deadline, and response checks. There are no new venv packages on RB-08.

## Testing on the rbvoice side

- Extend the `tests/conftest.py` fake server with a fake rb-llm (`/v1/health`, `/v1/catalog`, `/v1/parse`) that serves this repo's `examples/` responses.
- Add a routing-parity test: for every `rbvoice-test.<lang>.txt` phrase, `first` mode with the fake must route the same as `off` mode.
- Add a timeout test: a fake that sleeps past the deadline must still produce the keyword-matcher reply.
