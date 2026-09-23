"""
Un ciclo dell'agente delta-neutral: funding, stato delle coppie, uscite di
rischio, decisione, esecuzione.

Fratello dei bot perp (SynFutures), spot (degen) e yield: stessa struttura
e stesso ciclo decisionale. Qui non si specula sul prezzo e non si presta:
si tiene spot contro short e si incassa il funding quando i long pagano.
"""

import json
import os
import sys

import config
import db_utils
import funding
from neutral_agent import OPENROUTER_MODEL, decide_action
from neutral_manager import NeutralManager
from synfutures_client import SynFuturesClient


def _positions_for_prompt(status):
    lines = []
    for p in status["positions"]:
        lines.append(
            f"- {p['asset']} ({p['symbol']}) | spot {p['spot_qty']:.6f} (${p['spot_value_usd']:.2f}) | "
            f"short {p['perp_size']:.6f} @ ${p['perp_entry']:,.2f}, mark ${p['mark_price']:,.2f} | "
            f"equity short ${p['perp_equity_usd']:.2f}, leva effettiva {p['effective_leverage']:.2f}x | "
            f"valore ${p['value_usd']:.2f} (P&L {p['pnl_usd']:+.2f}) | APR ora "
            f"{p['apr_now'] if p['apr_now'] is not None else 'n/d'}%, ingresso {p.get('entry_apr')}% | "
            f"aperta da {p['held_hours']:.0f}h"
        )
    return "\n".join(lines) or "nessuna coppia aperta"


def _client():
    if config.PAPER_TRADING and not config.WALLET_ADDRESS:
        return None  # in paper il wallet non serve
    from base_client import BaseClient
    client = BaseClient()
    if not client.is_connected():
        raise RuntimeError(f"RPC Base non raggiungibile: {config.BASE_RPC_URL}")
    problems = client.verify_contracts()
    if problems:
        raise RuntimeError("Verifica dei contratti fallita:\n  - " + "\n  - ".join(problems))
    print(f"⛓️  Connesso a Base (chain {client.chain_id()}), gas {client.gas_price_gwei():.4f} gwei")
    return client


def run_cycle():
    print(f"🚀 Avvio Neutral Agent su Base (wallet: {config.WALLET_ADDRESS or 'paper'})")
    if config.PAPER_TRADING:
        print(f"📝 PAPER: portafoglio virtuale, funding e prezzi reali di SynFutures. "
              f"Capitale iniziale ${config.PAPER_START_USDC:.2f}.")
    elif config.DRY_RUN:
        print("🧪 DRY-RUN attivo: nessuna transazione verra' firmata.")

    client = None if config.PAPER_TRADING else _client()
    sf = SynFuturesClient()
    manager = NeutralManager(client, sf)

    # 1. Funding
    print("📡 Lettura del funding su SynFutures...")
    markets = manager.load_markets()
    for asset in config.NEUTRAL_ASSETS:
        m = markets.get(asset)
        if m:
            print(f"   {asset}: APR short ora {m['short_apr']:+.2f}%, media {m['avg_apr']:+.2f}% "
                  f"su {m['observations']} osservazioni")
        else:
            print(f"   {asset}: nessun perpetual in USDC su SynFutures")
    try:
        db_utils.log_funding(markets)
    except Exception as db_err:
        print(f"[db_utils] funding non salvato: {db_err}")

    # 2. Stato
    print("👛 Lettura delle coppie...")
    status = manager.get_account_status(markets)
    print(f"   Valore ${status['total_value_usd']:.2f} | inattivo ${status['idle_usd']:.2f} | "
          f"coperto ${status['hedged_notional_usd']:.2f} @ {status['weighted_apr']:.2f}% APR")

    # 3. Uscite di rischio, prima di sentire il modello
    exit_results = []
    for pos in manager.risk_exits(status):
        print(f"🚨 {pos['trigger']} su {pos['asset']}: chiusura della coppia")
        if config.AUTO_RISK_EXITS:
            result = manager.close_pair(pos["asset"], markets.get(pos["asset"]), pos["trigger"])
            exit_results.append(result)
            try:
                db_utils.log_operation({"operation": "close", "asset": pos["asset"],
                                        "reason": f"uscita automatica: {pos['trigger']}"}, result)
            except Exception as db_err:
                print(f"[db_utils] uscita non salvata: {db_err}")
    if exit_results:
        status = manager.get_account_status(markets)

    try:
        db_utils.log_snapshot(status)
    except Exception as db_err:
        print(f"[db_utils] snapshot non salvato: {db_err}")

    # 4. Decisione
    context = (
        f"<funding>\n{funding.format_for_prompt(markets)}\n</funding>\n\n"
        f"<coppie>\n{_positions_for_prompt(status)}\n</coppie>\n\n"
        f"<limiti_esecutore>\n"
        f"leva short {config.PERP_LEVERAGE:.1f}x, max {config.MAX_ASSET_PCT:.0%} per asset, capitale per coppia "
        f"${config.MIN_POSITION_USD:.0f}-${config.MAX_POSITION_USD:.0f}, APR medio minimo per entrare "
        f"{config.MIN_ENTRY_APR:.1f}% su almeno {config.FUNDING_MIN_OBSERVATIONS} osservazioni, costi ripagati "
        f"entro {config.MAX_BREAKEVEN_DAYS:.0f} giorni, uscita automatica sotto {config.EXIT_APR:.1f}% medio "
        f"o leva effettiva oltre {config.MAX_EFFECTIVE_LEVERAGE:.1f}x\n"
        f"</limiti_esecutore>\n"
    )
    portfolio_data = (
        f"{json.dumps({k: v for k, v in status.items() if k != 'positions'}, default=str)}\n"
        f"Uscite automatiche in questo ciclo: "
        f"{json.dumps(exit_results, default=str) if exit_results else 'nessuna'}"
    )
    with open("system_prompt.txt", encoding="utf-8") as f:
        system_prompt = f.read().format(portfolio_data, context)

    print(f"🤖 L'agente AI (OpenRouter {OPENROUTER_MODEL}) sta decidendo...")
    decision = decide_action(system_prompt)
    print(f"   Decisione: {json.dumps(decision, indent=2)}")

    # 5. Esecuzione
    print("⚡ Esecuzione...")
    result = manager.execute_signal(decision, status, markets)
    print(f"   Risultato: {json.dumps(result, default=str)}")
    try:
        db_utils.log_operation(decision, result, system_prompt=system_prompt)
    except Exception as db_err:
        print(f"[db_utils] operazione non salvata: {db_err}")

    if result.get("status") not in ("hold", "rejected"):
        status = manager.get_account_status(markets)
        try:
            db_utils.log_snapshot(status)
        except Exception:
            pass

    try:
        from telegram_bot import notify_cycle_result
        notify_cycle_result(decision, result, status, exit_results)
    except Exception as tg_err:
        print(f"[telegram] notifica non inviata: {tg_err}")

    print("✅ Ciclo completato.")
    return status


if __name__ == "__main__":
    if not config.PAPER_TRADING and not config.WALLET_ADDRESS:
        raise RuntimeError("WALLET_ADDRESS mancante nel .env (non serve solo in PAPER_TRADING)")
    if not os.getenv("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY mancante nel .env")

    try:
        run_cycle()
    except Exception as exc:
        try:
            from telegram_bot import notify_error
            notify_error(str(exc))
        except Exception:
            pass
        try:
            db_utils.log_error(exc, context={"wallet": config.WALLET_ADDRESS})
        except Exception:
            pass
        print(f"❌ Si è verificato un errore: {exc}")
        sys.exit(1)
