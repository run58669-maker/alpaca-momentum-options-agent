"""Tests for the strategy, journal and the --dry end-to-end path.

Stdlib only (unittest + asyncio) so it runs with no deps and no API keys:
    py -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from agent import run_once  # noqa: E402
from journal import Journal  # noqa: E402
from mcp_client import MockAlpacaMCPClient  # noqa: E402
from strategy import MomentumRiskCapStrategy  # noqa: E402


def bars_from_closes(closes: list[float]) -> list[dict]:
    return [{"c": c} for c in closes]


# Fixed clock so DTE-window assertions stay true whatever day the suite runs on.
AS_OF = date(2026, 8, 24)

CHAIN = [
    {"symbol": "T240906C00100000", "type": "call", "strike": 100.0, "expiry": "2026-09-06", "last_price": 1.5},
    {"symbol": "T240906P00098000", "type": "put", "strike": 98.0, "expiry": "2026-09-06", "last_price": 1.5},
]


def contract(kind: str, strike: float, dte: int, price: float = 1.5) -> dict:
    expiry = (AS_OF + timedelta(days=dte)).isoformat()
    letter = "C" if kind == "call" else "P"
    return {
        "symbol": f"X{expiry.replace('-', '')}{letter}{int(strike * 1000):08d}",
        "type": kind,
        "strike": strike,
        "expiry": expiry,
        "last_price": price,
    }


class TestComputeMomentum(unittest.TestCase):
    def test_percent_change_over_lookback(self):
        s = MomentumRiskCapStrategy(lookback=10)
        self.assertAlmostEqual(s.compute_momentum(bars_from_closes([100.0] * 9 + [110.0])), 0.10)

    def test_only_last_lookback_bars_count(self):
        # 20 bars, lookback 10: the first 10 must be ignored entirely.
        s = MomentumRiskCapStrategy(lookback=10)
        closes = [1.0] * 10 + [100.0] * 9 + [105.0]
        self.assertAlmostEqual(s.compute_momentum(bars_from_closes(closes)), 0.05)

    def test_fewer_than_two_bars_is_flat(self):
        s = MomentumRiskCapStrategy(lookback=10)
        self.assertEqual(s.compute_momentum([]), 0.0)
        self.assertEqual(s.compute_momentum(bars_from_closes([100.0])), 0.0)

    def test_negative_momentum(self):
        s = MomentumRiskCapStrategy(lookback=10)
        self.assertAlmostEqual(s.compute_momentum(bars_from_closes([100.0] * 9 + [90.0])), -0.10)


class TestSizePosition(unittest.TestCase):
    def test_budget_divided_by_contract_notional(self):
        # 1% of 100k = $1000 budget; $1.50/share x100 = $150/contract -> 6, capped at 5.
        s = MomentumRiskCapStrategy(risk_pct=0.01, max_contracts=5)
        self.assertEqual(s.size_position(100_000.0, 1.5), 5)

    def test_below_cap_is_not_padded(self):
        s = MomentumRiskCapStrategy(risk_pct=0.01, max_contracts=50)
        self.assertEqual(s.size_position(100_000.0, 1.5), 6)

    def test_expensive_contract_gives_zero(self):
        s = MomentumRiskCapStrategy(risk_pct=0.01, max_contracts=5)
        self.assertEqual(s.size_position(10_000.0, 5.0), 0)

    def test_nonpositive_price_gives_zero(self):
        s = MomentumRiskCapStrategy()
        self.assertEqual(s.size_position(100_000.0, 0.0), 0)
        self.assertEqual(s.size_position(100_000.0, -1.0), 0)


class TestDecide(unittest.TestCase):
    def setUp(self):
        self.s = MomentumRiskCapStrategy(
            lookback=10, momentum_threshold=0.02, risk_pct=0.01, max_contracts=5, max_daily_loss_pct=0.03
        )

    def decide(self, closes, equity=100_000.0, realized_loss=0.0, chain=CHAIN, strategy=None):
        return (strategy or self.s).decide(
            symbol="TEST",
            bars=bars_from_closes(closes),
            option_chain=chain,
            equity=equity,
            realized_loss_today=realized_loss,
            as_of=AS_OF,
        )

    def test_bullish_buys_call(self):
        d = self.decide([100.0] * 9 + [110.0])
        self.assertEqual(d.action, "buy_call")
        self.assertEqual(d.contracts, 5)
        self.assertIn("T240906C00100000", d.reason)

    def test_bearish_buys_put(self):
        d = self.decide([100.0] * 9 + [90.0])
        self.assertEqual(d.action, "buy_put")
        self.assertEqual(d.contracts, 5)
        self.assertIn("T240906P00098000", d.reason)

    def test_weak_signal_holds(self):
        d = self.decide([100.0] * 9 + [100.5])  # +0.5% < 2% threshold
        self.assertEqual(d.action, "hold")
        self.assertEqual(d.contracts, 0)
        self.assertIn("below", d.reason)

    def test_exactly_at_threshold_trades(self):
        d = self.decide([100.0] * 9 + [102.0])  # exactly +2%
        self.assertEqual(d.action, "buy_call")

    def test_circuit_breaker_forces_hold_on_strong_signal(self):
        d = self.decide([100.0] * 9 + [110.0], realized_loss=3_000.0)  # 3% of 100k
        self.assertEqual(d.action, "hold")
        self.assertEqual(d.contracts, 0)
        self.assertIn("circuit breaker", d.reason)

    def test_loss_just_under_the_cap_still_trades(self):
        d = self.decide([100.0] * 9 + [110.0], realized_loss=2_999.99)
        self.assertEqual(d.action, "buy_call")

    def test_missing_contract_type_holds(self):
        calls_only = [c for c in CHAIN if c["type"] == "call"]
        d = self.decide([100.0] * 9 + [90.0], chain=calls_only)  # bearish, no put available
        self.assertEqual(d.action, "hold")
        self.assertIn("no suitable put", d.reason)
        self.assertIsNone(d.contract)

    def test_trade_decision_carries_the_contract_it_named(self):
        d = self.decide([100.0] * 9 + [110.0])
        self.assertIsNotNone(d.contract)
        self.assertIn(d.contract["symbol"], d.reason)

    def test_zero_affordable_contracts_holds(self):
        s = MomentumRiskCapStrategy(lookback=10, momentum_threshold=0.02, risk_pct=0.01, max_contracts=5)
        d = self.decide([100.0] * 9 + [110.0], equity=1_000.0, strategy=s)  # $10 budget, $150/contract
        self.assertEqual(d.action, "hold")
        self.assertIn("buys 0 contracts", d.reason)

    def test_every_decision_carries_a_reason_and_momentum(self):
        cases = [([100.0] * 9 + [110.0], 0.0), ([100.0] * 9 + [100.1], 0.0), ([100.0] * 9 + [110.0], 9e9)]
        for closes, loss in cases:
            d = self.decide(closes, realized_loss=loss)
            self.assertTrue(d.reason.strip(), "reason must never be empty")
            self.assertIsInstance(d.momentum_pct, float)


class TestJournal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "nested" / "decisions.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_log_appends_one_line_per_record_with_ts(self):
        j = Journal(self.path)
        j.log(action="hold", contracts=0)
        j.log(action="buy_call", contracts=5)
        lines = self.path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0])
        self.assertEqual(first["action"], "hold")
        self.assertIn("ts", first)

    def test_missing_file_reports_no_loss(self):
        self.assertEqual(Journal(self.path).realized_loss_today(), 0.0)

    def test_sums_only_todays_negative_pnl(self):
        j = Journal(self.path)
        now = datetime.now(timezone.utc).isoformat()
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        rows = [
            {"ts": now, "realized_pnl": -500.0},
            {"ts": now, "realized_pnl": -250.0},
            {"ts": now, "realized_pnl": 1_000.0},      # profit ignored
            {"ts": yesterday, "realized_pnl": -9_999.0},  # other day ignored
            {"ts": now, "action": "hold"},              # no pnl field
        ]
        with self.path.open("w", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(r) for r in rows) + "\n\n")  # trailing blank line too
        self.assertAlmostEqual(j.realized_loss_today(), 750.0)


def long_leg_symbol(order: dict) -> str:
    """The bought contract, for either order shape (single-leg or two-leg spread)."""
    return order["legs"][0]["symbol"] if "legs" in order else order["symbol"]


class TestDryRunEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "decisions.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def run_passes(self, strategy, n=1):
        async def go():
            journal = Journal(self.path)
            async with MockAlpacaMCPClient() as client:
                return [await run_once(client, strategy, journal, "SPY") for _ in range(n)]

        return asyncio.run(go())

    def test_dry_run_needs_no_keys_and_journals_every_pass(self):
        records = self.run_passes(MomentumRiskCapStrategy(), n=3)
        self.assertEqual(len(records), 3)
        on_disk_all = [json.loads(l) for l in self.path.read_text(encoding="utf-8").strip().splitlines()]
        decisions = [r for r in on_disk_all if "action" in r]
        # Three passes, three decisions -- plus exactly ONE realized-P&L record, even
        # though all three passes reconciled the same closed position.
        self.assertEqual(len(decisions), 3)
        self.assertEqual(len([r for r in on_disk_all if "realized_pnl" in r]), 1)
        for on_disk, record in zip(decisions, records):
            self.assertEqual(on_disk["reason"], record["reason"])
            self.assertEqual(on_disk["symbol"], "SPY")
            for field in ("ts", "market_open", "equity", "momentum_pct", "action", "contracts", "reason", "order"):
                self.assertIn(field, on_disk)

    def test_mock_uptrend_buys_a_call_spread_and_fills_an_order(self):
        # The mock chain has seven strikes per expiry, so the defined-risk path is
        # the one that runs: a call debit spread, not a naked long.
        record = self.run_passes(MomentumRiskCapStrategy())[0]
        self.assertEqual(record["action"], "buy_call_spread")
        self.assertEqual(record["order"]["status"], "filled")
        self.assertEqual(record["order"]["qty"], record["contracts"])
        self.assertEqual([leg["side"] for leg in record["order"]["legs"]], ["buy", "sell"])

    def test_dry_run_max_loss_is_the_debit_it_actually_ordered(self):
        record = self.run_passes(MomentumRiskCapStrategy())[0]
        debit = float(record["order"]["limit_price"])
        self.assertAlmostEqual(record["max_loss"], debit * 100 * record["contracts"], places=2)

    def test_dry_run_spread_sells_a_higher_strike_than_it_buys(self):
        async def chain_of(symbol):
            async with MockAlpacaMCPClient() as c:
                return await c.get_option_chain(symbol)

        chain = {c["symbol"]: c for c in asyncio.run(chain_of("SPY"))}
        legs = self.run_passes(MomentumRiskCapStrategy())[0]["order"]["legs"]
        bought, sold = chain[legs[0]["symbol"]], chain[legs[1]["symbol"]]
        self.assertGreater(sold["strike"], bought["strike"], "a call debit spread sells the higher strike")
        self.assertEqual(sold["expiry"], bought["expiry"], "both legs must share an expiry")
        self.assertLess(sold["last_price"], bought["last_price"], "the short leg must be the cheaper one")

    def test_order_is_for_the_contracts_the_reason_names(self):
        record = self.run_passes(MomentumRiskCapStrategy())[0]
        for leg in record["order"]["legs"]:
            self.assertIn(leg["symbol"], record["reason"])

    def test_dry_run_picks_an_in_window_contract_from_the_full_chain(self):
        async def go():
            async with MockAlpacaMCPClient() as c:
                return await c.get_option_chain("SPY")

        chain = asyncio.run(go())
        record = self.run_passes(MomentumRiskCapStrategy())[0]
        picked = next(c for c in chain if c["symbol"] == long_leg_symbol(record["order"]))
        expiry = datetime.strptime(picked["expiry"], "%Y-%m-%d").date()
        dte = (expiry - datetime.now(timezone.utc).date()).days
        self.assertTrue(7 <= dte <= 21, f"agent traded a {dte} DTE contract, outside its own window")
        self.assertNotEqual(picked["symbol"], chain[0]["symbol"], "must not just take the first chain row")

    def test_unreachable_threshold_holds_and_places_no_order(self):
        record = self.run_passes(MomentumRiskCapStrategy(momentum_threshold=10.0))[0]
        self.assertEqual(record["action"], "hold")
        self.assertIsNone(record["order"])

    def test_circuit_breaker_trips_from_journal_history(self):
        # A logged loss from earlier today must stop the next pass placing an order.
        Journal(self.path).log(symbol="SPY", realized_pnl=-5_000.0)
        record = self.run_passes(MomentumRiskCapStrategy(max_daily_loss_pct=0.03))[0]
        self.assertEqual(record["action"], "hold")
        self.assertIsNone(record["order"])
        self.assertIn("circuit breaker", record["reason"])


class TestSelectContract(unittest.TestCase):
    """Contract choice must be the near-ATM one inside the DTE window, not row 0."""

    def setUp(self):
        self.s = MomentumRiskCapStrategy(min_dte=7, max_dte=21)

    def test_picks_strike_nearest_spot_not_first_row(self):
        chain = [contract("call", 120.0, 14), contract("call", 101.0, 14), contract("call", 90.0, 14)]
        picked, note = self.s.select_contract(chain, "call", spot=100.0, as_of=AS_OF)
        self.assertEqual(picked["strike"], 101.0)
        self.assertIn("$1.00 from spot", note)

    def test_expiries_outside_the_window_are_excluded(self):
        # The 100-strike is a perfect ATM match but expires in 2 days -> must be skipped.
        chain = [contract("call", 100.0, 2), contract("call", 105.0, 14), contract("call", 100.0, 60)]
        picked, _ = self.s.select_contract(chain, "call", spot=100.0, as_of=AS_OF)
        self.assertEqual(picked["strike"], 105.0)
        self.assertEqual(picked["expiry"], (AS_OF + timedelta(days=14)).isoformat())

    def test_window_edges_are_inclusive(self):
        for dte in (7, 21):
            picked, _ = self.s.select_contract([contract("call", 100.0, dte)], "call", 100.0, AS_OF)
            self.assertIsNotNone(picked, f"{dte} DTE is on the window edge and must qualify")
        for dte in (6, 22):
            picked, _ = self.s.select_contract([contract("call", 100.0, dte)], "call", 100.0, AS_OF)
            self.assertIsNone(picked, f"{dte} DTE is outside the window and must be rejected")

    def test_equal_strike_distance_breaks_toward_mid_window(self):
        # Same strike, 8d vs 14d vs 20d: 14 is nearest the middle of a 7-21 window.
        chain = [contract("call", 100.0, 8), contract("call", 100.0, 14), contract("call", 100.0, 20)]
        picked, _ = self.s.select_contract(chain, "call", spot=100.0, as_of=AS_OF)
        self.assertEqual(picked["expiry"], (AS_OF + timedelta(days=14)).isoformat())

    def test_selection_is_deterministic_regardless_of_chain_order(self):
        chain = [contract("call", 99.0, 14), contract("call", 101.0, 14), contract("call", 103.0, 10)]
        first, _ = self.s.select_contract(chain, "call", 100.0, AS_OF)
        second, _ = self.s.select_contract(list(reversed(chain)), "call", 100.0, AS_OF)
        self.assertEqual(first["symbol"], second["symbol"])

    def test_wrong_type_is_never_returned(self):
        chain = [contract("put", 100.0, 14), contract("call", 130.0, 14)]
        picked, _ = self.s.select_contract(chain, "call", spot=100.0, as_of=AS_OF)
        self.assertEqual(picked["type"], "call")

    def test_empty_window_explains_what_was_available(self):
        chain = [contract("call", 100.0, 2), contract("call", 100.0, 60)]
        picked, note = self.s.select_contract(chain, "call", spot=100.0, as_of=AS_OF)
        self.assertIsNone(picked)
        self.assertIn("2d", note)
        self.assertIn("60d", note)

    def test_unparseable_rows_are_skipped_not_traded(self):
        broken = [
            {"symbol": "BAD1", "type": "call", "strike": 100.0},                       # no expiry
            {"symbol": "BAD2", "type": "call", "strike": None, "expiry": "2026-09-06"},
            {"symbol": "BAD3", "type": "call", "strike": 100.0, "expiry": "not-a-date"},
        ]
        picked, _ = self.s.select_contract(broken, "call", spot=100.0, as_of=AS_OF)
        self.assertIsNone(picked)
        good = broken + [contract("call", 100.0, 14)]
        picked, _ = self.s.select_contract(good, "call", spot=100.0, as_of=AS_OF)
        self.assertEqual(picked["symbol"], contract("call", 100.0, 14)["symbol"])


class TestMockChain(unittest.TestCase):
    def test_chain_spans_strikes_and_expiries_around_spot(self):
        async def go():
            async with MockAlpacaMCPClient() as c:
                return await c.get_stock_bars("SPY", limit=11), await c.get_option_chain("SPY")

        bars, chain = asyncio.run(go())
        spot = bars[-1]["c"]
        self.assertEqual(len(chain), len(MockAlpacaMCPClient.CHAIN_EXPIRY_DAYS) * 7 * 2)
        self.assertEqual({c["type"] for c in chain}, {"call", "put"})
        self.assertEqual(
            len({c["expiry"] for c in chain}), len(MockAlpacaMCPClient.CHAIN_EXPIRY_DAYS)
        )
        strikes = sorted({c["strike"] for c in chain})
        self.assertLess(strikes[0], spot)
        self.assertGreater(strikes[-1], spot)
        self.assertTrue(all(c["last_price"] > 0 for c in chain))

    def test_extrinsic_value_decays_away_from_spot(self):
        # A flat time value across strikes makes every vertical spread cost only its
        # intrinsic difference -- a 2-wide spread for 2 cents -- which would make the
        # defined-risk demo nonsense. Extrinsic must shrink as the strike leaves spot.
        async def go():
            async with MockAlpacaMCPClient() as c:
                return await c.get_stock_bars("SPY", limit=11), await c.get_option_chain("SPY")

        bars, chain = asyncio.run(go())
        spot = bars[-1]["c"]
        expiry = sorted({c["expiry"] for c in chain})[1]
        calls = sorted(
            (c for c in chain if c["type"] == "call" and c["expiry"] == expiry),
            key=lambda c: c["strike"],
        )
        extrinsic = [(c["strike"], c["last_price"] - max(spot - c["strike"], 0.0)) for c in calls]
        atm = min(extrinsic, key=lambda p: abs(p[0] - spot))
        farthest = max(extrinsic, key=lambda p: abs(p[0] - spot))
        self.assertLess(farthest[1], atm[1] / 2, "extrinsic must decay well away from spot")


if __name__ == "__main__":
    unittest.main()
