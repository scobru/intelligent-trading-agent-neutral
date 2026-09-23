"""
Notifiche e comandi Telegram (opzionali).

Come nei bot fratelli: report a ogni ciclo, allarme in caso di errore e un
piccolo set di comandi. I comandi sono accettati solo dalla chat
configurata in TELEGRAM_CHAT_ID: /run fa partire un ciclo che puo' firmare.
"""

import html
import logging
import os
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else ""
SEP = "━━━━━━━━━━━━━━━━━━━━━━"

_run_lock = threading.Lock()


def is_configured() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def send_telegram_message(text: str, chat_id: Optional[str] = None, parse_mode: str = "HTML") -> bool:
    if not TELEGRAM_BOT_TOKEN:
        return False
    target = chat_id or TELEGRAM_CHAT_ID
    if not target:
        return False
    try:
        resp = requests.post(f"{API_BASE_URL}/sendMessage", json={
            "chat_id": target, "text": text, "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }, timeout=10)
        ok = resp.json().get("ok")
        if not ok:
            logger.warning("[Telegram] errore API: %s", resp.text[:200])
        return bool(ok)
    except Exception as exc:
        logger.warning("[Telegram] invio fallito: %s", exc)
        return False


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _pair_label(result: Dict[str, Any]) -> str:
    return _e(result.get("asset") or result.get("symbol") or "")


def _mode_line(status: Dict[str, Any]) -> Optional[str]:
    mode = status.get("mode")
    if mode == "paper":
        return f"📝 <i>PAPER — P&amp;L ${status.get('pnl_since_start_usd', 0.0):+.2f}</i>"
    if mode == "dry_run":
        return "🧪 <i>DRY-RUN: nessuna transazione firmata</i>"
    return None


def _gas_line(status: Dict[str, Any]) -> Optional[str]:
    """Avviso quando l'ETH per il gas nel wallet sta finendo (non in paper)."""
    eth = status.get("eth_balance")
    if status.get("mode") == "paper" or eth is None:
        return None
    if float(eth) < float(os.getenv("GAS_WARN_ETH", "0.002")):
        return f"⛽ <b>ETH per il gas: {float(eth):.5f}</b> ⚠️ ricarica il wallet"
    return None


def format_positions(status: Dict[str, Any], limit: int = 6) -> List[str]:
    lines = []
    for p in status.get("positions", [])[:limit]:
        apr = p.get("apr_now")
        apr_txt = f"{apr:+.2f}%" if apr is not None else "n/d"
        lines.append(f"   • {_e(p.get('asset'))}: ${p.get('value_usd', 0):.2f} "
                     f"(P&amp;L {p.get('pnl_usd', 0):+.2f}) | funding {apr_txt} | "
                     f"leva {p.get('effective_leverage', 0):.2f}x")
    return lines


def notify_cycle_result(decision: Dict[str, Any], result: Dict[str, Any],
                        status: Dict[str, Any], exits: List[Dict[str, Any]] = None):
    if not is_configured():
        return
    op = str(result.get("operation") or decision.get("operation") or "hold").lower()
    res_status = str(result.get("status", "")).lower()

    titles = {
        "open": f"🟢 <b>APERTA COPPIA: {_pair_label(result)}</b> (spot + short)",
        "close": f"🔴 <b>CHIUSA COPPIA: {_pair_label(result)}</b>",
    }
    lines = ["⚖️ <b>Neutral Agent • Report Ciclo</b>", SEP,
             titles.get(op, "⏸️ <b>HOLD: nessuna operazione</b>")]
    mode = _mode_line(status)
    if mode:
        lines.append(mode)
    gas = _gas_line(status)
    if gas:
        lines.append(gas)

    if op != "hold":
        if res_status == "rejected":
            lines.append(f"🛑 <b>Rifiutato dai limiti:</b> {_e(result.get('reason'))}")
        elif res_status == "error":
            lines.append(f"❌ <b>Errore:</b> {_e(result.get('reason'))}")
        else:
            if result.get("amount_usd") is not None:
                lines.append(f"💵 <b>Capitale:</b> ${float(result['amount_usd']):.2f}")
            if result.get("apr") is not None:
                lines.append(f"📈 <b>Funding atteso:</b> {float(result['apr']):+.2f}% APR, costi ripagati in "
                             f"{float(result.get('breakeven_days') or 0):.1f} giorni")
            if result.get("pnl_usd") is not None:
                lines.append(f"💰 <b>P&amp;L coppia:</b> {float(result['pnl_usd']):+.2f} "
                             f"(funding {float(result.get('funding_usd') or 0):+.2f})")
            for tx in (result.get("transactions") or []):
                if tx.get("explorer"):
                    lines.append(f"🔗 <a href=\"{tx['explorer']}\">{_e(tx.get('description'))}</a>")

    for ex in exits or []:
        lines.append(f"🚨 Uscita automatica {_pair_label(ex)}: {_e(ex.get('trigger'))} ({_e(ex.get('status'))})")

    lines += ["", f"💰 <b>Portafoglio:</b> ${status.get('total_value_usd', 0):.2f} "
                  f"(inattivo ${status.get('idle_usd', 0):.2f})",
              f"⚖️ <b>Coperto:</b> ${status.get('hedged_notional_usd', 0):.2f} @ "
              f"{status.get('weighted_apr', 0):+.2f}% → ~${status.get('est_daily_funding_usd', 0):.2f}/giorno"]
    lines += format_positions(status)
    lines += ["", "🧠 <b>Motivazione AI:</b>", f"<i>{_e(decision.get('reason'))}</i>", SEP]
    send_telegram_message("\n".join(lines))


def notify_error(error_msg: str):
    if not is_configured():
        return
    send_telegram_message(
        f"⚠️ <b>Allarme Neutral Agent</b>\n{SEP}\n"
        f"Errore durante il ciclo:\n<code>{_e(error_msg)[:3000]}</code>\n{SEP}\n"
        "<i>Il bot riprovera' al prossimo intervallo.</i>"
    )


# ---------------------------------------------------------------- comandi

def _latest():
    import db_utils
    return db_utils.fetch_dashboard_data(limit=5)


def _handle_command(text: str, chat_id: str):
    cmd = text.strip().split()[0].lower().split("@")[0]

    if cmd in ("/start", "/help"):
        send_telegram_message(
            "⚖️ <b>Neutral Agent</b>\n\n"
            "• /status - valore, capitale coperto, funding medio\n"
            "• /positions - coppie spot + short aperte\n"
            "• /funding - funding attuale degli short su SynFutures\n"
            "• /last - ultima decisione dell'AI\n"
            "• /run - esegue subito un ciclo\n", chat_id=chat_id)

    elif cmd == "/status":
        data = _latest()
        status = data.get("status")
        if not status:
            send_telegram_message("ℹ️ Nessuno snapshot ancora: il primo ciclo non e' terminato.", chat_id=chat_id)
            return
        lines = ["📊 <b>Stato Neutral Agent</b>", SEP]
        mode = _mode_line(status)
        if mode:
            lines.append(mode)
        lines += [f"💰 Totale: ${status['total_value_usd']:.2f}",
                  f"💤 Inattivo: ${status['idle_usd']:.2f}",
                  f"⚖️ Coperto: ${status['hedged_notional_usd']:.2f} @ {status['weighted_apr']:+.2f}%",
                  f"📅 Funding stimato: ${status['est_daily_funding_usd']:.2f}/giorno",
                  f"🕒 {_e(data.get('snapshot_at'))}", SEP]
        send_telegram_message("\n".join(lines), chat_id=chat_id)

    elif cmd == "/positions":
        status = _latest().get("status") or {}
        rows = format_positions(status, limit=20)
        send_telegram_message("\n".join(["⚖️ <b>Coppie</b>", SEP] + (rows or ["nessuna"])), chat_id=chat_id)

    elif cmd == "/funding":
        rows = _latest().get("funding_latest", [])
        lines = ["📡 <b>Funding short (SynFutures)</b>", SEP]
        for r in rows:
            lines.append(f"• {_e(r['asset'])}: {r['short_apr']:+.2f}% APR | premio {r['premium_pct']:+.4f}%")
        if not rows:
            lines.append("nessuna osservazione")
        send_telegram_message("\n".join(lines), chat_id=chat_id)

    elif cmd == "/last":
        ops = _latest().get("operations", [])
        if not ops:
            send_telegram_message("ℹ️ Nessuna decisione salvata.", chat_id=chat_id)
            return
        o = ops[0]
        send_telegram_message(
            f"🧠 <b>Ultima decisione</b>\n{SEP}\n"
            f"📍 {_e((o.get('operation') or '').upper())} {_e(o.get('asset'))}\n"
            f"📌 Esito: {_e(o.get('status'))} {_e(o.get('result_reason') or '')}\n"
            f"🕒 {_e(o.get('created_at'))}\n\n<i>{_e(o.get('llm_reason'))}</i>", chat_id=chat_id)

    elif cmd == "/run":
        if not _run_lock.acquire(blocking=False):
            send_telegram_message("⏳ Un ciclo e' gia' in corso.", chat_id=chat_id)
            return
        send_telegram_message("⚡ <b>Avvio ciclo...</b> riceverai il report a breve.", chat_id=chat_id)

        def _run():
            try:
                subprocess.run([sys.executable, "main.py"], check=False)
            finally:
                _run_lock.release()
        threading.Thread(target=_run, daemon=True).start()

    else:
        send_telegram_message("Comando non riconosciuto. Usa /help.", chat_id=chat_id)


def run_telegram_listener():
    if not TELEGRAM_BOT_TOKEN:
        logger.info("[Telegram] TELEGRAM_BOT_TOKEN non impostato: listener disattivato.")
        return
    print("📱 Telegram listener avviato.")
    offset = 0
    while True:
        try:
            resp = requests.get(f"{API_BASE_URL}/getUpdates",
                                params={"offset": offset, "timeout": 25}, timeout=35)
            data = resp.json()
            if not data.get("ok"):
                time.sleep(3)
                continue
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message") or {}
                text = msg.get("text")
                chat_id = str((msg.get("chat") or {}).get("id", ""))
                if not text or not text.startswith("/"):
                    continue
                if TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID):
                    logger.warning("[Telegram] comando ignorato da chat non autorizzata %s", chat_id)
                    continue
                try:
                    _handle_command(text, chat_id)
                except Exception as exc:
                    send_telegram_message(f"❌ Errore: {_e(exc)}", chat_id=chat_id)
        except Exception:
            time.sleep(5)


if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN:
        print("⚠️ TELEGRAM_BOT_TOKEN non configurato nel .env.")
    else:
        run_telegram_listener()
