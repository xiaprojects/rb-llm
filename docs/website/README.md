# Website material for RB-08 AI Voice

| File | What it is |
|---|---|
| [rb-08-ai-voice.md](rb-08-ai-voice.md) | Website copy: headline, benefits, example phrases with sample answers, how it works, limitations, short versions |
| [rb-08-ai-section.html](rb-08-ai-section.html) | The same content as a section in the site's markup (CRLF, existing CSS classes only). Paste it into `www.rbavionics.com/templates/body/rb-08.html` between the Product and Downloads sections |
| [phrase-results.md](phrase-results.md) | Verification results: every candidate phrase, what the engine did, pass or fail (generated) |
| [phrase-results.json](phrase-results.json) | The same results, machine-readable (generated) |

## The rule: publish only what passed

The website's own rules say it must not invent product specs or safety claims. So:

1. Every example phrase and every quoted answer must come from `phrase-results.md`, quoted exactly as the engine produced it. Do not tidy up the answers.
2. To add a phrase, add it to `data/website-phrases.json` with its expected result, run the verifier, and use it only if it passes.
3. After any change to the model, the prompts or `tasks.py`, run the verifier again. A change can break phrases that passed before.

```bash
make run WARM_LANG=en              # warm the language you verify
python3 scripts/verify_phrases.py --lang en
make run WARM_LANG=it
python3 scripts/verify_phrases.py --lang it
```

## What the copy deliberately does not claim

- **No speed figures.** The results were measured on an x86 dev laptop, not on a Raspberry Pi 5. Publish latency only after Phase 0 on the Pi ([roadmap](../roadmap.md)).
- **No French, Spanish or German phrases.** The command vocabulary exists for those languages, but no phrase has been verified in them yet.
- **No certification or workload claims.** It is described as supplementary information, with the limitations paragraph kept next to the examples.
- **"In development."** rb-llm is not yet integrated in the released RB-08 (see [integration-rbvoice.md](../integration-rbvoice.md)). Remove that label only when it ships.
- **The known failure is left out.** "How far is Molinella airfield?" failed: the model gave the nearest airfield's distance instead. Questions about a specific, non-nearest airfield are not shown.

## Note on the current RB-08 page

The current page says "LLM-based AI command handling", while the released RB-08 has no LLM (rb-manuals finding F1, open question Q-08A-9d808). The new section labels the AI engine as in development and optional. Consider aligning the existing feature list with it.
