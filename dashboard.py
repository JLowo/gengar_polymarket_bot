#!/usr/bin/env python3
"""PolyBot Trading Dashboard — live charts from CSV data.

Usage:
    python dashboard.py          # opens http://localhost:8050
    python dashboard.py 9000     # custom port
"""

import csv
import json
import os
import sys
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler

LOG_DIR = "logs"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8050

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
    # v15+
    for row in load_csv(os.path.join(LOG_DIR, "trades.csv")):
        v = row.get("version", "15")
        row["version"] = f"v{v}" if not str(v).startswith("v") else v
        trades.append(row)
    trades.sort(key=lambda r: float(r.get("timestamp", 0)))
    return trades

def get_skipped():
    return load_csv(os.path.join(LOG_DIR, "skipped.csv"))

def get_hold_ticks():
    return load_csv(os.path.join(LOG_DIR, "hold_ticks.csv"))

# ── HTTP handler ───────────────────────────────────────────────────

class DashboardHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/trades":
            self._json_response(get_all_trades())
        elif self.path == "/api/skipped":
            self._json_response(get_skipped())
        elif self.path == "/api/hold_ticks":
            self._json_response(get_hold_ticks())
        elif self.path == "/" or self.path == "/index.html":
            self._html_response(DASHBOARD_HTML)
        else:
            self.send_error(404)

    def _json_response(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
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
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
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
  @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } #trade-detail .detail-grid { grid-template-columns: 1fr; } }
</style>
</head>
<body>

<div class="header">
  <h1><span>PolyBot</span> Dashboard</h1>
  <button class="refresh-btn" onclick="location.reload()">Refresh</button>
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
  <div class="chart-box"><h3>Skipped Signals by Reason</h3><canvas id="skippedBarChart"></canvas></div>
  <div class="chart-box"><h3>Skipped: Would-Have-Won Rate</h3><canvas id="skippedPieChart"></canvas></div>
  <div class="chart-box full"><h3>Recent Trades <span style="font-size:11px;color:#666;font-weight:400;text-transform:none;letter-spacing:0">(click a row to see hold trajectory)</span></h3><div id="trades-table"></div><div id="trade-detail"><h4 id="detail-title"></h4><div class="detail-sub" id="detail-sub"></div><div class="detail-grid" id="detail-charts"></div></div></div>
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
let ACTIVE_FILTER = 'all';
let CHART_INSTANCES = {};
let DETAIL_CHARTS = {};

async function loadData() {
  const [trades, skipped, holdTicks] = await Promise.all([
    fetch('/api/trades').then(r => r.json()),
    fetch('/api/skipped').then(r => r.json()),
    fetch('/api/hold_ticks').then(r => r.json()),
  ]);
  return { trades, skipped, holdTicks };
}

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

function buildFilterButtons(trades) {
  const versions = [...new Set(trades.map(t => t.version))];
  const bar = document.getElementById('filter-bar');
  versions.forEach(v => {
    const btn = document.createElement('button');
    btn.className = 'filter-btn';
    btn.dataset.v = v;
    btn.textContent = v;
    btn.onclick = () => setFilter(v);
    bar.appendChild(btn);
  });
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
  const skippedWon = skipped.filter(s => s.would_have_won === 'True').length;

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
    <div class="stat-card"><div class="label">Skipped</div><div class="value">${skipped.length}</div><div class="sub">${skippedWon} would have won</div></div>
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
  if (!skipped.length) return;
  const reasons = {};
  skipped.forEach(s => {
    const r = s.reason || 'unknown';
    if (!reasons[r]) reasons[r] = { won: 0, lost: 0 };
    s.would_have_won === 'True' ? reasons[r].won++ : reasons[r].lost++;
  });
  const labels = Object.keys(reasons).map(r => r.replace(/_/g, ' '));
  CHART_INSTANCES.skippedBar = new Chart(document.getElementById('skippedBarChart'), {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { label: 'Would Have Won', data: Object.values(reasons).map(v => v.won), backgroundColor: GREEN + 'cc', borderColor: GREEN, borderWidth: 1 },
        { label: 'Would Have Lost', data: Object.values(reasons).map(v => v.lost), backgroundColor: RED + 'cc', borderColor: RED, borderWidth: 1 },
      ]
    },
    options: { plugins: { legend: { position: 'top' } }, scales: { y: { beginAtZero: true, ticks: { stepSize: 1 } } } }
  });
}

function renderSkippedPie(skipped) {
  if (!skipped.length) return;
  const won = skipped.filter(s => s.would_have_won === 'True').length;
  const lost = skipped.length - won;
  CHART_INSTANCES.skippedPie = new Chart(document.getElementById('skippedPieChart'), {
    type: 'doughnut',
    data: {
      labels: [`Would Have Won (${won})`, `Would Have Lost (${lost})`],
      datasets: [{ data: [won, lost], backgroundColor: [GREEN + 'cc', RED + 'cc'], borderColor: [GREEN, RED], borderWidth: 2 }]
    },
    options: { plugins: { legend: { position: 'bottom' } } }
  });
}

function renderTradesTable(trades) {
  const recent = trades.slice(-20).reverse();
  let html = '<table><thead><tr><th>Time</th><th>Ver</th><th>Side</th><th>Entry</th><th>Cost</th><th>Shares</th><th>Result</th><th>Profit</th><th>Return</th></tr></thead><tbody>';
  recent.forEach((t, i) => {
    const won = t.won_resolution === 'True';
    const profit = parseFloat(t.profit || 0);
    const ret = parseFloat(t.return_pct || 0);
    const v = t.version || 'v15';
    const wts = t.window_ts || '';
    const tid = t.trade_id || '';
    const vc = vColor(v);
    html += `<tr class="clickable" data-window-ts="${wts}" data-trade-id="${tid}" data-idx="${i}" onclick="showTradeDetail(this)">
      <td>${t.window_time || ''}</td>
      <td><span class="version-badge" style="background:${vc}33;color:${vc}">${v}</span></td>
      <td>${t.side}</td>
      <td>$${parseFloat(t.entry_price).toFixed(2)}</td>
      <td>$${parseFloat(t.entry_cost).toFixed(2)}</td>
      <td>${parseFloat(t.entry_shares).toFixed(0)}</td>
      <td class="${won ? 'won' : 'lost'}">${won ? 'WIN' : 'LOSS'}</td>
      <td class="${profit >= 0 ? 'won' : 'lost'}">$${profit >= 0 ? '+' : ''}${profit.toFixed(2)}</td>
      <td class="${ret >= 0 ? 'won' : 'lost'}">${ret >= 0 ? '+' : ''}${ret.toFixed(0)}%</td>
    </tr>`;
  });
  html += '</tbody></table>';
  document.getElementById('trades-table').innerHTML = html;
  // Hide detail panel on re-render
  document.getElementById('trade-detail').classList.remove('visible');
}

function showTradeDetail(row) {
  // Highlight selected row
  document.querySelectorAll('tr.clickable').forEach(r => r.classList.remove('selected'));
  row.classList.add('selected');

  const windowTs = row.dataset.windowTs;
  const tradeId = row.dataset.tradeId;

  // Find matching ticks — prefer trade_id match, fall back to window_ts
  let ticks = ALL_HOLD_TICKS.filter(tk => tradeId && tk.trade_id === tradeId);
  if (!ticks.length) {
    ticks = ALL_HOLD_TICKS.filter(tk => tk.window_ts === windowTs);
  }

  const panel = document.getElementById('trade-detail');
  const chartsDiv = document.getElementById('detail-charts');

  // Find the trade data from the table row
  const cells = row.querySelectorAll('td');
  const time = cells[0].textContent;
  const side = cells[2].textContent;
  const entry = cells[3].textContent;
  const result = cells[6].textContent;
  const profit = cells[7].textContent;

  document.getElementById('detail-title').textContent = `${side} ${entry} @ ${time}`;
  document.getElementById('detail-sub').textContent = `${result} ${profit} | ${ticks.length} ticks recorded`;

  // Destroy old detail charts
  Object.values(DETAIL_CHARTS).forEach(c => c.destroy());
  DETAIL_CHARTS = {};

  if (!ticks.length) {
    chartsDiv.innerHTML = '<div class="no-ticks">No hold tick data for this trade (pre-v16 or pending buy)</div>';
    panel.classList.add('visible');
    panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    return;
  }

  // Sort ticks by seconds_remaining descending (time flows left to right: 240s → 0s)
  ticks.sort((a, b) => parseFloat(b.seconds_remaining) - parseFloat(a.seconds_remaining));
  const labels = ticks.map(tk => `T-${parseFloat(tk.seconds_remaining).toFixed(0)}s`);

  chartsDiv.innerHTML = `
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
  panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function renderAll(trades, skipped) {
  renderStats(trades, skipped);
  renderEquityCurve(trades);
  renderWinRate(trades);
  renderPnL(trades);
  renderWaterfall(trades);
  renderSkippedBar(skipped);
  renderSkippedPie(skipped);
  renderTradesTable(trades);
}

async function init() {
  const { trades, skipped, holdTicks } = await loadData();
  ALL_TRADES = trades;
  ALL_SKIPPED = skipped;
  ALL_HOLD_TICKS = holdTicks;
  buildFilterButtons(trades);
  applyFilterColors();
  renderAll(trades, skipped);
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
    print(f"  Press Ctrl+C to stop\n")
    webbrowser.open(f"http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Dashboard stopped.")
        server.server_close()
