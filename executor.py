"""Order executor for Polymarket's CLOB, on the unified SDK (polymarket-client).

Live mode only talks to Polymarket through this module. Dry run never connects:
it fills on paper at real order-book prices (see buy()).

Wallet
  The client is always created for SAFE_ADDRESS, and live trading is refused
  unless the SDK classifies it as GNOSIS_SAFE (signature type 2). Without a
  wallet the SDK would deploy a new Deposit Wallet, and an address it doesn't
  recognise falls back to a session-key signing path; neither is wanted here.
  Trading also requires the new CLOB V2 approvals to be in place.

Buys
  Fill-and-kill (FAK) market orders capped at a maximum price: whatever is
  available at or below the cap fills immediately, the rest is cancelled, and
  nothing rests on the book. The order response says what filled. If the
  request fails in a way where the order may still have reached the exchange,
  the token balance is checked once to find out.

Winnings
  redeem() turns resolved winning positions back into pUSD through the SDK's
  gasless relayer, which needs a builder API key (BUILDER_API_* in .env).
  Without one, winnings stay redeemable and can be claimed in the Polymarket UI.
"""

import time
from dataclasses import dataclass
from typing import Optional

import httpx
from polymarket import SecureClient
from polymarket import errors as pm_errors
from polymarket.auth import BuilderApiKey

from market import walk_book


FILLED = "FILLED"
PARTIAL = "PARTIAL"
REJECTED = "REJECTED"
FAILED = "FAILED"

MIN_SHARES = 1.0
MIN_AMOUNT_USD = 1.0
MAX_BUY_PRICE = 0.90  # Allow high-conviction buys — profit comes from resolution at $1.00
POLY_MIN_NOTIONAL = 5.0  # Polymarket rejects orders below $5 notional

CLOB_HOST = "https://clob.polymarket.com"
BASE_UNITS = 1_000_000          # pUSD and outcome shares both use 6 decimals
DELAYED_CHECKS = 4              # get_order checks when Polymarket reports "delayed"
DELAYED_CHECK_INTERVAL = 2.0    # seconds
# Errors that mean the order was definitely not placed (no fill to look for)
_NOT_PLACED = (pm_errors.UserInputError, pm_errors.RequestRejectedError)


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
    """Integer shares × price = clean 2-decimal USD amount (used for paper fills).

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
    def __init__(self, private_key: str, safe_address: str = "", dry_run: bool = True,
                 builder_key: Optional[BuilderApiKey] = None):
        self.private_key = private_key
        self.safe_address = safe_address
        self.dry_run = dry_run
        self.builder_key = builder_key
        self.client: Optional[SecureClient] = None
        self.wallet_type = ""
        self._initialized = False
        self._redeem_submitted: set = set()   # condition_ids already sent for redemption

    @property
    def can_redeem(self) -> bool:
        return self.builder_key is not None

    # ── Connection ───────────────────────────────────────────────────

    def initialize(self) -> bool:
        """Connect for live trading. Returns False (and says why) unless the
        wallet is the Polymarket Safe for this key and trading is approved."""
        if not self.private_key or not self.safe_address:
            print("[executor] PRIVATE_KEY and SAFE_ADDRESS are both required for live trading")
            return False
        try:
            self.client = SecureClient.create(
                private_key=self.private_key,
                wallet=self.safe_address,
                api_key=self.builder_key,
            )
        except Exception as e:
            print(f"[executor] Could not connect to Polymarket: {type(e).__name__}: {e}")
            return False

        self.wallet_type = self.client.wallet_type
        if self.wallet_type != "GNOSIS_SAFE":
            print(f"[executor] REFUSING to trade: the SDK sees SAFE_ADDRESS as {self.wallet_type}, "
                  f"not GNOSIS_SAFE. Check SAFE_ADDRESS is the Polymarket Safe for this PRIVATE_KEY.")
            self.close()
            return False

        try:
            approvals = self.client.get_trading_approvals_state()
        except Exception as e:
            print(f"[executor] Could not read trading approvals: {type(e).__name__}: {e}")
            self.close()
            return False
        if not approvals.is_fully_approved:
            print(f"[executor] REFUSING to trade: CLOB V2 trading approvals are missing "
                  f"({approvals.missing}). Enable trading in the Polymarket UI, then run preflight.py.")
            self.close()
            return False

        self._initialized = True
        print(f"[executor] Connected: Safe {self.safe_address[:6]}…{self.safe_address[-4:]} "
              f"(GNOSIS_SAFE), trading approved, "
              f"{'redeems winnings' if self.can_redeem else 'no builder key: claim winnings in the UI'}")
        return True

    def close(self):
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
        self.client = None
        self._initialized = False

    def health_check(self):
        """Raise if the CLOB API isn't answering (replaces py-clob-client's get_ok()).
        In live mode httpx is routed through the proxy by proxy.apply_proxy()."""
        r = httpx.get(f"{CLOB_HOST}/", timeout=10)
        r.raise_for_status()

    # ── Balances and prices ──────────────────────────────────────────

    def read_balance(self) -> Optional[float]:
        """pUSD collateral balance in USD, or None if it can't be read
        (a real balance can be exactly 0 while everything is staked)."""
        if not self._initialized:
            return None
        try:
            return self.client.get_balance_allowance(asset_type="COLLATERAL").balance / BASE_UNITS
        except Exception as e:
            print(f"[executor] Balance check failed: {type(e).__name__}: {e}")
            return None

    def get_balance(self) -> float:
        """pUSD collateral balance in USD (0.0 if it can't be read)."""
        return self.read_balance() or 0.0

    def token_balance(self, token_id: str) -> float:
        """Shares of one outcome token held by the Safe (0.0 if unknown)."""
        try:
            return self.client.get_balance_allowance(
                asset_type="CONDITIONAL", token_id=token_id).balance / BASE_UNITS
        except Exception as e:
            print(f"[executor] Token balance check failed: {type(e).__name__}: {e}")
            return 0.0

    def get_market_price(self, token_id: str, side: str, amount: float) -> float:
        """Price to BUY `amount` USD or SELL `amount` shares, from the live book."""
        if not self._initialized:
            return 0.0
        try:
            book = self.client.get_order_book(token_id=token_id)
        except Exception as e:
            print(f"[executor] Price check failed: {type(e).__name__}: {e}")
            return 0.0
        asks = [(float(lv.price), float(lv.size)) for lv in book.asks]
        bids = [(float(lv.price), float(lv.size)) for lv in book.bids]
        return walk_book(asks, bids, side, amount)

    def redeemable_winnings(self) -> list:
        """[(condition_id, value_usd)] for resolved positions worth redeeming."""
        if not self._initialized:
            return []
        try:
            positions = self.client.list_positions(user=self.safe_address, status="REDEEMABLE")
            return [(p.condition_id, float(p.current_value)) for p in positions.iter_items()
                    if p.redeemable and p.current_value > 0]
        except Exception as e:
            print(f"[executor] Position check failed: {type(e).__name__}: {e}")
            return []

    # ── Orders ───────────────────────────────────────────────────────

    def buy(self, token_id: str, amount_usd: float, price: float = 0.0,
            max_price: float = 0.0) -> OrderResult:
        """Buy up to amount_usd of a token at no more than max_price.

        Live: a fill-and-kill market order. Dry run: a paper fill at `price`
        (the real book price the caller saw), sized exactly like a live order.
        """
        amount_usd = round(float(amount_usd), 2)
        tok = token_id[:16] + "..."
        if amount_usd < MIN_AMOUNT_USD:
            return OrderResult(success=False, status=REJECTED, side="BUY",
                               error=f"Amount ${amount_usd:.2f} below min")
        cap = MAX_BUY_PRICE if max_price <= 0 else min(max_price, MAX_BUY_PRICE)

        if self.dry_run:
            if price <= 0:
                return OrderResult(success=False, status=FAILED, side="BUY", token_id=tok,
                                   error="No market price for paper fill")
            market_price = round(price, 2)
            if market_price > round(cap, 2):
                return OrderResult(success=False, status=REJECTED, side="BUY", token_id=tok,
                                   price=market_price,
                                   error=f"Price ${market_price:.2f} above cap ${cap:.2f}")
            shares, spend = calculate_order_size(market_price, amount_usd)
            if shares < 1 or spend < POLY_MIN_NOTIONAL:
                return OrderResult(success=False, status=REJECTED, side="BUY", token_id=tok,
                                   price=market_price,
                                   error=f"Can't size an order ≥ ${POLY_MIN_NOTIONAL:.0f} "
                                         f"from ${amount_usd:.2f} at ${market_price:.2f}")
            print(f"  📊 Market price: ${market_price:.3f}/share → {int(shares)} shares for ${spend:.2f}")
            return OrderResult(success=True, order_id=f"DRY-{int(time.time())}", status=FILLED,
                               side="BUY", price=market_price, amount_usd=spend, shares=shares,
                               token_id=tok, dry_run=True)

        if not self._initialized:
            return OrderResult(success=False, status=FAILED, side="BUY", error="Not initialized")
        if amount_usd < POLY_MIN_NOTIONAL:
            return OrderResult(success=False, status=REJECTED, side="BUY", token_id=tok,
                               error=f"Amount ${amount_usd:.2f} < ${POLY_MIN_NOTIONAL:.0f} min")
        cap = round(cap, 2)
        print(f"  📊 FAK buy: up to ${amount_usd:.2f} at ≤ ${cap:.2f}/share")
        try:
            resp = self.client.place_market_order(
                token_id=token_id, side="BUY", amount=f"{amount_usd:.2f}",
                max_price=f"{cap:.2f}", order_type="FAK",
            )
        except _NOT_PLACED as e:
            return OrderResult(success=False, status=FAILED, side="BUY", token_id=tok,
                               error=f"order not placed: {type(e).__name__}: {e}")
        except Exception as e:
            return self._after_uncertain_buy(token_id, cap, e)

        if not resp.ok:
            no_fill = resp.code in ("fak_not_filled", "fok_not_filled", "unmatched")
            return OrderResult(success=False, status=REJECTED if no_fill else FAILED,
                               side="BUY", token_id=tok, error=f"{resp.code}: {resp.message}")

        spent, shares = float(resp.making_amount), float(resp.taking_amount)
        if resp.status != "matched" or shares <= 0:
            spent, shares = self._settle(resp.order_id, token_id, cap, spent, shares)
        if shares > 0 and spent > shares:   # BUY: shares cost < $1 each, so shares > spent
            print(f"  ⚠️  Fill amounts look swapped (making {spent}, taking {shares}); swapping")
            spent, shares = shares, spent
        if shares < MIN_SHARES:
            return OrderResult(success=False, status=REJECTED, order_id=resp.order_id,
                               side="BUY", token_id=tok, error="FAK order filled nothing")
        return OrderResult(success=True, order_id=resp.order_id, status=FILLED, side="BUY",
                           price=spent / shares, amount_usd=spent, shares=shares,
                           token_id=tok, dry_run=False)

    def _settle(self, order_id: str, token_id: str, cap: float,
                spent: float, shares: float) -> tuple:
        """For a 'delayed' (or unreported) match, ask the exchange what filled."""
        for _ in range(DELAYED_CHECKS):
            time.sleep(DELAYED_CHECK_INTERVAL)
            try:
                order = self.client.get_order(order_id=order_id)
            except Exception:
                break   # no longer an open order: fall back to the token balance
            matched = float(order.size_matched)
            if matched > 0:
                return matched * float(order.price), matched
            if order.status not in ("delayed", "live"):
                break
        held = self.token_balance(token_id)
        if held > shares:
            print(f"  ⚠️  Fill read from token balance: {held:.2f} shares "
                  f"(cost estimated at the ${cap:.2f} cap)")
            return held * cap, held
        return spent, shares

    def _after_uncertain_buy(self, token_id: str, cap: float, err: Exception) -> OrderResult:
        """The request failed in a way where the order may still have reached
        the exchange. FAK orders can't rest, so either we hold shares now or
        nothing happened. (The bot holds no shares of this window's tokens
        before its one buy.)"""
        print(f"  ⚠️  Buy request failed ({type(err).__name__}: {err}); checking for a fill...")
        time.sleep(3)
        held = self.token_balance(token_id)
        tok = token_id[:16] + "..."
        if held >= MIN_SHARES:
            print(f"  👻 Order went through despite the error: {held:.2f} shares "
                  f"(cost estimated at the ${cap:.2f} cap)")
            return OrderResult(success=True, status=FILLED, side="BUY", price=cap,
                               amount_usd=held * cap, shares=held, token_id=tok,
                               dry_run=False, error=f"filled despite error: {err}")
        return OrderResult(success=False, status=FAILED, side="BUY", token_id=tok,
                           error=f"network: {type(err).__name__}: {err}")

    def redeem(self, condition_id: str) -> Optional[str]:
        """Submit redemption of a resolved market's winning shares (once per
        market). Returns the relayer transaction id, or None if not submitted."""
        if not self._initialized or not self.can_redeem or condition_id in self._redeem_submitted:
            return None
        try:
            handle = self.client.redeem_positions(condition_id=condition_id)
        except Exception as e:
            print(f"[executor] Redeem failed for {condition_id[:12]}…: {type(e).__name__}: {e}")
            return None
        self._redeem_submitted.add(condition_id)
        return handle.transaction_id

    def cancel_all(self) -> bool:
        """Cancel any open orders (FAK orders never rest, so normally none)."""
        if not self._initialized:
            return False
        try:
            self.client.cancel_all()
            return True
        except Exception as e:
            print(f"[executor] Cancel all failed: {type(e).__name__}: {e}")
            return False
