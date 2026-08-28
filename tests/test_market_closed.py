"""The entry side stands down when the clock says the market is closed.

Stdlib only (unittest + asyncio), same as the rest of the suite:
    py -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from agent import run_once  # noqa: E402
from exits import ExitPolicy  # noqa: E402
from journal import Journal  # noqa: E402
from mcp_client import MockAlpacaMCPClient  # noqa: E402
from strategy import MomentumRiskCapStrategy  # noqa: E402


class ClosedClockClient(MockAlpacaMCPClient):
    """The mock book, but the clock says closed -- the one state the mock never reaches."""

    async def get_clock(self) -> dict:
        return {"is_open": False, "timestamp": datetime.now(timezone.utc).isoformat()}


class NoIsOpenClient(MockAlpacaMCPClient):
    """A clock that came back without the field at all."""

    async def get_clock(self) -> dict:
        return {"timestamp": datetime.now(timezone.utc).isoformat()}


class TestMarketClosedStandsDownTheEntrySide(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "decisions.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def run_once_with(self, client_cls, exit_policy=None):
        async def go():
            journal = Journal(self.path)
            async with client_cls() as client:
                record = await run_once(client, MomentumRiskCapStrategy(), journal, "SPY",
                                        30, exit_policy)
                return record, client._orders

        return asyncio.run(go())

    def records(self):
        return [json.loads(l) for l in self.path.read_text(encoding="utf-8").strip().splitlines()]

    def test_open_market_still_trades(self):
        # The control: the same book, the same strategy, clock open -- this pass buys.
        record, orders = self.run_once_with(MockAlpacaMCPClient)
        self.assertEqual(record["action"], "buy_call_spread")
        self.assertEqual(len(orders), 1)

    def test_closed_market_sends_no_opening_order(self):
        record, orders = self.run_once_with(ClosedClockClient)
        self.assertEqual(record["action"], "hold")
        self.assertIsNone(record["order"])
        self.assertEqual(orders, [], "nothing may be sent while the clock says closed")
        self.assertIsNone(record["client_order_id"], "no key is spent on a trade that never went out")

    def test_closed_market_books_no_risk_and_says_why(self):
        record, _ = self.run_once_with(ClosedClockClient)
        self.assertEqual(record["contracts"], 0)
        self.assertEqual(record["max_loss"], 0.0)
        self.assertIn("market is closed", record["reason"])
        # The signal it would have taken is still in the record, not thrown away.
        self.assertIn("Entry signal was:", record["reason"])

    def test_the_journal_says_the_market_was_shut(self):
        self.run_once_with(ClosedClockClient)
        decisions = [r for r in self.records() if "action" in r]
        self.assertEqual(len(decisions), 1)
        self.assertIs(decisions[0]["market_open"], False)
        self.assertEqual(decisions[0]["action"], "hold")

    def test_exits_still_run_when_the_market_is_closed(self):
        # Same rule as the circuit breaker: standing down on new risk must never
        # freeze the side that takes risk off. Whether a close *fills* out of hours
        # is the broker's call; not attempting it is this agent's, and it does not
        # make that call here.
        record, _ = self.run_once_with(ClosedClockClient, ExitPolicy())
        self.assertEqual(len([r for r in record["exits"] if r["action"] == "exit_close"]), 3)
        self.assertEqual(record["action"], "hold")

    def test_a_clock_without_is_open_is_not_treated_as_closed(self):
        # An absent field is a gap in the answer, not a shut exchange. Reading it as
        # closed would halt an unattended run on one malformed response.
        record, orders = self.run_once_with(NoIsOpenClient)
        self.assertEqual(record["action"], "buy_call_spread")
        self.assertEqual(len(orders), 1)
        self.assertNotIn("market is closed", record["reason"])


if __name__ == "__main__":
    unittest.main()
