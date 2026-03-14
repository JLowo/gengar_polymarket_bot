"""Strategy engine for the oracle lag scalper.

Features:
- Brownian motion probability estimation
- Kelly criterion position sizing (quarter-Kelly default)
- Hourly stats tracking for Telegram summaries
"""

import time
import math
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TradeSignal:
    side: str
    confidence: float
    btc_delta_pct: float
    market_price: float
    edge: float
    true_prob: float
    seconds_remaining: float
    kelly_size: float


@dataclass
class StrategyConfig:
    min_edge: float = 0.03
    entry_window_start: int = 60
    entry_window_end: int = 10
    max_price: float = 0.95
    min_price: float = 0.50
    min_btc_delta_pct: float = 0.01
    kelly_fraction: float = 0.25    # Quarter-Kelly (conservative)
    min_bet: float = 1.0            # Polymarket minimum
    max_bet: float = 25.0           # Hard cap per trade


@dataclass
class HourlyStats:
    """Tracks metrics for the current hour. Resets every hour."""
    hour_start: float = 0.0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    pnl: float = 0.0
    windows_seen: int = 0
    windows_skipped: int = 0
    edges: list = field(default_factory=list)
    deltas: list = field(default_factory=list)
    trade_profits: list = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        return (self.wins / self.trades * 100) if self.trades > 0 else 0.0

    @property
    def avg_edge(self) -> float:
        return sum(self.edges) / len(self.edges) if self.edges else 0.0

    @property
    def avg_delta(self) -> float:
        return sum(abs(d) for d in self.deltas) / len(self.deltas) if self.deltas else 0.0

    @property
    def best_trade(self) -> float:
        return max(self.trade_profits) if self.trade_profits else 0.0

    @property
    def worst_trade(self) -> float:
        return min(self.trade_profits) if self.trade_profits else 0.0

    def record_trade(self, edge: float, delta: float):
        self.trades += 1
        self.edges.append(edge)
        self.deltas.append(delta)

    def record_result(self, profit: float, won: bool):
        if won:
            self.wins += 1
        else:
            self.losses += 1
        self.pnl += profit
        self.trade_profits.append(profit)

    def record_window(self, traded: bool):
        self.windows_seen += 1
        if not traded:
            self.windows_skipped += 1

    def reset(self):
        self.hour_start = time.time()
        self.trades = 0
        self.wins = 0
        self.losses = 0
        self.pnl = 0.0
        self.windows_seen = 0
        self.windows_skipped = 0
        self.edges.clear()
        self.deltas.clear()
        self.trade_profits.clear()

    def to_dict(self) -> dict:
        return {
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": self.win_rate,
            "pnl": self.pnl,
            "windows_seen": self.windows_seen,
            "windows_skipped": self.windows_skipped,
            "avg_edge": self.avg_edge,
            "avg_delta": self.avg_delta,
            "best_trade": self.best_trade,
            "worst_trade": self.worst_trade,
        }


@dataclass
class TradingStats:
    """Overall lifetime stats with embedded hourly tracker."""
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    bankroll: float = 100.0
    hourly: HourlyStats = field(default_factory=HourlyStats)

    @property
    def win_rate(self) -> float:
        return (self.wins / self.total_trades * 100) if self.total_trades > 0 else 0.0

    def record_win(self, profit: float):
        self.total_trades += 1
        self.wins += 1
        self.total_pnl += profit
        self.bankroll += profit
        self.hourly.record_result(profit, won=True)

    def record_loss(self, loss: float):
        self.total_trades += 1
        self.losses += 1
        self.total_pnl -= abs(loss)
        self.bankroll -= abs(loss)
        self.hourly.record_result(-abs(loss), won=False)

    def to_dict(self) -> dict:
        return {
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": self.win_rate,
            "pnl": self.total_pnl,
            "bankroll": self.bankroll,
        }


def kelly_bet_size(
    true_prob: float,
    market_price: float,
    bankroll: float,
    fraction: float = 0.25,
    min_bet: float = 1.0,
    max_bet: float = 25.0,
) -> float:
    """Calculate Kelly criterion bet size.

    Binary market: buy at market_price, win pays $1.
      b = (1 - market_price) / market_price   (net odds)
      kelly_f = (b * p - q) / b

    Uses fractional Kelly (default 0.25 = quarter Kelly) for safety.
    """
    if market_price <= 0 or market_price >= 1:
        return 0.0

    b = (1.0 - market_price) / market_price
    q = 1.0 - true_prob
    kelly_f = (b * true_prob - q) / b

    if kelly_f <= 0:
        return 0.0

    bet = bankroll * kelly_f * fraction
    return max(min(bet, max_bet), min_bet)


def estimate_true_probability(btc_delta_pct: float, seconds_remaining: float) -> float:
    """Estimate true probability using Brownian motion model."""
    btc_5min_vol = 0.08

    time_factor = max(seconds_remaining, 1) / 300
    effective_vol = btc_5min_vol * math.sqrt(time_factor)

    if effective_vol == 0:
        return 1.0 if btc_delta_pct > 0 else 0.0

    z_score = abs(btc_delta_pct) / effective_vol
    prob = 0.5 * (1 + math.erf(z_score / math.sqrt(2)))

    return min(max(prob, 0.01), 0.99)


def evaluate(
    btc_price: float,
    opening_price: float,
    up_market_price: float,
    down_market_price: float,
    seconds_remaining: float,
    bankroll: float = 100.0,
    config: StrategyConfig = None,
) -> Optional[TradeSignal]:
    """Evaluate whether to enter a trade. Returns signal with Kelly size or None."""
    if config is None:
        config = StrategyConfig()

    if seconds_remaining > config.entry_window_start:
        return None
    if seconds_remaining < config.entry_window_end:
        return None
    if opening_price <= 0:
        return None

    btc_delta_pct = ((btc_price - opening_price) / opening_price) * 100

    if abs(btc_delta_pct) < config.min_btc_delta_pct:
        return None

    side = "UP" if btc_delta_pct > 0 else "DOWN"
    market_price = up_market_price if btc_delta_pct > 0 else down_market_price

    if market_price > config.max_price or market_price < config.min_price:
        return None

    true_prob = estimate_true_probability(btc_delta_pct, seconds_remaining)
    edge = true_prob - market_price

    if edge < config.min_edge:
        return None

    bet_size = kelly_bet_size(
        true_prob=true_prob,
        market_price=market_price,
        bankroll=bankroll,
        fraction=config.kelly_fraction,
        min_bet=config.min_bet,
        max_bet=config.max_bet,
    )

    confidence = min(edge / 0.10, 1.0)

    return TradeSignal(
        side=side,
        confidence=confidence,
        btc_delta_pct=btc_delta_pct,
        market_price=market_price,
        edge=edge,
        true_prob=true_prob,
        seconds_remaining=seconds_remaining,
        kelly_size=bet_size,
    )
