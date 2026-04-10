#!/usr/bin/env python3
"""
reconcile.py — Cross-check PolyBot tracking against Polymarket CSV exports.

Usage:
  # Audit internal consistency of trades.csv
  python reconcile.py

  # Cross-check against Polymarket export (download from Polymarket UI → History → Export)
  python reconcile.py polymarket_export.csv

Reports:
  1. Internal audit: verifies profit math for every trade
  2. Claim sell analysis: shows revenue leaked by selling at market vs $1 resolution
  3. Cross-check (if Polymarket CSV provided): matches bot trades to on-chain transactions
"""

import csv
import sys
from pathlib import Path
from datetime import datetime, timezone


TRADES_PATH = Path(__file__).parent / "logs" / "trades.csv"


def load_trades(path: str = None) -> list[dict]:
    p = Path(path) if path else TRADES_PATH
    if not p.exists():
        print(f"  Error: {p} not found")
        sys.exit(1)
    with open(p, "r", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def audit_internal(trades: list[dict], version_filter: str = None):
    """Check that profit = f(claim_revenue, exit_revenue, entry_cost, won) for each trade."""
    print("\n" + "=" * 70)
    print("  INTERNAL AUDIT — Profit Math Verification")
    print("=" * 70)

    mismatches = []
    for r in trades:
        if version_filter and version_filter not in r.get("version", ""):
            continue

        won = r["won_resolution"] == "True"
        cost = float(r["entry_cost"] or 0)
        profit = float(r["profit"] or 0)
        shares = float(r["entry_shares"] or 0)
        exit_rev = float(r.get("exit_revenue", 0) or 0)
        payout = float(r.get("resolution_payout", 0) or 0)
        method = r.get("resolution_method", "")

        # Reconstruct what profit SHOULD be given the resolution method
        if method == "exited":
            expected = exit_rev - cost
        elif won and method == "claim_sell":
            # claim_revenue = profit + cost - exit_rev (reverse-engineer)
            # Can't independently verify — just check payout vs shares
            claim_rev = profit - exit_rev + cost
            # Flag if claim_rev is negative (impossible) or > shares
            if claim_rev < 0:
                mismatches.append((r, f"negative claim_revenue={claim_rev:.2f}"))
            elif claim_rev > shares * 1.05:  # small tolerance
                mismatches.append((r, f"claim_revenue {claim_rev:.2f} > shares*$1.05={shares*1.05:.2f}"))
            continue  # Can't verify further without independent claim data
        elif won:
            # balance_check / binance_fallback: profit = exit_rev + shares*$1 - cost
            expected = exit_rev + payout - cost
        else:
            expected = exit_rev - cost

        if abs(profit - expected) > 0.05:
            mismatches.append((r, f"profit={profit:+.2f} expected={expected:+.2f} diff={profit-expected:+.2f}"))

    if mismatches:
        print(f"\n  {len(mismatches)} MISMATCHES found:\n")
        for r, msg in mismatches:
            print(f"    {r.get('window_time', '?')} {r.get('side', '?')} — {msg}")
    else:
        print("\n  All trades pass internal consistency check.")


def claim_sell_analysis(trades: list[dict], version_filter: str = None):
    """Analyze revenue leaked by claim sells vs full $1 resolution."""
    print("\n" + "=" * 70)
    print("  CLAIM SELL ANALYSIS — Revenue Leakage")
    print("=" * 70)

    filtered = [r for r in trades if not version_filter or version_filter in r.get("version", "")]
    wins = [r for r in filtered if r["won_resolution"] == "True"]
    losses = [r for r in filtered if r["won_resolution"] != "True"]
    claim_sells = [r for r in wins if r.get("resolution_method") == "claim_sell"]

    total_pnl = sum(float(r["profit"] or 0) for r in filtered)
    total_cost = sum(float(r["entry_cost"] or 0) for r in filtered)
    win_pnl = sum(float(r["profit"] or 0) for r in wins)
    loss_pnl = sum(float(r["entry_cost"] or 0) for r in losses)

    # Theoretical: all wins at $1/share
    theoretical_win_pnl = sum(float(r["entry_shares"]) - float(r["entry_cost"]) for r in wins)
    theoretical_pnl = theoretical_win_pnl - loss_pnl
    leaked = theoretical_pnl - total_pnl

    # Per-trade claim sell breakdown
    claim_revenues = []
    for r in claim_sells:
        cost = float(r["entry_cost"] or 0)
        profit = float(r["profit"] or 0)
        exit_rev = float(r.get("exit_revenue", 0) or 0)
        shares = float(r["entry_shares"] or 0)
        claim_rev = profit - exit_rev + cost
        theoretical = shares * 0.99
        fill_pct = claim_rev / theoretical * 100 if theoretical > 0 else 0
        claim_revenues.append({
            "time": r.get("window_time", "?"),
            "shares": shares,
            "claim_rev": claim_rev,
            "theoretical": theoretical,
            "fill_pct": fill_pct,
            "lost": theoretical - claim_rev,
        })

    print(f"\n  Trades: {len(filtered)} ({len(wins)}W / {len(losses)}L = {len(wins)/len(filtered)*100:.0f}% WR)")
    print(f"  Total deployed:   ${total_cost:.2f}")
    print(f"  Actual P&L:       ${total_pnl:+.2f}")
    print(f"  Theoretical P&L:  ${theoretical_pnl:+.2f}  (all wins at $1/share)")
    print(f"  Revenue leaked:   ${leaked:.2f}  ({leaked/theoretical_pnl*100:.0f}% of theoretical profit)")
    print(f"  Avg leak/trade:   ${leaked/len(claim_sells):.2f}  ({len(claim_sells)} claim sells)")

    # Flag worst fills
    bad = [c for c in claim_revenues if c["fill_pct"] < 92]
    if bad:
        print(f"\n  Worst fills (<92%):")
        for c in sorted(bad, key=lambda x: x["fill_pct"]):
            print(f"    {c['time']} — {c['shares']:.0f} shares, "
                  f"got ${c['claim_rev']:.2f} / ${c['theoretical']:.2f} "
                  f"({c['fill_pct']:.1f}%, lost ${c['lost']:.2f})")


def cross_check_polymarket(trades: list[dict], pm_path: str, version_filter: str = None):
    """Match bot trades against Polymarket CSV export."""
    print("\n" + "=" * 70)
    print("  CROSS-CHECK vs Polymarket Export")
    print("=" * 70)

    # Load Polymarket CSV (BOM-encoded)
    with open(pm_path, "r", encoding="utf-8-sig") as f:
        pm_rows = list(csv.DictReader(f))

    if not pm_rows:
        print("  Error: Polymarket CSV is empty")
        return

    # Show available columns
    print(f"\n  Polymarket CSV: {len(pm_rows)} transactions")
    print(f"  Columns: {', '.join(pm_rows[0].keys())}")

    # Filter to BTC Up/Down 5-min markets
    btc_trades = [r for r in pm_rows if "BTC" in r.get("marketName", "")
                  and ("5 minute" in r.get("marketName", "").lower()
                       or "five minute" in r.get("marketName", "").lower()
                       or "5-min" in r.get("marketName", "").lower()
                       or "5min" in r.get("marketName", "").lower())]

    if not btc_trades:
        # Try broader match
        btc_trades = [r for r in pm_rows if "BTC" in r.get("marketName", "")]
        if btc_trades:
            print(f"\n  No '5 minute' BTC trades found. Showing all BTC trades ({len(btc_trades)}):")
            markets = set(r.get("marketName", "?") for r in btc_trades)
            for m in sorted(markets):
                print(f"    - {m}")
        else:
            print("\n  No BTC trades found in Polymarket export.")
            return
    else:
        print(f"  BTC 5-min trades: {len(btc_trades)}")

    # Summarize Polymarket-side P&L
    buys = [r for r in btc_trades if r.get("action", "").lower() == "buy"]
    sells = [r for r in btc_trades if r.get("action", "").lower() == "sell"]
    redeems = [r for r in btc_trades if r.get("action", "").lower() == "redeem"]

    buy_total = sum(float(r.get("usdcAmount", 0) or 0) for r in buys)
    sell_total = sum(float(r.get("usdcAmount", 0) or 0) for r in sells)
    redeem_total = sum(float(r.get("usdcAmount", 0) or 0) for r in redeems)
    pm_pnl = sell_total + redeem_total - buy_total

    print(f"\n  Polymarket-side totals:")
    print(f"    Buys:    {len(buys)} txns, ${buy_total:.2f} spent")
    print(f"    Sells:   {len(sells)} txns, ${sell_total:.2f} received")
    print(f"    Redeems: {len(redeems)} txns, ${redeem_total:.2f} received")
    print(f"    Net P&L: ${pm_pnl:+.2f}")

    # Compare to bot tracking
    filtered = [r for r in trades if not version_filter or version_filter in r.get("version", "")]
    bot_pnl = sum(float(r["profit"] or 0) for r in filtered)
    bot_deployed = sum(float(r["entry_cost"] or 0) for r in filtered)

    print(f"\n  Bot-side totals (trades.csv):")
    print(f"    Trades:  {len(filtered)}")
    print(f"    Cost:    ${bot_deployed:.2f}")
    print(f"    P&L:     ${bot_pnl:+.2f}")

    drift = pm_pnl - bot_pnl
    print(f"\n  Drift (Polymarket - Bot): ${drift:+.2f}")
    if abs(drift) > 1.0:
        print(f"  ⚠️  Significant drift detected!")
        if drift > 0:
            print(f"       Bot is UNDERCOUNTING profit by ${drift:.2f}")
            print(f"       Likely cause: claim sell partial fills + uncaptured resolution payouts")
        else:
            print(f"       Bot is OVERCOUNTING profit by ${abs(drift):.2f}")
            print(f"       Likely cause: ghost fills or balance-delta contamination")
    else:
        print(f"  ✓ Tracking within $1 tolerance")


def summary_stats(trades: list[dict], version_filter: str = None):
    """Print key performance stats."""
    filtered = [r for r in trades if not version_filter or version_filter in r.get("version", "")]
    if not filtered:
        print("\n  No trades found for filter.")
        return

    print("\n" + "=" * 70)
    print("  PERFORMANCE SUMMARY")
    print("=" * 70)

    wins = [r for r in filtered if r["won_resolution"] == "True"]
    losses = [r for r in filtered if r["won_resolution"] != "True"]
    n = len(filtered)
    wr = len(wins) / n * 100

    profits = [float(r["profit"] or 0) for r in filtered]
    total_pnl = sum(profits)
    total_deployed = sum(float(r["entry_cost"] or 0) for r in filtered)
    roi = total_pnl / total_deployed * 100 if total_deployed > 0 else 0

    gross_wins = sum(p for p in profits if p > 0)
    gross_losses = abs(sum(p for p in profits if p < 0))
    pf = gross_wins / gross_losses if gross_losses > 0 else float("inf")
    avg_win = gross_wins / len(wins) if wins else 0
    avg_loss = gross_losses / len(losses) if losses else 0
    expectancy = total_pnl / n

    # Max drawdown
    peak = cum = max_dd = 0
    for p in profits:
        cum += p
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd

    print(f"  Record:      {len(wins)}W / {len(losses)}L ({wr:.0f}% WR)")
    print(f"  P&L:         ${total_pnl:+.2f}")
    print(f"  ROI:         {roi:+.1f}% on ${total_deployed:.0f} deployed")
    print(f"  Profit F:    {pf:.2f}")
    print(f"  Avg Win:     ${avg_win:+.2f}")
    print(f"  Avg Loss:    ${avg_loss:.2f}")
    print(f"  Expectancy:  ${expectancy:+.2f}/trade")
    print(f"  Max DD:      ${max_dd:.2f}")


def main():
    pm_csv = None
    version_filter = None

    for arg in sys.argv[1:]:
        if arg.startswith("--version="):
            version_filter = arg.split("=", 1)[1]
        elif arg.endswith(".csv"):
            pm_csv = arg
        elif arg in ("-h", "--help"):
            print(__doc__)
            return

    trades = load_trades()
    print(f"\n  Loaded {len(trades)} trades from {TRADES_PATH}")

    if version_filter:
        count = sum(1 for t in trades if version_filter in t.get("version", ""))
        print(f"  Filter: version contains '{version_filter}' ({count} trades)")

    summary_stats(trades, version_filter)
    audit_internal(trades, version_filter)
    claim_sell_analysis(trades, version_filter)

    if pm_csv:
        cross_check_polymarket(trades, pm_csv, version_filter)
    else:
        print("\n" + "-" * 70)
        print("  Tip: Export your Polymarket history CSV and run:")
        print("    python reconcile.py polymarket_export.csv")
        print("  to cross-check bot tracking against on-chain transactions.")


if __name__ == "__main__":
    main()
