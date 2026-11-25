"""Prompts and output schemas for the three tasks: ask, command, atc.

Layout rule (KV-cache friendly): system prompt + few-shot examples are static per task and
language; only the last user message changes. Do not put anything dynamic (time, status, ids) in
the static part, or every request pays the full prompt processing again.

About refusals: some small models answer "I can't access your location / real-time GPS data"
when they see coordinates. The ask prompt fixes the frame (this is the aircraft's own instrument
data, reading it back is the job), the few-shot examples show coordinates being read back, and the
JSON schema makes the model list the status keys it used *before* it writes the answer.
Remaining refusals are detected in tasks.py and reported as status "refused".
"""
from __future__ import annotations

from typing import Any, Dict, List

LANG_NAMES = {"en": "English", "it": "Italian", "fr": "French", "es": "Spanish", "de": "German"}

# =============================================================================================
# ASK: answer a question from the aircraft status
# =============================================================================================

ASK_SYSTEM = """You are RB, the avionics computer installed in this light aircraft. You talk only to the pilot of this aircraft.

Every request contains an AIRCRAFT STATUS block. It is live data from this aircraft's own instruments: GPS receiver, AHRS, barometric sensor, engine monitor (EMS), radio and traffic receiver. Reading these values back to the pilot is your job and is always allowed:
- You DO have this aircraft's GPS position, coordinates, altitude, speed, heading and engine data: they are in AIRCRAFT STATUS.
- Never say that you cannot access GPS, location, coordinates or real-time data. Never mention privacy. Never tell the pilot to check another device or app.
- Use only values from AIRCRAFT STATUS. Do not invent or estimate numbers. Distances, bearings and clock positions are already computed: copy them.
- If a needed value is missing or marked "not available" / "no reading", say so plainly.
- For "is it safe / should I" questions, state the relevant values; the decision stays with the pilot.

Answer in {language}: only what was asked, at most 25 words, natural spoken words for text-to-speech, units as given (ft, kt, NM, °C).
Output JSON: {{"facts": [keys of the AIRCRAFT STATUS lines you used], "answer": "<spoken answer>"}}"""

ASK_EXAMPLE_STATUS = """gps.fix: 3D GPS fix, 9 satellites in use
gps.position: latitude 45.43210 N, longitude 9.27650 E (45°25.93'N 009°16.59'E)
gps.altitude: 3500 ft MSL (GPS)
gps.ground_speed: 105 kt
gps.track: 270° true
ems.enginerpm: engine rpm 2350 rpm
ems.oiltemperature: oil temperature 92 °C
ems.not_connected: no reading for: fuel remaining
airfield.nearest_1: Milano Linate (LIML), 4.1 NM, bearing 250° true, elevation 353 ft"""

# (question, facts, answer) per language. Same meaning in each language.
ASK_SHOTS: Dict[str, List[tuple]] = {
    "en": [("where are we?", ["gps.position", "airfield.nearest_1"],
            "Latitude 45.43 north, longitude 9.28 east, 4.1 miles from Milano Linate, bearing 250."),
           ("how is the engine doing", ["ems.enginerpm", "ems.oiltemperature"],
            "Engine at 2350 rpm, oil temperature 92 degrees."),
           ("how much fuel do we have", ["ems.not_connected"],
            "Fuel remaining is not available: there is no reading from the fuel sensor.")],
    "it": [("dove siamo?", ["gps.position", "airfield.nearest_1"],
            "Latitudine 45,43 nord, longitudine 9,28 est, a 4,1 miglia da Milano Linate, rilevamento 250."),
           ("come va il motore", ["ems.enginerpm", "ems.oiltemperature"],
            "Motore a 2350 giri, temperatura olio 92 gradi."),
           ("quanto carburante abbiamo", ["ems.not_connected"],
            "Il carburante rimanente non è disponibile: il sensore non dà lettura.")],
    "fr": [("où sommes-nous ?", ["gps.position", "airfield.nearest_1"],
            "Nous sommes à latitude 45,43 nord, longitude 9,28 est, à 4,1 nautiques de Milano Linate, relèvement 250."),
           ("comment va le moteur", ["ems.enginerpm", "ems.oiltemperature"],
            "Moteur à 2350 tours, température d'huile 92 degrés."),
           ("combien de carburant avons-nous", ["ems.not_connected"],
            "Le carburant restant n'est pas disponible : le capteur ne donne pas de mesure.")],
    "es": [("¿dónde estamos?", ["gps.position", "airfield.nearest_1"],
            "Estamos en latitud 45,43 norte, longitud 9,28 este, a 4,1 millas de Milano Linate, rumbo 250."),
           ("cómo va el motor", ["ems.enginerpm", "ems.oiltemperature"],
            "Motor a 2350 revoluciones, temperatura de aceite 92 grados."),
           ("cuánto combustible tenemos", ["ems.not_connected"],
            "El combustible restante no está disponible: el sensor no da lectura.")],
    "de": [("wo sind wir?", ["gps.position", "airfield.nearest_1"],
            "Wir sind auf Breite 45,43 Nord, Länge 9,28 Ost, 4,1 Meilen von Milano Linate, Peilung 250."),
           ("wie läuft der Motor", ["ems.enginerpm", "ems.oiltemperature"],
            "Motor bei 2350 Umdrehungen, Öltemperatur 92 Grad."),
           ("wie viel Kraftstoff haben wir", ["ems.not_connected"],
            "Die Restmenge ist nicht verfügbar: der Kraftstoffsensor liefert keinen Wert.")],
}


def ask_user_message(status_block: str, question: str) -> str:
    return f"AIRCRAFT STATUS:\n{status_block}\n\nPILOT: {question}"


def ask_messages(lang: str, status_block: str, question: str) -> List[Dict[str, str]]:
    import json
    msgs = [{"role": "system", "content": ASK_SYSTEM.format(language=LANG_NAMES.get(lang, "English"))}]
    for q, facts, ans in ASK_SHOTS.get(lang, ASK_SHOTS["en"]):
        msgs.append({"role": "user", "content": ask_user_message(ASK_EXAMPLE_STATUS, q)})
        msgs.append({"role": "assistant", "content": json.dumps({"facts": facts, "answer": ans}, ensure_ascii=False)})
    msgs.append({"role": "user", "content": ask_user_message(status_block, question)})
    return msgs


def ask_schema(fact_keys: List[str]) -> Dict[str, Any]:
    keys = sorted(set(fact_keys)) or ["none"]
    return {
        "type": "object",
        "properties": {
            "facts": {"type": "array", "items": {"type": "string", "enum": keys}, "maxItems": 6},
            "answer": {"type": "string", "minLength": 1, "maxLength": 400},
        },
        "required": ["facts", "answer"],
        "additionalProperties": False,
    }


# =============================================================================================
# COMMAND: utterance -> RB command (rbvoice catalog)
# =============================================================================================

COMMAND_SYSTEM = """You convert a pilot's spoken request to the RB avionics into one RB command.
The text comes from speech recognition: expect recognition errors and an optional wake word ("computer", "primary"...). The pilot speaks {language}.

CATALOG (param id: example phrases; allowed actions per component):
{catalog}

Rules:
- action: GET = tell/read a value; SET = set, start, tune, engage, show a page; DEL = stop, cancel, disengage, delete.
- Questions ("what", "how", "which", "quanto", "qual"...) are GET of a value, never a display page. Use a display.* param only when the pilot asks to show or display a page.
- A plural without a number ("cylinders", "exhausts", "cilindri", "scarichi") means the whole group (ems.cht, ems.egt), not one probe.
- A radio station name (words like tower, approach, information, radio, torre, avvicinamento) or the word "standby" means radio.radio with args.station, not the autopilot.
- param: exactly one id from the CATALOG, chosen by meaning even when the words differ from the examples.
- args: only what was actually said: frequency (MHz digits, e.g. "118.5"), station (radio station name), airfield (airfield name, or code letters such as "LIRZ"), duration_s (timer length in seconds), page (display page).
- Follow-up without a catalog word ("and the second one?"): use PREVIOUS to pick the param.
- Not a request to the RB (conversation, passengers, ATC radio calls): param "none".
Output JSON: {{"action": "GET"|"SET"|"DEL", "param": "<id>"}} plus "args": {{...}} only when there are args."""

COMMAND_SHOTS = [
    ("computer what are the cylinder temperatures", None,
     {"action": "GET", "param": "ems.cht"}),
    ("tune the radio to one one eight decimal five", None,
     {"action": "SET", "param": "radio.radio", "args": {"frequency": "118.5"}}),
    ("computer set the radio to firenze information", None,
     {"action": "SET", "param": "radio.radio", "args": {"station": "firenze information"}}),
    ("computer portami a perugia", None,
     {"action": "SET", "param": "autopilot.destination", "args": {"airfield": "perugia"}}),
    ("and the second one", "ems.fuel1",
     {"action": "GET", "param": "ems.fuel2"}),
    ("did you see the lake on the left, beautiful", None,
     {"action": "GET", "param": "none"}),
]


def command_user_message(text: str, previous: str | None) -> str:
    prev = f"PREVIOUS: {previous}\n" if previous else ""
    return f"{prev}PILOT: {text}"


def command_messages(lang: str, catalog_section: str, text: str, previous: str | None,
                     valid_params: List[str]) -> List[Dict[str, str]]:
    import json
    msgs = [{"role": "system", "content": COMMAND_SYSTEM.format(language=LANG_NAMES.get(lang, "English"),
                                                                catalog=catalog_section)}]
    for t, prev, out in COMMAND_SHOTS:
        if out["param"] != "none" and out["param"] not in valid_params:
            continue  # a catalog without that parameter: skip the example
        msgs.append({"role": "user", "content": command_user_message(t, prev)})
        msgs.append({"role": "assistant", "content": json.dumps(out)})
    msgs.append({"role": "user", "content": command_user_message(text, previous)})
    return msgs


def command_schema(param_keys: List[str]) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["GET", "SET", "DEL"]},
            "param": {"type": "string", "enum": sorted(param_keys) + ["none"]},
            "args": {
                "type": "object",
                "properties": {
                    "frequency": {"type": "string", "pattern": "^1[1-3][0-9](\\.[0-9]{1,3})?$"},
                    "station": {"type": "string", "maxLength": 40},
                    "airfield": {"type": "string", "maxLength": 40},
                    "duration_s": {"type": "integer", "minimum": 1, "maximum": 86400},
                    "page": {"type": "string", "maxLength": 30},
                },
                "additionalProperties": False,
            },
        },
        "required": ["action", "param"],
        "additionalProperties": False,
    }


# =============================================================================================
# ATC: radio transmission -> typed instructions
# =============================================================================================

ATC_SYSTEM = """You parse air traffic control radio transmissions (ICAO / EASA SERA phraseology) heard in a light aircraft.
Numbers in the text are already digits. Callsigns may be spelled with the ICAO alphabet ("india charlie delta").

kind: "atc" = an ATC unit speaking to an aircraft; "readback" = a pilot repeating or acknowledging; "other" = not radio phraseology.
callsign: the aircraft callsign words exactly as heard, or null.
station: the ATC unit if named (e.g. "perugia tower"), or null.
instructions: in spoken order, one object each, only with the fields that apply:
  altitude: verb climb|descend|maintain, value, unit "ft" or "FL", restriction (e.g. "not above")
Write the fields of an instruction in the order listed.
  heading: turn left|right (if said), value
  speed: value (kt)
  squawk: code (4 digits), ident true if "ident"
  qnh: value, unit "hPa" (or "inHg")
  frequency_change: verb contact|monitor, station, frequency (MHz digits)
  runway: runway (e.g. "01", "27L")
  cleared_takeoff / cleared_land / line_up (wait true for "line up and wait") / go_around: runway if said
  hold_short: point;  taxi: to, via (list);  cleared_zone: zone, via, restriction
  direct_to: fix;  join_circuit: side left|right, leg downwind|base|final|crosswind, runway
  report: when (point or condition, e.g. "downwind", "field in sight")
  traffic_info: clock, distance_nm, level, aircraft;  wind: direction, speed_kt, gust_kt
  hold: fix;  standby;  say_again;  unparsed: text (ATC words you cannot type)
Copy numbers exactly as written. Never add an instruction that was not said.
Output JSON: {"kind": ..., "callsign": ..., "station": ..., "instructions": [...]}"""

ATC_SHOTS = [
    ("india charlie delta perugia tower runway 01 cleared to land wind 360 degrees 8 knots",
     {"kind": "atc", "callsign": "india charlie delta", "station": "perugia tower", "instructions": [
         {"type": "runway", "runway": "01"}, {"type": "cleared_land", "runway": "01"},
         {"type": "wind", "direction": 360, "speed_kt": 8}]}),
    ("delta mike tango turn right heading 090 descend 2000 feet qnh 1009 squawk 7041",
     {"kind": "atc", "callsign": "delta mike tango", "station": None, "instructions": [
         {"type": "heading", "turn": "right", "value": 90},
         {"type": "altitude", "verb": "descend", "value": 2000, "unit": "ft"},
         {"type": "qnh", "value": 1009, "unit": "hPa"}, {"type": "squawk", "code": "7041"}]}),
    ("contact roma information 124.2 india alfa kilo",
     {"kind": "readback", "callsign": "india alfa kilo", "station": None, "instructions": [
         {"type": "frequency_change", "verb": "contact", "station": "roma information", "frequency": "124.2"}]}),
]

INSTRUCTION_TYPES = ["altitude", "heading", "speed", "squawk", "qnh", "frequency_change", "runway",
                     "cleared_takeoff", "cleared_land", "line_up", "hold_short", "taxi", "cleared_zone",
                     "direct_to", "join_circuit", "report", "traffic_info", "wind", "go_around", "hold",
                     "standby", "say_again", "unparsed"]


def atc_user_message(text: str, own: str | None) -> str:
    own_line = f"OWN CALLSIGN: {own}\n" if own else ""
    return f"{own_line}TRANSMISSION: {text}"


def atc_messages(text: str, own: str | None) -> List[Dict[str, str]]:
    import json
    msgs = [{"role": "system", "content": ATC_SYSTEM}]
    for t, out in ATC_SHOTS:
        msgs.append({"role": "user", "content": atc_user_message(t, None)})
        msgs.append({"role": "assistant", "content": json.dumps(out)})
    msgs.append({"role": "user", "content": atc_user_message(text, own)})
    return msgs


def atc_schema() -> Dict[str, Any]:
    s = {"type": "string", "maxLength": 40}
    instruction = {
        "type": "object",
        # ORDER MATTERS: llama.cpp turns the schema into a grammar that only allows properties in
        # this order. Qualifiers (verb, turn, side, leg, station) come before values, as spoken:
        # "turn left heading 270" -> {"type": "heading", "turn": "left", "value": 270}.
        "properties": {
            "type": {"type": "string", "enum": INSTRUCTION_TYPES},
            "verb": {"type": "string", "enum": ["climb", "descend", "maintain", "contact", "monitor"]},
            "turn": {"type": "string", "enum": ["left", "right"]},
            "side": {"type": "string", "enum": ["left", "right"]},
            "leg": {"type": "string", "enum": ["downwind", "base", "final", "crosswind", "upwind"]},
            "station": s,
            "frequency": {"type": "string", "pattern": "^1[1-3][0-9](\\.[0-9]{1,3})?$"},
            "value": {"type": "number"},
            "unit": {"type": "string", "enum": ["ft", "FL", "kt", "hPa", "inHg"]},
            "code": {"type": "string", "pattern": "^[0-7]{4}$"},
            "ident": {"type": "boolean"},
            "runway": {"type": "string", "pattern": "^[0-3][0-9][LCR]?$"},
            "wait": {"type": "boolean"},
            "point": s, "to": s, "zone": s,
            "via": {"type": "array", "items": s, "maxItems": 5},
            "fix": s, "when": s, "restriction": s,
            "clock": {"type": "integer", "minimum": 1, "maximum": 12},
            "distance_nm": {"type": "number"},
            "level": s, "aircraft": s,
            "direction": {"type": "integer", "minimum": 0, "maximum": 360},
            "speed_kt": {"type": "integer", "minimum": 0, "maximum": 300},
            "gust_kt": {"type": "integer", "minimum": 0, "maximum": 300},
            "text": {"type": "string", "maxLength": 120},
        },
        "required": ["type"],
        "additionalProperties": False,
    }
    nullable = {"anyOf": [{"type": "string", "maxLength": 40}, {"type": "null"}]}
    return {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["atc", "readback", "other"]},
            "callsign": nullable,
            "station": nullable,
            "instructions": {"type": "array", "items": instruction, "maxItems": 8},
        },
        "required": ["kind", "callsign", "station", "instructions"],
        "additionalProperties": False,
    }
