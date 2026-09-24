"""
Tool di gestione del collaterale sul Gate di SynFutures V3 (Base Chain).

Permette di:
1. Verificare i saldi correnti (ETH per gas, USDC nel wallet, USDC sul Gate).
2. Calcolare la quantita' di USDC raccomandata per far funzionare la strategia
   delta-neutral (margine short per le coppie configurate + cuscinetto di sicurezza).
3. Eseguire il deposito o il ritiro dal Gate via microservizio REST o direttamente
   via Web3 / contratto smart.

Uso:
    python tools/deposit_gate.py --status
    python tools/deposit_gate.py --deposit                  # deposita la quantita' raccomandata
    python tools/deposit_gate.py --deposit --amount 100     # deposita un importo specifico
    python tools/deposit_gate.py --withdraw --amount 50     # ritira dal Gate nel wallet
    python tools/deposit_gate.py --deposit --dry-run
"""

import argparse
import logging
import os
import sys

# Aggiunge la root del progetto al path per importare moduli fratelli
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from base_client import BaseClient, BaseChainError
from synfutures_client import SynFuturesClient, SynFuturesError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("DepositGate")


def print_banner():
    print("=" * 65)
    print(" 🚪 SynFutures V3 Gate — Gestione Collaterale USDC (Base Chain)")
    print("=" * 65)


def get_status(client: BaseClient, sf: SynFuturesClient):
    wallet = client.address
    eth = client.eth_balance()
    usdc_wallet = client.balance_of_float(config.USDC)

    gate_usdc = 0.0
    gate_method = "direct"
    try:
        # Prova prima con il microservizio
        gate_usdc = sf.gate_usdc(wallet)
        gate_method = "service"
    except Exception:
        # Fallback diretto su Web3
        try:
            gate_usdc = client.gate_balance_of_float(config.USDC)
        except Exception as e:
            logger.warning("Impossibile leggere Gate da Web3: %s", e)

    # Calcolo quantita' raccomandata
    num_assets = len(config.NEUTRAL_ASSETS)
    min_per_pair = config.min_gate_usdc_per_pair()
    rec_total = config.recommended_gate_usdc()

    # Shortfall rispetto al raccomandato
    shortfall = max(0.0, rec_total - gate_usdc)

    return {
        "wallet": wallet,
        "eth": eth,
        "usdc_wallet": usdc_wallet,
        "gate_usdc": gate_usdc,
        "gate_method": gate_method,
        "num_assets": num_assets,
        "assets": config.NEUTRAL_ASSETS,
        "min_per_pair": min_per_pair,
        "recommended_total": rec_total,
        "shortfall": round(shortfall, 2),
    }


def display_status(status: dict):
    print(f"📍 Wallet:               {status['wallet']}")
    print(f"⛽ ETH (Gas):            {status['eth']:.5f} ETH")
    print(f"💵 USDC nel Wallet:      ${status['usdc_wallet']:,.2f}")
    print(f"🚪 USDC sul Gate:        ${status['gate_usdc']:,.2f}")
    print("-" * 65)
    print(f"📊 Parametri Strategia:")
    print(f"   • Asset attivi:       {', '.join(status['assets'])} ({status['num_assets']} coppie)")
    print(f"   • Capitale min/pos:   ${config.MIN_POSITION_USD:.2f}")
    print(f"   • Leva short:         {config.PERP_LEVERAGE:.1f}x (margine teorico: ${config.MIN_POSITION_USD / (config.PERP_LEVERAGE + 1):.2f})")
    print(f"   • Min Gate per coppia: ${status['min_per_pair']:.2f} USDC (include max 90% use e +{int((config.GATE_MARGIN_BUFFER - 1) * 100)}% buffer)")
    print(f"   • Totale raccomandato: ${status['recommended_total']:.2f} USDC")
    print("-" * 65)
    if status['shortfall'] <= 0.01:
        print(f"✅ Il Gate ha gia' collaterale sufficiente (${status['gate_usdc']:.2f} >= ${status['recommended_total']:.2f}).")
    else:
        print(f"⚠️  Fabbisogno Gate: servono ancora ${status['shortfall']:.2f} USDC per coprire tutte le coppie.")
    print("=" * 65)


def do_deposit(client: BaseClient, sf: SynFuturesClient, amount: float, method: str = "auto", dry_run: bool = False):
    status = get_status(client, sf)
    display_status(status)

    if amount <= 0:
        amount = status["shortfall"]
        if amount <= 0:
            print("✅ Il Gate ha gia' la quantita' raccomandata di collaterale. Nessun deposito necessario.")
            return

    print(f"\n🚀 Avvio deposito di ${amount:.2f} USDC sul Gate SynFutures...")
    if status["usdc_wallet"] < amount:
        print(f"❌ Errore: USDC insufficienti nel wallet (${status['usdc_wallet']:.2f} disponibili < ${amount:.2f} richiesti).")
        return

    if dry_run:
        print(f"[DRY-RUN] Simulazione completata: verrebbero depositati ${amount:.2f} USDC sul contratto Gate {config.SYNFUTURES_GATE}.")
        return

    # Esecuzione
    # Prova tramite microservizio se attivo, altrimenti fallback diretto
    res = None
    if method in ("auto", "service"):
        try:
            print("⏳ Invio richiesta al microservizio SynFutures...")
            res = sf.deposit_usdc(amount)
            print(f"✅ Deposito completato via SynFutures Service! Tx: {res.get('txHash')}")
        except Exception as exc:
            if method == "service":
                print(f"❌ Errore microservizio: {exc}")
                return
            logger.warning("Microservizio non disponibile (%s), fallback su transazione Web3 diretta...", exc)

    if res is None:
        try:
            print(f"⏳ Invio transazione diretta al contratto Gate {config.SYNFUTURES_GATE}...")
            res = client.deposit_to_gate(config.USDC, amount)
            print(f"✅ Deposito completato via Web3! Explorer: {res.get('explorer')}")
        except Exception as exc:
            print(f"❌ Errore deposito Web3: {exc}")
            return

    # Verifica saldo finale
    new_gate = sf.gate_usdc(client.address) if method != "direct" else client.gate_balance_of_float(config.USDC)
    print(f"🎉 Nuovo saldo Gate: ${new_gate:.2f} USDC.")


def do_withdraw(client: BaseClient, sf: SynFuturesClient, amount: float, method: str = "auto", dry_run: bool = False):
    status = get_status(client, sf)
    display_status(status)

    if amount <= 0:
        amount = status["gate_usdc"]

    if amount <= 0:
        print("❌ Nessun saldo disponibile sul Gate da ritirare.")
        return

    if amount > status["gate_usdc"]:
        print(f"❌ Errore: importo richiesto (${amount:.2f}) maggiore del saldo Gate (${status['gate_usdc']:.2f}).")
        return

    print(f"\n🚀 Avvio ritiro di ${amount:.2f} USDC dal Gate...")
    if dry_run:
        print(f"[DRY-RUN] Simulazione completata: verrebbero ritirati ${amount:.2f} USDC dal Gate nel wallet.")
        return

    res = None
    if method in ("auto", "service"):
        try:
            print("⏳ Invio richiesta al microservizio SynFutures...")
            res = sf.withdraw_usdc(amount)
            print(f"✅ Ritiro completato via SynFutures Service! Tx: {res.get('txHash')}")
        except Exception as exc:
            if method == "service":
                print(f"❌ Errore microservizio: {exc}")
                return
            logger.warning("Microservizio non disponibile (%s), fallback su transazione Web3 diretta...", exc)

    if res is None:
        try:
            print(f"⏳ Invio transazione diretta di ritiro a Gate {config.SYNFUTURES_GATE}...")
            res = client.withdraw_from_gate(config.USDC, amount)
            print(f"✅ Ritiro completato via Web3! Explorer: {res.get('explorer')}")
        except Exception as exc:
            print(f"❌ Errore ritiro Web3: {exc}")
            return

    new_gate = sf.gate_usdc(client.address) if method != "direct" else client.gate_balance_of_float(config.USDC)
    print(f"🎉 Nuovo saldo Gate: ${new_gate:.2f} USDC.")


def main():
    parser = argparse.ArgumentParser(description="Gestione collaterale Gate SynFutures V3")
    parser.add_argument("--status", action="store_true", help="Mostra lo stato dei saldi e il fabbisogno Gate")
    parser.add_argument("--deposit", action="store_true", help="Deposita USDC sul Gate")
    parser.add_argument("--withdraw", action="store_true", help="Ritira USDC dal Gate nel wallet")
    parser.add_argument("--amount", type=float, default=0.0, help="Quantita' USDC specifica (default: quantita' raccomandata)")
    parser.add_argument("--method", choices=["auto", "service", "direct"], default="auto", help="Metodo di esecuzione")
    parser.add_argument("--dry-run", action="store_true", help="Simula l'operazione senza inviare transazioni")

    args = parser.parse_args()

    print_banner()

    if not config.WALLET_ADDRESS:
        print("❌ WALLET_ADDRESS non configurato nel file .env")
        sys.exit(1)

    try:
        client = BaseClient()
    except Exception as exc:
        print(f"❌ Errore inizializzazione BaseClient: {exc}")
        sys.exit(1)

    sf = SynFuturesClient()

    if args.deposit:
        do_deposit(client, sf, args.amount, method=args.method, dry_run=args.dry_run)
    elif args.withdraw:
        do_withdraw(client, sf, args.amount, method=args.method, dry_run=args.dry_run)
    else:
        # Default: mostra status
        status = get_status(client, sf)
        display_status(status)
        print("\nSuggerimenti:")
        print(" • Per depositare la quantita' raccomandata: python tools/deposit_gate.py --deposit")
        print(" • Per depositare un importo specifico:      python tools/deposit_gate.py --deposit --amount 100")
        print(" • Per ritirare i fondi dal Gate:           python tools/deposit_gate.py --withdraw")


if __name__ == "__main__":
    main()
