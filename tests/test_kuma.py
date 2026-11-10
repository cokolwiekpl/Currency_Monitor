"""Optional Push reporting, including failures through the monitoring entry point."""
import contextlib
import io
import os
import unittest
from decimal import Decimal
from http.client import RemoteDisconnected
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

import main

PUSH_URL = "http://uptime.home/api/push/private-test-token?status=up&msg=OK&ping=&extra=keep"


class KumaTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {
            "UPTIME_KUMA_PUSH_URL": PUSH_URL,
            "DISCORD_WEBHOOK_PRIMARY": "https://discord.com/api/webhooks/123/example",
        }, clear=True))
        self.output = self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.http = self.stack.enter_context(patch.object(main, "urlopen"))
        self.http.side_effect = lambda *args, **kwargs: io.BytesIO(b'{"ok":true}')

    def prepare_cycle(self):
        config = main.load_config(Path(__file__).resolve().parents[1] / "config/alerts.json")
        self.load = self.stack.enter_context(patch.object(main, "load_config", return_value=config))
        self.stack.enter_context(patch.object(main, "state_lock", return_value=contextlib.nullcontext()))
        self.stack.enter_context(patch.object(main, "load_state", return_value={"version": 1, "alerts": {}}))
        self.stack.enter_context(patch.object(main, "checkpoint"))
        self.stack.enter_context(patch.object(main.time, "monotonic", side_effect=[10.0, 11.25]))
        quotes = [main.Quote("previous", Decimal("4"), "NBP", "previous"),
                  main.Quote("current", Decimal("4.1"), "NBP", "current")]
        self.fetch = self.stack.enter_context(patch.object(main, "fetch_nbp_history", return_value=quotes))
        self.discord = self.stack.enter_context(patch.object(main, "send_to_discord"))

    def assert_status(self, expected):
        self.http.assert_called_once()
        request = self.http.call_args.args[0]
        query = parse_qs(urlparse(request.full_url).query)
        self.assertEqual(query["status"], [expected])
        self.assertEqual(query["ping"], ["1250.00"])
        self.assertEqual(query["extra"], ["keep"])
        self.assertEqual(len(query["msg"]), 1)
        self.assertNotEqual(query["msg"], ["OK"])
        self.assertEqual(self.http.call_args.kwargs["timeout"], 5)
        self.assertNotIn(PUSH_URL, self.output.getvalue())
        self.assertNotIn("private-test-token", self.output.getvalue())

    def test_successful_cycle_reports_up(self):
        self.prepare_cycle()
        self.assertEqual(main.main(["--once"]), 0)
        self.discord.assert_called_once()
        self.assert_status("up")

    def test_provider_failure_reports_down_even_when_other_check_succeeds(self):
        self.prepare_cycle()
        quotes = self.fetch.return_value
        self.fetch.side_effect = [URLError("provider unavailable"), quotes]
        self.assertEqual(main.main(["--once"]), 1)
        self.assertEqual(self.fetch.call_count, 2)
        self.assert_status("down")

    def test_discord_failure_reports_down(self):
        self.prepare_cycle()
        self.discord.side_effect = RuntimeError("Discord HTTP 500")
        self.assertEqual(main.main(["--once"]), 1)
        self.assert_status("down")

    def test_configuration_reload_failure_reports_down(self):
        self.prepare_cycle()
        self.load.side_effect = [self.load.return_value, ValueError("Invalid configuration")]
        self.assertEqual(main.main(["--once"]), 1)
        self.fetch.assert_not_called()
        self.assert_status("down")

    def test_disabled_integration_makes_no_request(self):
        for value in (None, "", " "):
            with self.subTest(value=value):
                if value is None:
                    os.environ.pop("UPTIME_KUMA_PUSH_URL", None)
                else:
                    os.environ["UPTIME_KUMA_PUSH_URL"] = value
                main.send_kuma_heartbeat(True, 1.25)
        self.http.assert_not_called()

    def test_connection_errors_do_not_change_cycle_result_or_expose_token(self):
        self.prepare_cycle()
        self.http.side_effect = URLError(PUSH_URL)
        self.assertEqual(main.main(["--once"]), 0)
        self.discord.assert_called_once()
        self.assertIn("heartbeat could not be delivered", self.output.getvalue())
        self.assertNotIn("private-test-token", self.output.getvalue())

    def test_http_timeout_and_protocol_errors_are_safe(self):
        for error in (HTTPError(PUSH_URL, 500, PUSH_URL, {}, None), TimeoutError(PUSH_URL),
                      RemoteDisconnected(PUSH_URL), ValueError(PUSH_URL)):
            with self.subTest(error=type(error).__name__):
                self.http.side_effect = error
                main.send_kuma_heartbeat(False, 1.25)
        self.assertNotIn("private-test-token", self.output.getvalue())

    def test_rejected_and_invalid_responses_are_safe(self):
        for body in (b'{"ok":false,"msg":"private-test-token"}', b'private-test-token', b'[]'):
            with self.subTest(body=body):
                self.http.side_effect = lambda *args, **kwargs: io.BytesIO(body)
                main.send_kuma_heartbeat(True, 1.25)
        self.assertNotIn("private-test-token", self.output.getvalue())
        self.assertEqual(self.output.getvalue().count("heartbeat could not be delivered"), 3)

    def test_invalid_push_url_is_safe(self):
        for url in ("file:///private-test-token", "http://[private-test-token", "http://"):
            with self.subTest(url=url), patch.dict(os.environ, {"UPTIME_KUMA_PUSH_URL": url}):
                main.send_kuma_heartbeat(True, 1.25)
        self.http.assert_not_called()
        self.assertNotIn("private-test-token", self.output.getvalue())

    def test_non_monitoring_modes_never_send_heartbeats(self):
        self.prepare_cycle()
        for args in (["--validate"], ["--preview"], ["--test"], ["--test", "--test-profile", "primary"]):
            with self.subTest(args=args):
                self.assertEqual(main.main(args), 0)
        self.http.assert_not_called()

    def test_periodic_monitor_continues_after_push_failure(self):
        self.prepare_cycle()
        self.stack.enter_context(patch.object(main.time, "monotonic", side_effect=[10, 11.25, 20, 21.25]))
        sleep = self.stack.enter_context(patch.object(main.time, "sleep", side_effect=[None, KeyboardInterrupt]))
        self.http.side_effect = [URLError(PUSH_URL), io.BytesIO(b'{"ok":true}')]
        with self.assertRaises(KeyboardInterrupt):
            main.main([])
        self.assertEqual(self.http.call_count, 2)
        self.assertEqual(self.fetch.call_count, 4)
        self.assertEqual(sleep.call_count, 2)

    def test_https_url_without_query_is_supported(self):
        os.environ["UPTIME_KUMA_PUSH_URL"] = "https://uptime.home/api/push/private-test-token"
        main.send_kuma_heartbeat(True, 1.25)
        query = parse_qs(urlparse(self.http.call_args.args[0].full_url).query)
        self.assertEqual(query["status"], ["up"])
        self.assertEqual(query["ping"], ["1250.00"])
