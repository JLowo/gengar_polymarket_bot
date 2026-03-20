# CLAUDE.md — PolyBot Project Context

## What this is

An algorithmic trading bot ("PolyBot") for Polymarket's 5-minute BTC Up/Down binary markets. The strategy exploits oracle lag between Binance real-time BTC prices and Polymarket's delayed repricing. Built in Python, runs locally, trades real USDC on Polygon.

## Owner

JLow (jlowplayground on Polymarket). Solo developer. Started from zero software development knowledge in January 2026, built this from scratch using Claude + Cursor. Treats this as a serious trading operation.

## Current version: v13 — Recalibrated + Safety Systems

### Files

```
bot.py              → Main loop, position lifecycle, circuit breakers (v13)
strategy.py         → Brownian motion probability + Kelly criterion (recalibrated vol=0.12)
executor.py         → Polymarket CLOB order execution (balance-verified, create_order path)
market.py           → Market discovery via Gamma API
price_feed.py       → Binance WebSocket for real-time BTC
tracker.py          → Quant analytics logger (signals.csv, trades.csv, executions.csv)
telegram_notifier.py → Mobile alerts + hourly summaries
proxy.py            → Tor proxy for CLOB API geo-restrictions
```

### Key dependencies

- `py-clob-client` v0.34.6 — Polymarket CLOB SDK
- Binance WebSocket — real-time BTC price feed
- Polymarket Gamma API (`gamma-api.polymarket.com`) — market discovery

### Wallet

- Address: `0xB6bA4816128256af4fa9ac2172991f35c111FC60`
- Polymarket Safe: `0xbcd8Da52677827188A4c205dCC0D46eda3038A50`
- Signature type: 2 (Safe/proxy)

---

## Strategy — How it works

### The edge

Every 5 minutes, Polymarket opens a market: "Will BTC be higher or lower?" Shares pay $1 (correct) or $0 (wrong). BTC moves on Binance instantly, but Polymarket's order book reprices with a lag. The bot buys the correct side during that lag.

### Entry pipeline (three filters)

1. **Brownian motion model** (`estimate_true_probability` in strategy.py)
   - Input: `btc_delta_pct` (BTC move from window open) + `seconds_remaining`
   - Volatility parameter: `btc_5min_vol = 0.12` (recalibrated — was 0.08, see calibration section)
   - Output: probability that BTC will be above/below opening price at resolution

2. **Minimum probability gate**: `min_prob = 0.80` — model must be 80%+ confident

3. **Margin of safety**: `market_price ≤ true_prob × safety_factor` where `safety_factor = 0.85`
   - Borrowed from Noisy's article on Polymarket ML trading
   - Even if model is 15% wrong, we break even
   - Was 0.70 initially — too aggressive for 5-min markets, no trades fired. Raised to 0.85.

### Position sizing

Quarter-Kelly criterion. `kelly_f = (b*p - q) / b` where `b = (1-price)/price`. Then `bet = bankroll × kelly_f × 0.25`. Bounded: `$5 ≤ bet ≤ $25`.

### Exit: Hold to resolution (no stops)

All trades hold until the 5-minute window closes. No prob-stop, no price-stop, no take-profit, no forced exit. Data showed stops cost $35.45 across 5 fires (4 of 5 stopped trades won at resolution). The 5-minute window is too short for stops to work — BTC micro-bounces trigger panic sells that reverse.

---

## Safety systems in bot.py

### 1. CLOB health check (circuit breaker)

Before every trade: `self.executor.client.get_ok()` — pings Polymarket's unauthenticated health endpoint.
- Fail → increment `_consecutive_buy_failures`, skip trade
- 3 consecutive failures → `_clob_halted = True`, Telegram alert, stop all trading
- Auto-recovery: each new 5-min window, probe `get_ok()` again. If OK, reset and resume.

### 2. Daily loss limit

`session_pnl = self.stats.bankroll - self._session_start_balance`
If `session_pnl ≤ -DAILY_LOSS_LIMIT` (default $30): halt all trading, Telegram alert.

### 3. Balance-verified buys

- Snapshot USDC before order
- Wait 5s + 3 verification rounds (balance check + order API check + 3s wait each)
- Ghost fills caught via balance drop even when API throws exception
- NEVER cancel on timeout — returns `UNVERIFIED_BUY` for pending detection

### 4. Pending buy safety net

If buy can't be verified in 14s, save order details. Next window boundary:
- Query real balance
- If balance dropped > $1 since buy attempt → retroactively track as filled position
- Resolve normally (claim if won, record loss if lost)

### 5. Window-boundary balance sync

Every new window: query real USDC balance, overwrite internal tracking. Logs drift > $0.50. This is the ultimate source of truth that corrects any accumulated errors.

### 6. Minimum notional guard

Before any sell: check `shares × price ≥ $5`. If below, don't attempt — hold to resolution. Polymarket rejects sells below $5 and the error previously stranded shares.

---

## Critical technical knowledge

### Two-book architecture

Polymarket has TWO order books per token:
- **Raw token book**: illiquid, $0.06/$0.94 spread, almost no volume. `create_order` with default routing lands here.
- **Complement engine book**: tight 1¢ spreads, all real volume. This is where market makers and the UI trade.

All orders MUST route through the complement engine. `create_order(OrderArgs)` routes correctly when you specify price and size. `create_market_order(MarketOrderArgs)` also routes through complement engine but has float precision issues (see below).

### Float precision — the decimal bug

The py-clob-client library internally computes `shares × (1 - price)` using float math:
```python
1.0 - 0.71 = 0.29000000000000004  # IEEE 754 artifact
21 * 0.29000000000000004 * 1e6 = 6090000.000000001  # Violates 4-decimal rule
```

Polymarket CLOB rejects: `"invalid amounts, max accuracy of 4 decimals"`

**The fix**: Use `create_order(OrderArgs(price=round(price, 2), size=float(int(shares))))` — pass integer shares and 2-decimal prices. The library never divides. This was the v10 fix that got lost during file shuffles and had to be re-applied.

DO NOT use `create_market_order(MarketOrderArgs(amount=X))` for buys — the library internally divides `amount/price` producing `21.000000000004` shares.

Sells still use `create_market_order` because the sell path doesn't have the same complement computation issue.

### Gamma API parsing

`clobTokenIds` and `outcomes` fields return as JSON strings, not native JSON. Always parse with `json.loads()`.

### USDC decimals

Balance API returns 6 decimals (1e6). Conditional tokens are ERC-1155 requiring per-token approval.

### VPN/geo restrictions

CLOB API blocks POST `/order` from datacenter/VPN IPs. Resolved by routing through Tor (`proxy.py`). If Tor exit node gets blocked, restart Tor for a new circuit. Don't try to fix with header patching — it doesn't work.

### Order verification timing

`create_order` (limit order) routes through complement engine in 5-15s (slower than market orders). Initial wait is 5s, then 3 rounds of verification at 3s intervals = ~14s total. If still unverified, return `UNVERIFIED_BUY` — never cancel.

---

## Calibration history

### v1-v10 (vol=0.08): 60% WR, -35% ROC

The Brownian motion model with `btc_5min_vol = 0.08` was ~2x overconfident:
- Model said 60-75% → actual WR: 50% (barely better than coin flip)
- Model said 75-85% → actual WR: 40% (WORSE than random)
- Model said 85-100% → actual WR: 83% (roughly calibrated)

A 0.05% BTC move (~$37 on $74K) is noise in a 5-minute window. The old model treated it as "91% confident."

### v13 (vol=0.12): 100% WR on clean data, +55% ROC

Raising vol to 0.12 means a 0.05% move gives ~70% probability (not 91%). Only genuinely significant moves (0.10%+) reach the 80% threshold. Phase 1 of the first v13 session: 6 trades, 6 wins, +$45.73 on $83 deployed.

The 0.15 value was also tested — too conservative, zero trades in 2.5 hours. 0.12 is the sweet spot.

### Safety factor calibration

- 0.70 (Noisy article default for multi-day markets): too aggressive for 5-min markets, zero trades
- 0.85: allows trades when oracle lag creates genuine edge, filters out fully-priced moves
- Works because Polymarket's 5-min market mispricings are 5-15%, not 30-50%

---

## Bug history (resolved)

| Bug | Symptom | Fix | Version |
|-----|---------|-----|---------|
| Ghost orders | Buy "failed" but shares appeared, P&L diverged | Balance-verified buys | v11 |
| Partial fill trap | Sell below $5 minimum → error → shares stranded | Minimum notional guard | v11 |
| P&L tracking drift | Tracked -$6.46, real loss -$15.30 | Window-boundary balance sync | v11 |
| Decimal precision | `invalid amounts, max accuracy 4 decimals` | Integer shares via `create_order` | v10, re-applied v13 |
| Prob-stop destroying value | 4/5 stopped trades won at resolution | All stops removed | v12 |
| Model overconfidence | 60% WR despite "80% confident" signals | Vol recalibrated 0.08→0.12 | v13 |
| Safety factor too tight | Zero trades in 2.5 hours | Raised 0.70→0.85 | v13 |
| `take_profit_pct` crash | `'PolyBot' object has no attribute` | Removed all stop/TP references | v13 |
| CLOB outage losses | $42 lost trading on frozen/stale prices | `get_ok()` circuit breaker | v13 |
| `create_market_order` regression | Decimal precision error returned | Switched back to `create_order` | v13 |

---

## .env reference

```env
# Required
PRIVATE_KEY=0x...
SAFE_ADDRESS=0x...
DRY_RUN=false

# Strategy
MIN_EDGE=0.05
MIN_PROB=0.80
SAFETY_FACTOR=0.85
ENTRY_WINDOW_START=240
ENTRY_WINDOW_END=10
KELLY_FRACTION=0.25
MIN_BET=5.0
MAX_BET=25.0
BANKROLL=100.0

# Safety
DAILY_LOSS_LIMIT=30

# Notifications
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

# Other
MARKET_PERIOD=5
LOG_DIR=logs
```

---

## Working with this codebase

### Preferences

- **Discuss before coding** — JLow prefers to discuss issues before actioning fixes
- **Complete replacement files** — deliver full files, not diffs or partial edits (unless explicitly requested)
- **Forensic debugging** — cross-reference bot terminal logs against Polymarket CSV transaction history trade-by-trade
- **Iterates rapidly** through versioned rewrites when architectural issues are discovered

### Polymarket CSV export

Available from Polymarket UI. Columns: `marketName`, `action` (Buy/Sell/Redeem), `usdcAmount`, `tokenAmount`, `tokenName`, `timestamp` (unix), `hash`. The CSV uses BOM encoding (`utf-8-sig`).

### Key API endpoints

- CLOB API: `https://clob.polymarket.com`
  - `GET /` → health check (`get_ok()`)
  - `GET /time` → server time
  - `POST /order` → place order (requires auth + Tor routing)
- Gamma API: `https://gamma-api.polymarket.com`
  - `GET /markets` → market discovery
- Status page: `https://status.polymarket.com`
- Binance WS: `wss://stream.binance.com:9443/ws/btcusdt@trade`

### Testing

No automated tests yet. Validation is done via:
1. `python -c "import ast; ast.parse(open('file.py').read())"` for syntax
2. Dry run mode (`DRY_RUN=true`)
3. Live run with small bankroll + terminal log review
4. Post-session CSV analysis comparing tracker output to Polymarket history

---

## Open questions / future work

- **Model improvement**: Current model uses one signal (BTC delta from open). Adding momentum (direction of last 30-60s) could filter out bounce-backs that cause losses.
- **VPS deployment**: Running locally means process crash = lost position. A VPS with systemd/pm2 would add resilience.
- **Automated testing**: Backtesting framework against historical 5-min windows would allow rapid strategy iteration without risking capital.
- **Multi-market**: The strategy could theoretically work on ETH, SOL, or other assets' Up/Down markets if they have similar oracle lag.
