"""
Funding di SynFutures V3 e selezione delle coppie da coprire.

Come funziona il funding in SynFutures V3 (Oyster AMM): e' continuo, non a
scadenze. Quando il prezzo "fair" dell'AMM e' sopra il mark (index spot), i
long pagano gli short |fair - mark| per unita' al giorno; il totale pagato
dai long viene diviso fra tutti gli short. Quindi uno short incassa:

    tasso giornaliero = (fair - mark) / mark * (totale long / totale short)

e se fair < mark paga lui (fair - mark) / mark al giorno. Il fattore
long/short conta: su un mercato sbilanciato verso i long lo short incassa
piu' del premio, e la nostra stessa posizione lo diluisce (va inclusa).

Un'osservazione sola e' rumore: il tasso si salva a ogni ciclo e si decide
sulla media delle ultime FUNDING_LOOKBACK_HOURS.
"""

import logging
from typing import Any, Dict, List, Optional

import config

logger = logging.getLogger(__name__)

DAYS_PER_YEAR = 365.0


def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def short_daily_rate(mark: float, fair: float, total_long: float, total_short: float,
                     extra_short: float = 0.0) -> float:
    """
    Tasso giornaliero (frazione) incassato da uno short; negativo se lo paga.
    `extra_short` e' la quantita' che vorremmo aggiungere: diluisce l'incasso.
    """
    if mark <= 0 or fair <= 0:
        return 0.0
    premium = (fair - mark) / mark
    if premium < 0:
        return premium  # gli short pagano, a prescindere dalle quantita'
    shorts = total_short + extra_short
    if shorts <= 0:
        return 0.0
    return premium * (total_long / shorts)


def to_apr(daily_rate: float) -> float:
    return daily_rate * DAYS_PER_YEAR * 100.0


def parse_markets(raw_rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Dalle righe di /funding tiene i mercati configurati (perp in USDC) e
    calcola premio e APR istantaneo dello short.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for row in raw_rows:
        sym = str(row.get("symbol") or "")
        parts = sym.upper().split("-")
        if len(parts) < 2 or parts[1] != "USDC":
            continue
        asset = parts[0]
        if asset not in config.MARKETS or asset in out:
            continue
        mark = _num(row.get("markPrice"))
        fair = _num(row.get("fairPrice"))
        spot = _num(row.get("spotPrice")) or mark
        tl = _num(row.get("totalLong"))
        ts = _num(row.get("totalShort"))
        daily = short_daily_rate(mark, fair, tl, ts)
        out[asset] = {
            "asset": asset,
            "symbol": sym,
            "spot_price": spot,
            "mark_price": mark,
            "fair_price": fair,
            "total_long": tl,
            "total_short": ts,
            "premium_pct": ((fair - mark) / mark * 100.0) if mark > 0 else 0.0,
            "short_daily_rate": daily,
            "short_apr": to_apr(daily),
        }
    return out


def attach_history(markets: Dict[str, Dict[str, Any]],
                   history: Dict[str, List[float]]) -> Dict[str, Dict[str, Any]]:
    """
    Aggiunge a ogni mercato la media degli APR osservati (incluso l'attuale)
    e il numero di osservazioni: e' su questi che si decide.
    """
    for asset, m in markets.items():
        values = list(history.get(asset, [])) + [m["short_apr"]]
        m["observations"] = len(values)
        m["avg_apr"] = sum(values) / len(values)
        m["min_apr"] = min(values)
        m["enough_history"] = len(values) >= config.FUNDING_MIN_OBSERVATIONS
    return markets


def entry_apr(market: Dict[str, Any], notional_usd: float) -> float:
    """APR atteso dopo il nostro ingresso: il nostro short diluisce l'incasso."""
    mark = market["mark_price"]
    if mark <= 0:
        return 0.0
    extra = notional_usd / mark
    daily = short_daily_rate(mark, market["fair_price"], market["total_long"],
                             market["total_short"], extra_short=extra)
    # la media storica, scalata dello stesso fattore di diluizione
    now = market["short_apr"]
    if now > 0 and daily >= 0:
        return market.get("avg_apr", now) * (to_apr(daily) / now)
    return min(market.get("avg_apr", now), to_apr(daily))


def round_trip_cost(notional_usd: float) -> float:
    """Costo stimato di aprire e chiudere la coppia (swap, fee perp, gas)."""
    bps = 2 * config.EST_SWAP_COST_BPS + 2 * config.EST_PERP_FEE_BPS
    return notional_usd * bps / 10_000 + config.EST_TX_COST_USD * config.TX_PER_ROUND_TRIP


def breakeven_days(notional_usd: float, apr: float) -> Optional[float]:
    daily_income = notional_usd * apr / 100.0 / DAYS_PER_YEAR
    if daily_income <= 0:
        return None
    return round_trip_cost(notional_usd) / daily_income


def format_for_prompt(markets: Dict[str, Dict[str, Any]]) -> str:
    lines = []
    for asset in config.NEUTRAL_ASSETS:
        m = markets.get(asset)
        if not m:
            lines.append(f"- {asset}: dati di funding non disponibili")
            continue
        lines.append(
            f"- {asset} ({m['symbol']}): prezzo ${m['mark_price']:,.2f} | premio fair/mark "
            f"{m['premium_pct']:+.4f}% | APR short ora {m['short_apr']:+.2f}% | media "
            f"{m.get('avg_apr', m['short_apr']):+.2f}% su {m.get('observations', 1)} osservazioni "
            f"(min {m.get('min_apr', m['short_apr']):+.2f}%) | OI long {m['total_long']:.3f} / "
            f"short {m['total_short']:.3f}"
            + ("" if m.get("enough_history", False) else " | STORICO INSUFFICIENTE: non si entra")
        )
    return "\n".join(lines)
