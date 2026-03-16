"""Order executor for Polymarket CLOB.

v9.2 — Fixes:
  - Approves CONDITIONAL token transfers on init (fixes sell failures)
  - Complement engine pricing via calculate_market_price
  - Clean decimal sizing via regular OrderArgs
  - GTC for slippage tolerance
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

            # Approve BOTH collateral (USDC) and conditional (shares) transfers
            # Without conditional approval, sells fail with "not enough balance/allowance"
            self.client.update_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            self.client.update_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL)
            )

            print(f"[executor] Initialized ({'DRY RUN' if self.dry_run else 'LIVE'})")
            print(f"[executor] Approved: COLLATERAL + CONDITIONAL")
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
        """What would we actually pay/receive? Queries the merged book."""
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

    # ── Buy ─────────────────────────────────────────────────────────

    def buy(self, token_id: str, amount_usd: float) -> OrderResult:
        """Buy shares using complement engine price + clean decimals."""
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

        # Get real price from complement engine
        market_price = self.get_market_price(token_id, "BUY", amount_usd)
        if market_price <= 0:
            return OrderResult(
                success=False, status=FAILED,
                error="Could not get market price", side="BUY",
                token_id=token_id[:16] + "...",
            )

        # Clean decimal sizing
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

        return self._place_order(token_id, "BUY", market_price, shares, spend)

    # ── Sell ────────────────────────────────────────────────────────

    def sell(self, token_id: str, shares: float, price: float = 0.0) -> OrderResult:
        """Sell shares. If price=0, gets market price automatically."""
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

        # Get sell price from complement engine if not provided
        if price <= 0:
            notional = float(sell_shares) * 0.50
            price = self.get_market_price(token_id, "SELL", notional)
            if price <= 0:
                return OrderResult(
                    success=False, status=FAILED,
                    error="Could not get sell price", side="SELL",
                    token_id=token_id[:16] + "...",
                )

        spend = round(sell_shares * price, 2)
        print(f"  📊 Sell: {sell_shares} shares @ ${price:.3f} = ${spend:.2f}")

        return self._place_order(token_id, "SELL", price, float(sell_shares), spend)

    # ── Internal: place order ───────────────────────────────────────

    def _place_order(
        self, token_id: str, side: str, price: float, shares: float, spend: float,
    ) -> OrderResult:
        try:
            order_args = OrderArgs(
                price=price, size=shares, side=side, token_id=token_id,
            )

            signed_order = self.client.create_order(order_args)
            result = self.client.post_order(signed_order, OrderType.GTC)

            order_id = result.get("orderID", "")
            if not order_id:
                return OrderResult(
                    success=False, status=REJECTED,
                    error="No orderID", side=side, price=price,
                    token_id=token_id[:16] + "...",
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
                    actual_usd = size_matched * fill_price
                    return OrderResult(
                        success=True, order_id=order_id, status=FILLED,
                        side=side, price=fill_price,
                        amount_usd=actual_usd, shares=size_matched,
                        token_id=token_id[:16] + "...", dry_run=False,
                    )

            # GTC on book — wait briefly
            time.sleep(2)
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
                        amount_usd=size_matched * fill_price, shares=size_matched,
                        token_id=token_id[:16] + "...", dry_run=False,
                    )

            # Still not filled — cancel
            self.cancel_order(order_id)
            return OrderResult(
                success=False, order_id=order_id, status=FAILED,
                error="Order not filled within 2s",
                side=side, price=price, token_id=token_id[:16] + "...",
            )

        except Exception as e:
            return OrderResult(
                success=False, status=FAILED, error=str(e),
                side=side, price=price, token_id=token_id[:16] + "...",
            )

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
