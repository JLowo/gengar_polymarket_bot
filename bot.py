#!/usr/bin/env python3
"""
PolyBot v3 — Oracle Lag Scalper with Kelly Criterion

Fixed version: trades are only counted when orders are actually FILLED
on the Polymarket CLOB. P&L is derived from real USDC balance changes,
not local guesswork.
"""

import os
import sys
import time
import signal
from dotenv import load_dotenv

from market import get_current_market, current_window_ts, PERIOD_SECONDS
from price_feed import BinancePriceFeed
from strategy import evaluate, StrategyConfig, TradingStats
from executor import Executor, PLACED, FILLED, PARTIAL, CANCELLED, FAILED
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

        # ── Order tracking (the fix) ───────────────────────────────
        self._last_order_id: str = ""
        self._last_trade_side: str = ""
        self._last_trade_price: float = 0.0
        self._last_trade_amount: float = 0.0
        self._last_order_filled: bool = False
        self._last_fill_size: float = 0.0
        self._balance_before_trade: float = 0.0

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
        self._last_order_id = ""
        self._last_order_filled = False
        self._last_fill_size = 0.0

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

        # Snapshot balance BEFORE placing the order
        if not self.dry_run:
            self._balance_before_trade = self.executor.get_balance()

        # ── Place the order ─────────────────────────────────────────
        result = self.executor.place_order(
            token_id=token_id,
            side="BUY",
            price=sig.market_price,
            amount_usd=trade_amount,
        )

        if not result.success:
            print(f"  ❌ Order rejected: {result.error}")
            self.telegram.error_alert(f"Order rejected: {result.error}")
            return

        print(f"  📤 Order placed: {result.order_id[:16]}... "
              f"({result.size_requested:.2f} shares @ ${result.price:.3f})")

        # ── Wait for fill confirmation ──────────────────────────────
        if self.dry_run:
            fill_result = result  # Dry run: instant fill
        else:
            # Poll for up to 30 seconds — these markets move fast
            print(f"  ⏳ Waiting for fill...")
            fill_result = self.executor.wait_for_fill(
                order_id=result.order_id,
                timeout=30.0,
                poll_interval=2.0,
            )

        # ── Record what actually happened ───────────────────────────
        self._last_order_id = result.order_id
        self._last_trade_side = sig.side
        self._last_trade_price = sig.market_price
        self._last_trade_amount = trade_amount
        self._last_order_filled = fill_result.filled
        self._last_fill_size = fill_result.size_filled

        if fill_result.filled:
            self._window_traded = True
            self.stats.hourly.record_trade(sig.edge, sig.btc_delta_pct)

            mode = "PAPER" if self.dry_run else "LIVE"
            filled_amt = fill_result.size_filled * fill_result.price
            print(f"  ✅ {mode}: {fill_result.size_filled:.2f} shares @ "
                  f"${fill_result.price:.3f} = ${filled_amt:.2f}")

            self.telegram.trade_alert(
                side=sig.side,
                price=sig.market_price,
                amount=filled_amt,
                market_slug=slug,
                dry_run=self.dry_run,
                edge=sig.edge,
                kelly_size=sig.kelly_size,
            )
        else:
            print(f"  ⚠️  Order NOT filled (status: {fill_result.status})")
            if fill_result.error:
                print(f"     Reason: {fill_result.error}")
            self.telegram.error_alert(
                f"Order not filled: {fill_result.status} — {fill_result.error}"
            )

    def _resolve_previous_trade(self):
        """Resolve the previous window's trade using REAL data.

        For dry run: use BTC price logic (simulation).
        For live: check actual USDC balance change on Polymarket.
        """
        # ── If the order never filled, there's nothing to resolve ───
        if not self._last_order_filled:
            # Cancel any lingering order just in case
            if self._last_order_id and not self._last_order_id.startswith("DRY-"):
                self.executor.cancel_order(self._last_order_id)
            print(f"  ⏭️  Skipped — order was not filled")
            return

        # ── Dry run: simulate outcome from BTC price ────────────────
        if self.dry_run:
            btc_price, _ = self.price_feed.get_price()
            if self._opening_price <= 0 or btc_price <= 0:
                return

            won = (btc_price >= self._opening_price) == (self._last_trade_side == "UP")
            amount = self._last_trade_amount
            price = self._last_trade_price

            if won:
                profit = amount * ((1.0 / price) - 1)
                self.stats.record_win(profit)
                print(f"  ✅ WIN +${profit:.2f} | P&L: ${self.stats.total_pnl:+.2f} | "
                      f"Bank: ${self.stats.bankroll:.2f}")
                self.telegram.win_alert(profit, self.stats.total_pnl)
            else:
                self.stats.record_loss(amount)
                print(f"  ❌ LOSS -${amount:.2f} | P&L: ${self.stats.total_pnl:+.2f} | "
                      f"Bank: ${self.stats.bankroll:.2f}")
                self.telegram.loss_alert(amount, self.stats.total_pnl)
            return

        # ── Live: derive P&L from actual USDC balance change ────────
        #
        # This is the only source of truth. The market resolves on-chain,
        # and our balance reflects the outcome. No guessing.
        #
        balance_now = self.executor.get_balance()
        balance_change = balance_now - self._balance_before_trade

        if balance_change > 0:
            # We made money — balance went up
            profit = balance_change
            self.stats.record_win(profit)
            self.stats.bankroll = balance_now
            print(f"  ✅ WIN +${profit:.2f} | P&L: ${self.stats.total_pnl:+.2f} | "
                  f"Bank: ${balance_now:.2f} (verified)")
            self.telegram.win_alert(profit, self.stats.total_pnl)

        elif balance_change < 0:
            # We lost money — balance went down
            loss = abs(balance_change)
            self.stats.record_loss(loss)
            self.stats.bankroll = balance_now
            print(f"  ❌ LOSS -${loss:.2f} | P&L: ${self.stats.total_pnl:+.2f} | "
                  f"Bank: ${balance_now:.2f} (verified)")
            self.telegram.loss_alert(loss, self.stats.total_pnl)

        else:
            # No balance change — could be pending settlement
            # Sync balance anyway to stay honest
            self.stats.bankroll = balance_now
            print(f"  🔄 No balance change yet — may be settling | "
                  f"Bank: ${balance_now:.2f} (verified)")

    def _check_hourly_summary(self):
        """Send hourly summary and reset hourly stats."""
        current_hour = int(time.time() // 3600)
        if current_hour != self._last_hour_check:
            self._last_hour_check = current_hour

            h = self.stats.hourly.to_dict()
            o = self.stats.to_dict()

            # Sync real balance into stats if live
            if not self.dry_run and self.executor._initialized:
                real_balance = self.executor.get_balance()
                self.stats.bankroll = real_balance
                o["bankroll"] = real_balance

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
            print(f"{'═' * 55}\n")

            self.telegram.hourly_summary(h, o)
            self.stats.hourly.reset()

    def _handle_shutdown(self, signum, frame):
        print(f"\n\n🛑 Shutting down...")
        self._running = False
        self.price_feed.stop()
        if self.executor._initialized:
            self.executor.cancel_all()

        # Final balance sync
        if not self.dry_run and self.executor._initialized:
            real_balance = self.executor.get_balance()
            self.stats.bankroll = real_balance

        o = self.stats.to_dict()
        print(f"\n{'═' * 55}")
        print(f"  FINAL: {o['total_trades']} trades | "
              f"{o['wins']}W/{o['losses']}L | "
              f"WR: {o['win_rate']:.1f}%")
        print(f"  P&L: ${o['pnl']:+.2f} | Bank: ${o['bankroll']:.2f}")
        print(f"{'═' * 55}")

        self.telegram.status_update(o)
        time.sleep(1)
        sys.exit(0)


if __name__ == "__main__":
    bot = PolyBot()
    bot.start()
