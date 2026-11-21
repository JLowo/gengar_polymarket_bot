# PolyBot Changelog

Standard sections: **Strategy** (parameter/model/filter changes), **Bug fixes** (things that were broken), **Dashboard** (visualization/UI), **Infrastructure** (refactoring, deployment, docs, tooling). Sections omitted when empty.

---

## Unreleased

### Bug fixes
- **A CSV schema change could delete an earlier archive**: the tracker renamed the old file to `*_pre_schema_change.csv`, overwriting one that already existed. Added columns are now filled in place (older rows blank); other header changes archive under a new name.
- **A failed buy attempt could leave no reason**: every way an attempt can stop now logs a `signals.csv` row (`skipped_halted`, `skipped_health_check`, `skipped_loss_limit`, `skipped_no_market`, `skipped_too_late`, `buy_failed` with a reason such as `no_fill`). The executor returns that reason with each failed order. An "unknown fill" that turns out to have filled also gets a `traded` row.
- **Quiet windows were logged at their last second**, when the market had settled, so the reason (usually "price out of range") said nothing about the entry window. The bot now logs the window's closest call: the entry-window moment that passed most checks, with the check it failed, at the vol in force then. No such row after an attempt, so no duplicate rows either.

### Infrastructure
- **Session log**: everything the bot prints is also saved to `logs/bot_<date>_<time>.log`, timestamped, so a session can be reviewed after tmux closes.
- New columns: `signals.csv` `vol`; `trades.csv` `fee`, `vol_at_entry`; `sessions.csv` `fees`. The shutdown summary shows the session's fees.
- `signals.csv` attempt rows now always carry the signal's price and edge in `market_price`/`edge`, and the checked book price in `actual_price`/`actual_edge` (edge-gone rows used to put the checked price in both).
- `strategy.explain_entry()` gives evaluate()'s first failed check with its numbers; a test keeps the two in step.

### Dashboard
- **Near misses vs quiet windows**: "Skipped" counted every window without a trade. It now separates near misses (the bot tried to buy but didn't) from quiet windows (no signal at all, whose logged reason is from the window's last second and isn't shown).
- **Would-have-won for near misses**: Polymarket's result for each near-miss window (Gamma, cached in `logs/outcomes.json`) and the P&L if taken at the price the bot would have paid, before fees, in a new table and in the stat card and charts.
- **Edge: Seen vs At Fill** chart: the edge the bot saw at its cached price next to the edge at the fill price.
- **Sessions table** from `sessions.csv`: real vs tracked P&L, drift, trades, win rate.
- **Auto-refresh** every 30s (can be switched off), redrawing only when the logs change and keeping the filter and open trade detail. New `/api/sessions` route.
- Trade detail title showed the BTC open price instead of the entry price.
- Labels for the new attempt reasons; **Quiet Windows: Closest Call** chart; **Fees** card and fee columns in Recent Trades and Sessions. Numbered schema archives are read too.

---

## 2026.11.21 — Fixes from the first live session

### Bug fixes
- **Auto-redeemed wins counted twice**: the live bankroll now values every tracked position at the shares the Safe still holds, so a win counts once however it is redeemed. Buys no longer move the bankroll (positions are carried at their fill price); results move it by payout − cost.
- **A failed positions lookup counted as $0**: it is now an unknown. That window's bankroll sync and redemptions are skipped, and start-up won't go live without it.
- **Fees not recorded**: Polymarket's trading fee is measured from the pUSD change after each fill and included in the trade's cost and profit.
- **Ctrl+C stopped Tor first**, so the final order cancel and balance read failed and the session summary was wrong. Tor now runs in its own process group and stops when the bot exits. Its log pipe is also drained, so it can't fill up and stall Tor.
- **Window header** showed only booked results ("Trades: 0" with a trade waiting); it now shows fills, waiting results and the bankroll.
- The hourly summary no longer resets the bankroll to pUSD alone.

### Strategy
- **Early results**: a finished window's decisive final price (≥ 0.95) values the waiting trade before Polymarket resolves it, so the daily loss limit reacts straight away. Booking still waits for the resolution.
- The pre-trade CLOB check is now a pUSD read; no order is sent with under 5s of the window left.

---

## 2026.11.19 — Bug-review fixes + v16 port

### Bug fixes
- **Bankroll counted every win and loss twice** (`record_win`/`record_loss` moved bankroll as well as their callers).
- **Hold-period probability was inverted** for winning DOWN and losing UP trades (also fixes `hold_ticks.csv` and the min/max prob columns).
- **Realized vol read ~40% low**: RMS of signed window deltas instead of the stdev of |delta|.
- **Unknown opening price**: a window whose first price arrives >5s late is not traded and kept out of the vol estimate.
- **Results from Polymarket, not balance changes**: dry-run trades and unconfirmed live claims are booked from Gamma's resolution (split/cancelled markets at their real payout); trades with no result still get a row.
- **Tracker keeps open trades by `trade_id`**, so a late result can't land on the next trade's row.
- **Dry run trades on real order-book prices** instead of a flat $0.55 fill.
- No price fetches in the last 15s of a window; one market lookup per window.
- `sessions.csv` averages, `check_proxy()` on HTTP errors, `start.sh` window kept open after Ctrl+C.

### Dashboard
- v16 dashboard ported onto the fixed code. Hardened: only its four routes are served (no file handler), no wildcard CORS header, Chart.js pinned to `dist/chart.umd.js` with an SRI hash. Still reachable from the local network.
- Skipped-signal panel reads `signals.csv` (which now has a `version` column); whether a skipped signal would have won isn't logged yet and is shown as "not logged".

### Infrastructure
- v16's `open_trade`/`close_trade`/`log_hold_tick` API and `TradeRecord` are kept, but `close_trade()` now takes a required `trade_id` and the profit the caller booked (needed for split payouts and deferred resolution).
- Setup for a fresh Mac: `start.sh` works from the repo folder with the repo's `.venv`; `.env.example`; `logs/` ignored.

### Known gap
- Live trading needs the SDK migration: Polymarket rejects the v1 `py-clob-client` since CLOB V2 went live on 2026-04-28.

---

## v16 — Data Collection Refactor

### Dashboard
- **Interactive dashboard**: `dashboard.py` — self-contained HTTP server (no external deps beyond stdlib) with Chart.js frontend. Equity curve, win rate by version, P&L waterfall, skipped signal analysis, recent trades table. Version filter buttons. Run with `python dashboard.py`.
- **Trade trajectory drill-down**: Click any trade in the recent trades table to see 4 trajectory charts (BTC delta %, model probability, unrealized P&L, sell price) from hold_ticks.csv data.
- **Dynamic version colors**: Dashboard auto-assigns colors to any version string. Supports date-based versions (e.g. `v2026.3.26`) alongside legacy `v13`/`v14`/`v15`.

### Infrastructure
- **TradeRecord dataclass**: Replaced dict-based `_current_trade` in tracker.py with a typed `TradeRecord` dataclass. Entry and hold fields are populated during the trade lifecycle; exit and resolution fields are computed at close.
- **Centralized profit computation**: `close_trade()` computes profit for all 5 resolution paths (exited, market_price, claim_sell, balance_check, binance_fallback). Bot.py no longer calculates profit — it uses the returned value from `close_trade()`. *(Superseded in 2026.11.19: the caller passes the profit it booked.)*
- **Lifecycle API rename**: `log_trade_entry()` → `open_trade()`, `log_trade_resolve()` → `close_trade()`. Clearer intent, matches the open/update/close lifecycle pattern.
- **Dead code removed**: `log_trade_exit()` removed from tracker.py (never called from bot.py).
- **Hold trajectory logging**: New `logs/hold_ticks.csv` records every position-check tick (~3s intervals) with BTC price, model probability, sell price, and unrealized P&L. Enables per-trade trajectory analysis and reversion tracking.
- **Pending buy logging fix**: Late-fill trades detected at window boundary now call `open_trade()` so they appear in trades.csv.
- **Dashboard guide**: Added `DASHBOARD_GUIDE.md` — explains how to read every chart and metric for filter tuning decisions.
- **Date-based versioning**: `BOT_VERSION` changed from integer (`15`) to date string (`"2026.3.26"`). Version recorded in trades.csv, displayed as `v2026.3.26` in dashboard.
- **Gitignore**: Added `logs/`, `charts/`, `.DS_Store`.
