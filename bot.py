#!/usr/bin/env python3
"""
PolyBot v11 — Balance-Verified Everything

Fixes from live run analysis ($79 → $63):
  1. Balance-verified buys AND sells: snapshots USDC before/after every
     order. Ghost fills caught automatically. Real cost/revenue used.
  2. Minimum notional guard: if sell notional < $5, don't attempt —
     hold to resolution instead of getting "Size lower than minimum" trap.
  3. Claim guard: if remaining shares × $0.99 < $5, skip sell claim.
     Shares auto-resolve at $1.00 on Polymarket.
  4. Window-boundary balance sync: real USDC balance replaces internal
     tracking at every window transition. Drift detected and logged.
  5. Real P&L line: session_start_balance → current_balance shown
     alongside tracked P&L in hourly/shutdown summaries.
  6. Silenced 'price check failed: no match' spam.

Position management (from v9.2):
  1. Price stop-loss: sell price < buy × (1 - 40%)
  2. Prob stop-loss: probability < 35%
  3. Take-profit: return ≥ 30%
  4. Forced exit: T-5s, sell if profitable
"""

import os
import sys
import time
import signal
import math
from dotenv import load_dotenv

import logging
logging.getLogger("httpx").setLevel(logging.WARNING)

from market import get_current_market, current_window_ts, PERIOD_SECONDS
from price_feed import BinancePriceFeed
from strategy import evaluate, estimate_true_probability, StrategyConfig, TradingStats
from executor import Executor, FILLED, PARTIAL, FAILED
from telegram_notifier import TelegramNotifier


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
            min_edge=float(os.getenv("MIN_EDGE", "0.03")),
            entry_window_start=int(os.getenv("ENTRY_WINDOW_START", "240")),
            entry_window_end=int(os.getenv("ENTRY_WINDOW_END", "10")),
            kelly_fraction=float(os.getenv("KELLY_FRACTION", "0.25")),
            min_bet=float(os.getenv("MIN_BET", "1.0")),
            max_bet=float(os.getenv("MAX_BET", "25.0")),
        )

        self.take_profit_pct = float(os.getenv("TAKE_PROFIT_PCT", "0.30"))
        self.stop_loss_pct = float(os.getenv("STOP_LOSS_PCT", "0.40"))
        self.stop_loss_prob = float(os.getenv("STOP_LOSS_PROB", "0.35"))

        initial_bankroll = float(os.getenv("BANKROLL", "100.0"))

        self.price_feed = BinancePriceFeed()
        self.executor = Executor(
            private_key=os.getenv("PRIVATE_KEY", ""),
            safe_address=os.getenv("SAFE_ADDRESS", ""),
            dry_run=self.dry_run,
        )
        self.telegram = TelegramNotifier()
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
        self._exit_retries: int = 0
        self._exit_gave_up: bool = False

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
        tp = self.take_profit_pct * 100
        sl_price = self.stop_loss_pct * 100
        sl_prob = self.stop_loss_prob * 100
        print("=" * 55)
        print(f"  PolyBot v11 — Balance-Verified Everything")
        print(f"  Mode: {'DRY RUN' if self.dry_run else '🔴 LIVE TRADING'}")
        print(f"  Kelly: {kf*100:.0f}% fraction | "
              f"Bets: ${self.strategy_config.min_bet:.0f}–${self.strategy_config.max_bet:.0f}")
        print(f"  Min edge: {self.strategy_config.min_edge*100:.1f}%")
        print(f"  Entry: T-{self.strategy_config.entry_window_start}s to "
              f"T-{self.strategy_config.entry_window_end}s")
        print(f"  Take profit: {tp:.0f}% | Max buy price: $0.75")
        print(f"  Stop loss: price -{sl_price:.0f}% OR prob < {sl_prob:.0f}%")
        print(f"  Sells: balance-verified (partial fill aware)")
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
        else:
            print("  [dry run — no wallet connection]")
            self._session_start_balance = self.stats.bankroll
            self._last_real_balance = self.stats.bankroll

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
            time.sleep(1)

    def _tick(self):
        now = time.time()
        period_secs = PERIOD_SECONDS[self.period]
        window_ts = int(now) - (int(now) % period_secs)

        if window_ts != self._current_window:
            self._on_new_window(window_ts)

        seconds_remaining = (window_ts + period_secs) - now

        btc_price, is_fresh = self.price_feed.get_price()
        if not is_fresh or btc_price <= 0:
            return

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

        signal_result = evaluate(
            btc_price=btc_price,
            opening_price=self._opening_price,
            up_market_price=up_price,
            down_market_price=down_price,
            seconds_remaining=seconds_remaining,
            bankroll=self.stats.bankroll,
            config=self.strategy_config,
        )

        if signal_result:
            self._execute_trade(signal_result, seconds_remaining)

        if int(now) % 30 == 0:
            delta = ((btc_price - self._opening_price) / self._opening_price * 100) if self._opening_price > 0 else 0
            d = "↑" if delta > 0 else "↓" if delta < 0 else "→"
            state = "HOLDING" if self._traded else "IDLE"
            print(
                f"  ⏱  T-{seconds_remaining:5.1f}s | "
                f"BTC ${btc_price:,.2f} {d}{abs(delta):.3f}% | "
                f"UP ${up_price:.3f} DN ${down_price:.3f} | "
                f"P&L ${self.stats.total_pnl:+.2f} [{state}]"
            )

    # ── Active position management ──────────────────────────────────

    def _manage_position(self, btc_price: float, seconds_remaining: float, now: float):
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
            if int(now) % 30 == 0:
                d = "↑" if btc_delta_pct > 0 else "↓" if btc_delta_pct < 0 else "→"
                print(
                    f"  ⏱  T-{seconds_remaining:5.1f}s | "
                    f"BTC {d}{abs(btc_delta_pct):.3f}% | "
                    f"Prob: {our_prob:.2f} | "
                    f"P&L ${self.stats.total_pnl:+.2f} [HOLDING]"
                )
            return

        self._last_position_check = now

        # Get current sell price
        if self.dry_run:
            current_sell_price = round(max(our_prob, 0.01), 2)
        else:
            sell_probe = round(self._trade_shares * self._trade_price, 2)
            current_sell_price = self.executor.get_market_price(
                self._trade_token_id, "SELL", max(sell_probe, 1.0)
            )

        if current_sell_price <= 0:
            return

        current_value = self._trade_shares * current_sell_price
        unrealized_pnl = current_value - self._trade_cost
        return_pct = (current_sell_price - self._trade_price) / self._trade_price if self._trade_price > 0 else 0

        # Check 1: Price stop-loss
        price_floor = self._trade_price * (1.0 - self.stop_loss_pct)
        if current_sell_price <= price_floor:
            print(f"\n  🛑 PRICE STOP: sell ${current_sell_price:.3f} ≤ "
                  f"floor ${price_floor:.3f} (-{self.stop_loss_pct:.0%}) | "
                  f"Saving ${current_value:.2f} of ${self._trade_cost:.2f}")
            self._exit_position(current_sell_price, seconds_remaining, "price-stop")
            return

        # Check 2: Prob stop-loss
        if our_prob < self.stop_loss_prob:
            print(f"\n  🛑 PROB STOP: prob {our_prob:.2f} < {self.stop_loss_prob:.2f} | "
                  f"BTC Δ{btc_delta_pct:+.3f}% | "
                  f"sell @ ${current_sell_price:.3f}")
            self._exit_position(current_sell_price, seconds_remaining, "prob-stop")
            return

        # Check 3: Take-profit
        if return_pct >= self.take_profit_pct:
            print(f"\n  🎯 TAKE PROFIT: return {return_pct:.0%} ≥ {self.take_profit_pct:.0%} | "
                  f"sell @ ${current_sell_price:.3f} (bought @ ${self._trade_price:.3f})")
            self._exit_position(current_sell_price, seconds_remaining, "take-profit")
            return

        # Check 4: Forced exit at T-5s
        if FORCED_EXIT_START >= seconds_remaining >= FORCED_EXIT_END:
            if current_sell_price > self._trade_price:
                print(f"\n  ⏰ FORCED EXIT: T-{seconds_remaining:.0f}s | "
                      f"sell @ ${current_sell_price:.3f} (profitable)")
                self._exit_position(current_sell_price, seconds_remaining, "forced-exit")
            else:
                print(f"  ⏰ T-{seconds_remaining:.0f}s | "
                      f"sell @ ${current_sell_price:.3f} not profitable — holding to resolution")
            return

        # Status line
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

    def _on_new_window(self, window_ts: int):
        if self._current_window > 0:
            self.stats.hourly.record_window(self._traded)
            if self._traded:
                self._resolve_previous_trade()

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
        self._cached_up = 0.50
        self._cached_down = 0.50
        self._price_last_fetched = 0.0

        t = time.strftime("%H:%M:%S", time.localtime(window_ts))
        print(f"\n{'─' * 55}")
        print(f"🕐 {t} | Trades: {self.stats.total_trades} | "
              f"W/L: {self.stats.wins}/{self.stats.losses} | "
              f"P&L: ${self.stats.total_pnl:+.2f}")
        print(f"{'─' * 55}")

    # ── Market prices (cached, complement engine) ───────────────────

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

        # Preview actual market price and re-check edge
        if not self.dry_run and self.executor._initialized:
            actual_price = self.executor.get_market_price(token_id, "BUY", trade_amount)
            if actual_price > 0:
                actual_edge = sig.true_prob - actual_price
                print(f"  📊 Actual price: ${actual_price:.3f} (edge: {actual_edge:.3f})")

                if actual_edge < self.strategy_config.min_edge:
                    print(f"  ⚠️  Edge gone at market price — skipping")
                    return

        result = self.executor.buy(token_id=token_id, amount_usd=trade_amount)

        if result.success:
            self._traded = True
            self._trade_side = sig.side
            self._trade_price = result.price
            self._trade_cost = result.amount_usd
            self._trade_shares = result.shares
            self._trade_token_id = token_id

            self.stats.bankroll -= result.amount_usd
            self.stats.hourly.record_trade(sig.edge, sig.btc_delta_pct)

            tp_price = result.price * (1 + self.take_profit_pct)
            sl_price = result.price * (1 - self.stop_loss_pct)
            mode = "PAPER" if self.dry_run else "LIVE"
            print(f"  ✅ {mode}: {result.shares:.0f} shares @ "
                  f"${result.price:.3f} = ${result.amount_usd:.2f}")
            print(f"     TP @ ${tp_price:.3f} ({self.take_profit_pct:.0%}) | "
                  f"SL @ ${sl_price:.3f} (-{self.stop_loss_pct:.0%}) or prob < {self.stop_loss_prob:.0%}")

            self.telegram.trade_alert(
                side=sig.side, price=result.price, amount=result.amount_usd,
                market_slug=slug, dry_run=self.dry_run,
                edge=sig.edge, kelly_size=sig.kelly_size,
            )
        else:
            print(f"  ❌ Buy failed: {result.error}")

    # ── Resolve (partial fill aware) ────────────────────────────────

    def _resolve_previous_trade(self):
        if self._exited:
            # _trade_cost = original cost (never modified)
            # _exit_revenue = cumulative USDC received from all sells
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
            return

        # Held through resolution (may have partial exit revenue)
        btc_price, _ = self.price_feed.get_price()
        if self._opening_price <= 0 or btc_price <= 0:
            return

        won = (btc_price >= self._opening_price) == (self._trade_side == "UP")
        original_cost = self._trade_cost  # Never modified
        remaining_shares = self._trade_shares  # Reduced on partial fills

        if won:
            # Remaining shares pay $1 each at resolution
            resolution_payout = remaining_shares * 1.0
            total_received = self._exit_revenue + resolution_payout
            profit = total_received - original_cost

            if self._trade_token_id and not self._trade_token_id.startswith("DRY-"):
                claim_notional = remaining_shares * 0.99
                if claim_notional < 5.0:
                    # Below $5 minimum — can't sell, shares auto-resolve
                    print(f"  💰 Won {remaining_shares:.0f} shares — below $5 min, "
                          f"auto-resolving (${claim_notional:.2f})")
                    self.stats.bankroll += resolution_payout
                    self.stats.record_win(profit)
                    self._unclaimed_winnings += resolution_payout
                    partial_note = f" (partial exit saved ${self._exit_revenue:.2f})" if self._exit_revenue > 0 else ""
                    print(f"  ✅ WIN{partial_note} +${profit:.2f} (unclaimed) | "
                          f"P&L: ${self.stats.total_pnl:+.2f} | "
                          f"Bank: ${self.stats.bankroll:.2f}")
                    self.telegram.win_alert(profit, self.stats.total_pnl)
                    return

                print(f"  💰 Claiming: sell {remaining_shares:.0f} shares @ $0.99...")
                claim = self.executor.sell(
                    token_id=self._trade_token_id,
                    shares=remaining_shares,
                    price=0.99,
                )
                if claim.success:
                    self.stats.bankroll += claim.amount_usd
                    total_received = self._exit_revenue + claim.amount_usd
                    actual_profit = total_received - original_cost
                    self.stats.record_win(actual_profit)
                    partial_note = f" (partial exit saved ${self._exit_revenue:.2f})" if self._exit_revenue > 0 else ""
                    print(f"  ✅ WIN (claimed{partial_note}) +${actual_profit:.2f} | "
                          f"P&L: ${self.stats.total_pnl:+.2f} | "
                          f"Bank: ${self.stats.bankroll:.2f}")
                    self.telegram.win_alert(actual_profit, self.stats.total_pnl)
                    return
                else:
                    print(f"  ⚠️  Claim failed: {claim.error}")

            self.stats.bankroll += resolution_payout
            self.stats.record_win(profit)
            self._unclaimed_winnings += resolution_payout
            partial_note = f" (partial exit saved ${self._exit_revenue:.2f})" if self._exit_revenue > 0 else ""
            print(f"  ✅ WIN{partial_note} +${profit:.2f} (unclaimed) | "
                  f"P&L: ${self.stats.total_pnl:+.2f} | "
                  f"Bank: ${self.stats.bankroll:.2f}")
            self.telegram.win_alert(profit, self.stats.total_pnl)
        else:
            # Lost — remaining shares worth $0. Any partial exit revenue was already banked.
            net_loss = original_cost - self._exit_revenue
            self.stats.record_loss(net_loss)
            partial_note = f" (partial exit saved ${self._exit_revenue:.2f})" if self._exit_revenue > 0 else ""
            print(f"  ❌ LOSS{partial_note} -${net_loss:.2f} | "
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
        time.sleep(1)
        sys.exit(0)


if __name__ == "__main__":
    bot = PolyBot()
    bot.start()
