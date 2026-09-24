"""
Configurazione del bot delta-neutral su Base (chain id 8453).

La strategia: per ogni asset (ETH, BTC) si compra lo spot su Uniswap V3 e
si apre uno short della stessa quantita' sul perpetual SynFutures V3. Il
prezzo si annulla (quello che guadagna una gamba lo perde l'altra) e resta
il funding: quando i long pagano gli short, lo short incassa.

Come nei bot fratelli, gli indirizzi dei token vengono verificati on-chain
all'avvio (symbol e decimals) prima di muovere un solo dollaro.
"""

import os

from dotenv import load_dotenv

load_dotenv()


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def _i(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, default)))
    except (TypeError, ValueError):
        return int(default)


def _b(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on", "si")


# ---------------------------------------------------------------- rete
CHAIN_ID = 8453
BASE_RPC_URL = os.getenv("BASE_RPC_URL") or os.getenv("BASE_RPC") or "https://mainnet.base.org"
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "")
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
SYNFUTURES_SERVICE_URL = os.getenv("SYNFUTURES_SERVICE_URL", "http://localhost:3100")
SYNFUTURES_API_KEY = os.getenv("API_KEY", "")

# ---------------------------------------------------------------- contratti Base
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
CBBTC = "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf"

UNISWAP_V3_FACTORY = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"
UNISWAP_V3_QUOTER_V2 = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
UNISWAP_V3_SWAP_ROUTER_02 = "0x2626664c2603336E57B271c5C0b26F421741e481"
FEE_TIERS = (100, 500, 3000, 10000)

# SynFutures V3 Gate (contratto di deposito collaterale su Base)
SYNFUTURES_GATE = "0x208B443983D8BcC8578e9D86Db23FbA547071270"
GATE_MARGIN_BUFFER = _f("GATE_MARGIN_BUFFER", 1.20)  # cuscinetto di sicurezza (+20%)

KNOWN_ASSETS = {
    "USDC": {"address": USDC, "decimals": 6, "stable": True},
    "WETH": {"address": WETH, "decimals": 18, "stable": False},
    "CBBTC": {"address": CBBTC, "decimals": 8, "stable": False},
}

# Coppie gestite: nome -> gamba spot (token su Base) e gamba perp (base
# dello strumento SynFutures, es. "ETH" -> ETH-USDC-LINK)
MARKETS = {
    "ETH": {"spot_symbol": "WETH", "spot": WETH, "decimals": 18, "perp_base": "ETH"},
    "BTC": {"spot_symbol": "CBBTC", "spot": CBBTC, "decimals": 8, "perp_base": "BTC"},
}
NEUTRAL_ASSETS = [
    a.strip().upper() for a in os.getenv("NEUTRAL_ASSETS", "ETH,BTC").split(",")
    if a.strip().upper() in MARKETS
] or ["ETH"]

# ---------------------------------------------------------------- modalita'
# Di default NON firma nulla: mettere DRY_RUN=false per operare davvero.
DRY_RUN = _b("DRY_RUN", True)

# Paper: portafoglio virtuale, funding e prezzi reali di SynFutures
PAPER_TRADING = _b("PAPER_TRADING", False)
PAPER_START_USDC = _f("PAPER_START_USDC", 1000.0)
PAPER_GAS_USD = _f("PAPER_GAS_USD", 0.02)
if PAPER_TRADING:
    DRY_RUN = True

# ---------------------------------------------------------------- esecuzione
MIN_ETH_RESERVE = _f("MIN_ETH_RESERVE", 0.001)
MAX_SLIPPAGE_BPS = _i("MAX_SLIPPAGE_BPS", 100)
DEFAULT_SLIPPAGE_BPS = _i("DEFAULT_SLIPPAGE_BPS", 50)
MAX_GAS_PRICE_GWEI = _f("MAX_GAS_PRICE_GWEI", 0.5)
TX_DEADLINE_SECONDS = _i("TX_DEADLINE_SECONDS", 300)
TX_TIMEOUT_SECONDS = _i("TX_TIMEOUT_SECONDS", 180)
HTTP_TIMEOUT = _i("HTTP_TIMEOUT", 30)

# Stime dei costi per i conti di convenienza (andata + ritorno)
EST_SWAP_COST_BPS = _f("EST_SWAP_COST_BPS", 10.0)    # fee del pool 0.05% + slippage
EST_PERP_FEE_BPS = _f("EST_PERP_FEE_BPS", 5.0)       # taker SynFutures per lato
EST_TX_COST_USD = _f("EST_TX_COST_USD", 0.05)        # una transazione su Base
TX_PER_ROUND_TRIP = _i("TX_PER_ROUND_TRIP", 8)       # approve+swap, deposit, trade x2

# ---------------------------------------------------------------- dimensionamento
# Leva dello short: con 2x il capitale va 2/3 in spot e 1/3 in margine,
# e lo short regge circa un +40% del prezzo prima di essere a rischio.
PERP_LEVERAGE = _f("PERP_LEVERAGE", 2.0)
MAX_ASSET_PCT = _f("MAX_ASSET_PCT", 0.60)          # quota massima su un solo asset
MIN_POSITION_USD = _f("MIN_POSITION_USD", 150.0)   # capitale totale per coppia
MAX_POSITION_USD = _f("MAX_POSITION_USD", 5000.0)
MIN_NOTIONAL_USD = _f("MIN_NOTIONAL_USD", 70.0)    # minimo di SynFutures su Base


def min_gate_usdc_per_pair() -> float:
    """
    Margine minimo USDC necessario sul Gate per aprire una singola coppia.
    Con leva L e capitale minimo C: margine teorico M = C / (L + 1).
    SynFutures accetta ordini solo fino al 90% del saldo Gate; in piu'
    applichiamo GATE_MARGIN_BUFFER (+20%) per reggere funding e volatilita'.
    """
    margin = MIN_POSITION_USD / (PERP_LEVERAGE + 1)
    return round((margin / 0.9) * GATE_MARGIN_BUFFER, 2)


def recommended_gate_usdc(total_capital: float = None) -> float:
    """
    Quantita' raccomandata di USDC da tenere sul Gate SynFutures per far
    funzionare la strategia senza intoppi.
    Se total_capital non e' specificato, calcola il minimo per coprire
    tutti gli asset configurati in NEUTRAL_ASSETS.
    """
    num_assets = max(1, len(NEUTRAL_ASSETS))
    min_rec = round(num_assets * min_gate_usdc_per_pair(), 2)
    if total_capital is None or total_capital <= 0:
        return min_rec

    margin_total = total_capital / (PERP_LEVERAGE + 1)
    rec = round((margin_total / 0.9) * GATE_MARGIN_BUFFER, 2)
    return max(min_rec, rec)

# ---------------------------------------------------------------- funding
FUNDING_LOOKBACK_HOURS = _f("FUNDING_LOOKBACK_HOURS", 24.0)
FUNDING_MIN_OBSERVATIONS = _i("FUNDING_MIN_OBSERVATIONS", 3)
MIN_ENTRY_APR = _f("MIN_ENTRY_APR", 10.0)          # % annuo medio per entrare
EXIT_APR = _f("EXIT_APR", 0.0)                     # sotto questa media si esce
PANIC_EXIT_APR = _f("PANIC_EXIT_APR", -30.0)       # funding istantaneo molto negativo
MAX_BREAKEVEN_DAYS = _f("MAX_BREAKEVEN_DAYS", 7.0)
MIN_HOLD_HOURS = _f("MIN_HOLD_HOURS", 12.0)

# ---------------------------------------------------------------- rischio
AUTO_RISK_EXITS = _b("AUTO_RISK_EXITS", True)
# leva effettiva dello short (nozionale / equity della posizione) oltre cui
# si chiude tutto prima che il margine diventi un problema
MAX_EFFECTIVE_LEVERAGE = _f("MAX_EFFECTIVE_LEVERAGE", 5.0)
# differenza massima fra quantita' spot e short prima di considerare rotto l'hedge
MAX_HEDGE_DRIFT_PCT = _f("MAX_HEDGE_DRIFT_PCT", 10.0)


def persistent_path(filename: str) -> str:
    """
    Su CapRover/Docker /app/data e' il volume persistente: lo stato va li',
    altrimenti a ogni redeploy si perde lo storico. In locale resta accanto
    al codice.
    """
    project_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(project_dir, "data")
    if os.path.isdir(data_dir) and os.access(data_dir, os.W_OK):
        return os.path.join(data_dir, filename)
    return os.path.join(project_dir, filename)


SQLITE_DB_PATH = os.getenv("SQLITE_DB_PATH") or persistent_path("neutral_agent.db")
POSITIONS_PATH = os.getenv("POSITIONS_PATH") or persistent_path("positions.json")
PAPER_STATE_PATH = os.getenv("PAPER_STATE_PATH") or persistent_path("paper_portfolio.json")
