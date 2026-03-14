"""Order executor for Polymarket CLOB.

Handles placing maker limit orders with dry-run support.
Uses py-clob-client for all Polymarket interactions.
"""

import os
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


@dataclass
class OrderResult:
    success: bool
    order_id: str = ""
    side: str = ""
    price: float = 0.0
    size: float = 0.0
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

    def initialize(self) -> bool:
        """Initialize the CLOB client and derive API credentials."""
        try:
            # Create client - funder is the safe/proxy address for gasless trades
            self.client = ClobClient(
                host="https://clob.polymarket.com",
                key=self.private_key,
                chain_id=POLYGON,
                funder=self.safe_address if self.safe_address else None,
                signature_type=2 if self.safe_address else 0,
            )
            
            # Derive or create API credentials
            self.client.set_api_creds(self.client.create_or_derive_api_creds())
            
            self._initialized = True
            print(f"[executor] Initialized ({'DRY RUN' if self.dry_run else 'LIVE'})")
            print(f"[executor] Address: {self.client.get_address()}")
            return True
            
        except Exception as e:
            print(f"[executor] Failed to initialize: {e}")
            return False

    def get_balance(self) -> float:
        """Get current USDC balance on Polymarket."""
        if not self._initialized:
            return 0.0
        try:
            # Query USDC collateral balance/allowance
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            bal = self.client.get_balance_allowance(params)
            return float(bal.get("balance", 0)) / 1e6  # USDC has 6 decimals
        except Exception as e:
            print(f"[executor] Balance check failed: {e}")
            return 0.0

    def get_orderbook_price(self, token_id: str) -> tuple[float, float]:
        """Get best bid and ask for a token.
        
        Returns (best_bid, best_ask). Returns (0, 0) on error.
        """
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

    def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        amount_usd: float,
    ) -> OrderResult:
        """Place a maker limit order.
        
        Args:
            token_id: The CLOB token ID to trade
            side: "BUY" 
            price: Limit price (e.g. 0.65 = 65 cents per share)
            amount_usd: Total USD to spend
            
        Returns:
            OrderResult with success status and details.
        """
        # Calculate number of shares: amount / price
        shares = amount_usd / price
        
        if self.dry_run:
            return OrderResult(
                success=True,
                order_id=f"DRY-{int(time.time())}",
                side=side,
                price=price,
                size=shares,
                token_id=token_id[:16] + "...",
                dry_run=True,
            )
        
        if not self._initialized:
            return OrderResult(success=False, error="Client not initialized")
        
        try:
            # Build the order
            order_args = OrderArgs(
                price=price,
                size=shares,
                side=side,
                token_id=token_id,
            )
            
            signed_order = self.client.create_order(order_args)
            result = self.client.post_order(signed_order, OrderType.GTC)
            
            order_id = result.get("orderID", "unknown")
            
            return OrderResult(
                success=True,
                order_id=order_id,
                side=side,
                price=price,
                size=shares,
                token_id=token_id[:16] + "...",
                dry_run=False,
            )
            
        except Exception as e:
            return OrderResult(
                success=False,
                error=str(e),
                side=side,
                price=price,
                token_id=token_id[:16] + "...",
            )

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
    # Dry run test
    exe = Executor(
        private_key="0x" + "a" * 64,  # Fake key for testing
        dry_run=True,
    )
    
    result = exe.place_order(
        token_id="fake_token_123",
        side="BUY",
        price=0.65,
        amount_usd=5.0,
    )
    
    print(f"Order: {result}")
    print(f"  Shares: {result.size:.2f} @ ${result.price:.2f} = ${result.size * result.price:.2f}")
