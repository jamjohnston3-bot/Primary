import json

import pytest

import amazon_monitor as am


def page(body: str) -> str:
    return f"<html><body><span id='productTitle'> Widget Pro </span>{body}</body></html>"


IN_STOCK = page("""
  <div id="corePrice_feature_div"><span class="a-offscreen">$19.99</span></div>
  <div id="availability"><span>In Stock</span></div>
  <input id="add-to-cart-button" type="submit">
""")
OUT_OF_STOCK = page("""
  <div id="outOfStock"><span>Currently unavailable.</span>
  <span>We don't know when or if this item will be back in stock.</span></div>
""")
CAPTCHA = "<html><form action='/errors/validateCaptcha'>Enter the characters you see below</form></html>"


@pytest.mark.parametrize("url, asin", [
    ("https://www.amazon.com/dp/B0ABCDEF12", "B0ABCDEF12"),
    ("https://www.amazon.com/Some-Name/dp/B0ABCDEF12/ref=sr_1_1?keywords=x", "B0ABCDEF12"),
    ("https://www.amazon.co.uk/gp/product/b0abcdef12?th=1", "B0ABCDEF12"),
    ("https://www.amazon.com/s?k=widget", None),
])
def test_extract_asin(url, asin):
    assert am.extract_asin(url) == asin


def test_in_stock_page():
    result = am.parse_availability(IN_STOCK)
    assert result.status is am.Status.IN_STOCK
    assert result.title == "Widget Pro"
    assert result.price == "$19.99"


def test_low_stock_message_counts_as_in_stock():
    html = page("<div id='availability'>Only 3 left in stock - order soon.</div>")
    assert am.parse_availability(html).status is am.Status.IN_STOCK


def test_out_of_stock_page():
    assert am.parse_availability(OUT_OF_STOCK).status is am.Status.OUT_OF_STOCK


def test_temporarily_out_of_stock_beats_buy_button():
    html = page("<div id='availability'>Temporarily out of stock.</div><input id='add-to-cart-button'>")
    assert am.parse_availability(html).status is am.Status.OUT_OF_STOCK


def test_no_buy_button_means_out_of_stock():
    html = page("<a id='buybox-see-all-buying-choices'>See All Buying Options</a>")
    assert am.parse_availability(html).status is am.Status.OUT_OF_STOCK


def test_captcha_is_unknown():
    assert am.parse_availability(CAPTCHA).status is am.Status.UNKNOWN


def test_non_product_page_is_unknown():
    assert am.parse_availability("<html><body>Hello</body></html>").status is am.Status.UNKNOWN


@pytest.mark.parametrize("text, seconds", [("30m", 1800), ("12h", 43200), ("1d", 86400), ("90", 90)])
def test_parse_interval(text, seconds):
    assert am.parse_interval(text) == seconds


@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(am.time, "sleep", lambda s: None)
    products = tmp_path / "products.json"
    products.write_text(json.dumps([{"name": "Widget", "url": "https://www.amazon.com/dp/B0ABCDEF12"}]))
    return products, tmp_path / "state.json"


def run_sequence(files, statuses):
    """Run the monitor once per status and return the list of notifications sent."""
    products, state = files
    sent = []
    for status in statuses:
        am.run_once(products, state,
                    check=lambda url, s=status: am.CheckResult(s, "Widget", "$5", s.value),
                    send=lambda *args: sent.append(args))
    return sent


def test_notifies_only_on_transition_to_in_stock(files):
    S = am.Status
    sent = run_sequence(files, [S.OUT_OF_STOCK, S.OUT_OF_STOCK, S.IN_STOCK, S.IN_STOCK])
    assert len(sent) == 1
    assert "Back in stock" in sent[0][0]
    state = json.loads(files[1].read_text())
    assert state["B0ABCDEF12"]["status"] == "in_stock"


def test_notifies_again_after_going_out_and_back(files):
    S = am.Status
    sent = run_sequence(files, [S.IN_STOCK, S.OUT_OF_STOCK, S.IN_STOCK])
    assert len(sent) == 2


def test_unknown_result_keeps_previous_status(files):
    S = am.Status
    sent = run_sequence(files, [S.IN_STOCK, S.UNKNOWN, S.IN_STOCK])
    assert len(sent) == 1  # the captcha in the middle must not trigger a second alert
    state = json.loads(files[1].read_text())
    assert state["B0ABCDEF12"]["consecutive_failures"] == 0


def test_notify_uses_ntfy_when_configured(monkeypatch):
    calls = []

    class Resp:
        def raise_for_status(self):
            pass

    monkeypatch.setenv("NTFY_TOPIC", "my-topic")
    for var in ("DISCORD_WEBHOOK_URL", "SLACK_WEBHOOK_URL", "SMTP_HOST", "EMAIL_TO"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(am.requests, "post", lambda url, **kw: calls.append(url) or Resp())
    assert am.notify("subj", "body", "https://amazon.com/dp/X") == ["ntfy"]
    assert calls == ["https://ntfy.sh/my-topic"]
