"""Order executor for Polymarket CLOB.

v8 — Market orders via complement matching engine.

Previous versions placed limit orders on the raw token book, which has
a $0.94/$0.06 void in the middle. All real volume flows through the
complement engine, which matches UP buyers with DOWN sellers.

Now we use:
  - calculate_market_price() to preview what we'd actually pay
  - create_market_order() + post_order() to fill instantly
  - Same methods for selling (exit + claim)
"""

import time
from dataclasses import dataclass
from typing import Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    MarketOrderArgs,
    OrderType,
    BalanceAllowanceParams,
    AssetType,
)
from py_clob_client.constants import POLYGON


# ── Constants ───────────────────────────────────────────────────────

FILLED = "FILLED"
REJECTED = "REJECTED"
FAILED = "FAILED"

MIN_AMOUNT_USD = 1.0   # Minimum trade in USD


@dataclass
class OrderResult:
    success: bool
    order_id: str = ""
    status: str = FAILED
    side: str = ""
    price: float = 0.0          # Effective price per share
    amount_usd: float = 0.0     # USD spent (buy) or received (sell)
    shares: float = 0.0         # Shares received (buy) or sold (sell)
    token_id: str = ""
    error: str = ""
    dry_run: bool = True


class Executor:
    def __init__(self, private_key: str, safe_address: str = "", dry_run: bool = True):
        self.dry_run = dry_run
        self.private_key = private_key
        self.safe_address = safe_address
        self.client: Optional[ClobClient] = None
        self._initialized = False

    # ── Init ────────────────────────────────────────────────────────

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

    # ── Price check (merged book via complement engine) ─────────────

    def get_market_price(self, token_id: str, side: str, amount_usd: float) -> float:
        """Get the effective price we'd pay/receive for a market order.

        This queries the MERGED book (complement engine) — the same
        book the Polymarket UI shows. Returns price per share.
        Returns 0.0 on error.
        """
        if not self._initialized:
            return 0.0
        try:
            price = self.client.calculate_market_price(
                token_id=token_id,
                side=side,
                amount=amount_usd,
                order_type=OrderType.FOK,
            )
            return float(price) if price else 0.0
        except Exception as e:
            print(f"[executor] Price check failed: {e}")
            return 0.0

    # ── Buy (market order) ──────────────────────────────────────────

    def buy(self, token_id: str, amount_usd: float) -> OrderResult:
        """Buy shares at market price via complement engine.

        amount_usd: how much to spend.
        Returns shares received and effective price.
        """
        amount_usd = float(amount_usd)
        if amount_usd < MIN_AMOUNT_USD:
            return OrderResult(
                success=False, status=REJECTED,
                error=f"Amount ${amount_usd:.2f} below minimum ${MIN_AMOUNT_USD:.2f}",
                side="BUY",
            )

        # Round to 2 decimals (API requirement)
        amount_usd = round(amount_usd, 2)

        # ── Dry run ─────────────────────────────────────────────────
        if self.dry_run:
            # Simulate: assume we'd pay roughly $0.55 per share
            sim_price = 0.55
            sim_shares = amount_usd / sim_price
            return OrderResult(
                success=True, order_id=f"DRY-{int(time.time())}",
                status=FILLED, side="BUY",
                price=sim_price, amount_usd=amount_usd,
                shares=sim_shares,
                token_id=token_id[:16] + "...", dry_run=True,
            )

        if not self._initialized:
            return OrderResult(success=False, status=FAILED, error="Not initialized")

        # ── Preview price ───────────────────────────────────────────
        preview_price = self.get_market_price(token_id, "BUY", amount_usd)
        if preview_price > 0:
            preview_shares = amount_usd / preview_price
            print(f"  📊 Market price: ${preview_price:.3f}/share "
                  f"→ ~{preview_shares:.0f} shares for ${amount_usd:.2f}")

        # ── Send market order ───────────────────────────────────────
        try:
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=amount_usd,
                side="BUY",
            )

            signed_order = self.client.create_market_order(order_args)
            result = self.client.post_order(signed_order, OrderType.GTC)

            order_id = result.get("orderID", "")
            if not order_id:
                return OrderResult(
                    success=False, status=REJECTED,
                    error="No orderID in response",
                    side="BUY", token_id=token_id[:16] + "...",
                )

            # Verify fill
            fill = self._check_order(order_id)
            if fill:
                size_matched = float(
                    fill.get("size_matched", 0) if isinstance(fill, dict)
                    else getattr(fill, "size_matched", 0)
                )
                if size_matched > 0:
                    fill_price = float(
                        fill.get("price", preview_price) if isinstance(fill, dict)
                        else getattr(fill, "price", preview_price)
                    )
                    actual_price = amount_usd / size_matched if size_matched > 0 else fill_price
                    return OrderResult(
                        success=True, order_id=order_id,
                        status=FILLED, side="BUY",
                        price=actual_price, amount_usd=amount_usd,
                        shares=size_matched,
                        token_id=token_id[:16] + "...", dry_run=False,
                    )

            return OrderResult(
                success=False, order_id=order_id, status=FAILED,
                error="Market order not filled — unexpected",
                side="BUY", token_id=token_id[:16] + "...",
            )

        except Exception as e:
            return OrderResult(
                success=False, status=FAILED, error=str(e),
                side="BUY", token_id=token_id[:16] + "...",
            )

    # ── Sell (market order) ─────────────────────────────────────────

    def sell(self, token_id: str, amount_usd: float) -> OrderResult:
        """Sell shares at market price. amount_usd = notional value to sell."""
        amount_usd = float(round(amount_usd, 2))
        if amount_usd < MIN_AMOUNT_USD:
            return OrderResult(
                success=False, status=REJECTED,
                error=f"Amount ${amount_usd:.2f} below minimum",
                side="SELL",
            )

        if self.dry_run:
            sim_price = 0.95
            sim_shares = amount_usd / sim_price
            return OrderResult(
                success=True, order_id=f"DRY-SELL-{int(time.time())}",
                status=FILLED, side="SELL",
                price=sim_price, amount_usd=amount_usd,
                shares=sim_shares,
                token_id=token_id[:16] + "...", dry_run=True,
            )

        if not self._initialized:
            return OrderResult(success=False, status=FAILED, error="Not initialized")

        # Preview
        preview_price = self.get_market_price(token_id, "SELL", amount_usd)
        if preview_price > 0:
            print(f"  📊 Sell price: ${preview_price:.3f}/share")

        try:
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=amount_usd,
                side="SELL",
            )

            signed_order = self.client.create_market_order(order_args)
            result = self.client.post_order(signed_order, OrderType.GTC)

            order_id = result.get("orderID", "")
            if not order_id:
                return OrderResult(
                    success=False, status=REJECTED,
                    error="No orderID from sell",
                    side="SELL", token_id=token_id[:16] + "...",
                )

            fill = self._check_order(order_id)
            if fill:
                size_matched = float(
                    fill.get("size_matched", 0) if isinstance(fill, dict)
                    else getattr(fill, "size_matched", 0)
                )
                if size_matched > 0:
                    fill_price = float(
                        fill.get("price", preview_price) if isinstance(fill, dict)
                        else getattr(fill, "price", preview_price)
                    )
                    received = size_matched * fill_price
                    return OrderResult(
                        success=True, order_id=order_id,
                        status=FILLED, side="SELL",
                        price=fill_price, amount_usd=received,
                        shares=size_matched,
                        token_id=token_id[:16] + "...", dry_run=False,
                    )

            return OrderResult(
                success=False, order_id=order_id, status=FAILED,
                error="Sell not filled",
                side="SELL", token_id=token_id[:16] + "...",
            )

        except Exception as e:
            return OrderResult(
                success=False, status=FAILED, error=str(e),
                side="SELL", token_id=token_id[:16] + "...",
            )

    # ── Helpers ─────────────────────────────────────────────────────

    def _check_order(self, order_id: str) -> Optional[dict]:
        if not self._initialized:
            return None
        try:
            return self.client.get_order(order_id)
        except Exception as e:
            print(f"[executor] Order check failed: {e}")
            return None

    def cancel_all(self) -> bool:
        if self.dry_run or not self._initialized:
            return True
        try:
            self.client.cancel_all()
            return True
        except Exception as e:
            print(f"[executor] Cancel all failed: {e}")
            return False
