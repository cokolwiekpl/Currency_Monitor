"""Regression coverage for validation, persistence failures, and CLI behavior."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
import storage

ROOT = Path(__file__).resolve().parents[1]


class ValidationTests(unittest.TestCase):
    def test_invalid_config_types_raise_readable_errors(self):
        cases = [
            ("source_type", "type must be"),
            ("profile_source", "unknown source"),
            ("enabled", "boolean"),
            ("rate_type", "rate_type must be"),
            ("direction", "direction must be"),
            ("timestamp", "JSON path"),
        ]
        for field, message in cases:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                raw = json.loads((ROOT / "config/example_multi_source.json").read_text())
                profile = raw["profiles"][0]
                if field == "source_type":
                    raw["sources"]["nbp"]["type"] = []
                elif field == "profile_source":
                    profile["source"] = []
                elif field == "enabled":
                    profile["enabled"] = "false"
                elif field == "timestamp":
                    raw["sources"]["example_bank"]["timestamp_path"] = None
                else:
                    profile["alerts"][0][field] = []
                path = Path(directory, "config.json")
                path.write_text(json.dumps(raw))
                with self.assertRaisesRegex(ValueError, message):
                    main.load_config(path)

    def test_numeric_environment_errors_name_the_setting(self):
        for name in ("CHECK_INTERVAL_SECONDS", "REQUEST_TIMEOUT_SECONDS"):
            with self.subTest(name=name), patch.dict(os.environ, {name: "invalid"}):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(main.main(["--validate"]), 2)
                self.assertIn(name, output.getvalue())

    def test_test_profile_requires_test_even_with_preview(self):
        with contextlib.redirect_stdout(io.StringIO()), patch.object(main, "preview") as preview:
            self.assertEqual(main.main(["--preview", "--test-profile", "primary"]), 2)
            preview.assert_not_called()

    def test_discord_rejects_custom_port(self):
        with self.assertRaises(ValueError):
            main.check_webhook("https://discord.com:8443/api/webhooks/123/example")

    def test_decimal_json_response_preserves_precision(self):
        with patch.object(main, "urlopen", return_value=io.BytesIO(b'{"rate": 4.0000000000000001}')):
            self.assertEqual(main.http_get_json("https://example.com")["rate"], Decimal("4.0000000000000001"))

    def test_missing_publication_timestamp_is_rejected(self):
        source = {"url": "https://example.com", "rates_path": "rates",
                  "currency_path": "code", "price_paths": {"mid": "value"},
                  "timestamp_path": "updated"}
        payload = {"rates": [{"code": "USD", "value": "4.00"}], "updated": None}
        with patch.object(main, "http_get_json", return_value=payload):
            with self.assertRaisesRegex(ValueError, "publication date"):
                main.fetch_json_snapshot("example", source, "USD", "mid", {})


class PersistenceTests(unittest.TestCase):
    def test_failed_checkpoint_does_not_advance_memory_or_disk(self):
        profile = main.Profile("primary", "example", "WEBHOOK", ())
        rule = main.Rule("usd", "USD", "mid", "above", Decimal("4"))
        previous = main.Quote("2026-11-09", Decimal("3.9"), "Example", "2026-11-09")
        current = main.Quote("2026-11-10", Decimal("4.1"), "Example", "2026-11-10")
        for provider in ("json", "nbp"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as directory:
                path = Path(directory, "state.json")
                state = {"version": 1, "alerts": {}}
                storage.checkpoint(state, "primary/usd", main.signature(profile, rule), previous, path)
                saved = json.loads(path.read_text())
                with patch.object(storage, "save_state", side_effect=OSError("disk full")), patch.object(main, "send_to_discord"):
                    with self.assertRaises(OSError):
                        if provider == "json":
                            main.process_json(profile, rule, current, state, "unused", path)
                        else:
                            main.process_nbp(profile, rule, [previous, current], state, "unused", path)
                self.assertEqual(state, saved)
                self.assertEqual(storage.load_state(path), saved)

    def test_corrupt_state_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "state.json")
            for entry in (None, {}, {"signature": "s", "last_marker": "date", "last_rate": "NaN"}):
                with self.subTest(entry=entry):
                    path.write_text(json.dumps({"version": 1, "alerts": {"primary/usd": entry}}))
                    with self.assertRaises(ValueError):
                        storage.load_state(path)

    def test_process_lock_excludes_second_monitor_and_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "state.json")
            script = "from pathlib import Path; from storage import state_lock; import sys\nwith state_lock(Path(sys.argv[1])): pass"
            with storage.state_lock(path):
                blocked = subprocess.run([sys.executable, "-c", script, str(path)], cwd=ROOT, capture_output=True, text=True, timeout=15)
                self.assertNotEqual(blocked.returncode, 0)
                self.assertIn("Another monitor", blocked.stderr)
            released = subprocess.run([sys.executable, "-c", script, str(path)], cwd=ROOT, capture_output=True, text=True, timeout=15)
            self.assertEqual(released.returncode, 0, released.stderr)


class EntryPointTests(unittest.TestCase):
    def test_once_starts_and_persists_with_mocked_network(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "state.json")
            quotes = [main.Quote("2026-11-09", Decimal("4"), "NBP", "2026-11-09"),
                      main.Quote("2026-11-10", Decimal("4.1"), "NBP", "2026-11-10")]
            with patch.object(main, "CONFIG_PATH", ROOT / "config/alerts.json"), patch.object(storage, "STATE_PATH", path), patch.dict(os.environ, {"DISCORD_WEBHOOK_PRIMARY": "https://discord.com/api/webhooks/123/example"}), patch.object(main, "fetch_nbp_history", return_value=quotes), patch.object(main, "send_to_discord") as send, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main.main(["--once"]), 0)
                self.assertEqual(main.main(["--once"]), 0)
                self.assertEqual(send.call_count, 1)
            self.assertEqual(len(storage.load_state(path)["alerts"]), 2)

    def test_preview_does_not_send_or_write(self):
        config = main.load_config(ROOT / "config/alerts.json")
        quote = main.Quote("today", Decimal("4.1"), "NBP", "today")
        with patch.object(main, "fetch_nbp_history", return_value=[quote]), patch.object(main, "send_to_discord") as send, patch.object(storage, "save_state") as save, contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(main.preview(config))
            send.assert_not_called()
            save.assert_not_called()
