# PolyBot Setup Guide

This guide walks you through setting up PolyBot from scratch — a trading bot for Polymarket's 5-minute BTC Up/Down binary markets.

**Time to complete:** ~60–90 minutes
**Prerequisites:** A computer running macOS or Linux, basic comfort with a terminal

---

## What you'll need

- A Polymarket account funded with USDC
- A Polygon wallet (private key)
- Python 3.10+
- Tor (for geo-restriction bypass)
- A Telegram bot (optional but strongly recommended for mobile alerts)

---

## Step 1 — Clone the repo

```bash
git clone <repo-url>
cd gengar_polybot
```

---

## Step 2 — Python environment

**Check your Python version** (must be 3.10 or higher):

```bash
python3 --version
```

**Create a virtual environment and activate it:**

```bash
python3 -m venv venv
source venv/bin/activate   # macOS/Linux
# or on Windows: venv\Scripts\activate
```

**Install dependencies:**

```bash
pip install -r requirements.txt
```

The key packages this installs:
- `py-clob-client` — Polymarket CLOB SDK for order execution
- `python-dotenv` — reads your `.env` config file
- `websockets` + `aiohttp` — Binance real-time price feed
- `httpx[socks]` + `pysocks` — Tor SOCKS5 proxy routing

---

## Step 3 — Set up Tor

Polymarket's CLOB API (the order placement endpoint) blocks requests from many countries and datacenter/VPN IPs. The bot routes all trading traffic through Tor to bypass this. **This is not optional** — without it, order placement will fail silently or be rejected.

**Install Tor:**

```bash
# macOS
brew install tor

# Ubuntu/Debian
sudo apt install tor
```

**Do not start Tor manually.** The bot manages its own Tor process automatically at startup, including writing a custom `torrc` that pins exit nodes to Polymarket-friendly countries (Switzerland, Sweden, Romania, etc.).

When you first run the bot, you'll see log lines like:
```
Starting Tor — waiting for full bootstrap (this may take ~60s)...
Tor bootstrap: 10%
Tor bootstrap: 50%
...
Tor bootstrap: 100%
Tor ready — circuits built, proxy active on port 9050
```

If Tor bootstrap fails or trading starts getting rejected, just restart the bot — it will spin up a new Tor circuit with a different exit node.

---

## Step 4 — Polymarket wallet setup

You need a Polygon wallet with USDC deposited on Polymarket. There are two wallet configurations:

### Option A: Standard EOA wallet (simpler)

Use your wallet's private key directly. Leave `SAFE_ADDRESS` blank in `.env`.

### Option B: Polymarket Safe wallet (used by the original bot)

Polymarket creates a Safe (multi-sig proxy) for each account. If you use the Polymarket UI normally, you likely have one. You must make at least one trade on Polymarket first (even a small one) for the Safe to be created.

**To find your proxy wallet address:**

1. Go to [polymarket.com](https://polymarket.com) and log in
2. Open browser dev tools (Cmd+Option+I on Mac, F12 on Windows)
3. Go to **Application** tab → **Local Storage** → click `https://polymarket.com`
4. Look for a key containing `proxyWallet` — the value will look like:
   ```
   "proxyWallet": "0x94a42c9be304a39703ec24fae8bd448f07601301"
   ```
5. Copy that address — this is your `SAFE_ADDRESS`

**Alternative method:** In dev tools, go to the **Network** tab, filter requests, and look for API calls containing your wallet address — the Safe address appears as `proxyWallet` in responses.

If using a Safe, set `SAFE_ADDRESS` in `.env` to that address and `signature_type` in the code will automatically be set to `2`.

### Depositing funds

1. Go to [polymarket.com](https://polymarket.com)
2. Connect your wallet
3. Deposit USDC via the UI (bridges from Ethereum or direct on Polygon)
4. Recommended starting balance: **$50–$100** to start

> **Note:** The bot tracks bankroll internally. Set `BANKROLL` in `.env` to match your actual USDC balance on Polymarket when you start.

---

## Step 5 — Telegram bot setup (recommended)

The bot sends trade alerts and hourly summaries to Telegram. Without it you're flying blind when the bot is running in the background.

1. Open Telegram and message `@BotFather`
2. Send `/newbot` and follow the prompts to create a bot
3. Copy the **bot token** (looks like `1234567890:ABCdef...`)
4. Start a chat with your new bot (search for it by username)
5. Get your **chat ID**: message `@userinfobot` — it will reply with your numeric chat ID

You'll put both values in `.env` in the next step.

If you skip Telegram, the bot still works — it just prints everything to the terminal only.

---

## Step 6 — Create your `.env` file

Copy the example and fill in your values:

```bash
cp .env.example .env   # if an example exists
# or create it from scratch:
touch .env
```

Open `.env` in a text editor and fill in:

```env
# ── Wallet ───────────────────────────────────────────────────────────
PRIVATE_KEY=0x<your-private-key>
SAFE_ADDRESS=0x<your-polymarket-safe-address>   # leave blank if not using Safe

# ── Mode ─────────────────────────────────────────────────────────────
DRY_RUN=true   # set to false when ready to trade real money

# ── Strategy parameters ──────────────────────────────────────────────
MIN_EDGE=0.05
MIN_PROB=0.80
SAFETY_FACTOR=0.85
ENTRY_WINDOW_START=240
ENTRY_WINDOW_END=10
KELLY_FRACTION=0.25
MIN_BET=5.0
MAX_BET=25.0
BANKROLL=100.0   # set this to your actual USDC balance on Polymarket

# ── Safety ───────────────────────────────────────────────────────────
DAILY_LOSS_LIMIT=30   # bot halts trading if session P&L drops below -$30

# ── Notifications ────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN=<your-bot-token>
TELEGRAM_CHAT_ID=<your-chat-id>

# ── Other ────────────────────────────────────────────────────────────
MARKET_PERIOD=5
LOG_DIR=logs
```

### Parameter guide

| Parameter | What it does | Default | Notes |
|-----------|-------------|---------|-------|
| `MIN_PROB` | Model must be this confident to trade | 0.80 | Don't lower below 0.75 |
| `SAFETY_FACTOR` | Market price must be ≤ true_prob × this | 0.85 | Filters fully-priced moves |
| `KELLY_FRACTION` | Fraction of Kelly criterion to bet | 0.25 | Quarter-Kelly = conservative |
| `MIN_BET` | Minimum bet size in USD | 5.0 | Polymarket min order is $5 |
| `MAX_BET` | Maximum bet size in USD | 25.0 | Hard cap regardless of Kelly |
| `BANKROLL` | Current trading capital | 100.0 | Update to your real balance |
| `DAILY_LOSS_LIMIT` | Stop trading after losing this much in a session | 30 | Circuit breaker |
| `ENTRY_WINDOW_START` | Seconds before window close to start looking for entries | 240 | 4 minutes into a 5-min window |
| `ENTRY_WINDOW_END` | Seconds before close to stop entering | 10 | Don't enter in final 10s |

> **Security:** Never commit `.env` to git. Your private key grants full control of your wallet.

---

## Step 7 — Verify the setup (syntax check)

Before running anything, check all files parse correctly:

```bash
python -c "import ast; ast.parse(open('bot.py').read())" && echo "OK"
python -c "import ast; ast.parse(open('executor.py').read())" && echo "OK"
python -c "import ast; ast.parse(open('strategy.py').read())" && echo "OK"
```

---

## Step 8 — Run in dry-run mode first

With `DRY_RUN=true` in `.env`, the bot simulates trades without spending real money. Run it for at least one full session to verify everything connects correctly.

```bash
python bot.py
```

You should see startup output like:

```
[proxy] Starting Tor — waiting for full bootstrap...
[proxy] Tor ready (pid 12345)
[executor] Initialized (DRY RUN)
[executor] Address: 0x...
[market] Found market: Will BTC be above $84,250 at 3:05 PM?
[price_feed] Connected to Binance WebSocket
PolyBot running — press Ctrl+C to stop
```

The bot will then wait for trading opportunities, printing each 5-minute window evaluation.

**What to look for in dry-run:**
- Tor bootstraps successfully (or errors with a clear message)
- Executor initializes and shows your wallet address
- Market discovery finds the current BTC Up/Down market
- Binance price feed connects and streams prices
- When a signal fires, you see `[DRY RUN]` trade output

---

## Step 9 — Go live

When dry-run looks healthy for a session or two:

1. Set `DRY_RUN=false` in `.env`
2. Confirm `BANKROLL` matches your actual Polymarket USDC balance
3. Set `DAILY_LOSS_LIMIT` to a number you're comfortable losing in one day
4. Run:

```bash
python bot.py
```

---

## Understanding the logs

The bot creates a `logs/` directory with three CSV files:

| File | Contents |
|------|----------|
| `signals.csv` | Every market evaluation — price, probability, edge, decision |
| `trades.csv` | Every trade taken — entry, exit, P&L |
| `executions.csv` | Raw order details — order IDs, fills, balances |

These are your primary audit trail. After each session, compare `trades.csv` against your Polymarket transaction CSV (exported from the UI) to verify P&L tracking is accurate.

**Downloading your Polymarket CSV:**
1. Go to your Polymarket profile
2. Click the activity/history section
3. Export to CSV
4. The file uses BOM encoding (`utf-8-sig`) — this is normal

---

## Circuit breakers and safety systems

The bot has several automatic safeguards:

**Daily loss limit** — If your session P&L drops below `-DAILY_LOSS_LIMIT`, the bot halts and sends a Telegram alert. You must restart it manually to resume.

**CLOB health check** — Before every trade, the bot pings Polymarket's health endpoint. Three consecutive failures → trading halts automatically. It auto-recovers when the API comes back.

**Pending buy safety net** — If an order can't be verified within ~14 seconds, it's flagged as `UNVERIFIED_BUY`. At the next window boundary, the bot checks your real balance — if it dropped, the order is retroactively tracked as filled. Orders are **never cancelled** on timeout.

**Window-boundary balance sync** — Every 5 minutes, the bot queries your real USDC balance and overwrites internal tracking. This corrects any accumulated drift.

---

## Troubleshooting

### Tor won't bootstrap
```
RuntimeError: Tor did not finish bootstrapping within 120s
```
- Try running `tor` manually to see error output
- On macOS, check: `brew info tor`
- Try: `brew services stop tor` first (in case a brew-managed instance is running)

### "invalid amounts, max accuracy of 4 decimals"
This is the float precision bug. Make sure you're on the latest `bot.py` — the fix uses `create_order(OrderArgs(...))` with integer shares, not `create_market_order`.

### Orders not filling / always getting rejected
- The CLOB API may be blocking your Tor exit node — restart the bot to get a new circuit
- Check `https://status.polymarket.com` for outages
- Verify your USDC balance is sufficient (minimum $5 per order)

### No trades firing in dry-run
This is normal if BTC isn't moving significantly. The model requires an 80% probability signal, which only fires on genuine moves (0.10%+ within a 5-minute window). You may watch several empty windows before seeing a signal.

### "CLOB halted" alert on Telegram
Three consecutive order failures triggered the circuit breaker. Check:
1. Polymarket status page for outages
2. Your Tor connection (restart bot to get new circuit)
3. Your wallet balance

---

## Running continuously (optional)

For unattended operation, use a process manager. On macOS:

```bash
# Simple: run in a tmux session so it survives terminal close
tmux new -s polybot
python bot.py
# Detach with Ctrl+B then D
# Reattach with: tmux attach -t polybot
```

On Linux with systemd (more robust):

```ini
# /etc/systemd/system/polybot.service
[Unit]
Description=PolyBot Trading Bot

[Service]
WorkingDirectory=/path/to/gengar_polybot
ExecStart=/path/to/venv/bin/python bot.py
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

> **Note:** If the process crashes while holding an open position, the position will resolve at the 5-minute window end without a managed exit. The bot will reconcile the balance on next startup via window-boundary balance sync.

---

## Key facts to understand before going live

1. **This trades real USDC on Polygon.** Start with a small bankroll ($50–$100) and observe several sessions.

2. **The strategy holds all positions to resolution** (no stops, no take-profits). This is intentional — data showed stops cost more than they saved in 5-minute windows.

3. **A 5-minute BTC move of 0.10%+ is required** for the model to fire at 80% confidence. Slow markets = zero trades. That's correct behavior.

4. **The bot requires an active internet connection** and Tor running. If your connection drops mid-position, the position holds until resolution automatically.

5. **Geo-restrictions are real.** Even if you're in an allowed country, datacenter/VPN IPs get blocked. Tor is the solution.
