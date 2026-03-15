#!/usr/bin/env python3
"""
PolyBot v8 — Market Orders via Complement Engine

The breakthrough: Polymarket's order book has two layers.
  - Raw token book: $0.06 bid / $0.94 ask (the void we kept hitting)
  - Merged book: $0.50 bid / $0.51 ask (where all $10K/window flows)

The merged book works through complement matching — buying UP at $0.51
is the same as someone selling DOWN at $0.49. Market orders go through
this engine and fill instantly.

Flow per window:
  1. Wait for BTC to move (signal from Binance WebSocket)
  2. Preview market price via calculate_market_price()
  3. Check: does edge still exist at this price?
  4. Buy at market → instant fill through complement engine
  5. At T-5s: if profitable, sell at market → instant exit
  6. If held through close: resolve from BTC outcome
"""

import os
import sys
import time
import signal
from dotenv import load_dotenv

from market import get_current_market, current_window_ts, PERIOD_SECONDS
from price_feed import BinancePriceFeed
from strategy import evaluate, StrategyConfig, TradingStats
from executor import Executor, FILLED, FAILED
from telegram_notifier import TelegramNotifier


EXIT_WINDOW_START = 5
EXIT_WINDOW_END = 1


class PolyBot:
    def __init__(self):
        load_dotenv()

        self.dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
        self.period = int(os.getenv("MARKET_PERIOD", "5"))

        self.strategy_config = StrategyConfig(
            min_edge=float(os.getenv("MIN_EDGE", "0.03")),
            entry_window_start=int(os.getenv("ENTRY_WINDOW_START", "60")),
            entry_window_end=int(os.getenv("ENTRY_WINDOW_END", "10")),
            kelly_fraction=float(os.getenv("KELLY_FRACTION", "0.25")),
            min_bet=float(os.getenv("MIN_BET", "1.0")),
            max_bet=float(os.getenv("MAX_BET", "25.0")),
        )

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

        # ── Trade state ─────────────────────────────────────────────
        self._traded: bool = False
        self._trade_attempted: bool = False
        self._trade_side: str = ""
        self._trade_price: float = 0.0
        self._trade_cost: float = 0.0
        self._trade_shares: float = 0.0
        self._trade_token_id: str = ""

        # ── Exit state ──────────────────────────────────────────────
        self._exited: bool = False
        self._exit_attempted: bool = False
        self._exit_revenue: float = 0.0

        # ── Unclaimed ───────────────────────────────────────────────
        self._unclaimed_winnings: float = 0.0

    def start(self):
        if not self.dry_run:
            from proxy import ensure_tor, apply_proxy
            import logging
            logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")
            print("\n🧅 Starting Tor proxy for CLOB API...")
            proxy_url = ensure_tor()
            apply_proxy(proxy_url)
            print(f"✅ Tor active: {proxy_url}\n")

        kf = self.strategy_config.kelly_fraction
        print("=" * 55)
        print(f"  PolyBot v8 — Market Orders + Complement Engine")
        print(f"  Mode: {'DRY RUN' if self.dry_run else '🔴 LIVE TRADING'}")
        print(f"  Kelly: {kf*100:.0f}% fraction | "
              f"Bets: ${self.strategy_config.min_bet:.0f}–${self.strategy_config.max_bet:.0f}")
        print(f"  Min edge: {self.strategy_config.min_edge*100:.1f}%")
        print(f"  Entry: T-{self.strategy_config.entry_window_start}s to "
              f"T-{self.strategy_config.entry_window_end}s")
        print(f"  Exit:  T-{EXIT_WINDOW_START}s to T-{EXIT_WINDOW_END}s")
        print(f"  Bankroll: ${self.stats.bankroll:.2f}")
        print("=" * 55)

        if not self.dry_run:
            if not self.executor.initialize():
                print("\n❌ Failed to initialize. Check credentials.")
                return
            balance = self.executor.get_balance()
            print(f"  USDC balance: ${balance:.2f}")
            self.stats.bankroll = balance
        else:
            print("  [dry run — no wallet connection]")

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

    # ── Main tick ───────────────────────────────────────────────────

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

        # ── Holding: watch for exit ─────────────────────────────────
        if self._traded and not self._exited:
            if EXIT_WINDOW_START >= seconds_remaining >= EXIT_WINDOW_END:
                if not self._exit_attempted:
                    self._attempt_exit(seconds_remaining)
            return

        # ── Already done for this window ────────────────────────────
        if self._traded or self._trade_attempted:
            return

        # ── Look for entry ──────────────────────────────────────────
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

        # Status line
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

    # ── Window management ───────────────────────────────────────────

    def _on_new_window(self, window_ts: int):
        if self._current_window > 0:
            self.stats.hourly.record_window(self._traded)
            if self._traded:
                self._resolve_previous_trade()

        self._current_window = window_ts
        self._opening_price = 0.0
        self._traded = False
        self._trade_attempted = False
        self._exited = False
        self._exit_attempted = False
        self._exit_revenue = 0.0

        t = time.strftime("%H:%M:%S", time.localtime(window_ts))
        print(f"\n{'─' * 55}")
        print(f"🕐 {t} | Trades: {self.stats.total_trades} | "
              f"W/L: {self.stats.wins}/{self.stats.losses} | "
              f"P&L: ${self.stats.total_pnl:+.2f}")
        print(f"{'─' * 55}")

    # ── Market prices (merged book via complement engine) ───────────

    def _get_market_prices(self, btc_price: float, seconds_remaining: float) -> tuple:
        if self.dry_run or not self.executor._initialized:
            if self._opening_price <= 0:
                return 0.50, 0.50
            delta_pct = (btc_price - self._opening_price) / self._opening_price
            time_factor = 1 - (seconds_remaining / PERIOD_SECONDS[self.period])
            import math
            lag_factor = min(time_factor * 0.7, 0.85)
            implied = 0.5 + lag_factor * math.tanh(delta_pct * 500) * 0.45
            up = round(min(max(implied, 0.02), 0.98), 3)
            return up, round(1.0 - up, 3)

        try:
            market = get_current_market(self.period)
            if not market:
                return 0.50, 0.50

            # Query the MERGED book (complement engine) — same prices the UI shows
            probe_amount = 5.0  # Small probe to get current price
            up_price = self.executor.get_market_price(market.token_id_up, "BUY", probe_amount)
            down_price = self.executor.get_market_price(market.token_id_down, "BUY", probe_amount)

            # Fallback if price check fails
            if up_price <= 0 and down_price <= 0:
                return 0.50, 0.50
            if up_price <= 0:
                up_price = round(1.0 - down_price, 3)
            if down_price <= 0:
                down_price = round(1.0 - up_price, 3)

            return up_price, down_price

        except Exception as e:
            print(f"[price] Error: {e}")
            return 0.50, 0.50

    # ── Entry: buy at market ────────────────────────────────────────

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

        # ── Preview: what would we actually pay? ────────────────────
        if not self.dry_run and self.executor._initialized:
            actual_price = self.executor.get_market_price(token_id, "BUY", trade_amount)
            if actual_price > 0:
                actual_edge = sig.true_prob - actual_price
                print(f"  📊 Actual market price: ${actual_price:.3f} "
                      f"(edge at this price: {actual_edge:.3f})")

                if actual_edge < self.strategy_config.min_edge:
                    print(f"  ⚠️  Edge gone at market price — skipping")
                    return

        # ── Buy at market ───────────────────────────────────────────
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

            mode = "PAPER" if self.dry_run else "LIVE"
            print(f"  ✅ {mode}: {result.shares:.0f} shares @ "
                  f"${result.price:.3f} = ${result.amount_usd:.2f}")
            print(f"     Exit window: T-{EXIT_WINDOW_START}s to T-{EXIT_WINDOW_END}s")

            self.telegram.trade_alert(
                side=sig.side, price=result.price, amount=result.amount_usd,
                market_slug=slug, dry_run=self.dry_run,
                edge=sig.edge, kelly_size=sig.kelly_size,
            )
        else:
            print(f"  ❌ Buy failed: {result.error}")

    # ── Exit: sell at market before close ───────────────────────────

    def _attempt_exit(self, seconds_remaining: float):
        self._exit_attempted = True

        if self.dry_run:
            btc_price, _ = self.price_feed.get_price()
            if self._opening_price <= 0:
                return
            delta_pct = (btc_price - self._opening_price) / self._opening_price
            on_our_side = (delta_pct > 0 and self._trade_side == "UP") or \
                          (delta_pct < 0 and self._trade_side == "DOWN")
            if on_our_side and abs(delta_pct) > 0.0001:
                import math
                sim_price = min(0.50 + abs(delta_pct) * 5000 * 0.45, 0.97)
                sim_price = round(sim_price, 2)
                if sim_price > self._trade_price:
                    revenue = self._trade_shares * sim_price
                    self._exited = True
                    self._exit_revenue = revenue
                    self.stats.bankroll += revenue
                    profit = revenue - self._trade_cost
                    print(f"  💰 EXIT (paper): {self._trade_shares:.0f} shares @ "
                          f"${sim_price:.3f} | Profit: ${profit:.2f}")
            return

        # ── Live: preview sell price ────────────────────────────────
        sell_notional = round(self._trade_shares * self._trade_price, 2)
        sell_price = self.executor.get_market_price(
            self._trade_token_id, "SELL", sell_notional
        )

        if sell_price <= 0 or sell_price <= self._trade_price:
            print(f"  ⏳ Sell price ${sell_price:.3f} not profitable "
                  f"(bought at ${self._trade_price:.3f}) — holding")
            return

        print(f"  💰 Exiting: sell @ ${sell_price:.3f} > buy ${self._trade_price:.3f} | "
              f"T-{seconds_remaining:.0f}s")

        result = self.executor.sell(
            token_id=self._trade_token_id,
            amount_usd=sell_notional,
        )

        if result.success:
            self._exited = True
            self._exit_revenue = result.amount_usd
            self.stats.bankroll += result.amount_usd
            profit = result.amount_usd - self._trade_cost
            print(f"  💰 SOLD: {result.shares:.0f} shares @ ${result.price:.3f} "
                  f"= ${result.amount_usd:.2f} | Profit: ${profit:+.2f}")
        else:
            print(f"  ⚠️  Exit failed: {result.error} — holding through resolution")

    # ── Resolve ─────────────────────────────────────────────────────

    def _resolve_previous_trade(self):
        if self._exited:
            profit = self._exit_revenue - self._trade_cost
            self.stats.record_win(profit)
            print(f"  ✅ WIN (pre-close) +${profit:.2f} | "
                  f"P&L: ${self.stats.total_pnl:+.2f} | "
                  f"Bank: ${self.stats.bankroll:.2f}")
            self.telegram.win_alert(profit, self.stats.total_pnl)
            return

        # Held through resolution
        btc_price, _ = self.price_feed.get_price()
        if self._opening_price <= 0 or btc_price <= 0:
            return

        won = (btc_price >= self._opening_price) == (self._trade_side == "UP")
        cost = self._trade_cost
        shares = self._trade_shares

        if won:
            payout = shares * 1.0
            profit = payout - cost

            # Try to sell at $0.99 to claim
            if self._trade_token_id and not self._trade_token_id.startswith("DRY-"):
                print(f"  💰 Claiming: sell {shares:.0f} shares @ market...")
                claim_notional = round(shares * 0.99, 2)
                claim = self.executor.sell(
                    token_id=self._trade_token_id,
                    amount_usd=claim_notional,
                )
                if claim.success:
                    self.stats.bankroll += claim.amount_usd
                    actual_profit = claim.amount_usd - cost
                    self.stats.record_win(actual_profit)
                    print(f"  ✅ WIN (claimed) +${actual_profit:.2f} | "
                          f"P&L: ${self.stats.total_pnl:+.2f} | "
                          f"Bank: ${self.stats.bankroll:.2f}")
                    self.telegram.win_alert(actual_profit, self.stats.total_pnl)
                    return
                else:
                    print(f"  ⚠️  Claim failed: {claim.error}")

            # Fallback: count theoretical payout
            self.stats.bankroll += payout
            self.stats.record_win(profit)
            self._unclaimed_winnings += payout
            print(f"  ✅ WIN +${profit:.2f} (unclaimed) | "
                  f"P&L: ${self.stats.total_pnl:+.2f} | "
                  f"Bank: ${self.stats.bankroll:.2f}")
            self.telegram.win_alert(profit, self.stats.total_pnl)
        else:
            self.stats.record_loss(cost)
            print(f"  ❌ LOSS -${cost:.2f} | "
                  f"P&L: ${self.stats.total_pnl:+.2f} | "
                  f"Bank: ${self.stats.bankroll:.2f}")
            self.telegram.loss_alert(cost, self.stats.total_pnl)

    # ── Hourly summary ──────────────────────────────────────────────

    def _check_hourly_summary(self):
        current_hour = int(time.time() // 3600)
        if current_hour != self._last_hour_check:
            self._last_hour_check = current_hour
            h = self.stats.hourly.to_dict()
            o = self.stats.to_dict()
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
            if self._unclaimed_winnings > 0:
                print(f"  💰 Unclaimed: ${self._unclaimed_winnings:.2f}")
            print(f"{'═' * 55}\n")
            self.telegram.hourly_summary(h, o)
            self.stats.hourly.reset()

    # ── Shutdown ────────────────────────────────────────────────────

    def _handle_shutdown(self, signum, frame):
        print(f"\n\n🛑 Shutting down...")
        self._running = False
        self.price_feed.stop()
        if self.executor._initialized:
            self.executor.cancel_all()

        o = self.stats.to_dict()
        print(f"\n{'═' * 55}")
        print(f"  FINAL: {o['total_trades']} trades | "
              f"{o['wins']}W/{o['losses']}L | "
              f"WR: {o['win_rate']:.1f}%")
        print(f"  P&L: ${o['pnl']:+.2f} | Bank: ${o['bankroll']:.2f}")
        if self._unclaimed_winnings > 0:
            print(f"  💰 Unclaimed: ${self._unclaimed_winnings:.2f}")
        print(f"{'═' * 55}")

        self.telegram.status_update(o)
        time.sleep(1)
        sys.exit(0)


if __name__ == "__main__":
    bot = PolyBot()
    bot.start()
