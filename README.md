# Amazon Availability Monitor

Checks whether Amazon products are in stock, re-checks them every day, and
notifies you when an item that was unavailable becomes available again.

- Detects **in stock** (Add to Cart button, "In Stock", "Only 3 left in stock")
  vs. **out of stock** ("Currently unavailable", "Temporarily out of stock", no buy box).
- Remembers the last status in `state.json` and alerts **only on the
  out-of-stock → in-stock transition**, so you are not spammed daily.
- If Amazon shows a captcha or the page cannot be loaded, the result is
  `unknown` and the previous status is kept. A blocked check can't cause a
  false alert.
- Notifications: [ntfy](https://ntfy.sh) phone push, Discord, Slack and/or
  email. Use any combination.

## Quick start

```bash
pip install -r requirements.txt

# One-off check of a single item
python amazon_monitor.py check "https://www.amazon.com/dp/B08N5WRWNW"
```

Output:

```json
{
  "status": "out_of_stock",
  "title": "…",
  "price": "$49.99",
  "detail": "Currently unavailable."
}
```

## Monitoring items

1. Put the items you want to watch in `products.json`:

   ```json
   [
     {"name": "Echo Dot", "url": "https://www.amazon.com/dp/B08N5WRWNW"},
     {"name": "Some other thing", "url": "https://www.amazon.co.uk/dp/B0XXXXXXXX"}
   ]
   ```

2. Configure at least one notification channel (environment variables):

   | Channel | Variables |
   |---|---|
   | ntfy (easiest: install the ntfy app and subscribe to a hard-to-guess topic name) | `NTFY_TOPIC`, optional `NTFY_SERVER` |
   | Discord | `DISCORD_WEBHOOK_URL` |
   | Slack | `SLACK_WEBHOOK_URL` |
   | Email (e.g. Gmail with an [app password](https://support.google.com/accounts/answer/185833)) | `SMTP_HOST`, `SMTP_PORT` (587), `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_TO`, optional `EMAIL_FROM` |

   Verify it with `python amazon_monitor.py test-notify`.

3. Pick how to run it daily:

### Option A: GitHub Actions (no computer needs to stay on)

`.github/workflows/monitor.yml` runs every day at 14:00 UTC. It checks each
product and commits the updated `state.json` back to the repository.

1. Push this repo to GitHub.
2. Under **Settings → Secrets and variables → Actions**, add the secrets for
   your notification channel (e.g. `NTFY_TOPIC`).
3. Under **Settings → Actions → General → Workflow permissions**, allow
   *Read and write permissions* so the job can save `state.json`.
4. Optionally trigger it now from the **Actions** tab ("Run workflow").

To change the time, edit the `cron` line (it is in UTC).

### Option B: Your own computer or server

Keep it running in the foreground:

```bash
python amazon_monitor.py watch --every 24h
```

Or schedule a single run with cron (Linux/macOS), e.g. every day at 9:00:

```cron
0 9 * * * cd /path/to/repo && NTFY_TOPIC=my-topic /usr/bin/python3 amazon_monitor.py run >> monitor.log 2>&1
```

On Windows, use Task Scheduler to run `python amazon_monitor.py run` daily.

## Limitations

Amazon has no public stock API and actively discourages scraping. It
sometimes returns a captcha page, especially to cloud and datacenter IPs such
as GitHub Actions runners. The monitor retries with backoff, reports `unknown`
when it is blocked, and tries again the next day. If blocks happen often, run
it from a home connection (Option B). Keep the check frequency low (daily is
fine) and review Amazon's Conditions of Use for your region.

Amazon's page layout changes occasionally. If detection stops working,
the selectors in `parse_availability()` are the place to update.

## Development

```bash
pip install -r requirements-dev.txt
pytest -q
```
