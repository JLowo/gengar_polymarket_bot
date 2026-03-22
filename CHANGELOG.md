# PolyBot Changelog

## v15 — Skipped Signal Tracking + Live Dashboard + Analytics

### Skipped signal tracking
- **Skipped signal logging**: When a signal passes strategy filters but gets blocked by a pre-trade check (edge_gone, slippage, btc_reversed), the bot logs it to `logs/skipped.csv` with the BTC outcome at window close (`would_have_won`). Validates whether filters are saving money or being too aggressive.
- **Terminal feedback on skips**: Prints `Skipped UP (slippage) -> would have WON/LOST` at window boundary.
- **Early data**: 82% of edge_gone skips would have won — filter may be too tight. Monitoring with more data.

### Live web dashboard
- **dashboard.py**: Interactive trading dashboard served at `http://localhost:8050`. Reads CSVs fresh on every page load — refresh during a session to see new trades.
- **6 interactive charts**: Equity curve (continuous line across versions), win rate by version, P&L by version, return distribution, per-trade waterfall, skipped signal analysis.
- **Stats cards**: Total trades, overall win rate, total P&L, average return — all update with filter.
- **Version filter**: Click All/v13/v14/v15 buttons to isolate any version's data across all charts and stats.
- **Recent trades table**: Last 20 trades with entry price, cost, profit, and return %.
- **Tech stack**: Python stdlib HTTP server + Chart.js via CDN. No Flask or external web dependencies.

### Static chart generator
- **charts.py**: Generates 6 PNG charts to `charts/` directory using matplotlib. Same data as dashboard but as static images for sharing or archiving.

### Version tracking
- All trades and skipped signals now include a `version` column in CSV output.
- Existing v14 trades archived to `logs/trades_v14.csv`.
- `BOT_VERSION = 15` constant in bot.py drives version tagging.

### Documentation
- **README.md**: Added Dashboard section with install/launch instructions and feature list. Updated Tracker output to include `skipped.csv`.

## v14 — Bug Fixes + Pre-Trade Filters
- **Position sizing fix**: `_verify_buy_via_balance()` used raw balance delta as trade cost. Concurrent settlements (previous sells arriving) contaminated the delta — $25 bets recorded as $114. Fix: cap `spent` at `shares * price`.
- **P&L double-counting fix**: `record_win()`/`record_loss()` adjusted bankroll, but bot.py already adjusted it via buy/sell operations. Removed duplicate `self.bankroll +=/-=` from record_win/record_loss.
- **Slippage filter**: If market repriced >$0.09 between cached price and execution, skip the trade. Data showed 0/3 win rate on slippage >$0.10.
- **Slippage logging**: Added `planned_price` and `slippage` columns to trades.csv.
- **BTC direction re-check**: Verify BTC is still moving in the signal's direction before buying. Catches stale-signal bug where UP signal persisted after BTC reversed.
- **min_btc_delta raised**: 0.01% -> 0.04%. Filters out tiny BTC moves that are noise and frequently reverse.
- **Missing CSV logging**: Added `log_trade_resolve()` to two win paths (claim success, below-$5 auto-resolve) that were returning before logging.
- **Tracker wiring**: `log_trade_entry()` and `log_trade_resolve()` actually called from bot.py (were defined but never used).

## v13 — Recalibrated + Safety Systems
- **Volatility recalibration**: `btc_5min_vol` 0.08 -> 0.12. Old model was ~2x overconfident (0.05% move = "91% confident"). Now only genuine moves (0.10%+) reach the 80% threshold.
- **Safety factor**: 0.70 -> 0.85. Original was too aggressive for 5-min markets (zero trades in 2.5 hours).
- **CLOB health check circuit breaker**: `get_ok()` before every trade. 3 consecutive failures halt trading with Telegram alert. Auto-recovers at next window boundary.
- **Daily loss limit**: Session P&L <= -$30 halts all trading.
- **All stops removed**: Prob-stop, price-stop, take-profit all removed. Data showed stops cost $35.45 across 5 fires; 4/5 stopped trades won at resolution.
- **`create_order` fix re-applied**: Switched back from `create_market_order` to `create_order` with integer shares to avoid float precision errors.

## v12 — Stop Removal
- **Prob-stop removal**: Removed probability-based stop-loss. BTC micro-bounces in 5-min windows triggered panic sells that reversed.
- **Price-stop removal**: Removed price-based stop-loss for same reason.
- **Take-profit removal**: Removed take-profit. Holding to resolution consistently outperformed early exits.

## v11 — Balance Verification + Ghost Fill Detection
- **Balance-verified buys**: Snapshot USDC before/after order. Ghost fills caught via balance drop even when API throws exception.
- **Pending buy safety net**: Unverified buys checked at next window boundary via balance comparison. Retroactively tracked as filled if balance dropped.
- **Window-boundary balance sync**: Real USDC balance overwrites internal tracking every 5 minutes.
- **Minimum notional guard**: Skip sells below $5 notional to avoid Polymarket rejection errors.

## v10 — Decimal Precision Fix
- **Integer shares via `create_order`**: Float math (`1.0 - 0.71 = 0.29000000000000004`) violated Polymarket's 4-decimal rule. Fix: pass `int(shares)` and `round(price, 2)` to `create_order(OrderArgs)`.
- **Stopped using `create_market_order`**: Internal `amount/price` division produces non-integer shares.

## v1-v9 — Initial Development
- Brownian motion probability model
- Kelly criterion position sizing
- Binance WebSocket price feed
- Polymarket Gamma API market discovery
- Tor proxy for CLOB API geo-restrictions
- Telegram notifications
