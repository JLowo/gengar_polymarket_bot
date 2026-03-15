"""Order executor for Polymarket CLOB.

v7 — Maker-first strategy:
  - If book has liquidity at our price → FOK (instant taker fill)
  - If book is thin/empty → GTC (sit on book as maker, catch volume)
  - Persistent fill checking for GTC orders across ticks
  - Detailed diagnostics before every order
  - sell_shares() for exit, claim_winnings() for post-resolution
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
PLACED = "PLACED"       # GTC on book, not yet matched
REJECTED = "REJECTED"
CANCELLED = "CANCELLED"
FAILED = "FAILED"

MIN_SHARES = 5.0
CLAIM_SELL_PRICE = 0.99


@dataclass
class OrderResult:
    success: bool           # For FOK: did it fill? For GTC: is it on the book?
    filled: bool = False    # Actually matched with a counterparty?
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
    best_bid: float = 0.0
    best_ask: float = 0.0
    spread: float = 0.0
    liquidity_at_price: float = 0.0
    total_ask_depth: float = 0.0
    total_bid_depth: float = 0.0


# ── Decimal-safe share sizing ───────────────────────────────────────

def calculate_order_size(price: float, max_usd: float) -> tuple[float, float]:
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
    def __init__(self, private_key: str, safe_address: str = "", dry_run: bool = True):
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

    # ── Order book ──────────────────────────────────────────────────

    def _read_book(self, token_id: str) -> tuple[list, list]:
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
        if side.upper() == "BUY":
            snap.liquidity_at_price = sum(s for p, s in asks if p <= our_price)
        else:
            snap.liquidity_at_price = sum(s for p, s in bids if p >= our_price)
        return snap

    def get_best_bid(self, token_id: str) -> float:
        _, bids = self._read_book(token_id)
        return bids[0][0] if bids else 0.0

    # ── Smart order placement ───────────────────────────────────────

    def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        amount_usd: float,
    ) -> OrderResult:
        """Smart order routing: FOK if liquidity exists, GTC maker if not.

        Returns OrderResult with:
          - filled=True → instant fill (FOK matched)
          - filled=False, status=PLACED → GTC on book, check later
          - success=False → rejected/failed
        """
        price = float(price)
        amount_usd = float(amount_usd)

        # Size calculation
        shares, spend = calculate_order_size(price, amount_usd)
        if shares < MIN_SHARES or spend <= 0:
            return OrderResult(
                success=False, status=REJECTED,
                error=f"Can't meet min {MIN_SHARES:.0f} shares within ${amount_usd:.2f} at ${price}",
                side=side, price=price, token_id=token_id[:16] + "...",
            )

        # Dry run
        if self.dry_run:
            return OrderResult(
                success=True, filled=True,
                order_id=f"DRY-{int(time.time())}",
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

        # ── Route: FOK if liquidity exists, GTC if not ──────────────
        if snap.liquidity_at_price >= shares:
            # Enough liquidity → try instant fill
            print(f"  ⚡ Liquidity available — trying FOK")
            result = self._send_order(token_id, side, price, shares, OrderType.FOK)
            if result.filled:
                return result
            print(f"  ⚡ FOK killed despite apparent liquidity — falling through to GTC")

        # Place as maker (GTC) — sit on the book
        print(f"  📋 Placing GTC maker order — sitting on the book")
        return self._send_order(token_id, side, price, shares, OrderType.GTC)

    # ── Check if a pending order has filled ─────────────────────────

    def check_fill(self, order_id: str, price: float = 0) -> OrderResult:
        """Check whether a GTC order on the book has been filled.

        Call this each tick for pending orders.
        Returns filled=True if matched, filled=False if still waiting.
        """
        if not order_id or order_id.startswith("DRY-"):
            return OrderResult(success=True, filled=True, status=FILLED, dry_run=True)

        if not self._initialized:
            return OrderResult(success=False, error="Not initialized")

        fill = self._check_order(order_id)
        if not fill:
            return OrderResult(
                success=True, filled=False, order_id=order_id,
                status=PLACED, error="Still on book",
            )

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
                success=True, filled=True,
                order_id=order_id, status=FILLED,
                price=fill_price,
                size_filled=size_matched,
                amount_spent=size_matched * fill_price,
                dry_run=False,
            )

        # Check if cancelled/expired
        status_raw = ""
        if isinstance(fill, dict):
            status_raw = fill.get("status", "").upper()
        else:
            status_raw = getattr(fill, "status", "").upper()

        if status_raw in ("CANCELLED", "EXPIRED"):
            return OrderResult(
                success=False, filled=False,
                order_id=order_id, status=CANCELLED,
                error="Order was cancelled/expired",
            )

        # Still on book
        return OrderResult(
            success=True, filled=False,
            order_id=order_id, status=PLACED,
        )

    # ── Internal: send a single order ───────────────────────────────

    def _send_order(
        self, token_id: str, side: str, price: float, shares: float,
        order_type: OrderType,
    ) -> OrderResult:
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

            # Check immediate fill
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
                        success=True, filled=True,
                        order_id=order_id, status=FILLED,
                        side=side, price=fill_price,
                        size_requested=shares, size_filled=size_matched,
                        amount_spent=size_matched * fill_price,
                        token_id=token_id[:16] + "...", dry_run=False,
                    )

            if order_type == OrderType.FOK:
                return OrderResult(
                    success=False, filled=False,
                    order_id=order_id, status=CANCELLED,
                    error="FOK not matched",
                    side=side, price=price, size_requested=shares,
                    token_id=token_id[:16] + "...", dry_run=False,
                )

            # GTC: on the book, waiting
            return OrderResult(
                success=True, filled=False,
                order_id=order_id, status=PLACED,
                side=side, price=price, size_requested=shares,
                token_id=token_id[:16] + "...", dry_run=False,
            )

        except Exception as e:
            return OrderResult(
                success=False, status=FAILED, error=str(e),
                side=side, price=price, token_id=token_id[:16] + "...",
            )

    # ── Sell / claim ────────────────────────────────────────────────

    def sell_shares(self, token_id: str, shares: float, price: float) -> OrderResult:
        sell_int = int(shares)
        if sell_int < 1:
            return OrderResult(
                success=False, status=REJECTED,
                error="Less than 1 share", side="SELL", price=price,
            )
        return self._send_order(token_id, "SELL", price, float(sell_int), OrderType.FOK)

    def claim_winnings(self, token_id: str, shares: float) -> OrderResult:
        return self.sell_shares(token_id, shares, CLAIM_SELL_PRICE)

    # ── Helpers ─────────────────────────────────────────────────────

    def _check_order(self, order_id: str) -> Optional[dict]:
        if not self._initialized:
            return None
        try:
            return self.client.get_order(order_id)
        except Exception as e:
            print(f"[executor] Order check failed: {e}")
            return None

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
