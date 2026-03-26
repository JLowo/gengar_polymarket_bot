# PolyBot Changelog

Standard sections: **Strategy** (parameter/model/filter changes), **Bug fixes** (things that were broken), **Dashboard** (visualization/UI), **Infrastructure** (refactoring, deployment, docs, tooling). Sections omitted when empty.

---

## v16 — Data Collection Refactor

### Dashboard
- **Interactive dashboard**: `dashboard.py` — self-contained HTTP server (no external deps beyond stdlib) with Chart.js frontend. Equity curve, win rate by version, P&L waterfall, skipped signal analysis, recent trades table. Version filter buttons. Run with `python dashboard.py`.
- **Trade trajectory drill-down**: Click any trade in the recent trades table to see 4 trajectory charts (BTC delta %, model probability, unrealized P&L, sell price) from hold_ticks.csv data.
- **Dynamic version colors**: Dashboard auto-assigns colors to any version string. Supports date-based versions (e.g. `v2026.3.26`) alongside legacy `v13`/`v14`/`v15`.

### Infrastructure
- **TradeRecord dataclass**: Replaced dict-based `_current_trade` in tracker.py with a typed `TradeRecord` dataclass. Entry and hold fields are populated during the trade lifecycle; exit and resolution fields are computed at close.
- **Centralized profit computation**: `close_trade()` computes profit for all 5 resolution paths (exited, market_price, claim_sell, balance_check, binance_fallback). Bot.py no longer calculates profit — it uses the returned value from `close_trade()`.
- **Lifecycle API rename**: `log_trade_entry()` → `open_trade()`, `log_trade_resolve()` → `close_trade()`. Clearer intent, matches the open/update/close lifecycle pattern.
- **Dead code removed**: `log_trade_exit()` removed from tracker.py (never called from bot.py).
- **Hold trajectory logging**: New `logs/hold_ticks.csv` records every position-check tick (~3s intervals) with BTC price, model probability, sell price, and unrealized P&L. Enables per-trade trajectory analysis and reversion tracking.
- **Pending buy logging fix**: Late-fill trades detected at window boundary now call `open_trade()` so they appear in trades.csv.
- **Dashboard guide**: Added `DASHBOARD_GUIDE.md` — explains how to read every chart and metric for filter tuning decisions.
- **Date-based versioning**: `BOT_VERSION` changed from integer (`15`) to date string (`"2026.3.26"`). Version recorded in trades.csv, displayed as `v2026.3.26` in dashboard.
- **Gitignore**: Added `logs/`, `charts/`, `.DS_Store`.
