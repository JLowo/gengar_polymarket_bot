"""Market discovery for Polymarket 5-minute BTC Up/Down markets.

The market slug is deterministic: btc-updown-5m-{window_ts}
where window_ts = now - (now % 300), i.e. the start of the current 5-min window.
"""

import time
import json
import urllib.request
from dataclasses import dataclass
from typing import Optional


GAMMA_API = "https://gamma-api.polymarket.com"
PERIOD_SECONDS = {5: 300, 15: 900}


@dataclass
class MarketWindow:
    slug: str
    condition_id: str
    token_id_up: str
    token_id_down: str
    window_start: int
    window_end: int
    opening_price: Optional[float] = None
    up_price: float = 0.50
    down_price: float = 0.50

    @property
    def seconds_remaining(self) -> float:
        return max(0, self.window_end - time.time())


def current_window_ts(period_minutes: int = 5) -> int:
    """Calculate the Unix timestamp for the start of the current window."""
    period = PERIOD_SECONDS[period_minutes]
    now = int(time.time())
    return now - (now % period)


def next_window_ts(period_minutes: int = 5) -> int:
    """Calculate when the next window opens."""
    period = PERIOD_SECONDS[period_minutes]
    return current_window_ts(period_minutes) + period


def market_slug(period_minutes: int = 5, window_ts: int = None) -> str:
    """Generate the deterministic market slug."""
    ts = window_ts or current_window_ts(period_minutes)
    return f"btc-updown-{period_minutes}m-{ts}"


def fetch_market_by_slug(slug: str) -> Optional[dict]:
    """Fetch market data from Gamma API by slug."""
    try:
        url = f"{GAMMA_API}/events?slug={slug}"
        req = urllib.request.Request(url, headers={"User-Agent": "PolyBot/1.0"})
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read().decode())
        if data and len(data) > 0:
            return data[0]
        return None
    except Exception as e:
        print(f"[market] Failed to fetch {slug}: {e}")
        return None


def extract_token_ids(event_data: dict) -> tuple[str, str]:
    """Extract UP and DOWN token IDs from event data.
    
    Returns (token_id_up, token_id_down).
    
    Polymarket BTC 5-min markets have a single market with:
      outcomes: ["Up", "Down"]
      clobTokenIds: ["<up_token>", "<down_token>"]  (JSON string)
    """
    markets = event_data.get("markets", [])
    if len(markets) < 1:
        raise ValueError("No markets found in event data")
    
    market = markets[0]
    
    # clobTokenIds comes as a JSON string — parse it
    clob_tokens = market.get("clobTokenIds", [])
    if isinstance(clob_tokens, str):
        clob_tokens = json.loads(clob_tokens)
    
    if len(clob_tokens) < 2:
        raise ValueError(f"Expected 2 token IDs, got {len(clob_tokens)}")
    
    # outcomes: ["Up", "Down"] — tokens are in same order
    outcomes = market.get("outcomes", "")
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    
    # Map tokens to Up/Down based on outcomes order
    token_up = None
    token_down = None
    
    for i, outcome in enumerate(outcomes):
        if outcome.lower() == "up":
            token_up = clob_tokens[i]
        elif outcome.lower() == "down":
            token_down = clob_tokens[i]
    
    # Fallback: assume index 0 = Up, index 1 = Down
    if token_up is None:
        token_up = clob_tokens[0]
    if token_down is None:
        token_down = clob_tokens[1]
    
    return token_up, token_down


def get_current_market(period_minutes: int = 5) -> Optional[MarketWindow]:
    """Get the current active 5-min BTC market with all required info."""
    wts = current_window_ts(period_minutes)
    slug = market_slug(period_minutes, wts)
    period = PERIOD_SECONDS[period_minutes]
    
    event = fetch_market_by_slug(slug)
    if not event:
        # Try the next window (market might already be created for upcoming)
        return None
    
    try:
        token_up, token_down = extract_token_ids(event)
    except ValueError as e:
        print(f"[market] {e}")
        return None
    
    condition_id = event.get("markets", [{}])[0].get("conditionId", "")
    
    # Parse outcome prices if available
    market_data = event.get("markets", [{}])[0]
    outcome_prices = market_data.get("outcomePrices", "")
    if isinstance(outcome_prices, str) and outcome_prices:
        outcome_prices = json.loads(outcome_prices)
    
    outcomes = market_data.get("outcomes", "")
    if isinstance(outcomes, str) and outcomes:
        outcomes = json.loads(outcomes)
    
    up_price = 0.50
    down_price = 0.50
    if isinstance(outcomes, list) and isinstance(outcome_prices, list):
        for i, outcome in enumerate(outcomes):
            if i < len(outcome_prices):
                if outcome.lower() == "up":
                    up_price = float(outcome_prices[i])
                elif outcome.lower() == "down":
                    down_price = float(outcome_prices[i])
    
    return MarketWindow(
        slug=slug,
        condition_id=condition_id,
        token_id_up=token_up,
        token_id_down=token_down,
        window_start=wts,
        window_end=wts + period,
        up_price=up_price,
        down_price=down_price,
    )


CLOB_API = "https://clob.polymarket.com"


def get_book_price(token_id: str, side: str, amount: float) -> float:
    """Price to BUY `amount` USD of a token, or to SELL `amount` shares of it,
    from Polymarket's public order book (read-only, no wallet needed).

    Walks the book from the best level until `amount` is covered, the same
    rule py-clob-client's calculate_market_price() uses, so dry run sees the
    price live mode would. If the book is too thin it returns the worst level
    (as the SDK does for non-FOK orders). Returns 0.0 if there is no book.
    """
    try:
        url = f"{CLOB_API}/book?token_id={token_id}"
        req = urllib.request.Request(url, headers={"User-Agent": "PolyBot/1.0"})
        book = json.loads(urllib.request.urlopen(req, timeout=5).read().decode())
    except Exception:
        return 0.0
    try:
        if side == "BUY":
            levels = sorted(book.get("asks") or [], key=lambda lv: float(lv["price"]))
            covered = lambda lv: float(lv["size"]) * float(lv["price"])   # USD
        else:
            levels = sorted(book.get("bids") or [], key=lambda lv: -float(lv["price"]))
            covered = lambda lv: float(lv["size"])                         # shares
        total = 0.0
        for lv in levels:
            total += covered(lv)
            if total >= amount:
                return float(lv["price"])
        return float(levels[-1]["price"]) if levels else 0.0
    except (KeyError, ValueError, TypeError):
        return 0.0


def get_resolution_payouts(window_ts: int, period_minutes: int = 5) -> Optional[dict]:
    """Payout per share for each side of a finished window, per Polymarket.

    Returns e.g. {"UP": 1.0, "DOWN": 0.0} once Gamma shows the market closed
    with umaResolutionStatus "resolved" (usually ~20s after the window ends,
    sometimes minutes later). A split or cancelled market comes back as its
    real payouts (e.g. 0.5 / 0.5). Returns None if it isn't resolved yet or
    Gamma can't be reached; callers should simply ask again later.

    This is the ground truth the market pays out on (Chainlink BTC/USD),
    unlike Binance prices or balance changes.
    """
    slug = market_slug(period_minutes, window_ts)
    try:
        url = f"{GAMMA_API}/markets/slug/{slug}"
        req = urllib.request.Request(url, headers={"User-Agent": "PolyBot/1.0"})
        m = json.loads(urllib.request.urlopen(req, timeout=5).read().decode())
    except Exception:
        return None
    if not m.get("closed") or m.get("umaResolutionStatus") != "resolved":
        return None
    try:
        outcomes = m.get("outcomes", "[]")
        prices = m.get("outcomePrices", "[]")
        outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
        prices = json.loads(prices) if isinstance(prices, str) else prices
        payouts = {o.upper(): float(p) for o, p in zip(outcomes, prices)
                   if o.lower() in ("up", "down")}
    except (ValueError, TypeError, AttributeError):
        return None
    return payouts if set(payouts) == {"UP", "DOWN"} else None


if __name__ == "__main__":
    # Quick test
    print(f"Current window ts: {current_window_ts()}")
    print(f"Market slug: {market_slug()}")
    print(f"Next window in: {next_window_ts() - time.time():.0f}s")
    
    market = get_current_market()
    if market:
        print(f"\nActive market: {market.slug}")
        print(f"Token UP:   {market.token_id_up[:20]}...")
        print(f"Token DOWN: {market.token_id_down[:20]}...")
        print(f"Closes in:  {market.seconds_remaining:.0f}s")
    else:
        print("\nNo active market found (may be between windows)")
