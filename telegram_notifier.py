"""Telegram notification module with hourly summary reports."""

import os
import time
import urllib.request
import urllib.parse
import json
import threading


class TelegramNotifier:
    def __init__(self, bot_token: str = None, chat_id: str = None):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
        self.enabled = bool(self.bot_token and self.chat_id)
        if not self.enabled:
            print("[telegram] No token/chat_id configured — notifications disabled")

    def send(self, message: str, silent: bool = False):
        if not self.enabled:
            return
        threading.Thread(
            target=self._send_sync, args=(message, silent), daemon=True
        ).start()

    def _send_sync(self, message: str, silent: bool):
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            payload = json.dumps({
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "Markdown",
                "disable_notification": silent,
            }).encode("utf-8")
            req = urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json"}
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print(f"[telegram] Failed to send: {e}")

    def trade_alert(self, side: str, price: float, amount: float, market_slug: str, dry_run: bool, edge: float = 0, kelly_size: float = 0):
        mode = "PAPER" if dry_run else "LIVE"
        self.send(
            f"{'📝' if dry_run else '🔔'} *{mode} TRADE*\n"
            f"Side: *{side}*\n"
            f"Price: ${price:.4f}\n"
            f"Amount: ${amount:.2f} (Kelly: ${kelly_size:.2f})\n"
            f"Edge: {edge*100:.1f}%\n"
            f"Market: `{market_slug}`"
        )

    def win_alert(self, profit: float, total_pnl: float):
        self.send(f"✅ *WIN* +${profit:.2f}\nTotal P&L: ${total_pnl:.2f}")

    def loss_alert(self, loss: float, total_pnl: float):
        self.send(f"❌ *LOSS* -${abs(loss):.2f}\nTotal P&L: ${total_pnl:.2f}")

    def hourly_summary(self, hourly: dict, overall: dict):
        """Send the full hourly report with all metrics."""
        h = hourly
        o = overall

        # Build the message
        lines = [
            "📊 *HOURLY SUMMARY*",
            "",
            "*This hour:*",
            f"  Trades: {h['trades']} ({h['wins']}W / {h['losses']}L)",
            f"  Win rate: {h['win_rate']:.1f}%",
            f"  P&L: ${h['pnl']:+.2f}",
        ]

        if h['trades'] > 0:
            lines.append(f"  Avg edge at entry: {h['avg_edge']*100:.1f}%")
            lines.append(f"  Avg BTC delta: {h['avg_delta']:.3f}%")
            lines.append(f"  Best trade: ${h['best_trade']:+.2f}")
            lines.append(f"  Worst trade: ${h['worst_trade']:+.2f}")

        lines.append(f"  Windows seen: {h['windows_seen']}")
        lines.append(f"  Windows skipped: {h['windows_skipped']} (no signal)")

        lines.extend([
            "",
            "*Overall:*",
            f"  Total trades: {o['total_trades']} ({o['wins']}W / {o['losses']}L)",
            f"  Win rate: {o['win_rate']:.1f}%",
            f"  Total P&L: ${o['pnl']:+.2f}",
            f"  Bankroll: ${o['bankroll']:.2f}",
        ])

        self.send("\n".join(lines))

    def status_update(self, stats: dict):
        self.send(
            f"📊 *Status*\n"
            f"Trades: {stats.get('total_trades', 0)}\n"
            f"W/L: {stats.get('wins', 0)}/{stats.get('losses', 0)}\n"
            f"Win rate: {stats.get('win_rate', 0):.1f}%\n"
            f"P&L: ${stats.get('pnl', 0):.2f}\n"
            f"Bankroll: ${stats.get('bankroll', 0):.2f}",
            silent=True,
        )

    def error_alert(self, error: str):
        self.send(f"⚠️ *ERROR*\n`{error[:200]}`")

    def startup_alert(self, config: dict):
        kelly = config.get('kelly_fraction', 0.25)
        self.send(
            f"🚀 *Bot Started*\n"
            f"Mode: *{'DRY RUN' if config.get('dry_run') else 'LIVE'}*\n"
            f"Kelly fraction: {kelly*100:.0f}%\n"
            f"Min edge: {config.get('min_edge', 0)*100:.1f}%\n"
            f"Bet range: ${config.get('min_bet', 1):.0f}–${config.get('max_bet', 25):.0f}\n"
            f"Entry: T-{config.get('entry_start', 60)}s to T-{config.get('entry_end', 10)}s"
        )
