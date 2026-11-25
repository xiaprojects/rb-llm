# RB-08 AI Voice — website copy

Source text for the RB-08 page of www.rbavionics.com. Every example phrase below passed
`scripts/verify_phrases.py` (see [phrase-results.md](phrase-results.md)). Read
[README.md](README.md) before changing or adding claims.

---

## Headline

**Talk to your aircraft. In your own words.**

RB-08 AI Voice is the language engine for RB-08. It understands what you mean, not just the
keywords you remember. Ask where you are, how the engine is doing, or who is around you. It
answers from your aircraft's own instruments, and it keeps track of the ATC calls addressed to you.
It runs offline, on board, on open-source software.

*In development: an optional engine for RB-08, built on the open-source rb-llm project.*

## Benefits

**Speak naturally.** Say "how fast are we going?" or "put Perugia Approach on the standby". You do
not need a list of fixed commands: the engine works out the meaning of the request.

**Ask the aircraft.** Position, nearest airfield, altitude, heading, engine temperatures, traffic,
radio, UTC time. The answer comes from your own GPS, AHRS, engine monitor and traffic receiver,
read through the RB-01.

**No invented numbers.** The language model never calculates. Distances, bearings and clock
positions are computed by the software, and every number in an answer is checked against your
instrument data. When a value is missing, for example a fuel sensor that is not connected, you are
told it is not available.

**ATC calls for you.** A clearance for your callsign is turned into a short list: altitude,
heading, squawk, QNH, frequency, runway. You get the readback text to check against what you heard.
A new frequency is offered as a suggestion, and nothing changes until you confirm.

**Ignores the chatter.** Passengers talking, or a call for another aircraft: the engine recognises
it is not for you and does nothing.

**Confirms when unsure.** When a request to change something is not clear, the engine asks for
confirmation instead of guessing. The classic RB-08 voice recognition stays available as a
fallback.

**Offline and private.** No cloud and no subscription. Everything runs on a dedicated Raspberry
Pi 5 in the aircraft, and your voice and flight data stay on board.

**Open source, open models.** The software is GPL-3.0 and the default model (Qwen3, Apache-2.0)
is open-weight. You can inspect it, rebuild it, change it, and test new models with the same checks.

## Example phrases

Tested in English and Italian. Commands start with the wake word ("computer", "primary"), or
follow a press of the push-to-talk button. The sample answers are quoted as the engine produced them, from real RB-01 data captured on
the ground near Perugia.

### Ask the aircraft

| You say | RB-08 answers |
|---|---|
| "Where are we?" | "Latitude 43.10 north, longitude 12.25 east, 0.2 miles from Montemelino Airfield, bearing 135° true, elevation 731 ft." |
| "What's the nearest airfield?" | "The nearest airfield is Montemelino Airfield, 0.2 miles away, bearing 135° true, elevation 731 ft." |
| "How high are we?" | "We are at 763 feet MSL, with the barometric altimeter reading 546 feet." |
| "What time is it in UTC?" | "UTC time is 17:30:01 on 12 September 2026." |
| "How are the engine temperatures?" | Cylinder head and exhaust temperatures, probe by probe |
| "Is there any traffic around us?" | "There is 1 aircraft in traffic: RYR465X, 34,000 ft, 26.6 NM, bearing 175°, track 325°, 424 kt." |
| "How much fuel do we have?" | "Fuel remaining is not available: there is no reading from the fuel sensor." |
| "What frequency is the radio on?" | "Radio is active on 130.000 MHz and standby on 119.555 MHz." |
| "Dove siamo?" | "Latitudine 43,10 nord, longitudine 12,25 est, a 0,2 miglia da Montemelino Airfield, rilevamento 135." |
| "Qual è il campo più vicino?" | "Il campo più vicino è Montemelino Airfield, a 0,2 miglia, rilevamento 135°." |

### Voice commands

| You say | RB-08 does |
|---|---|
| "Computer, how fast are we going?" | Reads the ground speed |
| "Computer, how hot is the oil?" | Reads the oil temperature |
| "Computer, what are the cylinder temperatures?" | Reads the cylinder head temperatures |
| "Computer, any traffic around us?" | Reports nearby traffic |
| "Computer, tune the radio to one two two decimal five" | Sets standby to 122.500 |
| "Computer, put Perugia Approach on the standby" | Sets standby to the Perugia Approach frequency |
| "Computer, take me to Perugia" | Sets Perugia as the RB-01 destination |
| "Computer, direct to Lima India Romeo Zulu" | Sets LIRZ as the destination |
| "Computer, cancel the autopilot" | Cancels the destination |
| "Computer, start the timer" / "stop the timer" | Starts or stops the timer |
| "Primary, show the engine page" | Switches the primary display to the engine page |
| "Computer, quanto carburante nel secondo serbatoio?" | Reads fuel tank 2 |
| "Computer, imposta la radio su uno uno otto virgola cinque" | Sets standby to 118.500 |
| "Primario, visualizza mappa" | Switches the primary display to the map |
| "What a beautiful view of the lake" | Nothing: not addressed to the RB |

### ATC calls (own callsign I-ABCD)

| You hear | RB-08 shows |
|---|---|
| "India Charlie Delta, Perugia Tower, climb altitude five thousand feet, QNH one zero one three" | ALT CLB 5000 FT · QNH 1013 — readback: "Climb 5000 feet, QNH 1013, India Charlie Delta" |
| "India Charlie Delta, turn left heading two seven zero, descend two thousand feet, squawk four three two one" | readback: "Turn left heading 270, descend 2000 feet, squawk 4321, India Charlie Delta" |
| "India Charlie Delta, contact Perugia Approach one two zero decimal seven" | Suggests standby 120.700, applied only after you confirm |
| "India Charlie Delta, join right downwind runway zero one, report final" | readback: "Join right downwind runway 01, India Charlie Delta" |
| "India X-ray Yankee, report downwind runway zero one" | Recognised as a call for another aircraft: no action |
| "India Charlie Delta, salite a cinque mila piedi, QNH uno zero uno tre" | readback: "Salire 5000 piedi, QNH 1013, India Charlie Delta" |

## How it works

1. RB-08 hears you and turns your speech into text, on board.
2. The language engine finds the request in it, using a small open model running on a
   Raspberry Pi 5.
3. The software checks the result: every number against the words you said or against the
   instruments, and every setting against the RB safety rules.
4. RB-08 carries it out on the RB-01, or speaks the answer.

## Limitations

Speech recognition and language models make mistakes: they can mishear a word, or pick the wrong
value. RB-08 AI Voice is supplementary information, not certified avionics. Check every spoken
value on the display or on the approved instruments, never use it for a safety-critical action, and
always listen to, read back and follow ATC yourself. The ATC feature helps your awareness. It does
not replace your own listening and readback.

## Short versions

- **One line:** Talk to your aircraft in your own words: answers from your own instruments, offline
  and open source.
- **Card text (≤ 120 characters):** Natural-language voice control and ATC awareness, offline on a
  Raspberry Pi 5.
- **Chips:** Natural language · Offline · ATC readback · Open source
