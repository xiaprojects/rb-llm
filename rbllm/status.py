"""Aircraft status: from an RB-01 snapshot to a list of plain-text *facts* for the model.

Why this layer exists:
- Small models are bad at arithmetic (haversine, clock positions, unit conversion) and some
  refuse to "use GPS coordinates". So everything numeric is computed here, in code, and given to
  the model as short labelled sentences ("nearest airfield: Perugia (LIRZ), 6.2 NM, bearing 095").
- Stratux marks invalid values with sentinels (3276.7, 99999). They are dropped here, so the model
  never reads a sentinel as a real heading.
- Every fact keeps its numbers, so the answer can be checked against them (evidence check).

A snapshot is a dict keyed by RB-01 endpoint, like rbvoice's tests/fixtures/rb01.json:
  {"/getSituation": {...}, "/getEMS": {...}, "/radio": [...], "/getTraffic": [...],
   "/settings/db.airfields.json": [...]}
"""
from __future__ import annotations

import json
import math
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

ENDPOINTS = ["/getSituation", "/getEMS", "/radio", "/getTraffic"]
AIRFIELDS_ENDPOINT = "/settings/db.airfields.json"
SENTINELS = (3276.7, 32767.0, 99999.0, 999999.0)  # Stratux/RB "no data" markers (either sign)
M_TO_FT = 3.28084
NM_M = 1852.0

# EMS fields: (label, unit) — units as rbvoice speaks them
EMS_FIELDS = {
    "enginerpm": ("engine rpm", "rpm"),
    "oiltemperature": ("oil temperature", "°C"),
    "oilpressure": ("oil pressure", "PSI"),
    "cht1": ("cylinder 1 head temperature", "°C"),
    "cht2": ("cylinder 2 head temperature", "°C"),
    "cht3": ("cylinder 3 head temperature", "°C"),
    "cht4": ("cylinder 4 head temperature", "°C"),
    "egt1": ("exhaust 1 gas temperature", "°C"),
    "egt2": ("exhaust 2 gas temperature", "°C"),
    "egt3": ("exhaust 3 gas temperature", "°C"),
    "egt4": ("exhaust 4 gas temperature", "°C"),
    "fuelremaining": ("fuel remaining", "liters"),
    "fuel": ("fuel", "liters"),
    "fuel1": ("fuel tank 1", "liters"),
    "fuel2": ("fuel tank 2", "liters"),
    "fuelpressure": ("fuel pressure", "PSI"),
    "manifoldpressure": ("manifold pressure", "PSI"),
    "batteryvoltage": ("battery voltage", "V"),
    "alternatorout": ("alternator output", "A"),
    "amps": ("current", "A"),
    "coolanttemperature": ("coolant temperature", "°C"),
    "outsidetemperature": ("outside air temperature", "°C"),
}


@dataclass
class Fact:
    key: str
    text: str
    values: List[float] = field(default_factory=list)


@dataclass
class AircraftStatus:
    facts: List[Fact]
    source: str
    age_s: float = 0.0

    def keys(self) -> List[str]:
        return [f.key for f in self.facts]

    def prompt_block(self) -> str:
        if not self.facts:
            return "(no aircraft data available)"
        return "\n".join(f"{f.key}: {f.text}" for f in self.facts)

    def numbers(self) -> List[float]:
        return [v for f in self.facts for v in f.values]

    def to_json(self) -> Dict[str, Any]:
        return {"source": self.source, "age_s": round(self.age_s, 1),
                "facts": [{"key": f.key, "text": f.text} for f in self.facts]}


# --- geometry --------------------------------------------------------------------------------

def distance_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a)) / NM_M


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    b = round(math.degrees(math.atan2(y, x))) % 360
    return 360 if b == 0 else b


def clock_position(bearing: float, own_heading: float) -> int:
    rel = (bearing - own_heading) % 360
    c = round(rel / 30) % 12
    return 12 if c == 0 else c


def dm(value: float, pos: str, neg: str, width: int) -> str:
    hemi = pos if value >= 0 else neg
    v = abs(value)
    deg = int(v)
    minutes = (v - deg) * 60
    return f"{deg:0{width}d}°{minutes:05.2f}'{hemi}"


def _valid(v: Any) -> bool:
    if not isinstance(v, (int, float)) or isinstance(v, bool) or math.isnan(v) or math.isinf(v):
        return False
    return not any(abs(abs(v) - s) < 0.05 for s in SENTINELS)


def _hdg(v: float) -> int:
    h = round(v) % 360
    return 360 if h == 0 else h


# --- facts -----------------------------------------------------------------------------------

def build_status(snapshot: Dict[str, Any], source: str = "request", age_s: float = 0.0,
                 max_traffic: int = 5, max_airfields: int = 3) -> AircraftStatus:
    facts: List[Fact] = []
    if not any(snapshot.get(ep) for ep in ENDPOINTS):
        return AircraftStatus(facts=[], source=source, age_s=age_s)  # no RB-01 data at all
    add = lambda key, text, *vals: facts.append(Fact(key, text, [float(v) for v in vals]))  # noqa: E731
    sit = snapshot.get("/getSituation") or {}

    # GPS
    lat, lon = sit.get("GPSLatitude"), sit.get("GPSLongitude")
    fixq = sit.get("GPSFixQuality", 0)
    has_fix = bool(fixq) and _valid(lat) and _valid(lon) and not (lat == 0 and lon == 0)
    sats = sit.get("GPSSatellites")
    if has_fix:
        kind = {1: "3D GPS", 2: "3D GPS with SBAS/DGPS"}.get(fixq, "GPS")
        acc = sit.get("GPSHorizontalAccuracy")
        acc_txt = f", horizontal accuracy {acc:.0f} m" if _valid(acc) else ""
        add("gps.fix", f"{kind} fix, {sats} satellites in use{acc_txt}", sats or 0)
        add("gps.position",
            f"latitude {lat:.5f} {'N' if lat >= 0 else 'S'}, longitude {lon:.5f} {'E' if lon >= 0 else 'W'} "
            f"({dm(lat, 'N', 'S', 2)} {dm(lon, 'E', 'W', 3)})",
            round(lat, 5), round(lon, 5))
    else:
        add("gps.fix", "NO GPS FIX: position, ground speed and track are not available")

    gs = sit.get("GPSGroundSpeed")
    if has_fix and _valid(sit.get("GPSAltitudeMSL")):
        alt = sit["GPSAltitudeMSL"]
        add("gps.altitude", f"{alt:.0f} ft MSL (GPS)", round(alt))
    if has_fix and _valid(gs):
        add("gps.ground_speed", f"{gs:.0f} kt", round(gs))
        if gs >= 5 and _valid(sit.get("GPSTrueCourse")):
            add("gps.track", f"{_hdg(sit['GPSTrueCourse']):03d}° true", _hdg(sit["GPSTrueCourse"]))
        else:
            add("gps.track", "not available (ground speed below 5 kt)")
    if has_fix and _valid(sit.get("GPSVerticalSpeed")):
        add("gps.vertical_speed", f"{sit['GPSVerticalSpeed']:.0f} ft/min", round(sit["GPSVerticalSpeed"]))
    t = sit.get("GPSTime") or ""
    if t and not t.startswith("0001"):
        add("gps.time", f"{t[11:19]} UTC on {t[:10]}")

    # AHRS
    if _valid(sit.get("AHRSMagHeading")):
        add("ahrs.heading", f"{_hdg(sit['AHRSMagHeading']):03d}° magnetic", _hdg(sit["AHRSMagHeading"]))
    if _valid(sit.get("AHRSPitch")) and _valid(sit.get("AHRSRoll")):
        p, r = sit["AHRSPitch"], sit["AHRSRoll"]
        add("ahrs.attitude",
            f"pitch {abs(p):.0f}° {'up' if p >= 0 else 'down'}, bank {abs(r):.0f}° {'right' if r >= 0 else 'left'}",
            round(abs(p)), round(abs(r)))
    if _valid(sit.get("AHRSSlipSkid")):
        add("ahrs.slip_skid", f"{sit['AHRSSlipSkid']:.1f}°", round(sit["AHRSSlipSkid"], 1))
    if _valid(sit.get("AHRSTurnRate")):
        add("ahrs.turn_rate", f"{sit['AHRSTurnRate']:.1f}°/s", round(sit["AHRSTurnRate"], 1))
    if _valid(sit.get("AHRSGLoad")):
        g, gmin, gmax = sit["AHRSGLoad"], sit.get("AHRSGLoadMin", 0), sit.get("AHRSGLoadMax", 0)
        add("ahrs.g_load", f"{g:.1f} g (session min {gmin:.1f} g, max {gmax:.1f} g)",
            round(g, 1), round(gmin, 1), round(gmax, 1))

    # Baro
    if _valid(sit.get("BaroPressureAltitude")) and sit.get("BaroSourceType", 1):
        add("baro.pressure_altitude", f"{sit['BaroPressureAltitude']:.0f} ft (1013.25 hPa)",
            round(sit["BaroPressureAltitude"]), 1013.25)
    if _valid(sit.get("BaroVerticalSpeed")) and sit.get("BaroSourceType", 1):
        add("baro.vertical_speed", f"{sit['BaroVerticalSpeed']:.0f} ft/min", round(sit["BaroVerticalSpeed"]))
    if _valid(sit.get("BaroTemperature")) and sit.get("BaroTemperature"):
        add("baro.sensor_temperature", f"{sit['BaroTemperature']:.0f} °C (instrument, not outside air)",
            round(sit["BaroTemperature"]))

    # EMS: zero means "sensor not connected" on RB-01, so zeros are listed as unavailable
    ems = snapshot.get("/getEMS") or {}
    missing = []
    for name, (label, unit) in EMS_FIELDS.items():
        v = ems.get(name)
        if not _valid(v):
            continue
        if v == 0:
            missing.append(label)
            continue
        digits = 1 if unit == "V" else 0
        add(f"ems.{name}", f"{label} {v:.{digits}f} {unit}", round(v, digits))
    if missing and ems:
        add("ems.not_connected", "no reading for: " + ", ".join(missing))

    # Radio
    radios = snapshot.get("/radio") or []
    if radios:
        r0 = radios[0]
        act, stb = r0.get("FrequencyActive"), r0.get("FrequencyStandby")
        if act:
            add("radio.active", f"{act} MHz ({r0.get('LabelActive') or 'no label'})", float(act))
        if stb:
            add("radio.standby", f"{stb} MHz ({r0.get('LabelStandby') or 'no label'})", float(stb))

    own_hdg = None
    if has_fix and _valid(gs) and gs >= 5 and _valid(sit.get("GPSTrueCourse")):
        own_hdg = sit["GPSTrueCourse"]
    elif _valid(sit.get("AHRSMagHeading")):
        own_hdg = sit["AHRSMagHeading"]

    # Traffic: nearest first
    if has_fix:
        tfc = []
        for t in snapshot.get("/getTraffic") or []:
            if not t.get("Position_valid", True) or not _valid(t.get("Lat")) or not _valid(t.get("Lng")):
                continue
            d = distance_nm(lat, lon, t["Lat"], t["Lng"])
            tfc.append((d, t))
        tfc.sort(key=lambda x: x[0])
        if not tfc:
            add("traffic.count", "no traffic received", 0)
        else:
            add("traffic.count", f"{len(tfc)} aircraft received", len(tfc))
        for i, (d, t) in enumerate(tfc[:max_traffic], 1):
            b = bearing_deg(lat, lon, t["Lat"], t["Lng"])
            parts = [t.get("Tail") or f"ICAO {t.get('Icao_addr', '?')}"]
            vals: List[float] = [round(d, 1), b]
            if _valid(t.get("Alt")):
                parts.append(f"{t['Alt']:.0f} ft")
                vals.append(t["Alt"])
            pos = f"{d:.1f} NM, bearing {b:03d}°"
            if own_hdg is not None:
                c = clock_position(b, own_hdg)
                pos += f", {c} o'clock"
                vals.append(c)
            parts.append(pos)
            if t.get("OnGround"):
                parts.append("on ground")
            elif t.get("Speed"):
                parts.append(f"track {t.get('Track', 0):03.0f}°, {t['Speed']} kt")
                vals += [t.get("Track", 0), t["Speed"]]
            facts.append(Fact(f"traffic.{i}", ", ".join(parts), [float(v) for v in vals]))

    # Nearest airfields (db elevations are meters)
    if has_fix:
        afs = []
        for a in snapshot.get(AIRFIELDS_ENDPOINT) or []:
            if _valid(a.get("Lat")) and _valid(a.get("Lon")):
                afs.append((distance_nm(lat, lon, a["Lat"], a["Lon"]), a))
        afs.sort(key=lambda x: x[0])
        for i, (d, a) in enumerate(afs[:max_airfields], 1):
            b = bearing_deg(lat, lon, a["Lat"], a["Lon"])
            code = a.get("icao_code") or a.get("gps_code") or a.get("local_code") or a.get("ident") or ""
            txt = f"{a.get('name', '?')} ({code}), {d:.1f} NM, bearing {b:03d}° true"
            vals = [round(d, 1), b]
            if _valid(a.get("Ele")):
                ele_ft = round(a["Ele"] * M_TO_FT)
                txt += f", elevation {ele_ft} ft"
                vals.append(ele_ft)
            if a.get("freq"):
                txt += f", frequency {a['freq']}"
                vals.append(float(a["freq"]))
            facts.append(Fact(f"airfield.nearest_{i}", txt, [float(v) for v in vals]))

    return AircraftStatus(facts=facts, source=source, age_s=age_s)


# --- sources ---------------------------------------------------------------------------------

class StatusProvider:
    """Gets a snapshot from the request, from a live RB-01, or from the demo file."""

    def __init__(self, rb_url: str = "", demo_path: str = "", timeout_s: float = 1.5,
                 airfields_path: str = "") -> None:
        self.rb_url = rb_url.rstrip("/")
        self.demo_path = demo_path
        self.timeout_s = timeout_s
        self.airfields_path = airfields_path
        self._airfields: Optional[List[Any]] = None
        self._cache: Optional[Dict[str, Any]] = None
        self._cache_t = 0.0

    def _get(self, path: str) -> Any:
        with urllib.request.urlopen(self.rb_url + path, timeout=self.timeout_s) as r:
            return json.load(r)

    def _airfield_db(self) -> List[Any]:
        if self._airfields is None:
            try:
                if self.airfields_path:
                    with open(self.airfields_path, encoding="utf-8") as f:
                        self._airfields = json.load(f)
                elif self.rb_url:
                    self._airfields = self._get(AIRFIELDS_ENDPOINT)
            except Exception:  # noqa: BLE001 - airfields are optional
                self._airfields = None
                return []
        return self._airfields or []

    def snapshot(self, source: str = "auto") -> tuple:
        """Returns (snapshot, source_name). source: auto | rb | demo."""
        if source in ("auto", "rb") and self.rb_url:
            now = time.monotonic()
            if self._cache is None or now - self._cache_t > 1.0:
                snap: Dict[str, Any] = {}
                for ep in ENDPOINTS:
                    try:
                        snap[ep] = self._get(ep)
                    except Exception:  # noqa: BLE001 - a missing endpoint just drops its facts
                        pass
                if not snap and source == "rb":
                    raise ConnectionError(f"RB-01 not reachable at {self.rb_url}")
                if snap:
                    snap[AIRFIELDS_ENDPOINT] = self._airfield_db()
                    self._cache, self._cache_t = snap, now
            if self._cache:
                return self._cache, "rb"
        if source in ("auto", "demo") and self.demo_path:
            with open(self.demo_path, encoding="utf-8") as f:
                return json.load(f), "demo"
        return {}, "none"


# --- relevance filter ------------------------------------------------------------------------
# The status block is re-processed by the model on every request (live values change), so its
# size is latency. Only the fact groups the question is about are sent; unknown topic -> all.
TOPICS = [
    (r"where|position|location|coordinat|latitud|longitud|lat\b|lon\b|dove|posizion|coordinat|siamo|"
     r"où|position|coordonn|dónde|posici|coordenad|wo\b|position|koordinat|standort",
     ["gps.fix", "gps.position", "gps.altitude", "airfield."]),
    (r"airfield|airport|airstrip|runway|land|campo|aeroport|aviosuperfic|pista|atterr|aérodrome|aéroport|"
     r"aeródromo|aeropuerto|flugplatz|flughafen|landen", ["gps.fix", "airfield."]),
    (r"alti|height|high|level|quota|alto|altezza|altitud|hauteur|höhe|hoch|climb|descen|vario|sali|scend",
     ["gps.altitude", "baro.", "gps.vertical_speed"]),
    (r"speed|fast|heading|track|course|direction|velocit|prua|rotta|direzion|vitesse|cap\b|velocidad|rumbo|"
     r"geschwindigkeit|kurs|steuerkurs", ["gps.ground_speed", "gps.track", "ahrs.heading"]),
    (r"attitude|pitch|bank|roll|slip|skid|g load|\bg\b|assetto|beccheggio|rollio|sbandament|inclin|"
     r"assiette|inclinaison|actitud|alabeo|lage|querlage", ["ahrs."]),
    (r"engine|motor|rpm|oil|cht|egt|cylind|exhaust|temperatur|olio|cilindr|scaric|giri|moteur|huile|"
     r"aceite|cilindro|escape|öl|zylinder|abgas|battery|volt|alternat|batteri|amp", ["ems."]),
    (r"fuel|tank|carburant|serbatoio|benzina|combustible|depósito|kraftstoff|tank|sprit",
     ["ems.fuel", "ems.not_connected"]),
    (r"traffic|aircraft|plane|traffic|traffico|traffici|aerei|trafic|avion|tráfico|avión|verkehr|flugzeug",
     ["traffic.", "gps.fix"]),
    (r"radio|frequenc|freq|com\b|fréquence|frecuencia|frequenz|funk", ["radio."]),
    (r"time|clock|utc|ora\b|orario|che ore|heure|hora|uhr|zeit", ["gps.time"]),
    (r"outside|oat|esterna|extérieur|exterior|außen", ["ems.outsidetemperature"]),
    (r"gps|satellit|fix\b|accura|precision|precisi|genauigkeit", ["gps.fix"]),
]


def select_facts(status: "AircraftStatus", question: str) -> "AircraftStatus":
    import re as _re
    q = question.lower()
    prefixes: List[str] = []
    for pattern, keys in TOPICS:
        if _re.search(pattern, q):
            prefixes += keys
    if not prefixes:
        return status
    prefixes.append("gps.fix")  # always say whether there is a GPS fix
    keep = [f for f in status.facts if any(f.key == p or (p.endswith(".") and f.key.startswith(p))
                                            or (not p.endswith(".") and f.key.startswith(p)) for p in prefixes)]
    return AircraftStatus(facts=keep or status.facts, source=status.source, age_s=status.age_s)
