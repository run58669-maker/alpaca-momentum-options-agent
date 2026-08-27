"""Tests for the entry-side idempotency key: `make_open_client_order_id`, the key on
the wire in both clients, and the refusal path through `run_once`.

The hole these exist for: an opening order that is `accepted` but unfilled is not a
position, so `open_risk` cannot see it and the portfolio cap cannot charge for it. The
next pass reads the same momentum, makes the same decision, and sends a second identical
order on top of the first. Only Alpaca knows the first one exists, so only Alpaca can
refuse the second -- and it will only do that if both carry the same `client_order_id`.

Every test below is written so that removing the key (or letting `qty` into it) makes it
fail -- see the mutation runs recorded in PROGRESS.md 2026-08-26 00:00.

Stdlib only:
    py -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from agent import run_once  # noqa: E402
from journal import Journal  # noqa: E402
from mcp_client import (  # noqa: E402
    CLIENT_ORDER_ID_MAX_LEN,
    AlpacaMCPClient,
    MockAlpacaMCPClient,
    make_close_client_order_id,
    make_open_client_order_id,
)
from strategy import MomentumRiskCapStrategy  # noqa: E402

LONG = {"symbol": "SPY260908C00100000", "side": "buy"}
SHORT = {"symbol": "SPY260908C00103000", "side": "sell"}
DAY = "2026-08-26"


class FakeSession:
    """Stands in for an MCP ClientSession, recording the args it was called with."""

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


class TestOpenKeyIdentity(unittest.TestCase):
    def test_the_same_structure_on_the_same_day_gets_the_same_key(self):
        self.assertEqual(
            make_open_client_order_id([LONG, SHORT], day=DAY),
            make_open_client_order_id([LONG, SHORT], day=DAY),
        )

    def test_tomorrow_gets_a_different_key(self):
        # Entries go out time_in_force="day"; an unfilled one is dead by the bell and
        # the next session has to be able to try again.
        self.assertNotEqual(
            make_open_client_order_id([LONG, SHORT], day=DAY),
            make_open_client_order_id([LONG, SHORT], day="2026-08-27"),
        )

    def test_different_strikes_get_different_keys(self):
        other = {"symbol": "SPY260908C00104000", "side": "sell"}
        self.assertNotEqual(
            make_open_client_order_id([LONG, SHORT], day=DAY),
            make_open_client_order_id([LONG, other], day=DAY),
        )

    def test_the_sides_are_part_of_the_identity(self):
        # The same two strikes bought and sold the other way round is the opposite
        # structure, not a duplicate of this one.
        flipped = [{"symbol": LONG["symbol"], "side": "sell"},
                   {"symbol": SHORT["symbol"], "side": "buy"}]
        self.assertNotEqual(
            make_open_client_order_id([LONG, SHORT], day=DAY),
            make_open_client_order_id(flipped, day=DAY),
        )

    def test_a_naked_long_is_not_the_same_order_as_the_spread_it_starts(self):
        self.assertNotEqual(
            make_open_client_order_id([LONG], day=DAY),
            make_open_client_order_id([LONG, SHORT], day=DAY),
        )

    def test_an_open_never_collides_with_the_close_of_the_same_legs(self):
        # Different prefixes, so closing today what was opened today is not mistaken
        # for a duplicate of the opening order.
        position_legs = [{"symbol": LONG["symbol"], "side": "long"}]
        self.assertNotEqual(
            make_open_client_order_id([LONG], day=DAY),
            make_close_client_order_id(position_legs, 5, day=DAY),
        )

    def test_the_key_is_recognisable_and_inside_the_api_length_cap(self):
        key = make_open_client_order_id([LONG, SHORT], day=DAY)
        self.assertTrue(key.startswith("mrcap-open-"))
        self.assertLessEqual(len(key), CLIENT_ORDER_ID_MAX_LEN)

    def test_the_day_defaults_to_the_utc_date(self):
        today = datetime.now(timezone.utc).date().isoformat()
        self.assertEqual(
            make_open_client_order_id([LONG, SHORT]),
            make_open_client_order_id([LONG, SHORT], day=today),
        )


class TestOpenKeyOnTheWire(unittest.TestCase):
    def test_a_spread_carries_the_key_on_the_parent(self):
        client = real_client()
        asyncio.run(client.place_option_spread_order(
            LONG["symbol"], SHORT["symbol"], 5, 1.20, "key-abc"))
        _, args = client._session.calls[0]
        self.assertEqual(args["client_order_id"], "key-abc")
        self.assertEqual(args["order_class"], "mleg")
        for sent_leg in args["legs"]:
            self.assertNotIn("client_order_id", sent_leg)  # parent-level only

    def test_a_single_leg_entry_carries_the_key(self):
        client = real_client()
        asyncio.run(client.place_option_order(LONG["symbol"], "buy", 3, 2.35, "key-xyz"))
        _, args = client._session.calls[0]
        self.assertEqual(args["client_order_id"], "key-xyz")

    def test_an_entry_without_an_explicit_key_still_gets_one(self):
        # No caller can send an unkeyed entry, however it reaches the client.
        client = real_client()
        asyncio.run(client.place_option_spread_order(
            LONG["symbol"], SHORT["symbol"], 5, 1.20))
        _, args = client._session.calls[0]
        self.assertEqual(args["client_order_id"], make_open_client_order_id([LONG, SHORT]))


class TestDuplicateEntryIsRefused(unittest.TestCase):
    def test_the_identical_spread_is_rejected_not_duplicated(self):
        async def go():
            async with MockAlpacaMCPClient() as client:
                first = await client.place_option_spread_order(
                    LONG["symbol"], SHORT["symbol"], 5, 1.20)
                second = await client.place_option_spread_order(
                    LONG["symbol"], SHORT["symbol"], 5, 1.20)
                return first, second, client._orders

        first, second, orders = asyncio.run(go())
        self.assertEqual(first["status"], "filled")
        self.assertEqual(second["error"]["http_status"], 422)
        self.assertIn("client_order_id", second["error"]["detail"]["message"])
        self.assertEqual(len(orders), 1)  # the duplicate never became an order

    def test_a_resize_does_not_mint_a_fresh_key(self):
        # The whole reason qty is out of the key: pass two sizes smaller because the
        # portfolio headroom shrank, and that is the same attempt, not a new one.
        async def go():
            async with MockAlpacaMCPClient() as client:
                await client.place_option_spread_order(LONG["symbol"], SHORT["symbol"], 5, 1.20)
                return await client.place_option_spread_order(
                    LONG["symbol"], SHORT["symbol"], 1, 1.20)

        self.assertEqual(asyncio.run(go())["error"]["http_status"], 422)

    def test_a_different_structure_is_allowed_through(self):
        async def go():
            async with MockAlpacaMCPClient() as client:
                await client.place_option_spread_order(LONG["symbol"], SHORT["symbol"], 5, 1.20)
                return await client.place_option_spread_order(
                    LONG["symbol"], "SPY260908C00104000", 5, 1.20)

        self.assertNotIn("error", asyncio.run(go()))

    def test_the_naked_entry_path_is_deduped_too(self):
        async def go():
            async with MockAlpacaMCPClient() as client:
                await client.place_option_order(LONG["symbol"], "buy", 3, 2.35)
                return await client.place_option_order(LONG["symbol"], "buy", 3, 2.35)

        self.assertEqual(asyncio.run(go())["error"]["http_status"], 422)


class TestRefusedEntryEndToEnd(unittest.TestCase):
    """Through `run_once` on the book `--dry` actually shows."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "decisions.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def two_passes(self):
        async def go():
            journal = Journal(self.path)
            async with MockAlpacaMCPClient() as client:
                strategy = MomentumRiskCapStrategy()
                # No exit policy: this is about the entry side re-sending itself, and
                # leaving the exits out keeps both passes reading the same book.
                first = await run_once(client, strategy, journal, "SPY", 30, None)
                second = await run_once(client, strategy, journal, "SPY", 30, None)
                return first, second, client._orders

        return asyncio.run(go())

    def test_the_first_pass_opens_and_journals_the_key_it_sent(self):
        first, _, _ = self.two_passes()
        self.assertNotEqual(first["action"], "hold")
        self.assertFalse(first["order_rejected"])
        self.assertTrue(first["client_order_id"].startswith("mrcap-open-"))
        self.assertEqual(first["order"]["client_order_id"], first["client_order_id"])

    def test_the_second_identical_pass_is_refused(self):
        first, second, orders = self.two_passes()
        self.assertTrue(second["order_rejected"])
        self.assertEqual(second["client_order_id"], first["client_order_id"])
        self.assertEqual(len(orders), 1)  # one order for two passes

    def test_a_refused_entry_books_no_risk(self):
        # The journal's risk fields describe risk actually taken. An order the API
        # rejected bought nothing, so booking its max loss would over-state the book.
        _, second, _ = self.two_passes()
        self.assertEqual(second["contracts"], 0)
        self.assertEqual(second["max_loss"], 0.0)

    def test_the_reason_names_the_key_and_says_nothing_was_bought(self):
        _, second, _ = self.two_passes()
        self.assertIn(second["client_order_id"], second["reason"])
        self.assertIn("not opened", second["reason"])

    def test_a_pass_that_sends_nothing_has_no_key(self):
        async def go():
            journal = Journal(self.path)
            async with MockAlpacaMCPClient() as client:
                # A breaker low enough that today's canned $450 loss trips it, so the
                # entry side never reaches the client.
                strategy = MomentumRiskCapStrategy(max_daily_loss_pct=0.001)
                return await run_once(client, strategy, journal, "SPY", 30, None)

        record = asyncio.run(go())
        self.assertEqual(record["action"], "hold")
        self.assertIsNone(record["client_order_id"])
        self.assertFalse(record["order_rejected"])


if __name__ == "__main__":
    unittest.main()
