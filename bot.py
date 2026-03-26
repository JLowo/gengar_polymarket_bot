#!/usr/bin/env python3
"""
PolyBot v13 — Recalibrated + Safety Systems

Strategy:
  - Brownian motion model with vol=0.12 (recalibrated from 0.08)
  - Entry gate: model confidence >= 80%, market price <= true_prob * 0.85
  - Position sizing: quarter-Kelly, $5–$25 per trade
  - Exit: hold all positions to resolution — no stops, no take-profit

Safety systems:
  1. CLOB health check: get_ok() before every trade; 3 consecutive
     failures halt trading and send Telegram alert. Auto-recovers
     when API comes back at next window boundary.
  2. Daily loss limit: if session P&L <= -DAILY_LOSS_LIMIT, halt trading.
  3. Balance-verified buys: snapshot USDC before/after; ghost fills
     caught even when API throws. Never cancels on timeout — returns
     UNVERIFIED_BUY for pending detection at next window boundary.
  4. Pending buy safety net: if buy unverified, check balance at next
     window boundary; retroactively track as filled if balance dropped.
  5. Window-boundary balance sync: real USDC balance overwrites internal
     tracking every 5 minutes. Corrects any accumulated drift.
  6. Minimum notional guard: skip sells below $5 notional; hold to
     resolution instead of hitting Polymarket's minimum-size rejection.
"""

import os
import sys
import time
import signal
import math
import statistics
from dotenv import load_dotenv

import logging
logging.getLogger("httpx").setLevel(logging.WARNING)

from market import get_current_market, current_window_ts, PERIOD_SECONDS
from price_feed import BinancePriceFeed
from strategy import evaluate, estimate_true_probability, get_skip_reason, StrategyConfig, TradingStats
from executor import Executor, FILLED, PARTIAL, FAILED, MAX_BUY_PRICE
from telegram_notifier import TelegramNotifier
from tracker import Tracker


BOT_VERSION = "2026.03.26"

FORCED_EXIT_START = 5
FORCED_EXIT_END = 1
POSITION_CHECK_INTERVAL = 3
MAX_EXIT_RETRIES = 3
EXIT_RETRY_COOLDOWN = 10


class PolyBot:
    def __init__(self):
        load_dotenv()

        self.dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
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
        )
        self.telegram = TelegramNotifier()
        self.tracker = Tracker(
            log_dir=os.getenv("LOG_DIR", "logs"),
            log_executions=os.getenv("LOG_EXECUTIONS", "false").lower() == "true",
        )
        self.stats = TradingStats(bankroll=initial_bankroll)
        self.stats.hourly.hour_start = time.time()

        self._running = False
        self._current_window: int = 0
        self._opening_price: float = 0.0
        self._last_hour_check: int = 0

        # Trade state
        self._traded: bool = False
        self._trade_attempted: bool = False
        self._trade_side: str = ""
        self._trade_price: float = 0.0
        self._trade_cost: float = 0.0
        self._trade_shares: float = 0.0
        self._trade_token_id: str = ""

        # Exit state
        self._exited: bool = False
        self._exit_revenue: float = 0.0
        self._exit_shares_sold: float = 0.0
        self._residual_shares: float = 0.0  # Shares left after partial fill
        self._last_position_check: float = 0.0
        self._last_status_print: float = 0.0
        self._last_tick_context: dict = {}   # last entry-window state, for window-end signal logging
        self._session_start_time: float = time.time()
        self._recent_window_deltas: list = []  # rolling abs(close_delta_pct) per window
        self._exit_retries: int = 0
        self._exit_gave_up: bool = False
        self._last_sell_price_seen: float = 0.0  # last observed sell price during hold period

        # Pending buy (unverified — Polygon settlement too slow)
        self._pending_buy_side: str = ""
        self._pending_buy_price: float = 0.0
        self._pending_buy_amount: float = 0.0
        self._pending_buy_shares: float = 0.0
        self._pending_buy_token_id: str = ""
        self._pending_buy_edge: float = 0.0
        self._pending_buy_delta: float = 0.0
        self._balance_before_buy: float = 0.0

        # Unclaimed
        self._unclaimed_winnings: float = 0.0

        # Real balance tracking (source of truth)
        self._session_start_balance: float = 0.0
        self._last_real_balance: float = 0.0

        # Price cache
        self._cached_up: float = 0.50
        self._cached_down: float = 0.50
        self._price_last_fetched: float = 0.0
        self._PRICE_REFRESH: float = 5.0

        # Circuit breaker — detects CLOB API degradation
        self._consecutive_buy_failures: int = 0
        self._clob_halted: bool = False
        self._HALT_AFTER_FAILURES: int = 3
        self._daily_loss_halted: bool = False

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
            balance = self.executor.get_balance()
            print(f"  USDC balance: ${balance:.2f}")
            self.stats.bankroll = balance
            self._session_start_balance = balance
            self._last_real_balance = balance
            self.tracker.set_session_balance(balance)
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
            print(f"  📌 Open: ${btc_price:,.2f}")

        # HOLDING: active position management
        if self._traded and not self._exited and not self._exit_gave_up:
            self._manage_position(btc_price, seconds_remaining, now)
            return

        # Already done
        if self._traded or self._trade_attempted:
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
        updated_prob = estimate_true_probability(btc_delta_pct, seconds_remaining)

        if self._trade_side == "DOWN":
            our_prob = 1.0 - updated_prob
        else:
            our_prob = updated_prob

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

        # Get current sell price (for tracking only)
        if self.dry_run:
            current_sell_price = round(max(our_prob, 0.01), 2)
        else:
            sell_probe = round(self._trade_shares * self._trade_price, 2)
            current_sell_price = self.executor.get_market_price(
                self._trade_token_id, "SELL", max(sell_probe, 1.0)
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

    def _exit_position(self, sell_price: float, seconds_remaining: float, reason: str):
        if self.dry_run:
            revenue = self._trade_shares * sell_price
            self._exited = True
            self._exit_revenue = revenue
            self.stats.bankroll += revenue
            profit = revenue - self._trade_cost
            print(f"  💰 EXIT ({reason}, paper): {self._trade_shares:.0f} shares @ "
                  f"${sell_price:.3f} = ${revenue:.2f} | Profit: ${profit:+.2f}")
            return

        result = self.executor.sell(
            token_id=self._trade_token_id,
            shares=self._trade_shares,
            price=sell_price,
        )

        if result.success:
            self._exit_revenue += result.amount_usd
            self._exit_shares_sold += result.shares
            self._residual_shares = result.shares_remaining
            self.stats.bankroll += result.amount_usd

            if result.status == PARTIAL and result.shares_remaining >= 1:
                # Partial fill: got some USDC back, still have shares
                print(f"  💰 EXIT ({reason}, partial): ~{result.shares:.0f} shares @ "
                      f"${result.price:.3f} = ${result.amount_usd:.2f} | "
                      f"~{result.shares_remaining:.0f} shares remaining → holding to resolution")
                # Update shares but keep original cost for clean P&L math
                self._trade_shares = result.shares_remaining
                # Mark exited — residual resolves at window close
                self._exited = True
            else:
                # Full fill (or residual < 1 share)
                self._exited = True
                profit = self._exit_revenue - self._trade_cost
                print(f"  💰 EXIT ({reason}): {result.shares:.0f} shares @ "
                      f"${result.price:.3f} = ${result.amount_usd:.2f} | "
                      f"Profit: ${profit:+.2f}")
        elif "hold to resolution" in result.error:
            # Below $5 minimum — can't sell, hold to resolution
            notional = self._trade_shares * sell_price
            print(f"  📌 Can't sell: ${notional:.2f} below $5 minimum — holding to resolution")
            self._exit_gave_up = True  # Skip further exit attempts
        else:
            self._exit_retries += 1
            if self._exit_retries >= MAX_EXIT_RETRIES:
                print(f"  ❌ Exit failed {MAX_EXIT_RETRIES} times ({reason}) — "
                      f"holding to resolution")
                self._exit_gave_up = True
            else:
                print(f"  ⚠️  Exit failed ({reason}, attempt "
                      f"{self._exit_retries}/{MAX_EXIT_RETRIES}): {result.error}")
                self._last_position_check = time.time() + EXIT_RETRY_COOLDOWN - POSITION_CHECK_INTERVAL

    # ── Window management ───────────────────────────────────────────

    def _on_new_window(self, window_ts: int, closing_btc_price: float = 0.0):
        if self._current_window > 0:
            # Record closing delta for rolling vol calculation
            if self._opening_price > 0 and closing_btc_price > 0:
                closing_delta = abs((closing_btc_price - self._opening_price) / self._opening_price * 100)
                self._recent_window_deltas.append(closing_delta)
                if len(self._recent_window_deltas) > self._rolling_vol_windows:
                    self._recent_window_deltas.pop(0)
            # Detect pending buy that settled after our verification timeout
            if self._pending_buy_side and not self._traded:
                if not self.dry_run and self.executor._initialized:
                    real_bal = self.executor.get_balance()
                    if real_bal > 0 and self._balance_before_buy > 0:
                        spent = self._balance_before_buy - real_bal
                        if spent > 1.0:
                            # The buy DID go through — retroactively track it
                            est_shares = spent / self._pending_buy_price if self._pending_buy_price > 0 else 0
                            print(f"\n  👻 LATE FILL: balance dropped ${spent:.2f} since buy attempt")
                            print(f"     Retroactively tracking: ~{est_shares:.0f} shares "
                                  f"{self._pending_buy_side} @ ${self._pending_buy_price:.3f}")

                            self._traded = True
                            self._trade_side = self._pending_buy_side
                            self._trade_price = self._pending_buy_price
                            self._trade_cost = spent
                            self._trade_shares = est_shares
                            self._trade_token_id = self._pending_buy_token_id
                            self.stats.bankroll = real_bal
                            self._last_real_balance = real_bal
                            self.stats.hourly.record_trade(
                                self._pending_buy_edge, self._pending_buy_delta)
                            self.tracker.open_trade(
                                window_ts=self._current_window,
                                side=self._pending_buy_side,
                                entry_price=self._pending_buy_price,
                                entry_shares=est_shares,
                                entry_cost=spent,
                                edge=self._pending_buy_edge,
                                prob=0.0,
                                btc_delta=self._pending_buy_delta,
                                seconds_remaining=0.0,
                                version=BOT_VERSION,
                            )

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

            # Sync real balance at window boundary (catches any drift)
            if not self.dry_run and self.executor._initialized:
                real_bal = self.executor.get_balance()
                if real_bal > 0:
                    drift = abs(real_bal - self.stats.bankroll)
                    if drift > 0.50:
                        print(f"  🔄 Balance sync: ${self.stats.bankroll:.2f} → "
                              f"${real_bal:.2f} (drift ${drift:.2f})")
                    self.stats.bankroll = real_bal
                    self._last_real_balance = real_bal

        self._current_window = window_ts
        self._opening_price = 0.0
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
        self._pending_buy_side = ""
        self._pending_buy_price = 0.0
        self._pending_buy_amount = 0.0
        self._pending_buy_shares = 0.0
        self._pending_buy_token_id = ""
        self._pending_buy_edge = 0.0
        self._pending_buy_delta = 0.0
        self._balance_before_buy = 0.0

        t = time.strftime("%H:%M:%S", time.localtime(window_ts))
        print(f"\n{'─' * 55}")
        print(f"🕐 {t} | Trades: {self.stats.total_trades} | "
              f"W/L: {self.stats.wins}/{self.stats.losses} | "
              f"P&L: ${self.stats.total_pnl:+.2f}")
        print(f"{'─' * 55}")

        # Circuit breaker auto-recovery: ping CLOB each new window
        if self._clob_halted and not self.dry_run and self.executor._initialized:
            try:
                self.executor.client.get_ok()
                self._clob_halted = False
                self._consecutive_buy_failures = 0
                print(f"  ✅ CLOB recovered (health check OK) — resuming trades")
            except Exception:
                print(f"  🔌 CLOB health check still failing — staying halted")

    # ── Market prices (cached, complement engine) ───────────────────

    def _compute_realized_vol(self) -> float:
        """Rolling std dev of recent window closing deltas.

        Returns the realized vol to pass into the Brownian motion model.
        Falls back to 0.12 until at least 6 windows have accumulated.
        Floored/capped to prevent extreme values breaking the model.
        """
        min_samples = max(6, self._rolling_vol_windows // 2)
        if len(self._recent_window_deltas) >= min_samples:
            vol = statistics.stdev(self._recent_window_deltas)
            return max(self._vol_floor, min(self._vol_cap, vol))
        return self._vol_fallback

    def _get_market_prices(self, btc_price: float, seconds_remaining: float) -> tuple:
        if self.dry_run or not self.executor._initialized:
            if self._opening_price <= 0:
                return 0.50, 0.50
            delta_pct = (btc_price - self._opening_price) / self._opening_price
            time_factor = 1 - (seconds_remaining / PERIOD_SECONDS[self.period])
            lag_factor = min(time_factor * 0.7, 0.85)
            implied = 0.5 + lag_factor * math.tanh(delta_pct * 500) * 0.45
            up = round(min(max(implied, 0.02), 0.98), 3)
            return up, round(1.0 - up, 3)

        now = time.time()
        if now - self._price_last_fetched < self._PRICE_REFRESH:
            return self._cached_up, self._cached_down

        try:
            market = get_current_market(self.period)
            if not market:
                return self._cached_up, self._cached_down

            probe_amount = 5.0
            up_price = self.executor.get_market_price(market.token_id_up, "BUY", probe_amount)
            down_price = self.executor.get_market_price(market.token_id_down, "BUY", probe_amount)

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

    # ── Entry ───────────────────────────────────────────────────────

    def _execute_trade(self, sig, seconds_remaining: float):
        self._trade_attempted = True

        # ── Circuit breaker: CLOB health check ───────────────────
        if self._clob_halted:
            print(f"  🔌 CLOB HALTED — skipping trade ({self._consecutive_buy_failures} consecutive failures)")
            return

        if not self.dry_run and self.executor._initialized:
            try:
                self.executor.client.get_ok()
            except Exception as e:
                self._consecutive_buy_failures += 1
                print(f"  🔌 CLOB health check failed: {e}")
                if self._consecutive_buy_failures >= self._HALT_AFTER_FAILURES:
                    self._clob_halted = True
                    msg = (f"🔌 CLOB HALTED after {self._consecutive_buy_failures} "
                           f"consecutive health check failures — stopping trades until recovery")
                    print(f"\n  {msg}")
                    self.telegram.status_update({"alert": msg})
                return

        # ── Daily loss limit ─────────────────────────────────────
        if self._daily_loss_halted:
            print(f"  🛑 DAILY LOSS LIMIT — session P&L ${self.stats.total_pnl:+.2f} "
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

        market = get_current_market(self.period) if not self.dry_run else None
        token_id = ""
        if market:
            token_id = market.token_id_up if sig.side == "UP" else market.token_id_down
        else:
            token_id = f"DRY-{sig.side}-{self._current_window}"

        slug = f"btc-updown-{self.period}m-{self._current_window}"
        trade_amount = round(sig.kelly_size, 2)

        print(f"\n  🎯 {sig.side} | edge={sig.edge:.3f} | "
              f"prob={sig.true_prob:.2f} | BTC Δ={sig.btc_delta_pct:+.3f}%")
        print(f"     Kelly: ${trade_amount:.2f} | mkt ${sig.market_price:.3f} | T-{seconds_remaining:.0f}s")

        # Preview actual market price, re-check edge, then pass price into buy()
        # so executor skips a second fetch (saves one Tor roundtrip ~500ms)
        hint_price = 0.0
        if not self.dry_run and self.executor._initialized:
            actual_price = self.executor.get_market_price(token_id, "BUY", trade_amount)
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

        result = self.executor.buy(token_id=token_id, amount_usd=trade_amount, price=hint_price)

        if result.success:
            self._consecutive_buy_failures = 0  # Reset circuit breaker
            self._traded = True
            self._trade_side = sig.side
            self._trade_price = result.price
            self._trade_cost = result.amount_usd
            self._trade_shares = result.shares
            self._trade_token_id = token_id

            self.stats.bankroll -= result.amount_usd
            self.stats.hourly.record_trade(sig.edge, sig.btc_delta_pct)

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
            self.tracker.open_trade(
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
            if result.error == "UNVERIFIED_BUY":
                # Order likely filled but Polygon hasn't settled.
                # Save details — window boundary sync will detect the fill.
                self._pending_buy_side = sig.side
                self._pending_buy_price = result.price
                self._pending_buy_amount = result.amount_usd
                self._pending_buy_shares = result.shares
                self._pending_buy_token_id = token_id
                self._pending_buy_edge = sig.edge
                self._pending_buy_delta = sig.btc_delta_pct
                self._balance_before_buy = self.stats.bankroll
                print(f"  ⏳ Buy sent but unverified — will detect via balance sync")
            else:
                print(f"  ❌ Buy failed: {result.error}")
                # Circuit breaker: track consecutive API failures
                err = str(result.error).lower()
                if "request exception" in err or "service not ready" in err or "status_code=none" in err:
                    self._consecutive_buy_failures += 1
                    if self._consecutive_buy_failures >= self._HALT_AFTER_FAILURES:
                        self._clob_halted = True
                        msg = (f"🔌 CLOB HALTED after {self._consecutive_buy_failures} "
                               f"consecutive API failures — stopping trades until restart")
                        print(f"\n  {msg}")
                        self.telegram.status_update({"alert": msg})

    # ── Resolve (partial fill aware) ────────────────────────────────

    def _resolve_previous_trade(self):
        if self._exited:
            btc_price, _ = self.price_feed.get_price()
            result = self.tracker.close_trade(
                btc_final_price=btc_price,
                opening_price=self._opening_price,
                won=True,  # close_trade overrides based on profit for "exited"
                exit_revenue=self._exit_revenue,
                resolution_method="exited",
            )
            profit = result["profit"]
            won = result["won"]
            if won:
                self.stats.record_win(profit)
            else:
                self.stats.record_loss(abs(profit))
            result_emoji = "✅ WIN" if won else "❌ LOSS"
            residual_note = f" (~{self._residual_shares:.0f} residual)" if self._residual_shares >= 1 else ""
            print(f"  {result_emoji} (exited{residual_note}) ${profit:+.2f} | "
                  f"P&L: ${self.stats.total_pnl:+.2f} | "
                  f"Bank: ${self.stats.bankroll:.2f}")
            if won:
                self.telegram.win_alert(profit, self.stats.total_pnl)
            else:
                self.telegram.loss_alert(abs(profit), self.stats.total_pnl)
            return

        original_cost = self._trade_cost
        remaining_shares = self._trade_shares

        # ── Dry run: Binance price fallback ──────────────────────────
        if self.dry_run:
            btc_price, _ = self.price_feed.get_price()
            if self._opening_price <= 0 or btc_price <= 0:
                return
            won = (btc_price >= self._opening_price) == (self._trade_side == "UP")
            self._record_resolution(
                won=won,
                original_cost=original_cost,
                remaining_shares=remaining_shares,
                resolution_method="binance_fallback",
                claim_revenue=0.0,
            )
            return

        # ── Live: attempt claim sell first — result is the truth ─────
        # Binance price and oracle can disagree when BTC is near the opening
        # price at resolution. The claim sell result is ground truth:
        #   - Sell succeeds at ~$0.99 → shares had value → won
        #   - "no match" or near-zero fill → shares worthless → lost
        won = None
        claim_revenue = 0.0
        claim_result = "not_attempted"
        resolution_method = "claim_sell"

        claim_notional = remaining_shares * 0.99
        live_token = (self._trade_token_id
                      and not self._trade_token_id.startswith("DRY-")
                      and self.executor._initialized)

        # Short-circuit: if last observed sell price is below $0.50, market has
        # already priced these shares as worthless — skip the claim API call.
        if self._last_sell_price_seen > 0 and self._last_sell_price_seen < 0.50:
            btc_price, _ = self.price_feed.get_price()
            result = self.tracker.close_trade(
                btc_final_price=btc_price,
                opening_price=self._opening_price,
                won=False,
                exit_revenue=self._exit_revenue,
                remaining_shares=remaining_shares,
                resolution_method="market_price",
                claim_result="skipped_losing",
            )
            profit = result["profit"]
            net_loss = abs(profit)
            self.stats.record_loss(net_loss)
            partial_note = f" (partial exit ${self._exit_revenue:.2f})" if self._exit_revenue > 0 else ""
            print(f"  ❌ LOSS{partial_note} -${net_loss:.2f} [market_price] | "
                  f"P&L: ${self.stats.total_pnl:+.2f} | "
                  f"Bank: ${self.stats.bankroll:.2f}")
            self.telegram.loss_alert(net_loss, self.stats.total_pnl)
            return

        if live_token and claim_notional >= 5.0:
            print(f"  💰 Claiming: sell {remaining_shares:.0f} shares @ $0.99...")
            claim = self.executor.sell(
                token_id=self._trade_token_id,
                shares=remaining_shares,
                price=0.99,
            )
            if claim.success and claim.amount_usd > remaining_shares * 0.50:
                # Got meaningful USDC back → shares had value → won
                won = True
                claim_revenue = claim.amount_usd
                claim_result = "filled"
                self.stats.bankroll += claim_revenue
            elif "no match" in claim.error.lower() or (
                claim.success and claim.amount_usd < remaining_shares * 0.10
            ):
                # No buyers for these shares → resolved against us → lost
                won = False
                claim_result = "no_match"
            else:
                # Network error or ambiguous result — fall back to balance check
                print(f"  ⚠️  Claim inconclusive ({claim.error}) — falling back to balance check")
                claim_result = "inconclusive"
                resolution_method = "balance_check"
        else:
            # Below $5 notional or no valid token — use balance check
            resolution_method = "balance_check"
            if live_token and claim_notional < 5.0:
                print(f"  💰 {remaining_shares:.0f} shares below $5 min — using balance check")

        # ── Balance-based fallback ────────────────────────────────────
        if won is None and self.executor._initialized:
            time.sleep(4)  # Wait for auto-resolution to settle on-chain
            current_balance = self.executor.get_balance()
            if current_balance > 0 and self._last_real_balance > 0:
                # self._last_real_balance = balance at last window boundary (pre-trade)
                # expected_if_lost = that balance minus what we spent
                expected_if_lost = self._last_real_balance - original_cost
                balance_delta = current_balance - expected_if_lost
                won = balance_delta > (remaining_shares * 0.50)
                print(f"  🔍 Balance check: expected_if_lost=${expected_if_lost:.2f} "
                      f"actual=${current_balance:.2f} delta=${balance_delta:+.2f} "
                      f"→ {'WON' if won else 'LOST'}")
            else:
                # Last resort: Binance price
                resolution_method = "binance_fallback"
                btc_price, _ = self.price_feed.get_price()
                if self._opening_price > 0 and btc_price > 0:
                    won = (btc_price >= self._opening_price) == (self._trade_side == "UP")
                    print(f"  ⚠️  Balance unavailable — using Binance fallback")
                else:
                    print(f"  ⚠️  Cannot determine resolution outcome — skipping")
                    return

        if won is None:
            return

        self._record_resolution(
            won=won,
            original_cost=original_cost,
            remaining_shares=remaining_shares,
            resolution_method=resolution_method,
            claim_revenue=claim_revenue,
            claim_result=claim_result,
        )

    def _record_resolution(
        self, won: bool, original_cost: float, remaining_shares: float,
        resolution_method: str, claim_revenue: float, claim_result: str = "not_attempted",
    ):
        """Apply win/loss to stats, print result, alert Telegram, log to tracker."""
        # Bankroll adjustment for unclaimed wins (claimed wins already added by caller)
        if won and claim_revenue <= 0:
            resolution_payout = remaining_shares * 1.0
            self.stats.bankroll += resolution_payout
            self._unclaimed_winnings += resolution_payout

        btc_price, _ = self.price_feed.get_price()
        result = self.tracker.close_trade(
            btc_final_price=btc_price,
            opening_price=self._opening_price,
            won=won,
            exit_revenue=self._exit_revenue,
            claim_revenue=claim_revenue,
            remaining_shares=remaining_shares,
            resolution_method=resolution_method,
            claim_result=claim_result,
        )
        profit = result["profit"]

        if won:
            self.stats.record_win(profit)
            partial_note = f" (partial exit ${self._exit_revenue:.2f})" if self._exit_revenue > 0 else ""
            claimed_note = " (claimed)" if claim_revenue > 0 else " (unclaimed)"
            print(f"  ✅ WIN{partial_note}{claimed_note} +${profit:.2f} [{resolution_method}] | "
                  f"P&L: ${self.stats.total_pnl:+.2f} | "
                  f"Bank: ${self.stats.bankroll:.2f}")
            self.telegram.win_alert(profit, self.stats.total_pnl)
        else:
            net_loss = abs(profit)
            self.stats.record_loss(net_loss)
            partial_note = f" (partial exit ${self._exit_revenue:.2f})" if self._exit_revenue > 0 else ""
            print(f"  ❌ LOSS{partial_note} -${net_loss:.2f} [{resolution_method}] | "
                  f"P&L: ${self.stats.total_pnl:+.2f} | "
                  f"Bank: ${self.stats.bankroll:.2f}")
            self.telegram.loss_alert(net_loss, self.stats.total_pnl)

    # ── Hourly + shutdown ───────────────────────────────────────────

    def _check_hourly_summary(self):
        current_hour = int(time.time() // 3600)
        if current_hour != self._last_hour_check:
            self._last_hour_check = current_hour
            h = self.stats.hourly.to_dict()
            o = self.stats.to_dict()

            # Sync real balance for accuracy
            if not self.dry_run and self.executor._initialized:
                real_bal = self.executor.get_balance()
                if real_bal > 0:
                    self.stats.bankroll = real_bal
                    self._last_real_balance = real_bal
                    o["bankroll"] = real_bal

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

    def _handle_shutdown(self, signum, frame):
        print(f"\n\n🛑 Shutting down...")
        self._running = False
        self.price_feed.stop()
        if self.executor._initialized:
            self.executor.cancel_all()

        # Final real balance sync
        if not self.dry_run and self.executor._initialized:
            real_bal = self.executor.get_balance()
            if real_bal > 0:
                self.stats.bankroll = real_bal
                self._last_real_balance = real_bal

        real_pnl = self.stats.bankroll - self._session_start_balance
        o = self.stats.to_dict()
        print(f"\n{'═' * 55}")
        print(f"  FINAL: {o['total_trades']} trades | "
              f"{o['wins']}W/{o['losses']}L | "
              f"WR: {o['win_rate']:.1f}%")
        print(f"  Tracked P&L: ${o['pnl']:+.2f} | Bank: ${o['bankroll']:.2f}")
        print(f"  💰 Real P&L: ${real_pnl:+.2f} "
              f"(${self._session_start_balance:.2f} → ${self.stats.bankroll:.2f})")
        if self._unclaimed_winnings > 0:
            print(f"  💰 Unclaimed: ${self._unclaimed_winnings:.2f}")
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
            avg_entry_price=self.stats.hourly.avg_edge,   # proxy via hourly stats
            avg_edge=self.stats.hourly.avg_edge,
            avg_delta=self.stats.hourly.avg_delta,
        )

        time.sleep(1)
        sys.exit(0)


if __name__ == "__main__":
    bot = PolyBot()
    bot.start()
