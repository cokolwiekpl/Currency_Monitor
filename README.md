# Currency Monitor

A small Python application that monitors exchange rates and sends Discord alerts when a rate crosses a configured threshold. Run multiple independent profiles in one Docker Compose service on Ubuntu.

## Features

- NBP reference rates and configurable HTTPS JSON providers.
- Multiple profiles, sources, currencies, and thresholds.
- Mid, buy, and sell rates where available.
- Separate Discord webhooks per profile.
- Persistent checkpoints, retry after delivery failures, and NBP history catch-up.
- Offline configuration validation, rate preview, and test notifications.
- Python standard library only, with no runtime packages to install.

## How it works

Each cycle reloads the configuration, fetches rates, checks thresholds, and saves progress after each successful check. Requests are cached within the cycle so profiles sharing a source can reuse fetched data.

An `above` rule triggers when the previous rate is at or below the threshold and the new rate is strictly above it. A `below` rule triggers in the opposite direction. Staying beyond the threshold produces no further notifications. Returning to the threshold and crossing again can trigger another alert.

For NBP, the first check compares the latest two published quotes. Later checks process new quotes from the latest 30 publications. For a JSON provider, the first snapshot establishes a baseline. Later snapshots are compared with the saved rate.

## Requirements

- Ubuntu with Docker Engine and the Docker Compose plugin for deployment.
- Outbound HTTPS access to the selected providers and Discord.
- A Discord webhook for each enabled profile. Profiles may share a webhook.
- Python 3.12 or newer for local execution and tests. The Docker image uses Python 3.12.

## Quick start with Docker Compose

From the repository directory, create your local environment file and edit the example alerts.

```bash
cp .env.example .env
chmod 600 .env
nano .env
nano config/alerts.json
docker compose build
docker compose run --rm currency-monitor python main.py --validate
docker compose up -d
docker compose logs -f currency-monitor
```

Set `DISCORD_WEBHOOK_PRIMARY` in `.env` to your webhook URL. The default configuration uses NBP for USD and EUR, with illustrative thresholds that you should adjust.

The service runs as an unprivileged user, exposes no ports, mounts configuration read-only, and stores state in the `monitor-state` named volume. The container filesystem is read-only except for that volume.

## Configuration

`config/alerts.json` is the ready-to-edit NBP configuration. `config/example_multi_source.json` demonstrates NBP and a second JSON provider. Its `example.com` URL is fictional and must be replaced with a compatible API before use.

```json
{
  "sources": {"nbp": {"type": "nbp"}},
  "profiles": [
    {
      "id": "primary",
      "source": "nbp",
      "webhook_env": "DISCORD_WEBHOOK_PRIMARY",
      "alerts": [
        {"id": "usd-above-4", "currency": "USD", "rate_type": "mid", "direction": "above", "threshold": "4.00"},
        {"id": "eur-below-4-20", "currency": "EUR", "rate_type": "mid", "direction": "below", "threshold": "4.20"}
      ]
    }
  ]
}
```

| Environment variable | Default or purpose |
| --- | --- |
| `DISCORD_WEBHOOK_PRIMARY` | Required webhook for the default profile |
| `DISCORD_WEBHOOK_SECONDARY` | Webhook for the second example profile |
| `CHECK_INTERVAL_SECONDS` | `3600`, allowed range 60–86400 |
| `REQUEST_TIMEOUT_SECONDS` | `20`, allowed range 1–120 per HTTP request |
| `BANK_API_TOKEN` | Optional provider credential referenced through `headers_env` |
| `CONFIG_PATH` | Local default `config/alerts.json`, set to `/app/config/alerts.json` by Compose |
| `STATE_PATH` | Local default `data/state.json`, set to `/data/state.json` by Compose |

Put secrets in environment variables. Compose passes `.env` into the container. Direct Python execution reads the process environment and does not load `.env` automatically. Additional profiles and authenticated providers may reference additional variable names, which you must add to `.env`.

Configuration changes take effect at the next cycle. After changing `.env`, recreate the service.

```bash
docker compose up -d --force-recreate
```

## Exchange rate providers

### NBP

Use `{"type": "nbp"}`. The application queries table A, then table B if the currency is unavailable in A, for `mid`. It queries table C for `buy` (`bid`) and `sell` (`ask`). The API provides current and historical publications. [NBP API documentation](https://api.nbp.pl/en.html)

NBP rates are published reference rates, rather than continuously updated executable offers. Table C values do not promise the price available at a commercial bank. Polling more frequently does not make the underlying publications more frequent.

### Custom JSON API

A provider must expose an accessible HTTPS endpoint with compatible JSON. Arbitrary banks are not supported automatically. HTML pages, XML feeds, and account-specific offers require a separate integration.

For a response shaped like this example,

```json
{
  "data": {
    "updated_at": "2026-11-10T15:30:00+01:00",
    "rates": [{"currency": "EUR", "buy": "4.16", "sell": "4.28"}]
  }
}
```

configure the following source.

```json
{
  "type": "json_api",
  "label": "Example bank",
  "url": "https://example.com/api/rates",
  "rates_path": "data.rates",
  "currency_path": "currency",
  "timestamp_path": "data.updated_at",
  "price_paths": {"buy": "buy", "sell": "sell"},
  "headers_env": {"Authorization": "BANK_API_TOKEN"}
}
```

The URL above is illustrative and supplies no usable rates. Remove `headers_env` for a public API. If required by your provider, set the environment variable to the complete header value, such as `Bearer YOUR_API_TOKEN`.

- `rates_path` locates a list of quotes or one quote object in the response.
- `currency_path` locates the currency code inside each quote. Exactly one matching quote is required.
- `price_paths` maps supported rate types to fields inside each quote.
- `timestamp_path` optionally locates the publication timestamp in the response. Omit it if unavailable. Messages then explicitly display the check time.
- Paths support dot-separated object fields and numeric array indexes, such as `data.rates.0`.
- The URL may contain `{currency}`, replaced with the uppercase currency code.

Rates must represent PLN per one unit of the selected currency. Convert providers using another base currency or quotation unit before supplying their data. `buy` means the provider buys foreign currency from the customer. `sell` means the provider sells it to the customer.

## Profiles and alert rules

Each profile has a unique `id`, one `source`, a `webhook_env`, and a nonempty `alerts` list. Set `enabled` to `false` to skip a profile. At least one profile must remain enabled.

IDs contain 1–60 letters, digits, underscores, or hyphens. Alert IDs must be unique within a profile. Currency codes contain three letters and are normalized to uppercase. Rates and thresholds must be positive finite numbers. Quoted decimal thresholds help preserve exact values. `rate_type` defaults to `mid` and must be available from the source. `direction` is required and accepts `above` or `below`.

State is isolated by profile ID and alert ID. Changing an alert's currency, source ID, rate type, direction, or threshold establishes a new checkpoint. Use a new source ID if you replace a provider's endpoint or mapping, so its rates are not compared with the previous provider's saved value.

To try multiple sources, adapt `config/example_multi_source.json`, copy it to `config/alerts.json`, and configure both webhooks.

## Discord webhook setup

In your Discord server, open the target channel's settings, find Integrations and Webhooks, create a webhook, and copy its URL into the appropriate `.env` variable. Access depends on your server permissions.

Send a test notification explicitly when ready.

```bash
docker compose run --rm currency-monitor python main.py --test --test-profile primary
```

`--test` without `--test-profile` sends to all enabled profiles. Test mode leaves alert checkpoints untouched. Notification payloads disable mentions.

## Running and managing the application

```bash
# Validate configuration and numeric environment settings without network access.
docker compose run --rm currency-monitor python main.py --validate

# Fetch rates without notifications or state changes.
docker compose run --rm currency-monitor python main.py --preview

# List supported NBP currency codes, grouped by table.
docker compose run --rm currency-monitor python main.py --list-currencies

# Stop the periodic monitor before a one-time check that updates state.
docker compose stop currency-monitor
docker compose run --rm currency-monitor python main.py --once
docker compose up -d

# Inspect status and logs.
docker compose ps
docker compose logs --tail=100 currency-monitor

# Stop and remove the service while retaining its state volume.
docker compose down
```

A process lock rejects concurrent monitors using the same state file. `docker compose down -v` deletes the volume and resets all checkpoints. Back up the volume while the service is stopped if you need to preserve progress.

Local execution uses the same commands after replacing the Compose prefix with your Python executable. Export the required webhook variables first. Paths are relative to the current directory unless absolute paths are supplied.

```bash
python main.py --validate
python main.py --preview
python main.py
```

The code is organized into `main.py` for configuration, providers, notifications, and orchestration, `models.py` for shared data models, and `storage.py` for atomic persistence and process locking. `tests/` uses the standard-library `unittest` runner.

## Testing and troubleshooting

```bash
python -m unittest discover -s tests -v
docker compose config --quiet
```

Automated tests use mocked HTTP and notification calls and temporary state files. They require no provider credentials and send no real Discord notifications.

`--validate` checks JSON configuration and numeric timing settings. It does not check webhook credentials, authentication headers, provider availability, or live response structure. Use `--preview` to inspect provider data, then an explicit `--test` to verify Discord delivery.

Exit code `0` indicates success, `1` indicates failed checks or preview requests, and `2` indicates a startup, configuration, command-line, or test-notification failure. In periodic mode, individual failed checks are logged and retried on later cycles. Other profiles continue processing.

For missing environment variables, update `.env` and recreate the container. For provider errors, verify the endpoint and JSON mapping against its actual response. For state errors, inspect or restore the state file while the service is stopped. Deleting state resets deduplication and can repeat an initial NBP alert. Logs use UTC timestamps.

## Limitations

- Currency pairs are quoted against PLN. Source configuration must be maintained by the operator.
- JSON snapshots can miss crossings between polls. NBP catch-up is limited to the latest 30 quotes.
- Delivery and local persistence cannot form one transaction. A Discord acceptance followed by a connection failure or state-write failure can produce a duplicate on retry.
- Retries occur on subsequent polling cycles. There is no provider-specific rate-limit scheduling.
- Profiles are configuration groups, without user accounts or a web interface.
- The monitor requires one running process per state file on a filesystem supporting local process locks and atomic file replacement.
