"""Configuration precedence and validation for Portainer deployments."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main

DEFAULT = Path(__file__).resolve().parents[1] / "config" / "alerts.json"


class EnvironmentConfigTests(unittest.TestCase):
    def setUp(self):
        environment = dict(os.environ)
        environment.pop("ALERTS_CONFIG_JSON", None)
        self.environment = patch.dict(os.environ, environment, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_file_configuration_without_environment(self):
        self.assertEqual(main.load_config(DEFAULT).profiles[0].id, "primary")

    def test_environment_has_precedence_over_file(self):
        expected = main.load_config(DEFAULT)
        with patch.dict(os.environ, {"ALERTS_CONFIG_JSON": DEFAULT.read_text()}):
            self.assertEqual(main.load_config(Path("missing.json")), expected)

    def test_multiple_sources_and_profiles_from_environment(self):
        path = DEFAULT.with_name("example_multi_source.json")
        expected = main.load_config(path)
        with patch.dict(os.environ, {"ALERTS_CONFIG_JSON": path.read_text()}):
            self.assertEqual(main.load_config(DEFAULT), expected)

    def test_invalid_environment_never_falls_back(self):
        for value in ("", " ", "{", "null", "[]", "{}", '{"sources":[],"profiles":[]}'):
            with self.subTest(value=value), patch.dict(os.environ, {"ALERTS_CONFIG_JSON": value}):
                with self.assertRaises(ValueError):
                    main.load_config(DEFAULT)

    def test_missing_and_malformed_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "alerts.json")
            with self.assertRaisesRegex(ValueError, "Configuration file not found"):
                main.load_config(path)
            path.write_text("{")
            with self.assertRaisesRegex(ValueError, "invalid JSON at line"):
                main.load_config(path)

    def test_environment_reloaded_and_validated_on_each_call(self):
        raw = json.loads(DEFAULT.read_text())
        with patch.dict(os.environ, {"ALERTS_CONFIG_JSON": json.dumps(raw)}):
            before = main.load_config()
            raw["profiles"][0]["alerts"][0]["threshold"] = "4.50"
            os.environ["ALERTS_CONFIG_JSON"] = json.dumps(raw)
            after = main.load_config()
            self.assertNotEqual(main.signature(before.profiles[0], before.profiles[0].alerts[0]),
                                main.signature(after.profiles[0], after.profiles[0].alerts[0]))
            raw["profiles"][0]["alerts"][0]["direction"] = "invalid"
            os.environ["ALERTS_CONFIG_JSON"] = json.dumps(raw)
            with self.assertRaisesRegex(ValueError, "direction"):
                main.load_config()

    def test_malformed_environment_does_not_log_its_contents(self):
        secret = "private-placeholder-value"
        with patch.dict(os.environ, {"ALERTS_CONFIG_JSON": '{"' + secret}), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main.main(["--validate"]), 2)
            self.assertIn("ALERTS_CONFIG_JSON", output.getvalue())
            self.assertNotIn(secret, output.getvalue())

    def test_provider_request_errors_hide_header_values(self):
        source = {"url": "https://example.com/rates"}
        with patch.object(main, "http_get_json", side_effect=ValueError("Invalid header value: private-token")):
            with self.assertRaisesRegex(ValueError, "invalid HTTP request or JSON response") as caught:
                main.fetch_json_snapshot("example", source, "USD", "mid", {})
        self.assertNotIn("private-token", str(caught.exception))
