"""Tests for the naked-long fallback going out as a *limit* order.

The hole these exist for: when `select_spread` finds no second leg, the agent buys the
bare long option, and until 2026-08-26 it sent that as a market order. Two things follow
from an unpriced buy, and they point the same way:

1. On a long option the premium *is* the maximum loss, and `decide()` computed
   `max_loss = net_debit * 100 * contracts` from `last_price` before the order left. A
   market order fills at whatever the book gives it, so a fill above `last_price` raises
   the real maximum loss above the number the risk budget approved and above the number
   journalled next to it. Nothing downstream re-derives it from the fill.
2. `portfolio.working_order_risk` refuses to price an order with no limit price -- that
   is unmeasured risk, not zero risk -- so while the agent's own market order was
   working, the whole entry side stood down. The agent could not price the order it had
   itself just sent.

The accepted cost is on the record too: a limit at the sized price does not chase a
market that has moved up, so the fallback can end a pass unfilled. That is the trade --
no position, rather than a position bought above budget.

Every test below is written so that restoring `"type": "market"` (and dropping
`limit_price`) makes it fail -- see the mutation run recorded in PROGRESS.md
2026-08-26 15:25.

Stdlib only:
    py -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from agent import run_once  # noqa: E402
from journal import Journal  # noqa: E402
from mcp_client import (  # noqa: E402
    AlpacaMCPClient,
    AlpacaMCPError,
    MockAlpacaMCPClient,
    normalize_orders,
)
from portfolio import working_order_risk  # noqa: E402
from strategy import MomentumRiskCapStrategy  # noqa: E402


class FakeSession:
    def __init__(self):
        self.calls = []

    async def call_tool(self, tool, args):
        self.calls.append((tool, args))
        return {}


def real_client() -> AlpacaMCPClient:
    """A real AlpacaMCPClient without the env-var check, wired to a fake session."""
    client = object.__new__(AlpacaMCPClient)
    client._session = FakeSession()
    return client


def sent_args(limit_price: float, qty: int = 3) -> dict:
    client = real_client()
    asyncio.run(client.place_option_order("SPY260908C00100000", "buy", qty, limit_price))
    _, args = client._session.calls[0]
    return args


class TestTheWire(unittest.TestCase):
    """What `place_option_order` actually puts on the wire."""

    def test_the_order_is_a_limit_order(self):
        self.assertEqual(sent_args(2.35)["type"], "limit")

    def test_the_limit_is_the_price_it_was_sized_against(self):
        # Not the ask, not a padded price: the same per-share number `max_loss` was
        # computed from. Anything higher fills above the approved loss.
        self.assertEqual(sent_args(2.35)["limit_price"], "2.35")

    def test_the_limit_price_is_a_string_of_two_decimals(self):
        # Upstream types limit_price as str, same as on the mleg path.
        args = sent_args(2.5)
        self.assertIsInstance(args["limit_price"], str)
        self.assertEqual(args["limit_price"], "2.50")

    def test_the_rest_of_the_single_leg_shape_is_unchanged(self):
        args = sent_args(2.35, qty=3)
        self.assertEqual(args["symbol"], "SPY260908C00100000")
        self.assertEqual(args["side"], "buy")
        self.assertEqual(args["qty"], "3")
        self.assertEqual(args["position_intent"], "buy_to_open")
        self.assertEqual(args["time_in_force"], "day")
        self.assertNotIn("legs", args)
        self.assertNotIn("order_class", args)

    def test_a_non_positive_limit_is_refused_rather_than_sent(self):
        # A buy_to_open at zero or less is not a price. Sending it as-is would be an
        # order at an unmeanable limit; sending it *without* the field would silently
        # become the market order this change exists to remove.
        for bad in (0.0, -1.25):
            with self.subTest(bad=bad):
                client = real_client()
                with self.assertRaises(AlpacaMCPError):
                    asyncio.run(client.place_option_order("SPY260908C00100000", "buy", 1, bad))
                self.assertEqual(client._session.calls, [])

    def test_the_mock_client_refuses_the_same_prices(self):
        # The --dry client is what the demo runs on; a mock that accepts an order the
        # real client rejects makes the demo evidence of the wrong thing.
        async def go():
            async with MockAlpacaMCPClient() as client:
                await client.place_option_order("SPY260908C00100000", "buy", 1, 0.0)

        with self.assertRaises(AlpacaMCPError):
            asyncio.run(go())


class TestThroughRunOnce(unittest.TestCase):
    """End to end on the book `--dry` shows, with spreads switched off.

    `spread_width_pct=0` is the documented way to trade the long leg naked, so it is
    the fallback path without having to fake an empty chain.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "decisions.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def naked_pass(self) -> dict:
        async def go():
            journal = Journal(self.path)
            async with MockAlpacaMCPClient() as client:
                strategy = MomentumRiskCapStrategy(spread_width_pct=0.0)
                return await run_once(client, strategy, journal, "SPY", 30, None)

        return asyncio.run(go())

    def test_the_pass_really_takes_the_naked_path(self):
        record = self.naked_pass()
        self.assertTrue(record["action"].startswith("buy_"))
        self.assertNotIn("spread", record["action"])
        self.assertGreater(record["contracts"], 0)

    def test_the_order_carries_a_limit_price(self):
        record = self.naked_pass()
        self.assertIn("limit_price", record["order"])

    def test_the_limit_price_matches_the_max_loss_that_was_journalled(self):
        # The one identity that makes the number in the journal true: the price on the
        # order x 100 x contracts is exactly the max loss the record claims.
        record = self.naked_pass()
        limit = float(record["order"]["limit_price"])
        self.assertAlmostEqual(limit * 100 * record["contracts"], record["max_loss"], places=2)


class TestTheOrderThisAgentSendsIsPriceable(unittest.TestCase):
    """The other end of the same asymmetry, in `portfolio.working_order_risk`.

    A working order the agent cannot price stands the entry side down. Before this
    change, the order it stood down for was one it had sent itself.
    """

    def wire_row(self, **overrides) -> dict:
        row = {
            "id": "o1",
            "client_order_id": "mrcap-open-2026-08-26-naked",
            "asset_class": "us_option",
            "symbol": "SPY260908C00100000",
            "side": "buy",
            "status": "new",
            "order_class": "simple",
            "order_type": "limit",
            "type": "limit",
            "qty": "3",
            "filled_qty": "0",
            "limit_price": "2.35",
            "position_intent": "buy_to_open",
            "legs": None,
        }
        row.update(overrides)
        return row

    def test_the_limit_version_prices_to_premium_x_100_x_unfilled(self):
        order = normalize_orders([self.wire_row()])[0]
        self.assertEqual(working_order_risk(order, []), 705.00)

    def test_the_market_version_is_unmeasured_and_stands_the_agent_down(self):
        # Same order, no limit price: None, not 0.0. This is what the old code sent.
        order = normalize_orders([
            self.wire_row(type="market", order_type="market", limit_price=None)
        ])[0]
        self.assertIsNone(working_order_risk(order, []))

    def test_only_the_unfilled_part_is_charged(self):
        # The filled part is a position and is counted there; charging it twice would
        # make the agent stand down on risk it does not have.
        order = normalize_orders([self.wire_row(filled_qty="1")])[0]
        self.assertEqual(working_order_risk(order, []), 470.00)


if __name__ == "__main__":
    unittest.main()
