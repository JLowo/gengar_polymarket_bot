"""Order executor for Polymarket CLOB.

v6 — Execution improvements:
  - FOK first, GTC fallback with 10s timeout
  - Detailed pre-order diagnostics (shares, liquidity, spread)
  - Minimum 5 shares, clean decimals
  - sell_shares() for pre-close exit
  - claim_winnings() for post-resolution fallback
"""

import math
import time
from dataclasses import dataclass
from typing import Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    OrderArgs,
    OrderType,
    BalanceAllowanceParams,
    AssetType,
)
from py_clob_client.constants import POLYGON


# ── Constants ───────────────────────────────────────────────────────

FILLED = "FILLED"
REJECTED = "REJECTED"
CANCELLED = "CANCELLED"
FAILED = "FAILED"

MIN_SHARES = 5.0
CLAIM_SELL_PRICE = 0.99
GTC_TIMEOUT = 10.0       # Seconds to wait for GTC fill before cancelling
GTC_POLL_INTERVAL = 2.0  # Poll every 2s during GTC wait


@dataclass
class OrderResult:
    success: bool
    order_id: str = ""
    status: str = FAILED
    side: str = ""
    price: float = 0.0
    size_requested: float = 0.0
    size_filled: float = 0.0
    amount_spent: float = 0.0
    token_id: str = ""
    error: str = ""
    dry_run: bool = True


@dataclass
class BookSnapshot:
    """Pre-order diagnostics about the order book."""
    best_bid: float = 0.0
    best_ask: float = 0.0
    spread: float = 0.0
    liquidity_at_price: float = 0.0   # Shares available at our price
    total_ask_depth: float = 0.0      # Total shares on ask side
    total_bid_depth: float = 0.0      # Total shares on bid side


# ── Decimal-safe share sizing ───────────────────────────────────────

def calculate_order_size(price: float, max_usd: float) -> tuple[float, float]:
    """Largest whole-number share count where shares × price ≤ max_usd
    and has at most 2 decimal places."""
    if price <= 0 or max_usd <= 0:
        return 0.0, 0.0

    price_cents = round(price * 100)
    max_usd_cents = int(max_usd * 100)
    max_shares = max_usd_cents // price_cents if price_cents > 0 else 0

    if max_shares < MIN_SHARES:
        min_cost_cents = int(MIN_SHARES) * price_cents
        if min_cost_cents <= max_usd_cents:
            max_shares = int(MIN_SHARES)
        else:
            return 0.0, 0.0

    shares = int(max_shares)
    spend = shares * price_cents / 100.0
    if shares < MIN_SHARES:
        return 0.0, 0.0
    return float(shares), spend


class Executor:
    def __init__(
        self,
        private_key: str,
        safe_address: str = "",
        dry_run: bool = True,
    ):
        self.dry_run = dry_run
        self.private_key = private_key
        self.safe_address = safe_address
        self.client: Optional[ClobClient] = None
        self._initialized = False

    def initialize(self) -> bool:
        try:
            self.client = ClobClient(
                host="https://clob.polymarket.com",
                key=self.private_key,
                chain_id=POLYGON,
                funder=self.safe_address if self.safe_address else None,
                signature_type=2 if self.safe_address else 0,
            )
            self.client.set_api_creds(self.client.create_or_derive_api_creds())
            self._initialized = True
            print(f"[executor] Initialized ({'DRY RUN' if self.dry_run else 'LIVE'})")
            print(f"[executor] Address: {self.client.get_address()}")
            return True
        except Exception as e:
            print(f"[executor] Init failed: {e}")
            return False

    def get_balance(self) -> float:
        if not self._initialized:
            return 0.0
        try:
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            bal = self.client.get_balance_allowance(params)
            return float(bal.get("balance", 0)) / 1e6
        except Exception as e:
            print(f"[executor] Balance check failed: {e}")
            return 0.0

    # ── Order book reading ──────────────────────────────────────────

    def _read_book(self, token_id: str) -> tuple[list, list]:
        """Read order book → (asks, bids) as lists of (price, size)."""
        if not self._initialized:
            return [], []
        try:
            book = self.client.get_order_book(token_id)
            if hasattr(book, 'asks'):
                raw_asks = book.asks or []
                raw_bids = book.bids or []
            else:
                raw_asks = book.get("asks", [])
                raw_bids = book.get("bids", [])

            asks, bids = [], []
            for level in raw_asks:
                p = float(level.price if hasattr(level, 'price') else level.get("price", 0))
                s = float(level.size if hasattr(level, 'size') else level.get("size", 0))
                asks.append((p, s))
            for level in raw_bids:
                p = float(level.price if hasattr(level, 'price') else level.get("price", 0))
                s = float(level.size if hasattr(level, 'size') else level.get("size", 0))
                bids.append((p, s))
            return asks, bids
        except Exception as e:
            print(f"[executor] Book read failed: {e}")
            return [], []

    def get_book_snapshot(self, token_id: str, our_price: float, side: str = "BUY") -> BookSnapshot:
        """Get a diagnostic snapshot of the order book."""
        asks, bids = self._read_book(token_id)
        snap = BookSnapshot()

        if bids:
            snap.best_bid = bids[0][0]
            snap.total_bid_depth = sum(s for _, s in bids)
        if asks:
            snap.best_ask = asks[0][0]
            snap.total_ask_depth = sum(s for _, s in asks)

        if snap.best_bid > 0 and snap.best_ask > 0:
            snap.spread = snap.best_ask - snap.best_bid

        # Liquidity available at our target price
        if side.upper() == "BUY":
            snap.liquidity_at_price = sum(s for p, s in asks if p <= our_price)
        else:
            snap.liquidity_at_price = sum(s for p, s in bids if p >= our_price)

        return snap

    def get_best_bid(self, token_id: str) -> float:
        _, bids = self._read_book(token_id)
        return bids[0][0] if bids else 0.0

    # ── Place order: FOK first, GTC fallback ────────────────────────

    def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        amount_usd: float,
    ) -> OrderResult:
        """Place order with FOK → GTC fallback strategy.

        1. Try FOK (instant fill or cancel)
        2. If FOK fails, place GTC and wait up to 10 seconds
        3. If GTC doesn't fill in time, cancel it

        Prints detailed diagnostics before placing.
        """
        price = float(price)
        amount_usd = float(amount_usd)

        # ── Calculate share size ────────────────────────────────────
        shares, spend = calculate_order_size(price, amount_usd)
        if shares < MIN_SHARES or spend <= 0:
            return OrderResult(
                success=False, status=REJECTED,
                error=f"Can't meet min {MIN_SHARES:.0f} shares (got {shares:.0f}) "
                      f"within ${amount_usd:.2f} at ${price}",
                side=side, price=price, token_id=token_id[:16] + "...",
            )

        # ── Dry run ─────────────────────────────────────────────────
        if self.dry_run:
            return OrderResult(
                success=True, order_id=f"DRY-{int(time.time())}",
                status=FILLED, side=side, price=price,
                size_requested=shares, size_filled=shares, amount_spent=spend,
                token_id=token_id[:16] + "...", dry_run=True,
            )

        if not self._initialized:
            return OrderResult(success=False, status=FAILED, error="Not initialized")

        # ── Diagnostics ─────────────────────────────────────────────
        snap = self.get_book_snapshot(token_id, price, side)
        print(f"  📊 Book: bid ${snap.best_bid:.3f} / ask ${snap.best_ask:.3f} "
              f"(spread ${snap.spread:.3f})")
        print(f"     Liq @ ${price:.3f}: {snap.liquidity_at_price:.0f} shares | "
              f"We want: {shares:.0f} shares (${spend:.2f})")

        if snap.liquidity_at_price < shares:
            print(f"  ⚠️  Only {snap.liquidity_at_price:.0f} of {shares:.0f} shares "
                  f"available — FOK will likely fail")

        # ── Step 1: Try FOK ─────────────────────────────────────────
        fok_result = self._send_order(token_id, side, price, shares, OrderType.FOK)
        if fok_result.success:
            print(f"  ⚡ FOK filled!")
            return fok_result

        print(f"  ⚡ FOK killed — trying GTC fallback ({GTC_TIMEOUT:.0f}s timeout)")

        # ── Step 2: GTC fallback ────────────────────────────────────
        gtc_result = self._send_order(token_id, side, price, shares, OrderType.GTC)
        if not gtc_result.success and not gtc_result.order_id:
            # GTC also rejected outright
            return gtc_result

        if gtc_result.success:
            # Rare: GTC filled instantly
            return gtc_result

        # GTC is on the book — poll for fill
        order_id = gtc_result.order_id
        return self._wait_for_gtc_fill(order_id, side, price, shares, token_id)

    def _send_order(
        self, token_id: str, side: str, price: float, shares: float,
        order_type: OrderType,
    ) -> OrderResult:
        """Send a single order (FOK or GTC). Returns result."""
        try:
            order_args = OrderArgs(
                price=price, size=shares, side=side, token_id=token_id,
            )
            signed_order = self.client.create_order(order_args)
            result = self.client.post_order(signed_order, order_type)

            order_id = result.get("orderID", "")
            if not order_id:
                return OrderResult(
                    success=False, status=REJECTED,
                    error="No orderID in response",
                    side=side, price=price, token_id=token_id[:16] + "...",
                )

            # Check fill
            fill = self._check_order(order_id)
            if fill:
                size_matched = float(
                    fill.get("size_matched", 0) if isinstance(fill, dict)
                    else getattr(fill, "size_matched", 0)
                )
                if size_matched > 0:
                    fill_price = float(
                        fill.get("price", price) if isinstance(fill, dict)
                        else getattr(fill, "price", price)
                    )
                    return OrderResult(
                        success=True, order_id=order_id, status=FILLED,
                        side=side, price=fill_price,
                        size_requested=shares, size_filled=size_matched,
                        amount_spent=size_matched * fill_price,
                        token_id=token_id[:16] + "...", dry_run=False,
                    )

            # Order placed but not filled (GTC on book, or FOK killed)
            return OrderResult(
                success=False, order_id=order_id,
                status=CANCELLED if order_type == OrderType.FOK else "PLACED",
                error="Not matched" if order_type == OrderType.FOK else "On book, waiting",
                side=side, price=price, size_requested=shares,
                token_id=token_id[:16] + "...", dry_run=False,
            )

        except Exception as e:
            return OrderResult(
                success=False, status=FAILED, error=str(e),
                side=side, price=price, token_id=token_id[:16] + "...",
            )

    def _wait_for_gtc_fill(
        self, order_id: str, side: str, price: float, shares: float,
        token_id: str,
    ) -> OrderResult:
        """Poll a GTC order for up to GTC_TIMEOUT seconds."""
        deadline = time.time() + GTC_TIMEOUT
        polls = 0

        while time.time() < deadline:
            time.sleep(GTC_POLL_INTERVAL)
            polls += 1

            fill = self._check_order(order_id)
            if not fill:
                continue

            size_matched = float(
                fill.get("size_matched", 0) if isinstance(fill, dict)
                else getattr(fill, "size_matched", 0)
            )

            if size_matched > 0:
                fill_price = float(
                    fill.get("price", price) if isinstance(fill, dict)
                    else getattr(fill, "price", price)
                )
                print(f"  📋 GTC filled after {polls * GTC_POLL_INTERVAL:.0f}s!")
                return OrderResult(
                    success=True, order_id=order_id, status=FILLED,
                    side=side, price=fill_price,
                    size_requested=shares, size_filled=size_matched,
                    amount_spent=size_matched * fill_price,
                    token_id=token_id[:16] + "...", dry_run=False,
                )

            status_raw = ""
            if isinstance(fill, dict):
                status_raw = fill.get("status", "").upper()
            else:
                status_raw = getattr(fill, "status", "").upper()

            if status_raw in ("CANCELLED", "EXPIRED"):
                return OrderResult(
                    success=False, order_id=order_id, status=CANCELLED,
                    error="GTC cancelled/expired during wait",
                    side=side, price=price, size_requested=shares,
                    token_id=token_id[:16] + "...", dry_run=False,
                )

        # Timed out — cancel
        print(f"  ⏰ GTC not filled after {GTC_TIMEOUT:.0f}s — cancelling")
        self.cancel_order(order_id)

        # Final check — might have filled between last poll and cancel
        final = self._check_order(order_id)
        if final:
            size_matched = float(
                final.get("size_matched", 0) if isinstance(final, dict)
                else getattr(final, "size_matched", 0)
            )
            if size_matched > 0:
                fill_price = float(
                    final.get("price", price) if isinstance(final, dict)
                    else getattr(final, "price", price)
                )
                print(f"  📋 GTC filled just before cancel!")
                return OrderResult(
                    success=True, order_id=order_id, status=FILLED,
                    side=side, price=fill_price,
                    size_requested=shares, size_filled=size_matched,
                    amount_spent=size_matched * fill_price,
                    token_id=token_id[:16] + "...", dry_run=False,
                )

        return OrderResult(
            success=False, order_id=order_id, status=CANCELLED,
            error=f"GTC not filled in {GTC_TIMEOUT:.0f}s",
            side=side, price=price, size_requested=shares,
            token_id=token_id[:16] + "...", dry_run=False,
        )

    # ── Sell shares (for exit or claim) ─────────────────────────────

    def sell_shares(self, token_id: str, shares: float, price: float) -> OrderResult:
        """Sell shares via FOK. Used for pre-close exit and fallback claim."""
        sell_int = int(shares)
        if sell_int < 1:
            return OrderResult(
                success=False, status=REJECTED,
                error="Less than 1 share to sell", side="SELL", price=price,
            )
        notional = sell_int * price
        # Use FOK only for sells — we don't want sells sitting on book
        return self._send_order(token_id, "SELL", price, float(sell_int), OrderType.FOK)

    def claim_winnings(self, token_id: str, shares: float) -> OrderResult:
        """Sell resolved shares at $0.99 — post-resolution fallback."""
        return self.sell_shares(token_id, shares, CLAIM_SELL_PRICE)

    # ── Check order ─────────────────────────────────────────────────

    def _check_order(self, order_id: str) -> Optional[dict]:
        if not self._initialized:
            return None
        try:
            return self.client.get_order(order_id)
        except Exception as e:
            print(f"[executor] Order check failed: {e}")
            return None

    # ── Cancel ──────────────────────────────────────────────────────

    def cancel_order(self, order_id: str) -> bool:
        if self.dry_run or not self._initialized:
            return True
        try:
            self.client.cancel(order_id=order_id)
            return True
        except Exception as e:
            print(f"[executor] Cancel {order_id[:12]}... failed: {e}")
            return False

    def cancel_all(self) -> bool:
        if self.dry_run or not self._initialized:
            return True
        try:
            self.client.cancel_all()
            return True
        except Exception as e:
            print(f"[executor] Cancel all failed: {e}")
            return False
