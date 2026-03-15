"""Order executor for Polymarket CLOB.

v4 — Fixes and features:
  - FOK (Fill or Kill) orders with liquidity-aware sizing
  - Minimum 5 shares enforced (Polymarket CLOB requirement)
  - Maker amount rounded to 2 decimals, taker to 4 (API constraint)
  - OrderBookSummary handled as object (not dict)
  - Auto-claim winnings via SELL at $0.99
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
CLAIM_SELL_PRICE = 0.99   # Sell winning shares at 99¢ to auto-collect


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


# ── Decimal-safe share sizing ───────────────────────────────────────

def calculate_order_size(price: float, max_usd: float) -> tuple[float, float]:
    """Find the largest whole-number share count where shares × price
    has at most 2 decimal places, and total ≤ max_usd.

    Returns (shares, spend_usd). Returns (0, 0) if no valid size found.
    """
    if price <= 0 or max_usd <= 0:
        return 0.0, 0.0

    # Price in cents (integer). Polymarket prices are multiples of $0.01.
    price_cents = round(price * 100)
    max_usd_cents = int(max_usd * 100)

    # Max shares we could possibly afford
    max_shares = max_usd_cents // price_cents if price_cents > 0 else 0

    # Enforce minimum
    if max_shares < MIN_SHARES:
        # Check if we can afford the minimum
        min_cost_cents = int(MIN_SHARES) * price_cents
        if min_cost_cents <= max_usd_cents:
            max_shares = int(MIN_SHARES)
        else:
            return 0.0, 0.0

    # With integer shares and price as cents/100:
    # spend = shares * price_cents / 100 → always has ≤ 2 decimals ✓
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

    # ── Initialization ──────────────────────────────────────────────

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

    # ── Balance ─────────────────────────────────────────────────────

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

    # ── Order book (handles both dict and object responses) ─────────

    def _read_book(self, token_id: str) -> tuple[list, list]:
        """Read order book, returning (asks, bids) as lists of (price, size).

        Handles both dict responses and OrderBookSummary objects.
        """
        if not self._initialized:
            return [], []
        try:
            book = self.client.get_order_book(token_id)

            # Try object attributes first, fall back to dict access
            if hasattr(book, 'asks'):
                raw_asks = book.asks or []
                raw_bids = book.bids or []
            else:
                raw_asks = book.get("asks", [])
                raw_bids = book.get("bids", [])

            # Normalize each level to (price, size) tuples
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

    def get_orderbook_price(self, token_id: str) -> tuple[float, float]:
        """Get best bid and ask."""
        asks, bids = self._read_book(token_id)
        best_bid = bids[0][0] if bids else 0.0
        best_ask = asks[0][0] if asks else 0.0
        return best_bid, best_ask

    def _available_liquidity(self, token_id: str, side: str, price: float) -> float:
        """How many shares can we BUY (or SELL) at this price or better?"""
        asks, bids = self._read_book(token_id)
        total = 0.0
        if side.upper() == "BUY":
            for lvl_price, lvl_size in asks:
                if lvl_price <= price:
                    total += lvl_size
        else:
            for lvl_price, lvl_size in bids:
                if lvl_price >= price:
                    total += lvl_size
        return total

    # ── Place FOK order ─────────────────────────────────────────────

    def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        amount_usd: float,
    ) -> OrderResult:
        """Place a Fill-or-Kill order with liquidity-aware sizing.

        1. Check available liquidity at our price
        2. Cap order to what the book can fill
        3. Round to clean decimals (API constraint)
        4. FOK: fills instantly or cancels — one verification call
        """
        price = float(price)
        amount_usd = float(amount_usd)

        # ── Check liquidity first ───────────────────────────────────
        if not self.dry_run and self._initialized:
            liq = self._available_liquidity(token_id, side, price)
            if liq > 0:
                # Cap USD spend to what the book can actually fill
                max_from_liq = liq * price
                if amount_usd > max_from_liq:
                    amount_usd = max_from_liq
                    print(f"  📉 Capped to book liquidity: {liq:.0f} shares (${amount_usd:.2f})")

        # ── Calculate clean share size ──────────────────────────────
        shares, spend = calculate_order_size(price, amount_usd)

        if shares < MIN_SHARES or spend <= 0:
            return OrderResult(
                success=False,
                status=REJECTED,
                error=f"Can't meet min {MIN_SHARES:.0f} shares within ${amount_usd:.2f} at ${price}",
                side=side,
                price=price,
                token_id=token_id[:16] + "...",
            )

        # ── Dry run ─────────────────────────────────────────────────
        if self.dry_run:
            return OrderResult(
                success=True,
                order_id=f"DRY-{int(time.time())}",
                status=FILLED,
                side=side,
                price=price,
                size_requested=shares,
                size_filled=shares,
                amount_spent=spend,
                token_id=token_id[:16] + "...",
                dry_run=True,
            )

        if not self._initialized:
            return OrderResult(success=False, status=FAILED, error="Not initialized")

        # ── Send FOK ────────────────────────────────────────────────
        try:
            order_args = OrderArgs(
                price=price,
                size=shares,
                side=side,
                token_id=token_id,
            )

            signed_order = self.client.create_order(order_args)
            result = self.client.post_order(signed_order, OrderType.FOK)

            order_id = result.get("orderID", "")
            if not order_id:
                return OrderResult(
                    success=False, status=REJECTED,
                    error="No orderID in response",
                    side=side, price=price,
                    token_id=token_id[:16] + "...",
                )

            # Single verification — FOK is instant
            fill = self._check_order(order_id)
            if fill:
                size_matched = float(
                    fill.get("size_matched", 0)
                    if isinstance(fill, dict)
                    else getattr(fill, "size_matched", 0)
                )
                if size_matched > 0:
                    fill_price = float(
                        fill.get("price", price)
                        if isinstance(fill, dict)
                        else getattr(fill, "price", price)
                    )
                    return OrderResult(
                        success=True,
                        order_id=order_id,
                        status=FILLED,
                        side=side,
                        price=fill_price,
                        size_requested=shares,
                        size_filled=size_matched,
                        amount_spent=size_matched * fill_price,
                        token_id=token_id[:16] + "...",
                        dry_run=False,
                    )

            return OrderResult(
                success=False,
                order_id=order_id,
                status=CANCELLED,
                error="FOK not matched — no liquidity",
                side=side, price=price,
                size_requested=shares,
                token_id=token_id[:16] + "...",
                dry_run=False,
            )

        except Exception as e:
            return OrderResult(
                success=False, status=FAILED,
                error=str(e), side=side, price=price,
                token_id=token_id[:16] + "...",
            )

    # ── Auto-claim: sell winning shares at $0.99 ────────────────────

    def claim_winnings(self, token_id: str, shares: float) -> OrderResult:
        """Sell resolved winning shares at $0.99 to convert back to USDC.

        Loses ~1¢/share but eliminates manual claiming.
        The market must be resolved (shares worth $1) for this to work.
        """
        if self.dry_run:
            payout = shares * CLAIM_SELL_PRICE
            return OrderResult(
                success=True,
                order_id=f"DRY-CLAIM-{int(time.time())}",
                status=FILLED,
                side="SELL",
                price=CLAIM_SELL_PRICE,
                size_requested=shares,
                size_filled=shares,
                amount_spent=payout,  # USD received
                token_id=token_id[:16] + "...",
                dry_run=True,
            )

        if not self._initialized:
            return OrderResult(success=False, status=FAILED, error="Not initialized")

        # Use integer shares for clean decimals
        sell_shares = int(shares)
        if sell_shares < 1:
            return OrderResult(
                success=False, status=REJECTED,
                error="Less than 1 share to claim",
            )

        try:
            order_args = OrderArgs(
                price=CLAIM_SELL_PRICE,
                size=float(sell_shares),
                side="SELL",
                token_id=token_id,
            )

            signed_order = self.client.create_order(order_args)
            result = self.client.post_order(signed_order, OrderType.FOK)

            order_id = result.get("orderID", "")
            if not order_id:
                return OrderResult(
                    success=False, status=REJECTED,
                    error="No orderID from claim sell",
                    side="SELL", price=CLAIM_SELL_PRICE,
                )

            # Verify fill
            fill = self._check_order(order_id)
            if fill:
                size_matched = float(
                    fill.get("size_matched", 0)
                    if isinstance(fill, dict)
                    else getattr(fill, "size_matched", 0)
                )
                if size_matched > 0:
                    payout = size_matched * CLAIM_SELL_PRICE
                    return OrderResult(
                        success=True,
                        order_id=order_id,
                        status=FILLED,
                        side="SELL",
                        price=CLAIM_SELL_PRICE,
                        size_requested=float(sell_shares),
                        size_filled=size_matched,
                        amount_spent=payout,
                        token_id=token_id[:16] + "...",
                        dry_run=False,
                    )

            return OrderResult(
                success=False, order_id=order_id,
                status=CANCELLED,
                error="Claim sell not matched — market may not be settled yet",
                side="SELL", price=CLAIM_SELL_PRICE,
            )

        except Exception as e:
            return OrderResult(
                success=False, status=FAILED,
                error=f"Claim failed: {e}",
                side="SELL", price=CLAIM_SELL_PRICE,
            )

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


if __name__ == "__main__":
    # Test decimal sizing
    print("=== Decimal sizing tests ===")
    for price, usd in [(0.58, 15.0), (0.535, 20.0), (0.685, 10.0), (0.50, 3.0)]:
        shares, spend = calculate_order_size(price, usd)
        print(f"  ${usd:.2f} @ ${price:.3f} → {shares:.0f} shares, ${spend:.2f} spend")

    print("\n=== Dry run order test ===")
    exe = Executor(private_key="0x" + "a" * 64, dry_run=True)
    result = exe.place_order("fake_token", "BUY", 0.65, 2.0)
    print(f"  {result.status}: {result.size_filled:.0f} shares @ ${result.price} = ${result.amount_spent:.2f}")
