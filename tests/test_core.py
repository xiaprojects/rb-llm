"""Unit tests: normalizer, aircraft status, catalog, task post-processing (fake engine)."""
import json
import unittest

from helpers import CATALOG_DIR, DEMO, FakeEngine, assert_valid

from rbllm import prompts, tasks
from rbllm.catalog import CatalogStore
from rbllm.llama_client import EngineError
from rbllm.normalize import normalize, numbers_in, spelled_letters
from rbllm.status import build_status, clock_position, select_facts, _valid


def demo_snapshot():
    with open(DEMO, encoding="utf-8") as f:
        return json.load(f)


def catalog(lang="en"):
    cs = CatalogStore()
    cs.load_dir(CATALOG_DIR)
    return cs.get(lang)


class TestNormalize(unittest.TestCase):
    def test_atc_numbers(self):
        cases = {
            "India Charlie Delta, climb altitude five thousand feet, QNH one zero one three":
                "india charlie delta climb altitude 5000 feet qnh 1013",
            "contact perugia approach one two zero decimal seven": "contact perugia approach 120.7",
            "one thousand five hundred feet": "1500 feet",
            "flight level eight five": "flight level 85",
            "squawk four three two one": "squawk 4321",
            "turn left heading two seven zero": "turn left heading 270",
            "runway zero one": "runway 01",
            "wind three six zero degrees niner knots": "wind 360 degrees 9 knots",
        }
        for src, want in cases.items():
            self.assertEqual(normalize(src, "en"), want, src)

    def test_italian(self):
        self.assertEqual(normalize("quota due mila", "it"), "quota 2000")
        self.assertEqual(normalize("qnh uno zero uno tre", "it"), "qnh 1013")

    def test_articles_are_not_numbers(self):
        self.assertEqual(normalize("imposta una frequenza", "it"), "imposta una frequenza")

    def test_aliases(self):
        self.assertEqual(normalize("dimmi ci acca ti", "it", {"ci acca ti": "cht"}), "dimmi cht")

    def test_spelled(self):
        self.assertEqual(spelled_letters("india charlie delta climb".split()), "ICD")
        self.assertEqual(spelled_letters("papa golf 0 2".split()), "PG02")

    def test_numbers_in(self):
        self.assertEqual(numbers_in("qnh 1013 freq 120.700 at 45,43"), [1013.0, 120.7, 45.43])


class TestStatus(unittest.TestCase):
    def setUp(self):
        self.st = build_status(demo_snapshot(), "demo")
        self.facts = {f.key: f.text for f in self.st.facts}

    def test_position_and_airfields(self):
        self.assertIn("43.10466 N", self.facts["gps.position"])
        self.assertIn("12.25112 E", self.facts["gps.position"])
        self.assertIn("Montemelino", self.facts["airfield.nearest_1"])

    def test_sentinels_dropped(self):
        # AHRSGyroHeading and AHRSTurnRate are 3276.7 (= no data) in the demo capture
        self.assertNotIn("ahrs.turn_rate", self.facts)
        self.assertFalse(any("3276" in t for t in self.facts.values()))
        self.assertTrue(_valid(34000))  # a real traffic altitude is not a sentinel
        self.assertFalse(_valid(3276.7))
        self.assertFalse(_valid(-3276.7))

    def test_ems_zero_is_not_connected(self):
        self.assertNotIn("ems.enginerpm", self.facts)
        self.assertIn("engine rpm", self.facts["ems.not_connected"])

    def test_traffic(self):
        self.assertIn("RYR465X", self.facts["traffic.1"])
        self.assertIn("34000 ft", self.facts["traffic.1"])

    def test_no_fix(self):
        snap = demo_snapshot()
        snap["/getSituation"]["GPSFixQuality"] = 0
        facts = {f.key: f.text for f in build_status(snap).facts}
        self.assertIn("NO GPS FIX", facts["gps.fix"])
        self.assertNotIn("gps.position", facts)
        self.assertFalse(any(k.startswith("airfield.") for k in facts))

    def test_clock(self):
        self.assertEqual(clock_position(90, 0), 3)
        self.assertEqual(clock_position(0, 0), 12)  # never "0 o'clock"
        self.assertEqual(clock_position(350, 10), 11)

    def test_select_facts(self):
        pos = select_facts(self.st, "where are we?").keys()
        self.assertIn("gps.position", pos)
        self.assertNotIn("ems.cht1", pos)
        eng = select_facts(self.st, "come va il motore?").keys()
        self.assertIn("ems.cht1", eng)
        self.assertNotIn("gps.position", eng)
        self.assertEqual(len(select_facts(self.st, "xyzzy").facts), len(self.st.facts))


class TestCatalog(unittest.TestCase):
    def test_load(self):
        cat = catalog("en")
        self.assertIsNotNone(cat)
        self.assertIn("ems.cht", cat.param_keys())
        self.assertIn("display.map", cat.param_keys())
        self.assertIn("ems.fuel2", cat.param_keys())  # endpoint without English keyword
        self.assertNotIn("display.display", cat.param_keys())
        self.assertEqual(len(cat.hash), 16)

    def test_prompt_is_deterministic(self):
        self.assertEqual(catalog("it").prompt_section(), catalog("it").prompt_section())


class TestAsk(unittest.TestCase):
    def test_ok(self):
        eng = FakeEngine({"facts": ["gps.position"], "answer": "Latitude 43.10 north, longitude 12.25 east."})
        st = build_status(demo_snapshot(), "demo")
        r = tasks.run_ask({"request_id": "a", "lang": "en", "text": "where are we?"}, eng, st)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["warnings"], [])
        self.assertEqual(eng.calls[0]["slot"], tasks.SLOT["ask"])
        assert_valid(self, "ask-response.schema.json", r)

    def test_refusal_retried_then_ok(self):
        eng = FakeEngine({"facts": [], "answer": "I'm sorry, I can't access your real-time GPS location."},
                         {"facts": ["gps.position"], "answer": "Latitude 43.10 north, longitude 12.25 east."})
        st = build_status(demo_snapshot(), "demo")
        req = {"request_id": "a", "lang": "en", "text": "where are we?", "options": {"deadline_ms": 10000}}
        r = tasks.run_ask(req, eng, st)
        self.assertEqual(r["status"], "ok")
        self.assertEqual(len(eng.calls), 2)
        self.assertTrue(any("refusal" in w for w in r["warnings"]))

    def test_refusal_reported(self):
        refuse = {"facts": [], "answer": "Non ho accesso alla posizione GPS in tempo reale."}
        eng = FakeEngine(refuse, refuse)
        r = tasks.run_ask({"request_id": "a", "lang": "it", "text": "dove siamo", "options": {"deadline_ms": 10000}},
                          eng, build_status(demo_snapshot(), "demo"))
        self.assertEqual(r["status"], "refused")
        assert_valid(self, "ask-response.schema.json", r)

    def test_ungrounded_number_warned(self):
        eng = FakeEngine({"facts": ["gps.altitude"], "answer": "We are at 4500 feet."})
        r = tasks.run_ask({"request_id": "a", "lang": "en", "text": "how high"}, eng, build_status(demo_snapshot()))
        self.assertTrue(any("4500" in w for w in r["warnings"]))

    def test_invented_distance_warned(self):
        # 34.6 is close to an unrelated fact (cylinder 34 °C): must still be flagged
        eng = FakeEngine({"facts": ["traffic.1"], "answer": "RYR465X is 34.6 NM away."})
        r = tasks.run_ask({"request_id": "a", "lang": "en", "text": "where is the closest traffic"}, eng,
                          build_status(demo_snapshot()))
        self.assertTrue(any("34.6" in w for w in r["warnings"]), r["warnings"])

    def test_thousands_separator_grounded(self):
        eng = FakeEngine({"facts": ["traffic.1"], "answer": "RYR465X at 34,000 ft, 26.6 NM."})
        r = tasks.run_ask({"request_id": "a", "lang": "en", "text": "any traffic around"}, eng,
                          build_status(demo_snapshot()))
        self.assertEqual(r["warnings"], [])

    def test_no_data(self):
        r = tasks.run_ask({"request_id": "a", "lang": "it", "text": "dove siamo"}, FakeEngine(), build_status({}))
        self.assertEqual(r["status"], "no_data")
        assert_valid(self, "ask-response.schema.json", r)

    def test_refusal_patterns(self):
        for t in ["I cannot access your location", "I don't have access to real-time GPS data",
                  "As an AI language model", "Non posso fornire la posizione"]:
            self.assertTrue(tasks.is_refusal(t), t)
        self.assertFalse(tasks.is_refusal("Latitude 43.10 north, longitude 12.25 east."))


class TestCommand(unittest.TestCase):
    def run_cmd(self, out, text, **extra):
        req = {"request_id": "c", "lang": "en", "text": text, "context": {"wake_word": "computer"}}
        req.update(extra)
        r = tasks.run_command(req, FakeEngine(out), catalog("en"))
        assert_valid(self, "parse-response.schema.json", r)
        return r

    def test_get(self):
        r = self.run_cmd({"action": "GET", "param": "ems.cht"}, "computer what are the cylinder temperatures")
        self.assertEqual((r["status"], r["command"]["component"], r["command"]["parameter"]), ("ok", "ems", "cht"))

    def test_radio_frequency(self):
        r = self.run_cmd({"action": "SET", "param": "radio.radio", "args": {"frequency": "118.5"}},
                         "computer tune radio one one eight decimal five")
        self.assertEqual(r["command"]["args"], {"frequency_mhz": "118.500"})
        self.assertFalse(r["command"]["requires_confirmation"])

    def test_invalid_frequency_dropped(self):
        r = self.run_cmd({"action": "SET", "param": "radio.radio", "args": {"frequency": "137.5"}},
                         "computer tune radio 137.5")
        self.assertNotIn("frequency_mhz", r["command"]["args"])
        self.assertTrue(r["command"]["requires_confirmation"])

    def test_spelled_airfield_code(self):
        r = self.run_cmd({"action": "SET", "param": "autopilot.destination",
                          "args": {"airfield": "lima india romeo zulu"}}, "set destination lima india romeo zulu")
        self.assertEqual(r["command"]["args"]["airfield"], {"code": "LIRZ"})

    def test_airfield_name(self):
        r = self.run_cmd({"action": "SET", "param": "autopilot.destination", "args": {"airfield": "pisa"}},
                         "take me to pisa")
        self.assertEqual(r["command"]["args"]["airfield"], {"name": "pisa"})

    def test_display_forced_to_set(self):
        r = self.run_cmd({"action": "GET", "param": "display.map"}, "primary show map")
        self.assertEqual(r["command"]["action"], "SET")

    def test_not_addressed(self):
        req = {"request_id": "c", "lang": "en", "text": "nice lake"}
        r = tasks.run_command(req, FakeEngine({"action": "GET", "param": "none"}), catalog("en"))
        self.assertEqual((r["status"], r["kind"]), ("not_addressed", "other"))
        assert_valid(self, "parse-response.schema.json", r)

    def test_no_match_when_addressed(self):
        r = self.run_cmd({"action": "GET", "param": "none"}, "computer sing a song")
        self.assertEqual((r["status"], r["kind"], r["addressee"]), ("no_match", "command", "rb"))

    def test_timeout(self):
        req = {"request_id": "c", "lang": "en", "text": "computer cht"}
        r = tasks.run_command(req, FakeEngine(EngineError("timeout", "deadline")), catalog("en"))
        self.assertEqual((r["status"], r["error"]["code"]), ("error", "timeout"))
        assert_valid(self, "parse-response.schema.json", r)

    def test_schema_enum_is_catalog(self):
        cat = catalog("en")
        enum = prompts.command_schema(cat.param_keys())["properties"]["param"]["enum"]
        self.assertEqual(set(enum), set(cat.param_keys()) | {"none"})


OWN = {"own_callsign": {"registration": "I-ABCD"}}


class TestAtc(unittest.TestCase):
    def run_atc(self, out, text, lang="en"):
        r = tasks.run_atc({"request_id": "t", "lang": lang, "text": text, "context": OWN}, FakeEngine(out))
        assert_valid(self, "parse-response.schema.json", r)
        return r

    def test_clearance(self):
        r = self.run_atc({"kind": "atc", "callsign": "india charlie delta", "station": "perugia tower",
                          "instructions": [{"type": "altitude", "verb": "climb", "value": 5000, "unit": "ft"},
                                           {"type": "qnh", "value": 1013, "unit": "hPa"}]},
                         "india charlie delta perugia tower climb altitude five thousand feet qnh one zero one three")
        self.assertEqual(r["addressee"], "own_aircraft")
        self.assertEqual(r["atc"]["readback"]["text"], "Climb 5000 feet, QNH 1013, India Charlie Delta")
        self.assertEqual(r["answer"]["display"], "ALT CLB 5000 FT · QNH 1013")

    def test_invented_number_dropped(self):
        r = self.run_atc({"kind": "atc", "callsign": "india charlie delta", "station": None,
                          "instructions": [{"type": "altitude", "verb": "climb", "value": 6000, "unit": "ft"}]},
                         "india charlie delta climb five thousand feet")
        self.assertEqual(r["atc"]["instructions"], [])
        self.assertTrue(any("6000" in w for w in r["warnings"]))

    def test_invented_station_dropped(self):
        r = self.run_atc({"kind": "atc", "callsign": "india charlie delta", "station": "perugia tower",
                          "instructions": [{"type": "cleared_land", "runway": "01"}]},
                         "india charlie delta runway zero one cleared to land")
        self.assertIsNone(r["atc"]["station"])

    def test_frequency_change_suggests_confirmed_action(self):
        r = self.run_atc({"kind": "atc", "callsign": "india charlie delta", "station": None, "instructions": [
            {"type": "frequency_change", "verb": "contact", "station": "perugia approach", "frequency": "120.7"}]},
            "india charlie delta contact perugia approach one two zero decimal seven")
        sa = r["atc"]["suggested_actions"]
        self.assertEqual(sa[0]["args"]["frequency_mhz"], "120.700")
        self.assertTrue(sa[0]["requires_confirmation"])

    def test_other_aircraft(self):
        r = self.run_atc({"kind": "atc", "callsign": "india xray yankee", "station": None,
                          "instructions": [{"type": "report", "when": "downwind"}]},
                         "india xray yankee report downwind")
        self.assertEqual((r["status"], r["addressee"]), ("not_addressed", "other_aircraft"))
        self.assertEqual(r["atc"]["suggested_actions"], [])

    def test_readback_detected(self):
        r = self.run_atc({"kind": "atc", "callsign": "india charlie delta", "station": None,
                          "instructions": [{"type": "altitude", "verb": "climb", "value": 5000, "unit": "ft"}]},
                         "climb five thousand feet india charlie delta")
        self.assertEqual((r["kind"], r["addressee"]), ("readback", "atc"))

    def test_missing_altitude_verb_taken_from_text(self):
        r = self.run_atc({"kind": "atc", "callsign": "india charlie delta", "station": None,
                          "instructions": [{"type": "altitude", "value": 5000, "unit": "ft"}]},
                         "india charlie delta salite a cinque mila piedi", lang="it")
        self.assertEqual(r["atc"]["instructions"][0]["verb"], "climb")
        self.assertEqual(r["atc"]["readback"]["text"], "Salire 5000 piedi, India Charlie Delta")

    def test_wrong_altitude_verb_corrected(self):
        r = self.run_atc({"kind": "atc", "callsign": "india charlie delta", "station": None,
                          "instructions": [{"type": "altitude", "verb": "maintain", "value": 5000, "unit": "ft"}]},
                         "india charlie delta salite a cinque mila piedi", lang="it")
        self.assertEqual(r["atc"]["instructions"][0]["verb"], "climb")
        self.assertTrue(any("corrected" in w for w in r["warnings"]))

    def test_missing_altitude_verb_never_defaulted(self):
        r = self.run_atc({"kind": "atc", "callsign": "india charlie delta", "station": None,
                          "instructions": [{"type": "altitude", "value": 5000, "unit": "ft"}]},
                         "india charlie delta 5000 feet")
        self.assertEqual(r["atc"]["instructions"], [])

    def test_readback_without_model_callsign(self):
        r = self.run_atc({"kind": "atc", "callsign": None, "station": None,
                          "instructions": [{"type": "altitude", "verb": "climb", "value": 5000, "unit": "ft"}]},
                         "climb five thousand feet india charlie delta")
        self.assertEqual((r["kind"], r["addressee"]), ("readback", "atc"))

    def test_callsign_forms(self):
        own = {"registration": "I-ABCD"}
        self.assertEqual(tasks.match_callsign("india alfa bravo charlie delta", own)["match"], "own")
        self.assertEqual(tasks.match_callsign("india charlie delta", own)["match"], "own")
        self.assertEqual(tasks.match_callsign("piper charlie delta", own)["match"], "own")
        self.assertEqual(tasks.match_callsign("india xray yankee", own)["match"], "other")
        self.assertEqual(tasks.match_callsign(None, own)["match"], "none")


class TestClassify(unittest.TestCase):
    def test_classify(self):
        cat = catalog("en")
        c = lambda text, **kw: tasks.classify({"text": text, "lang": "en", **kw}, cat)  # noqa: E731
        self.assertEqual(c("computer what is the altitude"), "command")
        self.assertEqual(c("india charlie delta climb five thousand feet"), "atc")
        self.assertEqual(c("qnh one zero one three"), "atc")
        self.assertEqual(c("squawk 1234", source="push_to_talk"), "command")


if __name__ == "__main__":
    unittest.main()
