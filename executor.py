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
from types import SimpleNamespace
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
HEALTH_TIMEOUT = 5              # seconds; short so a trade near close can't run into the next window
REDEEM_RETRY_AFTER = 900        # seconds before resubmitting a redemption that hasn't landed
BALANCE_CHECK_TRIES = 3         # token-balance reads after an uncertain buy
UNKNOWN_FILL = "UNKNOWN_FILL"   # error prefix: couldn't tell whether a buy filled


def _rejection_reason(code) -> str:
    """Reason code for an order Polymarket refused with a known code: its code
    when it's a short slug (e.g. "rejected_not_enough_balance"), else "rejected"."""
    c = str(code or "").strip().lower()
    return f"rejected_{c}" if c.replace("_", "").isalnum() and len(c) <= 30 else "rejected"


def _optional_approval_targets() -> set:
    """Contracts whose approvals the bot doesn't need: Polymarket's perpetuals
    deposit contract and its auto-redeem operator (the bot redeems winnings
    itself). The SDK's "fully approved" includes them, but granting them would
    give contracts the bot never uses unlimited pUSD spending or control of
    all outcome shares. If the SDK layout changes, nothing is optional."""
    try:
        from polymarket.environments import PRODUCTION
        from polymarket._internal.environment import get_environment_config
        c = get_environment_config(PRODUCTION)
        return {c.perps_deposit_contract.lower(), c.auto_redeem_operator.lower()}
    except Exception:
        return set()


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
    cost_estimated: bool = False   # fill not reported by the order response: cost guessed
    # Why a buy didn't fill, as a short code for signals.csv: "no_fill",
    # "price_cap", "too_small", "no_price", "not_initialized", "rejected" /
    # "rejected_<code>", "api_error", "unknown_fill"
    reason: str = ""


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
        self._redeem_submitted: dict = {}   # condition_id -> time its redemption was sent

    @property
    def can_redeem(self) -> bool:
        return self.builder_key is not None

    # ── Connection ───────────────────────────────────────────────────

    def connect(self) -> bool:
        """Create the SDK client for SAFE_ADDRESS (never without a wallet: the
        SDK would deploy a new Deposit Wallet). Doesn't enable trading."""
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
        return True

    def account_status(self) -> SimpleNamespace:
        """What live trading needs, read from Polymarket (after connect()):
        wallet_type, approved (every approval CLOB trading needs), missing (those
        still to grant), optional_missing (perps / auto-redeem approvals not
        granted, which the bot doesn't need), pUSD balance (None if unreadable),
        and is_safe / ready summaries. Shared by initialize() and preflight.py."""
        st = SimpleNamespace(wallet_type=self.wallet_type, is_safe=self.wallet_type == "GNOSIS_SAFE",
                             approved=False, missing=None, optional_missing=0, balance=None, error="")
        try:
            missing = self.client.get_trading_approvals_state().missing
            optional = _optional_approval_targets()
            needed = ([a for a in missing.erc20 if a.spender.lower() not in optional]
                      + [a for a in missing.erc1155 if a.operator.lower() not in optional])
            st.approved, st.missing = not needed, tuple(needed)
            st.optional_missing = len(missing.erc20) + len(missing.erc1155) - len(needed)
        except Exception as e:
            st.error = f"Could not read trading approvals: {type(e).__name__}: {e}"
        try:
            st.balance = self.client.get_balance_allowance(asset_type="COLLATERAL").balance / BASE_UNITS
        except Exception as e:
            st.error = st.error or f"Could not read the pUSD balance: {type(e).__name__}: {e}"
        st.ready = st.is_safe and st.approved and st.balance is not None
        return st

    def initialize(self) -> bool:
        """Connect for live trading. Returns False (and says why) unless the
        wallet is the Polymarket Safe for this key and trading is approved."""
        if not self.connect():
            return False
        st = self.account_status()
        if not st.is_safe:
            print(f"[executor] REFUSING to trade: the SDK sees SAFE_ADDRESS as {st.wallet_type}, "
                  f"not GNOSIS_SAFE. Check SAFE_ADDRESS is the Polymarket Safe for this PRIVATE_KEY.")
        elif not st.approved:
            print(f"[executor] REFUSING to trade: CLOB V2 trading approvals are missing "
                  f"({st.missing or st.error}). Enable trading in the Polymarket UI, then run preflight.py.")
        elif st.balance is None:
            print(f"[executor] REFUSING to trade: {st.error}")
        if not st.ready:
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
        r = httpx.get(f"{CLOB_HOST}/", timeout=HEALTH_TIMEOUT)
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

    def token_balance(self, token_id: str) -> Optional[float]:
        """Shares of one outcome token held by the Safe, or None if unreadable."""
        try:
            return self.client.get_balance_allowance(
                asset_type="CONDITIONAL", token_id=token_id).balance / BASE_UNITS
        except Exception as e:
            print(f"[executor] Token balance check failed: {type(e).__name__}: {e}")
            return None

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

    def redeemable_winnings(self) -> Optional[list]:
        """[(condition_id, value_usd)] for resolved positions worth redeeming,
        or None if Polymarket's positions data couldn't be read (an unknown,
        not "no winnings": callers skip that round instead of counting $0)."""
        if not self._initialized:
            return []
        try:
            positions = self.client.list_positions(user=self.safe_address, status="REDEEMABLE")
            return [(p.condition_id, float(p.current_value)) for p in positions.iter_items()
                    if p.redeemable and p.current_value > 0]
        except Exception as e:
            print(f"[executor] Position check failed: {type(e).__name__}: {e}")
            return None

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
            return OrderResult(success=False, status=REJECTED, side="BUY", reason="too_small",
                               error=f"Amount ${amount_usd:.2f} below min")
        cap = MAX_BUY_PRICE if max_price <= 0 else min(max_price, MAX_BUY_PRICE)

        if self.dry_run:
            if price <= 0:
                return OrderResult(success=False, status=FAILED, side="BUY", token_id=tok,
                                   reason="no_price", error="No market price for paper fill")
            market_price = round(price, 2)
            if market_price > round(cap, 2):
                return OrderResult(success=False, status=REJECTED, side="BUY", token_id=tok,
                                   price=market_price, reason="price_cap",
                                   error=f"Price ${market_price:.2f} above cap ${cap:.2f}")
            shares, spend = calculate_order_size(market_price, amount_usd)
            if shares < 1 or spend < POLY_MIN_NOTIONAL:
                return OrderResult(success=False, status=REJECTED, side="BUY", token_id=tok,
                                   price=market_price, reason="too_small",
                                   error=f"Can't size an order ≥ ${POLY_MIN_NOTIONAL:.0f} "
                                         f"from ${amount_usd:.2f} at ${market_price:.2f}")
            print(f"  📊 Market price: ${market_price:.3f}/share → {int(shares)} shares for ${spend:.2f}")
            return OrderResult(success=True, order_id=f"DRY-{int(time.time())}", status=FILLED,
                               side="BUY", price=market_price, amount_usd=spend, shares=shares,
                               token_id=tok, dry_run=True)

        if not self._initialized:
            return OrderResult(success=False, status=FAILED, side="BUY", reason="not_initialized",
                               error="Not initialized")
        if amount_usd < POLY_MIN_NOTIONAL:
            return OrderResult(success=False, status=REJECTED, side="BUY", token_id=tok,
                               reason="too_small",
                               error=f"Amount ${amount_usd:.2f} < ${POLY_MIN_NOTIONAL:.0f} min")
        cap = round(cap, 2)
        # Cost estimate for fills the response doesn't report: the book price
        # seen just before ordering (never above the cap)
        est = price if 0 < price <= cap else cap
        print(f"  📊 FAK buy: up to ${amount_usd:.2f} at ≤ ${cap:.2f}/share")
        try:
            resp = self.client.place_market_order(
                token_id=token_id, side="BUY", amount=f"{amount_usd:.2f}",
                max_price=f"{cap:.2f}", order_type="FAK",
            )
        except pm_errors.UserInputError as e:      # refused locally: nothing was sent
            return OrderResult(success=False, status=REJECTED, side="BUY", token_id=tok,
                               reason="rejected",
                               error=f"order not placed: {type(e).__name__}: {e}")
        except pm_errors.RequestRejectedError as e:  # refused by the server
            return OrderResult(success=False, status=FAILED, side="BUY", token_id=tok,
                               reason="api_error",
                               error=f"order not placed: {type(e).__name__}: {e}")
        except Exception as e:
            return self._after_uncertain_buy(token_id, est, e)

        if not resp.ok:
            # A rejection with a known reason (no fill, not enough balance,
            # market not ready...) isn't an API outage; only "unknown" counts.
            unknown = resp.code == "unknown"
            return OrderResult(success=False, status=FAILED if unknown else REJECTED,
                               side="BUY", token_id=tok, error=f"{resp.code}: {resp.message}",
                               reason="api_error" if unknown else _rejection_reason(resp.code))

        spent, shares = float(resp.making_amount), float(resp.taking_amount)
        estimated = False
        if resp.status != "matched" or shares <= 0:
            spent, shares, estimated = self._settle(resp.order_id, token_id, est, spent, shares)
        if shares > 0 and spent > shares:   # BUY: shares cost < $1 each, so shares > spent
            print(f"  ⚠️  Fill amounts look swapped (making {spent}, taking {shares}); swapping")
            spent, shares = shares, spent
        if shares < MIN_SHARES:
            return OrderResult(success=False, status=REJECTED, order_id=resp.order_id,
                               side="BUY", token_id=tok, reason="no_fill",
                               error="FAK order filled nothing")
        return OrderResult(success=True, order_id=resp.order_id, status=FILLED, side="BUY",
                           price=spent / shares, amount_usd=spent, shares=shares,
                           token_id=tok, dry_run=False, cost_estimated=estimated)

    def _settle(self, order_id: str, token_id: str, est: float,
                spent: float, shares: float) -> tuple:
        """For a 'delayed' (or unreported) match, ask the exchange what filled.
        Returns (spent, shares, estimated): a fill found this way is costed at
        `est`, the book price seen before ordering."""
        for _ in range(DELAYED_CHECKS):
            time.sleep(DELAYED_CHECK_INTERVAL)
            try:
                order = self.client.get_order(order_id=order_id)
            except Exception:
                break   # no longer an open order: fall back to the token balance
            matched = float(order.size_matched)
            if matched > 0:
                return matched * est, matched, True
            if order.status not in ("delayed", "live"):
                break
        held = self.token_balance(token_id)
        if held is not None and held > shares:
            print(f"  ⚠️  Fill read from token balance: {held:.2f} shares "
                  f"(cost estimated at ${est:.2f}/share)")
            return held * est, held, True
        return spent, shares, False

    def _after_uncertain_buy(self, token_id: str, est: float, err: Exception) -> OrderResult:
        """The request failed in a way where the order may still have reached
        the exchange. FAK orders can't rest, so either we hold shares now or
        nothing happened. (The bot holds no shares of this window's tokens
        before its one buy.) If the balance can't be read either, say so:
        the bot then rechecks at the window boundary."""
        print(f"  ⚠️  Buy request failed ({type(err).__name__}: {err}); checking for a fill...")
        tok = token_id[:16] + "..."
        held = None
        for _ in range(BALANCE_CHECK_TRIES):
            time.sleep(3)
            held = self.token_balance(token_id)
            if held is not None:
                break
        if held is None:
            return OrderResult(success=False, status=FAILED, side="BUY", token_id=tok, price=est,
                               reason="unknown_fill",
                               error=f"{UNKNOWN_FILL}: can't tell whether the order filled "
                                     f"({type(err).__name__}: {err})")
        if held >= MIN_SHARES:
            print(f"  👻 Order went through despite the error: {held:.2f} shares "
                  f"(cost estimated at ${est:.2f}/share)")
            return OrderResult(success=True, status=FILLED, side="BUY", price=est,
                               amount_usd=held * est, shares=held, token_id=tok,
                               dry_run=False, error=f"filled despite error: {err}",
                               cost_estimated=True)
        return OrderResult(success=False, status=FAILED, side="BUY", token_id=tok,
                           reason="api_error", error=f"network: {type(err).__name__}: {err}")

    def redeem(self, condition_id: str) -> Optional[str]:
        """Submit redemption of a resolved market's winning shares. A market is
        resubmitted only if it is still redeemable REDEEM_RETRY_AFTER after the
        last attempt. Returns the relayer transaction id, or None if not sent."""
        if not self._initialized or not self.can_redeem:
            return None
        sent = self._redeem_submitted.get(condition_id)
        if sent is not None and time.time() - sent < REDEEM_RETRY_AFTER:
            return None   # sent recently; resubmit only if it still hasn't landed later
        try:
            handle = self.client.redeem_positions(condition_id=condition_id)
        except Exception as e:
            print(f"[executor] Redeem failed for {condition_id[:12]}…: {type(e).__name__}: {e}")
            return None
        self._redeem_submitted[condition_id] = time.time()
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
