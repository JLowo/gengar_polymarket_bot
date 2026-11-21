# PolyBot Dashboard — Chart & Metric Guide

How to read each metric and chart to decide which filter settings produce the best results.

## Stat Cards

### Total Trades
- **What**: Count of executed trades, with W/L breakdown.
- **Why it matters**: More trades = more data = more confidence in the numbers. Below ~30 trades, any metric could be noise. Compare across versions to see if tighter filters reduce volume too much.

### Win Rate
- **What**: Wins / Total Trades as a percentage.
- **Why it matters**: In binary markets (shares pay $1 or $0), you need roughly **~70% win rate to break even** at typical entry prices ($0.60-$0.80). Below 70%, you're losing money even if individual wins feel good. The breakeven threshold depends on your average entry price — lower entry prices (more edge) lower the breakeven WR.
- **Formula**: `win_rate = wins / total_trades × 100`

### Total P&L
- **What**: Sum of all trade profits (wins minus losses).
- **Why it matters**: The bottom line. But don't compare raw P&L across versions with different trade counts — use EV/Trade instead.

### EV / Trade (Expected Value per Trade)
- **What**: Average profit per trade, including both wins and losses.
- **Why it matters**: The single best metric for comparing versions. A version with fewer trades but higher EV/Trade is better — it means every trade you take is worth more. If EV/Trade is negative, you're losing money on average and no amount of volume fixes that.
- **Formula**: `ev_per_trade = total_pnl / total_trades`
- **Decision rule**: Higher EV/Trade = better filter settings. Even if trade count drops.

### Avg Win
- **What**: Average profit on winning trades only.
- **Why it matters**: Shows how much you make when you're right. In binary markets this is `(1 - entry_price) × shares`. Lower entry prices = bigger wins. If tighter filters force you into higher-priced entries (less edge), avg win shrinks.
- **Formula**: `avg_win = sum(profit for wins) / count(wins)`

### Avg Loss
- **What**: Average loss on losing trades (shown as positive number for readability).
- **Why it matters**: In binary markets with hold-to-resolution, every loss is -100% of entry cost. So avg loss ≈ avg entry cost on losing trades. The "X wins to recover" subtitle tells you how many winning trades it takes to make back one loss. If this number is >3, your losses are too expensive relative to your wins.
- **Formula**: `avg_loss = |sum(profit for losses)| / count(losses)`
- **Recovery ratio**: `wins_to_recover = avg_loss / avg_win`

### Profit Factor
- **What**: Total dollars won / Total dollars lost.
- **Why it matters**: The efficiency of your edge. Profit factor > 1.0 means you're profitable. > 2.0 is strong. > 3.0 is exceptional. Unlike win rate, this accounts for *how much* you win vs lose, not just how often. A version with 70% WR but profit factor 3.0 is better than 90% WR with profit factor 1.5.
- **Formula**: `profit_factor = sum(profits on wins) / |sum(profits on losses)|`
- **Decision rule**: Compare profit factor across versions. Higher = more efficient edge.

### Near Misses
- **What**: Windows where the strategy gave a signal and the bot tried to buy but didn't, with how many would have won and what they'd have made in total if taken (at the price the bot would have paid, before fees).
- **Reasons**: *price moved too far* (`edge_gone_at_market`: the book price the bot checked left less than MIN_EDGE) and *attempt failed (no reason logged)* (the buy didn't happen and the bot didn't record why, e.g. no fill or a failed balance read).
- **Results**: Polymarket's own resolution, looked up on Gamma when the dashboard loads and saved in `logs/outcomes.json`, so each window is only fetched once. A window not resolved yet shows as *pending*.
- **Why it matters**: If most near misses would have won *and* the P&L if taken is positive, the pre-trade check may be too strict. Look at the P&L, not just the win count: near misses are usually at high prices (80–90¢), where one loss wipes out several wins.

### Quiet Windows
- **What**: Windows with no signal at all. The bot logs these at the window's last second, when the market has already settled, so their logged skip reason (usually "price out of range") says nothing about the entry window and isn't shown.

---

## Charts

### Equity Curve
- **What**: Cumulative P&L over time, with each trade as a point. Color-coded by version, green/red dots for win/loss.
- **Why it matters**: Shows the *trajectory* of your bankroll. A smooth upward curve = consistent edge. Sharp drops = losing streaks. Compare the slope across version segments — steeper upward slope = better version. Also reveals if a version started strong then degraded (market regime change).
- **What to look for**:
  - Steady upward slope (good)
  - Flat or declining segments (filter settings not working for current market)
  - Big drops from single losses (motivates tighter filters)

### Win Rate by Version
- **What**: Bar chart comparing win rate across v13, v14, v15.
- **Why it matters**: Direct comparison of how accurate each version is. The dashed line at 70% marks approximate breakeven. Versions above the line are profitable on win rate alone. But win rate isn't everything — a version could have high WR with tiny wins and still underperform.
- **What to look for**: Versions consistently above 70% line. If a version is below, its filters or model calibration need work.

### Total P&L by Version
- **What**: Bar chart showing total dollars made/lost per version.
- **Why it matters**: The final scoreboard. But misleading if versions ran for different durations. A version that ran for 2 hours and made $20 is better than one that ran for 24 hours and made $30. Cross-reference with trade count.

### Per-Trade P&L Waterfall
- **What**: Every trade as a green (win) or red (loss) bar, in chronological order.
- **Why it matters**: Visualizes loss clustering. If you see red bars grouped together, that's a losing streak — check what market conditions caused it (time of day, BTC volatility). Also shows the magnitude asymmetry: red bars are typically taller than green bars because losses are -100% of entry cost while wins are +(1/price - 1) × cost.
- **What to look for**:
  - Tall red bars = expensive losses (high entry cost on wrong trades)
  - Clusters of red = regime where the strategy fails
  - Version boundaries = did the new version improve the pattern?

### Edge vs Outcome (Scatter)
- **What**: Every trade plotted with edge (prob - price) on X-axis and profit on Y-axis. Wins are green circles, losses are red X markers.
- **Why it matters**: **The most important chart for choosing min_edge.** Visually shows where losses cluster on the edge spectrum. If all losses are below edge 0.08, that's your filter threshold. If losses are spread evenly, edge isn't predictive and you need a different filter.
- **How to read it**:
  - Losses clustered at low edge → raise min_edge to that threshold
  - Losses at high edge → edge isn't the problem; look at btc_delta or time_remaining instead
  - Clear separation between win zone and loss zone → strong filter signal
- **Decision rule**: Draw a mental vertical line where losses stop appearing. That's your optimal min_edge.

### Avg Win / Avg Loss by Version
- **What**: Grouped bar chart showing average win (green) and average loss (red) side by side for each version. Tooltip shows profit factor.
- **Why it matters**: Tells the "recovery story" per version. If the red bar is 2x the green bar, each loss erases 2 wins — you need 75%+ WR just to break even. The ideal pattern is green bar growing (bigger wins) while red bar shrinks (smaller or fewer losses). This is how you measure whether filter changes are actually improving the economics.
- **What to look for**:
  - Green bar > red bar (each win covers each loss — only need >50% WR)
  - Red bar >> green bar (dangerous — need very high WR to survive)
  - Profit factor in tooltip: >2.0 is strong, >3.0 is excellent

### Edge: Seen vs At Fill
- **What**: For each trade, the edge the bot saw when it decided (model probability − its cached book price, up to 5s old) next to the edge it actually got (probability − fill price). The title shows both averages and how often the fill was dearer than the price seen.
- **Why it matters**: The logged edge (`edge_at_entry`, and `avg_edge` in sessions.csv) uses the price the bot saw, so it overstates the edge whenever the market moved first. A big, consistent gap means the bot's prices are too stale to compete with the market's repricing: faster prices would help more than a different MIN_EDGE.

### Near Misses by Reason
- **What**: Bar chart of near misses per reason, split into would have won / would have lost / pending.
- **Why it matters**: Tells you which check is helping and which is costing money.
  - **price moved too far**: If mostly would-have-won *and* positive P&L if taken → the check may be too strict, or the bot needs faster execution. If the P&L if taken is negative even with mostly wins, the check is doing its job (those entries were too expensive).
  - **attempt failed (no reason logged)**: Should be rare. Check the bot's terminal output for that window.

### Near Misses: Would-Have-Won Rate (Pie)
- **What**: Doughnut chart of near misses that would have won vs lost (pending ones left out).
- **Why it matters**: Quick gut check, but read it next to the P&L if taken: at 85¢ you need about 6 wins per loss to break even.

### Near Misses Table
- **What**: The last 30 near misses: model probability, BTC move, time left, the price the bot saw, the price it would have paid (the book price it checked; "~" means it didn't get that far, so this is the price it saw), Polymarket's result and the P&L if taken on the stake it had sized.

### Recent Trades Table
- **What**: Last 20 trades with time, version, side, entry price, edge %, cost, shares, result, profit, and return %.
- **Why it matters**: Quick scan for patterns. Look at the Edge % column alongside Result — do all your losses have low edge? That confirms the min_edge filter thesis. Also useful for spotting entry price patterns (are you buying too expensive on losses?).

### Sessions Table
- **What**: One row per bot run from `sessions.csv` (written when the bot stops, so a running session isn't there yet): length, balance at start and end, real P&L (from the wallet), tracked P&L (the bot's own sum of trades), the drift between them, trades and win rate. Not affected by the version filter.
- **Why it matters**: Drift should be within a few cents. A drift of $0.50 or more (shown red) means the bot's bookkeeping and the wallet disagree: compare that session's trades against Polymarket's history.

## Refreshing

The page re-reads the logs every 30 seconds while **Auto-refresh** is ticked (remembered per browser) and redraws only when something changed, keeping the version filter and any open trade detail. **Refresh** redraws straight away.

---

## How to Compare Versions

Use this checklist when deciding which filter settings to keep:

1. **EV/Trade** — Higher is better. The primary metric.
2. **Profit Factor** — Higher is better. Above 2.0 is strong.
3. **Win Rate** — Higher is better, but only matters relative to avg entry price.
4. **Wins to Recover** — Lower is better. Below 2.0 means each loss only costs ~2 wins.
5. **Edge scatter** — Losses should cluster at low edge (filterable), not spread randomly.
6. **Near misses** — P&L if taken near zero or negative means the pre-trade check is earning its keep; clearly positive means it's too strict.
7. **Trade volume** — More is better *if EV/Trade stays positive*. Don't sacrifice EV for volume.

### The Binary Market Rule

In binary markets where every loss is -100%, the math is asymmetric:
- Losing a $7.70 trade costs you $7.70
- Missing a $3.89 win only costs you $3.89
- **The cost of a false positive (bad trade) is ~2x the cost of a false negative (missed trade)**

This means **accuracy > volume** for binary markets. When in doubt, be more selective.
