"""Tests for what an exit is sent into: the mark it was decided on vs the quote it crosses.

The exit rules fire on `unrealized_pl`, which the position book derives from
`current_price` -- a mark. The order that follows is a market order, so what it will
receive is the far side of the quote. These pin the arithmetic of that gap, that it is
reported and never enforced, and that a chain call which fails cannot stand an exit down.

Stdlib only:
    py -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from agent import closing_quotes, manage_exits  # noqa: E402
from exits import ExitPolicy, closing_crossing_cost, crossing_note  # noqa: E402
from journal import Journal  # noqa: E402

# UTC, because that is the clock `ExitPolicy.evaluate` reads when no date is passed.
TODAY = datetime.now(timezone.utc).date()


def leg(symbol="SPY260908C00100000", side="long", qty=5.0, available=None,
        cost_basis=1000.0, unrealized=0.0, expiry="2026-09-08", kind="call",
        strike=100.0, underlying="SPY", current=2.00) -> dict:
    """An already-normalized position, in the shape the exit policy consumes."""
    return {
        "symbol": symbol, "underlying": underlying, "type": kind, "strike": strike,
        "expiry": expiry, "side": side, "qty": qty,
        "qty_available": qty if available is None else available,
        "avg_entry_price": 2.0, "current_price": current,
        "cost_basis": cost_basis, "unrealized_pl": unrealized,
    }


def quote(bid: float, ask: float) -> dict[str, float]:
    return {"bid": bid, "ask": ask}


LONG = "SPY260908C00100000"
SHORT = "SPY260908C00103000"


def vertical(long_current=1.46, short_current=0.95) -> list[dict]:
    return [
        leg(symbol=LONG, strike=100.0, current=long_current),
        leg(symbol=SHORT, strike=103.0, side="short", current=short_current,
            cost_basis=-475.0),
    ]


class TestCrossingArithmetic(unittest.TestCase):
    def test_longs_leave_at_the_bid_and_shorts_are_bought_back_at_the_ask(self):
        # Marks: 1.46 - 0.95 = 0.51/share. Quotes: bid 1.44 on the long, ask 0.96 on
        # the short = 0.48/share. Five contracts, 100 shares each.
        crossing = closing_crossing_cost(
            vertical(), 5, {LONG: quote(1.44, 1.48), SHORT: quote(0.94, 0.96)}
        )
        self.assertEqual(crossing["mark_proceeds"], 255.00)
        self.assertEqual(crossing["quoted_proceeds"], 240.00)
        self.assertEqual(crossing["crossing_cost"], 15.00)

    def test_a_single_long_leg_is_sold_into_its_bid(self):
        crossing = closing_crossing_cost(
            [leg(symbol=LONG, current=3.00)], 5, {LONG: quote(2.90, 3.10)}
        )
        self.assertEqual(crossing["mark_proceeds"], 1500.00)
        self.assertEqual(crossing["quoted_proceeds"], 1450.00)
        self.assertEqual(crossing["crossing_cost"], 50.00)

    def test_a_short_being_bought_back_subtracts_from_proceeds(self):
        # Nothing long here: flattening pays out, so both numbers are negative and the
        # ask being above the mark still makes crossing cost positive.
        crossing = closing_crossing_cost(
            [leg(symbol=SHORT, side="short", current=1.00)], 3, {SHORT: quote(0.98, 1.06)}
        )
        self.assertEqual(crossing["mark_proceeds"], -300.00)
        self.assertEqual(crossing["quoted_proceeds"], -318.00)
        self.assertEqual(crossing["crossing_cost"], 18.00)

    def test_the_quantity_priced_is_the_one_the_close_carries_not_the_position(self):
        # The naked-short buyback closes 3 of a 5-contract leg; pricing 5 would report
        # a cost for contracts this order is not touching.
        legs = [leg(symbol=SHORT, side="short", qty=5.0, current=1.00)]
        self.assertEqual(
            closing_crossing_cost(legs, 3, {SHORT: quote(0.98, 1.06)})["crossing_cost"],
            18.00,
        )
        self.assertEqual(
            closing_crossing_cost(legs, 5, {SHORT: quote(0.98, 1.06)})["crossing_cost"],
            30.00,
        )

    def test_a_mark_struck_under_the_bid_makes_crossing_negative_and_it_is_kept(self):
        # A stale last trade below a firm book. The book pays better than the mark;
        # clamping that to zero would hide a real thing from the journal.
        crossing = closing_crossing_cost(
            [leg(symbol=LONG, current=1.40)], 1, {LONG: quote(1.44, 1.48)}
        )
        self.assertEqual(crossing["crossing_cost"], -4.00)

    def test_the_widest_leg_is_reported_as_a_fraction_of_its_own_mid(self):
        # Long: 0.04/1.46 = 2.74%. Short: 0.02/0.95 = 2.11%. The wider one is reported.
        crossing = closing_crossing_cost(
            vertical(), 5, {LONG: quote(1.44, 1.48), SHORT: quote(0.94, 0.96)}
        )
        self.assertAlmostEqual(crossing["widest_leg_spread_pct"], 0.0274, places=4)


class TestUnquotedLegs(unittest.TestCase):
    def test_a_leg_with_no_quote_withholds_the_arithmetic_and_names_itself(self):
        crossing = closing_crossing_cost([leg(symbol=LONG, current=3.00)], 5, {})
        self.assertEqual(crossing["unquoted"], [LONG])
        self.assertIsNone(crossing["quoted_proceeds"])
        self.assertIsNone(crossing["crossing_cost"])

    def test_the_mark_is_still_reported_when_the_quote_is_missing(self):
        # The mark comes off the position book, which is present either way; only the
        # comparison is unavailable.
        crossing = closing_crossing_cost([leg(symbol=LONG, current=3.00)], 5, {})
        self.assertEqual(crossing["mark_proceeds"], 1500.00)

    def test_one_unquoted_leg_withholds_the_whole_structure_not_just_its_own_half(self):
        # Half a vertical priced at the quote and half at the mark is not a number
        # anything can be concluded from.
        crossing = closing_crossing_cost(vertical(), 5, {LONG: quote(1.44, 1.48)})
        self.assertEqual(crossing["unquoted"], [SHORT])
        self.assertIsNone(crossing["quoted_proceeds"])
        self.assertIsNone(crossing["crossing_cost"])
        self.assertIsNone(crossing["widest_leg_spread_pct"])

    def test_the_note_says_which_legs_could_not_be_priced(self):
        note = crossing_note(closing_crossing_cost(vertical(), 5, {LONG: quote(1.44, 1.48)}))
        self.assertIn(SHORT, note)
        self.assertIn("unmeasured", note)
        self.assertIn("still goes out at market", note)

    def test_the_note_names_both_numbers_when_the_legs_are_quoted(self):
        note = crossing_note(closing_crossing_cost(
            vertical(), 5, {LONG: quote(1.44, 1.48), SHORT: quote(0.94, 0.96)}
        ))
        self.assertIn("$255.00", note)
        self.assertIn("$240.00", note)
        self.assertIn("$15.00", note)


class QuotingClient:
    """Just enough client for `manage_exits`: a fixed book, a chain, closes recorded."""

    def __init__(self, positions, chain=None, chain_error=None):
        self.positions = positions
        self.chain = chain if chain is not None else [
            {"symbol": LONG, "last_price": 1.46, "bid": 1.44, "ask": 1.48},
            {"symbol": SHORT, "last_price": 0.95, "bid": 0.94, "ask": 0.96},
        ]
        self.chain_error = chain_error
        self.chain_calls: list[str] = []
        self.closes: list[dict] = []

    async def get_option_chain(self, underlying):
        self.chain_calls.append(underlying)
        if self.chain_error is not None:
            raise self.chain_error
        return self.chain

    async def get_all_positions(self):
        return self.positions

    async def place_option_close_order(self, legs, qty, client_order_id=None):
        self.closes.append({"symbols": [l["symbol"] for l in legs], "qty": qty})
        return {"id": f"mock-{len(self.closes)}", "status": "filled", "qty": qty,
                "client_order_id": client_order_id}


class TestManageExitsRecordsWhatItCrosses(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "decisions.jsonl"
        # One day to expiry: the time stop fires, so a close actually goes out.
        self.expiring = (TODAY + timedelta(days=1)).isoformat()

    def tearDown(self):
        self.tmp.cleanup()

    def book_that_closes(self):
        return [
            leg(symbol=LONG, strike=100.0, expiry=self.expiring, current=1.46),
            leg(symbol=SHORT, strike=103.0, side="short", expiry=self.expiring,
                current=0.95, cost_basis=-475.0),
        ]

    def run_exits(self, client):
        return asyncio.run(manage_exits(client, Journal(self.path), ExitPolicy()))

    def records(self):
        with self.path.open(encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def test_the_close_carries_the_crossing_measurement_into_the_journal(self):
        client = QuotingClient(self.book_that_closes())
        records = self.run_exits(client)
        self.assertEqual(len(client.closes), 1)
        self.assertEqual(records[0]["action"], "exit_close")
        self.assertEqual(records[0]["crossing"]["crossing_cost"], 15.00)
        self.assertEqual(records[0]["crossing"]["mark_proceeds"], 255.00)
        self.assertIn("Exit pricing", records[0]["reason"])
        # And it is on disk, not just in the returned dict.
        self.assertEqual(self.records()[0]["crossing"]["quoted_proceeds"], 240.00)

    def test_the_time_stop_reason_is_still_there_alongside_it(self):
        # The measurement is appended to the decision's reason, it does not replace it.
        records = self.run_exits(QuotingClient(self.book_that_closes()))
        self.assertIn("time stop", records[0]["reason"])

    def test_a_wide_book_does_not_stop_the_exit(self):
        # $0.10/$3.00 on the long leg: nothing here is allowed to turn that into a hold.
        client = QuotingClient(self.book_that_closes(), chain=[
            {"symbol": LONG, "last_price": 1.46, "bid": 0.10, "ask": 3.00},
            {"symbol": SHORT, "last_price": 0.95, "bid": 0.94, "ask": 0.96},
        ])
        records = self.run_exits(client)
        self.assertEqual(records[0]["action"], "exit_close")
        self.assertEqual(len(client.closes), 1)
        self.assertEqual(records[0]["crossing"]["crossing_cost"], 685.00)

    def test_a_hold_costs_no_chain_call_and_carries_no_measurement(self):
        far = (TODAY + timedelta(days=30)).isoformat()
        client = QuotingClient([
            leg(symbol=LONG, strike=100.0, expiry=far, current=2.00),
            leg(symbol=SHORT, strike=103.0, side="short", expiry=far, current=1.00,
                cost_basis=-475.0),
        ])
        records = self.run_exits(client)
        self.assertEqual(records[0]["action"], "exit_hold")
        self.assertEqual(client.chain_calls, [])
        self.assertIsNone(records[0]["crossing"])

    def test_one_chain_call_per_underlying_not_per_structure(self):
        # Two expiries on SPY, both inside the time stop -> two structures, one chain.
        client = QuotingClient([
            leg(symbol=LONG, strike=100.0, expiry=self.expiring, current=1.46),
            leg(symbol="SPY260909C00100000", strike=100.0, current=1.46,
                expiry=TODAY.isoformat()),
        ])
        records = self.run_exits(client)
        self.assertEqual([r["action"] for r in records], ["exit_close", "exit_close"])
        self.assertEqual(client.chain_calls, ["SPY"])

    def test_a_chain_that_cannot_be_read_is_journalled_and_the_close_still_goes_out(self):
        client = QuotingClient(self.book_that_closes(),
                               chain_error=RuntimeError("chain endpoint down"))
        records = self.run_exits(client)
        self.assertEqual(len(client.closes), 1)
        self.assertEqual(records[0]["action"], "exit_close")
        self.assertEqual(sorted(records[0]["crossing"]["unquoted"]), sorted([LONG, SHORT]))
        self.assertIsNone(records[0]["crossing"]["crossing_cost"])
        on_disk = self.records()
        self.assertEqual(on_disk[0]["action"], "exit_quotes_unavailable")
        self.assertIn("chain endpoint down", on_disk[0]["reason"])

    def test_a_chain_row_with_only_one_side_is_not_used_as_a_quote(self):
        client = QuotingClient(self.book_that_closes(), chain=[
            {"symbol": LONG, "last_price": 1.46, "bid": 1.44},
            {"symbol": SHORT, "last_price": 0.95, "bid": 0.94, "ask": 0.96},
        ])
        records = self.run_exits(client)
        self.assertEqual(records[0]["crossing"]["unquoted"], [LONG])
        self.assertIsNone(records[0]["crossing"]["crossing_cost"])


class TestClosingQuotes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "decisions.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_underlyings_means_no_calls(self):
        client = QuotingClient([])
        quotes = asyncio.run(closing_quotes(client, Journal(self.path), set()))
        self.assertEqual(quotes, {})
        self.assertEqual(client.chain_calls, [])

    def test_only_two_sided_rows_survive(self):
        client = QuotingClient([], chain=[
            {"symbol": LONG, "bid": 1.44, "ask": 1.48},
            {"symbol": SHORT, "ask": 0.96},
            {"symbol": "SPY260908C00105000", "last_price": 0.20},
        ])
        quotes = asyncio.run(closing_quotes(client, Journal(self.path), {"SPY"}))
        self.assertEqual(list(quotes), [LONG])
        self.assertEqual(quotes[LONG], {"bid": 1.44, "ask": 1.48})


if __name__ == "__main__":
    unittest.main()
