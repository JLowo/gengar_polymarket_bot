# PolyBot Changelog

Standard sections: **Strategy** (parameter/model/filter changes), **Bug fixes** (things that were broken), **Dashboard** (visualization/UI), **Infrastructure** (refactoring, deployment, docs, tooling). Sections omitted when empty.

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
