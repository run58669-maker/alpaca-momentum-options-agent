"""Tests for the account-level risk budget: `src/portfolio.py`, the cap inside
`MomentumRiskCapStrategy.decide`, and the whole pass through `run_once`.

The hole these exist for: `risk_pct` caps a single order at 1% of equity and nothing
capped the sum. Ten passes, ten orders, each one inside the limit, 10% of the account
at risk. Every test below is written so that removing the cap makes it fail -- see the
mutation runs recorded in PROGRESS.md 2026-08-25 22:00.

Stdlib only:
    py -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from agent import run_once  # noqa: E402
from exits import group_structures  # noqa: E402
from journal import Journal  # noqa: E402
from mcp_client import MockAlpacaMCPClient, normalize_positions  # noqa: E402
from portfolio import open_risk, structure_risk  # noqa: E402
from strategy import MomentumRiskCapStrategy  # noqa: E402


def structure(sid: str, cost_basis: float) -> dict:
    """The two fields `portfolio` reads. Grouping is `exits.group_structures`' job."""
    return {"id": sid, "cost_basis": cost_basis}


def wire_leg(root: str, dte: int, kind: str, strike: float, side: str,
             qty: int, entry: float, current: float) -> dict:
    """One `GET /v2/positions` row, in Alpaca's all-strings wire shape."""
    expiry = (datetime.now(timezone.utc) + timedelta(days=dte)).strftime("%y%m%d")
    letter = "C" if kind == "call" else "P"
    sign = 1 if side == "long" else -1
    cost_basis = sign * entry * qty * 100
    return {
        "symbol": f"{root}{expiry}{letter}{int(round(strike * 1000)):08d}",
        "asset_class": "us_option",
        "side": side,
        "qty": f"{sign * qty}",
        "qty_available": f"{sign * qty}",
        "avg_entry_price": f"{entry:.2f}",
        "current_price": f"{current:.2f}",
        "cost_basis": f"{cost_basis:.2f}",
        "unrealized_pl": f"{sign * (current - entry) * qty * 100:.2f}",
    }


class TestStructureRisk(unittest.TestCase):
    """What a structure can lose is its net debit -- and only when it *is* a debit."""

    def test_a_debit_structure_risks_what_it_cost(self):
        self.assertEqual(structure_risk(structure("SPY 2026-09-11 call", 440.0)), 440.0)

    def test_a_net_credit_structure_is_not_priceable_from_cost_basis(self):
        # A credit vertical's worst case is (strike width - credit), which cost basis
        # cannot express. Returning the credit here would report a $100 risk on a
        # structure that can lose $400.
        self.assertIsNone(structure_risk(structure("SPY 2026-09-11 put", -100.0)))

    def test_a_zero_cost_structure_is_not_priceable_either(self):
        # Long options cost money, so a zero net basis means short legs paid for the
        # long ones -- a risk reversal, not a free position.
        self.assertIsNone(structure_risk(structure("SPY 2026-09-11 call", 0.0)))

    def test_it_reads_the_wire_string_alpaca_actually_sends(self):
        # cost_basis arrives as a string on the wire; normalize_positions floats it,
        # but structure_risk must not assume that happened.
        self.assertEqual(structure_risk({"id": "X", "cost_basis": "440.00"}), 440.0)


class TestOpenRisk(unittest.TestCase):
    def test_it_sums_the_debits_and_breaks_them_down(self):
        total, unpriceable, breakdown = open_risk(
            [structure("A", 440.0), structure("B", 300.0)]
        )
        self.assertEqual(total, 740.0)
        self.assertEqual(unpriceable, [])
        self.assertEqual(breakdown, {"A": 440.0, "B": 300.0})

    def test_an_unpriceable_structure_is_named_and_left_out_of_the_total(self):
        total, unpriceable, breakdown = open_risk(
            [structure("A", 440.0), structure("B", -100.0)]
        )
        # Not silently counted as 0 and not silently counted as 100: named, so the
        # caller can refuse to trade on a total it knows is incomplete.
        self.assertEqual(total, 440.0)
        self.assertEqual(len(unpriceable), 1)
        self.assertIn("B", unpriceable[0])
        self.assertNotIn("B", breakdown)

    def test_an_empty_book_risks_nothing(self):
        self.assertEqual(open_risk([]), (0.0, [], {}))

    def test_it_reads_a_real_grouped_book(self):
        # End to end through the normalizer and the grouper: a debit vertical
        # (5 x $2.00 long against 5 x $0.80 short = $600 net) plus a naked long put.
        rows = [
            wire_leg("SPY", 14, "call", 100.0, "long", 5, 2.00, 3.60),
            wire_leg("SPY", 14, "call", 103.0, "short", 5, 0.80, 1.30),
            wire_leg("SPY", 10, "put", 96.0, "long", 3, 2.50, 0.90),
        ]
        total, unpriceable, breakdown = open_risk(
            group_structures(normalize_positions(rows))
        )
        self.assertEqual(unpriceable, [])
        self.assertEqual(len(breakdown), 2)
        self.assertEqual(total, 600.0 + 750.0)


class TestPortfolioCapInDecide(unittest.TestCase):
    """The strategy sizes against what is left of the account budget, not just its own."""

    EQUITY = 100_000.0

    def bars(self, drift: float = 0.008) -> list[dict]:
        price = 100.0
        out = []
        for _ in range(11):
            price *= 1 + drift
            out.append({"c": round(price, 2)})
        return out

    def chain(self) -> list[dict]:
        expiry = (datetime.now(timezone.utc) + timedelta(days=14)).strftime("%Y-%m-%d")
        stem = expiry[2:].replace("-", "")
        return [
            # `price` is the ASK, because that is what a buyer pays and therefore what
            # the size is computed from. Last trades a cent below it, so these rows
            # would size differently if anything ever went back to pricing off last.
            {"symbol": f"SPY{stem}C{int(strike * 1000):08d}", "type": "call",
             "strike": float(strike), "expiry": expiry,
             "last_price": round(price - 0.01, 2),
             "bid": round(price - 0.02, 2), "ask": price}
            for strike, price in ((105.0, 4.00), (108.0, 2.00), (111.0, 1.00))
        ]

    def decide(self, open_risk_dollars: float, **kwargs):
        strategy = MomentumRiskCapStrategy(spread_width_pct=0.0, max_spread_pct=0.0, **kwargs)
        return strategy.decide(
            symbol="SPY", bars=self.bars(), option_chain=self.chain(),
            equity=self.EQUITY, realized_loss_today=0.0, open_risk=open_risk_dollars,
        )

    def test_an_empty_book_trades_the_full_per_trade_budget(self):
        # Baseline: spot is $109.16, so the pick is the $108 strike, offered at $2.00 =
        # $200 a contract, and the $1,000 per-trade budget buys 5. Every assertion below
        # is a departure from this one.
        decision = self.decide(0.0)
        self.assertNotEqual(decision.action, "hold")
        self.assertEqual(decision.contracts, 5)
        self.assertEqual(decision.max_loss, 1_000.0)
        self.assertIn("portfolio headroom $5000.00", decision.reason)

    def test_a_book_at_the_cap_holds(self):
        decision = self.decide(5_000.0)
        self.assertEqual(decision.action, "hold")
        self.assertEqual(decision.contracts, 0)
        self.assertEqual(decision.max_loss, 0.0)
        self.assertIn("portfolio risk cap", decision.reason)
        self.assertIn("per order, not per account", decision.reason)

    def test_a_book_past_the_cap_holds_too(self):
        self.assertEqual(self.decide(9_999.0).action, "hold")

    def test_partial_headroom_shrinks_the_size_instead_of_refusing(self):
        # $4,800 committed leaves $200 of the $5,000 cap: one $200 contract, not five.
        # The per-trade cap alone would still have allowed five.
        decision = self.decide(4_800.0)
        self.assertEqual(decision.contracts, 1)
        self.assertEqual(decision.max_loss, 200.0)
        self.assertIn("binding budget (portfolio headroom, $200.00", decision.reason)

    def test_headroom_too_thin_for_one_contract_holds_and_says_which_budget_bound(self):
        # $4,900 leaves $100 -- less than one $200 contract. The distinction that
        # matters in the journal: this is not "the signal was weak".
        decision = self.decide(4_900.0)
        self.assertEqual(decision.action, "hold")
        self.assertIn("binding budget (portfolio headroom, $100.00", decision.reason)
        self.assertIn("buys 0", decision.reason)

    def test_the_per_trade_cap_still_binds_when_it_is_the_smaller_one(self):
        # Headroom $5,000 vs per-trade $1,000: the account limit must not *raise*
        # the per-trade size.
        decision = self.decide(0.0)
        self.assertIn("binding budget (per-trade, $1000.00", decision.reason)
        self.assertLessEqual(decision.max_loss, 1_000.0)

    def test_the_cap_scales_with_equity_not_with_a_fixed_dollar_amount(self):
        strategy = MomentumRiskCapStrategy(spread_width_pct=0.0, max_spread_pct=0.0,
                                           max_portfolio_risk_pct=0.05)
        decision = strategy.decide(symbol="SPY", bars=self.bars(), option_chain=self.chain(),
                                   equity=10_000.0, realized_loss_today=0.0, open_risk=600.0)
        self.assertEqual(decision.action, "hold")
        self.assertIn("$500.00", decision.reason)  # 5% of $10k, not of $100k

    def test_the_circuit_breaker_still_wins_when_both_would_fire(self):
        # Order matters for the journal: a day that is already over should say so,
        # rather than blaming a book that would be irrelevant either way.
        decision = MomentumRiskCapStrategy(max_daily_loss_pct=0.001).decide(
            symbol="SPY", bars=self.bars(), option_chain=self.chain(),
            equity=self.EQUITY, realized_loss_today=5_000.0, open_risk=99_000.0,
        )
        self.assertIn("circuit breaker", decision.reason)


class TestPortfolioCapEndToEnd(unittest.TestCase):
    """Through `run_once` with the mock client, on the book `--dry` actually shows."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "decisions.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def run_pass(self, seed_positions=None, **strategy_kwargs):
        async def go():
            journal = Journal(self.path)
            kwargs = {} if seed_positions is None else {"seed_positions": seed_positions}
            async with MockAlpacaMCPClient(**kwargs) as client:
                strategy = MomentumRiskCapStrategy(**strategy_kwargs)
                # No exit policy: this is about the entry side reading the book, and
                # leaving the exits out keeps the open risk fixed for the assertion.
                record = await run_once(client, strategy, journal, "SPY", 30, None)
                return record, client._orders

        return asyncio.run(go())

    def test_the_open_book_is_measured_and_journalled_on_every_pass(self):
        record, _ = self.run_pass()
        # The full seeded book, now priced off the mock's own chain (see
        # MockAlpacaMCPClient._seed_on_chain): $265 IWM put + $404 QQQ call + $441 SPY
        # put + $580 SPY 09-12 vertical + $428 SPY 10-10 vertical = $2,118.
        self.assertEqual(record["open_risk"], 2118.0)
        self.assertEqual(len(record["open_risk_by_structure"]), 5)
        self.assertEqual(record["unpriceable_risk"], [])
        self.assertEqual(sum(record["open_risk_by_structure"].values()), 2118.0)

    def test_that_book_stands_the_entry_down_under_a_2pct_cap(self):
        record, orders = self.run_pass(max_portfolio_risk_pct=0.02)
        self.assertEqual(record["action"], "hold")
        self.assertEqual(record["contracts"], 0)
        self.assertEqual(record["max_loss"], 0.0)
        self.assertIsNone(record["order"])
        self.assertIn("portfolio risk cap", record["reason"])
        self.assertEqual(orders, [])

    def test_control_the_same_book_trades_under_the_5pct_default(self):
        # $2,290 against a $5,000 cap leaves room. Without this, the test above
        # would also pass if the agent had simply stopped trading.
        record, orders = self.run_pass()
        self.assertNotEqual(record["action"], "hold")
        self.assertIsNotNone(record["order"])
        self.assertEqual(len(orders), 1)

    def test_a_credit_structure_on_the_book_stands_the_entry_down(self):
        # A short call the agent did not open -- assigned, or placed by hand. Its
        # maximum loss is unbounded and is not in `open_risk`, so the headroom the
        # agent computes is fiction and it must not spend it.
        book = [wire_leg("SPY", 14, "call", 120.0, "short", 2, 1.50, 1.40)]
        record, orders = self.run_pass(seed_positions=book)
        self.assertEqual(record["action"], "hold")
        self.assertEqual(record["contracts"], 0)
        self.assertEqual(record["max_loss"], 0.0)
        self.assertEqual(orders, [])
        self.assertEqual(len(record["unpriceable_risk"]), 1)
        self.assertIn("maximum loss this agent cannot compute", record["reason"])
        # Standing down is not the same as not having seen anything.
        self.assertIn("Entry signal was:", record["reason"])

    def test_an_empty_book_reports_zero_and_trades(self):
        record, _ = self.run_pass(seed_positions=[])
        self.assertEqual(record["open_risk"], 0.0)
        self.assertEqual(record["open_risk_by_structure"], {})
        self.assertNotEqual(record["action"], "hold")


if __name__ == "__main__":
    unittest.main()
