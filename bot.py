#!/usr/bin/env python3
"""
PolyBot v13 — Recalibrated + Safety Systems

Strategy:
  - Brownian motion model with vol=0.12 (recalibrated from 0.08)
  - Entry gate: model confidence >= 80%, market price <= true_prob * 0.85
  - Position sizing: quarter-Kelly, $5–$25 per trade
  - Exit: hold all positions to resolution — no stops, no take-profit

Safety systems:
  1. CLOB health check before every live trade; 3 consecutive failures
     halt trading and send a Telegram alert. Auto-recovers when the API
     answers again at a window boundary.
  2. Daily loss limit: if session P&L <= -DAILY_LOSS_LIMIT, halt trading.
  3. Fill-and-kill buys capped at the price that still leaves MIN_EDGE:
     nothing rests on the book and the response says what filled.
  4. Live trading only starts for the Polymarket Safe (GNOSIS_SAFE) with
     CLOB V2 trading approvals in place (see executor.py, preflight.py).
  5. Results are booked from Polymarket's own resolution, and winnings
     are redeemed when a builder API key is configured.
  6. Window-boundary sync: bankroll = pUSD + every tracked position at the
     shares the Safe still holds (so a win counts once, however it is
     redeemed) + other redeemable winnings; sizing and the loss limit use
     real money. A failed read skips the sync rather than counting $0.
"""

import os
import sys
import time
import signal
import math
from dotenv import load_dotenv

import logging
logging.getLogger("httpx").setLevel(logging.WARNING)

from market import (get_current_market, current_window_ts, get_market_outcome,
                    get_book_price, PERIOD_SECONDS)
from price_feed import BinancePriceFeed
from strategy import evaluate, estimate_true_probability, get_skip_reason, StrategyConfig, TradingStats
from executor import Executor, FILLED, PARTIAL, FAILED, MAX_BUY_PRICE, MIN_SHARES, UNKNOWN_FILL
from polymarket.auth import BuilderApiKey
from telegram_notifier import TelegramNotifier
from tracker import Tracker


BOT_VERSION = "2026.11.19"  # bump when trading logic changes; the dashboard groups by it

FORCED_EXIT_START = 5
FORCED_EXIT_END = 1
POSITION_CHECK_INTERVAL = 3
# If the first fresh BTC price of a window arrives later than this (bot just
# started mid-window, or the feed stalled), the real opening price is unknown,
# so that window is not traded and not used for the vol estimate.
OPEN_PRICE_MAX_LAG = 5  # seconds
# Trades whose result isn't known at the window boundary wait in a list and
# are booked from Polymarket's own resolution (Gamma), checked this often.
RESOLUTION_POLL_INTERVAL = 10      # seconds
RESOLUTION_GIVE_UP_AFTER = 3600    # stop waiting (and alert) after an hour
# Until then, a finished window's market price this decisive (or 1 minus it)
# shows the likely result: the trade is valued at that price for the bankroll
# and loss limit, but still booked only from Polymarket's resolution.
EARLY_RESULT_MIN = 0.95
# No price fetches this close to the window end, so a slow request can't delay
# the boundary tick past OPEN_PRICE_MAX_LAG and make the next window "partial".
PRICE_FETCH_CUTOFF = 15            # seconds before close
# Balance and positions reads at start-up (the Data API is often rate-limited
# through Tor)
STARTUP_LOOKUP_TRIES = 3
# Fewer shares than this left of a booked win means it has been redeemed
HELD_DUST = 0.01
# Polymarket charges its trading fee on top of the fill, and the order
# response doesn't say how much. It's measured from the pUSD change: read
# every FEE_CHECK_INTERVAL until the buy shows up, for up to FEE_CHECK_FOR.
# A change bigger than the fill + FEE_MAX_SHARE of it isn't the buy alone.
FEE_CHECK_INTERVAL = 3             # seconds
FEE_CHECK_FOR = 60                 # seconds
FEE_MAX_SHARE = 0.05
# Don't send an order this close to the window end (slow API calls before it)
LATE_ORDER_GUARD = 5               # seconds


def dry_run_from_env(value) -> tuple:
    """(dry_run, recognised) for a DRY_RUN setting. Only an explicit "false"
    trades live; anything else (missing, empty, a typo) is a dry run, so a
    blank line in .env can never start live trading by accident."""
    v = (value if value is not None else "true").strip().lower()
    return v != "false", v in ("true", "false")


class PolyBot:
    def __init__(self):
        load_dotenv()

        self.dry_run, recognised = dry_run_from_env(os.getenv("DRY_RUN"))
        if not recognised:
            print(f"[config] DRY_RUN={os.getenv('DRY_RUN')!r} isn't 'true' or 'false': "
                  f"running as a DRY RUN. Set DRY_RUN=false to trade live.")
        self.period = int(os.getenv("MARKET_PERIOD", "5"))

        self.strategy_config = StrategyConfig(
            min_edge=float(os.getenv("MIN_EDGE", "0.05")),
            min_prob=float(os.getenv("MIN_PROB", "0.80")),
            min_btc_delta=float(os.getenv("MIN_BTC_DELTA", "0.06")),
            entry_window_start=int(os.getenv("ENTRY_WINDOW_START", "240")),
            entry_window_end=int(os.getenv("ENTRY_WINDOW_END", "10")),
            kelly_fraction=float(os.getenv("KELLY_FRACTION", "0.25")),
            min_bet=float(os.getenv("MIN_BET", "5.0")),
            max_bet=float(os.getenv("MAX_BET", "25.0")),
        )

        initial_bankroll = float(os.getenv("BANKROLL", "100.0"))
        self._daily_loss_limit = float(os.getenv("DAILY_LOSS_LIMIT", "30.0"))
        self._rolling_vol_windows = int(os.getenv("ROLLING_VOL_WINDOWS", "12"))
        self._vol_floor = float(os.getenv("VOL_FLOOR", "0.06"))
        self._vol_cap = float(os.getenv("VOL_CAP", "0.30"))
        self._vol_fallback = 0.12  # used until enough windows accumulate

        self.price_feed = BinancePriceFeed()
        self.executor = Executor(
            private_key=os.getenv("PRIVATE_KEY", ""),
            safe_address=os.getenv("SAFE_ADDRESS", ""),
            dry_run=self.dry_run,
            builder_key=self._builder_key_from_env(),
        )
        self.telegram = TelegramNotifier()
        self.tracker = Tracker(
            log_dir=os.getenv("LOG_DIR", "logs"),
            log_executions=os.getenv("LOG_EXECUTIONS", "false").lower() == "true",
            version=BOT_VERSION,
        )
        self.stats = TradingStats(bankroll=initial_bankroll)
        self.stats.hourly.hour_start = time.time()

        self._running = False
        self._current_window: int = 0
        self._opening_price: float = 0.0
        self._last_hour_check: int = 0

        # Trade state
        self._partial_window: bool = False  # True if this window's open price is unknown
        self._traded: bool = False
        self._trade_attempted: bool = False
        self._trade_side: str = ""
        self._trade_price: float = 0.0
        self._trade_cost: float = 0.0
        self._trade_shares: float = 0.0
        self._trade_token_id: str = ""
        self._trade_id: int = 0  # tracker trade_id of the current trade
        self._trade_condition_id: str = ""
        self._claim_note_total: float = -1.0   # last "claim in the UI" total printed
        self._unknown_fill: dict = {}          # a buy whose fill couldn't be checked
        # Live: booked wins (or split payouts) whose shares may still be in the
        # Safe, token_id -> {"condition_id", "per_share"}; dropped once redeemed
        self._unredeemed: dict = {}
        self._session_cids: set = set()        # markets traded this session
        self._last_pusd: float = 0.0           # pUSD at the last full balance read
        self._fee_watch: dict = {}             # last live buy whose fee isn't measured yet

        # Exit state
        self._exited: bool = False
        self._exit_revenue: float = 0.0
        self._exit_shares_sold: float = 0.0
        self._residual_shares: float = 0.0  # Shares left after partial fill
        self._last_position_check: float = 0.0
        self._last_status_print: float = 0.0
        self._last_tick_context: dict = {}   # last entry-window state, for window-end signal logging
        self._session_start_time: float = time.time()
        self._recent_window_deltas: list = []  # rolling signed close_delta_pct per window
        self._exit_retries: int = 0
        self._exit_gave_up: bool = False
        self._last_sell_price_seen: float = 0.0  # last observed sell price during hold period

        # Trades whose result wasn't known at the window boundary (dry run, or a
        # live claim that couldn't be confirmed). Booked from Polymarket's own
        # resolution by _check_pending_resolutions(), not from balance changes.
        self._pending_resolutions: list = []
        self._last_resolution_check: float = 0.0


        # Unclaimed
        self._unclaimed_winnings: float = 0.0

        # Every entry this session as (entry_price, edge, btc_delta_pct), for the
        # session averages in sessions.csv (hourly stats reset each hour)
        self._session_entries: list = []

        # Real balance tracking (source of truth)
        self._session_start_balance: float = 0.0
        self._last_real_balance: float = 0.0

        # Price cache
        self._cached_up: float = 0.50
        self._cached_down: float = 0.50
        self._price_last_fetched: float = 0.0
        self._PRICE_REFRESH: float = 5.0
        self._market = None              # this window's MarketWindow, looked up once

        # Circuit breaker — detects CLOB API degradation
        self._consecutive_buy_failures: int = 0
        self._clob_halted: bool = False
        self._HALT_AFTER_FAILURES: int = 3
        self._daily_loss_halted: bool = False

    @staticmethod
    def _builder_key_from_env():
        """Builder API key for redeeming winnings, if all three parts are set."""
        parts = [os.getenv(k, "") for k in
                 ("BUILDER_API_KEY", "BUILDER_API_SECRET", "BUILDER_API_PASSPHRASE")]
        if all(parts):
            return BuilderApiKey(key=parts[0], secret=parts[1], passphrase=parts[2])
        if any(parts):
            print("[config] BUILDER_API_KEY/SECRET/PASSPHRASE are only partly set — "
                  "winnings won't be redeemed automatically")
        return None

    def start(self):
        if not self.dry_run:
            from proxy import ensure_tor, apply_proxy
            import logging as _log
            _log.basicConfig(level=_log.INFO, format="[%(name)s] %(message)s")
            print("\n🧅 Starting Tor proxy for CLOB API...")
            proxy_url = ensure_tor()
            apply_proxy(proxy_url)
            print(f"✅ Tor active: {proxy_url}\n")

        kf = self.strategy_config.kelly_fraction
        mp = self.strategy_config.min_prob
        me = self.strategy_config.min_edge
        print("=" * 55)
        print(f"  PolyBot v13 — Recalibrated (vol=0.12)")
        print(f"  Mode: {'DRY RUN' if self.dry_run else '🔴 LIVE TRADING'}")
        print(f"  Kelly: {kf*100:.0f}% fraction | "
              f"Bets: ${self.strategy_config.min_bet:.0f}–${self.strategy_config.max_bet:.0f}")
        print(f"  Min prob: {mp:.0%} | Min edge: {me:.0%} | Min BTC delta: {self.strategy_config.min_btc_delta:.2f}%")
        print(f"  Entry: T-{self.strategy_config.entry_window_start}s to "
              f"T-{self.strategy_config.entry_window_end}s")
        print(f"  Vol: dynamic (fallback=0.12, floor={self._vol_floor}, cap={self._vol_cap}, windows={self._rolling_vol_windows})")
        print(f"  Exits: hold to resolution")
        print(f"  Daily loss limit: ${self._daily_loss_limit:.0f}")
        print(f"  Bankroll: ${self.stats.bankroll:.2f}")
        print("=" * 55)

        if not self.dry_run:
            if not self.executor.initialize():
                print("\n❌ Failed to initialize. Check credentials.")
                return
            equity, redeemable = None, []
            for attempt in range(STARTUP_LOOKUP_TRIES):
                equity, redeemable = self._live_equity()
                if equity is not None:
                    break
                time.sleep(2)
            if equity is None:
                # Starting from a guess (e.g. winnings left out) would understate the
                # session's start and loosen the daily loss limit once they're counted.
                print("\n❌ Couldn't read your pUSD balance or Polymarket positions (see above; the "
                      "positions lookup is often rate-limited through Tor). Try again in a minute: "
                      "./start.sh gets a fresh Tor circuit.")
                return
            print(f"  pUSD balance: ${self._last_pusd:.2f} | with redeemable winnings: ${equity:.2f}")
            self.stats.bankroll = equity
            self._session_start_balance = equity
            self._last_real_balance = equity
            self.tracker.set_session_balance(equity)
            self._redeem_winnings(redeemable)
        else:
            print("  [dry run — no wallet connection]")
            self._session_start_balance = self.stats.bankroll
            self._last_real_balance = self.stats.bankroll
            self.tracker.set_session_balance(self.stats.bankroll)

        self.price_feed.start()
        print("\n⏳ Waiting for BTC price...")
        price = self.price_feed.wait_for_price(timeout=30)
        if not price:
            print("❌ No price feed. Check internet.")
            return
        print(f"✅ BTC: ${price:,.2f} ({self.price_feed.state.source})")

        self.telegram.startup_alert({
            "dry_run": self.dry_run,
            "kelly_fraction": kf,
            "min_edge": self.strategy_config.min_edge,
            "min_bet": self.strategy_config.min_bet,
            "max_bet": self.strategy_config.max_bet,
            "entry_start": self.strategy_config.entry_window_start,
            "entry_end": self.strategy_config.entry_window_end,
        })

        self._running = True
        self._last_hour_check = int(time.time() // 3600)
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

        print("\n🚀 Running. Ctrl+C to stop.\n")
        self._main_loop()

    def _main_loop(self):
        while self._running:
            try:
                self._tick()
                self._check_hourly_summary()
                self._check_fee()
                self._check_pending_resolutions()
            except Exception as e:
                print(f"[error] {e}")
                self.telegram.error_alert(str(e))
            time.sleep(0.1)

    def _tick(self):
        now = time.time()
        period_secs = PERIOD_SECONDS[self.period]
        window_ts = int(now) - (int(now) % period_secs)

        btc_price, is_fresh = self.price_feed.get_price()
        if not is_fresh or btc_price <= 0:
            return

        if window_ts != self._current_window:
            self._on_new_window(window_ts, closing_btc_price=btc_price)

        seconds_remaining = (window_ts + period_secs) - now

        if self._opening_price <= 0:
            self._opening_price = btc_price
            open_lag = now - window_ts
            self._partial_window = open_lag > OPEN_PRICE_MAX_LAG
            if self._partial_window:
                print(f"  📌 Open: ${btc_price:,.2f} — first price came {open_lag:.0f}s into the "
                      f"window, so the real open is unknown: not trading this window")
            else:
                print(f"  📌 Open: ${btc_price:,.2f}")

        # HOLDING: active position management
        if self._traded and not self._exited and not self._exit_gave_up:
            self._manage_position(btc_price, seconds_remaining, now)
            return

        # Already done
        if self._traded or self._trade_attempted:
            return

        # Partial window: never trade against a guessed opening price
        if self._partial_window:
            if now - self._last_status_print >= 30:
                self._last_status_print = now
                print(f"  ⏱  T-{seconds_remaining:5.1f}s | BTC ${btc_price:,.2f} | "
                      f"[PARTIAL WINDOW — waiting for the next one]")
            return

        # IDLE: look for entry
        up_price, down_price = self._get_market_prices(btc_price, seconds_remaining)

        realized_vol = self._compute_realized_vol()
        signal_result = evaluate(
            btc_price=btc_price,
            opening_price=self._opening_price,
            up_market_price=up_price,
            down_market_price=down_price,
            seconds_remaining=seconds_remaining,
            bankroll=self.stats.bankroll,
            config=self.strategy_config,
            realized_vol=realized_vol,
        )

        # Store context for window-end no-trade signal logging
        self._last_tick_context = {
            "btc_price": btc_price,
            "up_price": up_price,
            "down_price": down_price,
            "seconds_remaining": seconds_remaining,
            "window_ts": self._current_window,
            "signal": signal_result,
        }

        if signal_result:
            self._execute_trade(signal_result, seconds_remaining)

        if now - self._last_status_print >= 30:
            self._last_status_print = now
            delta = ((btc_price - self._opening_price) / self._opening_price * 100) if self._opening_price > 0 else 0
            d = "↑" if delta > 0 else "↓" if delta < 0 else "→"
            if self._traded:
                state = "HOLDING"
            elif self._opening_price > 0 and abs(delta) < self.strategy_config.min_btc_delta:
                state = f"ΔSMALL ({abs(delta):.3f}%<{self.strategy_config.min_btc_delta:.3f}%)"
            else:
                state = "IDLE"
            n = len(self._recent_window_deltas)
            vol_label = f"{self._compute_realized_vol():.3f}({'r' if n >= 6 else f'fb,n={n}'})"
            print(
                f"  ⏱  T-{seconds_remaining:5.1f}s | "
                f"BTC ${btc_price:,.2f} {d}{abs(delta):.3f}% | "
                f"UP ${up_price:.3f} DN ${down_price:.3f} | "
                f"vol={vol_label} | P&L ${self.stats.total_pnl:+.2f} [{state}]"
            )

    # ── Active position management ──────────────────────────────────

    # ── Position monitoring (hold to resolution) ────────────────────

    def _manage_position(self, btc_price: float, seconds_remaining: float, now: float):
        """Monitor only — all trades hold to resolution. No stops.
        Tracker logs hold-period stats for future optimization.
        """
        if self._opening_price <= 0:
            return

        btc_delta_pct = ((btc_price - self._opening_price) / self._opening_price) * 100
        # estimate_true_probability() works on |delta|: it returns the chance
        # BTC finishes on the side it is on NOW, not the chance it finishes UP.
        # Convert to P(UP) first, then take our side. (Using it directly as
        # P(UP) inverted the figure for winning DOWN and losing UP trades.)
        # Same vol as entry uses, so hold-period figures are comparable.
        p_current_side = estimate_true_probability(
            btc_delta_pct, seconds_remaining, vol=self._compute_realized_vol()
        )
        p_up = p_current_side if btc_delta_pct >= 0 else 1.0 - p_current_side
        our_prob = p_up if self._trade_side == "UP" else 1.0 - p_up

        # Throttled check
        if now - self._last_position_check < POSITION_CHECK_INTERVAL:
            if now - self._last_status_print >= 30:
                self._last_status_print = now
                d = "↑" if btc_delta_pct > 0 else "↓" if btc_delta_pct < 0 else "→"
                print(
                    f"  ⏱  T-{seconds_remaining:5.1f}s | "
                    f"BTC {d}{abs(btc_delta_pct):.3f}% | "
                    f"Prob: {our_prob:.2f} | "
                    f"P&L ${self.stats.total_pnl:+.2f} [HOLDING→RES]"
                )
            return

        self._last_position_check = now

        # No price fetch right before close: a slow request (Tor) could delay the
        # boundary tick past OPEN_PRICE_MAX_LAG and make the next window "partial"
        if seconds_remaining < PRICE_FETCH_CUTOFF:
            return

        # Get current sell price (for tracking only)
        if self.dry_run:
            current_sell_price = get_book_price(self._trade_token_id, "SELL", self._trade_shares)
        else:
            current_sell_price = self.executor.get_market_price(
                self._trade_token_id, "SELL", self._trade_shares
            )

        if current_sell_price <= 0:
            return

        self._last_sell_price_seen = current_sell_price

        # Track hold-period extremes
        self.tracker.update_hold_stats(our_prob, current_sell_price)

        current_value = self._trade_shares * current_sell_price
        unrealized_pnl = current_value - self._trade_cost
        return_pct = (current_sell_price - self._trade_price) / self._trade_price if self._trade_price > 0 else 0

        # Log tick to hold_ticks.csv for trajectory analysis
        self.tracker.log_hold_tick(
            seconds_remaining=seconds_remaining,
            btc_price=btc_price,
            btc_delta_pct=btc_delta_pct,
            prob=our_prob,
            sell_price=current_sell_price,
            unrealized_pnl=unrealized_pnl,
            return_pct=return_pct,
        )

        # Status line (monitoring only — no exits)
        d = "↑" if btc_delta_pct > 0 else "↓" if btc_delta_pct < 0 else "→"
        pnl_emoji = "📈" if unrealized_pnl > 0 else "📉"
        print(
            f"  {pnl_emoji} T-{seconds_remaining:5.1f}s | "
            f"BTC {d}{abs(btc_delta_pct):.3f}% | "
            f"Prob: {our_prob:.2f} | "
            f"Sell: ${current_sell_price:.3f} | "
            f"PnL: ${unrealized_pnl:+.2f} ({return_pct:+.0%})"
        )

    # ── Execute exit (balance-verified, partial fill aware) ─────────

    # ── Window management ───────────────────────────────────────────

    def _on_new_window(self, window_ts: int, closing_btc_price: float = 0.0):
        if self._current_window > 0:
            # Record closing delta for rolling vol calculation. Skip partial
            # windows (their "open" was a mid-window price) and boundaries seen
            # late (feed stalled), whose "close" is a price from the next window.
            close_lag = time.time() - window_ts
            if (self._opening_price > 0 and closing_btc_price > 0 and not self._partial_window
                    and close_lag <= OPEN_PRICE_MAX_LAG):
                closing_delta = (closing_btc_price - self._opening_price) / self._opening_price * 100
                self._recent_window_deltas.append(closing_delta)
                if len(self._recent_window_deltas) > self._rolling_vol_windows:
                    self._recent_window_deltas.pop(0)
            if self._unknown_fill and not self._traded:
                self._recheck_unknown_fill()

            self.stats.hourly.record_window(self._traded)
            if self._traded:
                self._resolve_previous_trade()
            elif self._last_tick_context:
                # Log the no-trade signal for this window using last tick state
                ctx = self._last_tick_context
                skip_reason = get_skip_reason(
                    btc_price=ctx["btc_price"],
                    opening_price=self._opening_price,
                    up_market_price=ctx["up_price"],
                    down_market_price=ctx["down_price"],
                    seconds_remaining=ctx["seconds_remaining"],
                    config=self.strategy_config,
                    realized_vol=self._compute_realized_vol(),
                )
                sig = ctx.get("signal")
                self.tracker.log_signal(
                    window_ts=ctx["window_ts"],
                    btc_price=ctx["btc_price"],
                    opening_price=self._opening_price,
                    up_price=ctx["up_price"],
                    down_price=ctx["down_price"],
                    seconds_remaining=ctx["seconds_remaining"],
                    side=sig.side if sig else "",
                    true_prob=sig.true_prob if sig else 0.0,
                    market_price=sig.market_price if sig else 0.0,
                    edge=sig.edge if sig else 0.0,
                    kelly_size=sig.kelly_size if sig else 0.0,
                    action="no_signal",
                    skip_reason=skip_reason,
                )

            # Sync real money at window boundary (catches any drift), and
            # redeem any winnings that have become redeemable
            if not self.dry_run and self.executor._initialized:
                real_bal, redeemable = self._live_equity()
                if real_bal is None:
                    print("  ⚠️  Couldn't read balances or positions: bankroll sync and "
                          "redemptions skipped this window")
                else:
                    # Settle a pending fee from this same pUSD reading first, so
                    # the fee isn't taken off again after the sync includes it
                    self._check_fee(pusd=self._last_pusd)
                    drift = abs(real_bal - self.stats.bankroll)
                    if drift > 0.50:
                        print(f"  🔄 Balance sync: ${self.stats.bankroll:.2f} → "
                              f"${real_bal:.2f} (drift ${drift:.2f})")
                    self.stats.bankroll = real_bal
                    self._last_real_balance = real_bal
                    self._redeem_winnings(redeemable)

        self._current_window = window_ts
        self._opening_price = 0.0
        self._partial_window = False
        self._last_tick_context = {}
        self._trade_id = 0
        self._traded = False
        self._trade_attempted = False
        self._exited = False
        self._exit_revenue = 0.0
        self._exit_shares_sold = 0.0
        self._residual_shares = 0.0
        self._last_position_check = 0.0
        self._exit_retries = 0
        self._exit_gave_up = False
        self._last_sell_price_seen = 0.0
        self._cached_up = 0.50
        self._cached_down = 0.50
        self._price_last_fetched = 0.0

        # Filled = this session's fills; W/L and P&L count booked results only,
        # so the trades still waiting for Polymarket's result are shown too
        t = time.strftime("%H:%M:%S", time.localtime(window_ts))
        print(f"\n{'─' * 55}")
        print(f"🕐 {t} | Trades: {len(self._session_entries)} filled | "
              f"W/L: {self.stats.wins}/{self.stats.losses}, "
              f"{len(self._pending_resolutions)} waiting | "
              f"P&L: ${self.stats.total_pnl:+.2f} | Bank: ${self.stats.bankroll:.2f}")
        print(f"{'─' * 55}")

        # Circuit breaker auto-recovery: ping CLOB each new window
        if self._clob_halted and not self.dry_run and self.executor._initialized:
            try:
                self.executor.health_check()
                self._clob_halted = False
                self._consecutive_buy_failures = 0
                print(f"  ✅ CLOB recovered (health check OK) — resuming trades")
            except Exception:
                print(f"  🔌 CLOB health check still failing — staying halted")

    # ── Market prices (cached, complement engine) ───────────────────

    def _compute_realized_vol(self) -> float:
        """Realized 5-min vol: RMS of recent SIGNED window closing deltas (in %).

        The Brownian model assumes zero drift, so sqrt(mean(delta²)) estimates
        its vol directly. (The old stdev of |delta| measured only ~0.60× the
        true vol, making the model overconfident.)
        Returns the realized vol to pass into the Brownian motion model.
        Falls back to 0.12 until at least 6 windows have accumulated.
        Floored/capped to prevent extreme values breaking the model.
        """
        min_samples = max(6, self._rolling_vol_windows // 2)
        deltas = self._recent_window_deltas
        if len(deltas) >= min_samples:
            vol = math.sqrt(sum(d * d for d in deltas) / len(deltas))
            return max(self._vol_floor, min(self._vol_cap, vol))
        return self._vol_fallback

    def _get_market_prices(self, btc_price: float, seconds_remaining: float) -> tuple:
        # Dry run reads the same real order book as live (public, read-only),
        # instead of the old synthetic price model, so paper trades are realistic.
        # Only fetch inside the entry window (and not right before close).
        cfg = self.strategy_config
        if (seconds_remaining > cfg.entry_window_start
                or seconds_remaining < max(cfg.entry_window_end, PRICE_FETCH_CUTOFF)):
            return self._cached_up, self._cached_down
        now = time.time()
        if now - self._price_last_fetched < self._PRICE_REFRESH:
            return self._cached_up, self._cached_down

        try:
            market = self._window_market()
            if not market:
                return self._cached_up, self._cached_down

            probe_amount = 5.0
            up_price = self._buy_price(market.token_id_up, probe_amount)
            down_price = self._buy_price(market.token_id_down, probe_amount)

            if up_price <= 0 and down_price <= 0:
                return self._cached_up, self._cached_down
            if up_price <= 0:
                up_price = round(1.0 - down_price, 3)
            if down_price <= 0:
                down_price = round(1.0 - up_price, 3)

            self._cached_up = up_price
            self._cached_down = down_price
            self._price_last_fetched = now

            return up_price, down_price

        except Exception as e:
            print(f"[price] Error: {e}")
            return self._cached_up, self._cached_down

    def _window_market(self):
        """This window's market (token ids never change within a window), looked
        up on Gamma once and cached instead of on every price refresh."""
        if self._market is None or self._market.window_start != self._current_window:
            market = get_current_market(self.period)
            if market and market.window_start == self._current_window:
                self._market = market
            return market
        return self._market

    def _buy_price(self, token_id: str, amount_usd: float) -> float:
        """Price to buy amount_usd of a token: via the executor when trading
        live, from the public order book in dry run (same book-walk rule)."""
        if self.dry_run or not self.executor._initialized:
            return get_book_price(token_id, "BUY", amount_usd)
        return self.executor.get_market_price(token_id, "BUY", amount_usd)

    # ── Entry ───────────────────────────────────────────────────────

    def _execute_trade(self, sig, seconds_remaining: float):
        self._trade_attempted = True

        # ── Circuit breaker: CLOB health check ───────────────────
        if self._clob_halted:
            print(f"  🔌 CLOB HALTED — skipping trade ({self._consecutive_buy_failures} consecutive failures)")
            return

        # Live: reading pUSD is an authenticated CLOB call, so it doubles as the
        # health check, and it's the "before" figure for measuring the fee.
        pusd_before = None
        if not self.dry_run and self.executor._initialized:
            pusd_before = self.executor.read_balance()
            if pusd_before is None:
                self._consecutive_buy_failures += 1
                print(f"  🔌 CLOB health check failed (couldn't read the pUSD balance)")
                if self._consecutive_buy_failures >= self._HALT_AFTER_FAILURES:
                    self._clob_halted = True
                    msg = (f"🔌 CLOB HALTED after {self._consecutive_buy_failures} "
                           f"consecutive health check failures — stopping trades until recovery")
                    print(f"\n  {msg}")
                    self.telegram.status_update({"alert": msg})
                return

        # ── Daily loss limit ─────────────────────────────────────
        if self._daily_loss_halted:
            # Same figure the limit is checked on (bankroll vs session start)
            print(f"  🛑 DAILY LOSS LIMIT — session P&L "
                  f"${self.stats.bankroll - self._session_start_balance:+.2f} "
                  f"exceeds -${self._daily_loss_limit:.0f}")
            return

        session_pnl = self.stats.bankroll - self._session_start_balance
        if session_pnl <= -self._daily_loss_limit:
            self._daily_loss_halted = True
            msg = (f"🛑 DAILY LOSS LIMIT HIT: ${session_pnl:+.2f} "
                   f"(limit -${self._daily_loss_limit:.0f}) — stopping trades")
            print(f"\n  {msg}")
            self.telegram.status_update({"alert": msg})
            return

        market = self._window_market()
        if not market:
            print(f"  ⚠️  {sig.side} signal, but this window's market wasn't found — skipping")
            return
        token_id = market.token_id_up if sig.side == "UP" else market.token_id_down
        self._trade_condition_id = market.condition_id

        slug = f"btc-updown-{self.period}m-{self._current_window}"
        trade_amount = round(sig.kelly_size, 2)

        print(f"\n  🎯 {sig.side} | edge={sig.edge:.3f} | "
              f"prob={sig.true_prob:.2f} | BTC Δ={sig.btc_delta_pct:+.3f}%")
        print(f"     Kelly: ${trade_amount:.2f} | mkt ${sig.market_price:.3f} | T-{seconds_remaining:.0f}s")

        # Preview actual market price, re-check edge, then pass price into buy()
        # so executor skips a second fetch (saves one Tor roundtrip ~500ms)
        hint_price = 0.0
        if self.dry_run or self.executor._initialized:
            actual_price = self._buy_price(token_id, trade_amount)
            if actual_price > 0:
                actual_edge = sig.true_prob - actual_price
                print(f"  📊 Actual price: ${actual_price:.3f} (edge: {actual_edge:.3f})")

                if actual_edge < self.strategy_config.min_edge:
                    print(f"  ⚠️  Edge gone at market price — skipping")
                    btc_approx = self._opening_price * (1 + sig.btc_delta_pct / 100) if self._opening_price > 0 else 0
                    self.tracker.log_signal(
                        window_ts=self._current_window,
                        btc_price=btc_approx,
                        opening_price=self._opening_price,
                        up_price=self._cached_up,
                        down_price=self._cached_down,
                        seconds_remaining=seconds_remaining,
                        side=sig.side,
                        true_prob=sig.true_prob,
                        market_price=actual_price,
                        edge=actual_edge,
                        kelly_size=sig.kelly_size,
                        action="skipped_edge_gone",
                        skip_reason="edge_gone_at_market",
                        actual_price=actual_price,
                        actual_edge=actual_edge,
                    )
                    return

                hint_price = actual_price

        # Slow API calls above can eat the time left; never order into the close
        if self._current_window + PERIOD_SECONDS[self.period] - time.time() < LATE_ORDER_GUARD:
            print(f"  ⏱  Too close to the window end after slow API calls — skipping")
            return

        # Never pay more than leaves MIN_EDGE (executor also caps at MAX_BUY_PRICE)
        max_price = sig.true_prob - self.strategy_config.min_edge
        result = self.executor.buy(token_id=token_id, amount_usd=trade_amount,
                                   price=hint_price, max_price=max_price)

        if result.success:
            self._consecutive_buy_failures = 0  # Reset circuit breaker
            self._traded = True
            self._trade_side = sig.side
            self._trade_price = result.price
            self._trade_cost = result.amount_usd
            self._trade_shares = result.shares
            self._trade_token_id = token_id
            self._session_cids.add(market.condition_id)

            # The bankroll counts the position at its fill price (its "mark"), so
            # buying doesn't change it; the result moves it (see _book_market_resolution)
            self.stats.hourly.record_trade(sig.edge, sig.btc_delta_pct)
            self._session_entries.append((result.price, sig.edge, sig.btc_delta_pct))

            btc_approx = self._opening_price * (1 + sig.btc_delta_pct / 100) if self._opening_price > 0 else 0
            self.tracker.log_signal(
                window_ts=self._current_window,
                btc_price=btc_approx,
                opening_price=self._opening_price,
                up_price=self._cached_up,
                down_price=self._cached_down,
                seconds_remaining=seconds_remaining,
                side=sig.side,
                true_prob=sig.true_prob,
                market_price=sig.market_price,
                edge=sig.edge,
                kelly_size=sig.kelly_size,
                action="traded",
                actual_price=result.price,
                actual_edge=sig.true_prob - result.price,
                fill_price=result.price,
            )
            self._trade_id = self.tracker.open_trade(
                window_ts=self._current_window,
                side=sig.side,
                entry_price=result.price,
                entry_shares=result.shares,
                entry_cost=result.amount_usd,
                edge=sig.edge,
                prob=sig.true_prob,
                btc_delta=sig.btc_delta_pct,
                seconds_remaining=seconds_remaining,
                entry_delta_pct=sig.btc_delta_pct,
                entry_seconds_remaining=seconds_remaining,
                version=BOT_VERSION,
            )
            if pusd_before is not None and not result.cost_estimated:
                self._fee_watch = {"trade_id": self._trade_id, "before": pusd_before,
                                   "spent": result.amount_usd, "since": time.time(), "last": 0.0}

            mode = "PAPER" if self.dry_run else "LIVE"
            print(f"  ✅ {mode}: {result.shares:.0f} shares @ "
                  f"${result.price:.3f} = ${result.amount_usd:.2f}")
            print(f"     Holding to resolution (no stops)")

            self.telegram.trade_alert(
                side=sig.side, price=result.price, amount=result.amount_usd,
                market_slug=slug, dry_run=self.dry_run,
                edge=sig.edge, kelly_size=sig.kelly_size,
            )
        else:
            print(f"  ❌ Buy failed: {result.error}")
            if result.error.startswith(UNKNOWN_FILL):
                self._unknown_fill = {
                    "token_id": token_id, "side": sig.side, "price": result.price,
                    "edge": sig.edge, "prob": sig.true_prob, "delta": sig.btc_delta_pct,
                    "seconds_remaining": seconds_remaining,
                    "condition_id": self._trade_condition_id,
                }
                self.telegram.error_alert(
                    f"Couldn't tell whether a {sig.side} buy filled; rechecking at the window end")
            # Circuit breaker: consecutive API failures (not "no fill at my price")
            if result.status == FAILED and not self.dry_run:
                self._consecutive_buy_failures += 1
                if self._consecutive_buy_failures >= self._HALT_AFTER_FAILURES:
                    self._clob_halted = True
                    msg = (f"🔌 CLOB HALTED after {self._consecutive_buy_failures} "
                           f"consecutive API failures — stopping trades until recovery")
                    print(f"\n  {msg}")
                    self.telegram.status_update({"alert": msg})

    # ── Resolve (partial fill aware) ────────────────────────────────

    def _resolve_previous_trade(self):
        if self._exited:
            profit = self._exit_revenue - self._trade_cost
            if profit > 0:
                self.stats.record_win(profit)
            else:
                self.stats.record_loss(abs(profit))
            result_emoji = "✅ WIN" if profit > 0 else "❌ LOSS"
            residual_note = f" (~{self._residual_shares:.0f} residual)" if self._residual_shares >= 1 else ""
            print(f"  {result_emoji} (exited{residual_note}) ${profit:+.2f} | "
                  f"P&L: ${self.stats.total_pnl:+.2f} | "
                  f"Bank: ${self.stats.bankroll:.2f}")
            if profit > 0:
                self.telegram.win_alert(profit, self.stats.total_pnl)
            else:
                self.telegram.loss_alert(abs(profit), self.stats.total_pnl)
            btc_price, _ = self.price_feed.get_price()
            self.tracker.close_trade(
                btc_final_price=btc_price,
                opening_price=self._opening_price,
                won=profit > 0,
                profit=profit,
                exit_revenue=self._exit_revenue,
                resolution_method="exited",
                bankroll=self.stats.bankroll,
                trade_id=self._trade_id,
            )
            return

        original_cost = self._trade_cost
        remaining_shares = self._trade_shares

        # ── Both modes: book from Polymarket's own result ────────────
        # (Binance can disagree with the Chainlink price the market settles
        # on; live winnings are redeemed by _redeem_winnings() once they show
        # up as redeemable.)
        self._defer_resolution(original_cost, remaining_shares)

    def _defer_resolution(self, original_cost: float, remaining_shares: float,
                          claim_result: str = "not_attempted"):
        """Queue the just-finished trade to be booked from Polymarket's result."""
        btc_price, _ = self.price_feed.get_price()
        self._pending_resolutions.append({
            "window_ts": self._current_window,
            "side": self._trade_side,
            "trade_id": self._trade_id,
            "cost": original_cost,
            "shares": remaining_shares,
            "token_id": self._trade_token_id,
            "mark": self._trade_price,      # value per share counted in the bankroll
            "exit_revenue": self._exit_revenue,
            "opening_price": self._opening_price,
            "closing_price": btc_price,
            "claim_result": claim_result,
            "condition_id": self._trade_condition_id,
            "since": time.time(),
        })
        t = time.strftime("%H:%M", time.localtime(self._current_window))
        print(f"  ⏳ {self._trade_side} trade in the {t} window: waiting for Polymarket's result")

    def _check_pending_resolutions(self):
        """Book any queued trades whose market Polymarket has now resolved."""
        if not self._pending_resolutions:
            return
        now = time.time()
        if now - self._last_resolution_check < RESOLUTION_POLL_INTERVAL:
            return
        # Keep slow HTTP calls away from the window boundary, where the
        # opening price is captured.
        into_window = now % PERIOD_SECONDS[self.period]
        if into_window < 2 or PERIOD_SECONDS[self.period] - into_window < 10:
            return
        self._last_resolution_check = now

        still_waiting = []
        for p in self._pending_resolutions:
            outcome = get_market_outcome(p["window_ts"], self.period)
            # (a trade whose fee is still being measured waits for it, ≤ FEE_CHECK_FOR)
            if (outcome and outcome["resolved"]
                    and self._fee_watch.get("trade_id") != p["trade_id"]):
                self._book_market_resolution(p, outcome["prices"])
                continue
            if outcome and not outcome["resolved"]:
                self._note_early_result(p, outcome["prices"][p["side"]])
            if now - p["since"] > RESOLUTION_GIVE_UP_AFTER:
                t = time.strftime("%H:%M", time.localtime(p["window_ts"]))
                msg = (f"No Polymarket result for the {t} window after "
                       f"{RESOLUTION_GIVE_UP_AFTER // 60} min — trade {p['trade_id']} "
                       f"({p['side']}, ${p['cost']:.2f}) NOT booked; check it manually")
                print(f"  ⚠️  {msg}")
                self.telegram.error_alert(msg)
                self.tracker.log_trade_unresolved("unresolved_timeout", trade_id=p["trade_id"])
            else:
                still_waiting.append(p)
        self._pending_resolutions = still_waiting

    def _note_early_result(self, p: dict, price: float):
        """A finished window's market price shows the likely result well before
        Polymarket resolves it. Once it's decisive, value the trade at that
        price, so the bankroll and daily loss limit see a likely loss straight
        away, and say so once. Booking still waits for the resolution."""
        if 1.0 - EARLY_RESULT_MIN < price < EARLY_RESULT_MIN:
            return
        self.stats.bankroll += p["shares"] * (price - p["mark"])
        p["mark"] = price
        likely = "WIN" if price >= EARLY_RESULT_MIN else "LOSS"
        if p.get("likely") != likely:
            p["likely"] = likely
            t = time.strftime("%H:%M", time.localtime(p["window_ts"]))
            print(f"  🔮 Likely {likely}: {p['side']} in the {t} window is at ${price:.3f} "
                  f"after the close (≈ ${p['shares'] * price - p['cost']:+.2f}); "
                  f"booked when Polymarket resolves it | Bank: ${self.stats.bankroll:.2f}")

    def _recheck_unknown_fill(self):
        """A buy failed in a way where the fill couldn't be checked. Look at the
        token balance once more and, if it filled, track it like any trade."""
        uf, self._unknown_fill = self._unknown_fill, {}
        t = time.strftime("%H:%M", time.localtime(self._current_window))
        held = self.executor.token_balance(uf["token_id"])
        if held is None:
            msg = (f"Still can't verify the {uf['side']} buy in the {t} window — "
                   f"check Polymarket for an untracked position")
            print(f"  ⚠️  {msg}")
            self.telegram.error_alert(msg)
            return
        if held < MIN_SHARES:
            print(f"  ✓ The unverified {uf['side']} buy in the {t} window didn't fill")
            return
        cost = held * uf["price"]
        print(f"  👻 The unverified {uf['side']} buy did fill: {held:.0f} shares "
              f"(cost estimated at ${uf['price']:.2f}/share = ${cost:.2f})")
        self._traded = True
        self._trade_side, self._trade_price = uf["side"], uf["price"]
        self._trade_cost, self._trade_shares = cost, held
        self._trade_token_id, self._trade_condition_id = uf["token_id"], uf["condition_id"]
        self._session_cids.add(uf["condition_id"])
        self.stats.hourly.record_trade(uf["edge"], uf["delta"])
        self._session_entries.append((uf["price"], uf["edge"], uf["delta"]))
        self._trade_id = self.tracker.open_trade(
            window_ts=self._current_window, side=uf["side"], entry_price=uf["price"],
            entry_shares=held, entry_cost=cost, edge=uf["edge"], prob=uf["prob"],
            btc_delta=uf["delta"], seconds_remaining=uf["seconds_remaining"],
            version=BOT_VERSION,
        )

    def _check_fee(self, force: bool = False, pusd: float = None):
        """Measure Polymarket's fee on the last live buy from the real pUSD
        change: pUSD before the order − pUSD now − what the fill cost. The fee
        isn't in the order response. Read every FEE_CHECK_INTERVAL (outside the
        window boundary) until the buy shows up; give up after FEE_CHECK_FOR, or
        when force is set (shutdown), if the change isn't the buy alone (e.g. a
        redemption landed in between). pusd: use this reading instead of a new one."""
        w = self._fee_watch
        if not w:
            return
        now = time.time()
        if pusd is None and not force:
            period = PERIOD_SECONDS[self.period]
            into_window = now % period
            if (now - w["last"] < FEE_CHECK_INTERVAL
                    or into_window < 2 or period - into_window < 10):
                return
        w["last"] = now
        balance = pusd if pusd is not None else self.executor.read_balance()
        if balance is not None:
            fee = w["before"] - balance - w["spent"]
            if 0.001 <= fee <= FEE_MAX_SHARE * w["spent"]:
                self._fee_watch = {}
                self._apply_fee(w["trade_id"], fee, w["spent"])
                return
        if force or now - w["since"] > FEE_CHECK_FOR:
            self._fee_watch = {}
            print(f"  ⚠️  Couldn't measure the fee on trade {w['trade_id']}: pUSD didn't change "
                  f"by the buy alone. trades.csv shows its cost without the fee.")

    def _apply_fee(self, trade_id: int, fee: float, spent: float):
        """Add a measured fee to the trade's cost (open or awaiting its result),
        so its profit and trades.csv entry_cost match what left the wallet."""
        # Waiting list first: at the window boundary a trade is already queued
        # there while _traded still names it
        waiting = [p for p in self._pending_resolutions if p["trade_id"] == trade_id]
        if waiting:
            waiting[0]["cost"] += fee
        elif self._traded and self._trade_id == trade_id:
            self._trade_cost += fee
        self.stats.bankroll -= fee
        self.tracker.add_fee(trade_id, fee)
        print(f"  💸 Polymarket fee ${fee:.3f} on the ${spent:.2f} buy "
              f"(measured from pUSD): cost ${spent + fee:.2f}")

    def _tracked_marks(self) -> dict:
        """token_id -> value per share of every position the bot tracks: the
        open trade and trades awaiting a result at their mark, and booked wins
        (or split payouts) not yet redeemed at their payout."""
        marks = {}
        if self._traded and self._trade_token_id:
            marks[self._trade_token_id] = self._trade_price
        for p in self._pending_resolutions:
            marks[p["token_id"]] = p["mark"]
        for tok, u in self._unredeemed.items():
            marks[tok] = u["per_share"]
        return marks

    def _live_equity(self) -> tuple:
        """(equity, redeemable) in live mode, or (None, []) if any part couldn't
        be read, so the caller skips that sync instead of using a wrong figure.

        equity = pUSD + each tracked position at the shares the Safe still holds
                 × its value per share + redeemable winnings in markets the bot
                 didn't trade this session (from Polymarket's positions data).
        Reading the shares means a win counts exactly once, whether the bot,
        Polymarket's auto-redeem or nobody has redeemed it yet, and Polymarket's
        positions data can lag without making a win vanish or count twice.
        redeemable = [(condition_id, value)] still to redeem.
        """
        # pUSD first: a redemption landing between the reads then undercounts
        # once (shares gone, pUSD read before) instead of counting twice
        balance = self.executor.read_balance()
        if balance is None:
            return None, []
        marks = self._tracked_marks()
        held = {}
        for tok in marks:
            shares = self.executor.token_balance(tok)
            if shares is None:
                return None, []
            held[tok] = shares
        winnings = self.executor.redeemable_winnings()
        if winnings is None:
            return None, []
        for tok in [t for t in self._unredeemed if held[t] < HELD_DUST]:
            del self._unredeemed[tok]            # redeemed, by the bot or by Polymarket
        other = [(cid, v) for cid, v in winnings if cid not in self._session_cids]
        redeemable = [(u["condition_id"], held[tok] * u["per_share"])
                      for tok, u in self._unredeemed.items()] + other
        self._last_pusd = balance
        return balance + sum(held[t] * marks[t] for t in marks) + sum(v for _, v in other), redeemable

    def _redeem_winnings(self, winnings: list):
        """Submit redemption for every resolved winning position (live only)."""
        if self.dry_run or not self.executor._initialized or not winnings:
            return
        if not self.executor.can_redeem:
            total = round(sum(v for _, v in winnings), 2)
            if total != self._claim_note_total:      # say it once per change, not every window
                self._claim_note_total = total
                print(f"  💰 ${total:.2f} of winnings to claim in the Polymarket UI (no builder key)")
            return
        for cid, value in winnings:
            tx = self.executor.redeem(cid)
            if tx:
                print(f"  💰 Redeeming ${value:.2f} (tx {tx[:12]}…)")

    def _book_market_resolution(self, p: dict, payouts: dict):
        """Apply a queued trade's result now that Polymarket has resolved it.
        payouts is per-share, e.g. {"UP": 1.0, "DOWN": 0.0}, or 0.5/0.5 if split."""
        per_share = payouts.get(p["side"], 0.0)
        won = per_share >= 0.99
        winner = max(payouts, key=payouts.get) if max(payouts.values()) >= 0.99 else "SPLIT"
        payout = p["shares"] * per_share
        profit = p["exit_revenue"] + payout - p["cost"]
        # The bankroll held this position at shares × mark; now it's worth its payout
        self.stats.bankroll += payout - p["shares"] * p["mark"]
        if self.dry_run:
            if payout > 0:
                self._unclaimed_winnings += payout     # paper payout, shown as unclaimed
        elif per_share > 0 and p.get("token_id"):
            # Held until redeemed (by the bot or Polymarket's auto-redeem).
            # _live_equity values it from the shares still in the Safe.
            self._unredeemed[p["token_id"]] = {"condition_id": p["condition_id"],
                                               "per_share": per_share}
        if profit > 0:
            self.stats.record_win(profit)
            self.telegram.win_alert(profit, self.stats.total_pnl)
        else:
            self.stats.record_loss(-profit)
            self.telegram.loss_alert(-profit, self.stats.total_pnl)
        t = time.strftime("%H:%M", time.localtime(p["window_ts"]))
        print(f"  {'✅ WIN' if won else '❌ LOSS'} ({p['side']} in the {t} window; "
              f"Polymarket resolved {winner}) ${profit:+.2f} [market_outcome] | "
              f"P&L: ${self.stats.total_pnl:+.2f} | Bank: ${self.stats.bankroll:.2f}")
        self.tracker.close_trade(
            btc_final_price=p["closing_price"],
            opening_price=p["opening_price"],
            won=won,
            profit=profit,
            exit_revenue=p["exit_revenue"],
            resolution_method="market_outcome",
            claim_result=p["claim_result"],
            resolution_payout=payout,
            bankroll=self.stats.bankroll,
            trade_id=p["trade_id"],
        )

    # ── Hourly + shutdown ───────────────────────────────────────────

    def _check_hourly_summary(self):
        current_hour = int(time.time() // 3600)
        if current_hour != self._last_hour_check:
            self._last_hour_check = current_hour
            h = self.stats.hourly.to_dict()
            o = self.stats.to_dict()
            # (Live: the bankroll was synced to real equity at this window's start;
            # pUSD alone would leave out positions and unredeemed winnings.)
            real_pnl = self.stats.bankroll - self._session_start_balance

            print(f"\n{'═' * 55}")
            print(f"  📊 HOURLY SUMMARY")
            print(f"  This hour: {h['trades']} trades | "
                  f"{h['wins']}W/{h['losses']}L | "
                  f"P&L: ${h['pnl']:+.2f}")
            if h['trades'] > 0:
                print(f"  Avg edge: {h['avg_edge']*100:.1f}%")
            print(f"  Windows: {h['windows_seen']} seen, "
                  f"{h['windows_skipped']} skipped")
            print(f"  Overall: {o['total_trades']} trades | "
                  f"P&L: ${o['pnl']:+.2f} | Bank: ${o['bankroll']:.2f}")
            print(f"  💰 Real P&L (balance): ${real_pnl:+.2f} "
                  f"(${self._session_start_balance:.2f} → ${self.stats.bankroll:.2f})")
            if self._unclaimed_winnings > 0:
                print(f"  💰 Unclaimed: ${self._unclaimed_winnings:.2f}")
            print(f"{'═' * 55}\n")
            self.telegram.hourly_summary(h, o)
            self.stats.hourly.reset()

    def _session_avg(self, field: int, absolute: bool = False) -> float:
        """Mean of one field of this session's entries (0=price, 1=edge, 2=delta)."""
        vals = [abs(e[field]) if absolute else e[field] for e in self._session_entries]
        return sum(vals) / len(vals) if vals else 0.0

    def _handle_shutdown(self, signum, frame):
        print(f"\n\n🛑 Shutting down...")
        self._running = False
        self.price_feed.stop()
        # Tor (live) keeps running until this process exits, so these still work
        if self.executor._initialized:
            self.executor.cancel_all()
            self._check_fee(force=True)

        # Final real money sync
        synced = self.dry_run
        if not self.dry_run and self.executor._initialized:
            real_bal, _ = self._live_equity()
            if real_bal is not None:
                self.stats.bankroll = real_bal
                self._last_real_balance = real_bal
                synced = True

        real_pnl = self.stats.bankroll - self._session_start_balance
        o = self.stats.to_dict()
        print(f"\n{'═' * 55}")
        print(f"  FINAL: {o['total_trades']} trades | "
              f"{o['wins']}W/{o['losses']}L | "
              f"WR: {o['win_rate']:.1f}%")
        print(f"  Tracked P&L: ${o['pnl']:+.2f} | Bank: ${o['bankroll']:.2f}")
        print(f"  💰 {'Real' if synced else 'Estimated'} P&L: ${real_pnl:+.2f} "
              f"(${self._session_start_balance:.2f} → ${self.stats.bankroll:.2f})"
              f"{'' if synced else ' — final balances unreadable, so from the bot tracking'}")
        if self._unclaimed_winnings > 0:
            print(f"  💰 Unclaimed: ${self._unclaimed_winnings:.2f}")
        # Trades without a result still get a trades.csv row (result left blank)
        for p in self._pending_resolutions:
            t = time.strftime("%H:%M", time.localtime(p["window_ts"]))
            print(f"  ⏳ Not booked (no Polymarket result yet): {p['side']} in the {t} window, "
                  f"${p['cost']:.2f} staked")
            self.tracker.log_trade_unresolved("unresolved_at_shutdown", trade_id=p["trade_id"])
        if self._traded and self._trade_id:
            print(f"  ⏳ Open position at shutdown: {self._trade_side}, ${self._trade_cost:.2f} staked "
                  f"— resolves on Polymarket without the bot")
            self.tracker.log_trade_unresolved("open_at_shutdown", trade_id=self._trade_id)
        print(f"{'═' * 55}")

        self.telegram.status_update(o)

        self.tracker.log_session(
            start_time=self._session_start_time,
            end_time=time.time(),
            start_balance=self._session_start_balance,
            end_balance=self.stats.bankroll,
            tracked_pnl=o["pnl"],
            trades=o["total_trades"],
            wins=o["wins"],
            losses=o["losses"],
            avg_entry_price=self._session_avg(0),
            avg_edge=self._session_avg(1),
            avg_delta=self._session_avg(2, absolute=True),
        )

        time.sleep(1)
        sys.exit(0)


if __name__ == "__main__":
    bot = PolyBot()
    bot.start()
