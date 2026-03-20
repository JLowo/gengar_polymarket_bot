# PolyBot — Oracle Lag Scalper for Polymarket BTC 5-min Markets

An algorithmic trading bot that exploits the oracle lag between Binance real-time BTC prices and Polymarket's 5-minute BTC Up/Down binary markets.

## How it works

Every 5 minutes, Polymarket opens a binary market: "Will BTC be higher or lower at the end of this window?" The market resolves automatically — winning shares pay $1.00, losing shares pay $0.00.

This bot watches BTC in real-time via Binance WebSocket while Polymarket's prices lag behind. When BTC has moved significantly but the market hasn't fully priced it in, the bot buys the correct side at a discount and holds to resolution.

### The edge

BTC moves on Binance. Polymarket's order book takes seconds to reprice. In that window, you can buy "BTC Up" shares at $0.60 when the true probability is 85%. If BTC stays up, those shares pay $1.00 — a 67% return in under 5 minutes.

## Architecture

```
bot.py                → Main loop, position lifecycle, circuit breakers
├── strategy.py       → Brownian motion probability model + Kelly criterion sizing
├── executor.py       → Polymarket CLOB order execution (balance-verified)
├── market.py         → Active market discovery via Gamma API
├── price_feed.py     → Binance WebSocket for real-time BTC
├── tracker.py        → Quantitative analytics logger (signals, trades, executions)
├── telegram_notifier.py → Mobile alerts + hourly summaries
└── proxy.py          → Tor proxy for CLOB API geo-restrictions
```

## Strategy (v13 — Recalibrated)

### Entry filters (three layers)

1. **Brownian motion model** — estimates true probability that BTC will be higher/lower at window close, based on current delta from opening price and time remaining. Volatility parameter calibrated to `0.12` from 15-trade backtest showing the original `0.08` was ~2x overconfident in the 60-85% probability range.

2. **Minimum probability** — model must output ≥80% confidence before considering entry. Below this, the signal is noise.

3. **Margin of safety** — inspired by value investing: only buy when `market_price ≤ model_probability × 0.85`. Even if the model is 15% wrong, we break even. This filters out situations where the market has already priced in the move.

### Position sizing

Quarter-Kelly criterion. At 80%+ win rate with avg entry $0.68, Kelly sizes $8-20 per trade depending on edge and bankroll. Hard floor at $5 (Polymarket minimum notional), hard cap at $25.

### Exit strategy

No stops. All trades hold to resolution. Data from 15 trades showed that probability-based and price-based stops cost $35+ by panic-selling during normal BTC micro-bounces. The 5-minute window is too short for mean-reversion stops — the edge comes from being right at resolution, not from timing exits.

## Safety systems

### Circuit breaker — CLOB health check

Before every trade, the bot pings `client.get_ok()` — Polymarket's unauthenticated health endpoint. If the CLOB operator isn't responding:
- Skip the trade immediately
- Increment failure counter
- After 3 consecutive failures, halt all trading and send a Telegram alert
- Every new 5-minute window, probe `get_ok()` again — auto-resume when healthy

This prevents trading on stale/frozen market prices during API outages.

### Daily loss limit

Configurable via `DAILY_LOSS_LIMIT` in `.env` (default: $30). Compares session P&L (current balance vs session start) before every trade. If breached, all trading stops for the session with a Telegram alert. Protects against correlated loss streaks.

### Balance-verified everything

- **Buys**: Snapshot USDC before order, verify balance drop after. Ghost fills (order succeeds on-chain despite API exception) are caught via balance change.
- **Sells**: Balance-verified with partial fill tracking.
- **Window sync**: Real USDC balance queried at every window boundary, overwrites internal tracking. Drift > $0.50 is logged.
- **Pending buy safety net**: If a buy can't be verified within 14s, details are saved. Next window boundary detects the fill via balance drop and retroactively tracks the position.

### Minimum notional guard

Polymarket rejects sells below $5 notional. The bot checks before attempting — if remaining shares × price < $5, it holds to resolution instead of hitting the error. Winning shares auto-resolve at $1.00.

## Quick start

```bash
# Clone
git clone https://github.com/JLowo/gengar_polymarket_bot.git
cd gengar_polymarket_bot

# Install dependencies
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with your credentials

# Paper trading (safe)
python bot.py

# Live trading (set DRY_RUN=false in .env)
```

## Configuration (.env)

### Required

| Variable | Description |
|----------|-------------|
| `PRIVATE_KEY` | Wallet private key (0x prefixed) |
| `SAFE_ADDRESS` | Polymarket Safe/proxy address |
| `DRY_RUN` | `true` = paper trade, `false` = real money |

### Strategy

| Variable | Default | Description |
|----------|---------|-------------|
| `MIN_EDGE` | `0.05` | Minimum edge (prob - price) to enter |
| `MIN_PROB` | `0.80` | Minimum model probability |
| `SAFETY_FACTOR` | `0.85` | Only buy at price ≤ prob × this |
| `ENTRY_WINDOW_START` | `240` | Start evaluating at T-240s |
| `ENTRY_WINDOW_END` | `10` | Stop evaluating at T-10s |
| `KELLY_FRACTION` | `0.25` | Quarter-Kelly (conservative) |
| `MIN_BET` | `5.0` | Polymarket minimum notional |
| `MAX_BET` | `25.0` | Hard cap per trade |
| `BANKROLL` | `100.0` | Starting bankroll for Kelly sizing |

### Safety

| Variable | Default | Description |
|----------|---------|-------------|
| `DAILY_LOSS_LIMIT` | `30.0` | Stop trading if session P&L drops below -$30 |

### Notifications

| Variable | Description |
|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | From @BotFather |
| `TELEGRAM_CHAT_ID` | Your numeric Telegram user ID |

### Other

| Variable | Default | Description |
|----------|---------|-------------|
| `MARKET_PERIOD` | `5` | Market window in minutes |
| `LOG_DIR` | `logs` | Directory for tracker CSVs |

## Tracker output

The bot logs three CSV files in `LOG_DIR/` for post-session analysis:

- **signals.csv** — every signal evaluated (traded or skipped), with model probability, market price, edge, Kelly size, and reason for action
- **trades.csv** — full trade lifecycle: entry, hold-period extremes (min/max prob and sell price), exit, resolution outcome, and `profit_if_held` counterfactual
- **executions.csv** — every CLOB API call with latency

## Lessons learned

These are architectural decisions made from live trading data, not theory:

- **Two-book architecture**: Polymarket has a raw token book (illiquid, wide spreads) and a complement engine book (tight spreads, real volume). All orders must route through the complement engine — the raw book is effectively unusable.
- **Ghost orders are real fills**: Network exceptions on buys don't mean the order failed. On-chain fills can occur silently. Always verify via balance change, never trust the API response alone.
- **Float precision kills orders**: `py-clob-client` internally computes `shares × (1 - price)` using float math. `1.0 - 0.71 = 0.29000000000000004` in IEEE 754. The bot uses `create_order` with explicit integer shares to avoid this.
- **Stops destroy value on 5-min windows**: Prob-stop fired 5 times in testing. 4 of 5 stopped trades won at resolution. Cost: $35.45. BTC micro-bounces trigger panic sells that the 5-minute trend ultimately reverses.
- **Model calibration matters more than strategy complexity**: Doubling the vol parameter from 0.08 to 0.12 (based on 15 trades of calibration data) turned a -35% ROC strategy into a +55% ROC strategy. Same code, different constant.

## Known limitations

- **Ghost fills during outages**: When the CLOB confirmation pipeline is down, orders can still execute on-chain. The `get_ok()` circuit breaker prevents *new* orders, but can't recall orders already submitted.
- **Tor latency**: Routing through Tor adds 200-500ms per API call. The total buy flow is 8-15 seconds. This is acceptable for 5-minute windows but would be a problem for faster markets.
- **Stranded shares**: Winning positions below $5 notional can't be sold via the API. They auto-resolve on Polymarket but the USDC may take time to appear.

## Risk warning

This bot trades real money on volatile 5-minute binary markets. Each trade is an all-or-nothing bet — shares pay $1 or $0. Even with an 80% win rate, a run of losses is inevitable over hundreds of trades.

Start with `DRY_RUN=true`. When going live, use money you can afford to lose entirely.

## Status monitoring

Polymarket publishes API status at [status.polymarket.com](https://status.polymarket.com). Subscribe to webhook notifications for outage alerts.
