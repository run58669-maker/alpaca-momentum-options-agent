"""Tests for the defined-risk leg: `select_spread`, the sizing that follows from it,
and the multi-leg order that leaves the client.

Stdlib only (unittest + asyncio), no keys, no network:
    py -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from mcp_client import AlpacaMCPClient, MockAlpacaMCPClient  # noqa: E402
from strategy import MomentumRiskCapStrategy  # noqa: E402

AS_OF = date(2026, 8, 24)


def contract(kind: str, strike: float, dte: int, price: float) -> dict:
    expiry = (AS_OF + timedelta(days=dte)).isoformat()
    letter = "C" if kind == "call" else "P"
    return {
        "symbol": f"X{expiry[2:].replace('-', '')}{letter}{int(strike * 1000):08d}",
        "type": kind,
        "strike": strike,
        "expiry": expiry,
        "last_price": price,
    }


def bars_from_closes(closes: list[float]) -> list[dict]:
    return [{"c": c} for c in closes]


class TestSelectSpread(unittest.TestCase):
    """The short leg must be farther OTM, same expiry, cheaper, near the target width."""

    def setUp(self):
        self.s = MomentumRiskCapStrategy(spread_width_pct=0.03)  # 3% of 100 spot = $3 wide

    def test_sells_the_strike_nearest_the_target_width(self):
        long_leg = contract("call", 100.0, 14, 5.00)
        chain = [
            long_leg,
            contract("call", 101.0, 14, 4.20),  # 1 wide
            contract("call", 103.0, 14, 3.10),  # 3 wide -- the target
            contract("call", 110.0, 14, 1.00),  # 10 wide
        ]
        short, note, _ = self.s.select_spread(chain, long_leg, spot=100.0)
        self.assertEqual(short["strike"], 103.0)
        self.assertIn("$3.00 wide", note)
        self.assertIn("$1.90 net debit", note)

    def test_a_call_spread_sells_a_higher_strike(self):
        long_leg = contract("call", 100.0, 14, 5.00)
        chain = [long_leg, contract("call", 97.0, 14, 7.00), contract("call", 103.0, 14, 3.10)]
        short, _, _ = self.s.select_spread(chain, long_leg, spot=100.0)
        self.assertGreater(short["strike"], long_leg["strike"])

    def test_a_put_spread_sells_a_lower_strike(self):
        long_leg = contract("put", 100.0, 14, 5.00)
        chain = [long_leg, contract("put", 103.0, 14, 7.00), contract("put", 97.0, 14, 3.10)]
        short, _, _ = self.s.select_spread(chain, long_leg, spot=100.0)
        self.assertLess(short["strike"], long_leg["strike"])
        self.assertEqual(short["strike"], 97.0)

    def test_a_different_expiry_is_never_sold_against_it(self):
        # Same strike distance, but a calendar spread is not the structure we sized for.
        long_leg = contract("call", 100.0, 14, 5.00)
        chain = [long_leg, contract("call", 103.0, 21, 3.10)]
        short, note, _ = self.s.select_spread(chain, long_leg, spot=100.0)
        self.assertIsNone(short)
        self.assertIn("naked", note)

    def test_a_leg_that_is_not_cheaper_is_rejected(self):
        # Selling something worth more than the long leg is a credit spread with
        # unbounded-ish risk on the wrong side -- refuse rather than mislabel it.
        long_leg = contract("call", 100.0, 14, 5.00)
        chain = [long_leg, contract("call", 103.0, 14, 5.00), contract("call", 105.0, 14, 6.00)]
        short, note, _ = self.s.select_spread(chain, long_leg, spot=100.0)
        self.assertIsNone(short)
        self.assertIn("no cheaper call strike", note)

    def test_the_long_leg_is_never_sold_against_itself(self):
        long_leg = contract("call", 100.0, 14, 5.00)
        short, _, _ = self.s.select_spread([long_leg], long_leg, spot=100.0)
        self.assertIsNone(short)

    def test_a_wrong_type_leg_is_never_sold_against_it(self):
        long_leg = contract("call", 100.0, 14, 5.00)
        chain = [long_leg, contract("put", 103.0, 14, 3.10)]
        short, _, _ = self.s.select_spread(chain, long_leg, spot=100.0)
        self.assertIsNone(short)

    def test_choice_is_deterministic_regardless_of_chain_order(self):
        long_leg = contract("call", 100.0, 14, 5.00)
        chain = [long_leg, contract("call", 102.0, 14, 3.50), contract("call", 104.0, 14, 2.50)]
        first, _, _ = self.s.select_spread(chain, long_leg, spot=100.0)
        second, _, _ = self.s.select_spread(list(reversed(chain)), long_leg, spot=100.0)
        self.assertEqual(first["symbol"], second["symbol"])

    def test_zero_width_pct_disables_spreads(self):
        s = MomentumRiskCapStrategy(spread_width_pct=0.0)
        long_leg = contract("call", 100.0, 14, 5.00)
        chain = [long_leg, contract("call", 103.0, 14, 3.10)]
        short, note, _ = s.select_spread(chain, long_leg, spot=100.0)
        self.assertIsNone(short)
        self.assertIn("naked", note)

    def test_unparseable_rows_are_skipped_not_sold(self):
        long_leg = contract("call", 100.0, 14, 5.00)
        expiry = long_leg["expiry"]
        broken = [
            {"symbol": "BAD1", "type": "call", "expiry": expiry, "last_price": 1.0},   # no strike
            {"symbol": "BAD2", "type": "call", "expiry": expiry, "strike": 103.0},     # no price
            {"symbol": "BAD3", "type": "call", "expiry": expiry, "strike": None, "last_price": 1.0},
        ]
        short, _, _ = self.s.select_spread([long_leg] + broken, long_leg, spot=100.0)
        self.assertIsNone(short)
        good = contract("call", 103.0, 14, 3.10)
        short, _, _ = self.s.select_spread([long_leg] + broken + [good], long_leg, spot=100.0)
        self.assertEqual(short["symbol"], good["symbol"])

    def test_a_long_leg_without_a_usable_price_builds_no_spread(self):
        long_leg = {"symbol": "X", "type": "call", "strike": 100.0, "expiry": "2026-09-07"}
        short, note, _ = self.s.select_spread([contract("call", 103.0, 14, 3.10)], long_leg, spot=100.0)
        self.assertIsNone(short)
        self.assertIn("cannot build a spread", note)


class TestDecideBuildsDefinedRisk(unittest.TestCase):
    """The spread has to change the decision, not just decorate the reason."""

    def setUp(self):
        self.s = MomentumRiskCapStrategy(
            lookback=10, momentum_threshold=0.02, risk_pct=0.01,
            max_contracts=50, spread_width_pct=0.03,
        )
        self.long_leg = contract("call", 100.0, 14, 5.00)
        self.short_leg = contract("call", 103.0, 14, 3.10)

    def decide(self, chain, equity=100_000.0, strategy=None):
        return (strategy or self.s).decide(
            symbol="TEST",
            # Ends at 100 so spot -- and therefore the long leg select_contract
            # picks -- is the 100 strike these fixtures are built around.
            bars=bars_from_closes([90.0] * 9 + [100.0]),  # +11%, bullish
            option_chain=chain,
            equity=equity,
            realized_loss_today=0.0,
            as_of=AS_OF,
        )

    def test_spread_action_carries_both_legs_and_the_net_debit(self):
        d = self.decide([self.long_leg, self.short_leg])
        self.assertEqual(d.action, "buy_call_spread")
        self.assertEqual(d.contract["symbol"], self.long_leg["symbol"])
        self.assertEqual(d.short_contract["symbol"], self.short_leg["symbol"])
        self.assertAlmostEqual(d.net_debit, 1.90)

    def test_max_loss_is_the_debit_and_the_reason_states_it(self):
        d = self.decide([self.long_leg, self.short_leg])
        self.assertAlmostEqual(d.max_loss, d.net_debit * 100 * d.contracts)
        self.assertIn(f"max loss ${d.max_loss:.2f}", d.reason)
        # $1000 budget / $190 per spread = 5.
        self.assertEqual(d.contracts, 5)

    def test_sizing_uses_the_debit_so_a_spread_buys_more_than_the_naked_long(self):
        naked = self.decide([self.long_leg])
        spread = self.decide([self.long_leg, self.short_leg])
        self.assertEqual(naked.action, "buy_call")
        self.assertEqual(naked.contracts, 2)   # $1000 / $500 per contract
        self.assertGreater(spread.contracts, naked.contracts)

    def test_naked_fallback_still_reports_full_premium_as_max_loss(self):
        d = self.decide([self.long_leg])
        self.assertIsNone(d.short_contract)
        self.assertAlmostEqual(d.max_loss, 5.00 * 100 * d.contracts)
        self.assertIn("naked", d.reason)

    def test_a_hold_takes_no_risk(self):
        d = self.s.decide(
            symbol="TEST",
            bars=bars_from_closes([100.0] * 10),  # flat -> hold, spot 100
            option_chain=[self.long_leg, self.short_leg],
            equity=100_000.0,
            realized_loss_today=0.0,
            as_of=AS_OF,
        )
        self.assertEqual(d.action, "hold")
        self.assertEqual(d.max_loss, 0.0)

    def test_bearish_builds_a_put_spread(self):
        long_put, short_put = contract("put", 100.0, 14, 5.00), contract("put", 97.0, 14, 3.10)
        d = self.s.decide(
            symbol="TEST",
            bars=bars_from_closes([110.0] * 9 + [100.0]),  # -9%, bearish, spot 100
            option_chain=[long_put, short_put],
            equity=100_000.0,
            realized_loss_today=0.0,
            as_of=AS_OF,
        )
        self.assertEqual(d.action, "buy_put_spread")
        self.assertEqual(d.short_contract["symbol"], short_put["symbol"])

    def test_a_debit_too_big_for_the_budget_still_holds(self):
        d = self.decide([self.long_leg, self.short_leg], equity=1_000.0)  # $10 budget
        self.assertEqual(d.action, "hold")
        self.assertIn("buys 0 spreads", d.reason)


class TestSpreadOrderOnTheWire(unittest.TestCase):
    """What actually leaves the client for a two-leg order."""

    def test_mock_fills_a_two_leg_order_at_the_net_debit(self):
        async def go():
            async with MockAlpacaMCPClient() as c:
                return await c.place_option_spread_order("LONGSYM", "SHORTSYM", 3, 1.9)

        order = asyncio.run(go())
        self.assertEqual(order["status"], "filled")
        self.assertEqual(order["qty"], 3)
        self.assertEqual(order["limit_price"], "1.90")
        self.assertEqual([leg["symbol"] for leg in order["legs"]], ["LONGSYM", "SHORTSYM"])
        self.assertEqual([leg["side"] for leg in order["legs"]], ["buy", "sell"])

    def test_real_client_sends_the_verified_multileg_shape(self):
        # Bypass __init__ so no keys are needed; only the outgoing args are under test.
        client = object.__new__(AlpacaMCPClient)
        sent = {}

        async def fake_call(tool, args):
            sent["tool"], sent["args"] = tool, args
            return {"id": "ok"}

        client._call = fake_call
        asyncio.run(client.place_option_spread_order("LONGSYM", "SHORTSYM", 3, 1.9))

        self.assertEqual(sent["tool"], "place_option_order")
        args = sent["args"]
        # Upstream types qty and limit_price as strings (overrides.py:258-265).
        self.assertEqual(args["qty"], "3")
        self.assertEqual(args["limit_price"], "1.90")
        self.assertIsInstance(args["limit_price"], str)
        # A market multi-leg could fill worse than the debit the strategy sized on.
        self.assertEqual(args["type"], "limit")
        self.assertEqual(args["time_in_force"], "day")
        self.assertEqual(args["order_class"], "mleg")
        self.assertEqual(
            args["legs"],
            [
                {"symbol": "LONGSYM", "ratio_qty": "1", "side": "buy", "position_intent": "buy_to_open"},
                {"symbol": "SHORTSYM", "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"},
            ],
        )
        # Parent symbol/side belong to single-leg orders only.
        self.assertNotIn("symbol", args)
        self.assertNotIn("side", args)

    def test_single_leg_order_still_sends_the_single_leg_shape(self):
        client = object.__new__(AlpacaMCPClient)
        sent = {}

        async def fake_call(tool, args):
            sent["args"] = args
            return {"id": "ok"}

        client._call = fake_call
        asyncio.run(client.place_option_order("LONGSYM", "buy", 2, 2.35))
        self.assertEqual(sent["args"]["symbol"], "LONGSYM")
        self.assertEqual(sent["args"]["qty"], "2")
        self.assertNotIn("legs", sent["args"])
        # Single leg, but priced for the same reason the mleg above is: see
        # tests/test_naked_long_limit.py for the full argument.
        self.assertEqual(sent["args"]["type"], "limit")
        self.assertEqual(sent["args"]["limit_price"], "2.35")


if __name__ == "__main__":
    unittest.main()
