"""Order executor for Polymarket CLOB.

Handles the full order lifecycle: place → verify fill → resolve.
Uses py-clob-client for all Polymarket interactions.

Key principle: an order accepted by the book is NOT the same as an order
that was filled. We never lie about what actually happened.
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


# ── Order lifecycle states ──────────────────────────────────────────

PLACED = "PLACED"       # Order accepted by the book, not yet matched
FILLED = "FILLED"       # Fully matched — shares are ours
PARTIAL = "PARTIAL"     # Some shares matched, some still on book
CANCELLED = "CANCELLED" # We cancelled it or it expired
FAILED = "FAILED"       # Never made it to the book


@dataclass
class OrderResult:
    """Tracks an order through its entire lifecycle.

    `success` means the order was PLACED on the book.
    `filled` means shares were actually MATCHED and we own them.
    These are different things — that distinction is the whole point.
    """
    success: bool            # Was it placed on the book?
    filled: bool = False     # Were shares actually matched?
    order_id: str = ""
    status: str = FAILED
    side: str = ""
    price: float = 0.0
    size_requested: float = 0.0   # Shares we asked for
    size_filled: float = 0.0      # Shares we actually got
    amount_spent: float = 0.0     # Actual USD committed
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

    # ── Place order ─────────────────────────────────────────────────

    def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        amount_usd: float,
    ) -> OrderResult:
        """Place a limit order on the book.

        IMPORTANT: A successful return means the order was PLACED, not FILLED.
        Call check_order() afterwards to verify if it actually matched.
        """
        shares = amount_usd / price

        if self.dry_run:
            return OrderResult(
                success=True,
                filled=True,  # Dry run assumes instant fill
                order_id=f"DRY-{int(time.time())}",
                status=FILLED,
                side=side,
                price=price,
                size_requested=shares,
                size_filled=shares,
                amount_spent=amount_usd,
                token_id=token_id[:16] + "...",
                dry_run=True,
            )

        if not self._initialized:
            return OrderResult(success=False, error="Client not initialized")

        try:
            order_args = OrderArgs(
                price=price,
                size=shares,
                side=side,
                token_id=token_id,
            )

            signed_order = self.client.create_order(order_args)
            result = self.client.post_order(signed_order, OrderType.GTC)

            order_id = result.get("orderID", "")
            if not order_id:
                return OrderResult(
                    success=False,
                    status=FAILED,
                    error="No orderID in response",
                    side=side,
                    price=price,
                    token_id=token_id[:16] + "...",
                )

            return OrderResult(
                success=True,
                filled=False,  # We don't know yet — must check
                order_id=order_id,
                status=PLACED,
                side=side,
                price=price,
                size_requested=shares,
                size_filled=0.0,
                amount_spent=0.0,
                token_id=token_id[:16] + "...",
                dry_run=False,
            )

        except Exception as e:
            return OrderResult(
                success=False,
                status=FAILED,
                error=str(e),
                side=side,
                price=price,
                token_id=token_id[:16] + "...",
            )

    # ── Check order fill status ─────────────────────────────────────

    def check_order(self, order_id: str) -> OrderResult:
        """Check whether a placed order has been filled.

        Queries the CLOB for the order's current state and returns
        an honest account of what actually happened.
        """
        if not order_id or order_id.startswith("DRY-"):
            return OrderResult(success=True, filled=True, status=FILLED, dry_run=True)

        if not self._initialized:
            return OrderResult(success=False, error="Client not initialized")

        try:
            order = self.client.get_order(order_id)

            status_raw = order.get("status", "UNKNOWN").upper()
            size_matched = float(order.get("size_matched", 0))
            original_size = float(order.get("original_size", order.get("size", 0)))
            price = float(order.get("price", 0))
            side = order.get("side", "")
            token_id = order.get("asset_id", order.get("token_id", ""))

            # Determine our canonical status
            if status_raw in ("MATCHED", "FILLED"):
                status = FILLED
                filled = True
            elif size_matched > 0:
                status = PARTIAL
                filled = True  # Partially filled still counts
            elif status_raw in ("CANCELLED", "EXPIRED"):
                status = CANCELLED
                filled = False
            else:
                status = PLACED  # Still sitting on book
                filled = False

            return OrderResult(
                success=True,
                filled=filled,
                order_id=order_id,
                status=status,
                side=side,
                price=price,
                size_requested=original_size,
                size_filled=size_matched,
                amount_spent=size_matched * price,
                token_id=str(token_id)[:16] + "..." if token_id else "",
                dry_run=False,
            )

        except Exception as e:
            return OrderResult(
                success=False,
                error=f"Order check failed: {e}",
                order_id=order_id,
            )

    # ── Wait for fill with polling ──────────────────────────────────

    def wait_for_fill(
        self,
        order_id: str,
        timeout: float = 30.0,
        poll_interval: float = 2.0,
    ) -> OrderResult:
        """Poll an order until it fills, times out, or is cancelled.

        Returns the final state of the order. If it didn't fill within
        the timeout, it gets cancelled automatically.
        """
        if not order_id or order_id.startswith("DRY-"):
            return OrderResult(success=True, filled=True, status=FILLED, dry_run=True)

        deadline = time.time() + timeout
        last_result = None

        while time.time() < deadline:
            result = self.check_order(order_id)
            last_result = result

            if result.filled:
                return result

            if result.status in (CANCELLED, FAILED):
                return result

            time.sleep(poll_interval)

        # Timed out — cancel the unfilled order
        print(f"  ⏰ Order {order_id[:12]}... not filled after {timeout:.0f}s, cancelling")
        self.cancel_order(order_id)

        # One final check — it might have filled between our last poll and cancel
        final = self.check_order(order_id)
        if final.filled:
            return final

        # Genuinely unfilled
        if last_result:
            last_result.status = CANCELLED
            last_result.filled = False
            return last_result

        return OrderResult(
            success=True,
            filled=False,
            order_id=order_id,
            status=CANCELLED,
            error="Timed out waiting for fill",
        )

    # ── Cancel orders ───────────────────────────────────────────────

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

    result = exe.place_order(
        token_id="fake_token_123",
        side="BUY",
        price=0.65,
        amount_usd=5.0,
    )

    print(f"Order: {result}")
    print(f"  Status: {result.status}")
    print(f"  Filled: {result.filled}")
    print(f"  Shares: {result.size_filled:.2f} / {result.size_requested:.2f}")
