"""Tests for contextual weather read recipes and proactivity correlation."""
import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
import unittest

from hermes_muse.host import HermesHost
from hermes_muse.service import Companion
from hermes_muse.store import Store
from hermes_muse.views import validate_tools_note
from test_companion import prepare, Host

ROOT = Path(__file__).resolve().parents[1]


class WeatherSwitchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name))
        template = (ROOT / "templates/PROACTIVE_PREFERENCES.md").read_text(encoding="utf-8")
        self.store.write_text("PROACTIVE_PREFERENCES.md", template)
        self.service = Companion(self.store, HermesHost(self.store, plugin_root=ROOT))

    def test_default_is_off_and_template_header_is_unchanged(self):
        # An absent field means off, so existing installs keep their behaviour and the byte-compared
        # default migration in initialize() still matches the shipped template.
        header = (ROOT / "templates/PROACTIVE_PREFERENCES.md").read_text(encoding="utf-8").splitlines()[0]
        self.assertNotIn("weather_context", header)
        self.assertIs(self.service.preferences()["weather_context"], False)

    def test_switch_on_and_off_round_trip_keeps_body(self):
        path = self.store.path("PROACTIVE_PREFERENCES.md")
        body = path.read_text(encoding="utf-8").partition("\n")[2]
        self.assertIs(self.service.preference_update({"weather_context": True})["weather_context"], True)
        self.assertIs(self.service.preferences()["weather_context"], True)
        self.assertEqual(path.read_text(encoding="utf-8").partition("\n")[2], body)
        self.service.preference_update({"weather_context": False})
        self.assertIs(self.service.preferences()["weather_context"], False)

    def test_invalid_value_is_rejected_with_repair_hint(self):
        before = self.store.path("PROACTIVE_PREFERENCES.md").read_bytes()
        for bad in ("true", 1, None, "yes"):
            with self.assertRaises(Exception) as caught:
                self.service.preference_update({"weather_context": bad})
            self.assertEqual(caught.exception.code, "invalid_argument")
            self.assertEqual(caught.exception.details["field"], "data.weather_context")
            self.assertIn("true or false", str(caught.exception))
            self.assertIn("Fix:", str(caught.exception))
        self.assertEqual(self.store.path("PROACTIVE_PREFERENCES.md").read_bytes(), before)

    def test_switch_is_in_the_muse_manage_contract(self):
        from hermes_muse.contracts import FIELDS
        self.assertIn("weather_context", FIELDS["preferences"].split())
        self.assertIn("weather_context", (ROOT / "docs/INTERFACE.md").read_text(encoding="utf-8"))


class WeatherProactivityTests(unittest.TestCase):
    def test_weather_recipe_validates_and_is_not_in_default_tools_note(self):
        recipe = (ROOT / "skills/companion/references/weather.md").read_text(encoding="utf-8")
        # Ensure validation passes (no forbidden credentials or private connection URLs)
        validate_tools_note(recipe)
        self.assertIn("Open-Meteo", recipe)
        self.assertIn("proactive-watch", recipe)
        tools_text = (ROOT / "templates/TOOLS.md").read_text(encoding="utf-8")
        self.assertNotIn("Open-Meteo", tools_text)
        self.assertNotIn("open-meteo", tools_text)

    def test_proactivity_reference_contains_weather_decision_triggers(self):
        weather_text = (ROOT / "skills/companion/references/weather.md").read_text(encoding="utf-8")
        self.assertIn("precipitation probability >= 70%", weather_text)
        self.assertIn(">= 5°C drop or rise", weather_text)
        self.assertIn("forecast:daily", weather_text)
        self.assertIn("practical advice", weather_text)

    def test_default_flow_has_no_weather_thresholds_and_is_gated(self):
        proactivity = (ROOT / "skills/companion/references/proactivity.md").read_text(encoding="utf-8")
        for text in (proactivity, (ROOT / "prompts/proactive-watch.md").read_text(encoding="utf-8")):
            self.assertIn("weather_context", text)
        self.assertIn("skip weather completely", proactivity)
        self.assertIn("no location lookup", proactivity)
        self.assertIn("no request to any weather service", (ROOT / "prompts/proactive-watch.md").read_text(encoding="utf-8"))
        # Thresholds, recipe and service name appear only in the conditional reference.
        for marker in ("precipitation probability >= 70%", ">= 5°C drop or rise", "open-meteo", "forecast:daily"):
            self.assertNotIn(marker, proactivity)
        for path in ("templates/TOOLS.md", "templates/PROACTIVE_PREFERENCES.md", "prompts/proactive-watch.md"):
            self.assertNotIn("open-meteo", (ROOT / path).read_text(encoding="utf-8").lower())

    def test_weather_source_check_lifecycle(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = prepare(temp.name)
        host = Host()
        now = datetime(2026, 10, 24, 12, tzinfo=timezone.utc).timestamp()
        companion = Companion(store, host, lambda: now)

        data = {
            "source": {"connection": "weather", "account": "open-meteo", "resource": "forecast:daily"},
            "scope": "Next 5 days rolling forecast for local context",
            "status": "checked",
            "expected_resources": ["forecast:daily"],
            "checked_resources": ["forecast:daily"],
            "summary": "5-day forecast evaluated: 100% rain on Sunday, 8°C temperature drop on Monday",
            "started_at": now - 10,
            "request_id": "weather-patrol-check-001",
            "expected_revision": "absent",
        }
        record = companion.source_check(data)
        self.assertEqual(record["status"], "checked")
        self.assertEqual(record["source"]["connection"], "weather")
        self.assertEqual(record["source"]["account"], "open-meteo")
        self.assertEqual(record["source"]["resource"], "forecast:daily")

        # Verify listing through context
        context = companion.context()
        source_checks = context.get("source_checks", [])
        self.assertTrue(any(s["id"] == record["id"] for s in source_checks))
