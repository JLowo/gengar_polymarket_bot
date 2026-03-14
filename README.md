# PolyBot — Oracle Lag Scalper

A Python trading bot for Polymarket's 5-minute BTC Up/Down markets.

## How it works

Every 5 minutes, Polymarket opens a market asking: "Will BTC be higher or lower at the end of this window?" 

This bot exploits the **oracle lag** — the delay between when BTC moves on Binance (real-time) and when Polymarket's prices catch up. It watches BTC via Binance WebSocket, compares to the window's opening price, and places maker limit orders when the market hasn't priced in a significant move.

## Quick Start

```bash
# 1. Clone or download this folder
cd polybot

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
# Edit .env with your credentials (see below)

# 4. Run in dry-run mode (paper trading)
python bot.py

# 5. When ready for real trades, set DRY_RUN=false in .env
```

## Configuration (.env)

| Variable | Description |
|----------|-------------|
| `PRIVATE_KEY` | Your wallet private key (0x prefixed) |
| `SAFE_ADDRESS` | Your Polymarket Safe address (from polymarket.com/settings) |
| `TELEGRAM_BOT_TOKEN` | From @BotFather on Telegram |
| `TELEGRAM_CHAT_ID` | Your numeric Telegram user ID |
| `TRADE_AMOUNT` | USD per trade (default: 5.0) |
| `MIN_EDGE` | Minimum edge to enter (default: 0.03 = 3%) |
| `DRY_RUN` | true = paper trade, false = real money |

## Architecture

```
bot.py              → Main loop (1 tick/second)
├── price_feed.py   → Binance WebSocket for real-time BTC price
├── market.py       → Discovers active 5-min market + token IDs
├── strategy.py     → Decision engine (compare BTC delta vs market price)
├── executor.py     → Places orders via py-clob-client
└── telegram_notifier.py → Mobile alerts
```

## Strategy

1. At each tick, calculate BTC's move from window open
2. Estimate true probability using a Brownian motion model
3. Compare to Polymarket's current implied probability
4. If edge > minimum threshold AND we're in the entry window (T-60s to T-10s), buy the correct side
5. Use maker limit orders (0% fee after Feb 2026 changes)

## Risk Warning

This bot trades real money on volatile markets. Start with DRY_RUN=true. 
When going live, use small amounts you can afford to lose entirely.
Past performance of any strategy does not guarantee future results.
