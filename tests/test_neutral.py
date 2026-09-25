"""
Test offline: calcolo del funding, parsing della decisione, paper book,
limiti dell'esecutore e uscite di rischio. Nessuna chiamata di rete.

    python -m unittest discover -s tests
"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import db_utils  # noqa: E402
import funding  # noqa: E402
from neutral_agent import _clean_and_parse_json  # noqa: E402
from neutral_manager import NeutralManager  # noqa: E402
from paper import PaperBook  # noqa: E402


def funding_row(symbol="ETH-USDC-LINK", mark=3000.0, fair=3001.0, long_=100.0, short=80.0, spot=None):
    return {"symbol": symbol, "markPrice": str(mark), "fairPrice": str(fair),
            "spotPrice": str(spot or mark), "totalLong": str(long_), "totalShort": str(short)}


def market(**over):
    m = funding.parse_markets([funding_row(**over)])["ETH"]
    return funding.attach_history(
        {"ETH": m}, {"ETH": [m["short_apr"]] * config.FUNDING_MIN_OBSERVATIONS})["ETH"]


class TmpState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._saved = (config.SQLITE_DB_PATH, config.PAPER_STATE_PATH, config.POSITIONS_PATH,
                       config.PAPER_TRADING)
        config.SQLITE_DB_PATH = os.path.join(self.tmp, "t.db")
        config.PAPER_STATE_PATH = os.path.join(self.tmp, "paper.json")
        config.POSITIONS_PATH = os.path.join(self.tmp, "positions.json")

    def tearDown(self):
        (config.SQLITE_DB_PATH, config.PAPER_STATE_PATH, config.POSITIONS_PATH,
         config.PAPER_TRADING) = self._saved


class FundingMathTest(unittest.TestCase):
    def test_longs_pay_shorts_scaled_by_open_interest(self):
        # premio 0.1% al giorno, long 2x gli short: lo short incassa 0.2%
        rate = funding.short_daily_rate(1000, 1001, total_long=200, total_short=100)
        self.assertAlmostEqual(rate, 0.002, places=9)
        self.assertAlmostEqual(funding.to_apr(rate), 73.0, places=6)

    def test_shorts_pay_when_fair_below_mark(self):
        rate = funding.short_daily_rate(1000, 999, total_long=200, total_short=100)
        self.assertAlmostEqual(rate, -0.001, places=9)

    def test_our_short_dilutes_income(self):
        before = funding.short_daily_rate(1000, 1001, 200, 100)
        after = funding.short_daily_rate(1000, 1001, 200, 100, extra_short=100)
        self.assertAlmostEqual(after, before / 2, places=9)

    def test_parse_keeps_only_configured_usdc_perps(self):
        rows = [funding_row(), funding_row(symbol="BTC-USDC-LINK", mark=60000, fair=60030),
                funding_row(symbol="DOGE-USDC-LINK"), funding_row(symbol="ETH-USDB-PYTH")]
        markets = funding.parse_markets(rows)
        self.assertEqual(set(markets), {"ETH", "BTC"})
        self.assertEqual(markets["ETH"]["symbol"], "ETH-USDC-LINK")

    def test_history_average_and_minimum(self):
        m = funding.parse_markets([funding_row()])
        out = funding.attach_history(m, {"ETH": [10.0, 20.0]})["ETH"]
        self.assertEqual(out["observations"], 3)
        self.assertAlmostEqual(out["avg_apr"], (10 + 20 + out["short_apr"]) / 3)
        self.assertTrue(out["enough_history"])

    def test_entry_apr_accounts_for_dilution(self):
        m = market()
        small = funding.entry_apr(m, 100)
        large = funding.entry_apr(m, 3000 * 80)   # quanto tutti gli short esistenti
        self.assertLess(large, small)
        self.assertAlmostEqual(large, m["avg_apr"] / 2, places=6)

    def test_breakeven(self):
        self.assertIsNone(funding.breakeven_days(1000, 0))
        days = funding.breakeven_days(1000, 36.5)   # 1$/giorno
        self.assertAlmostEqual(days, funding.round_trip_cost(1000), places=6)


class AgentParsingTest(unittest.TestCase):
    def test_normalizes_asset_and_operation(self):
        d = _clean_and_parse_json('{"operation": "OPEN", "asset": "weth", "target_portion_of_portfolio": 2}')
        self.assertEqual((d["operation"], d["asset"], d["target_portion_of_portfolio"]), ("open", "ETH", 1.0))

    def test_unknown_operation_becomes_hold(self):
        d = _clean_and_parse_json('```json\n{"operation": "long", "symbol": "BTC-USDC-LINK"}\n```')
        self.assertEqual((d["operation"], d["asset"]), ("hold", "BTC"))


class PaperBookTest(TmpState):
    def test_open_is_price_neutral(self):
        book = PaperBook(start_usdc=1000)
        m = market(mark=3000, fair=3000.0001)
        book.open("ETH", m, 600, 2.0, 20.0)
        pos = book.positions["ETH"]
        self.assertAlmostEqual(pos["spot_qty"], pos["perp_size"])
        v0 = book.position_value(pos, 3000, 3000)
        for price in (2000, 4500):
            self.assertAlmostEqual(book.position_value(pos, price, price), v0, places=6)

    def test_funding_accrues_on_the_short(self):
        book = PaperBook(start_usdc=1000)
        m = market(mark=1000, fair=1001, long_=200, short=100)   # 0.2% al giorno
        book.open("ETH", m, 600, 2.0, 73.0)
        pos = book.positions["ETH"]
        margin0 = pos["margin_usd"]
        earned = book.accrue({"ETH": m}, now=pos["last_accrual"] + 86_400)
        notional = pos["perp_size"] * 1000
        self.assertAlmostEqual(earned, notional * 0.002, places=6)
        self.assertAlmostEqual(book.positions["ETH"]["margin_usd"], margin0 + earned, places=6)

    def test_close_returns_capital_minus_costs(self):
        book = PaperBook(start_usdc=1000)
        m = market(mark=3000, fair=3000.0001)
        book.open("ETH", m, 600, 2.0, 20.0)
        res = book.close("ETH", m)
        self.assertLess(res["pnl_usd"], 0)            # solo costi, nessun funding
        self.assertGreater(res["pnl_usd"], -5)
        self.assertNotIn("ETH", book.positions)


class ManagerLimitsTest(TmpState):
    def setUp(self):
        super().setUp()
        config.PAPER_TRADING = True
        self.mgr = NeutralManager(client=None, synfutures=None)

    def status(self):
        return self.mgr.get_account_status({})

    def decision(self, portion=0.5, asset="ETH"):
        return {"operation": "open", "asset": asset, "target_portion_of_portfolio": portion}

    def test_good_market_opens_in_paper(self):
        m = market(mark=1000, fair=1001, long_=2000, short=1000)
        res = self.mgr.execute_signal(self.decision(), self.status(), {"ETH": m})
        self.assertEqual(res["status"], "paper", res)
        self.assertAlmostEqual(res["amount_usd"], 500)

    def test_rejections_keep_reason(self):
        good = market(mark=1000, fair=1001, long_=2000, short=1000)
        thin = dict(good, enough_history=False, observations=1)
        cheap = market(mark=1000, fair=1000.001, long_=100, short=100)
        paying = dict(good, short_apr=-5.0)
        cases = {
            "non gestito": (self.decision(asset="DOGE"), {"ETH": good}),
            "insufficiente": (self.decision(), {"ETH": thin}),
            "sotto il minimo di": (self.decision(), {"ETH": cheap}),
            "stanno pagando": (self.decision(), {"ETH": paying}),
            "capitale": (self.decision(portion=0.01), {"ETH": good}),
        }
        for needle, (dec, markets) in cases.items():
            res = self.mgr.execute_signal(dec, self.status(), markets)
            self.assertEqual(res["status"], "rejected", needle)
            self.assertIn(needle, res["reason"], f"{needle}: {res['reason']}")

    def test_no_second_pair_on_same_asset(self):
        m = market(mark=1000, fair=1001, long_=2000, short=1000)
        self.mgr.execute_signal(self.decision(0.3), self.status(), {"ETH": m})
        res = self.mgr.execute_signal(self.decision(0.3), self.mgr.get_account_status({"ETH": m}), {"ETH": m})
        self.assertIn("gia' aperta", res["reason"])

    def test_risk_exits(self):
        m = market(mark=1000, fair=1001, long_=2000, short=1000)
        self.mgr.execute_signal(self.decision(), self.status(), {"ETH": m})
        # prezzo +60%: lo short perde margine, la leva effettiva esplode
        up = dict(m, mark_price=1600.0, spot_price=1600.0)
        exits = self.mgr.risk_exits(self.mgr.get_account_status({"ETH": up}))
        self.assertTrue(exits and "leva effettiva" in exits[0]["trigger"], exits)
        # funding crollato
        panic = dict(m, short_apr=-50.0, avg_apr=-50.0)
        exits = self.mgr.risk_exits(self.mgr.get_account_status({"ETH": panic}))
        self.assertTrue(exits and "panico" in exits[0]["trigger"], exits)
        # nessun problema: nessuna uscita
        self.assertEqual(self.mgr.risk_exits(self.mgr.get_account_status({"ETH": m})), [])

    def test_close_decision(self):
        m = market(mark=1000, fair=1001, long_=2000, short=1000)
        self.mgr.execute_signal(self.decision(), self.status(), {"ETH": m})
        res = self.mgr.execute_signal({"operation": "close", "asset": "ETH"},
                                      self.mgr.get_account_status({"ETH": m}), {"ETH": m})
        self.assertEqual(res["status"], "paper")
        res = self.mgr.execute_signal({"operation": "close", "asset": "ETH"}, self.status(), {"ETH": m})
        self.assertEqual(res["status"], "rejected")


class FundingHistoryTest(TmpState):
    def test_history_roundtrip(self):
        m = funding.parse_markets([funding_row()])
        db_utils.log_funding(m)
        db_utils.log_funding(m)
        hist = db_utils.funding_history(24)
        self.assertEqual(len(hist["ETH"]), 2)
        data = db_utils.fetch_dashboard_data()
        self.assertEqual(data["funding_latest"][0]["asset"], "ETH")


class GateMarginTest(unittest.TestCase):
    def test_encode_gate_param(self):
        from base_client import encode_gate_param
        # 100 USDC (100,000,000 raw) + USDC address on Base
        packed = encode_gate_param(config.USDC, 100000000)
        self.assertEqual(len(packed), 32)
        # Check amount part (first 12 bytes)
        self.assertEqual(int.from_bytes(packed[:12], "big"), 100000000)
        # Check token part (last 20 bytes)
        self.assertEqual("0x" + packed[12:].hex().lower(), config.USDC.lower())

    def test_recommended_gate_usdc(self):
        # min per pair: (150 / 3) / 0.9 * 1.20 = 50 / 0.9 * 1.20 = 66.67
        min_pair = config.min_gate_usdc_per_pair()
        self.assertAlmostEqual(min_pair, 66.67, places=2)

        # 2 configured assets (ETH, BTC) -> min 2 * 66.67 = 133.34
        rec = config.recommended_gate_usdc()
        self.assertAlmostEqual(rec, len(config.NEUTRAL_ASSETS) * min_pair, places=2)

        # Custom capital $1000: (1000 / 3) / 0.9 * 1.20 = 444.44
        rec_1000 = config.recommended_gate_usdc(1000.0)
        self.assertAlmostEqual(rec_1000, 444.44, places=2)

    def test_check_open_with_existing_gate_balance(self):
        config.PAPER_TRADING = False
        mgr = NeutralManager(client=None, synfutures=None)
        m = market(mark=1000, fair=1001, long_=2000, short=1000)

        # Scenario: Total portfolio = $300 (idle = $300).
        # Target portion = 0.5 -> Capital = $150 ($100 notional, $50 margin).
        # Gate already has $50 USDC.
        # Wallet has $100 USDC.
        # This SHOULD pass without requiring extra Gate deposit.
        status = {
            "total_value_usd": 300.0,
            "idle_usd": 300.0,
            "usdc_balance": 100.0,
            "gate_usdc": 50.0,
            "eth_balance": 0.01,
            "positions": [],
        }
        decision = {"operation": "open", "asset": "ETH", "target_portion_of_portfolio": 0.5}
        reason, plan = mgr.check_open(decision, status, {"ETH": m})
        self.assertIsNone(reason)
        self.assertEqual(plan["capital_usd"], 150.0)
        self.assertEqual(plan["notional_usd"], 100.0)
        self.assertEqual(plan["margin_usd"], 50.0)

    def test_release_funds(self):
        mgr = NeutralManager()
        res = mgr.release_funds()
        self.assertEqual(res.get("status"), "success")
        self.assertIn("wallet_usdc", res)


if __name__ == "__main__":
    unittest.main()
