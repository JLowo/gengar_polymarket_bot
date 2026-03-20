"""Quant-grade performance tracker for PolyBot.

Three log files, all append-only CSV:

1. signals.csv — Every signal the strategy evaluates (traded or not).
   Answers: Are our edges real? What are we missing?

2. trades.csv — Full lifecycle of every trade (entry → hold → exit → resolve).
   Answers: How well do we execute? Where does P&L leak?

3. executions.csv — Every API call with timing.
   Answers: How fast are we? Where's the latency?

Usage:
    tracker = Tracker(log_dir="logs")
    tracker.log_signal(...)       # Every evaluate() result
    tracker.log_trade_entry(...)  # On buy fill
    tracker.log_trade_exit(...)   # On sell/stop/TP
    tracker.log_trade_resolve(...)# At window close
    tracker.log_execution(...)    # Every API call
    tracker.session_summary()     # On shutdown
"""

import os
import csv
import time
from dataclasses import dataclass, field, asdict
from typing import Optional


# ── Signal record ───────────────────────────────────────────────────

SIGNAL_FIELDS = [
    "timestamp", "window_ts", "window_time",
    # Market state
    "btc_price", "opening_price", "btc_delta_pct",
    "up_price", "down_price", "seconds_remaining",
    # Signal output
    "side", "true_prob", "market_price", "edge",
    "kelly_size",
    # What happened
    "action",           # "traded", "skipped_edge_gone", "skipped_below_min",
                        # "skipped_price_cap", "skipped_no_signal", etc.
    "actual_price",     # Real market price after preview (0 if not checked)
    "actual_edge",      # Edge at actual price
    "fill_price",       # What we actually paid (0 if not traded)
    "slippage",         # fill_price - market_price (0 if not traded)
]

# ── Trade record ────────────────────────────────────────────────────

TRADE_FIELDS = [
    "timestamp", "window_ts", "window_time", "trade_id",
    # Entry
    "side", "entry_price", "entry_shares", "entry_cost",
    "edge_at_entry", "prob_at_entry", "btc_delta_at_entry",
    "seconds_remaining_at_entry",
    "entry_latency_ms",         # signal → fill confirmed
    "planned_price", "slippage",  # strategy cache price vs execution price
    # Hold
    "max_prob_during_hold",     # Peak probability while holding
    "min_prob_during_hold",     # Trough probability
    "max_sell_price_seen",      # Best exit we saw
    "min_sell_price_seen",      # Worst exit we saw
    # Exit
    "exit_type",                # "take-profit", "prob-stop", "price-stop",
                                # "forced-exit", "resolution", "hold-to-resolution"
    "exit_price", "exit_shares_sold", "exit_revenue",
    "residual_shares", "residual_value",
    "exit_latency_ms",
    # Resolution
    "btc_final_price", "btc_final_delta_pct",
    "won_resolution",           # Did BTC go our way?
    "resolution_payout",        # What resolution would have paid
    # P&L
    "profit", "return_pct",
    "profit_if_held",           # What we'd have made holding to resolution
]

# ── Execution record ────────────────────────────────────────────────

EXECUTION_FIELDS = [
    "timestamp", "window_ts",
    "action",                   # "buy", "sell", "get_price", "get_balance",
                                # "check_order", "cancel"
    "latency_ms",
    "success",
    "error",
    "details",                  # JSON-safe string with relevant params
]


class Tracker:
    def __init__(self, log_dir: str = "logs"):
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)

        self._signal_path = os.path.join(log_dir, "signals.csv")
        self._trade_path = os.path.join(log_dir, "trades.csv")
        self._exec_path = os.path.join(log_dir, "executions.csv")

        self._ensure_headers(self._signal_path, SIGNAL_FIELDS)
        self._ensure_headers(self._trade_path, TRADE_FIELDS)
        self._ensure_headers(self._exec_path, EXECUTION_FIELDS)

        # In-memory state for current trade
        self._current_trade: dict = {}
        self._trade_counter: int = 0

        # Session stats
        self._session_start = time.time()
        self._session_start_balance: float = 0.0
        self._signals_total: int = 0
        self._signals_traded: int = 0
        self._signals_skipped_edge: int = 0
        self._signals_skipped_min: int = 0
        self._signals_skipped_cap: int = 0
        self._total_slippage: float = 0.0
        self._slippage_count: int = 0
        self._total_latency_ms: float = 0.0
        self._latency_count: int = 0

    def set_session_balance(self, balance: float):
        self._session_start_balance = balance

    # ── Signal logging ──────────────────────────────────────────────

    def log_signal(
        self,
        window_ts: int,
        btc_price: float,
        opening_price: float,
        up_price: float,
        down_price: float,
        seconds_remaining: float,
        # Signal (None if no signal)
        side: str = "",
        true_prob: float = 0.0,
        market_price: float = 0.0,
        edge: float = 0.0,
        kelly_size: float = 0.0,
        # Outcome
        action: str = "no_signal",
        actual_price: float = 0.0,
        actual_edge: float = 0.0,
        fill_price: float = 0.0,
    ):
        self._signals_total += 1
        if action == "traded":
            self._signals_traded += 1
        elif "edge" in action:
            self._signals_skipped_edge += 1
        elif "min" in action:
            self._signals_skipped_min += 1
        elif "cap" in action:
            self._signals_skipped_cap += 1

        slippage = fill_price - market_price if fill_price > 0 and market_price > 0 else 0.0
        if fill_price > 0:
            self._total_slippage += slippage
            self._slippage_count += 1

        btc_delta_pct = ((btc_price - opening_price) / opening_price * 100) if opening_price > 0 else 0

        row = {
            "timestamp": time.time(),
            "window_ts": window_ts,
            "window_time": time.strftime("%H:%M", time.localtime(window_ts)),
            "btc_price": round(btc_price, 2),
            "opening_price": round(opening_price, 2),
            "btc_delta_pct": round(btc_delta_pct, 4),
            "up_price": round(up_price, 3),
            "down_price": round(down_price, 3),
            "seconds_remaining": round(seconds_remaining, 1),
            "side": side,
            "true_prob": round(true_prob, 4),
            "market_price": round(market_price, 4),
            "edge": round(edge, 4),
            "kelly_size": round(kelly_size, 2),
            "action": action,
            "actual_price": round(actual_price, 4),
            "actual_edge": round(actual_edge, 4),
            "fill_price": round(fill_price, 4),
            "slippage": round(slippage, 4),
        }
        self._append_row(self._signal_path, row, SIGNAL_FIELDS)

    # ── Trade lifecycle ─────────────────────────────────────────────

    def log_trade_entry(
        self,
        window_ts: int,
        side: str,
        entry_price: float,
        entry_shares: float,
        entry_cost: float,
        edge: float,
        prob: float,
        btc_delta: float,
        seconds_remaining: float,
        latency_ms: float = 0.0,
        planned_price: float = 0.0,
    ):
        self._trade_counter += 1
        slippage = round(entry_price - planned_price, 4) if planned_price > 0 else 0.0
        self._current_trade = {
            "timestamp": time.time(),
            "window_ts": window_ts,
            "window_time": time.strftime("%H:%M", time.localtime(window_ts)),
            "trade_id": self._trade_counter,
            "side": side,
            "entry_price": round(entry_price, 4),
            "planned_price": round(planned_price, 4),
            "slippage": slippage,
            "entry_shares": round(entry_shares, 1),
            "entry_cost": round(entry_cost, 2),
            "edge_at_entry": round(edge, 4),
            "prob_at_entry": round(prob, 4),
            "btc_delta_at_entry": round(btc_delta, 4),
            "seconds_remaining_at_entry": round(seconds_remaining, 1),
            "entry_latency_ms": round(latency_ms, 0),
            # Hold tracking — updated live
            "max_prob_during_hold": round(prob, 4),
            "min_prob_during_hold": round(prob, 4),
            "max_sell_price_seen": 0.0,
            "min_sell_price_seen": 999.0,
        }

    def update_hold_stats(self, prob: float, sell_price: float):
        """Call on each position check tick to track hold-period extremes."""
        if not self._current_trade:
            return
        if prob > self._current_trade["max_prob_during_hold"]:
            self._current_trade["max_prob_during_hold"] = round(prob, 4)
        if prob < self._current_trade["min_prob_during_hold"]:
            self._current_trade["min_prob_during_hold"] = round(prob, 4)
        if sell_price > 0:
            if sell_price > self._current_trade["max_sell_price_seen"]:
                self._current_trade["max_sell_price_seen"] = round(sell_price, 4)
            if sell_price < self._current_trade["min_sell_price_seen"]:
                self._current_trade["min_sell_price_seen"] = round(sell_price, 4)

    def log_trade_exit(
        self,
        exit_type: str,
        exit_price: float,
        exit_shares_sold: float,
        exit_revenue: float,
        residual_shares: float,
        latency_ms: float = 0.0,
    ):
        if not self._current_trade:
            return
        self._current_trade["exit_type"] = exit_type
        self._current_trade["exit_price"] = round(exit_price, 4)
        self._current_trade["exit_shares_sold"] = round(exit_shares_sold, 1)
        self._current_trade["exit_revenue"] = round(exit_revenue, 2)
        self._current_trade["residual_shares"] = round(residual_shares, 1)
        self._current_trade["residual_value"] = round(residual_shares * exit_price, 2)
        self._current_trade["exit_latency_ms"] = round(latency_ms, 0)

    def log_trade_resolve(
        self,
        btc_final_price: float,
        opening_price: float,
        won: bool,
        profit: float,
        exit_revenue: float = 0.0,
    ):
        if not self._current_trade:
            return

        entry_cost = self._current_trade.get("entry_cost", 0)
        entry_shares = self._current_trade.get("entry_shares", 0)
        entry_price = self._current_trade.get("entry_price", 0)
        side = self._current_trade.get("side", "")

        btc_delta = ((btc_final_price - opening_price) / opening_price * 100) if opening_price > 0 else 0
        resolution_payout = entry_shares * 1.0 if won else 0.0
        profit_if_held = resolution_payout - entry_cost

        self._current_trade["btc_final_price"] = round(btc_final_price, 2)
        self._current_trade["btc_final_delta_pct"] = round(btc_delta, 4)
        self._current_trade["won_resolution"] = won
        self._current_trade["resolution_payout"] = round(resolution_payout, 2)
        self._current_trade["profit"] = round(profit, 2)
        self._current_trade["return_pct"] = round(
            (profit / entry_cost * 100) if entry_cost > 0 else 0, 2
        )
        self._current_trade["profit_if_held"] = round(profit_if_held, 2)

        # Set defaults for missing exit fields (held to resolution)
        if "exit_type" not in self._current_trade:
            self._current_trade["exit_type"] = "resolution"
            self._current_trade["exit_price"] = 0.0
            self._current_trade["exit_shares_sold"] = 0.0
            self._current_trade["exit_revenue"] = round(exit_revenue, 2)
            self._current_trade["residual_shares"] = round(entry_shares, 1)
            self._current_trade["residual_value"] = 0.0
            self._current_trade["exit_latency_ms"] = 0

        # Fix min_sell_price sentinel
        if self._current_trade.get("min_sell_price_seen", 999) >= 999:
            self._current_trade["min_sell_price_seen"] = 0.0

        # Write the complete trade record
        self._append_row(self._trade_path, self._current_trade, TRADE_FIELDS)
        self._current_trade = {}

    # ── Execution logging ───────────────────────────────────────────

    def log_execution(
        self,
        window_ts: int,
        action: str,
        latency_ms: float,
        success: bool,
        error: str = "",
        details: str = "",
    ):
        self._total_latency_ms += latency_ms
        self._latency_count += 1

        row = {
            "timestamp": time.time(),
            "window_ts": window_ts,
            "action": action,
            "latency_ms": round(latency_ms, 1),
            "success": success,
            "error": error,
            "details": details[:200],  # Truncate long error messages
        }
        self._append_row(self._exec_path, row, EXECUTION_FIELDS)

    # ── Session summary ─────────────────────────────────────────────

    def session_summary(self, final_balance: float) -> dict:
        runtime_min = (time.time() - self._session_start) / 60
        real_pnl = final_balance - self._session_start_balance
        avg_slippage = (self._total_slippage / self._slippage_count
                        if self._slippage_count > 0 else 0)
        avg_latency = (self._total_latency_ms / self._latency_count
                       if self._latency_count > 0 else 0)
        fill_rate = (self._signals_traded / self._signals_total * 100
                     if self._signals_total > 0 else 0)

        summary = {
            "runtime_minutes": round(runtime_min, 1),
            "signals_total": self._signals_total,
            "signals_traded": self._signals_traded,
            "signals_skipped_edge": self._signals_skipped_edge,
            "signals_skipped_min": self._signals_skipped_min,
            "signals_skipped_cap": self._signals_skipped_cap,
            "fill_rate_pct": round(fill_rate, 1),
            "avg_slippage": round(avg_slippage, 4),
            "avg_latency_ms": round(avg_latency, 1),
            "session_start_balance": round(self._session_start_balance, 2),
            "session_end_balance": round(final_balance, 2),
            "real_pnl": round(real_pnl, 2),
        }

        print(f"\n{'═' * 55}")
        print(f"  📊 SESSION ANALYTICS")
        print(f"  Runtime: {runtime_min:.0f}min | "
              f"Signals: {self._signals_total} "
              f"({self._signals_traded} traded, "
              f"{self._signals_skipped_edge} edge-gone, "
              f"{self._signals_skipped_min} below-min, "
              f"{self._signals_skipped_cap} price-cap)")
        print(f"  Fill rate: {fill_rate:.0f}% | "
              f"Avg slippage: {avg_slippage:+.4f} | "
              f"Avg latency: {avg_latency:.0f}ms")
        print(f"  Real P&L: ${real_pnl:+.2f} "
              f"(${self._session_start_balance:.2f} → ${final_balance:.2f})")
        print(f"  Logs: {self.log_dir}/")
        print(f"{'═' * 55}")

        return summary

    # ── Internal ────────────────────────────────────────────────────

    def _ensure_headers(self, path: str, fields: list):
        if not os.path.exists(path):
            with open(path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fields)
                writer.writeheader()

    def _append_row(self, path: str, row: dict, fields: list):
        # Only write fields that exist in the schema
        clean_row = {k: row.get(k, "") for k in fields}
        try:
            with open(path, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fields)
                writer.writerow(clean_row)
        except Exception as e:
            print(f"[tracker] Write failed: {e}")
