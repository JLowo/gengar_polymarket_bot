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

# ── HTTP handler ───────────────────────────────────────────────────

class DashboardHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/trades":
            self._json_response(get_all_trades())
        elif self.path == "/api/skipped":
            self._json_response(get_skipped())
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
  .filter-btn.active[data-v="v13"] { border-color: #ff6b6b; color: #ff6b6b; background: #ff6b6b18; }
  .filter-btn.active[data-v="v14"] { border-color: #ffd93d; color: #ffd93d; background: #ffd93d18; }
  .filter-btn.active[data-v="v15"] { border-color: #6bcb77; color: #6bcb77; background: #6bcb7718; }
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
  .v13 { background: #ff6b6b33; color: #ff6b6b; }
  .v14 { background: #ffd93d33; color: #ffd93d; }
  .v15 { background: #6bcb7733; color: #6bcb77; }
  .won { color: #6bcb77; }
  .lost { color: #ff6b6b; }
  @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
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
  <div class="chart-box full"><h3>Recent Trades</h3><div id="trades-table"></div></div>
</div>

<script>
const COLORS = { v13: '#ff6b6b', v14: '#ffd93d', v15: '#6bcb77' };
const GREEN = '#6bcb77', RED = '#ff6b6b', GOLD = '#ffd93d', BLUE = '#4ecdc4';

Chart.defaults.color = '#888';
Chart.defaults.borderColor = '#222';
Chart.defaults.font.family = '-apple-system, BlinkMacSystemFont, sans-serif';

let ALL_TRADES = [];
let ALL_SKIPPED = [];
let ACTIVE_FILTER = 'all';
let CHART_INSTANCES = {};

async function loadData() {
  const [trades, skipped] = await Promise.all([
    fetch('/api/trades').then(r => r.json()),
    fetch('/api/skipped').then(r => r.json()),
  ]);
  return { trades, skipped };
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
  // skipped only has v15 data, but filter anyway for future versions
  return skipped;
}

function buildFilterButtons(trades) {
  const versions = [...new Set(trades.map(t => t.version))];
  const bar = document.getElementById('filter-bar');
  // Keep the "All" button, add version buttons
  versions.forEach(v => {
    const btn = document.createElement('button');
    btn.className = 'filter-btn';
    btn.dataset.v = v;
    btn.textContent = v;
    btn.onclick = () => setFilter(v);
    bar.appendChild(btn);
  });
}

function setFilter(version) {
  ACTIVE_FILTER = version;
  // Update button states
  document.querySelectorAll('.filter-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.v === version);
  });
  // Destroy existing charts
  Object.values(CHART_INSTANCES).forEach(c => c.destroy());
  CHART_INSTANCES = {};
  // Re-render everything
  const trades = filterTrades(ALL_TRADES, version);
  const skipped = filterSkipped(ALL_SKIPPED, version);
  renderAll(trades, skipped);
}

function renderStats(trades, skipped) {
  const bar = document.getElementById('stats-bar');
  const totalTrades = trades.length;
  const wins = trades.filter(t => t.won_resolution === 'True').length;
  const pnl = trades.reduce((s, t) => s + parseFloat(t.profit || 0), 0);
  const wr = totalTrades > 0 ? (wins / totalTrades * 100) : 0;
  const skippedWon = skipped.filter(s => s.would_have_won === 'True').length;

  bar.innerHTML = `
    <div class="stat-card"><div class="label">Total Trades</div><div class="value">${totalTrades}</div><div class="sub">${wins}W / ${totalTrades - wins}L</div></div>
    <div class="stat-card"><div class="label">Win Rate</div><div class="value ${wr >= 70 ? 'green' : wr >= 50 ? 'gold' : 'red'}">${wr.toFixed(0)}%</div><div class="sub">~70% breakeven</div></div>
    <div class="stat-card"><div class="label">Total P&L</div><div class="value ${pnl >= 0 ? 'green' : 'red'}">$${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}</div></div>
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
        borderColor: COLORS[prevV] || BLUE,
        backgroundColor: (COLORS[prevV] || BLUE) + '11',
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
    colors.push(COLORS[v] || BLUE);
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
  recent.forEach(t => {
    const won = t.won_resolution === 'True';
    const profit = parseFloat(t.profit || 0);
    const ret = parseFloat(t.return_pct || 0);
    const v = t.version || 'v15';
    html += `<tr>
      <td>${t.window_time || ''}</td>
      <td><span class="version-badge ${v}">${v}</span></td>
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
  const { trades, skipped } = await loadData();
  ALL_TRADES = trades;
  ALL_SKIPPED = skipped;
  buildFilterButtons(trades);
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
