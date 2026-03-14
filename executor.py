"""Order executor for Polymarket CLOB.

v3 — Key changes:
  - FOK (Fill or Kill) orders: fill instantly or cancel. No polling.
  - Minimum 5 shares enforced (Polymarket CLOB requirement).
  - Single verification call after FOK — no 30-second polling loop.
"""

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


# ── Order states ────────────────────────────────────────────────────

FILLED = "FILLED"
REJECTED = "REJECTED"
CANCELLED = "CANCELLED"
FAILED = "FAILED"

# Polymarket CLOB minimum order size in shares
MIN_SHARES = 5.0

# Polymarket constraint for FOK orders (server-side validation):
# - maker amount (USDC) must have <= 2 decimals
# - taker amount (shares) must have <= 4 decimals


def _choose_share_size(
    price: float,
    max_usd: float,
    min_shares: int = int(MIN_SHARES),
    max_shares: Optional[int] = None,
) -> tuple[float, float]:
    """Pick an integer share size so that shares * price has at most 2 decimals.

    Returns (shares, spend_usd). If no valid combination exists within max_usd,
    returns (0.0, 0.0).
    """
    import math

    if price <= 0 or max_usd <= 0:
        return 0.0, 0.0

    # Cap max_usd to cents
    max_usd_cents = int(math.floor(max_usd * 100))
    if max_usd_cents <= 0:
        return 0.0, 0.0

    # Work backwards from the largest affordable share count
    # Shares are whole contracts; taker amount will then have 0 decimals.
    max_possible_shares = int(max_usd_cents / math.ceil(price * 100)) or 0
    if max_shares is not None:
        max_possible_shares = min(max_possible_shares, max_shares)
    max_possible_shares = max(max_possible_shares, min_shares)

    for shares in range(max_possible_shares, min_shares - 1, -1):
        total = shares * price
        # Check if total has at most 2 decimal places (no fractional cents).
        total_cents = round(total * 100) / 100.0
        if abs(total - total_cents) < 1e-9:
            # Also ensure we do not exceed max_usd
            if total_cents <= max_usd_cents / 100.0:
                return float(shares), float(total_cents)

    return 0.0, 0.0


@dataclass
class OrderResult:
    success: bool            # Did shares actually change hands?
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
        """Initialize the CLOB client and derive API credentials."""
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
        """Get current USDC balance on Polymarket."""
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

    def get_orderbook_price(self, token_id: str) -> tuple[float, float]:
        """Get best bid and ask. Returns (best_bid, best_ask)."""
        if not self._initialized:
            return 0.0, 0.0
        try:
            book = self.client.get_order_book(token_id)
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            best_bid = float(bids[0]["price"]) if bids else 0.0
            best_ask = float(asks[0]["price"]) if asks else 0.0
            return best_bid, best_ask
        except Exception as e:
            print(f"[executor] Orderbook fetch failed: {e}")
            return 0.0, 0.0

    def _max_fillable_shares(self, token_id: str, side: str, price: float) -> float:
        """Estimate max shares fillable at or better than price from the order book."""
        if not self._initialized:
            return 0.0
        try:
            book = self.client.get_order_book(token_id)
            if side.upper() == "BUY":
                asks = book.get("asks", [])
                qty = 0.0
                for level in asks:
                    lvl_price = float(level.get("price", 0))
                    lvl_size = float(level.get("size", 0))
                    if lvl_price <= price:
                        qty += lvl_size
                return qty
            else:
                bids = book.get("bids", [])
                qty = 0.0
                for level in bids:
                    lvl_price = float(level.get("price", 0))
                    lvl_size = float(level.get("size", 0))
                    if lvl_price >= price:
                        qty += lvl_size
                return qty
        except Exception as e:
            print(f"[executor] Liquidity check failed: {e}")
            return 0.0

    # ── Place FOK order ─────────────────────────────────────────────

    def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        amount_usd: float,
    ) -> OrderResult:
        """Place a Fill-or-Kill order.

        FOK = fill the entire order immediately, or cancel it.
        No sitting on the book. No polling. Instant truth.

        Enforces Polymarket minimum of 5 shares and adjusts size so that
        maker amount (USD) has at most 2 decimals, as required by the API.
        """
        price = float(price)
        amount_usd = float(amount_usd)

        # Cap by available liquidity at or better than price (FOK requirement).
        max_liq_shares = self._max_fillable_shares(token_id, side, price)

        # Choose integer share size so that shares * price has <= 2 decimals,
        # and does not exceed liquidity.
        shares, spend_usd = _choose_share_size(
            price,
            amount_usd,
            max_shares=int(max_liq_shares) if max_liq_shares > 0 else None,
        )

        # ── Enforce minimum 5 shares ────────────────────────────────
        if shares < MIN_SHARES:
            # Try again with minimum shares constraint, still respecting liquidity.
            shares, spend_usd = _choose_share_size(
                price,
                amount_usd,
                min_shares=int(MIN_SHARES),
                max_shares=int(max_liq_shares) if max_liq_shares > 0 else None,
            )

        if shares < MIN_SHARES or spend_usd <= 0:
            return OrderResult(
                success=False,
                status=REJECTED,
                error="Unable to find valid share size within decimal constraints",
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
                amount_spent=spend_usd,
                token_id=token_id[:16] + "...",
                dry_run=True,
            )

        if not self._initialized:
            return OrderResult(success=False, status=FAILED, error="Client not initialized")

        # ── Live FOK order ──────────────────────────────────────────
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
                    success=False,
                    status=REJECTED,
                    error="No orderID in response",
                    side=side,
                    price=price,
                    token_id=token_id[:16] + "...",
                )

            # ── Single verification call ────────────────────────────
            # FOK resolves instantly. One call to confirm fill details.
            fill = self._check_order(order_id)

            if fill:
                size_matched = float(fill.get("size_matched", 0))
                if size_matched > 0:
                    fill_price = float(fill.get("price", price))
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

            # FOK didn't match — no shares, no cost
            return OrderResult(
                success=False,
                order_id=order_id,
                status=CANCELLED,
                error="FOK not matched — no liquidity at this price",
                side=side,
                price=price,
                size_requested=shares,
                token_id=token_id[:16] + "...",
                dry_run=False,
            )

        except Exception as e:
            error_msg = str(e)
            if "lower than the minimum" in error_msg:
                return OrderResult(
                    success=False,
                    status=REJECTED,
                    error=f"Below minimum order size",
                    side=side,
                    price=price,
                    token_id=token_id[:16] + "...",
                )
            return OrderResult(
                success=False,
                status=FAILED,
                error=error_msg,
                side=side,
                price=price,
                token_id=token_id[:16] + "...",
            )

    # ── Check order (single call) ───────────────────────────────────

    def _check_order(self, order_id: str) -> Optional[dict]:
        """Single check of order status. Returns raw order dict or None."""
        if not self._initialized:
            return None
        try:
            return self.client.get_order(order_id)
        except Exception as e:
            print(f"[executor] Order check failed: {e}")
            return None

    # ── Cancel ──────────────────────────────────────────────────────

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a specific order by ID."""
        if self.dry_run or not self._initialized:
            return True
        try:
            self.client.cancel(order_id=order_id)
            return True
        except Exception as e:
            print(f"[executor] Cancel {order_id[:12]}... failed: {e}")
            return False

    def cancel_all(self) -> bool:
        """Cancel all open orders."""
        if self.dry_run or not self._initialized:
            return True
        try:
            self.client.cancel_all()
            return True
        except Exception as e:
            print(f"[executor] Cancel all failed: {e}")
            return False


if __name__ == "__main__":
    exe = Executor(private_key="0x" + "a" * 64, dry_run=True)

    # Test: amount below minimum should auto-bump
    result = exe.place_order(
        token_id="fake_token_123",
        side="BUY",
        price=0.65,
        amount_usd=2.0,
    )

    print(f"Order: {result}")
    print(f"  Status: {result.status}")
    print(f"  Shares: {result.size_filled:.2f} (requested {result.size_requested:.2f})")
    print(f"  Spent: ${result.amount_spent:.2f}")
