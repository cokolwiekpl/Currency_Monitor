"""Currency threshold alerts: NBP or configurable JSON APIs -> Discord.

Only Python standard library. One container can host multiple independent profiles.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urlparse
from urllib.request import Request, urlopen

from models import Config, Profile, Quote, Rule, as_decimal
from storage import checkpoint, load_state, state_lock

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "config/alerts.json"))
NBP_API = "https://api.nbp.pl/api"
HISTORY_LENGTH = 30
USER_AGENT = "currency-discord-monitor/1.0"
ID_PATTERN = re.compile(r"[a-zA-Z0-9_-]{1,60}\Z")
CURRENCY_PATTERN = re.compile(r"[a-zA-Z]{3}\Z")
ENV_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def log(msg: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{stamp}] {msg}", flush=True)


def validate_https_url(url: str, desc: str) -> None:
    if not isinstance(url, str):
        raise ValueError(f"{desc}: URL must be a string.")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"{desc}: a complete HTTPS URL without credentials is required.")
    if parsed.hostname.lower() in {"localhost", "localhost.localdomain"}:
        raise ValueError(f"{desc}: localhost URLs are not supported.")


def require_path(path, desc: str) -> None:
    if not isinstance(path, str) or not path.strip():
        raise ValueError(f"{desc}: a nonempty JSON path is required.")
    if not re.fullmatch(r"[A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)*", path):
        raise ValueError(f"{desc}: use dot-separated fields, such as data.rates.")


def load_config(path: Path | None = None) -> Config:
    environment_config = os.getenv("ALERTS_CONFIG_JSON")
    if environment_config is not None:
        try:
            raw = json.loads(environment_config, parse_float=Decimal)
        except json.JSONDecodeError as exc:
            raise ValueError(f"ALERTS_CONFIG_JSON contains invalid JSON at line {exc.lineno}, column {exc.colno}.") from None
    else:
        config_path = path or CONFIG_PATH
        try:
            with config_path.open(encoding="utf-8") as fp:
                raw = json.load(fp, parse_float=Decimal)
        except FileNotFoundError:
            raise ValueError(f"Configuration file not found: {config_path}") from None
        except json.JSONDecodeError as exc:
            raise ValueError(f"Configuration file contains invalid JSON at line {exc.lineno}, column {exc.colno}.") from None
    return validate_config(raw)


def validate_config(raw) -> Config:
    """Validate file and environment configuration through the same rules."""
    if not isinstance(raw, dict):
        raise ValueError("Configuration must be a JSON object.")
    sources = raw.get("sources")
    profiles = raw.get("profiles")
    if not isinstance(sources, dict) or not sources:
        raise ValueError("Configuration must contain a nonempty 'sources' object.")
    if not isinstance(profiles, list) or not profiles:
        raise ValueError("Configuration must contain a nonempty 'profiles' list.")
    for source_id, src in sources.items():
        if not isinstance(source_id, str) or not ID_PATTERN.fullmatch(source_id):
            raise ValueError("Invalid source ID.")
        if not isinstance(src, dict) or src.get("type") not in ("nbp", "json_api"):
            raise ValueError(f"Source {source_id}: type must be 'nbp' or 'json_api'.")
        if "label" in src and (not isinstance(src["label"], str) or not 1 <= len(src["label"]) <= 80):
            raise ValueError(f"Source {source_id}: label must be a string of 1 to 80 characters.")
        if src["type"] == "json_api":
            validate_https_url(src.get("url"), f"Source {source_id}")
            require_path(src.get("rates_path"), f"Source {source_id}: rates_path")
            require_path(src.get("currency_path"), f"Source {source_id}: currency_path")
            if "timestamp_path" in src:
                require_path(src["timestamp_path"], f"Source {source_id}: timestamp_path")
            price_paths = src.get("price_paths")
            if not isinstance(price_paths, dict) or not price_paths:
                raise ValueError(f"Source {source_id}: a nonempty 'price_paths' object is required.")
            for k, v in price_paths.items():
                if k not in {"mid", "buy", "sell"}:
                    raise ValueError(f"Source {source_id}: unknown rate type '{k}'.")
                require_path(v, f"Source {source_id}: price_paths.{k}")
            header_env = src.get("headers_env", {})
            if not isinstance(header_env, dict):
                raise ValueError(f"Source {source_id}: headers_env must be an object.")
            for name, env in header_env.items():
                if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9-]+", name):
                    raise ValueError(f"Source {source_id}: invalid header name.")
                if not isinstance(env, str) or not ENV_PATTERN.fullmatch(env):
                    raise ValueError(f"Source {source_id}: header must reference an environment variable.")

    result = []
    profile_ids = set()
    for p in profiles:
        if not isinstance(p, dict):
            raise ValueError("Profile must be a JSON object.")
        if not isinstance(p.get("enabled", True), bool):
            raise ValueError("Profile enabled must be a boolean.")
        if p.get("enabled", True) is False:
            continue
        pid = p.get("id")
        if not isinstance(pid, str) or not ID_PATTERN.fullmatch(pid) or pid in profile_ids:
            raise ValueError(f"Invalid or duplicate profile ID: {pid}")
        profile_ids.add(pid)
        sid = p.get("source")
        if not isinstance(sid, str) or sid not in sources:
            raise ValueError(f"Profile {pid}: unknown source {sid}")
        webhook_env = p.get("webhook_env")
        if not isinstance(webhook_env, str) or not ENV_PATTERN.fullmatch(webhook_env):
            raise ValueError(f"Profile {pid}: webhook_env must reference an environment variable.")
        alerts = p.get("alerts")
        if not isinstance(alerts, list) or not alerts:
            raise ValueError(f"Profile {pid}: a nonempty 'alerts' list is required.")
        rule_ids = set()
        parsed_alerts = []
        for a in alerts:
            if not isinstance(a, dict):
                raise ValueError(f"Profile {pid}: alert must be a JSON object.")
            aid = a.get("id")
            currency = a.get("currency")
            metric = a.get("rate_type", "mid")
            direction = a.get("direction")
            if not isinstance(aid, str) or not ID_PATTERN.fullmatch(aid) or aid in rule_ids:
                raise ValueError(f"Profile {pid}: invalid or duplicate alert ID: {aid}")
            rule_ids.add(aid)
            if not isinstance(currency, str) or not CURRENCY_PATTERN.fullmatch(currency):
                raise ValueError(f"{pid}/{aid}: currency must contain three letters.")
            if metric not in ("mid", "buy", "sell"):
                raise ValueError(f"{pid}/{aid}: rate_type must be mid, buy, or sell.")
            if direction not in ("above", "below"):
                raise ValueError(f"{pid}/{aid}: direction must be above or below.")
            threshold = as_decimal(a.get("threshold"), f"{pid}/{aid}: threshold")
            if sources[sid]["type"] == "json_api" and metric not in sources[sid]["price_paths"]:
                raise ValueError(f"{pid}/{aid}: source {sid} does not provide {metric} rates.")
            parsed_alerts.append(Rule(aid, currency.upper(), metric, direction, threshold))
        result.append(Profile(pid, sid, webhook_env, tuple(parsed_alerts)))
    if not result:
        raise ValueError("No profiles are enabled.")
    return Config(sources, tuple(result))


def request_timeout() -> int:
    return environment_integer("REQUEST_TIMEOUT_SECONDS", 20, 1, 120)


def environment_integer(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}.") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")
    return value


def http_get_json(url: str, headers: dict[str, str] | None = None):
    hdrs = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    req = Request(url, headers=hdrs)
    with urlopen(req, timeout=request_timeout()) as response:
        return json.load(response, parse_float=Decimal)


def get_path(obj, dotted: str):
    value = obj
    for key in dotted.split("."):
        if isinstance(value, dict):
            value = value[key]
        elif isinstance(value, list) and key.isdigit() and int(key) < len(value):
            value = value[int(key)]
        else:
            raise ValueError(f"Cannot read JSON path: {dotted}")
    return value


def fetch_nbp_history(currency: str, rate_type: str) -> list[Quote]:
    tables = ["a", "b"] if rate_type == "mid" else ["c"]
    field = {"mid": "mid", "buy": "bid", "sell": "ask"}[rate_type]
    for table in tables:
        url = f"{NBP_API}/exchangerates/rates/{table}/{currency.lower()}/last/{HISTORY_LENGTH}/?format=json"
        try:
            payload = http_get_json(url)
        except HTTPError as exc:
            if exc.code == 404:
                continue
            raise
        if not isinstance(payload, dict) or not isinstance(payload.get("rates"), list):
            raise ValueError(f"NBP: invalid response format for {currency}.")
        qs = []
        for row in payload["rates"]:
            when = str(row["effectiveDate"])
            qs.append(Quote(when, as_decimal(row[field], f"NBP {currency}"), f"NBP, table {table.upper()}", when))
        if len(qs) < 2:
            raise ValueError(f"NBP: too few {currency} quotes in table {table.upper()}.")
        return sorted(qs, key=lambda q: q.marker)
    raise ValueError(f"NBP: no {currency} quotes found for rate type {rate_type}.")


def fetch_json_snapshot(source_id: str, src: dict, currency: str, rate_type: str, cache: dict) -> Quote:
    # URL templating supports endpoints such as /rates/{currency}; otherwise one endpoint
    # may serve all currencies. No arbitrary expressions or query assembly are evaluated.
    url = src["url"].replace("{currency}", quote(currency, safe=""))
    cache_key = (source_id, url)
    if cache_key not in cache:
        headers = {}
        for header, env in src.get("headers_env", {}).items():
            val = os.getenv(env)
            if not val:
                raise ValueError(f"Source {source_id}: missing environment variable {env}.")
            headers[header] = val
        try:
            cache[cache_key] = http_get_json(url, headers)
        except HTTPError as exc:
            raise ValueError(f"Source {source_id}: HTTP {exc.code} while fetching rates.") from None
        except (URLError, OSError):
            raise ValueError(f"Source {source_id}: network error while fetching rates.") from None
        except ValueError:
            # urllib can include a secret header value in request validation errors.
            raise ValueError(f"Source {source_id}: invalid HTTP request or JSON response.") from None
    payload = cache[cache_key]
    rows = get_path(payload, src["rates_path"])
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        raise ValueError(f"Source {source_id}: rates_path must point to an array or object.")
    matched = [row for row in rows if isinstance(row, dict) and str(get_path(row, src["currency_path"])).upper() == currency]
    if len(matched) != 1:
        raise ValueError(f"Source {source_id}: expected exactly one {currency} quote, found {len(matched)}.")
    rate = as_decimal(get_path(matched[0], src["price_paths"][rate_type]), f"{source_id}/{currency}")
    if "timestamp_path" in src:
        published = get_path(payload, src["timestamp_path"])
        if not isinstance(published, str) or not published.strip():
            raise ValueError(f"Source {source_id}: missing publication date.")
    else:
        published = "publication date unavailable (checked at " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") + ")"
    return Quote(published, rate, src.get("label", source_id), published)


def check_webhook(url: str) -> None:
    parsed = urlparse(url)
    if (parsed.scheme != "https"
        or parsed.hostname not in {"discord.com", "discordapp.com"}
        or not re.fullmatch(r"/api/(?:v\d+/)?webhooks/\d+/[A-Za-z0-9._-]+/?", parsed.path)
        or parsed.netloc != parsed.hostname
        or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Invalid Discord webhook URL in environment variable.")


def send_to_discord(webhook: str, message: str) -> None:
    check_webhook(webhook)
    body = json.dumps({"content": message, "allowed_mentions": {"parse": []}}, ensure_ascii=False).encode("utf-8")
    req = Request(webhook, data=body, headers={"Content-Type": "application/json", "User-Agent": USER_AGENT}, method="POST")
    try:
        with urlopen(req, timeout=request_timeout()) as response:
            response.read()
    except HTTPError as exc:
        # Never log the URL, because it contains the secret webhook token.
        raise RuntimeError(f"Discord HTTP {exc.code}") from None
    except (URLError, OSError):
        raise RuntimeError("Network error while sending to Discord.") from None


def fmt(n: Decimal) -> str:
    return f"{n:.4f}"


def crossed(rule: Rule, prev: Decimal, curr: Decimal) -> bool:
    if rule.direction == "above":
        return prev <= rule.threshold < curr
    return prev >= rule.threshold > curr


def signature(profile: Profile, rule: Rule) -> str:
    return f"{profile.source_id}|{rule.currency}|{rule.rate_type}|{rule.direction}|{rule.threshold.normalize()}"


def alert_message(profile: Profile, rule: Rule, prev: Quote, curr: Quote) -> str:
    action = "rose above" if rule.direction == "above" else "fell below"
    label = {"mid": "mid", "buy": "buy (provider buys the currency)", "sell": "sell (provider sells the currency)"}[rule.rate_type]
    return (
        f"🚨 **{rule.currency}/PLN {action} {fmt(rule.threshold)} PLN!**\n"
        f"Source: **{curr.label}**, {label} rate\n"
        f"Current: **{fmt(curr.rate)} PLN** ({curr.published})\n"
        f"Previous: {fmt(prev.rate)} PLN ({prev.published})\n"
        f"Profile: `{profile.id}` · alert: `{rule.id}`"
    )


def process_nbp(profile: Profile, rule: Rule, quotes: list[Quote], state: dict, webhook: str, state_path: Path | None = None) -> None:
    key = f"{profile.id}/{rule.id}"
    sig = signature(profile, rule)
    saved = state["alerts"].get(key)
    start = len(quotes) - 2
    if isinstance(saved, dict) and saved.get("signature") == sig:
        old_marker = saved.get("last_marker")
        matched = next((i for i, q in enumerate(quotes) if q.marker == old_marker), None)
        if matched is not None:
            start = matched
        elif isinstance(old_marker, str) and old_marker < quotes[0].marker:
            start = 0
            log(f"{key}: some quotes during downtime may fall outside the {HISTORY_LENGTH}-quote history.")
        else:
            log(f"{key}: checkpoint missing from history, checking the latest pair.")
    for i in range(start + 1, len(quotes)):
        prev, curr = quotes[i - 1:i + 1]
        if crossed(rule, prev.rate, curr.rate):
            send_to_discord(webhook, alert_message(profile, rule, prev, curr))
            log(f"ALERT {key}: {fmt(prev.rate)} -> {fmt(curr.rate)} ({curr.marker})")
        checkpoint(state, key, sig, curr, state_path)


def process_json(profile: Profile, rule: Rule, quote_now: Quote, state: dict, webhook: str, state_path: Path | None = None) -> None:
    key = f"{profile.id}/{rule.id}"
    sig = signature(profile, rule)
    saved = state["alerts"].get(key)
    if isinstance(saved, dict) and saved.get("signature") == sig and "last_rate" in saved:
        old_rate = as_decimal(saved["last_rate"], f"saved rate {key}")
        if crossed(rule, old_rate, quote_now.rate):
            previous = Quote(str(saved.get("last_marker", "previous reading")), old_rate, quote_now.label, str(saved.get("last_marker", "previous reading")))
            send_to_discord(webhook, alert_message(profile, rule, previous, quote_now))
            log(f"ALERT {key}: {fmt(old_rate)} -> {fmt(quote_now.rate)}")
    # New JSON source has only one snapshot: first run establishes a baseline,
    # rather than falsely announcing a crossing without a previous quote.
    checkpoint(state, key, sig, quote_now, state_path)


def check_once(cfg: Config, state: dict, state_path: Path | None = None) -> bool:
    success = True
    json_cache = {}
    history_cache = {}
    for profile in cfg.profiles:
        webhook = os.getenv(profile.webhook_env, "").strip()
        try:
            check_webhook(webhook)
        except ValueError as exc:
            log(f"Profile {profile.id}: {exc} (variable {profile.webhook_env})")
            success = False
            continue
        for rule in profile.alerts:
            src = cfg.sources[profile.source_id]
            cache_key = (profile.source_id, rule.currency, rule.rate_type)
            try:
                if src["type"] == "nbp":
                    if cache_key not in history_cache:
                        history_cache[cache_key] = fetch_nbp_history(rule.currency, rule.rate_type)
                    history = history_cache[cache_key]
                    process_nbp(profile, rule, history, state, webhook, state_path)
                    current = history[-1]
                else:
                    if cache_key not in history_cache:
                        history_cache[cache_key] = fetch_json_snapshot(profile.source_id, src, rule.currency, rule.rate_type, json_cache)
                    current = history_cache[cache_key]
                    process_json(profile, rule, current, state, webhook, state_path)
                log(f"{profile.id}: {rule.currency}/PLN {rule.rate_type} = {fmt(current.rate)} ({current.published})")
            except (HTTPError, URLError, TimeoutError, ValueError, KeyError, TypeError, IndexError, OSError, RuntimeError, InvalidOperation) as exc:
                log(f"ERROR {profile.id}/{rule.id}: {exc}")
                success = False
    return success


def preview(cfg: Config) -> bool:
    ok = True
    json_cache, cache = {}, {}
    for profile in cfg.profiles:
        for rule in profile.alerts:
            key = (profile.source_id, rule.currency, rule.rate_type)
            try:
                if key not in cache:
                    src = cfg.sources[profile.source_id]
                    cache[key] = (fetch_nbp_history(rule.currency, rule.rate_type)[-1]
                                  if src["type"] == "nbp" else fetch_json_snapshot(profile.source_id, src, rule.currency, rule.rate_type, json_cache))
                q = cache[key]
                print(f"{profile.id}: {rule.currency}/PLN {rule.rate_type}: {fmt(q.rate)} ({q.label}; {q.published})")
            except (HTTPError, URLError, TimeoutError, ValueError, KeyError, TypeError, IndexError, OSError, InvalidOperation) as exc:
                log(f"Preview error {profile.id}/{rule.id}: {exc}")
                ok = False
    return ok


def list_nbp() -> None:
    for table in ("a", "b", "c"):
        payload = http_get_json(f"{NBP_API}/exchangerates/tables/{table}/?format=json")
        print(f"\nNBP table {table.upper()}:")
        for row in payload[0]["rates"]:
            print(f"  {row['code']}")


def send_kuma_heartbeat(success: bool, duration_seconds: float) -> None:
    """Report a completed cycle without exposing credentials or affecting alerts."""
    push_url = os.getenv("UPTIME_KUMA_PUSH_URL", "").strip()
    if not push_url:
        return
    try:
        parsed = urlparse(push_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Invalid Push URL")
        parameters = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                      if key not in ("status", "msg", "ping")]
        parameters.extend([
            ("status", "up" if success else "down"),
            ("msg", "All currency checks succeeded" if success else "Currency check failed; see application logs"),
            ("ping", f"{duration_seconds * 1000:.2f}"),
        ])
        url = parsed._replace(query=urlencode(parameters), fragment="").geturl()
        request = Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
        with urlopen(request, timeout=min(request_timeout(), 5)) as response:
            result = json.load(response)
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise ValueError("Push was not accepted")
    except (OSError, ValueError, HTTPException):
        # Exception messages and response bodies may contain the Push token.
        log("WARNING: Uptime Kuma heartbeat could not be delivered. Check its URL and connectivity.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Monitor currency thresholds from multiple sources and send Discord alerts")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--once", action="store_true", help="Check alerts once and exit")
    modes.add_argument("--validate", action="store_true", help="Validate configuration without fetching rates")
    modes.add_argument("--preview", action="store_true", help="Display rates without sending notifications or updating state")
    modes.add_argument("--test", action="store_true", help="Send a test notification to all profiles")
    parser.add_argument("--test-profile", metavar="ID", help="Send a test notification to one profile (requires --test)")
    modes.add_argument("--list-currencies", action="store_true", help="List currency codes supported by NBP")
    args = parser.parse_args(argv)
    try:
        request_timeout()
        interval = environment_integer("CHECK_INTERVAL_SECONDS", 3600, 60, 86400)
        if args.test_profile and not args.test:
            raise ValueError("--test-profile requires --test.")
        if args.list_currencies:
            list_nbp()
            return 0
        cfg = load_config()
        if args.validate:
            print(f"OK: {len(cfg.sources)} sources, {len(cfg.profiles)} profiles, {sum(len(p.alerts) for p in cfg.profiles)} alerts.")
            return 0
        if args.preview:
            return 0 if preview(cfg) else 1
        if args.test:
            profiles = [p for p in cfg.profiles if not args.test_profile or p.id == args.test_profile]
            if not profiles:
                raise ValueError(f"Profile not found {args.test_profile}.")
            for p in profiles:
                webhook = os.getenv(p.webhook_env, "").strip()
                send_to_discord(webhook, f"✅ **Currency monitor test** — profile `{p.id}`, source `{p.source_id}`.")
                log(f"Sent test notification to profile {p.id}.")
            return 0
        with state_lock():
            state = load_state()
            log(f"Start: {len(cfg.profiles)} profiles, {len(cfg.sources)} sources, checking every {interval} seconds.")
            while True:
                cycle_started = time.monotonic()
                # Config reload each time; changes need no rebuilding.
                try:
                    refreshed = load_config()
                    if refreshed != cfg:
                        log(f"Configuration reloaded: {len(refreshed.profiles)} profiles, {len(refreshed.sources)} sources.")
                    cfg = refreshed
                    ok = check_once(cfg, state)
                except (ValueError, OSError, json.JSONDecodeError) as exc:
                    log(f"Configuration error: {exc}")
                    ok = False
                send_kuma_heartbeat(ok, time.monotonic() - cycle_started)
                if args.once:
                    return 0 if ok else 1
                time.sleep(interval)
    except (ValueError, OSError, json.JSONDecodeError, HTTPError, URLError, RuntimeError, TimeoutError) as exc:
        log(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
