"""
Registro delle coppie delta-neutral aperte (una per asset).

On-chain si vedono solo il saldo spot nel wallet e lo short su SynFutures:
che cosa appartiene alla strategia, quando e' stata aperta, con che funding
e quanto capitale ci si e' messo lo sa solo questo registro. Serve per il
P&L, per il tempo minimo di permanenza e per riconoscere un hedge rotto
(short chiuso o liquidato mentre lo spot e' ancora in wallet).
"""

import json
import logging
import os
import time
from typing import Any, Dict, Optional

import config

logger = logging.getLogger(__name__)


class PositionStore:
    def __init__(self, path: str = None):
        self.path = path or config.POSITIONS_PATH
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                self.positions = json.load(fh) or {}
        except (OSError, ValueError):
            self.positions = {}

    def save(self):
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self.positions, fh, indent=2)
        except OSError as exc:
            logger.warning("Impossibile salvare %s: %s", self.path, exc)

    def get(self, asset: str) -> Optional[Dict[str, Any]]:
        return self.positions.get(asset)

    def record_open(self, asset: str, market: Dict[str, Any], capital_usd: float,
                    spot_qty: float, spot_price: float, perp_size: float, perp_price: float,
                    margin_usd: float, entry_apr: float) -> Dict[str, Any]:
        pos = {
            "asset": asset,
            "symbol": market["symbol"],
            "capital_usd": float(capital_usd),
            "spot_qty": float(spot_qty),
            "spot_entry": float(spot_price),
            "perp_size": float(perp_size),
            "perp_entry": float(perp_price),
            "margin_usd": float(margin_usd),
            "entry_apr": float(entry_apr),
            "opened_at": time.time(),
        }
        self.positions[asset] = pos
        self.save()
        return pos

    def record_close(self, asset: str):
        self.positions.pop(asset, None)
        self.save()
