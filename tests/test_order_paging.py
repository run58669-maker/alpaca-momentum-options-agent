"""Tests for reading the *whole* working-order book, and for what happens when it
cannot be read whole.

`GET /v2/orders` returns at most 500 rows. Until 2026-08-26 this repo asked for 500
and raised `AlpacaMCPError` if it got 500 back -- the exception left `run_once` and
ended the process, so the single condition under which the agent must not open a
position also stopped it closing any. Two changes here:

- a full page is followed with the spec's `before_order_id` cursor, so 501 working
  orders are 501 working orders and not a crash;
- what genuinely cannot be resolved raises `OrderBookIncomplete`, which `run_once`
  catches, journals as `order_book_gap`, and turns into an entry-side stand-down --
  after the exits have already run.

Every test below is written so that removing the paging loop (or the stand-down)
makes it fail; see the mutation run recorded in PROGRESS.md 2026-08-26 13:00.

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
    MockAlpacaMCPClient,
    OrderBookIncomplete,
)
from strategy import MomentumRiskCapStrategy  # noqa: E402

PAGE = AlpacaMCPClient.ORDER_PAGE_SIZE


def wire_order(order_id: str, status: str = "accepted") -> dict:
    """One single-leg `GET /v2/orders` row. Only id/status matter to the paging."""
    return {
        "id": order_id,
        "client_order_id": f"cid-{order_id}",
        "asset_class": "us_option",
        "symbol": "SPY260910C00101000",
        "side": "buy",
        "status": status,
        "order_class": "simple",
        "type": "limit",
        "qty": "1",
        "filled_qty": "0",
        "limit_price": "1.00",
        "position_intent": "buy_to_open",
        "legs": [],
    }


class PagingClient(AlpacaMCPClient):
    """A real client with the transport replaced: `_call` serves canned pages.

    Subclassed rather than mocked so the code under test is the shipping
    `get_orders`, argument building included -- the cursor is only correct if the
    arguments that carry it are.
    """

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls: list[dict] = []

    async def _call(self, tool: str, args: dict):
        self.calls.append({"tool": tool, "args": dict(args)})
        if not self.pages:
            return []
        return self.pages.pop(0)


def read_orders(client) -> list[dict]:
    return asyncio.run(client.get_orders())


class TestOrderPaging(unittest.TestCase):
    def test_a_short_first_page_is_the_whole_book_and_costs_one_call(self):
        client = PagingClient([[wire_order("a"), wire_order("b")]])
        rows = read_orders(client)
        self.assertEqual([r["id"] for r in rows], ["a", "b"])
        self.assertEqual(len(client.calls), 1)
        self.assertNotIn("before_order_id", client.calls[0]["args"])

    def test_the_first_page_asks_for_the_documented_maximum_newest_first(self):
        client = PagingClient([[]])
        read_orders(client)
        self.assertEqual(
            client.calls[0]["args"],
            {"status": "open", "nested": True, "limit": 500, "direction": "desc"},
        )

    def test_a_full_page_is_followed_by_the_cursor_not_raised_on(self):
        first = [wire_order(f"o{i}") for i in range(PAGE)]
        second = [wire_order("tail-1"), wire_order("tail-2")]
        client = PagingClient([first, second])
        rows = read_orders(client)
        # The whole book, both pages of it -- the old code raised here.
        self.assertEqual(len(rows), PAGE + 2)
        self.assertIn("tail-1", {r["id"] for r in rows})
        self.assertEqual(len(client.calls), 2)
        # Cursor = the last row of the previous page, and the page is newest-first,
        # so "before" that id is the next batch backwards in time.
        self.assertEqual(client.calls[1]["args"]["before_order_id"], f"o{PAGE - 1}")
        self.assertEqual(client.calls[1]["args"]["status"], "open")
        self.assertEqual(client.calls[1]["args"]["nested"], True)

    def test_an_exactly_full_book_takes_one_extra_empty_page_to_prove_it(self):
        client = PagingClient([[wire_order(f"o{i}") for i in range(PAGE)], []])
        rows = read_orders(client)
        self.assertEqual(len(rows), PAGE)
        self.assertEqual(len(client.calls), 2)

    def test_page_fullness_is_measured_before_terminal_rows_are_dropped(self):
        """A full page of `filled` orders normalizes to nothing -- and still has a
        page behind it. Measuring fullness on the normalized rows would stop here
        and miss every working order underneath."""
        filled = [wire_order(f"f{i}", status="filled") for i in range(PAGE)]
        client = PagingClient([filled, [wire_order("still-working")]])
        rows = read_orders(client)
        self.assertEqual([r["id"] for r in rows], ["still-working"])
        self.assertEqual(len(client.calls), 2)

    def test_a_row_repeated_across_pages_is_counted_once(self):
        """The cursor is exclusive; a server that echoes it anyway must not make the
        same order's risk appear twice in the total."""
        first = [wire_order(f"o{i}") for i in range(PAGE)]
        second = [wire_order(f"o{PAGE - 1}"), wire_order("new")]
        client = PagingClient([first, second])
        rows = read_orders(client)
        ids = [r["id"] for r in rows]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(rows), PAGE + 1)

    def test_a_book_past_the_page_cap_is_reported_not_truncated(self):
        pages = [[wire_order(f"p{p}-{i}") for i in range(PAGE)]
                 for p in range(AlpacaMCPClient.MAX_ORDER_PAGES + 1)]
        client = PagingClient(pages)
        with self.assertRaises(OrderBookIncomplete) as caught:
            read_orders(client)
        self.assertIn("under-states portfolio risk", str(caught.exception))
        self.assertEqual(len(client.calls), AlpacaMCPClient.MAX_ORDER_PAGES)

    def test_a_full_page_with_no_cursor_is_reported_not_truncated(self):
        page = [wire_order(f"o{i}") for i in range(PAGE - 1)] + [wire_order("")]
        client = PagingClient([page])
        with self.assertRaises(OrderBookIncomplete) as caught:
            read_orders(client)
        self.assertIn("no cursor to read the rest of the book from", str(caught.exception))

    def test_a_payload_that_is_not_a_list_is_reported_not_read_as_empty(self):
        client = PagingClient([{"error": "rate limited"}])
        with self.assertRaises(OrderBookIncomplete):
            read_orders(client)


class UnreadableBookClient(MockAlpacaMCPClient):
    """The mock, except the order book cannot be read whole."""

    async def get_orders(self):
        raise OrderBookIncomplete(
            "get_orders returned more than 5000 working orders; the book is larger than "
            "this agent will page through, and a partial book under-states portfolio risk"
        )


class TestUnreadableBookStandsTheEntryDown(unittest.TestCase):
    """Through `run_once`: the pass survives, and it does not open anything."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "decisions.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def run_pass(self, client_cls):
        async def go():
            journal = Journal(self.path)
            async with client_cls() as client:
                strategy = MomentumRiskCapStrategy()
                return await run_once(client, strategy, journal, "SPY", 30, None)

        return asyncio.run(go())

    def test_the_control_pass_does_trade(self):
        """Without the gap this same signal opens a structure -- so the stand-down
        below is caused by the unreadable book and nothing else."""
        record = self.run_pass(MockAlpacaMCPClient)
        self.assertNotEqual(record["action"], "hold")
        self.assertGreater(record["contracts"], 0)

    def test_an_unreadable_book_holds_instead_of_ending_the_process(self):
        record = self.run_pass(UnreadableBookClient)
        self.assertEqual(record["action"], "hold")
        self.assertEqual(record["contracts"], 0)
        self.assertEqual(record["max_loss"], 0.0)
        self.assertIsNone(record["order"])

    def test_the_gap_is_named_in_the_record_not_only_in_the_prose(self):
        record = self.run_pass(UnreadableBookClient)
        self.assertIn("working order book:", record["order_book_gap"])
        self.assertTrue(
            any("working order book:" in item for item in record["unpriceable_risk"])
        )

    def test_the_reason_says_the_measured_risk_is_positions_only(self):
        record = self.run_pass(UnreadableBookClient)
        self.assertIn("could not be read whole", record["reason"])
        self.assertIn("counts positions only", record["reason"])
        # `working_risk` is 0.0 on this pass, and that zero is not a measurement --
        # the record has to carry which of the two it is.
        self.assertEqual(record["working_risk"], 0.0)
        self.assertIsNotNone(record["order_book_gap"])

    def test_a_readable_book_leaves_the_gap_field_empty(self):
        record = self.run_pass(MockAlpacaMCPClient)
        self.assertIsNone(record["order_book_gap"])


if __name__ == "__main__":
    unittest.main()
