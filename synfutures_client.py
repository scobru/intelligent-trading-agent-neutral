"""
Client REST del microservizio synfutures-service (Node, Oyster SDK).

Lo stesso servizio del bot principale, con in piu' l'endpoint /funding.
Il servizio firma solo se SYNFUTURES_PRIVATE_KEY e' impostata: le letture
(strumenti, funding, saldi) funzionano anche senza chiave, quindi il paper
trading usa gli stessi dati reali.
"""

import logging
import time
from typing import Any, Dict, List, Optional

import requests

import config

logger = logging.getLogger(__name__)

WAD = 1e18


class SynFuturesError(RuntimeError):
    pass


class SynFuturesClient:
    def __init__(self, service_url: str = None, api_key: str = None, timeout: int = 60):
        self.service_url = (service_url or config.SYNFUTURES_SERVICE_URL).rstrip("/")
        self.api_key = api_key if api_key is not None else config.SYNFUTURES_API_KEY
        self.timeout = timeout
        self._markets: Dict[str, str] = {}
        self._markets_at = 0.0

    # ------------------------------------------------------------ trasporto
    def _request(self, method: str, endpoint: str, data: Dict[str, Any] = None) -> Any:
        url = f"{self.service_url}{endpoint}"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        try:
            if method == "GET":
                resp = requests.get(url, headers=headers, timeout=self.timeout)
            else:
                resp = requests.post(url, headers=headers, json=data or {}, timeout=self.timeout)
        except requests.exceptions.ConnectionError as exc:
            raise SynFuturesError(f"synfutures-service non raggiungibile su {url}: {exc}") from exc
        if resp.status_code >= 400:
            try:
                msg = resp.json().get("error") or resp.text
            except ValueError:
                msg = resp.text
            raise SynFuturesError(f"SynFutures {endpoint}: {msg}")
        return resp.json()

    def health(self) -> Dict[str, Any]:
        return self._request("GET", "/health")

    # ------------------------------------------------------------ mercati
    def markets(self) -> Dict[str, str]:
        """Ticker -> simbolo dello strumento perpetual in USDC (es. ETH -> ETH-USDC-LINK)."""
        if self._markets and time.time() - self._markets_at < 600:
            return self._markets
        markets: Dict[str, str] = {}
        for row in self.funding_raw():
            sym = str(row.get("symbol") or "")
            parts = sym.upper().split("-")
            if len(parts) >= 2 and parts[1] == "USDC" and parts[0] not in markets:
                markets[parts[0]] = sym
        if markets:
            self._markets, self._markets_at = markets, time.time()
        return self._markets

    def funding_raw(self) -> List[Dict[str, Any]]:
        rows = self._request("GET", "/funding")
        return rows if isinstance(rows, list) else []

    # ------------------------------------------------------------ conto
    def gate_usdc(self, address: str) -> float:
        total = 0.0
        for b in self._request("GET", f"/gate/balance/{address}") or []:
            if str(b.get("symbol", "")).upper() == "USDC":
                total += float(b.get("balance") or 0)
        return total

    def positions(self, address: str) -> Dict[str, Dict[str, Any]]:
        """Posizioni perpetual aperte, per ticker. size con segno (negativa = short)."""
        out: Dict[str, Dict[str, Any]] = {}
        for item in self._request("GET", f"/portfolio/{address}") or []:
            pos = item.get("position") or {}
            try:
                size = float(pos.get("size") or 0) / WAD
            except (TypeError, ValueError):
                continue
            if abs(size) < 1e-9:
                continue
            side = str(pos.get("side", "")).upper()
            if side in ("1", "SHORT") and size > 0:
                size = -size
            ticker = str(item.get("symbol") or "").split("-")[0].upper()
            out[ticker] = {
                "symbol": item.get("symbol"),
                "size": size,
                "entry_price": float(pos.get("entryPrice") or 0) / WAD,
                "margin": float(pos.get("margin") or 0) / WAD,
                "mark_price": float(pos.get("markPrice") or 0) / WAD,
            }
        return out

    # ------------------------------------------------------------ operazioni
    def deposit_usdc(self, amount: float) -> Dict[str, Any]:
        return self._request("POST", "/gate/deposit", {"token": "USDC", "amount": f"{amount:.6f}"})

    def withdraw_usdc(self, amount: float) -> Dict[str, Any]:
        # arrotondato per difetto: non si chiede mai piu' di quanto c'e'
        floored = int(amount * 1e6) / 1e6
        return self._request("POST", "/gate/withdraw", {"token": "USDC", "amount": f"{floored:.6f}"})

    def open_short(self, symbol: str, margin_usd: float, leverage: float, slippage_bps: int = 100) -> Dict[str, Any]:
        return self._request("POST", "/order/market", {
            "symbol": symbol, "side": "SHORT", "sizeUsd": round(margin_usd, 2),
            "leverage": round(leverage, 4), "slippage": int(slippage_bps),
        })

    def close_position(self, symbol: str) -> Dict[str, Any]:
        return self._request("POST", "/order/close", {"symbol": symbol})
