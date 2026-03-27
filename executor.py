"""Order executor for Polymarket CLOB.

All orders route through the complement engine (tight spreads, real volume).
Buys use create_order(OrderArgs) with integer shares and 2-decimal prices to
avoid the float precision bug that create_market_order triggers internally
(amount/price division produces 21.000000000004 shares, rejected by CLOB).
Sells use create_market_order — the sell path doesn't have the same issue.

Fill verification uses USDC balance change as the source of truth.
Ghost fills (order went through despite API exception) are caught via
balance snapshot before/after. Unverified buys are never cancelled —
the bot detects them via balance sync at the next window boundary.
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

MIN_SHARES = 1.0
MIN_AMOUNT_USD = 1.0
MAX_BUY_PRICE = 0.90  # Allow high-conviction buys — profit comes from resolution at $1.00
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
    """Integer shares × price = clean 2-decimal USD amount.

    If flooring to integer shares drops below POLY_MIN_NOTIONAL ($5),
    round UP one share so the order isn't rejected.
    """
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

    # If integer-share rounding dropped below $5 min, bump up one share
    if spend < POLY_MIN_NOTIONAL and shares >= MIN_SHARES:
        shares += 1
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

    # ── Buy (market order via complement engine) ─────────────────────

    def buy(self, token_id: str, amount_usd: float, price: float = 0.0) -> OrderResult:
        """Buy via create_order + OrderArgs (limit order, complement engine).

        Uses explicit integer shares and 2-decimal price to avoid
        float precision errors that create_market_order produces
        internally (amount/price division → 21.000000000004 shares).

        If price > 0, skips the internal get_market_price fetch (caller already
        has a fresh price, saves one Tor roundtrip at execution time).
        """
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

        if price > 0:
            market_price = round(price, 2)
        else:
            market_price = self.get_market_price(token_id, "BUY", amount_usd)
            if market_price <= 0:
                return OrderResult(
                    success=False, status=FAILED,
                    error="Could not get market price", side="BUY",
                    token_id=token_id[:16] + "...",
                )
            # Kill float artifacts: 0.7200000001 → 0.72
            market_price = round(market_price, 2)

        # Price cap: don't buy above MAX_BUY_PRICE
        if market_price > MAX_BUY_PRICE:
            return OrderResult(
                success=False, status=REJECTED,
                error=f"Price ${market_price:.3f} > cap ${MAX_BUY_PRICE:.2f} "
                      f"(TP impossible above this)",
                side="BUY", price=market_price, token_id=token_id[:16] + "...",
            )

        # Compute clean amounts with integer-cents math to avoid any
        # floating-point precision that can trip Polymarket's
        # "maker/taker accuracy" validation.
        # - maker (shares): ≤ 4 decimals (we use integers)
        # - taker (USDC):  ≤ 2 decimals (we use integer cents)
        shares, clean_amount = calculate_order_size(market_price, amount_usd)
        if shares < 1 or clean_amount <= 0:
            return OrderResult(
                success=False, status=REJECTED,
                error=f"Can't afford 1 share at ${market_price:.3f} "
                      f"within ${amount_usd:.2f}",
                side="BUY", price=market_price, token_id=token_id[:16] + "...",
            )

        # Re-check minimum notional with clean amount
        if clean_amount < POLY_MIN_NOTIONAL:
            return OrderResult(
                success=False, status=REJECTED,
                error=f"Amount ${clean_amount:.2f} < ${POLY_MIN_NOTIONAL:.0f} min",
                side="BUY", price=market_price, token_id=token_id[:16] + "...",
            )

        print(f"  📊 Market price: ${market_price:.3f}/share "
              f"→ {int(shares)} shares for ${clean_amount:.2f}")

        # Snapshot balance BEFORE buy
        balance_before = self.get_balance()

        try:
            # Use create_order + OrderArgs with explicit price/size.
            # NOT create_market_order — that internally divides
            # amount/price producing 21.000000000004 shares, which
            # the CLOB rejects as "invalid amounts, max accuracy 4 decimals".
            order_args = OrderArgs(
                token_id=token_id,
                price=market_price,       # Already rounded to 2 decimals
                size=float(int(shares)),  # Integer shares as float
                side="BUY",
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

            # Limit orders route through complement engine in 5-15s.
            # Wait for balance to settle, then verify.
            time.sleep(5)
            return self._verify_buy_via_balance(
                order_id, market_price, float(shares), token_id, balance_before,
            )

        except Exception as e:
            # Ghost fill defense: order may have gone through despite exception
            time.sleep(3)
            balance_after = self.get_balance()
            spent = balance_before - balance_after if balance_before > 0 else 0

            if spent > 1.0:
                # Cap at intended cost — concurrent settlements can inflate delta
                intended_cost = shares * market_price
                spent = min(spent, intended_cost)
                actual_shares = min(spent / market_price, shares) if market_price > 0 else 0
                print(f"  👻 GHOST BUY: balance dropped ${spent:.2f} despite error")
                return OrderResult(
                    success=True, order_id="ghost-buy",
                    status=FILLED, side="BUY", price=market_price,
                    amount_usd=spent, shares=actual_shares,
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
        """Verify buy fill. Tries 3 rounds of balance + order API checks.

        CRITICAL: never cancels on timeout — Polygon settlement can take 5-15s.
        If we can't verify, we return the order details so the bot can
        retroactively detect the fill via balance sync at window boundary.
        """
        for attempt in range(3):
            # Check balance (source of truth once chain settles)
            balance_after = self.get_balance()
            spent = balance_before - balance_after if balance_before > 0 else 0

            if spent > 0.50:
                # Cap at intended cost — concurrent settlements can inflate delta
                intended_cost = shares * price
                spent = min(spent, intended_cost)
                actual_shares = min(spent / price, shares) if price > 0 else shares
                suffix = f" (attempt {attempt+1})" if attempt > 0 else ""
                print(f"  ✓ Balance verified{suffix}: spent ${spent:.2f} "
                      f"(~{actual_shares:.0f} shares @ ${price:.3f})")
                return OrderResult(
                    success=True, order_id=order_id, status=FILLED,
                    side="BUY", price=price,
                    amount_usd=spent, shares=actual_shares,
                    token_id=token_id[:16] + "...", dry_run=False,
                )

            # Check order API (updates faster than chain balance)
            fill = self._check_order(order_id)
            if fill:
                matched = self._extract_fill(fill, price)
                if matched:
                    suffix = f" (attempt {attempt+1})" if attempt > 0 else ""
                    print(f"  ✓ Order API verified{suffix}: "
                          f"{matched[2]:.0f} shares @ ${matched[0]:.3f}")
                    return OrderResult(
                        success=True, order_id=order_id, status=FILLED,
                        side="BUY", price=matched[0],
                        amount_usd=matched[1], shares=matched[2],
                        token_id=token_id[:16] + "...", dry_run=False,
                    )

            if attempt < 2:
                time.sleep(3)  # Wait 3s between attempts

        # After 3 rounds (~11s since order): still can't verify.
        # DO NOT CANCEL — the order likely filled but chain hasn't settled.
        # Return details so bot can detect it via balance sync.
        print(f"  ⏳ Buy unverified after {3*3+5}s — NOT cancelling "
              f"(Polygon may still be settling)")
        return OrderResult(
            success=False, order_id=order_id, status=FAILED,
            error="UNVERIFIED_BUY",  # Special marker for bot to handle
            side="BUY", price=price, amount_usd=shares * price,
            shares=shares, token_id=token_id[:16] + "...",
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
                # Cap at intended revenue — concurrent settlements can inflate delta
                intended_revenue = sell_shares * price
                received = min(received, intended_revenue)
                shares_sold = min(received / price, sell_shares) if price > 0 else 0
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
                # Cap at intended revenue — concurrent settlements can inflate delta
                intended_revenue = sell_shares * price
                received = min(received, intended_revenue)
                shares_sold = min(received / price, sell_shares) if price > 0 else 0
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
