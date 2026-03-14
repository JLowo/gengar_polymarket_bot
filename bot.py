#!/usr/bin/env python3
"""
PolyBot v3 — Oracle Lag Scalper with Kelly Criterion

All four fixes applied:
  1. Minimum 5 shares enforced (handled in executor)
  2. P&L calculated from trade math, not balance comparison
     (buying shares ≠ losing money — cost basis tracking)
  3. FOK orders — instant fill or cancel, no 30s polling
  4. Unclaimed winnings tracked — periodic claim reminders
"""

import os
import sys
import time
import signal
from dotenv import load_dotenv

from market import get_current_market, current_window_ts, PERIOD_SECONDS
from price_feed import BinancePriceFeed
from strategy import evaluate, StrategyConfig, TradingStats
from executor import Executor, FILLED, CANCELLED, FAILED
from telegram_notifier import TelegramNotifier


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
        self._window_traded: bool = False
        self._opening_price: float = 0.0
        self._last_hour_check: int = 0

        # ── Trade tracking ──────────────────────────────────────────
        self._last_trade_side: str = ""
        self._last_trade_price: float = 0.0     # Price per share
        self._last_trade_cost: float = 0.0       # Total USD spent
        self._last_trade_shares: float = 0.0     # Shares filled
        self._last_order_filled: bool = False

        # ── Unclaimed winnings tracker ──────────────────────────────
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
        print(f"  PolyBot v3 — Oracle Lag Scalper + Kelly")
        print(f"  Mode: {'DRY RUN' if self.dry_run else '🔴 LIVE TRADING'}")
        print(f"  Kelly: {kf*100:.0f}% fraction | "
              f"Bets: ${self.strategy_config.min_bet:.0f}–${self.strategy_config.max_bet:.0f}")
        print(f"  Min edge: {self.strategy_config.min_edge*100:.1f}%")
        print(f"  Entry: T-{self.strategy_config.entry_window_start}s to "
              f"T-{self.strategy_config.entry_window_end}s")
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

        if self._window_traded:
            return

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
            print(
                f"  ⏱  T-{seconds_remaining:5.1f}s | "
                f"BTC ${btc_price:,.2f} {d}{abs(delta):.3f}% | "
                f"UP ${up_price:.3f} DN ${down_price:.3f} | "
                f"P&L ${self.stats.total_pnl:+.2f}"
            )

    def _on_new_window(self, window_ts: int):
        if self._current_window > 0:
            self.stats.hourly.record_window(self._window_traded)
            if self._window_traded:
                self._resolve_previous_trade()

        self._current_window = window_ts
        self._window_traded = False
        self._opening_price = 0.0
        self._last_order_filled = False

        t = time.strftime("%H:%M:%S", time.localtime(window_ts))
        print(f"\n{'─' * 55}")
        print(f"🕐 {t} | Trades: {self.stats.total_trades} | "
              f"W/L: {self.stats.wins}/{self.stats.losses} | "
              f"P&L: ${self.stats.total_pnl:+.2f}")
        print(f"{'─' * 55}")

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
            return market.up_price, market.down_price
        except Exception:
            return 0.50, 0.50

    def _execute_trade(self, sig, seconds_remaining: float):
        market = get_current_market(self.period) if not self.dry_run else None
        token_id = ""
        if market:
            token_id = market.token_id_up if sig.side == "UP" else market.token_id_down
        else:
            token_id = f"DRY-{sig.side}-{self._current_window}"

        slug = f"btc-updown-{self.period}m-{self._current_window}"
        trade_amount = sig.kelly_size

        print(f"\n  🎯 {sig.side} | edge={sig.edge:.3f} | "
              f"prob={sig.true_prob:.2f} | BTC Δ={sig.btc_delta_pct:+.3f}%")
        print(f"     Kelly: ${trade_amount:.2f} | mkt ${sig.market_price:.3f} | T-{seconds_remaining:.0f}s")

        # ── Place FOK order (instant fill or cancel) ────────────────
        result = self.executor.place_order(
            token_id=token_id,
            side="BUY",
            price=sig.market_price,
            amount_usd=trade_amount,
        )

        if result.success:
            # ── FOK filled — record the REAL fill data ──────────────
            self._window_traded = True
            self._last_order_filled = True
            self._last_trade_side = sig.side
            self._last_trade_price = result.price
            self._last_trade_cost = result.amount_spent
            self._last_trade_shares = result.size_filled

            # Deduct cost from bankroll (money is now in shares)
            self.stats.bankroll -= result.amount_spent
            self.stats.hourly.record_trade(sig.edge, sig.btc_delta_pct)

            mode = "PAPER" if self.dry_run else "LIVE"
            print(f"  ✅ {mode}: {result.size_filled:.2f} shares @ "
                  f"${result.price:.3f} = ${result.amount_spent:.2f}")

            self.telegram.trade_alert(
                side=sig.side,
                price=result.price,
                amount=result.amount_spent,
                market_slug=slug,
                dry_run=self.dry_run,
                edge=sig.edge,
                kelly_size=sig.kelly_size,
            )
        else:
            # ── FOK didn't fill or was rejected ─────────────────────
            print(f"  ⚠️  {result.status}: {result.error}")
            if result.status == FAILED:
                self.telegram.error_alert(f"Order failed: {result.error}")

    def _resolve_previous_trade(self):
        """Resolve the previous window's trade.

        P&L is calculated from the trade itself:
          - WIN:  payout ($1/share) minus cost = net profit
          - LOSS: cost is gone, shares worth $0 = net loss

        This works for both dry run and live because FOK guarantees
        we only count trades that actually filled.
        """
        if not self._last_order_filled:
            return

        btc_price, _ = self.price_feed.get_price()
        if self._opening_price <= 0 or btc_price <= 0:
            return

        won = (btc_price >= self._opening_price) == (self._last_trade_side == "UP")
        cost = self._last_trade_cost
        shares = self._last_trade_shares

        if won:
            # Shares pay $1 each. Profit = payout - cost.
            payout = shares * 1.0
            profit = payout - cost

            # Add payout back to bankroll (cost was already deducted on fill)
            self.stats.bankroll += payout
            self.stats.record_win(profit)

            # Track unclaimed winnings (live only — must claim manually)
            if not self.dry_run:
                self._unclaimed_winnings += payout

            print(f"  ✅ WIN +${profit:.2f} | "
                  f"Cost ${cost:.2f} → Payout ${payout:.2f} | "
                  f"P&L: ${self.stats.total_pnl:+.2f} | "
                  f"Bank: ${self.stats.bankroll:.2f}")
            self.telegram.win_alert(profit, self.stats.total_pnl)
        else:
            # Shares worth $0. We lose what we spent.
            self.stats.record_loss(cost)

            # Bankroll already had cost deducted on fill — no further change

            print(f"  ❌ LOSS -${cost:.2f} | "
                  f"P&L: ${self.stats.total_pnl:+.2f} | "
                  f"Bank: ${self.stats.bankroll:.2f}")
            self.telegram.loss_alert(cost, self.stats.total_pnl)

    def _check_hourly_summary(self):
        """Send hourly summary with claim reminder."""
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
                print(f"  Avg edge: {h['avg_edge']*100:.1f}% | "
                      f"Avg delta: {h['avg_delta']:.3f}%")
                print(f"  Best: ${h['best_trade']:+.2f} | "
                      f"Worst: ${h['worst_trade']:+.2f}")
            print(f"  Windows: {h['windows_seen']} seen, "
                  f"{h['windows_skipped']} skipped")
            print(f"  Overall: {o['total_trades']} trades | "
                  f"P&L: ${o['pnl']:+.2f} | "
                  f"Bank: ${o['bankroll']:.2f}")

            # ── Claim reminder ──────────────────────────────────────
            if self._unclaimed_winnings > 0:
                print(f"  💰 Unclaimed winnings: ${self._unclaimed_winnings:.2f} "
                      f"— visit Polymarket to claim!")

            print(f"{'═' * 55}\n")

            self.telegram.hourly_summary(h, o)
            self.stats.hourly.reset()

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
            print(f"  💰 Unclaimed: ${self._unclaimed_winnings:.2f} "
                  f"— claim at polymarket.com/portfolio")
        print(f"{'═' * 55}")

        self.telegram.status_update(o)
        time.sleep(1)
        sys.exit(0)


if __name__ == "__main__":
    bot = PolyBot()
    bot.start()
