"""Task logic: request -> prompt -> constrained LLM -> validation -> protocol response.

The LLM only maps words to structure. Everything that must be right (numbers, safety rules,
readbacks, callsign matching) is checked or produced here, in code.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from . import PROTOCOL, prompts
from .catalog import Catalog
from .llama_client import Completion, EngineError, LlamaClient
from .normalize import ICAO_LETTERS, normalize, numbers_in, spelled_letters
from .status import AircraftStatus, select_facts

SET_CONFIRM_BELOW = 0.85
NO_MATCH_BELOW = 0.6
MAX_TOKENS = {"command": 64, "atc": 320, "ask": 160}
# Default deadlines when the request has none (RB-08 should always send its own deadline_ms).
# Tuned for a Pi 5 with a 1.7B model; override with `serve --deadline-ms`.
DEFAULT_DEADLINE_MS = {"command": 4000, "atc": 8000, "ask": 12000}
# llama-server slot per task (engine runs with --parallel 3): keeps each static prompt cached
SLOT = {"command": 0, "atc": 1, "ask": 2}

# Refusal phrases seen from small models when they are given coordinates ("I can't access GPS")
REFUSAL = re.compile("|".join([
    r"\bi (?:can ?not|can't|cannot|am unable to|'m unable to|do not|don't) (?:access|provide|share|determine|know|track|see)",
    r"\b(?:no|don't have|do not have) access to (?:your |the )?(?:real[- ]time|gps|location|position|current)",
    r"\breal[- ]time (?:data|location|gps|information)\b.*\b(?:not|unable|can't|cannot)",
    r"\bas an ai\b", r"\blanguage model\b", r"\bprivacy\b",
    r"\bnon (?:posso|sono in grado di|ho accesso)\b", r"\bje ne peux pas\b", r"\bje n'ai pas accès\b",
    r"\bno (?:puedo|tengo acceso)\b", r"\bich kann (?:nicht|keine)\b", r"\bkeinen zugriff\b",
]), re.IGNORECASE)

NO_DATA = {"en": "Aircraft data is not available.", "it": "I dati dell'aereo non sono disponibili.",
           "fr": "Les données de l'avion ne sont pas disponibles.",
           "es": "Los datos del avión no están disponibles.", "de": "Die Flugzeugdaten sind nicht verfügbar."}

ATC_MARKERS = re.compile(
    r"\b(qnh|squawk|climb|descend|maintain|runway|cleared|contact|monitor|taxi|line up|hold short|"
    r"report|wind|go around|flight level|pista|salire|scendere|autorizzat\w*|riportate|vento|"
    r"mantenere|contattate|livello di volo)\b")
TYPE_PREFIXES = {"cessna", "piper", "diamond", "cirrus", "robin", "tecnam", "socata", "beech", "mooney",
                 "ultralight", "glider", "aliante", "helicopter", "elicottero"}


# =============================================================================================
# helpers
# =============================================================================================

def _response(req: Dict[str, Any], **kw: Any) -> Dict[str, Any]:
    r = {"protocol": PROTOCOL, "request_id": req.get("request_id", ""), "status": "ok", "kind": "unknown",
         "engine": {"path": "none", "model": None, "latency_ms": 0}, "warnings": [], "error": None}
    r.update(kw)
    return r


def _engine_info(c: Optional[Completion], t0: float, path: str = "llm") -> Dict[str, Any]:
    info: Dict[str, Any] = {"path": path if c else "none", "model": c.model if c else None,
                            "latency_ms": int((time.monotonic() - t0) * 1000)}
    if c:
        info.update(queue_ms=c.queue_ms, tokens_in=c.tokens_in, tokens_out=c.tokens_out,
                    cached_tokens=c.cached_tokens, tok_per_s=c.tok_per_s)
    return info


def _error(req: Dict[str, Any], t0: float, code: str, message: str, model: Optional[str] = None) -> Dict[str, Any]:
    return _response(req, status="error", kind="unknown",
                     engine={"path": "none", "model": model, "latency_ms": int((time.monotonic() - t0) * 1000)},
                     error={"code": code, "message": message})


def _deadline(req: Dict[str, Any], task: str) -> float:
    ms = int((req.get("options") or {}).get("deadline_ms") or DEFAULT_DEADLINE_MS[task])
    return time.monotonic() + ms / 1000.0


def freq3(f: Any) -> Optional[str]:
    try:
        v = float(f)
    except (TypeError, ValueError):
        return None
    return f"{v:.3f}" if 108.0 <= v < 137.0 else None


def is_com_frequency(f: str) -> bool:
    """VHF COM 118.000-136.990, as rbvoice's is_com_frequency."""
    try:
        return 118.0 <= float(f) <= 136.99
    except ValueError:
        return False


def _has_number(pool: List[float], v: float, rel: float = 0.0) -> bool:
    return any(abs(p - v) <= max(1e-6, rel * abs(v)) for p in pool)


# =============================================================================================
# callsigns
# =============================================================================================

def own_forms(own: Dict[str, Any]) -> Tuple[str, str, List[str]]:
    """('IABCD', 'ICD', spoken forms) from {'registration': 'I-ABCD', 'spoken': [...]}."""
    reg = re.sub(r"[^A-Z0-9]", "", (own.get("registration") or "").upper())
    abbr = reg[0] + reg[-2:] if len(reg) >= 4 else reg
    spoken = [s.lower().strip() for s in own.get("spoken") or []]
    return reg, abbr, spoken


def spoken_letters(letters: str) -> str:
    inv: Dict[str, str] = {}
    for word, letter in ICAO_LETTERS.items():
        inv.setdefault(letter, word)
    return " ".join(inv.get(ch, ch).capitalize() for ch in letters)


def find_own_callsign(text: str, own: Optional[Dict[str, Any]]) -> Optional[str]:
    """Own callsign (full or abbreviated, spelled) at the start or end of a transmission, else None."""
    if not own or not own.get("registration"):
        return None
    reg, abbr, spoken = own_forms(own)
    forms = sorted({spoken_letters(reg).lower(), spoken_letters(abbr).lower(), *spoken}, key=len, reverse=True)
    for f in forms:
        if f and (text.startswith(f + " ") or text.endswith(" " + f) or text == f):
            return f
    return None


def match_callsign(heard: Optional[str], own: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not heard:
        return {"heard": None, "match": "none", "score": 0.0}
    h = heard.lower().strip()
    if not own or not own.get("registration"):
        return {"heard": h, "match": "other", "score": 0.0}
    reg, abbr, spoken = own_forms(own)
    words = h.replace("-", " ").split()
    if h in spoken:
        return {"heard": h, "match": "own", "score": 1.0}
    prefix = words[0] in TYPE_PREFIXES if words else False
    letters = spelled_letters(words[1:] if prefix else words)
    if letters and (letters == reg or letters == abbr):
        return {"heard": h, "match": "own", "score": 1.0}
    if prefix and len(letters) >= 2 and reg.endswith(letters):
        return {"heard": h, "match": "own", "score": 0.9}
    common = sum(1 for a, b in zip(letters[::-1], reg[::-1], strict=False) if a == b)
    return {"heard": h, "match": "other", "score": round(common / max(len(reg), 1), 2)}


# =============================================================================================
# ASK
# =============================================================================================

def is_refusal(answer: str) -> bool:
    return bool(REFUSAL.search(answer or ""))


def _answer_numbers(answer: str) -> List[List[float]]:
    """Each number of the answer with its readings: "34,000" may be 34000 (English thousands) or 34.0."""
    out = []
    for m in re.finditer(r"(?<![\w.,])\d{1,3}(?:,\d{3})+(?![\d])|(?<![\w.])-?\d+(?:[.,]\d+)?", answer):
        tok = m.group(0)
        readings = [float(tok.replace(",", "."))] if tok.count(",") <= 1 else []
        if re.fullmatch(r"\d{1,3}(?:,\d{3})+", tok):
            readings.append(float(tok.replace(",", "")))
        out.append(readings or [float(tok.replace(",", ""))])
    return out


def ungrounded_numbers(answer: str, status: AircraftStatus) -> List[float]:
    pool = status.numbers() + [n for f in status.facts for n in numbers_in(f.text)]
    bad = []
    for readings in _answer_numbers(answer):
        if any(_grounded(n, pool) for n in readings):
            continue
        bad.append(readings[-1])
    return bad


def _grounded(n: float, pool: List[float]) -> bool:
    return _has_number(pool, n, rel=0.01) or any(abs(round(p, 1) - n) < 0.051 or abs(round(p) - n) < 0.51
                                                   for p in pool)


def run_ask(req: Dict[str, Any], engine: LlamaClient, status: AircraftStatus) -> Dict[str, Any]:
    t0 = time.monotonic()
    lang = req.get("lang", "en")
    debug = bool((req.get("options") or {}).get("debug"))
    if not status.facts:
        return _response(req, status="no_data", kind="ask", answer={"speak": NO_DATA.get(lang, NO_DATA["en"]),
                         "lang": lang}, facts_used=[], status_source=status.source,
                         engine=_engine_info(None, t0))
    deadline = _deadline(req, "ask")
    full_status = status
    status = select_facts(status, req["text"])  # fewer tokens to re-process per request
    messages = prompts.ask_messages(lang, status.prompt_block(), req["text"].strip())
    schema = prompts.ask_schema(status.keys())
    warnings: List[str] = []
    try:
        c = engine.complete_json(messages, schema, MAX_TOKENS["ask"], deadline, SLOT["ask"])
        answer = (c.data.get("answer") or "").strip()
        retried = False
        if is_refusal(answer) and deadline - time.monotonic() > 2.0:
            # one corrective retry: restate the frame and ask again
            messages = messages + [
                {"role": "assistant", "content": c.raw},
                {"role": "user", "content": "That is wrong: AIRCRAFT STATUS above is this aircraft's own instrument "
                                            "data and you have it. Answer the pilot's question using it."}]
            c = engine.complete_json(messages, schema, MAX_TOKENS["ask"], deadline, SLOT["ask"])
            answer = (c.data.get("answer") or "").strip()
            retried = True
            warnings.append("first answer was a refusal; retried once")
    except EngineError as e:
        return _error(req, t0, e.code, str(e), engine.model_name or None)

    facts_used = [k for k in c.data.get("facts") or [] if k in status.keys()]
    st = "ok"
    if is_refusal(answer):
        st = "refused"
        warnings.append("model refused to use the aircraft status; try another model (make check-refusal)")
    # only the facts the model was shown count as evidence (a loose match against unrelated facts,
    # e.g. "34.6 NM" vs a 34 °C cylinder, would hide an invented number)
    bad = ungrounded_numbers(answer, status)
    if bad:
        warnings.append("numbers not found in aircraft status: " + ", ".join(f"{b:g}" for b in bad))
    resp = _response(req, status=st, kind="ask", answer={"speak": answer, "lang": lang},
                     facts_used=facts_used, status_source=status.source,
                     engine=_engine_info(c, t0), warnings=warnings)
    if debug:
        resp["debug"] = {"raw": c.raw, "retried": retried, "status_block": status.prompt_block(),
                         "facts_sent": f"{len(status.facts)}/{len(full_status.facts)}",
                         "prompt_ms": c.extra.get("prompt_ms"), "predicted_ms": c.extra.get("predicted_ms")}
    return resp


# =============================================================================================
# COMMAND
# =============================================================================================

def run_command(req: Dict[str, Any], engine: LlamaClient, catalog: Catalog) -> Dict[str, Any]:
    t0 = time.monotonic()
    ctx = req.get("context") or {}
    debug = bool((req.get("options") or {}).get("debug"))
    norm = normalize(req["text"], req.get("lang", "en"), catalog.aliases)
    sess = ctx.get("session") or {}
    previous = None
    if sess.get("last_component") and sess.get("last_parameter"):
        previous = f"{sess['last_component']}.{sess['last_parameter']}"
    messages = prompts.command_messages(catalog.lang, catalog.prompt_section(), norm, previous, catalog.param_keys())
    try:
        c = engine.complete_json(messages, prompts.command_schema(catalog.param_keys()),
                                 MAX_TOKENS["command"], _deadline(req, "command"), SLOT["command"])
    except EngineError as e:
        return _error(req, t0, e.code, str(e), engine.model_name or None)

    d = c.data
    addressed = bool(ctx.get("wake_word")) or req.get("source") == "push_to_talk"
    warnings: List[str] = []
    base = dict(normalized_text=norm, atc=None, answer=None, engine=_engine_info(c, t0))
    if debug:
        base["debug"] = {"raw": c.raw, "previous": previous, "prompt_ms": c.extra.get("prompt_ms"),
                         "predicted_ms": c.extra.get("predicted_ms")}

    if d.get("param") in (None, "none"):
        if addressed:
            return _response(req, status="no_match", kind="command", addressee="rb", confidence=0.0,
                             command=None, warnings=warnings, **base)
        return _response(req, status="not_addressed", kind="other", addressee="unknown", confidence=0.8,
                         command=None, warnings=warnings, **base)

    comp, param = d["param"].split(".", 1)
    action = d.get("action", "GET")
    allowed = catalog.allowed_actions(comp)
    if action not in allowed:
        if len(allowed) == 1:
            warnings.append(f"action {action} not valid for {comp}; using {allowed[0]}")
            action = allowed[0]
        else:
            return _response(req, status="no_match", kind="command", addressee="rb", confidence=0.0,
                             command=None, warnings=[f"action {action} not valid for {comp}"], **base)

    confidence = 0.9
    args_in = d.get("args") or {}
    args: Dict[str, Any] = {}
    pool = numbers_in(norm)
    if "frequency" in args_in:
        f = freq3(args_in["frequency"])
        if f and is_com_frequency(f):
            args["frequency_mhz"] = f
            if not _has_number(pool, float(f)) and not _has_number(pool, float(args_in["frequency"])):
                warnings.append(f"frequency {f} not written as digits in the text (model converted words)")
                confidence -= 0.1
        else:
            warnings.append(f"dropped invalid COM frequency {args_in['frequency']!r}")
            confidence -= 0.2
    if args_in.get("station"):
        args["station"] = args_in["station"].lower()
    if args_in.get("airfield"):
        a = args_in["airfield"].strip()
        words = a.lower().replace("-", " ").split()
        spelled = spelled_letters(words)
        if len(words) >= 3 and len(spelled) == len(words) and any(ch.isalpha() for ch in spelled):
            a = spelled  # "lima india romeo zulu" -> "LIRZ" (rbvoice SPELLED_CODE_MIN = 3)
        # Upper-case token shaped like a code (LIRZ, PG02, IT-0003) = spelled code; anything else is a name
        # ("pisa" is a name even though it has 4 letters)
        is_code = a == a.upper() and re.fullmatch(r"[A-Z]{4}|[A-Z]{2}\d{2}|[A-Z]{2}-?\d{4}", a) is not None
        args["airfield"] = {"code": a.replace("-", "")} if is_code else {"name": a.lower()}
    if args_in.get("duration_s"):
        args["duration_s"] = int(args_in["duration_s"])
        if not _has_number(pool, args["duration_s"]) and not _has_number(pool, args["duration_s"] / 60):
            warnings.append("timer duration not found as digits in the text")
            confidence -= 0.1
    if args_in.get("page"):
        args["page"] = args_in["page"].lower()

    from_context = previous is not None and not any(ex in norm for ex in catalog.params.get(d["param"], []))
    if from_context:
        confidence -= 0.05
    confidence = round(max(0.0, min(1.0, confidence)), 2)
    if confidence < NO_MATCH_BELOW:
        return _response(req, status="no_match", kind="command", addressee="rb", confidence=confidence,
                         command=None, warnings=warnings, **base)
    command = {"action": action, "component": comp, "parameter": param, "target": ctx.get("target"),
               "args": args, "from_context": from_context,
               "requires_confirmation": action in ("SET", "DEL") and confidence < SET_CONFIRM_BELOW,
               "evidence": norm}
    return _response(req, status="ok", kind="command", addressee="rb", confidence=confidence,
                     command=command, warnings=warnings, **base)


# =============================================================================================
# ATC
# =============================================================================================

READBACK = {
    "en": {"altitude": "{verb} {alt}", "heading": "{turn}heading {value:03.0f}", "speed": "speed {value:.0f} knots",
           "squawk": "squawk {code}", "qnh": "QNH {value:g}", "frequency_change": "{station}{frequency}",
           "runway": "runway {runway}", "cleared_takeoff": "{rwy}cleared for takeoff",
           "cleared_land": "{rwy}cleared to land", "line_up": "line up{wait}{rwy_after}",
           "hold_short": "holding short {point}", "taxi": "taxi to {to}{via}", "cleared_zone": "cleared {zone}{via}",
           "direct_to": "direct {fix}", "join_circuit": "join {side}{leg}{rwy_after}", "go_around": "going around",
           "hold": "hold {fix}", "_ft": "{v:.0f} feet", "_fl": "flight level {v:.0f}", "_left": "turn left ",
           "_right": "turn right ", "_wait": " and wait", "_runway": "runway {r} ", "_runway_after": " runway {r}",
           "_via": " via {v}", "_climb": "climb", "_descend": "descend", "_maintain": "maintain"},
    "it": {"altitude": "{verb} {alt}", "heading": "{turn}prua {value:03.0f}", "speed": "velocità {value:.0f} nodi",
           "squawk": "squawk {code}", "qnh": "QNH {value:g}", "frequency_change": "{station}{frequency}",
           "runway": "pista {runway}", "cleared_takeoff": "{rwy}autorizzati al decollo",
           "cleared_land": "{rwy}autorizzati all'atterraggio", "line_up": "allineamento{wait}{rwy_after}",
           "hold_short": "attesa corta {point}", "taxi": "rullaggio per {to}{via}", "cleared_zone": "autorizzati {zone}{via}",
           "direct_to": "diretti {fix}", "join_circuit": "{leg} {side}{rwy_after}", "go_around": "riattacchiamo",
           "hold": "attesa {fix}", "_ft": "{v:.0f} piedi", "_fl": "livello di volo {v:.0f}", "_left": "virare a sinistra ",
           "_right": "virare a destra ", "_wait": " e attesa", "_runway": "pista {r} ", "_runway_after": " pista {r}",
           "_via": " via {v}", "_climb": "salire", "_descend": "scendere", "_maintain": "mantenere"},
}
# Circuit terms per language (side / leg values are English in the protocol)
TERMS = {"it": {"left": "sinistro", "right": "destro", "downwind": "sottovento", "base": "base",
                "final": "finale", "crosswind": "traverso", "upwind": "sopravento"}}
NO_READBACK = {"report", "traffic_info", "wind", "standby", "say_again", "unparsed"}
REQUIRED = {"altitude": ["verb", "value", "unit"], "heading": ["value"], "speed": ["value"], "squawk": ["code"],
            "qnh": ["value", "unit"], "runway": ["runway"], "hold_short": ["point"], "taxi": ["to"],
            "cleared_zone": ["zone"], "direct_to": ["fix"], "join_circuit": ["leg"], "report": ["when"],
            "wind": ["direction", "speed_kt"], "unparsed": ["text"]}
ALTITUDE_VERBS = {
    "climb": r"\b(climb\w*|sali\w*|monte[rz]|steigen|suba\w*)\b",
    "descend": r"\b(descen\w*|scend\w*|descende[zr]|sinken|desciend\w*|baje)\b",
    "maintain": r"\b(maintain\w*|manten\w*|maintene[zr]|halten)\b",
}
NUMERIC = ["value", "direction", "speed_kt", "gust_kt", "clock", "distance_nm"]
SHORT = {"altitude": "ALT", "heading": "HDG", "speed": "SPD", "squawk": "SQK", "qnh": "QNH",
         "frequency_change": "FREQ", "runway": "RWY", "cleared_takeoff": "CLR TKOF", "cleared_land": "CLR LAND",
         "line_up": "LINE UP", "hold_short": "HOLD SHORT", "taxi": "TAXI", "cleared_zone": "CLR ZONE",
         "direct_to": "DCT", "join_circuit": "JOIN", "report": "REPORT", "traffic_info": "TFC", "wind": "WIND",
         "go_around": "GO AROUND", "hold": "HOLD", "standby": "STBY", "say_again": "SAY AGAIN", "unparsed": "?"}


def _fix_instruction(ins: Dict[str, Any], norm: str, pool: List[float], warnings: List[str]) -> Optional[Dict[str, Any]]:
    t = ins.get("type")
    out = {k: v for k, v in ins.items() if v is not None}
    if "frequency" in out:
        f = freq3(out.pop("frequency"))
        if f:
            out["frequency_mhz"] = f
    if t == "altitude" and "unit" not in out and "value" in out:
        out["unit"] = "FL" if re.search(r"flight level|livello di volo|\bfl\b", norm) else "ft"
    if t == "altitude":
        # The transmission's own verb wins over the model's: a wrong one ("maintain" for "salite")
        # makes a wrong readback. Never default it: unclear -> drop the instruction.
        verbs = {v for v, rx in ALTITUDE_VERBS.items() if re.search(rx, norm)}
        said = verbs.pop() if len(verbs) == 1 else None
        if said and out.get("verb") not in (None, said):
            warnings.append(f"altitude verb {out['verb']!r} corrected to {said!r} from the transmission")
            out["verb"] = said
        elif "verb" not in out:
            if not said:
                warnings.append("dropped altitude: climb/descend/maintain not clear in the transmission")
                return None
            out["verb"] = said
    if t == "qnh" and "unit" not in out and "value" in out:
        out["unit"] = "hPa" if out["value"] > 100 else "inHg"
    if t == "frequency_change" and "verb" not in out:
        out["verb"] = "contact"
    missing = [k for k in REQUIRED.get(t, []) if k not in out]
    if t == "frequency_change" and "frequency_mhz" not in out and "station" not in out:
        missing.append("frequency_mhz/station")
    if missing:
        warnings.append(f"dropped {t}: missing {', '.join(missing)}")
        return None
    # evidence: every number must be in the transmission
    checks = [(k, float(out[k])) for k in NUMERIC if k in out]
    if "code" in out:
        checks.append(("code", float(out["code"])))
    if "frequency_mhz" in out:
        checks.append(("frequency", float(out["frequency_mhz"])))
    if "runway" in out:
        checks.append(("runway", float(re.sub(r"\D", "", out["runway"]) or 0)))
    for k, v in checks:
        if not _has_number(pool, v):
            warnings.append(f"dropped {t}: {k} {v:g} is not in the transmission")
            return None
    out["readback_required"] = t not in NO_READBACK
    return out


def build_readback(instructions: List[Dict[str, Any]], callsign_letters: str, lang: str) -> Optional[str]:
    tpl = READBACK.get(lang, READBACK["en"])
    parts: List[str] = []
    for ins in instructions:
        if not ins.get("readback_required"):
            continue
        t = ins["type"]
        if t not in tpl:
            continue
        rwy = ins.get("runway")
        f = dict(ins)
        f["rwy"] = tpl["_runway"].format(r=rwy) if rwy else ""
        f["rwy_after"] = tpl["_runway_after"].format(r=rwy) if rwy else ""
        f["turn"] = tpl.get("_" + ins.get("turn", ""), "") if ins.get("turn") else ""
        f["wait"] = tpl["_wait"] if ins.get("wait") else ""
        f["via"] = tpl["_via"].format(v=" ".join(ins.get("via") or [])) if ins.get("via") else ""
        terms = TERMS.get(lang, {})
        f["side"] = (terms.get(ins["side"], ins["side"]) + " ") if ins.get("side") else ""
        if ins.get("leg"):
            f["leg"] = terms.get(ins["leg"], ins["leg"])
        if lang == "it" and t == "join_circuit":
            f["side"] = f["side"].strip()
        f["station"] = (ins["station"].title() + " ") if ins.get("station") else ""
        f["frequency"] = ins.get("frequency_mhz", "").rstrip("0").rstrip(".") if ins.get("frequency_mhz") else ""
        if t == "altitude":
            f["verb"] = tpl["_" + ins["verb"]] if "_" + ins["verb"] in tpl else ins["verb"]
            f["alt"] = (tpl["_fl"] if ins["unit"] == "FL" else tpl["_ft"]).format(v=ins["value"])
        f.setdefault("value", 0)
        for k in ("point", "to", "zone", "fix", "leg"):
            f.setdefault(k, "")
        if t == "join_circuit" and lang != "it":
            f["leg"] = ins["leg"]
        try:
            parts.append(tpl[t].format(**f).strip())
        except (KeyError, ValueError):
            continue
    # "runway 01" is redundant when a clearance already names runway 01
    parts = [p for i, p in enumerate(parts)
             if not (p.startswith(tpl["_runway"].split("{")[0]) and any(p in q and q != p for q in parts))]
    if not parts:
        return None
    text = ", ".join(parts)
    text = text[0].upper() + text[1:]
    return f"{text}, {spoken_letters(callsign_letters)}" if callsign_letters else text


def display_line(instructions: List[Dict[str, Any]]) -> str:
    items = []
    for i in instructions:
        s = SHORT.get(i["type"], i["type"].upper())
        if i["type"] == "altitude":
            verb = {"climb": "CLB", "descend": "DES", "maintain": "MNT"}.get(i["verb"], i["verb"].upper())
            s += f" {verb} {'FL' + format(i['value'], '.0f') if i['unit'] == 'FL' else format(i['value'], '.0f') + ' FT'}"
        elif i["type"] in ("heading", "speed", "qnh"):
            s += f" {i['value']:g}"
        elif i["type"] == "join_circuit":
            s += f" {(i.get('side') or '')[:1].upper()} {i['leg'].upper()}".replace("  ", " ") + \
                (f" {i['runway']}" if i.get("runway") else "")
        elif i["type"] == "wind":
            s += f" {i['direction']:03d}/{i['speed_kt']}" + (f"G{i['gust_kt']}" if i.get("gust_kt") else "")
        elif i["type"] == "squawk":
            s += f" {i['code']}"
        elif i["type"] == "frequency_change":
            s = f"{(i.get('station') or 'FREQ').upper()} {i.get('frequency_mhz', '')}".strip()
        elif i.get("runway"):
            s += f" {i['runway']}"
        elif i.get("when"):
            s += f" {i['when'].upper()}"
        items.append(s)
    return " · ".join(items)


def _words_in(phrase: str, text: str) -> bool:
    """True when every word of `phrase` appears in `text` (order not required)."""
    words = set(text.lower().split())
    return all(w in words for w in phrase.lower().replace("-", " ").split())


def run_atc(req: Dict[str, Any], engine: LlamaClient) -> Dict[str, Any]:
    t0 = time.monotonic()
    ctx = req.get("context") or {}
    own = ctx.get("own_callsign")
    lang = req.get("lang", "en")
    debug = bool((req.get("options") or {}).get("debug"))
    norm = normalize(req["text"], lang)
    own_txt = None
    if own and own.get("registration"):
        own_txt = own["registration"] + (f" ({' / '.join(own.get('spoken') or [])})" if own.get("spoken") else "")
    try:
        c = engine.complete_json(prompts.atc_messages(norm, own_txt), prompts.atc_schema(),
                                 MAX_TOKENS["atc"], _deadline(req, "atc"), SLOT["atc"])
    except EngineError as e:
        return _error(req, t0, e.code, str(e), engine.model_name or None)

    d = c.data
    warnings: List[str] = []
    pool = numbers_in(norm)
    instructions = [x for x in (_fix_instruction(i, norm, pool, warnings) for i in d.get("instructions") or []) if x]
    # station / callsign must be words of the transmission (small models copy them from the examples)
    station = d.get("station")
    if station and not _words_in(station, norm):
        warnings.append(f"dropped station {station!r}: not in the transmission")
        station = None
    heard = d.get("callsign")
    if heard and not _words_in(heard, norm):
        warnings.append(f"dropped callsign {heard!r}: not in the transmission")
        heard = None
    if not heard:
        heard = find_own_callsign(norm, own)  # the model may omit it; our own callsign is easy to spot
    cs = match_callsign(heard, own)
    kind = d.get("kind", "other")
    # A transmission that ends with the callsign and does not start with it is a pilot readback
    if kind == "atc" and heard and norm.endswith(heard.lower()) and not norm.startswith(heard.lower()):
        kind = "readback"
    base = dict(normalized_text=norm, command=None, engine=_engine_info(c, t0))
    if debug:
        base["debug"] = {"raw": c.raw, "prompt_ms": c.extra.get("prompt_ms"), "predicted_ms": c.extra.get("predicted_ms")}
    if kind == "other" or (not instructions and not heard):
        return _response(req, status="not_addressed", kind="other", addressee="unknown", confidence=0.8,
                         atc=None, answer=None, warnings=warnings, **base)

    confidence = round(max(0.0, 0.9 - 0.15 * len(warnings)), 2)
    if kind == "readback":
        addressee = "atc"
    else:
        addressee = {"own": "own_aircraft", "other": "other_aircraft"}.get(cs["match"], "unknown")
    rb_lang = (req.get("options") or {}).get("readback_lang") or ("it" if lang == "it" and re.search(
        r"\b(pista|sali\w*|scend\w*|manten\w*|autorizzat\w*|riport\w*|contatt\w*|vento|piedi|prua|virate)\b",
        norm) else "en")
    readback = None
    suggested: List[Dict[str, Any]] = []
    if kind == "atc" and addressee == "own_aircraft":
        _, abbr, _ = own_forms(own or {})
        needs = any(i.get("readback_required") for i in instructions)
        readback = {"required": needs, "text": build_readback(instructions, abbr, rb_lang) if needs else None,
                    "lang": rb_lang}
        for i in instructions:
            if i["type"] == "frequency_change" and i.get("frequency_mhz") and is_com_frequency(i["frequency_mhz"]):
                args: Dict[str, Any] = {"frequency_mhz": i["frequency_mhz"]}
                if i.get("station"):
                    args["station"] = i["station"]
                suggested.append({"action": "SET", "component": "radio", "parameter": "radio", "args": args,
                                  "requires_confirmation": True})
    atc = {"callsign": cs, "station": station, "instructions": instructions,
           "readback": readback, "suggested_actions": suggested}
    status = "ok" if addressee in ("own_aircraft", "atc", "unknown") else "not_addressed"
    return _response(req, status=status, kind=kind, addressee=addressee, confidence=confidence, atc=atc,
                     answer={"speak": None, "display": display_line(instructions), "lang": rb_lang} if instructions else None,
                     warnings=warnings, **base)


# =============================================================================================
# AUTO
# =============================================================================================

def classify(req: Dict[str, Any], catalog: Optional[Catalog]) -> str:
    """'command' or 'atc' (cheap rules; the command LLM can still answer kind=none)."""
    ctx = req.get("context") or {}
    if ctx.get("wake_word") or req.get("source") == "push_to_talk":
        return "command"
    norm = normalize(req["text"], req.get("lang", "en"))
    if catalog:
        first = norm.split()[:3]
        if any(w in " ".join(first) for w in catalog.wake_words if w):
            return "command"
    words = norm.split()
    letters = spelled_letters(words) or (spelled_letters(words[1:]) if words and words[0] in TYPE_PREFIXES else "")
    if (len(letters) >= 2 or ATC_MARKERS.search(norm)) and re.search(r"\d", norm):
        return "atc"
    if len(letters) >= 3:
        return "atc"
    return "command"


def dumps(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")
