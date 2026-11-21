#!/usr/bin/env python3
"""PolyBot Trading Dashboard — live charts from CSV data.

Usage:
    python dashboard.py          # opens http://localhost:8050
    python dashboard.py 9000     # custom port
"""

import csv
import glob
import json
import os
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

from market import get_market_outcome, market_slug

LOG_DIR = "logs"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8050

# Polymarket results of near-miss windows (see window_outcomes). The dashboard
# doesn't read .env, so a 15-minute bot needs MARKET_PERIOD=15 set when starting it.
MARKET_PERIOD = int(os.getenv("MARKET_PERIOD", "5"))
OUTCOMES_CACHE = os.path.join(LOG_DIR, "outcomes.json")
LOOKUP_RETRY_SECS = 60     # wait before asking Gamma again about an unresolved window
MAX_LOOKUPS = 25           # per request, so a long history can't stall a page load
DEFAULT_STAKE = 5.0        # MIN_BET, when a signal row has no Kelly size
_lookup_failed: dict = {}  # slug -> time of the last unresolved/failed lookup

# ── CSV loading ────────────────────────────────────────────────────

def load_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))

def get_all_trades():
    trades = []
    # v13
    for row in load_csv(os.path.join(LOG_DIR, "trades_v13_pre_fix.csv")):
        row["version"] = "v13"
        trades.append(row)
    # v14
    for row in load_csv(os.path.join(LOG_DIR, "trades_v14.csv")):
        row["version"] = "v14"
        trades.append(row)
    # v15+: archives from schema changes (numbered when there's more than one),
    # then trades.csv
    archives = sorted(glob.glob(os.path.join(LOG_DIR, "trades_pre_schema_change*.csv")))
    for path in archives + [os.path.join(LOG_DIR, "trades.csv")]:
        for row in load_csv(path):
            v = row.get("version", "15")
            row["version"] = f"v{v}" if not str(v).startswith("v") else v
            trades.append(row)
    trades.sort(key=lambda r: float(r.get("timestamp", 0)))
    return trades

def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def _version(row):
    v = str(row.get("version") or "15")
    return v if v.startswith("v") else f"v{v}"

def _load_outcomes():
    try:
        with open(OUTCOMES_CACHE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}

def _save_outcomes(cache):
    tmp = OUTCOMES_CACHE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(cache, f, indent=1, sort_keys=True)
        os.replace(tmp, OUTCOMES_CACHE)
    except OSError as e:
        print(f"  Couldn't save {OUTCOMES_CACHE}: {e}")

def window_outcomes(window_ts_list):
    """Polymarket's payout per share for finished windows, keyed by market
    slug: {"btc-updown-5m-…": {"UP": 1.0, "DOWN": 0.0}}.

    Read from Gamma, the same source the bot books its trades from. Resolved
    results are final, so they're saved in logs/outcomes.json and never
    fetched twice. Windows not resolved yet (or Gamma unreachable) are left
    out and asked about again after LOOKUP_RETRY_SECS.
    """
    cache = _load_outcomes()
    now = time.time()
    changed = False
    lookups = 0
    for w in sorted({int(_num(w)) for w in window_ts_list if _num(w) > 0}):
        slug = market_slug(MARKET_PERIOD, w)
        if slug in cache:
            continue
        if now < w + MARKET_PERIOD * 60 or now - _lookup_failed.get(slug, 0) < LOOKUP_RETRY_SECS:
            continue
        if lookups >= MAX_LOOKUPS:
            break
        lookups += 1
        outcome = get_market_outcome(w, MARKET_PERIOD)
        if not outcome or not outcome["resolved"]:
            _lookup_failed[slug] = now
            continue
        cache[slug] = outcome["prices"]
        changed = True
    if changed:
        _save_outcomes(cache)
    return cache

def get_skipped():
    """Windows the bot didn't trade, one row per window, from signals.csv:

    - kind "near_miss": the strategy gave a signal and the bot tried to buy
      but didn't. Reason "edge_gone_at_market" (the price it checked had
      moved too far) or "attempt_unlogged" (the attempt failed without
      logging why). Each gets Polymarket's result and what the buy would have
      made at the price the bot would have paid (the book price it checked,
      else the price it saw), before fees. Rows logged since the vol column
      was added say what stopped every attempt (e.g. "no_fill",
      "daily_loss_limit").
    - kind "quiet": no signal all window. Rows with a vol are the window's
      closest call, and their reason is the check it failed. Older rows were
      logged at the window's last second, when the market had already
      settled, so their logged reason isn't used ("no_signal").

    A legacy skipped.csv (older versions, with would_have_won) is used as is.
    """
    path = os.path.join(LOG_DIR, "skipped.csv")
    if os.path.exists(path):
        return [{**row, "kind": "near_miss"} for row in load_csv(path)]

    by_window = {}
    for row in load_csv(os.path.join(LOG_DIR, "signals.csv")):
        by_window.setdefault(row.get("window_ts", ""), []).append(row)

    picks = []
    for rows in by_window.values():
        if any(r.get("action") == "traded" for r in rows):
            continue
        # A specific skip (e.g. skipped_edge_gone) is followed by the generic
        # end-of-window "no_signal" row for the same window: keep the specific one
        specific = [r for r in rows if r.get("action") != "no_signal"]
        if specific:
            r = specific[0]
            picks.append(("near_miss", r, r.get("skip_reason") or r.get("action") or "unknown"))
        elif rows[-1].get("vol"):
            # Since the vol column: the window's closest call, with the check it failed
            picks.append(("quiet", rows[-1], rows[-1].get("skip_reason") or "no_signal"))
        elif rows[-1].get("side"):
            # Older rows: the end-of-window row carries a signal only if the bot
            # acted on it (after an attempt the window isn't evaluated again)
            picks.append(("near_miss", rows[-1], "attempt_unlogged"))
        else:
            # Older rows: logged at the window's last second, reason not meaningful
            picks.append(("quiet", rows[-1], "no_signal"))

    outcomes = window_outcomes([r.get("window_ts") for kind, r, _ in picks if kind == "near_miss"])

    skipped = []
    for kind, r, reason in picks:
        item = {
            "kind": kind,
            "reason": reason,
            "version": _version(r),
            "window_ts": r.get("window_ts", ""),
            "window_time": r.get("window_time", ""),
            "side": r.get("side", ""),
            "would_have_won": "",
        }
        if kind == "near_miss":
            side = r.get("side", "")
            seen = _num(r.get("up_price") if side == "UP" else r.get("down_price"))
            checked = _num(r.get("actual_price"))
            price = checked if checked > 0 else seen
            stake = _num(r.get("kelly_size")) or DEFAULT_STAKE
            item.update({
                "prob": _num(r.get("true_prob")),
                "btc_delta_pct": _num(r.get("btc_delta_pct")),
                "seconds_remaining": _num(r.get("seconds_remaining")),
                "seen_price": seen,
                "price": price,
                "price_checked": checked > 0,
                "stake": round(stake, 2),
            })
            prices = outcomes.get(market_slug(MARKET_PERIOD, int(_num(r.get("window_ts")))))
            if prices and side in prices and 0 < price < 1:
                payout = float(prices[side])
                item["payout"] = payout
                item["would_have_won"] = ("True" if payout > 0.5 else
                                          "False" if payout < 0.5 else "Split")
                item["pnl_if_taken"] = round(stake / price * payout - stake, 2)
        skipped.append(item)
    skipped.sort(key=lambda s: _num(s["window_ts"]))
    return skipped

def get_sessions():
    """sessions.csv, most recent first. A session is written when the bot stops."""
    return list(reversed(load_csv(os.path.join(LOG_DIR, "sessions.csv"))))

def get_hold_ticks():
    return load_csv(os.path.join(LOG_DIR, "hold_ticks.csv"))

# ── HTTP handler ───────────────────────────────────────────────────

# BaseHTTPRequestHandler, not SimpleHTTPRequestHandler: only the routes below
# exist, so no request can reach files on disk (e.g. .env).
class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/trades":
            self._json_response(get_all_trades())
        elif self.path == "/api/skipped":
            self._json_response(get_skipped())
        elif self.path == "/api/hold_ticks":
            self._json_response(get_hold_ticks())
        elif self.path == "/api/sessions":
            self._json_response(get_sessions())
        elif self.path == "/" or self.path == "/index.html":
            self._html_response(DASHBOARD_HTML)
        else:
            self.send_error(404)

    def _json_response(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _html_response(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # silence request logs

# ── Dashboard HTML ─────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PolyBot Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.js"
        integrity="sha384-zYPBGXwO4633CABX/5Spf6emCKUJCfoOkhOMYyxMsatqQZPnDblmmOewfjsIVWCM" crossorigin="anonymous"></script>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #0a0a1a; color: #e0e0e0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }
  .header { padding: 24px 32px 16px; display: flex; justify-content: space-between; align-items: center; }
  .header h1 { font-size: 28px; color: #fff; }
  .header h1 span { color: #6bcb77; }
  .refresh-btn { background: #1a1a2e; border: 1px solid #333; color: #aaa; padding: 8px 16px; border-radius: 6px; cursor: pointer; font-size: 13px; }
  .refresh-btn:hover { background: #222; color: #fff; }
  .filter-bar { display: flex; gap: 8px; padding: 0 32px 16px; align-items: center; }
  .filter-bar span { font-size: 12px; color: #666; text-transform: uppercase; letter-spacing: 1px; margin-right: 4px; }
  .filter-btn { background: #1a1a2e; border: 1px solid #333; color: #888; padding: 6px 16px; border-radius: 6px; cursor: pointer; font-size: 13px; font-weight: 600; transition: all 0.15s; }
  .filter-btn:hover { background: #222; color: #fff; }
  .filter-btn.active { border-color: #6bcb77; color: #6bcb77; background: #6bcb7718; }
  .stats-bar { display: flex; gap: 16px; padding: 0 32px 20px; flex-wrap: wrap; }
  .stat-card { background: #1a1a2e; border: 1px solid #222; border-radius: 10px; padding: 16px 24px; min-width: 160px; }
  .stat-card .label { font-size: 11px; text-transform: uppercase; color: #666; letter-spacing: 1px; }
  .stat-card .value { font-size: 28px; font-weight: 700; margin-top: 4px; }
  .stat-card .sub { font-size: 12px; color: #888; margin-top: 2px; }
  .green { color: #6bcb77; }
  .red { color: #ff6b6b; }
  .gold { color: #ffd93d; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; padding: 0 32px 32px; }
  .chart-box { background: #1a1a2e; border: 1px solid #222; border-radius: 10px; padding: 20px; }
  .chart-box.full { grid-column: 1 / -1; }
  .chart-box h3 { font-size: 14px; color: #888; margin-bottom: 12px; text-transform: uppercase; letter-spacing: 1px; }
  canvas { max-height: 320px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; padding: 10px 12px; background: #111; color: #888; font-weight: 600; text-transform: uppercase; font-size: 11px; letter-spacing: 1px; }
  td { padding: 10px 12px; border-top: 1px solid #1a1a2e; }
  tr:nth-child(even) { background: #0f0f1f; }
  tr:hover { background: #151530; }
  .version-badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 700; }
  .won { color: #6bcb77; }
  .lost { color: #ff6b6b; }
  tr.clickable { cursor: pointer; }
  tr.clickable:hover { background: #1a1a3a !important; }
  tr.clickable.selected { background: #1a2a1a !important; border-left: 3px solid #6bcb77; }
  #trade-detail { display: none; margin-top: 16px; padding: 20px; background: #111; border: 1px solid #333; border-radius: 10px; }
  #trade-detail.visible { display: block; }
  #trade-detail h4 { font-size: 15px; color: #ccc; margin-bottom: 4px; }
  #trade-detail .detail-sub { font-size: 12px; color: #666; margin-bottom: 16px; }
  #trade-detail .detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  #trade-detail .detail-grid canvas { max-height: 220px; }
  #trade-detail .detail-chart { background: #0a0a1a; border: 1px solid #222; border-radius: 8px; padding: 12px; }
  #trade-detail .detail-chart.full { grid-column: 1 / -1; }
  #trade-detail .detail-chart h5 { font-size: 11px; color: #666; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; }
  .no-ticks { color: #666; font-size: 13px; padding: 20px; text-align: center; }
  .header-right { display: flex; gap: 14px; align-items: center; font-size: 12px; color: #666; }
  .header-right label { display: flex; gap: 6px; align-items: center; cursor: pointer; color: #888; }
  .chart-box h3 .note { font-size: 11px; color: #666; font-weight: 400; text-transform: none; letter-spacing: 0; }
  .empty { color: #666; font-size: 13px; padding: 12px 0; }
  .muted { color: #666; }
  .table-wrap { overflow-x: auto; }
  @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } #trade-detail .detail-grid { grid-template-columns: 1fr; } }
</style>
</head>
<body>

<div class="header">
  <h1><span>PolyBot</span> Dashboard</h1>
  <div class="header-right">
    <span id="updated"></span>
    <label><input type="checkbox" id="autoRefresh" onchange="setAutoRefresh(this.checked)"> Auto-refresh (30s)</label>
    <button class="refresh-btn" onclick="refresh(true)">Refresh</button>
  </div>
</div>

<div class="filter-bar" id="filter-bar">
  <span>Filter:</span>
  <button class="filter-btn active" data-v="all" onclick="setFilter('all')">All</button>
</div>
<div class="stats-bar" id="stats-bar"></div>

<div class="grid">
  <div class="chart-box full"><h3>Equity Curve</h3><canvas id="equityChart"></canvas></div>
  <div class="chart-box"><h3>Win Rate by Version</h3><canvas id="winRateChart"></canvas></div>
  <div class="chart-box"><h3>Total P&L by Version</h3><canvas id="pnlChart"></canvas></div>
  <div class="chart-box full"><h3>Per-Trade P&L Waterfall</h3><canvas id="waterfallChart"></canvas></div>
  <div class="chart-box full"><h3>Edge: Seen vs At Fill <span class="note" id="edge-note"></span></h3><canvas id="edgeChart"></canvas></div>
  <div class="chart-box"><h3>Near Misses by Reason</h3><canvas id="skippedBarChart"></canvas><div class="empty" id="skipped-bar-empty"></div></div>
  <div class="chart-box"><h3>Near Misses: Would-Have-Won Rate</h3><canvas id="skippedPieChart"></canvas><div class="empty" id="skipped-pie-empty"></div></div>
  <div class="chart-box full"><h3>Quiet Windows: Closest Call <span class="note">(the check that stopped each quiet window's closest moment to a signal)</span></h3><canvas id="closestChart" style="max-height:220px"></canvas><div class="empty" id="closest-empty"></div></div>
  <div class="chart-box full"><h3>Near Misses <span class="note">(signals the bot tried but didn't buy, with Polymarket's result; P&L at the price it would have paid, before fees)</span></h3><div class="table-wrap" id="near-miss-table"></div></div>
  <div class="chart-box full"><h3>Recent Trades <span class="note">(click a row to see hold trajectory)</span></h3><div class="table-wrap" id="trades-table"></div><div id="trade-detail"><h4 id="detail-title"></h4><div class="detail-sub" id="detail-sub"></div><div class="detail-grid" id="detail-charts"></div></div></div>
  <div class="chart-box full"><h3>Sessions <span class="note">(all versions; a session appears when the bot stops)</span></h3><div class="table-wrap" id="sessions-table"></div></div>
</div>

<script>
// Dynamic color palette — auto-assigns colors to any version string
const VERSION_PALETTE = ['#ff6b6b', '#ffd93d', '#6bcb77', '#4ecdc4', '#a78bfa', '#f472b6', '#fb923c', '#38bdf8', '#34d399', '#e879f9'];
const KNOWN_COLORS = { v13: '#ff6b6b', v14: '#ffd93d', v15: '#6bcb77' };
const _colorCache = {};
function vColor(v) {
  if (KNOWN_COLORS[v]) return KNOWN_COLORS[v];
  if (_colorCache[v]) return _colorCache[v];
  const idx = Object.keys(_colorCache).length;
  _colorCache[v] = VERSION_PALETTE[(idx + Object.keys(KNOWN_COLORS).length) % VERSION_PALETTE.length];
  return _colorCache[v];
}
const GREEN = '#6bcb77', RED = '#ff6b6b', GOLD = '#ffd93d', BLUE = '#4ecdc4';

Chart.defaults.color = '#888';
Chart.defaults.borderColor = '#222';
Chart.defaults.font.family = '-apple-system, BlinkMacSystemFont, sans-serif';

let ALL_TRADES = [];
let ALL_SKIPPED = [];
let ALL_HOLD_TICKS = [];
let ALL_SESSIONS = [];
let ACTIVE_FILTER = 'all';
let CHART_INSTANCES = {};
let DETAIL_CHARTS = {};
let LAST_DATA_KEY = '';
let AUTO_TIMER = null;
const AUTO_REFRESH_MS = 30000;

// CSV values go into innerHTML, so escape them
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
const money = (x, sign = true) => `${x < 0 ? '-' : sign ? '+' : ''}$${Math.abs(x).toFixed(2)}`;
const cents = p => `${Math.round(p * 100)}¢`;

async function loadData() {
  const get = url => fetch(url).then(r => { if (!r.ok) throw new Error(url); return r.json(); });
  const [trades, skipped, holdTicks, sessions] = await Promise.all([
    get('/api/trades'), get('/api/skipped'), get('/api/hold_ticks'), get('/api/sessions'),
  ]);
  return { trades, skipped, holdTicks, sessions };
}

// Near misses (the bot tried but didn't buy) vs quiet windows (no signal at all)
const nearMisses = skipped => skipped.filter(s => s.kind !== 'quiet');
const quietWindows = skipped => skipped.filter(s => s.kind === 'quiet');

const REASON_LABELS = {
  // attempts that didn't buy
  edge_gone_at_market: 'price moved too far',
  attempt_unlogged: 'attempt failed (no reason logged)',
  clob_halted: 'trading halted (API failures)',
  health_check_failed: 'balance check failed',
  daily_loss_limit: 'daily loss limit',
  market_not_found: 'market not found',
  too_late: 'too close to window end',
  no_fill: "order didn't fill",
  price_cap: 'price above cap',
  too_small: 'order too small',
  no_price: 'no price',
  not_initialized: 'not connected',
  rejected: 'order rejected',
  api_error: 'API error',
  unknown_fill: 'fill unknown',
  // closest call of a quiet window
  delta_too_small: 'BTC move too small',
  price_out_of_range: 'price out of range',
  prob_below_min: 'probability below min',
  edge_below_min: 'edge below min',
  no_signal: 'not logged (older rows)',
};
const reasonLabel = r => REASON_LABELS[r]
  || (String(r).startsWith('rejected_') ? `order rejected (${String(r).slice(9).replace(/_/g, ' ')})` : '')
  || String(r || 'unknown').replace(/_/g, ' ');
// Closest-call reasons, in the order evaluate() checks them
const CLOSEST_CALL_ORDER = ['delta_too_small', 'price_out_of_range', 'prob_below_min', 'edge_below_min'];

function groupByVersion(trades) {
  const groups = {};
  trades.forEach(t => {
    const v = t.version || 'v15';
    if (!groups[v]) groups[v] = [];
    groups[v].push(t);
  });
  return groups;
}

function filterTrades(trades, version) {
  if (version === 'all') return trades;
  return trades.filter(t => t.version === version);
}

function filterSkipped(skipped, version) {
  if (version === 'all') return skipped;
  return skipped.filter(s => s.version === version);
}

function buildFilterButtons(trades, skipped) {
  const versions = [...new Set([...trades.map(t => t.version), ...nearMisses(skipped).map(s => s.version)])];
  const bar = document.getElementById('filter-bar');
  // Rebuilt on every data refresh, so a new version gets its button
  bar.innerHTML = '<span>Filter:</span><button class="filter-btn" data-v="all" onclick="setFilter(\'all\')">All</button>';
  versions.forEach(v => {
    const btn = document.createElement('button');
    btn.className = 'filter-btn';
    btn.dataset.v = v;
    btn.textContent = v;
    btn.onclick = () => setFilter(v);
    bar.appendChild(btn);
  });
  return versions;
}

function applyFilterColors() {
  document.querySelectorAll('.filter-btn.active').forEach(btn => {
    const v = btn.dataset.v;
    if (v && v !== 'all') {
      const c = vColor(v);
      btn.style.borderColor = c;
      btn.style.color = c;
      btn.style.background = c + '18';
    } else {
      btn.style.borderColor = '';
      btn.style.color = '';
      btn.style.background = '';
    }
  });
  document.querySelectorAll('.filter-btn:not(.active)').forEach(btn => {
    btn.style.borderColor = '';
    btn.style.color = '';
    btn.style.background = '';
  });
}

function setFilter(version) {
  ACTIVE_FILTER = version;
  document.querySelectorAll('.filter-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.v === version);
  });
  applyFilterColors();
  Object.values(CHART_INSTANCES).forEach(c => c.destroy());
  CHART_INSTANCES = {};
  const trades = filterTrades(ALL_TRADES, version);
  const skipped = filterSkipped(ALL_SKIPPED, version);
  renderAll(trades, skipped);
}

function renderStats(trades, skipped) {
  const bar = document.getElementById('stats-bar');
  const totalTrades = trades.length;
  const wins = trades.filter(t => t.won_resolution === 'True').length;
  const losses = totalTrades - wins;
  const pnl = trades.reduce((s, t) => s + parseFloat(t.profit || 0), 0);
  const wr = totalTrades > 0 ? (wins / totalTrades * 100) : 0;
  const misses = nearMisses(skipped);
  const quiet = quietWindows(skipped).length;
  const skippedWon = misses.filter(s => s.would_have_won === 'True').length;
  const skippedKnown = misses.filter(s => s.would_have_won === 'True' || s.would_have_won === 'False').length;
  const priced = misses.filter(s => s.pnl_if_taken !== undefined);
  const missedPnl = priced.reduce((s, m) => s + m.pnl_if_taken, 0);
  const missSub = skippedKnown ? `${skippedWon} of ${skippedKnown} would have won` : misses.length ? 'results pending' : 'none';
  const missPnlSub = priced.length ? `<div class="sub ${missedPnl >= 0 ? 'won' : 'lost'}">${money(missedPnl)} if taken</div>` : '';
  // Fees: measured since the fee column was added (already in entry_cost and profit)
  const feeTrades = trades.filter(t => t.fee !== undefined && t.fee !== '');
  const fees = feeTrades.reduce((s, t) => s + parseFloat(t.fee || 0), 0);

  // Profit factor = gross wins / gross losses
  const grossWins = trades.filter(t => parseFloat(t.profit || 0) > 0).reduce((s, t) => s + parseFloat(t.profit), 0);
  const grossLosses = Math.abs(trades.filter(t => parseFloat(t.profit || 0) < 0).reduce((s, t) => s + parseFloat(t.profit), 0));
  const pf = grossLosses > 0 ? grossWins / grossLosses : grossWins > 0 ? Infinity : 0;

  // Avg win / avg loss
  const winTrades = trades.filter(t => parseFloat(t.profit || 0) > 0);
  const lossTrades = trades.filter(t => parseFloat(t.profit || 0) < 0);
  const avgWin = winTrades.length > 0 ? winTrades.reduce((s, t) => s + parseFloat(t.profit), 0) / winTrades.length : 0;
  const avgLoss = lossTrades.length > 0 ? lossTrades.reduce((s, t) => s + parseFloat(t.profit), 0) / lossTrades.length : 0;

  // Expectancy (avg profit per trade)
  const expectancy = totalTrades > 0 ? pnl / totalTrades : 0;

  // Max drawdown
  let peak = 0, cum = 0, maxDD = 0;
  trades.forEach(t => {
    cum += parseFloat(t.profit || 0);
    if (cum > peak) peak = cum;
    const dd = peak - cum;
    if (dd > maxDD) maxDD = dd;
  });

  // ROI % (total P&L / total capital deployed)
  const totalDeployed = trades.reduce((s, t) => s + parseFloat(t.entry_cost || 0), 0);
  const roi = totalDeployed > 0 ? (pnl / totalDeployed * 100) : 0;

  bar.innerHTML = `
    <div class="stat-card"><div class="label">Total Trades</div><div class="value">${totalTrades}</div><div class="sub">${wins}W / ${losses}L</div></div>
    <div class="stat-card"><div class="label">Win Rate</div><div class="value ${wr >= 70 ? 'green' : wr >= 50 ? 'gold' : 'red'}">${wr.toFixed(0)}%</div><div class="sub">~70% breakeven</div></div>
    <div class="stat-card"><div class="label">Total P&L</div><div class="value ${pnl >= 0 ? 'green' : 'red'}">$${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}</div></div>
    <div class="stat-card"><div class="label">Profit Factor</div><div class="value ${pf >= 1.5 ? 'green' : pf >= 1 ? 'gold' : 'red'}">${pf === Infinity ? '∞' : pf.toFixed(2)}</div><div class="sub">${pf >= 2 ? 'Strong' : pf >= 1.5 ? 'Good' : pf >= 1 ? 'Marginal' : 'Negative'}</div></div>
    <div class="stat-card"><div class="label">Avg Win / Loss</div><div class="value green">$${avgWin.toFixed(2)}</div><div class="sub red">$${avgLoss.toFixed(2)}</div></div>
    <div class="stat-card"><div class="label">Expectancy</div><div class="value ${expectancy >= 0 ? 'green' : 'red'}">$${expectancy >= 0 ? '+' : ''}${expectancy.toFixed(2)}</div><div class="sub">per trade</div></div>
    <div class="stat-card"><div class="label">Max Drawdown</div><div class="value red">$${maxDD.toFixed(2)}</div></div>
    <div class="stat-card"><div class="label">ROI</div><div class="value ${roi >= 0 ? 'green' : 'red'}">${roi >= 0 ? '+' : ''}${roi.toFixed(1)}%</div><div class="sub">on $${totalDeployed.toFixed(0)} deployed</div></div>
    <div class="stat-card"><div class="label">Near Misses</div><div class="value">${misses.length}</div><div class="sub">${missSub}</div>${missPnlSub}</div>
    <div class="stat-card"><div class="label">Quiet Windows</div><div class="value">${quiet}</div><div class="sub">no signal all window</div></div>
    <div class="stat-card"><div class="label">Fees</div><div class="value">$${fees.toFixed(2)}</div><div class="sub">${feeTrades.length
      ? `${feeTrades.length} of ${totalTrades} trades measured${grossWins > 0 ? ` · ${(fees / grossWins * 100).toFixed(0)}% of gross wins` : ''}`
      : 'not logged for these trades'}</div></div>
  `;
}

function renderEquityCurve(trades) {
  // Single continuous equity curve, colored by version segments
  let cum = 0;
  const labels = ['0'];
  const cumData = [0];
  const pointColors = ['#888'];
  const versions = [''];

  trades.forEach((t, i) => {
    cum += parseFloat(t.profit || 0);
    labels.push(String(i + 1));
    cumData.push(cum);
    pointColors.push(t.won_resolution === 'True' ? GREEN : RED);
    versions.push(t.version);
  });

  // Build per-version line segments (each version is a separate dataset for coloring)
  const datasets = [];
  let segStart = 1;
  let prevV = versions[1];
  for (let i = 2; i <= trades.length; i++) {
    if (versions[i] !== prevV || i === trades.length) {
      const end = (i === trades.length) ? i : i - 1;
      const data = new Array(labels.length).fill(null);
      // Include one point before segment start for continuity
      if (segStart > 1) data[segStart - 1] = cumData[segStart - 1];
      for (let j = segStart; j <= end; j++) data[j] = cumData[j];
      if (i === trades.length) data[i] = cumData[i];
      datasets.push({
        label: prevV,
        data,
        borderColor: vColor(prevV) || BLUE,
        backgroundColor: (vColor(prevV) || BLUE) + '11',
        fill: false,
        tension: 0.2,
        pointRadius: data.map(d => d !== null ? 5 : 0),
        pointBackgroundColor: data.map((d, j) => d !== null ? pointColors[j] : 'transparent'),
        spanGaps: false,
      });
      segStart = end;
      prevV = versions[i];
    }
  }

  CHART_INSTANCES.equity = new Chart(document.getElementById('equityChart'), {
    type: 'line',
    data: { labels, datasets },
    options: {
      scales: {
        x: { title: { display: true, text: 'Trade # (all versions, chronological)' } },
        y: { title: { display: true, text: 'Cumulative P&L ($)' } }
      },
      plugins: {
        legend: { position: 'top' },
        tooltip: { callbacks: {
          title: ctx => `Trade #${ctx[0].label}`,
          label: ctx => {
            const idx = parseInt(ctx.label);
            if (idx === 0) return 'Start';
            const t = trades[idx - 1];
            return [`${t.version}: ${t.side} @ $${parseFloat(t.entry_price).toFixed(2)}`, `P&L: $${parseFloat(t.profit).toFixed(2)}`, `Cumulative: $${ctx.raw >= 0 ? '+' : ''}${ctx.raw.toFixed(2)}`];
          }
        }}
      }
    }
  });
}

function renderWinRate(trades) {
  const groups = groupByVersion(trades);
  const labels = [], data = [], colors = [], counts = [];
  for (const [v, vTrades] of Object.entries(groups)) {
    const wins = vTrades.filter(t => t.won_resolution === 'True').length;
    labels.push(v);
    data.push((wins / vTrades.length * 100));
    colors.push(vColor(v) || BLUE);
    counts.push(vTrades.length);
  }
  CHART_INSTANCES.winRate = new Chart(document.getElementById('winRateChart'), {
    type: 'bar',
    data: { labels, datasets: [{ data, backgroundColor: colors.map(c => c + 'cc'), borderColor: colors, borderWidth: 1 }] },
    options: {
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: (ctx) => `${ctx.raw.toFixed(0)}% (${counts[ctx.dataIndex]} trades)` } },
        annotation: { annotations: { breakeven: { type: 'line', yMin: 70, yMax: 70, borderColor: '#666', borderDash: [5, 5], label: { content: '~Breakeven', display: true, position: 'end', color: '#666', font: { size: 10 } } } } }
      },
      scales: { y: { beginAtZero: true, max: 110, title: { display: true, text: 'Win Rate (%)' } } }
    }
  });
}

function renderPnL(trades) {
  const groups = groupByVersion(trades);
  const labels = [], data = [], colors = [];
  for (const [v, vTrades] of Object.entries(groups)) {
    const pnl = vTrades.reduce((s, t) => s + parseFloat(t.profit || 0), 0);
    labels.push(v);
    data.push(pnl);
    colors.push(pnl >= 0 ? GREEN : RED);
  }
  CHART_INSTANCES.pnl = new Chart(document.getElementById('pnlChart'), {
    type: 'bar',
    data: { labels, datasets: [{ data, backgroundColor: colors.map(c => c + 'cc'), borderColor: colors, borderWidth: 1 }] },
    options: { plugins: { legend: { display: false }, tooltip: { callbacks: { label: ctx => `$${ctx.raw >= 0 ? '+' : ''}${ctx.raw.toFixed(2)}` } } }, scales: { y: { title: { display: true, text: 'P&L ($)' } } } }
  });
}

function renderWaterfall(trades) {
  const profits = trades.map(t => parseFloat(t.profit || 0));
  const colors = profits.map(p => p >= 0 ? GREEN + 'cc' : RED + 'cc');
  const borders = profits.map(p => p >= 0 ? GREEN : RED);
  // Version dividers
  let prevV = '';
  const versionLabels = trades.map((t, i) => {
    if (t.version !== prevV) { prevV = t.version; return t.version; }
    return '';
  });

  CHART_INSTANCES.waterfall = new Chart(document.getElementById('waterfallChart'), {
    type: 'bar',
    data: {
      labels: trades.map((_, i) => i + 1),
      datasets: [{ data: profits, backgroundColor: colors, borderColor: borders, borderWidth: 1 }]
    },
    options: {
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: {
          title: ctx => `Trade #${ctx[0].label} (${trades[ctx[0].dataIndex].version})`,
          label: ctx => {
            const t = trades[ctx.dataIndex];
            return [`$${ctx.raw >= 0 ? '+' : ''}${ctx.raw.toFixed(2)}`, `${t.side} @ $${parseFloat(t.entry_price).toFixed(2)}`, `${t.won_resolution === 'True' ? 'WIN' : 'LOSS'}`];
          }
        }}
      },
      scales: { x: { title: { display: true, text: 'Trade # (chronological)' } }, y: { title: { display: true, text: 'Profit ($)' } } }
    }
  });
}

function renderSkippedBar(skipped) {
  const misses = nearMisses(skipped);
  const canvas = document.getElementById('skippedBarChart');
  canvas.style.display = misses.length ? '' : 'none';
  document.getElementById('skipped-bar-empty').textContent = misses.length ? '' : 'No near misses for this filter.';
  if (!misses.length) return;
  const reasons = {};
  misses.forEach(s => {
    const r = s.reason || 'unknown';
    if (!reasons[r]) reasons[r] = { won: 0, lost: 0, unknown: 0 };
    if (s.would_have_won === 'True') reasons[r].won++;
    else if (s.would_have_won === 'False') reasons[r].lost++;
    else reasons[r].unknown++;
  });
  const labels = Object.keys(reasons).map(reasonLabel);
  CHART_INSTANCES.skippedBar = new Chart(canvas, {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { label: 'Would Have Won', data: Object.values(reasons).map(v => v.won), backgroundColor: GREEN + 'cc', borderColor: GREEN, borderWidth: 1 },
        { label: 'Would Have Lost', data: Object.values(reasons).map(v => v.lost), backgroundColor: RED + 'cc', borderColor: RED, borderWidth: 1 },
        { label: 'Result Pending / Split', data: Object.values(reasons).map(v => v.unknown), backgroundColor: '#888888cc', borderColor: '#888888', borderWidth: 1 },
      ]
    },
    options: { plugins: { legend: { position: 'top' } }, scales: { y: { beginAtZero: true, ticks: { stepSize: 1 } } } }
  });
}

function renderSkippedPie(skipped) {
  const misses = nearMisses(skipped);
  const won = misses.filter(s => s.would_have_won === 'True').length;
  const lost = misses.filter(s => s.would_have_won === 'False').length;
  const canvas = document.getElementById('skippedPieChart');
  canvas.style.display = (won || lost) ? '' : 'none';
  document.getElementById('skipped-pie-empty').textContent =
    (won || lost) ? '' : misses.length ? 'Results not in yet.' : 'No near misses for this filter.';
  if (!won && !lost) return;   // no results yet: don't draw a fake 0%
  CHART_INSTANCES.skippedPie = new Chart(canvas, {
    type: 'doughnut',
    data: {
      labels: [`Would Have Won (${won})`, `Would Have Lost (${lost})`],
      datasets: [{ data: [won, lost], backgroundColor: [GREEN + 'cc', RED + 'cc'], borderColor: [GREEN, RED], borderWidth: 2 }]
    },
    options: { plugins: { legend: { position: 'bottom' } } }
  });
}

function renderEdge(trades) {
  // edge_at_entry is the model probability minus the price the bot SAW (its
  // cached book price, up to 5s old); the fill shows the price it really paid
  const rows = trades.map((t, i) => ({
    n: i + 1, t,
    prob: parseFloat(t.prob_at_entry), edge: parseFloat(t.edge_at_entry), fill: parseFloat(t.entry_price),
  })).filter(r => [r.prob, r.edge, r.fill].every(Number.isFinite) && r.prob > 0 && r.fill > 0).slice(-50);
  const note = document.getElementById('edge-note');
  if (!rows.length) { note.textContent = '(no trades with edge data)'; return; }
  const seen = rows.map(r => r.edge);
  const atFill = rows.map(r => r.prob - r.fill);
  const avg = a => a.reduce((s, x) => s + x, 0) / a.length;
  const paidMore = rows.filter(r => r.fill > r.prob - r.edge + 0.005).length;
  note.textContent = `(avg ${avg(seen).toFixed(3)} seen vs ${avg(atFill).toFixed(3)} at fill; paid more than the price it saw on ${paidMore} of ${rows.length} trades)`;
  CHART_INSTANCES.edge = new Chart(document.getElementById('edgeChart'), {
    type: 'bar',
    data: {
      labels: rows.map(r => r.n),
      datasets: [
        { label: 'Edge the bot saw', data: seen, backgroundColor: BLUE + 'cc', borderColor: BLUE, borderWidth: 1 },
        { label: 'Edge at fill', data: atFill, backgroundColor: GOLD + 'cc', borderColor: GOLD, borderWidth: 1 },
      ]
    },
    options: {
      plugins: {
        legend: { position: 'top' },
        tooltip: { callbacks: {
          title: ctx => { const r = rows[ctx[0].dataIndex]; return `Trade #${r.n} (${r.t.version}) ${r.t.window_time || ''} ${r.t.side}`; },
          afterBody: ctx => {
            const r = rows[ctx[0].dataIndex];
            return [`Model: ${(r.prob * 100).toFixed(0)}%`, `Saw ${cents(r.prob - r.edge)}, paid ${cents(r.fill)}`, r.t.won_resolution === 'True' ? 'WIN' : 'LOSS'];
          }
        }}
      },
      scales: { x: { title: { display: true, text: 'Trade # (chronological)' } }, y: { title: { display: true, text: 'Edge (prob − price)' } } }
    }
  });
}

function renderClosestCalls(skipped) {
  const quiet = quietWindows(skipped);
  const known = quiet.filter(q => CLOSEST_CALL_ORDER.includes(q.reason));
  const older = quiet.length - known.length;
  const canvas = document.getElementById('closestChart');
  canvas.style.display = known.length ? '' : 'none';
  document.getElementById('closest-empty').textContent = known.length
    ? (older ? `Plus ${older} older quiet window(s), logged before closest calls were recorded.` : '')
    : quiet.length ? 'These quiet windows are older: closest calls are recorded from the next run on.'
    : 'No quiet windows for this filter.';
  if (!known.length) return;
  CHART_INSTANCES.closest = new Chart(canvas, {
    type: 'bar',
    data: {
      labels: CLOSEST_CALL_ORDER.map(reasonLabel),
      datasets: [{ data: CLOSEST_CALL_ORDER.map(r => known.filter(q => q.reason === r).length),
                   backgroundColor: BLUE + 'cc', borderColor: BLUE, borderWidth: 1 }]
    },
    options: {
      indexAxis: 'y',
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: ctx => `${ctx.raw} window(s)` } } },
      scales: { x: { beginAtZero: true, ticks: { stepSize: 1 } } }
    }
  });
}

function renderNearMissTable(skipped) {
  const misses = nearMisses(skipped).slice(-30).reverse();
  const box = document.getElementById('near-miss-table');
  if (!misses.length) { box.innerHTML = '<div class="empty">No near misses for this filter.</div>'; return; }
  let html = '<table><thead><tr><th>Time</th><th>Ver</th><th>Side</th><th>Why Skipped</th><th>Model</th><th>BTC Move</th><th>Time Left</th><th>Price Seen</th><th>Would Have Paid</th><th>Result</th><th>P&L If Taken</th></tr></thead><tbody>';
  misses.forEach(m => {
    const vc = vColor(m.version);
    const result = m.would_have_won === 'True' ? '<span class="won">WIN</span>'
      : m.would_have_won === 'False' ? '<span class="lost">LOSS</span>'
      : m.would_have_won === 'Split' ? 'SPLIT' : '<span class="muted">pending</span>';
    const paid = m.price > 0 ? (m.price_checked ? cents(m.price) : `<span class="muted" title="The bot didn't check the book price, so this is the price it saw">~${cents(m.price)}</span>`) : '';
    const pnl = m.pnl_if_taken !== undefined ? `<span class="${m.pnl_if_taken >= 0 ? 'won' : 'lost'}">${money(m.pnl_if_taken)}</span> <span class="muted">on $${m.stake.toFixed(2)}</span>` : '';
    const d = m.btc_delta_pct || 0;
    html += `<tr>
      <td>${esc(m.window_time)}</td>
      <td><span class="version-badge" style="background:${vc}33;color:${vc}">${esc(m.version)}</span></td>
      <td>${esc(m.side)}</td>
      <td>${esc(reasonLabel(m.reason))}</td>
      <td>${m.prob ? (m.prob * 100).toFixed(0) + '%' : ''}</td>
      <td><span class="${d >= 0 ? 'won' : 'lost'}">${d >= 0 ? '+' : ''}${d.toFixed(3)}%</span></td>
      <td>${m.seconds_remaining ? 'T-' + m.seconds_remaining.toFixed(0) + 's' : ''}</td>
      <td>${m.seen_price > 0 ? cents(m.seen_price) : ''}</td>
      <td>${paid}</td>
      <td>${result}</td>
      <td>${pnl}</td>
    </tr>`;
  });
  box.innerHTML = html + '</tbody></table>';
}

function renderSessions(sessions) {
  const box = document.getElementById('sessions-table');
  if (!sessions.length) { box.innerHTML = '<div class="empty">No finished sessions yet.</div>'; return; }
  const when = s => new Date(String(s || '').replace(' ', 'T'));
  let html = '<table><thead><tr><th>Started</th><th>Length</th><th>Balance</th><th>Real P&L</th><th>Tracked P&L</th><th>Drift</th><th>Fees</th><th>Trades</th><th>W / L</th><th>Win Rate</th></tr></thead><tbody>';
  sessions.forEach(s => {
    const mins = (when(s.end_time) - when(s.start_time)) / 60000;
    const length = Number.isFinite(mins) ? (mins >= 60 ? `${Math.floor(mins / 60)}h ${Math.round(mins % 60)}m` : `${Math.round(mins)}m`) : '';
    const real = parseFloat(s.real_pnl || 0), tracked = parseFloat(s.tracked_pnl || 0), drift = parseFloat(s.pnl_drift || 0);
    const trades = parseInt(s.trades || 0);
    html += `<tr>
      <td>${esc(String(s.start_time || '').slice(0, 16))}</td>
      <td>${length}</td>
      <td>$${parseFloat(s.start_balance || 0).toFixed(2)} → $${parseFloat(s.end_balance || 0).toFixed(2)}</td>
      <td class="${real >= 0 ? 'won' : 'lost'}" style="font-weight:600">${money(real)}</td>
      <td>${money(tracked)}</td>
      <td class="${Math.abs(drift) >= 0.5 ? 'lost' : 'muted'}" title="Real minus tracked P&L">${money(drift)}</td>
      <td class="muted">${s.fees !== undefined && s.fees !== '' ? '$' + parseFloat(s.fees).toFixed(2) : ''}</td>
      <td>${trades}</td>
      <td>${esc(s.wins || 0)} / ${esc(s.losses || 0)}</td>
      <td>${trades ? parseFloat(s.win_rate || 0).toFixed(0) + '%' : ''}</td>
    </tr>`;
  });
  box.innerHTML = html + '</tbody></table>';
}

function renderTradesTable(trades) {
  // Compute running portfolio value across ALL trades (starting from $100 bankroll)
  const startBankroll = 100;
  let cumBal = [];
  let running = startBankroll;
  trades.forEach(t => { running += parseFloat(t.profit || 0); cumBal.push(running); });
  const startIdx = Math.max(0, trades.length - 20);
  const recent = trades.slice(-20).reverse();
  let html = '<table><thead><tr><th>Time</th><th>Ver</th><th>Side</th><th>BTC Open</th><th>BTC Move</th><th>Entry</th><th>Cost</th><th>Fee</th><th>Shares</th><th>Result</th><th>Profit</th><th>Return</th><th>Portfolio</th></tr></thead><tbody>';
  recent.forEach((t, i) => {
    const won = t.won_resolution === 'True';
    const profit = parseFloat(t.profit || 0);
    const ret = parseFloat(t.return_pct || 0);
    const v = t.version || 'v15';
    const wts = t.window_ts || '';
    const tid = t.trade_id || '';
    const vc = vColor(v);
    const delta = parseFloat(t.btc_delta_at_entry || 0);
    const deltaAbs = Math.abs(delta).toFixed(3);
    const deltaArrow = delta >= 0 ? '\u2191' : '\u2193';
    const deltaColor = delta >= 0 ? 'won' : 'lost';
    const btcFinal = parseFloat(t.btc_final_price || 0);
    const finalDelta = parseFloat(t.btc_final_delta_pct || 0);
    const btcOpen = (btcFinal > 0 && finalDelta !== 0) ? btcFinal / (1 + finalDelta / 100) : 0;
    const btcOpenStr = btcOpen > 0 ? `$${btcOpen.toLocaleString(undefined, {maximumFractionDigits: 0})}` : '';
    const finalMove = finalDelta !== 0 ? `${finalDelta >= 0 ? '+' : ''}${finalDelta.toFixed(3)}%` : '';
    const finalColor = (t.side === 'UP' ? finalDelta >= 0 : finalDelta < 0) ? 'won' : 'lost';
    const portTotal = parseFloat(t.bankroll) || cumBal[startIdx + (recent.length - 1 - i)] || startBankroll;
    html += `<tr class="clickable" data-window-ts="${wts}" data-trade-id="${tid}" data-idx="${i}" onclick="showTradeDetail(this)">
      <td>${t.window_time || ''}</td>
      <td><span class="version-badge" style="background:${vc}33;color:${vc}">${v}</span></td>
      <td>${t.side}</td>
      <td style="font-size:11px;color:#888">${btcOpenStr}</td>
      <td><span class="${finalColor}">${finalMove}</span></td>
      <td>$${parseFloat(t.entry_price).toFixed(2)}</td>
      <td>$${parseFloat(t.entry_cost).toFixed(2)}</td>
      <td class="muted">${t.fee !== undefined && t.fee !== '' ? '$' + parseFloat(t.fee).toFixed(3) : ''}</td>
      <td>${parseFloat(t.entry_shares).toFixed(0)}</td>
      <td class="${won ? 'won' : 'lost'}">${won ? 'WIN' : 'LOSS'}</td>
      <td class="${profit >= 0 ? 'won' : 'lost'}">$${profit >= 0 ? '+' : ''}${profit.toFixed(2)}</td>
      <td class="${ret >= 0 ? 'won' : 'lost'}">${ret >= 0 ? '+' : ''}${ret.toFixed(0)}%</td>
      <td class="${portTotal >= startBankroll ? 'won' : 'lost'}" style="font-weight:600">$${portTotal.toFixed(2)}</td>
    </tr>`;
  });
  html += '</tbody></table>';
  document.getElementById('trades-table').innerHTML = html;
  // Hide detail panel on re-render
  document.getElementById('trade-detail').classList.remove('visible');
}

function showTradeDetail(row, scroll = true) {
  // Highlight selected row
  document.querySelectorAll('tr.clickable').forEach(r => r.classList.remove('selected'));
  row.classList.add('selected');

  const windowTs = row.dataset.windowTs;
  const tradeId = row.dataset.tradeId;

  // Find matching ticks — match on both trade_id + window_ts (trade_id resets between sessions)
  let ticks = ALL_HOLD_TICKS.filter(tk => tradeId && tk.trade_id === tradeId && tk.window_ts === windowTs);
  if (!ticks.length) {
    ticks = ALL_HOLD_TICKS.filter(tk => tk.window_ts === windowTs);
  }

  const panel = document.getElementById('trade-detail');
  const chartsDiv = document.getElementById('detail-charts');

  // Find the trade data from the table row
  const cells = row.querySelectorAll('td');
  const time = cells[0].textContent;
  const side = cells[2].textContent;
  const entry = cells[5].textContent;
  const result = cells[9].textContent;
  const profit = cells[10].textContent;

  document.getElementById('detail-title').textContent = `${side} ${entry} @ ${time}`;
  document.getElementById('detail-sub').textContent = `${result} ${profit} | ${ticks.length} ticks recorded`;

  // Destroy old detail charts
  Object.values(DETAIL_CHARTS).forEach(c => c.destroy());
  DETAIL_CHARTS = {};

  if (!ticks.length) {
    chartsDiv.innerHTML = '<div class="no-ticks">No hold tick data for this trade (pre-v16 or pending buy)</div>';
    panel.classList.add('visible');
    if (scroll) panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    return;
  }

  // Sort ticks by seconds_remaining descending (time flows left to right: 240s → 0s)
  ticks.sort((a, b) => parseFloat(b.seconds_remaining) - parseFloat(a.seconds_remaining));
  const labels = ticks.map(tk => `T-${parseFloat(tk.seconds_remaining).toFixed(0)}s`);

  chartsDiv.innerHTML = `
    <div class="detail-chart full"><h5>BTC Price ($)</h5><canvas id="detailBtcPriceChart"></canvas></div>
    <div class="detail-chart"><h5>BTC Delta %</h5><canvas id="detailBtcChart"></canvas></div>
    <div class="detail-chart"><h5>Model Probability</h5><canvas id="detailProbChart"></canvas></div>
    <div class="detail-chart"><h5>Unrealized P&L ($)</h5><canvas id="detailPnlChart"></canvas></div>
    <div class="detail-chart"><h5>Sell Price ($)</h5><canvas id="detailSellChart"></canvas></div>
  `;

  const lineOpts = (yTitle) => ({
    animation: false,
    plugins: { legend: { display: false } },
    scales: {
      x: { ticks: { maxTicksToShow: 10, autoSkip: true, maxRotation: 0 } },
      y: { title: { display: true, text: yTitle } }
    },
    elements: { point: { radius: 2 }, line: { tension: 0.3 } }
  });

  // BTC Price
  const btcPriceData = ticks.map(tk => parseFloat(tk.btc_price));
  DETAIL_CHARTS.btcPrice = new Chart(document.getElementById('detailBtcPriceChart'), {
    type: 'line',
    data: { labels, datasets: [{ data: btcPriceData, borderColor: '#fb923c', borderWidth: 2, fill: false }] },
    options: { ...lineOpts('BTC ($)'), scales: { ...lineOpts('BTC ($)').scales, y: { title: { display: true, text: 'BTC ($)' }, ticks: { callback: v => '$' + v.toLocaleString() } } } }
  });

  // BTC Delta %
  const btcData = ticks.map(tk => parseFloat(tk.btc_delta_pct) * 100);
  DETAIL_CHARTS.btc = new Chart(document.getElementById('detailBtcChart'), {
    type: 'line',
    data: { labels, datasets: [{ data: btcData, borderColor: BLUE, borderWidth: 2, fill: { target: 'origin', above: BLUE + '22', below: RED + '22' } }] },
    options: lineOpts('BTC Delta (%)')
  });

  // Probability
  const probData = ticks.map(tk => parseFloat(tk.prob));
  DETAIL_CHARTS.prob = new Chart(document.getElementById('detailProbChart'), {
    type: 'line',
    data: { labels, datasets: [{ data: probData, borderColor: GOLD, borderWidth: 2, fill: false }] },
    options: { ...lineOpts('Probability'), scales: { ...lineOpts('Probability').scales, y: { title: { display: true, text: 'Probability' }, min: 0, max: 1 } } }
  });

  // Unrealized P&L
  const pnlData = ticks.map(tk => parseFloat(tk.unrealized_pnl));
  DETAIL_CHARTS.pnl = new Chart(document.getElementById('detailPnlChart'), {
    type: 'line',
    data: { labels, datasets: [{ data: pnlData, borderColor: GREEN, borderWidth: 2, fill: { target: 'origin', above: GREEN + '22', below: RED + '22' } }] },
    options: lineOpts('P&L ($)')
  });

  // Sell Price
  const sellData = ticks.map(tk => parseFloat(tk.sell_price));
  DETAIL_CHARTS.sell = new Chart(document.getElementById('detailSellChart'), {
    type: 'line',
    data: { labels, datasets: [{ data: sellData, borderColor: '#a78bfa', borderWidth: 2, fill: false }] },
    options: lineOpts('Sell Price ($)')
  });

  panel.classList.add('visible');
  if (scroll) panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function renderAll(trades, skipped) {
  renderStats(trades, skipped);
  renderEquityCurve(trades);
  renderWinRate(trades);
  renderPnL(trades);
  renderWaterfall(trades);
  renderEdge(trades);
  renderSkippedBar(skipped);
  renderSkippedPie(skipped);
  renderClosestCalls(skipped);
  renderNearMissTable(skipped);
  renderTradesTable(trades);
  renderSessions(ALL_SESSIONS);
}

// Fetch everything; re-render only if something changed (unless forced), keeping
// the version filter and any open trade detail
async function refresh(force) {
  const updated = document.getElementById('updated');
  let data;
  try {
    data = await loadData();
  } catch (e) {
    updated.textContent = "Couldn't reach the dashboard server";
    return;
  }
  updated.textContent = 'Updated ' + new Date().toLocaleTimeString();
  const key = JSON.stringify(data);
  if (!force && key === LAST_DATA_KEY) return;
  LAST_DATA_KEY = key;
  ALL_TRADES = data.trades;
  ALL_SKIPPED = data.skipped;
  ALL_HOLD_TICKS = data.holdTicks;
  ALL_SESSIONS = data.sessions;
  const openRow = document.getElementById('trade-detail').classList.contains('visible')
    ? document.querySelector('tr.clickable.selected') : null;
  const reopen = openRow && { w: openRow.dataset.windowTs, id: openRow.dataset.tradeId };
  const versions = buildFilterButtons(ALL_TRADES, ALL_SKIPPED);
  setFilter(versions.includes(ACTIVE_FILTER) ? ACTIVE_FILTER : 'all');
  if (reopen) {
    const row = [...document.querySelectorAll('tr.clickable')].find(r => r.dataset.windowTs === reopen.w && r.dataset.tradeId === reopen.id);
    if (row) showTradeDetail(row, false);
  }
}

function setAutoRefresh(on) {
  clearInterval(AUTO_TIMER);
  AUTO_TIMER = on ? setInterval(() => refresh(false), AUTO_REFRESH_MS) : null;
  try { localStorage.setItem('autoRefresh', on ? '1' : '0'); } catch (e) {}
}

function init() {
  let auto = true;
  try { auto = localStorage.getItem('autoRefresh') !== '0'; } catch (e) {}
  document.getElementById('autoRefresh').checked = auto;
  setAutoRefresh(auto);
  refresh(true);
}

init();
</script>
</body>
</html>
"""

# ── Main ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), DashboardHandler)
    print(f"  PolyBot Dashboard running at http://localhost:{PORT}")
    print(f"  (also reachable from other devices on your network)")
    print(f"  Press Ctrl+C to stop\n")
    webbrowser.open(f"http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Dashboard stopped.")
        server.server_close()
