import json
import os
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main


class ConfigTests(unittest.TestCase):
    def test_real_config_valid(self):
        config = main.load_config(Path(__file__).parents[1] / 'config' / 'alerts.json')
        self.assertEqual(config.profiles[0].id, 'primary')
        self.assertEqual(config.profiles[0].alerts[0].rate_type, 'mid')

    def test_two_profiles_sources(self):
        data = json.loads((Path(__file__).parents[1] / 'config' / 'example_multi_source.json').read_text())
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, 'config.json')
            p.write_text(json.dumps(data))
            cfg = main.load_config(p)
        self.assertEqual(len(cfg.profiles), 2)
        self.assertEqual(cfg.profiles[1].source_id, 'example_bank')
        self.assertEqual(cfg.profiles[1].alerts[0].rate_type, 'sell')

    def test_invalid_metric_for_bank_rejected(self):
        raw = json.loads((Path(__file__).parents[1] / 'config' / 'example_multi_source.json').read_text())
        raw['profiles'][1]['alerts'][0]['rate_type'] = 'mid'
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, 'config.json')
            p.write_text(json.dumps(raw))
            with self.assertRaisesRegex(ValueError, 'does not provide'):
                main.load_config(p)

    def test_invalid_webhook_rejected(self):
        for url in ['http://discord.com/api/webhooks/123/a', 'https://evil.com/api/webhooks/123/a', 'https://discord.com/']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                main.check_webhook(url)


class SourceTests(unittest.TestCase):
    def test_json_adapter_multiple_currencies_buy_sell(self):
        src = {
            'url': 'https://example.com/rates', 'rates_path': 'data.rates',
            'currency_path': 'currency', 'timestamp_path': 'data.updated_at',
            'price_paths': {'buy': 'bid', 'sell': 'ask'},
        }
        response = {'data': {'updated_at': '2026-11-10T15:45:00+01:00', 'rates': [
            {'currency': 'USD', 'bid': '3,95', 'ask': '4.02'},
            {'currency': 'EUR', 'bid': 4.11, 'ask': '4.29'},
        ]}}
        with patch.object(main, 'http_get_json', return_value=response) as get:
            cache = {}
            usd_buy = main.fetch_json_snapshot('example_bank', src, 'USD', 'buy', cache)
            eur_sell = main.fetch_json_snapshot('example_bank', src, 'EUR', 'sell', cache)
            self.assertEqual(usd_buy.rate, Decimal('3.95'))
            self.assertEqual(eur_sell.rate, Decimal('4.29'))
            self.assertEqual(get.call_count, 1)  # shared endpoint, only one request

    def test_json_adapter_template(self):
        src = {'url': 'https://example.com/rates/{currency}', 'rates_path': 'rate',
               'currency_path': 'code', 'price_paths': {'mid': 'value'}}
        with patch.object(main, 'http_get_json', return_value={'rate': {'code': 'USD', 'value': 4.02}}) as get:
            value = main.fetch_json_snapshot('bank', src, 'USD', 'mid', {})
            self.assertEqual(value.rate, Decimal('4.02'))
            self.assertEqual(get.call_args.args[0], 'https://example.com/rates/USD')

    def test_json_adapter_env_auth(self):
        src = {'url': 'https://example.com/rates', 'rates_path': 'rates',
               'currency_path': 'code', 'price_paths': {'buy': 'buy'},
               'headers_env': {'Authorization': 'BANK_KEY'}}
        with patch.dict(os.environ, {'BANK_KEY': 'Bearer abc'}), patch.object(main, 'http_get_json', return_value={'rates': [{'code': 'USD', 'buy': 3.9}]}) as get:
            main.fetch_json_snapshot('mybank', src, 'USD', 'buy', {})
            self.assertEqual(get.call_args.args[1], {'Authorization': 'Bearer abc'})

    def test_nbp_c_buy_sell(self):
        payload = {'rates': [
            {'effectiveDate': '2026-11-09', 'bid': 3.91, 'ask': 4.01},
            {'effectiveDate': '2026-11-10', 'bid': 4.04, 'ask': 4.10}]}
        with patch.object(main, 'http_get_json', return_value=payload) as get:
            quotes = main.fetch_nbp_history('USD', 'buy')
            self.assertEqual(quotes[-1].rate, Decimal('4.04'))
            self.assertIn('/c/usd/', get.call_args.args[0])


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.profile = main.Profile('user', 'nbp', 'DISCORD_WEBHOOK_PRIMARY', ())
        self.rule = main.Rule('usd4', 'USD', 'mid', 'above', Decimal('4.00'))
        self.fakewebhook = 'https://discord.com/api/webhooks/123456789/abcde_token'

    def test_crossing_edges(self):
        self.assertTrue(main.crossed(self.rule, Decimal('4.00'), Decimal('4.01')))
        self.assertFalse(main.crossed(self.rule, Decimal('4.01'), Decimal('4.02')))
        self.assertFalse(main.crossed(self.rule, Decimal('3.99'), Decimal('4.00')))
        down = main.Rule('eur', 'EUR', 'sell', 'below', Decimal('4.20'))
        self.assertTrue(main.crossed(down, Decimal('4.20'), Decimal('4.19')))

    def test_json_snapshot_initial_baseline_then_cross_once(self):
        state = {'version': 1, 'alerts': {}}
        with tempfile.TemporaryDirectory() as d, patch.object(main, 'send_to_discord') as send:
            path = Path(d, 'state.json')
            q1 = main.Quote('first', Decimal('3.99'), 'mybank', 'first')
            q2 = main.Quote('second', Decimal('4.05'), 'mybank', 'second')
            main.process_json(self.profile, self.rule, q1, state, self.fakewebhook, path)
            send.assert_not_called()
            main.process_json(self.profile, self.rule, q2, state, self.fakewebhook, path)
            self.assertEqual(send.call_count, 1)
            main.process_json(self.profile, self.rule, q2, state, self.fakewebhook, path)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(main.load_state(path)['alerts']['user/usd4']['last_rate'], '4.05')

    def test_json_failed_discord_retried(self):
        state = {'version': 1, 'alerts': {}}
        with tempfile.TemporaryDirectory() as d, patch.object(main, 'send_to_discord', side_effect=[RuntimeError('failed'), None]) as send:
            path = Path(d, 'state.json')
            main.process_json(self.profile, self.rule, main.Quote('first', Decimal('3.99'), 'bank', 'first'), state, self.fakewebhook, path)
            q = main.Quote('second', Decimal('4.01'), 'bank', 'second')
            with self.assertRaises(RuntimeError):
                main.process_json(self.profile, self.rule, q, state, self.fakewebhook, path)
            self.assertEqual(state['alerts']['user/usd4']['last_rate'], '3.99')
            main.process_json(self.profile, self.rule, q, state, self.fakewebhook, path)
            self.assertEqual(send.call_count, 2)

    def test_nbp_first_run_last_pair(self):
        state = {'version': 1, 'alerts': {}}
        quotes = [main.Quote('2026-11-08', Decimal('3.90'), 'NBP', '2026-11-08'),
                  main.Quote('2026-11-09', Decimal('3.98'), 'NBP', '2026-11-09'),
                  main.Quote('2026-11-10', Decimal('4.01'), 'NBP', '2026-11-10')]
        with tempfile.TemporaryDirectory() as d, patch.object(main, 'send_to_discord') as send:
            path = Path(d, 'state.json')
            main.process_nbp(self.profile, self.rule, quotes, state, self.fakewebhook, path)
            main.process_nbp(self.profile, self.rule, quotes, state, self.fakewebhook, path)
            self.assertEqual(send.call_count, 1)

    def test_nbp_backfills_events_after_downtime(self):
        state = {'version': 1, 'alerts': {'user/usd4': {'signature': main.signature(self.profile, self.rule), 'last_marker': '2026-11-07', 'last_rate': '3.9'}}}
        quotes = [main.Quote('2026-11-07', Decimal('3.90'), 'NBP', '2026-11-07'),
                  main.Quote('2026-11-08', Decimal('4.01'), 'NBP', '2026-11-08'),
                  main.Quote('2026-11-09', Decimal('3.98'), 'NBP', '2026-11-09'),
                  main.Quote('2026-11-10', Decimal('4.06'), 'NBP', '2026-11-10')]
        with tempfile.TemporaryDirectory() as d, patch.object(main, 'send_to_discord') as send:
            main.process_nbp(self.profile, self.rule, quotes, state, self.fakewebhook, Path(d, 'state.json'))
            self.assertEqual(send.call_count, 2)

    def test_user_states_are_isolated(self):
        state = {'version': 1, 'alerts': {}}
        with tempfile.TemporaryDirectory() as d, patch.object(main, 'send_to_discord') as send:
            path = Path(d, 'state.json')
            p1 = self.profile
            p2 = main.Profile('other', 'bank2', 'DISCORD_WEBHOOK_OTHER', ())
            initial = main.Quote('a', Decimal('3.98'), 'Bank', 'a')
            changed = main.Quote('b', Decimal('4.01'), 'Bank', 'b')
            main.process_json(p1, self.rule, initial, state, self.fakewebhook, path)
            main.process_json(p2, self.rule, initial, state, self.fakewebhook, path)
            main.process_json(p1, self.rule, changed, state, self.fakewebhook, path)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(state['alerts']['other/usd4']['last_rate'], '3.98')
            main.process_json(p2, self.rule, changed, state, self.fakewebhook, path)
            self.assertEqual(send.call_count, 2)


if __name__ == '__main__':
    unittest.main()
