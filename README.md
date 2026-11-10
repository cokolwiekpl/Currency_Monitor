# Currency Monitor

Currency Monitor checks exchange rates and sends a Discord message when a rate crosses your chosen threshold.

By default, it checks NBP rates every hour and alerts you when USD rises above **4.00 PLN** or EUR falls below **4.20 PLN**. It runs in the background and remembers previous checks after a restart.

## Run with Portainer

You need Docker, Portainer CE, an x86_64 Linux server, and a Discord webhook.

### 1. Create a Discord webhook

In Discord, open **Server Settings → Integrations → Webhooks → New Webhook**. Choose the channel for alerts and click **Copy Webhook URL**.

### 2. Add a stack

In Portainer, select your Docker environment, then **Stacks → Add stack → Git repository**.

| Field | Value |
| --- | --- |
| Name | `currency-monitor` |
| Repository URL | `https://github.com/cokolwiekpl/Currency_Monitor` |
| Repository reference | `refs/heads/master` |
| Compose path | `compose.yaml` |

### 3. Set the webhook and deploy

Under **Environment variables**, add `DISCORD_WEBHOOK_PRIMARY` and paste your full webhook URL as its value.

Click **Deploy the stack**. The default USD and EUR alerts are ready to use. Open the container's **Logs** to check that rates are being fetched.

### 4. Send a test message

Open the container's **Console**, choose `/bin/ash`, and connect as user `app`. Run this command.

```sh
python /app/main.py --test --test-profile primary
```

A test message should appear in your Discord channel.

## Change currencies or thresholds

Add `ALERTS_CONFIG_JSON` to the stack’s environment variables. Paste this entire example as its value and edit the currencies or thresholds.

```json
{
  "sources": {"nbp": {"type": "nbp"}},
  "profiles": [
    {
      "id": "primary",
      "source": "nbp",
      "webhook_env": "DISCORD_WEBHOOK_PRIMARY",
      "alerts": [
        {
          "id": "usd-above-4",
          "currency": "USD",
          "rate_type": "mid",
          "direction": "above",
          "threshold": "4.00"
        },
        {
          "id": "eur-below-4-20",
          "currency": "EUR",
          "rate_type": "mid",
          "direction": "below",
          "threshold": "4.20"
        }
      ]
    }
  ]
}
```

- `currency` is the currency code.
- `threshold` is its price in PLN.
- `direction` is `above` for a rise or `below` for a fall past the threshold.
- `mid` selects the NBP average rate.

Redeploy the stack to apply your changes. To use the defaults again, remove `ALERTS_CONFIG_JSON` entirely. Leaving it empty causes a configuration error.

Optional `CHECK_INTERVAL_SECONDS` changes the checking interval. Use `1800` for 30 minutes or `3600` for an hour. Allowed values are 60–86400.

## When will an alert arrive?

For a 4.00 PLN threshold, **3.99 → 4.01** triggers an `above` alert. **4.01 → 4.05** and exactly **4.00** do not.

The first check compares the last two NBP publications. If both are already above the threshold, no alert is sent. NBP publishes reference rates on its own schedule.

## Update or stop the application

To update the application, open the stack in Portainer and select **Pull and redeploy**. Enable **Re-pull image** when offered.

Keep the same stack name and its `monitor-state` volume to preserve alert history. Use **Stop this stack** to stop monitoring. Deleting the state volume resets the saved checks and may repeat alerts.

## Run locally with Docker Compose

Download or clone this repository and open a terminal in its directory. Create your environment file.

```sh
cp .env.example .env
```

Set `DISCORD_WEBHOOK_PRIMARY` in `.env` and edit `config/alerts.json` to choose your alerts. Then build and start the application.

```sh
docker compose -f compose.yaml -f compose.dev.yaml up -d --build
docker compose -f compose.yaml -f compose.dev.yaml logs -f currency-monitor
```

This local setup builds the image on your computer. Changes to `config/alerts.json` are read at the next check. After editing `.env`, run the following command.

```sh
docker compose -f compose.yaml -f compose.dev.yaml up -d --force-recreate
```

## Other sources and profiles

See [config/example_multi_source.json](config/example_multi_source.json) for multiple profiles and a custom JSON provider. Set `DISCORD_WEBHOOK_SECONDARY` for the second profile. Replace its placeholder URL with a compatible HTTPS API returning rates in PLN. The first custom-provider reading establishes a starting value for later checks.

## Troubleshooting

- **Invalid Discord webhook URL** — check that `DISCORD_WEBHOOK_PRIMARY` contains the full copied URL, then redeploy.
- **Image pull denied or image not found** — check the image name and tag in your stack settings. If the problem persists, report it to the project maintainer.
- **Invalid configuration** — check your JSON and read the error in the container logs.

From the Portainer container console, these commands check your configuration and display current rates.

```sh
python /app/main.py --validate
python /app/main.py --preview
```
