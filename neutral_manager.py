"""
Esecutore delta-neutral: legge lo stato delle coppie, applica le uscite di
rischio e i limiti, poi apre o chiude le due gambe.

Aprire una coppia (capitale C, leva dello short L):
  1. USDC -> spot su Uniswap V3 per il nozionale N = C * L / (L + 1)
  2. deposito del margine M = C - N sul Gate di SynFutures
  3. short della stessa quantita' comprata, al massimo con leva N / M
Chiuderla fa il percorso inverso: chiude lo short, ritira dal Gate, vende
lo spot. Se il passo 3 fallisce lo spot comprato viene rivenduto: una
gamba sola e' esattamente il rischio che questa strategia vuole evitare.

Il modello sceglie cosa fare; qui si decide se si puo' fare. Tutte le
soglie (APR minimo, storico sufficiente, rientro dei costi, esposizione
massima) sono controllate qui, non nel prompt.
"""

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import config
import db_utils
import funding
from paper import PaperBook
from positions import PositionStore

logger = logging.getLogger(__name__)

# Quota del margine depositato usata dall'ordine: il microservizio non
# accetta piu' del 90% del saldo Gate, il resto fa da cuscinetto
GATE_MARGIN_USE = 0.9


class NeutralManager:
    def __init__(self, client=None, synfutures=None):
        self.client = client
        self.sf = synfutures
        self.paper = PaperBook() if config.PAPER_TRADING else None
        self.store = PositionStore()
        self._uniswap = None

    @property
    def uniswap(self):
        if self._uniswap is None:
            from uniswap import UniswapV3
            self._uniswap = UniswapV3(self.client)
        return self._uniswap

    @property
    def mode(self) -> str:
        return "paper" if self.paper else ("dry_run" if config.DRY_RUN else "live")

    # ------------------------------------------------------------ auto-refuel
    def ensure_usdc_balance(self) -> Optional[Dict[str, Any]]:
        """
        Se in modalita' on-chain e il saldo USDC nel wallet e' insufficiente,
        ma c'e' ETH spendibile oltre la riserva gas, swappa in automatico l'eccesso in USDC.
        """
        if self.paper or not self.client:
            return None
        return self.uniswap.auto_refuel_usdc()

    # ------------------------------------------------------------ mercati
    def load_markets(self) -> Dict[str, Dict[str, Any]]:
        markets = funding.parse_markets(self.sf.funding_raw())
        history = db_utils.funding_history(config.FUNDING_LOOKBACK_HOURS)
        return funding.attach_history(markets, history)

    # ------------------------------------------------------------ stato
    def _row(self, asset: str, pos: Dict[str, Any], market: Optional[Dict[str, Any]],
             perp: Optional[Dict[str, Any]] = None, spot_qty: float = None) -> Dict[str, Any]:
        market = market or {}
        spot = market.get("spot_price") or float(pos["spot_entry"])
        mark = market.get("mark_price") or float(pos["perp_entry"])
        spot_qty = float(pos["spot_qty"]) if spot_qty is None else spot_qty
        if perp is not None:
            # solo uno short conta come copertura; niente short = niente margine
            perp_size = abs(float(perp["size"])) if float(perp.get("size") or 0) < 0 else 0.0
            perp_entry = float(perp.get("entry_price") or pos["perp_entry"])
            margin = float(perp.get("margin") or pos["margin_usd"]) if perp_size > 0 else 0.0
        elif self.paper:
            perp_size = float(pos["perp_size"])
            perp_entry = float(pos["perp_entry"])
            margin = float(pos["margin_usd"])
        else:
            perp_size, perp_entry, margin = 0.0, float(pos["perp_entry"]), 0.0
        perp_pnl = (perp_entry - mark) * perp_size
        perp_equity = margin + perp_pnl
        notional = perp_size * mark
        spot_value = spot_qty * spot
        value = spot_value + perp_equity
        drift = (abs(spot_qty - perp_size) / spot_qty * 100.0) if spot_qty > 0 else 100.0
        return {
            "asset": asset,
            "symbol": pos.get("symbol") or market.get("symbol"),
            "spot_qty": spot_qty,
            "spot_price": spot,
            "spot_value_usd": spot_value,
            "perp_size": perp_size,
            "perp_entry": perp_entry,
            "mark_price": mark,
            "perp_pnl_usd": perp_pnl,
            "margin_usd": margin,
            "perp_equity_usd": perp_equity,
            "notional_usd": notional,
            "effective_leverage": (notional / perp_equity) if perp_equity > 0 else float("inf"),
            "hedge_drift_pct": drift,
            "capital_usd": float(pos["capital_usd"]),
            "value_usd": value,
            "pnl_usd": value - float(pos["capital_usd"]),
            "funding_usd": pos.get("funding_usd"),
            "apr_now": market.get("short_apr"),
            "apr_avg": market.get("avg_apr"),
            "entry_apr": pos.get("entry_apr"),
            "held_hours": (time.time() - float(pos.get("opened_at", time.time()))) / 3600.0,
        }

    def get_account_status(self, markets: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        rows: List[Dict[str, Any]] = []
        wallet = self.client.address if self.client else None
        eth = usdc = gate = None

        if self.paper:
            self.paper.accrue(markets)
            usdc = self.paper.usdc
            for asset, pos in self.paper.positions.items():
                rows.append(self._row(asset, pos, markets.get(asset)))
            idle = usdc
        else:
            eth = self.client.eth_balance()
            usdc = self.client.balance_of_float(config.USDC)
            gate = self.sf.gate_usdc(wallet)
            perps = self.sf.positions(wallet)
            for asset, pos in self.store.positions.items():
                token = config.MARKETS[asset]["spot"]
                held = self.client.balance_of_float(token)
                rows.append(self._row(asset, pos, markets.get(asset), perps.get(asset, {"size": 0.0}),
                                      spot_qty=min(float(pos["spot_qty"]), held)))
            idle = usdc + gate

        invested = sum(r["value_usd"] for r in rows)
        total = idle + invested
        notional = sum(r["notional_usd"] for r in rows)
        daily_funding = sum(r["notional_usd"] * (r["apr_now"] or 0.0) / 100.0 / funding.DAYS_PER_YEAR
                            for r in rows)
        status = {
            "mode": self.mode,
            "wallet": wallet,
            "eth_balance": eth,
            "usdc_balance": usdc,
            "gate_usdc": gate,
            "idle_usd": idle,
            "invested_usd": invested,
            "total_value_usd": total,
            "hedged_notional_usd": notional,
            "est_daily_funding_usd": daily_funding,
            "weighted_apr": (daily_funding * funding.DAYS_PER_YEAR / notional * 100.0) if notional else 0.0,
            "positions": rows,
        }
        if self.paper:
            summary = self.paper.summary()
            status["paper"] = summary
            status["pnl_since_start_usd"] = total - summary["initial_usdc"]
        return status

    # ------------------------------------------------------------ rischio
    def risk_exits(self, status: Dict[str, Any]) -> List[Dict[str, Any]]:
        exits = []
        for r in status["positions"]:
            trigger = None
            if r["perp_size"] <= 0:
                trigger = "hedge rotto: short assente"
            elif r["hedge_drift_pct"] > config.MAX_HEDGE_DRIFT_PCT:
                trigger = f"hedge sbilanciato del {r['hedge_drift_pct']:.1f}%"
            elif r["effective_leverage"] > config.MAX_EFFECTIVE_LEVERAGE:
                trigger = f"leva effettiva {r['effective_leverage']:.1f}x oltre {config.MAX_EFFECTIVE_LEVERAGE:.1f}x"
            elif r["apr_now"] is not None and r["apr_now"] < config.PANIC_EXIT_APR:
                trigger = f"funding {r['apr_now']:+.1f}% sotto la soglia di panico"
            elif (r["apr_avg"] is not None and r["apr_avg"] < config.EXIT_APR
                  and r["held_hours"] >= config.MIN_HOLD_HOURS):
                trigger = f"funding medio {r['apr_avg']:+.1f}% sotto {config.EXIT_APR:.1f}%"
            if trigger:
                exits.append(dict(r, trigger=trigger))
        return exits

    # ------------------------------------------------------------ validazione
    def check_open(self, decision: Dict[str, Any], status: Dict[str, Any],
                   markets: Dict[str, Dict[str, Any]]) -> Tuple[Optional[str], Dict[str, Any]]:
        """Restituisce (motivo del rifiuto, piano). Motivo None = si puo' aprire."""
        asset = decision.get("asset")
        if asset not in config.NEUTRAL_ASSETS:
            return f"asset {asset!r} non gestito (ammessi: {', '.join(config.NEUTRAL_ASSETS)})", {}
        if any(r["asset"] == asset for r in status["positions"]):
            return f"coppia {asset} gia' aperta", {}
        m = markets.get(asset)
        if not m:
            return f"funding di {asset} non disponibile", {}
        if not m.get("enough_history"):
            return (f"storico funding insufficiente ({m.get('observations', 0)}/"
                    f"{config.FUNDING_MIN_OBSERVATIONS} osservazioni)"), {}

        total = status["total_value_usd"]
        wanted = max(0.0, min(1.0, float(decision.get("target_portion_of_portfolio") or 0))) * total
        capital = min(wanted, total * config.MAX_ASSET_PCT, config.MAX_POSITION_USD, status["idle_usd"])
        if capital < config.MIN_POSITION_USD:
            return (f"capitale ${capital:.2f} sotto il minimo di ${config.MIN_POSITION_USD:.0f} "
                    f"(disponibili ${status['idle_usd']:.2f})"), {}
        lev = config.PERP_LEVERAGE
        notional = capital * lev / (lev + 1)
        if notional < config.MIN_NOTIONAL_USD:
            return f"nozionale ${notional:.2f} sotto il minimo SynFutures di ${config.MIN_NOTIONAL_USD:.0f}", {}

        apr = funding.entry_apr(m, notional)
        if apr < config.MIN_ENTRY_APR:
            return (f"APR atteso {apr:+.2f}% (media {m['avg_apr']:+.2f}%, gia' diluito dal nostro short) "
                    f"sotto il minimo di {config.MIN_ENTRY_APR:.1f}%"), {}
        if m["short_apr"] <= 0:
            return f"funding istantaneo {m['short_apr']:+.2f}%: gli short stanno pagando", {}
        be = funding.breakeven_days(notional, apr)
        if be is None or be > config.MAX_BREAKEVEN_DAYS:
            return (f"i costi (${funding.round_trip_cost(notional):.2f}) si ripagano in "
                    f"{'mai' if be is None else f'{be:.1f} giorni'}, oltre {config.MAX_BREAKEVEN_DAYS:.0f}"), {}

        margin_usd = capital - notional
        if not self.paper:
            if status["eth_balance"] is not None and status["eth_balance"] < config.MIN_ETH_RESERVE:
                return f"ETH per il gas {status['eth_balance']:.5f} sotto la riserva di {config.MIN_ETH_RESERVE}", {}

            gate_usdc = float(status.get("gate_usdc") or 0.0)
            wallet_usdc = float(status.get("usdc_balance") or 0.0)
            gate_shortfall = max(0.0, margin_usd - gate_usdc)
            wallet_needed = notional + gate_shortfall

            if wallet_usdc < wallet_needed:
                shortfall_msg = (
                    f" e ${gate_shortfall:.2f} da depositare sul Gate (Gate ha gia' ${gate_usdc:.2f})"
                    if gate_shortfall > 0 else f" (margine Gate ${gate_usdc:.2f} gia' presente)"
                )
                return (f"USDC nel wallet ${wallet_usdc:.2f} insufficienti: servono ${wallet_needed:.2f} "
                        f"(${notional:.2f} per spot Uniswap{shortfall_msg})"), {}

        return None, {"asset": asset, "market": m, "capital_usd": capital, "notional_usd": notional,
                      "margin_usd": margin_usd, "entry_apr": apr, "breakeven_days": be}

    # ------------------------------------------------------------ esecuzione
    def execute_signal(self, decision: Dict[str, Any], status: Dict[str, Any],
                       markets: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        op = decision.get("operation", "hold")
        asset = decision.get("asset")
        base = {"operation": op, "asset": asset}
        if op == "hold":
            return dict(base, status="hold")

        if op == "close":
            row = next((r for r in status["positions"] if r["asset"] == asset), None)
            if not row:
                return dict(base, status="rejected", reason=f"nessuna coppia {asset} aperta")
            return self.close_pair(asset, markets.get(asset), trigger="decisione AI")

        reason, plan = self.check_open(decision, status, markets)
        if reason:
            return dict(base, status="rejected", reason=reason)
        return self.open_pair(plan)

    def open_pair(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        asset, m = plan["asset"], plan["market"]
        base = {"operation": "open", "asset": asset, "symbol": m["symbol"],
                "amount_usd": plan["capital_usd"], "notional_usd": plan["notional_usd"],
                "apr": plan["entry_apr"], "breakeven_days": plan["breakeven_days"]}

        if self.paper:
            res = self.paper.open(asset, m, plan["capital_usd"], config.PERP_LEVERAGE, plan["entry_apr"])
            return dict(base, status="paper", **res)

        spot = config.MARKETS[asset]["spot"]
        usdc_raw = int(plan["notional_usd"] * 1e6)
        route = self.uniswap.best_route(config.USDC, spot, usdc_raw)
        if not route:
            return dict(base, status="rejected", reason=f"nessuna rotta Uniswap USDC -> {asset}")
        expected_qty = route.amount_out / 10 ** config.MARKETS[asset]["decimals"]
        margin = plan["margin_usd"]
        leverage = expected_qty * m["mark_price"] / (margin * GATE_MARGIN_USE)

        gate_usdc = 0.0
        try:
            gate_usdc = self.sf.gate_usdc(self.client.address)
        except Exception as gerr:
            logger.warning("Impossibile leggere saldo Gate prima dell'apertura: %s", gerr)

        to_deposit = max(0.0, margin - gate_usdc)

        if config.DRY_RUN:
            plan_steps = [
                f"swap ${plan['notional_usd']:.2f} USDC -> ~{expected_qty:.6f} {asset} ({route.describe(self.client)})",
            ]
            if to_deposit > 0.01:
                plan_steps.append(
                    f"deposito ${to_deposit:.2f} USDC sul Gate SynFutures (saldo attuale ${gate_usdc:.2f}, necessario ${margin:.2f})"
                )
            else:
                plan_steps.append(
                    f"margine ${margin:.2f} USDC gia' presente sul Gate SynFutures (${gate_usdc:.2f} disponibili)"
                )
            plan_steps.append(
                f"short {m['symbol']} ~{expected_qty:.6f} con margine ${margin * GATE_MARGIN_USE:.2f} (leva {leverage:.2f}x)"
            )
            return dict(base, status="dry_run", plan=plan_steps)

        txs: List[Dict[str, Any]] = []
        before = self.client.balance_of(spot)
        txs.append(self.uniswap.swap(route, config.DEFAULT_SLIPPAGE_BPS))
        qty = (self.client.balance_of(spot) - before) / 10 ** config.MARKETS[asset]["decimals"]
        try:
            if to_deposit > 0.01:
                logger.info("Deposito di $%.2f USDC sul Gate SynFutures...", to_deposit)
                dep_res = self.sf.deposit_usdc(to_deposit)
                txs.append(dict(dep_res, description=f"deposito Gate (${to_deposit:.2f} USDC)"))
                time.sleep(2)
            leverage = qty * m["mark_price"] / (margin * GATE_MARGIN_USE)
            txs.append(dict(self.sf.open_short(m["symbol"], margin * GATE_MARGIN_USE, leverage),
                            description=f"short {m['symbol']}"))
        except Exception as exc:
            # una gamba sola e' esposizione pura: si torna indietro
            logger.error("Short fallito, rivendo lo spot: %s", exc)
            rollback = self._unwind_spot(asset, qty)
            try:
                gate = self.sf.gate_usdc(self.client.address)
                if gate > 0.01:
                    self.sf.withdraw_usdc(gate)
            except Exception as wexc:
                logger.error("Ritiro dal Gate dopo il rollback fallito: %s", wexc)
            return dict(base, status="error", transactions=txs + ([rollback] if rollback else []),
                        reason=f"short non aperto ({exc}): spot rivenduto")

        self.store.record_open(asset, m, plan["capital_usd"], qty, m["spot_price"], qty,
                               m["fair_price"] or m["mark_price"], margin * GATE_MARGIN_USE,
                               plan["entry_apr"])
        return dict(base, status="success", spot_qty=qty, transactions=txs)

    def _unwind_spot(self, asset: str, qty: float) -> Optional[Dict[str, Any]]:
        spot = config.MARKETS[asset]["spot"]
        raw = min(int(qty * 10 ** config.MARKETS[asset]["decimals"]), self.client.balance_of(spot))
        if raw <= 0:
            return None
        route = self.uniswap.best_route(spot, config.USDC, raw)
        if not route:
            logger.error("Nessuna rotta per rivendere %s: lo spot resta nel wallet", asset)
            return None
        return self.uniswap.swap(route, config.MAX_SLIPPAGE_BPS)

    def close_pair(self, asset: str, market: Optional[Dict[str, Any]], trigger: str) -> Dict[str, Any]:
        base = {"operation": "close", "asset": asset, "trigger": trigger}
        if self.paper:
            if not market:
                return dict(base, status="error", reason=f"prezzi di {asset} non disponibili")
            res = self.paper.close(asset, market)
            return dict(base, status="paper", amount_usd=res["proceeds_usd"], **res)

        pos = self.store.get(asset)
        if not pos:
            return dict(base, status="rejected", reason=f"{asset} non e' nel registro delle coppie")
        if config.DRY_RUN:
            return dict(base, status="dry_run", plan=[
                f"chiusura short {pos['symbol']}",
                "ritiro di tutto il saldo USDC dal Gate",
                f"vendita di {pos['spot_qty']:.6f} {asset} -> USDC",
            ])

        txs: List[Dict[str, Any]] = []
        errors: List[str] = []
        try:
            txs.append(dict(self.sf.close_position(pos["symbol"]), description=f"chiusura {pos['symbol']}"))
        except Exception as exc:
            errors.append(f"chiusura short: {exc}")
        try:
            gate = self.sf.gate_usdc(self.client.address)
            if gate > 0.01:
                txs.append(dict(self.sf.withdraw_usdc(gate), description="ritiro dal Gate"))
        except Exception as exc:
            errors.append(f"ritiro dal Gate: {exc}")
        try:
            swap = self._unwind_spot(asset, float(pos["spot_qty"]))
            if swap:
                txs.append(swap)
        except Exception as exc:
            errors.append(f"vendita spot: {exc}")

        if errors:
            # il registro resta: al prossimo ciclo si vede l'hedge rotto e si riprova
            return dict(base, status="error", transactions=txs, reason="; ".join(errors))
        self.store.record_close(asset)
        return dict(base, status="success", transactions=txs)
