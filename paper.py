"""
Paper trading: portafoglio finto, prezzi e funding veri.

Ogni coppia virtuale compra lo spot al prezzo oracolo di SynFutures e apre
lo short al prezzo fair dell'AMM (quello a cui si eseguirebbe davvero). A
ogni ciclo lo short matura il funding *corrente* osservato su SynFutures,
per il tempo trascorso dall'ultimo ciclo: e' la stessa idea del bot yield,
che fa maturare l'APY reale dei pool.

Semplificazioni dichiarate: swap e fee perp costano le stime di config
(EST_SWAP_COST_BPS, EST_PERP_FEE_BPS), il gas e' forfettario
(PAPER_GAS_USD per transazione) e il funding matura a gradini fra un ciclo
e l'altro invece che in continuo.
"""

import json
import logging
import os
import time
from typing import Any, Dict

import config

logger = logging.getLogger(__name__)

SECONDS_PER_DAY = 86_400.0
TX_OPEN = 4    # approve + swap, deposit sul Gate, trade
TX_CLOSE = 4   # trade, withdraw dal Gate, approve + swap


class PaperBook:
    def __init__(self, path: str = None, start_usdc: float = None):
        self.path = path or config.PAPER_STATE_PATH
        self.start_usdc = config.PAPER_START_USDC if start_usdc is None else start_usdc
        self.state: Dict[str, Any] = {}
        self.load()

    # ------------------------------------------------------------ persistenza
    def load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                self.state = json.load(fh) or {}
        except (OSError, ValueError):
            self.state = {}
        if not self.state:
            self.state = {
                "usdc": float(self.start_usdc),
                "initial_usdc": float(self.start_usdc),
                "positions": {},
                "created_at": time.time(),
                "costs_usd": 0.0,
                "funding_earned_usd": 0.0,
                "operations": 0,
            }
            self.save()

    def save(self):
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self.state, fh, indent=2)
        except OSError as exc:
            logger.warning("Impossibile salvare %s: %s", self.path, exc)

    # ------------------------------------------------------------ saldi
    @property
    def usdc(self) -> float:
        return float(self.state.get("usdc", 0.0))

    @property
    def positions(self) -> Dict[str, Dict[str, Any]]:
        return self.state.setdefault("positions", {})

    @staticmethod
    def perp_pnl(pos: Dict[str, Any], mark: float) -> float:
        """P&L dello short: guadagna se il prezzo scende."""
        return (float(pos["perp_entry"]) - mark) * float(pos["perp_size"])

    def position_value(self, pos: Dict[str, Any], spot: float, mark: float) -> float:
        spot_value = float(pos["spot_qty"]) * spot
        perp_equity = float(pos["margin_usd"]) + self.perp_pnl(pos, mark)
        return spot_value + perp_equity

    def _charge(self, notional: float, n_tx: int) -> float:
        cost = notional * (config.EST_SWAP_COST_BPS + config.EST_PERP_FEE_BPS) / 10_000
        cost += config.PAPER_GAS_USD * n_tx
        self.state["costs_usd"] = float(self.state.get("costs_usd", 0.0)) + cost
        return cost

    # ------------------------------------------------------------ funding
    def accrue(self, markets: Dict[str, Dict[str, Any]], now: float = None) -> float:
        """Fa maturare il funding corrente su ogni short, dall'ultimo ciclo."""
        now = now or time.time()
        earned = 0.0
        for asset, pos in self.positions.items():
            m = markets.get(asset)
            last = float(pos.get("last_accrual", now))
            dt = max(0.0, now - last)
            if m and dt > 0:
                notional = float(pos["perp_size"]) * m["mark_price"]
                gain = notional * m["short_daily_rate"] * dt / SECONDS_PER_DAY
                pos["margin_usd"] = float(pos["margin_usd"]) + gain
                pos["funding_usd"] = float(pos.get("funding_usd", 0.0)) + gain
                earned += gain
            pos["last_accrual"] = now
        self.state["funding_earned_usd"] = float(self.state.get("funding_earned_usd", 0.0)) + earned
        self.save()
        return earned

    # ------------------------------------------------------------ operazioni
    def open(self, asset: str, market: Dict[str, Any], capital_usd: float, leverage: float,
             entry_apr: float) -> Dict[str, Any]:
        capital_usd = min(capital_usd, self.usdc)
        notional = capital_usd * leverage / (leverage + 1)
        margin = capital_usd - notional
        cost = self._charge(notional, TX_OPEN)
        spot_price = market["spot_price"]
        # il costo si paga dal margine: lo spot resta pari allo short
        spot_qty = notional / spot_price
        perp_price = market["fair_price"] or market["mark_price"]
        self.state["usdc"] = self.usdc - capital_usd
        self.positions[asset] = {
            "asset": asset,
            "symbol": market["symbol"],
            "capital_usd": capital_usd,
            "spot_qty": spot_qty,
            "spot_entry": spot_price,
            "perp_size": spot_qty,
            "perp_entry": perp_price,
            "margin_usd": margin - cost,
            "funding_usd": 0.0,
            "entry_apr": entry_apr,
            "opened_at": time.time(),
            "last_accrual": time.time(),
        }
        self.state["operations"] = int(self.state.get("operations", 0)) + 1
        self.save()
        return {"capital_usd": capital_usd, "notional_usd": notional, "margin_usd": margin - cost,
                "spot_qty": spot_qty, "cost_usd": cost}

    def close(self, asset: str, market: Dict[str, Any]) -> Dict[str, Any]:
        pos = self.positions.get(asset)
        if not pos:
            return {"proceeds_usd": 0.0, "cost_usd": 0.0}
        spot = market["spot_price"]
        mark = market["mark_price"]
        gross = self.position_value(pos, spot, mark)
        cost = self._charge(float(pos["spot_qty"]) * spot, TX_CLOSE)
        proceeds = max(0.0, gross - cost)
        self.state["usdc"] = self.usdc + proceeds
        del self.positions[asset]
        self.state["operations"] = int(self.state.get("operations", 0)) + 1
        self.save()
        return {"proceeds_usd": proceeds, "cost_usd": cost,
                "pnl_usd": proceeds - float(pos["capital_usd"]),
                "funding_usd": float(pos.get("funding_usd", 0.0))}

    def summary(self) -> Dict[str, Any]:
        return {
            "initial_usdc": float(self.state.get("initial_usdc", self.start_usdc)),
            "funding_earned_usd": float(self.state.get("funding_earned_usd", 0.0)),
            "costs_usd": float(self.state.get("costs_usd", 0.0)),
            "operations": int(self.state.get("operations", 0)),
            "created_at": self.state.get("created_at"),
        }
