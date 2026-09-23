#!/usr/bin/env python3
"""Amazon availability monitor.

Checks whether Amazon products are in stock, remembers the last known status
of each one, and sends a notification when an item goes from unavailable to
available.

Usage:
    python amazon_monitor.py check <url>          # one-off check, prints status
    python amazon_monitor.py run                  # check every product in products.json once
    python amazon_monitor.py watch --every 24h    # keep running, checking on an interval
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import smtplib
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from enum import Enum
from pathlib import Path

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("amazon_monitor")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0",
]

ASIN_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d|product)/([A-Z0-9]{10})(?:[/?#]|$)", re.I)

OUT_OF_STOCK_PHRASES = (
    "currently unavailable",
    "out of stock",
    "temporarily out of stock",
    "we don't know when or if this item will be back in stock",
    "not available",
)
IN_STOCK_PHRASES = (
    "in stock",
    "left in stock",
    "usually ships",
    "ships within",
    "available to ship",
)


class Status(str, Enum):
    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    UNKNOWN = "unknown"  # page could not be read (captcha, network error, layout change)


@dataclass
class CheckResult:
    status: Status
    title: str | None = None
    price: str | None = None
    detail: str = ""


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def extract_asin(url: str) -> str | None:
    match = ASIN_RE.search(url)
    return match.group(1).upper() if match else None


def _text(soup: BeautifulSoup, selector: str) -> str | None:
    node = soup.select_one(selector)
    if node is None:
        return None
    text = " ".join(node.get_text(" ", strip=True).split())
    return text or None


def is_captcha_page(html: str) -> bool:
    lowered = html.lower()
    return (
        "/errors/validatecaptcha" in lowered
        or "enter the characters you see below" in lowered
        or "api-services-support@amazon.com" in lowered
    )


def parse_availability(html: str) -> CheckResult:
    """Decide whether a product page shows the item as purchasable."""
    if is_captcha_page(html):
        return CheckResult(Status.UNKNOWN, detail="Amazon returned a captcha / robot check page")

    soup = BeautifulSoup(html, "html.parser")
    title = _text(soup, "#productTitle")
    price = (
        _text(soup, "#corePrice_feature_div .a-offscreen")
        or _text(soup, "#corePriceDisplay_desktop_feature_div .a-offscreen")
        or _text(soup, ".a-price .a-offscreen")
        or _text(soup, "#priceblock_ourprice")
    )
    availability = _text(soup, "#availability") or _text(soup, "#outOfStock") or ""
    avail_lower = availability.lower()

    has_buy_button = bool(
        soup.select_one("#add-to-cart-button") or soup.select_one("#buy-now-button")
    )
    has_out_of_stock_block = bool(soup.select_one("#outOfStock"))

    if any(p in avail_lower for p in OUT_OF_STOCK_PHRASES) or has_out_of_stock_block:
        return CheckResult(Status.OUT_OF_STOCK, title, price, availability or "Out of stock block present")
    if has_buy_button or any(p in avail_lower for p in IN_STOCK_PHRASES):
        return CheckResult(Status.IN_STOCK, title, price, availability or "Add to Cart button present")

    if title is None:
        return CheckResult(Status.UNKNOWN, detail="Page did not look like a product page")
    # A product page with neither a buy button nor a stock message is not purchasable
    # (e.g. "See all buying options" only, or no offers at all).
    return CheckResult(Status.OUT_OF_STOCK, title, price, availability or "No Add to Cart button")


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def fetch_page(url: str, retries: int = 3, timeout: int = 20) -> str | None:
    session = requests.Session()
    for attempt in range(1, retries + 1):
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
        }
        try:
            resp = session.get(url, headers=headers, timeout=timeout)
            if resp.status_code == 200 and not is_captcha_page(resp.text):
                return resp.text
            log.warning("Attempt %d/%d for %s: HTTP %s%s", attempt, retries, url,
                        resp.status_code, " (captcha)" if is_captcha_page(resp.text) else "")
            if resp.status_code == 404:
                return resp.text
            last = resp.text
        except requests.RequestException as exc:
            log.warning("Attempt %d/%d for %s failed: %s", attempt, retries, url, exc)
            last = None
        if attempt < retries:
            time.sleep(2 ** attempt + random.random() * 3)
    return last


def check_url(url: str) -> CheckResult:
    html = fetch_page(url)
    if html is None:
        return CheckResult(Status.UNKNOWN, detail="Could not download the page")
    return parse_availability(html)


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #

def notify(subject: str, body: str, url: str) -> list[str]:
    """Send via every channel configured in the environment. Returns channels used."""
    sent = []
    channels = {
        "ntfy": _notify_ntfy,
        "discord": _notify_discord,
        "slack": _notify_slack,
        "email": _notify_email,
    }
    for name, fn in channels.items():
        try:
            if fn(subject, body, url):
                sent.append(name)
        except Exception as exc:  # one broken channel must not block the others
            log.error("Notification via %s failed: %s", name, exc)
    if not sent:
        log.warning("No notification channel configured; printing instead.")
    print(f"\n*** {subject} ***\n{body}\n{url}\n")
    return sent


def _notify_ntfy(subject: str, body: str, url: str) -> bool:
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return False
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    resp = requests.post(
        f"{server}/{topic}",
        data=body.encode("utf-8"),
        headers={"Title": subject.encode("utf-8"), "Click": url, "Tags": "shopping_cart", "Priority": "high"},
        timeout=15,
    )
    resp.raise_for_status()
    return True


def _notify_discord(subject: str, body: str, url: str) -> bool:
    hook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not hook:
        return False
    requests.post(hook, json={"content": f"**{subject}**\n{body}\n{url}"}, timeout=15).raise_for_status()
    return True


def _notify_slack(subject: str, body: str, url: str) -> bool:
    hook = os.environ.get("SLACK_WEBHOOK_URL")
    if not hook:
        return False
    requests.post(hook, json={"text": f"*{subject}*\n{body}\n{url}"}, timeout=15).raise_for_status()
    return True


def _notify_email(subject: str, body: str, url: str) -> bool:
    host, to_addr = os.environ.get("SMTP_HOST"), os.environ.get("EMAIL_TO")
    if not host or not to_addr:
        return False
    user = os.environ.get("SMTP_USER")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("EMAIL_FROM") or user or to_addr
    msg["To"] = to_addr
    msg.set_content(f"{body}\n\n{url}")
    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=30) as smtp:
        smtp.starttls()
        if user:
            smtp.login(user, os.environ.get("SMTP_PASSWORD", ""))
        smtp.send_message(msg)
    return True


# --------------------------------------------------------------------------- #
# Monitoring loop
# --------------------------------------------------------------------------- #

def load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    tmp.replace(path)


def load_products(path: Path) -> list[dict]:
    products = load_json(path, None)
    if products is None:
        sys.exit(f"Products file not found: {path}")
    if isinstance(products, dict):
        products = products.get("products", [])
    for p in products:
        if "url" not in p:
            sys.exit(f"Every product in {path} needs a 'url': {p}")
    return products


def run_once(products_file: Path, state_file: Path, check=check_url, send=notify) -> dict:
    """Check all products once. Notify on any out-of-stock -> in-stock transition."""
    products = load_products(products_file)
    state = load_json(state_file, {})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for i, product in enumerate(products):
        url = product["url"]
        key = extract_asin(url) or url
        previous = state.get(key, {})
        prev_status = previous.get("status")

        if i:
            time.sleep(random.uniform(3, 8))  # be polite between products
        result = check(url)
        name = product.get("name") or result.title or previous.get("title") or key
        log.info("%s -> %s (%s)", name, result.status.value, result.detail)

        entry = dict(previous)
        entry.update(url=url, last_checked=now, last_detail=result.detail)
        if result.title:
            entry["title"] = result.title
        if result.price:
            entry["price"] = result.price

        if result.status is Status.UNKNOWN:
            # Keep the last good status so a captcha doesn't cause a false alert later.
            entry["consecutive_failures"] = previous.get("consecutive_failures", 0) + 1
            state[key] = entry
            continue

        entry["consecutive_failures"] = 0
        entry["status"] = result.status.value
        if result.status.value != prev_status:
            entry["status_since"] = now

        if result.status is Status.IN_STOCK and prev_status != Status.IN_STOCK.value:
            price = f" at {result.price}" if result.price else ""
            send(
                f"Back in stock: {name[:80]}",
                f"{name} is now available on Amazon{price}.\n{result.detail}",
                url,
            )
            entry["last_notified"] = now
        state[key] = entry

    save_json(state_file, state)
    return state


def parse_interval(text: str) -> int:
    match = re.fullmatch(r"(\d+)\s*([smhd]?)", text.strip().lower())
    if not match:
        raise argparse.ArgumentTypeError(f"Invalid interval: {text!r} (use e.g. 30m, 12h, 1d)")
    value, unit = int(match.group(1)), match.group(2) or "s"
    return value * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Monitor Amazon products and get notified when they are back in stock.")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="Check a single product URL once and print the result")
    p_check.add_argument("url")

    for name, help_text in (("run", "Check all products once (use from cron / GitHub Actions)"),
                            ("watch", "Keep running and check all products on an interval")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--products", type=Path, default=Path("products.json"))
        p.add_argument("--state", type=Path, default=Path("state.json"))
        if name == "watch":
            p.add_argument("--every", type=parse_interval, default=parse_interval("24h"),
                           help="Interval between checks, e.g. 12h or 1d (default: 24h)")

    sub.add_parser("test-notify", help="Send a test notification through configured channels")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if args.command == "check":
        result = check_url(args.url)
        print(json.dumps({"status": result.status.value, "title": result.title,
                          "price": result.price, "detail": result.detail}, indent=2))
        return 0 if result.status is not Status.UNKNOWN else 2

    if args.command == "test-notify":
        sent = notify("Amazon monitor test", "Notifications are working.", "https://www.amazon.com")
        print("Sent via:", ", ".join(sent) or "(none configured)")
        return 0 if sent else 1

    if args.command == "run":
        run_once(args.products, args.state)
        return 0

    while True:  # watch
        try:
            run_once(args.products, args.state)
        except Exception:
            log.exception("Check run failed; will retry next interval")
        log.info("Next check in %s seconds", args.every)
        time.sleep(args.every)


if __name__ == "__main__":
    sys.exit(main())
