"""
Dashboard web: valore del portafoglio, coppie spot + short aperte, funding
di SynFutures (attuale e storico), storico delle operazioni ed errori.

Solo libreria standard, come nei bot fratelli; stile e helper arrivano
da static/dashboard.css e static/dashboard.js, identici nei quattro
repository. Il pulsante "Esegui ciclo" e' attivo solo se
DASHBOARD_RUN_TOKEN e' impostato: la dashboard non ha login e un ciclo
puo' firmare transazioni.
"""

import hmac
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

import config  # noqa: E402
import db_utils  # noqa: E402

PORT = int(os.getenv("DASHBOARD_PORT", os.getenv("PORT", "3000")))
RUN_TOKEN = os.getenv("DASHBOARD_RUN_TOKEN", "")
# Sotto MIN_ETH_RESERVE il bot non opera; sotto GAS_WARN_ETH la dashboard e
# Telegram chiedono di ricaricare il wallet
GAS_WARN_ETH = float(os.getenv("GAS_WARN_ETH", "0.002"))

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC_ROUTES = {
    "/favicon.ico": ("favicon.ico", "image/x-icon"),
    "/static/icon.svg": ("icon.svg", "image/svg+xml"),
    "/static/icon-small.svg": ("icon-small.svg", "image/svg+xml"),
    "/static/icon-192.png": ("icon-192.png", "image/png"),
    "/static/icon-512.png": ("icon-512.png", "image/png"),
    "/static/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
    "/static/site.webmanifest": ("site.webmanifest", "application/manifest+json"),
    "/static/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
    "/static/dashboard.js": ("dashboard.js", "application/javascript; charset=utf-8"),
}

_run_lock = threading.Lock()

HTML = r"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Neutral Agent</title>
<link rel="icon" href="/favicon.ico" sizes="48x48">
<link rel="icon" href="/static/icon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
<link rel="manifest" href="/static/site.webmanifest">
<meta name="theme-color" content="#06b6d4">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/dashboard.css?v=3">
<style>:root { --primary: #06b6d4; --accent: #0ea5e9; }</style>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script src="/static/dashboard.js?v=3"></script>
</head>
<body>
<header class="header">
  <div class="brand">
    <img src="/static/icon.svg" alt="">
    <div>
      <h1>Neutral Agent <span class="badge b-no" id="mode">…</span></h1>
      <p class="tagline">Delta-neutral su Base • spot Uniswap V3 + short SynFutures V3 • OpenRouter • SQLite</p>
    </div>
  </div>
  <div class="header-actions">
    <span class="updated" id="updated"></span>
    <button class="btn" id="run">⚡ Esegui ciclo ora</button>
  </div>
</header>

<section class="card paper-panel" id="paper-panel" hidden>
  <div class="card-head"><h2>📝 Paper trading <small>portafoglio virtuale, funding e prezzi reali</small></h2></div>
  <div class="paper-grid" id="paper-grid"></div>
  <p class="note" id="paper-note"></p>
</section>

<section class="card wallet-bar" id="wallet-panel" hidden>
  <div class="wallet-items" id="wallet-items"></div>
  <p class="note" id="wallet-note" hidden></p>
</section>

<section class="stats">
  <div class="card"><h3>Valore totale</h3><div class="value" id="total">--</div><div class="sub" id="total-sub"></div></div>
  <div class="card"><h3>Capitale coperto</h3><div class="value" id="hedged">--</div><div class="sub" id="hedged-sub"></div></div>
  <div class="card"><h3>Funding medio ponderato</h3><div class="value" id="apr">--</div><div class="sub" id="daily"></div></div>
  <div class="card"><h3>Ultima decisione</h3><div class="value" id="last-op">--</div><div class="sub" id="last-op-time">nessuna operazione registrata</div></div>
</section>

<section class="card section">
  <div class="card-head">
    <div class="tabs" data-tabs="chart">
      <button class="tab active" data-tab="equity">💼 Andamento capitale</button>
      <button class="tab" data-tab="funding">📡 Funding degli short</button>
    </div>
  </div>
  <div class="chart-box tall"><canvas id="chart"></canvas></div>
</section>

<div class="grid-2">
  <section class="card">
    <div class="card-head"><h2>⚖️ Coppie aperte</h2><small id="pos-note">spot + short</small></div>
    <div class="table-wrap"><table>
      <thead><tr><th>Asset</th><th>Spot</th><th>Short</th><th>Mark</th><th>Equity short</th><th>Leva eff.</th><th>Valore</th><th>P&amp;L</th><th>Funding</th><th>Aperta da</th></tr></thead>
      <tbody id="positions"></tbody>
    </table></div>
  </section>
  <section class="card">
    <div class="card-head"><h2>🧠 Ultima decisione AI</h2></div>
    <div class="decision-box">
      <div class="title" id="ai-action">In attesa del primo ciclo...</div>
      <div class="desc" id="ai-reason">L'agente valutera' il funding al prossimo intervallo o con "Esegui ciclo ora".</div>
    </div>
    <div class="kv">
      <div><b>Modello:</b> OpenRouter</div>
      <div><b>Input:</b> funding SynFutures (attuale e medio) + coppie + limiti di rischio</div>
    </div>
  </section>
</div>

<h2 class="section-title">📡 Funding su SynFutures <small>quanto incassa uno short, per asset</small></h2>
<section class="card section">
  <div class="table-wrap"><table>
    <thead><tr><th>Asset</th><th>Strumento</th><th>Spot</th><th>Mark</th><th>Fair</th><th>Premio</th><th>OI long / short</th><th>APR short ora</th><th>Aggiornato</th></tr></thead>
    <tbody id="funding"></tbody>
  </table></div>
  <p class="note">Il funding di SynFutures V3 e' continuo: quando il prezzo fair dell'AMM supera il mark, i long pagano gli short e il totale si divide fra gli short. L'agente entra solo su una media stabile, non su una singola lettura.</p>
</section>

<section class="card section">
  <div class="card-head"><h2>📜 Storico operazioni</h2></div>
  <div class="table-wrap"><table>
    <thead><tr><th>Data (UTC)</th><th>Operazione</th><th>Asset</th><th>Capitale</th><th>APR</th><th>Esito</th><th>Motivazione</th></tr></thead>
    <tbody id="ops"></tbody>
  </table></div>
</section>

<section class="card section">
  <div class="card-head"><h2>⚠️ Errori recenti</h2></div>
  <div class="table-wrap"><table>
    <thead><tr><th>Data (UTC)</th><th>Tipo</th><th>Messaggio</th></tr></thead>
    <tbody id="errors"></tbody>
  </table></div>
</section>

<footer class="footer">Neutral Agent • delta-neutral su Base (Uniswap V3 + SynFutures V3) &amp; OpenRouter • CapRover &amp; Docker</footer>

<script>
const { $, esc, usd, signedUsd, pct, signedPct, price, cls, time, empty, sideBadge, statusBadge } = ITA;
let chart = null, data = null, chartTab = 'equity';

function renderStatus(s) {
  if (!s) { $('positions').innerHTML = empty(10, 'In attesa del primo ciclo.'); return; }
  $('total').textContent = usd(s.total_value_usd);
  const pnl = s.pnl_since_start_usd;
  $('total-sub').innerHTML = s.mode === 'paper' && pnl != null
    ? `<span class="${cls(pnl)}">${signedUsd(pnl)}</span> dall'inizio (paper)`
    : `inattivo ${usd(s.idle_usd)}${s.gate_usdc != null ? ' • sul Gate ' + usd(s.gate_usdc) : ''}`;
  const pos = s.positions || [];
  $('hedged').textContent = usd(s.hedged_notional_usd);
  $('hedged-sub').textContent = pos.length + (pos.length === 1 ? ' coppia aperta' : ' coppie aperte');
  $('apr').innerHTML = `<span class="${cls(s.weighted_apr)}">${signedPct(s.weighted_apr)}</span>`;
  $('daily').textContent = '~' + usd(s.est_daily_funding_usd) + ' / giorno al tasso attuale';
  $('pos-note').textContent = s.mode === 'paper' ? 'portafoglio virtuale' : 'spot Uniswap + short SynFutures';
  $('positions').innerHTML = pos.map(p => {
    const lev = Number(p.effective_leverage);
    const levCls = lev > 4 ? 'neg' : lev > 3 ? 'muted' : '';
    return `<tr><td><b>${esc(p.asset)}</b><br><small>${esc(p.symbol)}</small></td>
      <td class="num">${Number(p.spot_qty).toFixed(5)}<br><small>${usd(p.spot_value_usd)}</small></td>
      <td class="num">${Number(p.perp_size).toFixed(5)}<br><small>@ ${price(p.perp_entry)}</small></td>
      <td class="num">${price(p.mark_price)}</td>
      <td class="num">${usd(p.perp_equity_usd)}</td>
      <td class="num ${levCls}">${isFinite(lev) ? lev.toFixed(2) + 'x' : '∞'}</td>
      <td class="num">${usd(p.value_usd)}</td>
      <td class="num ${cls(p.pnl_usd)}"><b>${signedUsd(p.pnl_usd)}</b></td>
      <td class="num ${cls(p.apr_now)}">${signedPct(p.apr_now)}<br><small>ingresso ${signedPct(p.entry_apr)}</small></td>
      <td class="num">${Number(p.held_hours || 0).toFixed(0)}h</td></tr>`;
  }).join('') || empty(10, "Nessuna coppia aperta: il capitale e' in USDC.");
}

function renderFunding(rows) {
  $('funding').innerHTML = rows.map(r => `<tr><td><b>${esc(r.asset)}</b></td><td class="mono">${esc(r.symbol)}</td>
    <td class="num">${price(r.spot_price)}</td><td class="num">${price(r.mark_price)}</td><td class="num">${price(r.fair_price)}</td>
    <td class="num ${cls(r.premium_pct)}">${signedPct(r.premium_pct, 4)}</td>
    <td class="num">${Number(r.total_long || 0).toFixed(2)} / ${Number(r.total_short || 0).toFixed(2)}</td>
    <td class="num ${cls(r.short_apr)}"><b>${signedPct(r.short_apr)}</b></td>
    <td class="num">${esc(time(r.created_at))}</td></tr>`).join('') || empty(9, 'Nessuna osservazione del funding.');
}

function renderChart() {
  if (chartTab === 'equity') {
    const pts = data?.equity || [];
    chart = ITA.lineChart(chart, $('chart'), pts.map(p => time(p.created_at)), [
      { label: 'Valore totale', data: pts.map(p => p.total_value_usd) },
      { label: 'Capitale coperto', data: pts.map(p => p.hedged_notional_usd) },
    ]);
    if (chart) chart.options.scales.y.ticks.callback = (v) => '$' + v;
  } else {
    const series = data?.funding_series || [];
    const labels = [...new Set(series.map(r => time(r.created_at)))];
    const assets = [...new Set(series.map(r => r.asset))];
    chart = ITA.lineChart(chart, $('chart'), labels, assets.map(a => {
      const byTime = Object.fromEntries(series.filter(r => r.asset === a).map(r => [time(r.created_at), r.short_apr]));
      return { label: a + ' APR short %', data: labels.map(l => byTime[l] ?? null) };
    }));
    if (chart) {
      chart.options.scales.y.ticks.callback = (v) => v + '%';
      chart.options.plugins.tooltip.callbacks.label = (c) => ' ' + c.dataset.label + ': ' + signedPct(c.parsed.y);
    }
  }
  chart && chart.update('none');
}

function renderOps(ops) {
  const last = ops[0];
  if (last) {
    $('last-op').textContent = (last.operation || '--').toUpperCase() + (last.asset ? ' ' + last.asset : '');
    $('last-op-time').textContent = time(last.created_at) + ' UTC • ' + (last.status || '');
    $('ai-action').textContent = `${(last.operation || '').toUpperCase()} ${last.asset || ''}`;
    $('ai-reason').textContent = last.llm_reason || last.result_reason || 'Nessuna spiegazione salvata.';
  }
  $('ops').innerHTML = ops.map(o => `<tr><td class="num">${esc(time(o.created_at))}</td>
    <td>${sideBadge(o.operation)}${o.trigger ? ' <small>' + esc(o.trigger) + '</small>' : ''}</td>
    <td><b>${esc(o.asset || '')}</b></td><td class="num">${usd(o.amount_usd)}</td>
    <td class="num">${o.apr != null ? signedPct(o.apr) : '--'}</td><td>${statusBadge(o.status)}</td>
    <td class="reason">${esc(o.llm_reason || '')}${o.result_reason ? '<br><span class="neg">' + esc(o.result_reason) + '</span>' : ''}</td></tr>`).join('')
    || empty(7, 'Nessuna operazione registrata.');
}

async function load() {
  try { data = await (await fetch('/api/data')).json(); }
  catch (e) { $('updated').textContent = 'dashboard non raggiungibile'; return; }
  ITA.renderMeta(data.meta);
  renderStatus(data.status);
  renderFunding(data.funding_latest || []);
  renderChart();
  renderOps(data.operations || []);
  ITA.renderErrors(data.errors);
}

ITA.setupTabs('chart', (t) => { chartTab = t; if (chart) { chart.destroy(); chart = null; } renderChart(); });
ITA.setupRun(load);
load();
setInterval(load, 30000);
</script>
</body>
</html>
"""


def build_meta(data):
    """Blocco "meta" nello schema comune alle dashboard dei quattro agenti."""
    status = data.get("status") or {}
    mode = status.get("mode")
    if not mode:
        mode = "paper" if config.PAPER_TRADING else ("dry_run" if config.DRY_RUN else None)
    paper = None
    if mode == "paper":
        p = status.get("paper") or {}
        paper = {
            "initial_usd": p.get("initial_usdc"),
            "value_usd": status.get("total_value_usd"),
            "pnl_usd": status.get("pnl_since_start_usd"),
            "operations": p.get("operations"),
            "costs_usd": p.get("costs_usd"),
            "costs_label": "Costi simulati",
            "started_at": p.get("created_at"),
            "extra": [
                ["Funding incassato", f"${float(p.get('funding_earned_usd') or 0.0):,.4f}"],
                ["Capitale coperto", f"${float(status.get('hedged_notional_usd') or 0.0):,.2f}"],
            ],
            "note": "Spot al prezzo oracolo e short al prezzo fair di SynFutures; a ogni ciclo lo short matura "
                    "il funding reale osservato. Swap, fee perp e gas sono stime (EST_*_BPS, PAPER_GAS_USD).",
        }
    wallet = None
    if mode != "paper" and status.get("eth_balance") is not None:
        eth = float(status["eth_balance"])
        eth_px = next((p.get("spot_price") for p in status.get("positions", []) if p.get("asset") == "ETH"), None)
        if not eth_px:
            eth_px = next((r.get("spot_price") for r in data.get("funding_latest", []) if r.get("asset") == "ETH"), None)
        wallet = {
            "address": status.get("wallet"),
            "eth": eth,
            "eth_usd": eth * float(eth_px) if eth_px else None,
            "min_eth": config.MIN_ETH_RESERVE,
            "warn_eth": GAS_WARN_ETH,
            "extra": [["USDC nel wallet", f"${float(status.get('usdc_balance') or 0):,.2f}"],
                      ["USDC sul Gate", f"${float(status.get('gate_usdc') or 0):,.2f}"]],
        }
    return {"mode": mode, "updated_at": data.get("snapshot_at"),
            "run_enabled": bool(RUN_TOKEN), "paper": paper, "wallet": wallet}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _json(self, code: int, payload):
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path in ("/api/data", "/api/status"):
            try:
                data = db_utils.fetch_dashboard_data()
            except Exception as exc:
                self._json(500, {"error": str(exc)})
                return
            data["run_enabled"] = bool(RUN_TOKEN)
            data["meta"] = build_meta(data)
            data["is_paused"] = db_utils.is_bot_paused()
            data["pause_info"] = db_utils.get_pause_info()
            self._json(200, data)
        elif path == "/health":
            self._json(200, {"status": "healthy", "service": "neutral-dashboard"})
        elif path in STATIC_ROUTES:
            filename, content_type = STATIC_ROUTES[path]
            try:
                with open(os.path.join(STATIC_DIR, filename), "rb") as fh:
                    payload = fh.read()
            except OSError:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_response(404)
            self.end_headers()

    def _is_auth_valid(self) -> bool:
        if not RUN_TOKEN:
            return False
        token = self.headers.get("X-Run-Token", "") or self.headers.get("X-Admin-Token", "")
        if not token and "Authorization" in self.headers:
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                token = auth[7:].strip()
            else:
                token = auth.strip()
        return bool(token and hmac.compare_digest(token, RUN_TOKEN))

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/api/run", "/api/pause", "/api/resume", "/api/release_funds"):
            self.send_response(404)
            self.end_headers()
            return

        if not self._is_auth_valid():
            self._json(403, {"message": "Token non valido o DASHBOARD_RUN_TOKEN non configurato."})
            return

        if path == "/api/pause":
            reason = "Pausa richiesta da API"
            try:
                clen = int(self.headers.get("Content-Length", 0))
                if clen > 0:
                    body = json.loads(self.rfile.read(clen).decode("utf-8"))
                    reason = body.get("reason", reason)
            except Exception:
                pass
            db_utils.set_bot_paused(True, reason=reason)
            self._json(200, {"status": "success", "is_paused": True, "message": f"Bot in pausa: {reason}"})
            return

        if path == "/api/resume":
            db_utils.set_bot_paused(False)
            self._json(200, {"status": "success", "is_paused": False, "message": "Bot riattivato con successo."})
            return

        if path == "/api/run":
            if db_utils.is_bot_paused():
                pinfo = db_utils.get_pause_info()
                self._json(200, {
                    "status": "paused",
                    "is_paused": True,
                    "message": f"Bot attualmente in PAUSA ({pinfo.get('reason', 'Pausa attiva')}). Ciclo ignorato."
                })
                return

            if not _run_lock.acquire(blocking=False):
                self._json(409, {"message": "Un ciclo e' gia' in corso."})
                return

            def _run():
                try:
                    subprocess.run([sys.executable, "main.py"], check=False)
                finally:
                    _run_lock.release()
            threading.Thread(target=_run, daemon=True).start()
            self._json(200, {"message": "Ciclo avviato: la dashboard si aggiorna da sola."})

        if path == "/api/release_funds":
            target_amount = 0.0
            try:
                clen = int(self.headers.get("Content-Length", 0))
                if clen > 0:
                    body = json.loads(self.rfile.read(clen).decode("utf-8"))
                    target_amount = float(body.get("amount_usd", 0.0) or body.get("amount", 0.0))
            except Exception:
                pass
            try:
                from base_client import BaseClient
                from neutral_manager import NeutralManager
                client = BaseClient()
                manager = NeutralManager(client)
                res = manager.release_funds(target_usdc=target_amount)
                self._json(200, res)
            except Exception as exc:
                self._json(500, {"status": "error", "message": str(exc)})
            return


def run_dashboard(port: int = PORT):
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"🌐 Dashboard attiva su http://0.0.0.0:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run_dashboard()
