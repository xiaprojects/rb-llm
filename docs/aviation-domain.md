# The "shrunk" aviation domain

rb-llm does not need general aviation knowledge. It needs a closed world: the commands an RB unit can execute, and the subset of ATC phraseology that a VFR/IFR general-aviation pilot hears and has to read back. Everything outside this world is `no_match` / `other`.

## 1. Voice commands

### Source of truth

The command catalog is rbvoice's `rbvoice-cases.<lang>.json`. Its components and parameter IDs (the keyword **values** and endpoint keys) are the identifiers used by the protocol. As of rbvoice 2.4.x they are:

| Component | Parameters (IDs) | Actions | Args |
|---|---|---|---|
| `ems` | `fullengine`, `egt`, `cht`, `egt1..4`, `cht1..4`, `batteryvoltage`, `alternatorout`, `amps`, `enginerpm`, `fuel`, `fuel1`, `fuel2`, `fuelremaining`, `fuelpressure`, `oilpressure`, `oiltemperature`, `manifoldpressure`, `outsidetemperature` | GET | — |
| `situation` | `attitude`, `AHRSMagHeading`, `AHRSGyroHeading`, `AHRSPitch`, `AHRSRoll`, `AHRSSlipSkid`, `AHRSTurnRate`, `AHRSGLoad`, `AHRSGLoadMin`, `AHRSGLoadMax`, `GPSAltitudeMSL`, `GPSSatellites`, `GPSLatitude`, `GPSLongitude`, `GPSFixQuality`, `GPSVerticalSpeed`, `GPSTrueCourse`, `GPSTurnRate`, `GPSGroundSpeed`, `GPSLastGroundTrackTime`, `GPSTime`, `BaroTemperature`, `BaroPressureAltitude`, `BaroHumidity`, `BaroPpm`, `BaroGasResistance`, `BaroVerticalSpeed` | GET | — |
| `radio` | `radio` | GET, SET | `frequency_mhz` or `station` |
| `autopilot` | `destination` | GET, SET, DEL | `airfield` (optional on SET: re-engage) |
| `airfields` | `find` | GET | `airfield` or "nearest" |
| `timers` | `timer` | GET, SET, DEL | `duration_s` (optional) |
| `traffic` | `traffic` | GET | — |
| `display` | `display` | SET | `page` |

rb-llm must not hard-code this table. It loads it from the catalog. The table is here for orientation only.

### Session follow-ups

As in rbvoice's `SessionContext`, an utterance with no recognizable parameter ("and the second one?", "e il secondo?") reuses `context.session.last_component` / `last_parameter`. The LLM receives those in the dynamic tail and may return them. The response marks this with `command.from_context: true`.

### Display targets

Wake words map to RB-01 display names (`primario`, `secondario`, `display`; see `display_targets` in the case files). rb-llm passes `target` through when RB-08 supplies it. It never invents one.

### Command safety

| Rule | Origin |
|---|---|
| Autopilot direct-to only through the `autopilot` component, never through `airfields` | rbvoice "explicit intent" rule |
| An airfield must be a clear match (score margin over the runner-up) | rbvoice `AIRFIELD_MIN_SCORE` / `AIRFIELD_MIN_MARGIN` |
| A radio SET writes **standby** only, and only a valid VHF COM frequency, 118.000–136.990 on the 8.33/25 kHz channel grid | rbvoice `is_com_frequency` |
| A SET with confidence below the threshold → `requires_confirmation: true` | rb-llm |

rb-llm only *returns* the intent. RB-08 still enforces these rules before it writes anything.

## 2. ATC phraseology

### Scope

Phase 1 covers what a GA pilot hears most often. The reference documents are **ICAO Doc 9432** (Manual of Radiotelephony) and **ICAO Doc 4444** (PANS-ATM), with **EASA SERA Part C** phraseology for Europe and ENAV AIP Italia GEN 3.4 for Italian practice.

Phase 1 languages: **English**, then **Italian** (Italian is used on VFR frequencies in Italy).

### Message structure

A typical ATC transmission is:

```
<addressee callsign> , [<station>] , <instruction> [, <instruction>...] [, <reason/info>]
```

For example: "India Charlie Delta, Perugia Tower, runway 01, cleared to land, wind 360 degrees 8 knots."

A pilot readback reverses the order: `<instructions> , <own callsign>`.

### Callsigns

| Form | Example |
|---|---|
| Full registration | I-ABCD → "India Alfa Bravo Charlie Delta" |
| Abbreviated (after ATC uses it first) | "India Charlie Delta" (first character + last two) |
| Type prefix | "Cessna Bravo Charlie Delta", "Piper India Charlie Delta" |
| Airline / telephony | "Alitalia one two three" (recognize it as `other`) |

`context.own_callsign` supplies the registration and the spoken variants. The response reports `callsign.match`: `own` / `other` / `none` and a score. An instruction addressed to another aircraft is still parsed (useful for situational awareness), but `addressee` is `other_aircraft`, and no suggested action is produced.

### ATC instruction types

These are the canonical `type` values of `atc.instructions[]` in the protocol. Each row also gives the compact tuple the LLM emits (see [architecture](architecture.md#compact-internal-form)).

| `type` | Fields | Compact tuple | Readback required (SERA.14055) | Example |
|---|---|---|---|---|
| `altitude` | `verb` (climb/descend/maintain), `value`, `unit` (ft/FL), `restriction`? | `["alt","climb",5000,"ft"]` | yes | "climb altitude 5000 feet" |
| `heading` | `turn` (left/right/null), `value` 1–360 | `["hdg","left",270]` | yes | "turn left heading 270" |
| `speed` | `value`, `unit` (kt) | `["spd",90]` | yes | "reduce speed 90 knots" |
| `squawk` | `code` (4 octal digits), `ident`? | `["sqk","4321"]` | yes | "squawk 4321" |
| `qnh` | `value`, `unit` (hPa/inHg) | `["qnh",1013]` | yes | "QNH 1013" |
| `frequency_change` | `verb` (contact/monitor), `station`, `frequency_mhz` | `["frq","contact","perugia tower","118.100"]` | yes | "contact Perugia Tower 118.1" |
| `runway` | `designator` | `["rwy","01"]` | yes | "runway 01" |
| `cleared_takeoff` | `runway`? | `["ctko","01"]` | yes | "cleared for takeoff" |
| `cleared_land` | `runway`? | `["clnd","01"]` | yes | "cleared to land" |
| `line_up` | `runway`?, `wait` | `["lup","01",true]` | yes | "line up and wait" |
| `hold_short` | `point` | `["hshrt","A"]` | yes | "hold short runway 01" / "holding point A" |
| `taxi` | `to`, `via`[] | `["taxi","holding point a",["b","a"]]` | yes | "taxi to holding point A via B" |
| `cleared_zone` | `zone`, `via`?, `altitude`? | `["czone","perugia ctr","nord","2000"]` | yes | "cleared to enter the CTR via Nord, not above 2000 feet" |
| `direct_to` | `fix` | `["dct","lirz"]` | yes | "proceed direct LIRZ" |
| `join_circuit` | `leg` (downwind/base/final/crosswind), `runway`? | `["join","downwind","01"]` | yes | "join right downwind runway 01" |
| `report` | `when` (point or condition) | `["rpt","downwind"]` | no (acknowledge) | "report downwind" |
| `traffic_info` | `clock`, `distance_nm`?, `level`?, `aircraft`? | `["tfc",2,3,"1500","cessna"]` | no | "traffic 2 o'clock 3 miles, Cessna" |
| `wind` | `direction`, `speed_kt`, `gust_kt`? | `["wind",360,8]` | no | "wind 360 degrees 8 knots" |
| `go_around` | — | `["ga"]` | yes | "go around" |
| `hold` | `fix`?, `instructions` text | `["hold","nord"]` | yes | "hold over Nord" |
| `standby` | — | `["stby"]` | no | "standby" |
| `say_again` | `what`? | `["sayag"]` | — | "say again" |
| `unparsed` | `text` | `["unp","…"]` | — | anything recognized as ATC but not typed |

The table extends only by adding rows, with a protocol minor version bump. `unparsed` keeps coverage honest: a transmission is never silently dropped.

### Readback generation

The readback is built from templates per instruction type and language, never by the LLM. Only instructions marked "readback required" are included, followed by the own callsign in its abbreviated form. For example, "Climb altitude 5000 feet, QNH 1013, India Charlie Delta."

Phase 4 adds a **readback check**: RB-08 sends the pilot's transmitted readback, and rb-llm compares it with the last clearance for our callsign (wrong digits, missing items).

### Units and number conventions in ATC

| Item | Spoken | Protocol |
|---|---|---|
| Altitude | "five thousand", "one thousand five hundred" | `5000`, `"unit":"ft"` |
| Flight level | "flight level eight five" | `85`, `"unit":"FL"` |
| Heading | "two seven zero" | `270` |
| Frequency | "one one eight decimal one" | `"118.100"` |
| QNH | "one zero one three" / "two niner niner two" (inHg) | `1013` hPa / `29.92` inHg |
| Squawk | "four three two one" | `"4321"` (string, octal digits 0–7) |
| Runway | "zero one", "two seven left" | `"01"`, `"27L"` |

## 3. Out of scope

- Free-form aviation Q&A ("what is the VFR minimum in class D?"). It can be considered later with a curated, retrieval-based knowledge base, but never from model memory.
- Producing flight data, weather or NOTAMs.
- Controlling anything directly. rb-llm has no access to the RB-01 API.
- Speech recognition. rb-llm receives text. However, ATC audio through general-purpose Vosk models will be poor: see [roadmap](roadmap.md) for the STT risk.
