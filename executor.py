"""Order executor for Polymarket CLOB.

v11 — Balance-verified everything:

1. Balance-verified buys: snapshot USDC before order, check after.
   Ghost fills caught even when API throws exceptions.
2. Balance-verified sells: unchanged from v10, still the source of truth.
3. Minimum notional guard: rejects sells below $5 with explicit
   "hold to resolution" message instead of hitting Polymarket's error.
4. Silenced 'price check failed: no match' spam — only logs real errors.

Still uses create_market_order for sells (complement engine routing).
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
PARTIAL = "PARTIAL"
REJECTED = "REJECTED"
FAILED = "FAILED"

MIN_SHARES = 5.0
MIN_AMOUNT_USD = 1.0
MAX_BUY_PRICE = 0.75  # Don't buy above this — TP can't trigger
POLY_MIN_NOTIONAL = 5.0  # Polymarket rejects orders below $5 notional


@dataclass
class OrderResult:
    success: bool
    order_id: str = ""
    status: str = FAILED
    side: str = ""
    price: float = 0.0
    amount_usd: float = 0.0
    shares: float = 0.0
    shares_remaining: float = 0.0  # For partial fills
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
            print(f"[executor] Max buy price: ${MAX_BUY_PRICE:.2f}")
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
            err = str(e).lower()
            # Only log genuinely unexpected errors, not "no match" book-empty noise
            if "no match" not in err and "none" not in err:
                print(f"[executor] Price check failed: {e}")
            return 0.0

    # ── Buy (clean OrderArgs + GTC + price cap) ─────────────────────

    def buy(self, token_id: str, amount_usd: float) -> OrderResult:
        """Buy via market order. Balance-verified: snapshots USDC before/after
        to detect ghost fills and get the real cost paid."""
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

        # Price cap: don't buy above MAX_BUY_PRICE
        if market_price > MAX_BUY_PRICE:
            return OrderResult(
                success=False, status=REJECTED,
                error=f"Price ${market_price:.3f} > cap ${MAX_BUY_PRICE:.2f} "
                      f"(TP impossible above this)",
                side="BUY", price=market_price, token_id=token_id[:16] + "...",
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

        # Snapshot balance BEFORE buy
        balance_before = self.get_balance()

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

            # Wait for settlement then verify via balance
            time.sleep(2)
            return self._verify_buy_via_balance(
                order_id, market_price, shares, token_id, balance_before,
            )

        except Exception as e:
            # Ghost fill defense: order may have gone through despite exception
            time.sleep(2)
            balance_after = self.get_balance()
            spent = balance_before - balance_after if balance_before > 0 else 0

            if spent > 1.0:
                est_shares = spent / market_price if market_price > 0 else 0
                print(f"  👻 GHOST BUY: balance dropped ${spent:.2f} despite error")
                return OrderResult(
                    success=True, order_id="ghost-buy",
                    status=FILLED, side="BUY", price=market_price,
                    amount_usd=spent, shares=est_shares,
                    token_id=token_id[:16] + "...", dry_run=False,
                )

            return OrderResult(
                success=False, status=FAILED, error=str(e),
                side="BUY", price=market_price, token_id=token_id[:16] + "...",
            )

    def _verify_buy_via_balance(
        self, order_id: str, price: float, shares: float,
        token_id: str, balance_before: float,
    ) -> OrderResult:
        """Verify buy fill via balance change (source of truth), with order API fallback."""
        balance_after = self.get_balance()
        spent = balance_before - balance_after if balance_before > 0 else 0

        if spent > 0.50:
            actual_shares = spent / price if price > 0 else shares
            print(f"  ✓ Balance verified: spent ${spent:.2f} "
                  f"(~{actual_shares:.0f} shares @ ${price:.3f})")
            return OrderResult(
                success=True, order_id=order_id, status=FILLED,
                side="BUY", price=price,
                amount_usd=spent, shares=actual_shares,
                token_id=token_id[:16] + "...", dry_run=False,
            )

        # Balance didn't drop — check order API as fallback
        fill = self._check_order(order_id)
        if fill:
            matched = self._extract_fill(fill, price)
            if matched:
                return OrderResult(
                    success=True, order_id=order_id, status=FILLED,
                    side="BUY", price=matched[0],
                    amount_usd=matched[1], shares=matched[2],
                    token_id=token_id[:16] + "...", dry_run=False,
                )

        # Wait one more second and try again
        time.sleep(1)
        balance_after2 = self.get_balance()
        spent2 = balance_before - balance_after2 if balance_before > 0 else 0

        if spent2 > 0.50:
            actual_shares = spent2 / price if price > 0 else shares
            print(f"  ✓ Balance verified (delayed): spent ${spent2:.2f}")
            return OrderResult(
                success=True, order_id=order_id, status=FILLED,
                side="BUY", price=price,
                amount_usd=spent2, shares=actual_shares,
                token_id=token_id[:16] + "...", dry_run=False,
            )

        # Truly unfilled
        self.cancel_order(order_id)
        return OrderResult(
            success=False, order_id=order_id, status=FAILED,
            error="Order not filled (no balance change)",
            side="BUY", price=price, token_id=token_id[:16] + "...",
        )

    # ── Sell (balance-verified, partial fill aware) ─────────────────

    def sell(self, token_id: str, shares: float, price: float = 0.0) -> OrderResult:
        """Sell shares via create_market_order.

        Verifies the sell via USDC balance change, not order status.
        Returns shares_remaining for partial fill tracking.
        Rejects if notional < $5 (Polymarket minimum) — caller should hold to resolution.
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
                shares_remaining=0.0,
                token_id=token_id[:16] + "...", dry_run=True,
            )

        if not self._initialized:
            return OrderResult(success=False, status=FAILED, error="Not initialized")

        if price <= 0:
            notional = float(sell_shares) * 0.50
            price = self.get_market_price(token_id, "SELL", notional)
            if price <= 0:
                return OrderResult(
                    success=False, status=FAILED,
                    error="Could not get sell price", side="SELL",
                    token_id=token_id[:16] + "...",
                )

        # Check minimum notional BEFORE attempting — prevents the
        # "$3.42 lower than minimum: 5" trap that strands shares
        sell_amount = round(sell_shares * price, 2)
        if sell_amount < POLY_MIN_NOTIONAL:
            return OrderResult(
                success=False, status=REJECTED,
                error=f"Notional ${sell_amount:.2f} < ${POLY_MIN_NOTIONAL:.0f} min "
                      f"— hold to resolution",
                side="SELL", price=price, shares=float(sell_shares),
                shares_remaining=float(sell_shares),
                token_id=token_id[:16] + "...",
            )

        print(f"  📊 Sell: {sell_shares} shares @ ${price:.3f} = ${sell_amount:.2f}")

        # Snapshot balance BEFORE sell
        balance_before = self.get_balance()

        try:
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=sell_amount,
                side="SELL",
            )

            signed_order = self.client.create_market_order(order_args)
            result = self.client.post_order(signed_order, OrderType.GTC)
            order_id = result.get("orderID", "")

            # Wait for settlement
            time.sleep(2)

            # Verify via balance change (the source of truth)
            balance_after = self.get_balance()
            received = balance_after - balance_before

            if received > 0.10:  # Got some USDC back
                # Estimate shares sold from received amount
                shares_sold = received / price if price > 0 else 0
                shares_left = max(0, sell_shares - shares_sold)

                status = FILLED if shares_left < 1 else PARTIAL
                if status == PARTIAL:
                    print(f"  ⚠️  Partial fill: sold ~{shares_sold:.0f} of {sell_shares}, "
                          f"~{shares_left:.0f} remaining")

                return OrderResult(
                    success=True, order_id=order_id or "balance-verified",
                    status=status, side="SELL", price=price,
                    amount_usd=received, shares=shares_sold,
                    shares_remaining=shares_left,
                    token_id=token_id[:16] + "...", dry_run=False,
                )

            # No balance change — check order status as fallback
            if order_id:
                fill = self._check_order(order_id)
                if fill:
                    matched = self._extract_fill(fill, price)
                    if matched:
                        return OrderResult(
                            success=True, order_id=order_id, status=FILLED,
                            side="SELL", price=matched[0],
                            amount_usd=matched[1], shares=matched[2],
                            shares_remaining=max(0, sell_shares - matched[2]),
                            token_id=token_id[:16] + "...", dry_run=False,
                        )

            # Nothing worked
            if order_id:
                self.cancel_order(order_id)
            return OrderResult(
                success=False, order_id=order_id or "", status=FAILED,
                error="Sell not verified (no balance change)",
                side="SELL", price=price, token_id=token_id[:16] + "...",
            )

        except Exception as e:
            # Even on exception, check if balance changed (ghost sell)
            time.sleep(1)
            balance_after = self.get_balance()
            received = balance_after - balance_before
            if received > 0.10:
                shares_sold = received / price if price > 0 else 0
                shares_left = max(0, sell_shares - shares_sold)
                print(f"  👻 Ghost sell! Got ${received:.2f} despite error")
                return OrderResult(
                    success=True, order_id="ghost-sell",
                    status=PARTIAL if shares_left >= 1 else FILLED,
                    side="SELL", price=price,
                    amount_usd=received, shares=shares_sold,
                    shares_remaining=shares_left,
                    token_id=token_id[:16] + "...", dry_run=False,
                )

            return OrderResult(
                success=False, status=FAILED, error=str(e),
                side="SELL", price=price, token_id=token_id[:16] + "...",
            )

    # ── Helpers ──────────────────────────────────────────────────────

    def _extract_fill(self, fill: dict, fallback_price: float) -> Optional[tuple]:
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
