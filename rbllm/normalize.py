"""Deterministic text normalization before the LLM sees an utterance.

Spoken numbers become digits ("one one eight decimal five" -> "118.5", "five thousand" -> "5000"),
so the model copies numbers instead of doing arithmetic on words. ICAO spelling-alphabet words are
left as words (the callsign matcher reads them), but `spelled_letters()` turns a run of them into
letters.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Dict, Iterable, List, Optional

# Indefinite articles that double as "one" (una, un, une, ein) are left out on purpose:
# "imposta una frequenza" must not become "imposta 1 frequenza".
DIGITS: Dict[str, Dict[str, str]] = {
    "en": {"zero": "0", "one": "1", "two": "2", "three": "3", "tree": "3", "four": "4", "fower": "4",
           "five": "5", "fife": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9", "niner": "9"},
    "it": {"zero": "0", "uno": "1", "due": "2", "tre": "3", "quattro": "4", "cinque": "5",
           "sei": "6", "sette": "7", "otto": "8", "nove": "9"},
    "fr": {"zéro": "0", "zero": "0", "deux": "2", "trois": "3", "quatre": "4",
           "cinq": "5", "six": "6", "sept": "7", "huit": "8", "neuf": "9"},
    "es": {"cero": "0", "uno": "1", "dos": "2", "tres": "3", "cuatro": "4", "cinco": "5",
           "seis": "6", "siete": "7", "ocho": "8", "nueve": "9"},
    "de": {"null": "0", "eins": "1", "zwei": "2", "zwo": "2", "drei": "3", "vier": "4",
           "fünf": "5", "fuenf": "5", "sechs": "6", "sieben": "7", "acht": "8", "neun": "9"},
}
THOUSAND = {"thousand", "mila", "mille", "mil", "tausend"}
HUNDRED = {"hundred", "cento", "cent", "cien", "ciento", "hundert"}
DECIMAL = {"decimal", "point", "punto", "virgola", "decimale", "virgule", "coma", "komma", "dezimal"}

# ICAO spelling alphabet (+ common STT spellings)
ICAO_LETTERS: Dict[str, str] = {
    "alfa": "A", "alpha": "A", "bravo": "B", "charlie": "C", "delta": "D", "echo": "E", "eco": "E",
    "foxtrot": "F", "golf": "G", "hotel": "H", "india": "I", "juliett": "J", "juliet": "J",
    "kilo": "K", "lima": "L", "mike": "M", "november": "N", "oscar": "O", "papa": "P",
    "quebec": "Q", "romeo": "R", "sierra": "S", "tango": "T", "uniform": "U", "victor": "V",
    "whiskey": "W", "whisky": "W", "x-ray": "X", "xray": "X", "yankee": "Y", "zulu": "Z",
}

_PUNCT = re.compile(r"[^\w\s.'\-]", re.UNICODE)


def _digit_map(lang: str) -> Dict[str, str]:
    m = dict(DIGITS["en"])  # ATC is often English inside other languages
    m.update(DIGITS.get(lang, {}))
    return m


def _is_num_token(tok: str, digits: Dict[str, str]) -> bool:
    return tok in digits or tok.isdigit() or tok in THOUSAND or tok in HUNDRED


def _collapse_run(run: List[str], digits: Dict[str, str]) -> str:
    """Turn one run of number tokens (no decimal word inside) into a digit string."""
    if not any(t in THOUSAND or t in HUNDRED for t in run):
        return "".join(digits.get(t, t) for t in run)
    total, current = 0, ""
    for t in run:
        if t in THOUSAND:
            total += int(current or "1") * 1000
            current = ""
        elif t in HUNDRED:
            total += int(current or "1") * 100
            current = ""
        else:
            current += digits.get(t, t)
    return str(total + int(current or "0"))


def normalize_numbers(text: str, lang: str = "en") -> str:
    digits = _digit_map(lang)
    toks = text.split()
    out: List[str] = []
    i = 0
    while i < len(toks):
        if not _is_num_token(toks[i], digits):
            out.append(toks[i])
            i += 1
            continue
        j = i
        while j < len(toks) and _is_num_token(toks[j], digits):
            j += 1
        number = _collapse_run(toks[i:j], digits)
        # "118 decimal 5" -> "118.5"
        if j + 1 < len(toks) and toks[j] in DECIMAL and _is_num_token(toks[j + 1], digits):
            k = j + 1
            while k < len(toks) and (toks[k] in digits or toks[k].isdigit()):
                k += 1
            frac = "".join(digits.get(t, t) for t in toks[j + 1:k])
            if frac:
                number = f"{number}.{frac}"
                j = k
        out.append(number)
        i = j
    return " ".join(out)


def basic_clean(text: str) -> str:
    text = unicodedata.normalize("NFC", text).lower()
    text = _PUNCT.sub(" ", text)
    text = text.replace(" - ", " ")
    return re.sub(r"\s+", " ", text).strip()


def apply_aliases(text: str, aliases: Dict[str, str]) -> str:
    """Whole-word alias rewrite, longest alias first (same rule as rbvoice)."""
    for key in sorted(aliases, key=len, reverse=True):
        text = re.sub(rf"(?<![\w']){re.escape(key)}(?!\w)", aliases[key], text)
    return text


def normalize(text: str, lang: str = "en", aliases: Optional[Dict[str, str]] = None) -> str:
    text = basic_clean(text)
    if aliases:
        text = apply_aliases(text, aliases)
    return normalize_numbers(text, lang)


def spelled_letters(words: Iterable[str]) -> str:
    """'india charlie delta' -> 'ICD'. Digits pass through. Stops at the first other word."""
    out = []
    for w in words:
        if w in ICAO_LETTERS:
            out.append(ICAO_LETTERS[w])
        elif w.isdigit():
            out.append(w)
        else:
            break
    return "".join(out)


def numbers_in(text: str) -> List[float]:
    """All numbers in a text, as floats (for evidence checks)."""
    vals = []
    for m in re.finditer(r"(?<![\w.])-?\d+(?:[.,]\d+)?", text):
        try:
            vals.append(float(m.group(0).replace(",", ".")))
        except ValueError:
            pass
    return vals
