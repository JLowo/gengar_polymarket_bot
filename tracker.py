"""Quant-grade performance tracker for PolyBot.

Log files (append-only CSV):

1. signals.csv — Every signal the strategy evaluates (traded or not).
   Answers: Are our edges real? What are we missing?

2. trades.csv — Full lifecycle of every trade (entry → hold → resolve).
   Answers: How well do we execute? Where does P&L leak?

3. executions.csv — Every API call with timing (optional).
   Answers: How fast are we? Where's the latency?

Usage:
    tracker = Tracker(log_dir="logs")
    tracker.log_signal(...)         # Every evaluate() result
    tracker.open_trade(...)         # On buy fill
    tracker.update_hold_stats(...)  # Each position check tick
    tracker.close_trade(...)        # At resolution — writes the trade row (by trade_id)
    tracker.log_execution(...)      # Every API call (optional)
    tracker.session_summary()       # On shutdown
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
    "skip_reason",      # "delta_too_small", "prob_below_min", "edge_below_min",
                        # "price_out_of_range", "edge_gone_at_market", or ""
    "actual_price",     # Real market price after preview (0 if not checked)
    "actual_edge",      # Edge at actual price
    "fill_price",       # What we actually paid (0 if not traded)
    "slippage",         # fill_price - market_price (0 if not traded)
    "version",          # BOT_VERSION that logged the row (dashboard filter)
]

# ── Trade record ────────────────────────────────────────────────────

TRADE_FIELDS = [
    "timestamp", "window_ts", "window_time", "trade_id",
    # Entry
    "side", "entry_price", "entry_shares", "entry_cost",
    "edge_at_entry", "prob_at_entry", "btc_delta_at_entry",
    "seconds_remaining_at_entry",
    "entry_delta_pct",          # BTC delta at moment of entry
    "entry_seconds_remaining",  # T-minus at entry
    "entry_latency_ms",         # signal → fill confirmed
    # Hold
    "max_prob_during_hold",     # Peak probability while holding
    "min_prob_during_hold",     # Trough probability
    "max_sell_price_seen",      # Best exit we saw
    "min_sell_price_seen",      # Worst exit we saw
    # Exit
    "exit_type",                # "hold-to-resolution", "forced-exit"
    "exit_price", "exit_shares_sold", "exit_revenue",
    "residual_shares", "residual_value",
    "exit_latency_ms",
    # Resolution
    "btc_final_price", "btc_final_delta_pct",
    "won_resolution",           # Did BTC go our way?
    "resolution_payout",        # What resolution would have paid
    "resolution_method",        # "claim_sell", "balance_check", "binance_fallback", "exited"
    "claim_result",             # "filled", "no_match", "inconclusive", "not_attempted"
    # P&L
    "profit", "return_pct",
    "profit_if_held",           # What we'd have made holding to resolution
    # Meta
    "version",
    "bankroll",                 # Portfolio balance after this trade resolves
]


@dataclass
class TradeRecord:
    """In-memory state for a single trade's lifecycle (entry + hold).

    Created by open_trade(), updated by update_hold_stats(),
    consumed by close_trade() which adds resolution fields and writes CSV.
    """
    timestamp: float = 0.0
    window_ts: int = 0
    window_time: str = ""
    trade_id: int = 0
    side: str = ""
    entry_price: float = 0.0
    entry_shares: float = 0.0
    entry_cost: float = 0.0
    edge_at_entry: float = 0.0
    prob_at_entry: float = 0.0
    btc_delta_at_entry: float = 0.0
    seconds_remaining_at_entry: float = 0.0
    entry_delta_pct: float = 0.0
    entry_seconds_remaining: float = 0.0
    entry_latency_ms: float = 0.0
    # Hold (updated live via update_hold_stats)
    max_prob_during_hold: float = 0.0
    min_prob_during_hold: float = 1.0
    max_sell_price_seen: float = 0.0
    min_sell_price_seen: float = 999.0
    # Meta
    version: str = ""


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

# ── Hold tick record (one row per position check, ~every 3s) ───────

HOLD_TICK_FIELDS = [
    "timestamp", "trade_id", "window_ts",
    "seconds_remaining",
    "btc_price", "btc_delta_pct",
    "prob", "sell_price",
    "unrealized_pnl", "return_pct",
]

SESSION_FIELDS = [
    "start_time", "end_time",
    "start_balance", "end_balance",
    "real_pnl",                 # end_balance - start_balance
    "tracked_pnl",              # from stats
    "pnl_drift",                # real_pnl - tracked_pnl
    "trades", "wins", "losses", "win_rate",
    "avg_entry_price", "avg_edge", "avg_delta",
]


class Tracker:
    def __init__(self, log_dir: str = "logs", log_executions: bool = False, version: str = ""):
        self.version = version   # written to every signals.csv row
        self.log_dir = log_dir
        self.log_executions = log_executions
        os.makedirs(log_dir, exist_ok=True)

        self._signal_path = os.path.join(log_dir, "signals.csv")
        self._trade_path = os.path.join(log_dir, "trades.csv")
        self._tick_path = os.path.join(log_dir, "hold_ticks.csv")
        self._exec_path = os.path.join(log_dir, "executions.csv")
        self._session_path = os.path.join(log_dir, "sessions.csv")

        self._ensure_headers(self._signal_path, SIGNAL_FIELDS)
        self._ensure_headers(self._trade_path, TRADE_FIELDS)
        self._ensure_headers(self._tick_path, HOLD_TICK_FIELDS)
        self._ensure_headers(self._session_path, SESSION_FIELDS)
        if self.log_executions:
            self._ensure_headers(self._exec_path, EXECUTION_FIELDS)

        # Trades not yet written, keyed by trade_id. A trade's result can arrive
        # after the next trade has opened (deferred resolution), so the trade
        # being held right now (hold stats, hold ticks) is tracked as "active".
        self._open_trades: dict[int, TradeRecord] = {}
        self._active_trade_id: int = 0
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
        skip_reason: str = "",
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
            "skip_reason": skip_reason,
            "actual_price": round(actual_price, 4),
            "actual_edge": round(actual_edge, 4),
            "fill_price": round(fill_price, 4),
            "slippage": round(slippage, 4),
            "version": self.version,
        }
        self._append_row(self._signal_path, row, SIGNAL_FIELDS)

    # ── Trade lifecycle ─────────────────────────────────────────────

    def open_trade(
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
        entry_delta_pct: float = 0.0,
        entry_seconds_remaining: float = 0.0,
        version: str = "",
    ) -> int:
        """Record a trade entry and make it the active trade. Returns its trade_id."""
        self._trade_counter += 1
        self._open_trades[self._trade_counter] = TradeRecord(
            timestamp=time.time(),
            window_ts=window_ts,
            window_time=time.strftime("%H:%M", time.localtime(window_ts)),
            trade_id=self._trade_counter,
            side=side,
            entry_price=round(entry_price, 4),
            entry_shares=round(entry_shares, 1),
            entry_cost=round(entry_cost, 2),
            edge_at_entry=round(edge, 4),
            prob_at_entry=round(prob, 4),
            btc_delta_at_entry=round(btc_delta, 4),
            seconds_remaining_at_entry=round(seconds_remaining, 1),
            entry_latency_ms=round(latency_ms, 0),
            entry_delta_pct=round(entry_delta_pct, 4),
            entry_seconds_remaining=round(entry_seconds_remaining, 1),
            max_prob_during_hold=round(prob, 4),
            min_prob_during_hold=round(prob, 4),
            version=version,
        )
        self._active_trade_id = self._trade_counter
        return self._trade_counter

    def add_fee(self, trade_id: int, fee: float):
        """Add Polymarket's trading fee (measured after the buy) to a trade's
        entry_cost, so trades.csv shows what left the wallet. entry_price stays
        the fill price, so the fee is about entry_cost - entry_shares × entry_price."""
        t = self._open_trades.get(trade_id)
        if t:
            t.entry_cost = round(t.entry_cost + fee, 2)

    def update_hold_stats(self, prob: float, sell_price: float):
        """Call on each position check tick to track hold-period extremes."""
        t = self._open_trades.get(self._active_trade_id)
        if not t:
            return
        if prob > t.max_prob_during_hold:
            t.max_prob_during_hold = round(prob, 4)
        if prob < t.min_prob_during_hold:
            t.min_prob_during_hold = round(prob, 4)
        if sell_price > 0:
            if sell_price > t.max_sell_price_seen:
                t.max_sell_price_seen = round(sell_price, 4)
            if sell_price < t.min_sell_price_seen:
                t.min_sell_price_seen = round(sell_price, 4)

    def log_hold_tick(
        self,
        seconds_remaining: float,
        btc_price: float,
        btc_delta_pct: float,
        prob: float,
        sell_price: float,
        unrealized_pnl: float,
        return_pct: float,
    ):
        """Log one position-check tick of the active trade to hold_ticks.csv (~every 3s)."""
        t = self._open_trades.get(self._active_trade_id)
        if not t:
            return
        row = {
            "timestamp": time.time(),
            "trade_id": t.trade_id,
            "window_ts": t.window_ts,
            "seconds_remaining": round(seconds_remaining, 1),
            "btc_price": round(btc_price, 2),
            "btc_delta_pct": round(btc_delta_pct, 4),
            "prob": round(prob, 4),
            "sell_price": round(sell_price, 4),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "return_pct": round(return_pct, 4),
        }
        self._append_row(self._tick_path, row, HOLD_TICK_FIELDS)

    def _pop_trade(self, trade_id: int) -> Optional[TradeRecord]:
        t = self._open_trades.pop(trade_id, None)
        if t and trade_id == self._active_trade_id:
            self._active_trade_id = 0
        return t

    def close_trade(
        self,
        btc_final_price: float,
        opening_price: float,
        won: bool,
        profit: float,
        exit_revenue: float = 0.0,
        resolution_method: str = "binance_fallback",
        claim_result: str = "not_attempted",
        resolution_payout: Optional[float] = None,
        bankroll: float = 0.0,
        *,
        trade_id: int,
    ):
        """Write the trade's row to trades.csv.

        trade_id is required: a trade can resolve after the next one has opened,
        so never fall back to "whichever is active". The caller passes the
        profit it booked (it knows claim revenue, split payouts, early exits),
        so stats and the CSV always agree. resolution_payout defaults to $1 per
        share for a win; pass the real figure for split results.
        """
        t = self._pop_trade(trade_id)
        if not t:
            print(f"[tracker] No open trade #{trade_id} to close — row not written")
            return

        if resolution_payout is None:
            resolution_payout = t.entry_shares * 1.0 if won else 0.0
        btc_delta = ((btc_final_price - opening_price) / opening_price * 100) if opening_price > 0 else 0
        self._write_trade_row(t, {
            # Exit
            "exit_type": "forced-exit" if resolution_method == "exited" else "hold-to-resolution",
            "exit_price": 0.0,
            "exit_shares_sold": 0.0,
            "exit_revenue": round(exit_revenue, 2),
            "residual_shares": round(t.entry_shares, 1),
            "residual_value": 0.0,
            "exit_latency_ms": 0,
            # Resolution
            "btc_final_price": round(btc_final_price, 2),
            "btc_final_delta_pct": round(btc_delta, 4),
            "won_resolution": won,
            "resolution_payout": round(resolution_payout, 2),
            "resolution_method": resolution_method,
            "claim_result": claim_result,
            # P&L
            "profit": round(profit, 2),
            "return_pct": round((profit / t.entry_cost * 100) if t.entry_cost > 0 else 0, 2),
            "profit_if_held": round(resolution_payout - t.entry_cost, 2),
            "bankroll": round(bankroll, 2),
        })

    def log_trade_unresolved(self, reason: str, *, trade_id: int):
        """Write a row for a trade whose result never arrived (bot stopped, or
        no resolution in time) so trades.csv still has it. The result columns
        (won_resolution, profit, ...) stay blank; resolution_method says why."""
        t = self._pop_trade(trade_id)
        if t:
            self._write_trade_row(t, {"resolution_method": reason})

    def _write_trade_row(self, t: TradeRecord, extra: dict):
        row = asdict(t)
        if row["min_sell_price_seen"] >= 999:   # never saw a sell price
            row["min_sell_price_seen"] = 0.0
        row.update(extra)
        self._append_row(self._trade_path, row, TRADE_FIELDS)

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

        if not self.log_executions:
            return

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

    # ── Session logging ─────────────────────────────────────────────

    def log_session(
        self,
        start_time: float,
        end_time: float,
        start_balance: float,
        end_balance: float,
        tracked_pnl: float,
        trades: int,
        wins: int,
        losses: int,
        avg_entry_price: float = 0.0,
        avg_edge: float = 0.0,
        avg_delta: float = 0.0,
    ):
        real_pnl = end_balance - start_balance
        win_rate = (wins / trades * 100) if trades > 0 else 0.0
        row = {
            "start_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_time)),
            "end_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(end_time)),
            "start_balance": round(start_balance, 2),
            "end_balance": round(end_balance, 2),
            "real_pnl": round(real_pnl, 2),
            "tracked_pnl": round(tracked_pnl, 2),
            "pnl_drift": round(real_pnl - tracked_pnl, 2),
            "trades": trades,
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 1),
            "avg_entry_price": round(avg_entry_price, 4),
            "avg_edge": round(avg_edge, 4),
            "avg_delta": round(avg_delta, 4),
        }
        self._append_row(self._session_path, row, SESSION_FIELDS)

    # ── Internal ────────────────────────────────────────────────────

    def _ensure_headers(self, path: str, fields: list):
        if not os.path.exists(path):
            with open(path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fields)
                writer.writeheader()
            return

        # Check if existing header matches current schema — if not, archive and recreate
        with open(path, "r", newline="") as f:
            reader = csv.reader(f)
            existing_header = next(reader, None)
        if existing_header and existing_header != fields:
            archive = path.replace(".csv", "_pre_schema_change.csv")
            os.rename(path, archive)
            print(f"[tracker] Schema changed — archived {path} → {archive}")
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
