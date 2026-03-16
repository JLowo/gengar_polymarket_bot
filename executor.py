"""Order executor for Polymarket CLOB.

v9.3 — Split buy/sell paths:
  - BUY: calculate_market_price → clean OrderArgs → post_order(GTC)
    (works perfectly, clean decimals)
  - SELL: create_market_order → post_order(GTC)
    (complement engine handles token routing/approval internally)

The sell "not enough balance/allowance" error happens because regular
OrderArgs tries to move ERC-1155 conditional tokens directly, which
needs on-chain approval. create_market_order routes through the
complement engine which handles this internally.
"""

import time
from dataclasses import dataclass
from typing import Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    OrderArgs,
    MarketOrderArgs,
    OrderType,
    BalanceAllowanceParams,
    AssetType,
)
from py_clob_client.constants import POLYGON


FILLED = "FILLED"
REJECTED = "REJECTED"
FAILED = "FAILED"

MIN_SHARES = 5.0
MIN_AMOUNT_USD = 1.0


@dataclass
class OrderResult:
    success: bool
    order_id: str = ""
    status: str = FAILED
    side: str = ""
    price: float = 0.0
    amount_usd: float = 0.0
    shares: float = 0.0
    token_id: str = ""
    error: str = ""
    dry_run: bool = True


def calculate_order_size(price: float, max_usd: float) -> tuple[float, float]:
    """Integer shares × price = clean 2-decimal USD amount."""
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

    # ── Price from complement engine ────────────────────────────────

    def get_market_price(self, token_id: str, side: str, amount_usd: float) -> float:
        if not self._initialized:
            return 0.0
        try:
            price = self.client.calculate_market_price(
                token_id=token_id,
                side=side,
                amount=amount_usd,
                order_type=OrderType.GTC,
            )
            return float(price) if price else 0.0
        except Exception as e:
            print(f"[executor] Price check failed: {e}")
            return 0.0

    # ── Buy (clean OrderArgs + GTC) ─────────────────────────────────

    def buy(self, token_id: str, amount_usd: float) -> OrderResult:
        amount_usd = round(float(amount_usd), 2)
        if amount_usd < MIN_AMOUNT_USD:
            return OrderResult(
                success=False, status=REJECTED,
                error=f"Amount ${amount_usd:.2f} below min", side="BUY",
            )

        if self.dry_run:
            sim_price = 0.55
            return OrderResult(
                success=True, order_id=f"DRY-{int(time.time())}",
                status=FILLED, side="BUY", price=sim_price,
                amount_usd=amount_usd, shares=amount_usd / sim_price,
                token_id=token_id[:16] + "...", dry_run=True,
            )

        if not self._initialized:
            return OrderResult(success=False, status=FAILED, error="Not initialized")

        market_price = self.get_market_price(token_id, "BUY", amount_usd)
        if market_price <= 0:
            return OrderResult(
                success=False, status=FAILED,
                error="Could not get market price", side="BUY",
                token_id=token_id[:16] + "...",
            )

        shares, spend = calculate_order_size(market_price, amount_usd)
        if shares < MIN_SHARES or spend <= 0:
            return OrderResult(
                success=False, status=REJECTED,
                error=f"Can't get {MIN_SHARES:.0f}+ shares at ${market_price:.3f} "
                      f"within ${amount_usd:.2f}",
                side="BUY", price=market_price, token_id=token_id[:16] + "...",
            )

        print(f"  📊 Market price: ${market_price:.3f}/share "
              f"→ {shares:.0f} shares for ${spend:.2f}")

        # Regular OrderArgs for buys — clean decimals
        try:
            order_args = OrderArgs(
                price=market_price, size=shares, side="BUY", token_id=token_id,
            )
            signed_order = self.client.create_order(order_args)
            result = self.client.post_order(signed_order, OrderType.GTC)

            order_id = result.get("orderID", "")
            if not order_id:
                return OrderResult(
                    success=False, status=REJECTED,
                    error="No orderID", side="BUY", price=market_price,
                    token_id=token_id[:16] + "...",
                )

            return self._verify_fill(order_id, "BUY", market_price, shares, token_id)

        except Exception as e:
            return OrderResult(
                success=False, status=FAILED, error=str(e),
                side="BUY", price=market_price, token_id=token_id[:16] + "...",
            )

    # ── Sell (create_market_order — complement engine handles routing) ─

    def sell(self, token_id: str, shares: float, price: float = 0.0) -> OrderResult:
        """Sell shares via create_market_order.

        Uses the complement engine path which handles ERC-1155
        token routing internally — no manual approval needed.
        """
        sell_shares = int(shares)
        if sell_shares < 1:
            return OrderResult(
                success=False, status=REJECTED,
                error="Less than 1 share", side="SELL",
            )

        if self.dry_run:
            sim_price = price if price > 0 else 0.90
            revenue = sell_shares * sim_price
            return OrderResult(
                success=True, order_id=f"DRY-SELL-{int(time.time())}",
                status=FILLED, side="SELL", price=sim_price,
                amount_usd=revenue, shares=float(sell_shares),
                token_id=token_id[:16] + "...", dry_run=True,
            )

        if not self._initialized:
            return OrderResult(success=False, status=FAILED, error="Not initialized")

        # Calculate sell amount in USD (what we expect to receive)
        if price <= 0:
            notional = float(sell_shares) * 0.50
            price = self.get_market_price(token_id, "SELL", notional)
            if price <= 0:
                return OrderResult(
                    success=False, status=FAILED,
                    error="Could not get sell price", side="SELL",
                    token_id=token_id[:16] + "...",
                )

        sell_amount = round(sell_shares * price, 2)
        print(f"  📊 Sell: {sell_shares} shares @ ${price:.3f} = ${sell_amount:.2f}")

        # Use create_market_order for sells — handles token routing
        try:
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=sell_amount,
                side="SELL",
            )

            signed_order = self.client.create_market_order(order_args)
            result = self.client.post_order(signed_order, OrderType.GTC)

            order_id = result.get("orderID", "")
            if not order_id:
                return OrderResult(
                    success=False, status=REJECTED,
                    error="No orderID from sell", side="SELL",
                    price=price, token_id=token_id[:16] + "...",
                )

            return self._verify_fill(order_id, "SELL", price, float(sell_shares), token_id)

        except Exception as e:
            return OrderResult(
                success=False, status=FAILED, error=str(e),
                side="SELL", price=price, token_id=token_id[:16] + "...",
            )

    # ── Verify fill (shared by buy and sell) ────────────────────────

    def _verify_fill(
        self, order_id: str, side: str, price: float, shares: float, token_id: str,
    ) -> OrderResult:
        """Check if an order filled, with one retry after 2s."""
        # First check
        fill = self._check_order(order_id)
        if fill:
            matched = self._extract_fill(fill, price)
            if matched:
                return OrderResult(
                    success=True, order_id=order_id, status=FILLED,
                    side=side, price=matched[0],
                    amount_usd=matched[1], shares=matched[2],
                    token_id=token_id[:16] + "...", dry_run=False,
                )

        # Wait and retry
        time.sleep(2)
        fill = self._check_order(order_id)
        if fill:
            matched = self._extract_fill(fill, price)
            if matched:
                return OrderResult(
                    success=True, order_id=order_id, status=FILLED,
                    side=side, price=matched[0],
                    amount_usd=matched[1], shares=matched[2],
                    token_id=token_id[:16] + "...", dry_run=False,
                )

        # Not filled — cancel
        self.cancel_order(order_id)
        return OrderResult(
            success=False, order_id=order_id, status=FAILED,
            error="Order not filled within 2s",
            side=side, price=price, token_id=token_id[:16] + "...",
        )

    def _extract_fill(self, fill: dict, fallback_price: float) -> Optional[tuple]:
        """Extract (price, usd, shares) from a fill response. Returns None if not filled."""
        size_matched = float(
            fill.get("size_matched", 0) if isinstance(fill, dict)
            else getattr(fill, "size_matched", 0)
        )
        if size_matched <= 0:
            return None

        fill_price = float(
            fill.get("price", fallback_price) if isinstance(fill, dict)
            else getattr(fill, "price", fallback_price)
        )
        return (fill_price, size_matched * fill_price, size_matched)

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
            print(f"[executor] Cancel failed: {e}")
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
