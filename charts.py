#!/usr/bin/env python3
"""Generate trading performance charts across all PolyBot versions."""

import csv
import os
from datetime import datetime

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np

LOG_DIR = "logs"
OUT_DIR = "charts"
os.makedirs(OUT_DIR, exist_ok=True)

# ── Load data ──────────────────────────────────────────────────────

def load_trades(path):
    trades = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            trades.append({
                "timestamp": float(row["timestamp"]),
                "side": row["side"],
                "entry_price": float(row["entry_price"]),
                "entry_cost": float(row["entry_cost"]),
                "entry_shares": float(row["entry_shares"]),
                "profit": float(row["profit"]),
                "return_pct": float(row["return_pct"]),
                "won": row["won_resolution"] == "True",
                "btc_delta": float(row.get("btc_delta_at_entry", 0)),
                "edge": float(row.get("edge_at_entry", 0)),
                "slippage": float(row.get("slippage", 0)),
            })
    return trades

def load_skipped(path):
    skipped = []
    if not os.path.exists(path):
        return skipped
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            skipped.append({
                "timestamp": float(row["timestamp"]),
                "side": row["side"],
                "prob": float(row["prob"]),
                "btc_delta": float(row["btc_delta_pct"]),
                "market_price": float(row["market_price"]),
                "reason": row["reason"],
                "would_have_won": row["would_have_won"] == "True",
            })
    return skipped

v13 = load_trades(os.path.join(LOG_DIR, "trades_v13_pre_fix.csv"))
v14 = load_trades(os.path.join(LOG_DIR, "trades_v14.csv"))
v15 = load_trades(os.path.join(LOG_DIR, "trades.csv"))
skipped = load_skipped(os.path.join(LOG_DIR, "skipped.csv"))

# ── Style ──────────────────────────────────────────────────────────

plt.style.use("dark_background")
COLORS = {"v13": "#ff6b6b", "v14": "#ffd93d", "v15": "#6bcb77"}
GREEN = "#6bcb77"
RED = "#ff6b6b"
GOLD = "#ffd93d"
BLUE = "#4ecdc4"

# ── Chart 1: Equity Curves ────────────────────────────────────────

fig, ax = plt.subplots(figsize=(14, 6))

for label, trades, color in [("v13", v13, COLORS["v13"]), ("v14", v14, COLORS["v14"]), ("v15", v15, COLORS["v15"])]:
    if not trades:
        continue
    cum_pnl = [0]
    trade_nums = [0]
    for i, t in enumerate(trades):
        cum_pnl.append(cum_pnl[-1] + t["profit"])
        trade_nums.append(i + 1)
    ax.plot(trade_nums, cum_pnl, color=color, linewidth=2.5, label=label, marker="o", markersize=5)
    # Shade wins/losses
    for i, t in enumerate(trades):
        ax.scatter(i + 1, cum_pnl[i + 1],
                   color=GREEN if t["won"] else RED,
                   s=60, zorder=5, edgecolors="white", linewidths=0.5)

ax.axhline(y=0, color="gray", linestyle="--", alpha=0.5)
ax.set_xlabel("Trade #", fontsize=12)
ax.set_ylabel("Cumulative P&L ($)", fontsize=12)
ax.set_title("Equity Curve by Version", fontsize=16, fontweight="bold")
ax.legend(fontsize=12)
ax.grid(alpha=0.2)
fig.tight_layout()
fig.savefig(os.path.join(OUT_DIR, "1_equity_curve.png"), dpi=150)
print(f"  Saved 1_equity_curve.png")

# ── Chart 2: Win Rate + P&L by Version ────────────────────────────

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))

versions = []
win_rates = []
total_pnls = []
trade_counts = []
colors_list = []

for label, trades, color in [("v13", v13, COLORS["v13"]), ("v14", v14, COLORS["v14"]), ("v15", v15, COLORS["v15"])]:
    if not trades:
        continue
    wins = sum(1 for t in trades if t["won"])
    versions.append(label)
    win_rates.append(wins / len(trades) * 100)
    total_pnls.append(sum(t["profit"] for t in trades))
    trade_counts.append(len(trades))
    colors_list.append(color)

bars = ax1.bar(versions, win_rates, color=colors_list, alpha=0.85, edgecolor="white", linewidth=0.5)
for bar, wr, n in zip(bars, win_rates, trade_counts):
    ax1.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 1,
             f"{wr:.0f}%\n({n} trades)", ha="center", fontsize=11, fontweight="bold")
ax1.set_ylabel("Win Rate (%)", fontsize=12)
ax1.set_title("Win Rate by Version", fontsize=14, fontweight="bold")
ax1.set_ylim(0, 110)
ax1.axhline(y=70, color="gray", linestyle="--", alpha=0.4, label="~Breakeven")
ax1.legend(fontsize=9)
ax1.grid(alpha=0.2, axis="y")

bar_colors = [GREEN if p > 0 else RED for p in total_pnls]
bars2 = ax2.bar(versions, total_pnls, color=bar_colors, alpha=0.85, edgecolor="white", linewidth=0.5)
for bar, pnl in zip(bars2, total_pnls):
    ax2.text(bar.get_x() + bar.get_width() / 2,
             bar.get_height() + (1 if pnl >= 0 else -3),
             f"${pnl:+.2f}", ha="center", fontsize=11, fontweight="bold")
ax2.set_ylabel("Total P&L ($)", fontsize=12)
ax2.set_title("Total P&L by Version", fontsize=14, fontweight="bold")
ax2.axhline(y=0, color="gray", linestyle="--", alpha=0.5)
ax2.grid(alpha=0.2, axis="y")

fig.tight_layout()
fig.savefig(os.path.join(OUT_DIR, "2_winrate_pnl.png"), dpi=150)
print(f"  Saved 2_winrate_pnl.png")

# ── Chart 3: Trade Returns Distribution ────────────────────────────

fig, ax = plt.subplots(figsize=(14, 6))

all_versions = [("v13", v13, COLORS["v13"]), ("v14", v14, COLORS["v14"]), ("v15", v15, COLORS["v15"])]
all_versions = [(l, t, c) for l, t, c in all_versions if t]

for label, trades, color in all_versions:
    returns = [t["return_pct"] for t in trades]
    ax.hist(returns, bins=15, alpha=0.5, color=color, label=label, edgecolor="white", linewidth=0.5)

ax.axvline(x=0, color="white", linestyle="--", alpha=0.5)
ax.set_xlabel("Return per Trade (%)", fontsize=12)
ax.set_ylabel("Count", fontsize=12)
ax.set_title("Trade Return Distribution", fontsize=16, fontweight="bold")
ax.legend(fontsize=12)
ax.grid(alpha=0.2)
fig.tight_layout()
fig.savefig(os.path.join(OUT_DIR, "3_return_distribution.png"), dpi=150)
print(f"  Saved 3_return_distribution.png")

# ── Chart 4: Per-Trade Waterfall ───────────────────────────────────

fig, ax = plt.subplots(figsize=(14, 6))

# Combine all trades chronologically
all_trades = []
for label, trades in [("v13", v13), ("v14", v14), ("v15", v15)]:
    for t in trades:
        t["version"] = label
        all_trades.append(t)
all_trades.sort(key=lambda t: t["timestamp"])

profits = [t["profit"] for t in all_trades]
colors_bar = [GREEN if p > 0 else RED for p in profits]
version_colors = [COLORS[t["version"]] for t in all_trades]

x = range(1, len(all_trades) + 1)
bars = ax.bar(x, profits, color=colors_bar, alpha=0.85, edgecolor="white", linewidth=0.3)

# Add version labels on x-axis
prev_v = ""
for i, t in enumerate(all_trades):
    if t["version"] != prev_v:
        ax.axvline(x=i + 0.5, color="white", linestyle=":", alpha=0.3)
        ax.text(i + 1, max(profits) * 0.95, t["version"],
                fontsize=9, color=COLORS[t["version"]], fontweight="bold", ha="left")
        prev_v = t["version"]

ax.axhline(y=0, color="gray", linestyle="--", alpha=0.5)
ax.set_xlabel("Trade # (chronological)", fontsize=12)
ax.set_ylabel("Profit ($)", fontsize=12)
ax.set_title("Per-Trade P&L Waterfall (All Versions)", fontsize=16, fontweight="bold")
ax.grid(alpha=0.2, axis="y")
fig.tight_layout()
fig.savefig(os.path.join(OUT_DIR, "4_waterfall.png"), dpi=150)
print(f"  Saved 4_waterfall.png")

# ── Chart 5: Skipped Signals Analysis ─────────────────────────────

if skipped:
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))

    # By reason: would have won vs lost
    reasons = sorted(set(s["reason"] for s in skipped))
    won_counts = []
    lost_counts = []
    for r in reasons:
        subset = [s for s in skipped if s["reason"] == r]
        won_counts.append(sum(1 for s in subset if s["would_have_won"]))
        lost_counts.append(sum(1 for s in subset if not s["would_have_won"]))

    x_pos = np.arange(len(reasons))
    width = 0.35
    ax1.bar(x_pos - width/2, won_counts, width, label="Would Have Won", color=GREEN, alpha=0.85, edgecolor="white", linewidth=0.5)
    ax1.bar(x_pos + width/2, lost_counts, width, label="Would Have Lost", color=RED, alpha=0.85, edgecolor="white", linewidth=0.5)
    ax1.set_xticks(x_pos)
    ax1.set_xticklabels([r.replace("_", " ").title() for r in reasons], fontsize=10)
    ax1.set_ylabel("Count", fontsize=12)
    ax1.set_title("Skipped Signals by Reason", fontsize=14, fontweight="bold")
    ax1.legend(fontsize=10)
    ax1.grid(alpha=0.2, axis="y")

    # Overall: would-have-won rate
    total_won = sum(1 for s in skipped if s["would_have_won"])
    total_lost = len(skipped) - total_won
    sizes = [total_won, total_lost]
    labels_pie = [f"Would Have Won\n({total_won})", f"Would Have Lost\n({total_lost})"]
    pie_colors = [GREEN, RED]
    wedges, texts, autotexts = ax2.pie(sizes, labels=labels_pie, colors=pie_colors,
                                        autopct="%1.0f%%", startangle=90,
                                        textprops={"fontsize": 11, "color": "white"})
    for at in autotexts:
        at.set_fontweight("bold")
        at.set_fontsize(14)
    ax2.set_title("Skipped Signals: Would-Have-Won Rate", fontsize=14, fontweight="bold")

    fig.tight_layout()
    fig.savefig(os.path.join(OUT_DIR, "5_skipped_analysis.png"), dpi=150)
    print(f"  Saved 5_skipped_analysis.png")

# ── Chart 6: Key Metrics Table ─────────────────────────────────────

fig, ax = plt.subplots(figsize=(14, 4))
ax.axis("off")

headers = ["Metric", "v13", "v14", "v15"]
rows = []

for label, trades in [("v13", v13), ("v14", v14), ("v15", v15)]:
    wins = sum(1 for t in trades if t["won"])
    losses = len(trades) - wins
    pnl = sum(t["profit"] for t in trades)
    avg_win = np.mean([t["profit"] for t in trades if t["won"]]) if wins else 0
    avg_loss = np.mean([t["profit"] for t in trades if not t["won"]]) if losses else 0
    avg_return = np.mean([t["return_pct"] for t in trades])
    avg_entry = np.mean([t["entry_price"] for t in trades])
    avg_cost = np.mean([t["entry_cost"] for t in trades])
    if not rows:
        rows = [
            ["Trades", 0, 0, 0],
            ["Win Rate", 0, 0, 0],
            ["Total P&L", 0, 0, 0],
            ["Avg Win", 0, 0, 0],
            ["Avg Loss", 0, 0, 0],
            ["Avg Return %", 0, 0, 0],
            ["Avg Entry Price", 0, 0, 0],
            ["Avg Bet Size", 0, 0, 0],
        ]
    col = {"v13": 1, "v14": 2, "v15": 3}[label]
    rows[0][col] = f"{len(trades)}"
    rows[1][col] = f"{wins}/{losses} ({wins/len(trades)*100:.0f}%)"
    rows[2][col] = f"${pnl:+.2f}"
    rows[3][col] = f"${avg_win:+.2f}"
    rows[4][col] = f"${avg_loss:+.2f}"
    rows[5][col] = f"{avg_return:+.1f}%"
    rows[6][col] = f"${avg_entry:.2f}"
    rows[7][col] = f"${avg_cost:.2f}"

table = ax.table(cellText=rows, colLabels=headers, loc="center", cellLoc="center")
table.auto_set_font_size(False)
table.set_fontsize(11)
table.scale(1, 1.6)

# Style header
for j in range(len(headers)):
    cell = table[0, j]
    cell.set_text_props(fontweight="bold", color="white")
    cell.set_facecolor("#333333")
    cell.set_edgecolor("#555555")

# Style data cells
for i in range(1, len(rows) + 1):
    for j in range(len(headers)):
        cell = table[i, j]
        cell.set_facecolor("#1a1a2e" if i % 2 == 0 else "#16213e")
        cell.set_edgecolor("#555555")
        cell.set_text_props(color="white")

ax.set_title("Performance Summary", fontsize=16, fontweight="bold", color="white", pad=20)
fig.tight_layout()
fig.savefig(os.path.join(OUT_DIR, "6_summary_table.png"), dpi=150)
print(f"  Saved 6_summary_table.png")

print(f"\n  All charts saved to {OUT_DIR}/")
