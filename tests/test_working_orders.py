"""Tests for the last hole in the account-level risk cap: orders that are working
but not yet filled.

`open_risk` prices *positions*. An entry sitting at `accepted` is not a position, so
until this pass the budget could not see it -- the account cap was computed off a
book that was missing everything currently in flight. The idempotency key added
2026-08-26 stops the same structure being submitted twice; it says nothing about the
risk of the one copy that really is live.

Every test below is written so that removing the working-order term makes it fail --
see the mutation runs recorded in PROGRESS.md 2026-08-26 02:00.

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
from journal import Journal  # noqa: E402
from mcp_client import (  # noqa: E402
    MockAlpacaMCPClient,
    normalize_orders,
    normalize_positions,
)
from portfolio import (  # noqa: E402
    booked_risk_change,
    close_does_not_raise_booked_risk,
    close_leaves_no_uncovered_short,
    working_order_risk,
    working_risk,
)
from strategy import MomentumRiskCapStrategy  # noqa: E402


def occ(root: str, dte: int, kind: str, strike: float) -> str:
    expiry = (datetime.now(timezone.utc) + timedelta(days=dte)).strftime("%y%m%d")
    letter = "C" if kind == "call" else "P"
    return f"{root}{expiry}{letter}{int(round(strike * 1000)):08d}"


# The one structure in the mock's seeded book the exit policy leaves alone, so a
# working order can be aimed at it without the exits closing it out from underneath the
# assertion. Its coordinates are grid points of the mock's own chain -- see
# MockAlpacaMCPClient._seed_positions -- not free-floating numbers.
HELD_DTE = 45
HELD_LONG_STRIKE = 108.7
HELD_SHORT_STRIKE = 113.1
HELD_QTY = "4"


def wire_spread_order(order_id: str, limit_price: str, qty: str, status: str = "accepted",
                      filled_qty: str = "0", intent: str = "open", dte: int = 14,
                      long_strike: float = 101.0, short_strike: float = 104.0) -> dict:
    """One `GET /v2/orders` row for a working debit vertical, in Alpaca's shape.

    Mirrors the spec's own MultilegOptionsOrderResponse example, empty parent
    `symbol` / `side` / `asset_class` included -- those blanks are the whole reason
    option-ness has to be read off the legs.
    """
    long_intent = "buy_to_open" if intent == "open" else "sell_to_close"
    short_intent = "sell_to_open" if intent == "open" else "buy_to_close"
    return {
        "id": order_id,
        "client_order_id": f"mrcap-open-2026-08-26-{order_id}",
        "asset_class": "",
        "symbol": "",
        "side": "",
        "status": status,
        "order_class": "mleg",
        "order_type": "limit",
        "type": "limit",
        "qty": qty,
        "filled_qty": filled_qty,
        "limit_price": limit_price,
        "legs": [
            {
                "symbol": occ("SPY", dte, "call", long_strike), "asset_class": "us_option",
                "side": "buy" if intent == "open" else "sell",
                "position_intent": long_intent, "ratio_qty": "1", "status": status,
            },
            {
                "symbol": occ("SPY", dte, "call", short_strike), "asset_class": "us_option",
                "side": "sell" if intent == "open" else "buy",
                "position_intent": short_intent, "ratio_qty": "1", "status": status,
            },
        ],
    }


def wire_position(dte: int, kind: str, strike: float, side: str, qty: int,
                  root: str = "SPY", price: float = 2.00) -> dict:
    """One `GET /v2/positions` row, in the shape `normalize_positions` reads."""
    sign = 1 if side == "long" else -1
    return {
        "asset_class": "us_option",
        "symbol": occ(root, dte, kind, strike),
        "side": side,
        "qty": f"{sign * qty}",
        "qty_available": f"{sign * qty}",
        "avg_entry_price": f"{price:.2f}",
        "current_price": f"{price:.2f}",
        "cost_basis": f"{sign * qty * price * 100:.2f}",
        "unrealized_pl": "0",
    }


def wire_leg_close(order_id: str, dte: int, kind: str, strike: float, intent: str,
                   qty: str = "3", root: str = "SPY") -> dict:
    """A close of a *single* leg -- the shape that can uncap a vertical."""
    return {
        "id": order_id,
        "client_order_id": f"close-{order_id}",
        "asset_class": "us_option",
        "symbol": occ(root, dte, kind, strike),
        "side": "sell" if intent == "sell_to_close" else "buy",
        "status": "accepted",
        "order_class": "simple",
        "order_type": "market",
        "type": "market",
        "qty": qty,
        "filled_qty": "0",
        "limit_price": None,
        "position_intent": intent,
        "legs": None,
    }


def vertical_book(dte: int = 14, qty: int = 3) -> list:
    """The book `wire_spread_order` closes: a 101/104 debit call vertical."""
    return normalize_positions([
        wire_position(dte, "call", 101.0, "long", qty, price=2.00),
        wire_position(dte, "call", 104.0, "short", qty, price=0.80),
    ])


def wire_single_order(order_id: str, **overrides) -> dict:
    """One `GET /v2/orders` row for a working single-leg option order."""
    row = {
        "id": order_id,
        "client_order_id": f"single-{order_id}",
        "asset_class": "us_option",
        "symbol": occ("SPY", 14, "call", 101.0),
        "side": "buy",
        "status": "new",
        "order_class": "simple",
        "order_type": "limit",
        "type": "limit",
        "qty": "2",
        "filled_qty": "0",
        "limit_price": "1.50",
        "position_intent": "buy_to_open",
        "legs": None,
    }
    row.update(overrides)
    return row


class TestNormalizeOrders(unittest.TestCase):
    """The wire shape, read off the vendored spec's examples rather than guessed."""

    def test_a_multileg_parent_is_an_option_order_despite_its_blank_asset_class(self):
        # The spec's own example carries asset_class "" on the parent. Classifying
        # off the parent would file every spread this agent sends as "not an option"
        # and drop it out of the budget entirely.
        rows = normalize_orders([wire_spread_order("o1", "1.10", "3")])
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["is_option"])
        self.assertEqual(len(rows[0]["symbols"]), 2)

    def test_parent_qty_is_the_structure_count_that_pairs_with_the_limit_price(self):
        rows = normalize_orders([wire_spread_order("o1", "1.10", "3")])
        self.assertEqual(rows[0]["qty"], 3.0)
        self.assertEqual(rows[0]["limit_price"], 1.10)

    def test_terminal_orders_are_dropped(self):
        rows = normalize_orders([
            wire_spread_order("filled", "1.10", "3", status="filled"),
            wire_spread_order("canceled", "1.10", "3", status="canceled"),
            wire_spread_order("expired", "1.10", "3", status="expired"),
            wire_spread_order("rejected", "1.10", "3", status="rejected"),
            wire_spread_order("replaced", "1.10", "3", status="replaced"),
            wire_spread_order("live", "1.10", "3", status="accepted"),
        ])
        self.assertEqual([r["id"] for r in rows], ["live"])

    def test_an_ambiguous_status_is_kept_because_missing_a_live_order_is_worse(self):
        # `held`, `calculated` and `done_for_day` are not clearly terminal in the
        # spec text. Counting a dead order costs a trade; skipping a live one lets
        # an unbudgeted structure onto the book.
        rows = normalize_orders([
            wire_spread_order("held", "1.10", "3", status="held"),
            wire_spread_order("dfd", "1.10", "3", status="done_for_day"),
        ])
        self.assertEqual(len(rows), 2)

    def test_remaining_qty_excludes_the_part_that_already_filled(self):
        # The filled 2 of 5 is already a position and is already counted there.
        rows = normalize_orders([wire_spread_order("o1", "1.10", "5", filled_qty="2")])
        self.assertEqual(rows[0]["remaining_qty"], 3.0)

    def test_a_non_list_payload_is_no_orders_not_a_crash(self):
        self.assertEqual(normalize_orders({"orders": []}), [])
        self.assertEqual(normalize_orders(None), [])


class TestWorkingOrderRisk(unittest.TestCase):
    def test_a_working_debit_spread_risks_limit_price_times_100_times_qty(self):
        row = normalize_orders([wire_spread_order("o1", "1.10", "3")])[0]
        self.assertEqual(working_order_risk(row, []), 330.0)

    def test_only_the_unfilled_part_is_charged(self):
        row = normalize_orders([wire_spread_order("o1", "1.10", "5", filled_qty="2")])[0]
        self.assertEqual(working_order_risk(row, []), 330.0)

    def test_a_working_close_of_the_whole_structure_adds_no_risk(self):
        # It removes exposure the position side already counted; charging its
        # notional again would bill the account twice for one structure. Both legs
        # come off together, so nothing is left behind to be naked.
        row = normalize_orders([wire_spread_order("o1", "1.10", "3", intent="close")])[0]
        self.assertEqual(working_order_risk(row, vertical_book()), 0.0)

    def test_a_close_with_no_book_to_match_against_is_not_free(self):
        # "It must be closing something" is the assumption this whole class of bug
        # lives in. With no position to match, there is no evidence of that.
        row = normalize_orders([wire_spread_order("o1", "1.10", "3", intent="close")])[0]
        self.assertIsNone(working_order_risk(row, []))

    def test_a_market_order_is_unmeasured_risk_not_zero_risk(self):
        # No limit price means the fill price is whatever the book gives it. The
        # naked-long fallback path sends exactly this kind of order.
        row = normalize_orders([
            wire_single_order("o1", order_type="market", type="market", limit_price=None)
        ])[0]
        self.assertIsNone(working_order_risk(row, []))

    def test_a_net_credit_order_is_unpriceable_like_a_credit_position(self):
        row = normalize_orders([wire_spread_order("o1", "-0.80", "3")])[0]
        self.assertIsNone(working_order_risk(row, []))

    def test_a_non_option_order_is_unpriceable(self):
        row = normalize_orders([
            wire_single_order("o1", asset_class="us_equity", symbol="SPY")
        ])[0]
        self.assertIsNone(working_order_risk(row, []))

    def test_an_order_with_no_readable_qty_is_unpriceable(self):
        row = normalize_orders([wire_single_order("o1", qty=None)])[0]
        self.assertIsNone(working_order_risk(row, []))


class TestWorkingRiskTotal(unittest.TestCase):
    def test_it_sums_and_breaks_down_by_client_order_id(self):
        rows = normalize_orders([
            wire_spread_order("o1", "1.10", "3"),
            wire_spread_order("o2", "0.50", "2"),
        ])
        total, unpriceable, breakdown = working_risk(rows, [])
        self.assertEqual(total, 430.0)
        self.assertEqual(unpriceable, [])
        self.assertEqual(sorted(breakdown.values()), [100.0, 330.0])

    def test_an_unpriceable_order_is_named_and_left_out_of_the_total(self):
        rows = normalize_orders([
            wire_spread_order("o1", "1.10", "3"),
            wire_spread_order("o2", "-0.80", "2"),
        ])
        total, unpriceable, _ = working_risk(rows, [])
        self.assertEqual(total, 330.0)
        self.assertEqual(len(unpriceable), 1)
        self.assertIn("o2", unpriceable[0])

    def test_no_working_orders_is_zero_and_says_so_cleanly(self):
        self.assertEqual(working_risk([], []), (0.0, [], {}))


class TestWorkingOrdersEndToEnd(unittest.TestCase):
    """Through `run_once`: the in-flight order has to reach the sizing decision."""

    HELD = 2118.0  # the mock's seeded position book, priced off its own chain

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "decisions.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def run_pass(self, seed_orders=None, **strategy_kwargs):
        async def go():
            journal = Journal(self.path)
            async with MockAlpacaMCPClient(seed_orders=seed_orders) as client:
                strategy = MomentumRiskCapStrategy(**strategy_kwargs)
                return await run_once(client, strategy, journal, "SPY", 30, None)

        return asyncio.run(go())

    def test_with_no_working_orders_the_total_is_the_position_book_alone(self):
        record = self.run_pass()
        self.assertEqual(record["held_risk"], self.HELD)
        self.assertEqual(record["working_risk"], 0.0)
        self.assertEqual(record["open_risk"], self.HELD)

    def test_a_working_entry_is_added_to_the_budget_before_it_fills(self):
        record = self.run_pass(seed_orders=[wire_spread_order("o1", "1.10", "3")])
        self.assertEqual(record["held_risk"], self.HELD)
        self.assertEqual(record["working_risk"], 330.0)
        self.assertEqual(record["open_risk"], self.HELD + 330.0)
        self.assertEqual(sum(record["working_risk_by_order"].values()), 330.0)

    def test_a_working_entry_can_be_what_pushes_the_book_over_the_cap(self):
        # $2,290 held is inside a 2.4% cap of $100k equity ($2,400); the extra
        # $330 in flight is not. Without the working-order term this pass trades.
        record = self.run_pass(
            seed_orders=[wire_spread_order("o1", "1.10", "3")],
            max_portfolio_risk_pct=0.024,
        )
        self.assertEqual(record["action"], "hold")
        self.assertEqual(record["contracts"], 0)
        self.assertEqual(record["max_loss"], 0.0)
        self.assertIsNone(record["order"])
        self.assertIn("portfolio risk cap", record["reason"])

    def test_the_same_book_without_the_working_order_does_trade(self):
        # The control for the test above: same cap, same positions, no in-flight
        # order -- so the hold there is caused by the working order and nothing else.
        record = self.run_pass(max_portfolio_risk_pct=0.024)
        self.assertNotEqual(record["action"], "hold")
        self.assertGreater(record["contracts"], 0)

    def test_an_unpriceable_working_order_stands_the_entry_side_down(self):
        # Same rule the position side already follows: a total known to be missing a
        # term cannot be used to compute headroom.
        record = self.run_pass(seed_orders=[wire_spread_order("o1", "-0.80", "2")])
        self.assertEqual(record["action"], "hold")
        self.assertEqual(record["contracts"], 0)
        self.assertIsNone(record["order"])
        self.assertEqual(len(record["unpriceable_risk"]), 1)
        self.assertIn("working order(s)", record["reason"])

    def test_a_working_close_does_not_inflate_the_budget(self):
        # Seeded against the structure the mock really holds and the exit policy
        # leaves alone: the 45-DTE SPY 108.7/113.1 vertical. Both legs, so it flattens.
        record = self.run_pass(
            seed_orders=[wire_spread_order(
                "o1", "1.10", HELD_QTY, intent="close", dte=HELD_DTE,
                long_strike=HELD_LONG_STRIKE, short_strike=HELD_SHORT_STRIKE,
            )]
        )
        self.assertEqual(record["working_risk"], 0.0)
        self.assertEqual(record["open_risk"], self.HELD)
        # 0.0 has to mean "matched the book leg for leg and found it flat", not "could
        # not match it at all" -- an unmatched close also contributes nothing, so
        # without this the test would keep passing if the seeded book moved.
        self.assertEqual(record["unpriceable_risk"], [])

    def test_a_close_of_the_protective_leg_alone_stands_the_entry_side_down(self):
        # The whole point of this step. `sell_to_close` on the long leg of the mock's
        # 45-DTE vertical leaves a bare short 113.1 call -- unbounded -- while the
        # position side still reports that structure at its net debit. The budget
        # cannot be shown to cover what is about to happen, so nothing new goes out.
        record = self.run_pass(
            seed_orders=[wire_leg_close(
                "c1", HELD_DTE, "call", HELD_LONG_STRIKE, "sell_to_close", qty=HELD_QTY
            )]
        )
        self.assertEqual(record["action"], "hold")
        self.assertEqual(record["contracts"], 0)
        self.assertIsNone(record["order"])
        self.assertEqual(len(record["unpriceable_risk"]), 1)
        self.assertIn("protective leg", record["unpriceable_risk"][0])

    def test_a_close_that_raises_the_booked_risk_stands_the_entry_side_down(self):
        # The mirror image of the test above, and the reason the check is leg by leg
        # rather than a blanket "single-leg closes are suspicious": buying back the
        # short leaves a long call, whose floor is zero, so nothing is left uncovered
        # -- the naked-short check passes. It is still not free. The short leg's
        # credit was netting the structure's basis down, and losing it takes the
        # mock's own booked risk from $428.00 to $904.00 the moment this fills. An
        # order that raises the account's risk figure must not be charged zero.
        record = self.run_pass(
            seed_orders=[wire_leg_close(
                "c1", HELD_DTE, "call", HELD_SHORT_STRIKE, "buy_to_close", qty=HELD_QTY
            )]
        )
        self.assertEqual(record["action"], "hold")
        self.assertEqual(record["contracts"], 0)
        self.assertIsNone(record["order"])
        self.assertEqual(len(record["unpriceable_risk"]), 1)
        self.assertIn("$428.00 -> $904.00", record["unpriceable_risk"][0])



class FillsBetweenReadsClient(MockAlpacaMCPClient):
    """A client whose working order fills in the gap between the two risk reads.

    `run_once` measures the book with two calls. Whichever it makes first sees the
    pre-fill world; the second sees the post-fill one. That gap is not theoretical
    -- it is one round trip during market hours on an order that was already
    marketable -- and which of the two numbers goes missing depends entirely on the
    order of the calls.
    """

    ORDER_ID = "race-1"
    LIMIT_PRICE = "1.10"
    QTY = "5"
    RISK = 550.0  # 1.10 x 100 x 5

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.filled = False
        self.reads: list[str] = []

    def _advance(self, which: str) -> None:
        self.reads.append(which)
        # The fill lands after whichever read happens first.
        self.filled = True

    async def get_orders(self):
        pre_fill = not self.filled
        self._advance("orders")
        row = wire_spread_order(
            self.ORDER_ID, self.LIMIT_PRICE, self.QTY,
            status="accepted" if pre_fill else "filled",
            filled_qty="0" if pre_fill else self.QTY,
        )
        return normalize_orders([row])

    async def get_all_positions(self):
        pre_fill = not self.filled
        self._advance("positions")
        rows = self._seed_positions()
        if not pre_fill:
            # The same structure, now on the book: a $2.00 long against a $0.90
            # short, 5 lots -> $550 of net debit, exactly what the order risked.
            rows = rows + [
                self._seed_position("SPY", 14, "call", 111.0, "long", 5, 2.00, 2.00),
                self._seed_position("SPY", 14, "call", 114.0, "short", 5, 0.90, 0.90),
            ]
        return [
            pos for pos in normalize_positions(rows) if pos["symbol"] not in self._closed_symbols
        ]


class TestFillBetweenTheTwoReads(unittest.TestCase):
    """The order of the two snapshots decides whether a fill is double-counted or lost.

    Raised by Codex's 2026-08-26 02:18 review, which reproduced the miss against the
    first version of this code (positions first). Verified here rather than taken on
    trust: with positions read first the $550 lands in neither snapshot.
    """

    HELD = 2118.0

    def run_pass(self, **strategy_kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Journal(Path(tmp) / "decisions.jsonl")

            async def go():
                async with FillsBetweenReadsClient() as client:
                    strategy = MomentumRiskCapStrategy(**strategy_kwargs)
                    record = await run_once(client, strategy, journal, "SPY", 30, None)
                    return record, client

            return asyncio.run(go())

    def test_the_orders_snapshot_is_taken_first(self):
        _, client = self.run_pass()
        # Not a style assertion: reading positions first is what loses the fill.
        self.assertEqual(client.reads[0], "orders")

    def test_a_fill_between_the_reads_is_counted_twice_not_lost(self):
        record, _ = self.run_pass()
        risk = FillsBetweenReadsClient.RISK
        # Working when the orders were read, a position when the positions were
        # read. Both snapshots are honest about the world they saw; the sum
        # over-states by exactly one copy of the structure.
        self.assertEqual(record["working_risk"], risk)
        self.assertEqual(record["held_risk"], self.HELD + risk)
        self.assertEqual(record["open_risk"], self.HELD + 2 * risk)
        # The point of preferring the over-count: the structure is never invisible.
        self.assertGreaterEqual(record["open_risk"], self.HELD + risk)

    def test_the_over_count_can_only_ever_hold_the_agent_back(self):
        # $3,390 measured against a 3.2% cap of $100k ($3,200) -> hold. An
        # over-count refuses a trade the account may have had room for; the miss it
        # replaces would have let a trade out on top of an unbudgeted $550.
        record, _ = self.run_pass(max_portfolio_risk_pct=0.032)
        self.assertEqual(record["action"], "hold")
        self.assertEqual(record["contracts"], 0)
        self.assertIsNone(record["order"])


def wire_two_leg_order(long_leg: dict, short_leg: dict, limit_price: str = "1.00",
                       qty: str = "1") -> dict:
    """A working two-leg option order whose leg geometry the caller chooses.

    Same parent as `wire_spread_order` -- blank symbol/side/asset_class, positive net
    limit price, `accepted` -- so every test below differs from a priceable vertical
    in its legs and nothing else.
    """
    return {
        "id": "geom", "client_order_id": "geom-1", "asset_class": "", "symbol": "",
        "side": "", "status": "accepted", "order_class": "mleg", "order_type": "limit",
        "type": "limit", "qty": qty, "filled_qty": "0", "limit_price": limit_price,
        "legs": [long_leg, short_leg],
    }


def leg(symbol: str, intent: str, ratio: str = "1") -> dict:
    return {
        "symbol": symbol,
        "asset_class": "us_option",
        "side": "buy" if intent.startswith("buy") else "sell",
        "position_intent": intent,
        "ratio_qty": ratio,
        "status": "accepted",
    }


class TestLegGeometrySurvivesNormalization(unittest.TestCase):
    """`normalize_orders` has to carry the ratio and the strikes, or the risk layer
    cannot tell a vertical from a ratio spread."""

    def test_each_leg_keeps_its_ratio_and_parsed_occ_geometry(self):
        rows = normalize_orders([wire_spread_order("o1", "1.10", "3")])
        legs = rows[0]["legs"]
        self.assertEqual([lg["ratio_qty"] for lg in legs], [1.0, 1.0])
        self.assertEqual([lg["strike"] for lg in legs], [101.0, 104.0])
        self.assertEqual({lg["option_type"] for lg in legs}, {"call"})
        self.assertEqual({lg["underlying"] for lg in legs}, {"SPY"})
        self.assertEqual(len({lg["expiry"] for lg in legs}), 1)

    def test_a_single_leg_order_keeps_the_parents_own_intent(self):
        rows = normalize_orders([wire_single_order("s1")])
        self.assertEqual(rows[0]["legs"], [])
        self.assertEqual(rows[0]["position_intent"], "buy_to_open")


class TestOnlyWhitelistedTopologiesArePricedAsNetDebit(unittest.TestCase):
    """The blocker Codex raised 2026-08-26 02:18: "option + opening + positive limit
    price" was being read as "maximum loss = net debit", which is false for any
    geometry other than a lone long option or a 1:1 debit vertical.

    Every test here priced to a finite number before this pass; each one is a
    structure whose real worst case is either unbounded or bigger than the debit.
    """

    def test_a_11_debit_vertical_is_still_priced(self):
        # The control: the shape this strategy actually sends must keep working.
        order = normalize_orders([wire_spread_order("o1", "1.10", "3")])[0]
        self.assertEqual(working_order_risk(order, []), 330.0)

    def test_a_lone_long_call_is_still_priced(self):
        order = normalize_orders([wire_single_order("s1")])[0]
        self.assertEqual(working_order_risk(order, []), 300.0)

    def test_a_buy1_sell2_ratio_spread_is_unpriceable(self):
        # Codex's counter-example: net debit $100, loss above the short strike
        # unbounded. Before this pass it reported $100.
        order = normalize_orders([wire_two_leg_order(
            leg(occ("SPY", 14, "call", 101.0), "buy_to_open", ratio="1"),
            leg(occ("SPY", 14, "call", 104.0), "sell_to_open", ratio="2"),
        )])[0]
        self.assertEqual(order["legs"][1]["ratio_qty"], 2.0)
        self.assertIsNone(working_order_risk(order, []))

    def test_a_mixed_roll_is_unpriceable(self):
        # The closing leg's credit shrinks the parent net debit, so the smaller the
        # number, the larger the new position it hides.
        order = normalize_orders([wire_two_leg_order(
            leg(occ("SPY", 7, "call", 101.0), "sell_to_close"),
            leg(occ("SPY", 21, "call", 104.0), "buy_to_open"),
        )])[0]
        self.assertTrue(order["opening"])
        self.assertIsNone(working_order_risk(order, []))

    def test_a_naked_short_is_unpriceable(self):
        # A positive limit price on a lone sell_to_open is a credit received, and
        # the loss on the other side of the strike has no ceiling at all.
        order = normalize_orders([wire_single_order(
            "s2", side="sell", position_intent="sell_to_open"
        )])[0]
        self.assertIsNone(working_order_risk(order, []))

    def test_an_unreadable_intent_is_unpriceable(self):
        order = normalize_orders([wire_single_order("s3", position_intent="")])[0]
        self.assertIsNone(working_order_risk(order, []))

    def test_a_credit_vertical_is_unpriceable(self):
        # Long above short on calls: worst case is strike width, not the price paid.
        order = normalize_orders([wire_two_leg_order(
            leg(occ("SPY", 14, "call", 104.0), "buy_to_open"),
            leg(occ("SPY", 14, "call", 101.0), "sell_to_open"),
        )])[0]
        self.assertIsNone(working_order_risk(order, []))

    def test_a_calendar_is_unpriceable(self):
        order = normalize_orders([wire_two_leg_order(
            leg(occ("SPY", 21, "call", 101.0), "buy_to_open"),
            leg(occ("SPY", 7, "call", 101.0), "sell_to_open"),
        )])[0]
        self.assertIsNone(working_order_risk(order, []))

    def test_mismatched_option_types_are_unpriceable(self):
        order = normalize_orders([wire_two_leg_order(
            leg(occ("SPY", 14, "call", 101.0), "buy_to_open"),
            leg(occ("SPY", 14, "put", 104.0), "sell_to_open"),
        )])[0]
        self.assertIsNone(working_order_risk(order, []))

    def test_two_legs_on_different_underlyings_are_unpriceable(self):
        order = normalize_orders([wire_two_leg_order(
            leg(occ("SPY", 14, "call", 101.0), "buy_to_open"),
            leg(occ("QQQ", 14, "call", 104.0), "sell_to_open"),
        )])[0]
        self.assertIsNone(working_order_risk(order, []))

    def test_a_three_leg_structure_is_unpriceable(self):
        row = wire_two_leg_order(
            leg(occ("SPY", 14, "call", 101.0), "buy_to_open"),
            leg(occ("SPY", 14, "call", 104.0), "sell_to_open"),
        )
        row["legs"].append(leg(occ("SPY", 14, "call", 107.0), "sell_to_open"))
        order = normalize_orders([row])[0]
        self.assertIsNone(working_order_risk(order, []))

    def test_an_unparseable_leg_symbol_is_unpriceable(self):
        order = normalize_orders([wire_two_leg_order(
            leg("NOT-AN-OCC-SYMBOL", "buy_to_open"),
            leg(occ("SPY", 14, "call", 104.0), "sell_to_open"),
        )])[0]
        self.assertIsNone(working_order_risk(order, []))

    def test_an_unpriceable_topology_reaches_the_caller_as_a_named_gap(self):
        # Not silently dropped from the total: `working_risk` has to report it so
        # the entry side stands down rather than sizing against a short number.
        order = normalize_orders([wire_two_leg_order(
            leg(occ("SPY", 14, "call", 101.0), "buy_to_open", ratio="1"),
            leg(occ("SPY", 14, "call", 104.0), "sell_to_open", ratio="2"),
        )])[0]
        total, unpriceable, breakdown = working_risk([order], [])
        self.assertEqual(total, 0.0)
        self.assertEqual(breakdown, {})
        self.assertEqual(len(unpriceable), 1)
        self.assertIn("buy_to_open/sell_to_open", unpriceable[0])


class TestCloseLegMatching(unittest.TestCase):
    """A close is only worth zero if the book says it leaves nothing uncovered.

    Charging every closing order 0.0 reads as conservative and is not. A vertical is
    two legs that only have a defined loss together; a close that removes the long
    one converts it into a naked short with no upper bound on the loss -- and at that
    moment the account's own arithmetic still shows the pre-close debit on the
    position side and 0.0 on the order side. The exposure exists in the market and
    nowhere in the risk total.
    """

    def close_risk(self, order_row, positions):
        return working_order_risk(normalize_orders([order_row])[0], positions)

    def test_closing_the_long_leg_alone_leaves_a_naked_short_and_is_not_free(self):
        risk = self.close_risk(
            wire_leg_close("c1", 14, "call", 101.0, "sell_to_close"), vertical_book()
        )
        self.assertIsNone(risk)

    def test_closing_the_short_leg_alone_leaves_nothing_naked(self):
        # The narrow claim, and the only one this predicate makes: a lone long has no
        # short behind it. Whether the close is *free* is a different question, and
        # the next test is the one that answers it.
        order = normalize_orders([wire_leg_close("c1", 14, "call", 104.0, "buy_to_close")])[0]
        self.assertTrue(close_leaves_no_uncovered_short(order, vertical_book()))

    def test_buying_back_the_short_leg_raises_the_booked_risk(self):
        """Closed 2026-08-26 09:xx -- was an expectedFailure pinned at 06:16.

        `close_leaves_no_uncovered_short` proves one thing: after the fill, no short
        stands without a long. It does not prove the other thing a budget needs:
        that the risk booked after the fill is not *higher* than the risk booked
        before it. Buying back the short leg of a debit vertical removes the credit
        that was reducing the structure's net cost basis, so the position side's own
        number goes up the moment this close fills -- and the order was charged 0.0.
        Reproduced on the mock's own book in
        `scratch/close_raises_risk_20260826_0616.txt`: booked risk $440.00 -> $800.00,
        a $360.00 rise charged as zero. On this fixture it is $360.00 -> $600.00.
        """
        book = vertical_book()
        order = normalize_orders([wire_leg_close("c1", 14, "call", 104.0, "buy_to_close")])[0]
        self.assertIsNone(working_order_risk(order, book))
        self.assertFalse(close_does_not_raise_booked_risk(order, book))
        group = f"SPY {book[0]['expiry']} call"
        self.assertEqual(booked_risk_change(order, book), {group: (360.0, 600.0)})

    def test_a_close_that_raises_the_book_is_reported_as_a_rise_not_as_unreadable(self):
        # Two different failures wear the word "unpriceable" now. An operator who
        # cannot tell "we could not read this order" from "we read it and it makes
        # the book worse" cannot act on either.
        order = normalize_orders([wire_leg_close("c1", 14, "call", 104.0, "buy_to_close")])[0]
        _, unpriceable, _ = working_risk([order], vertical_book())
        self.assertEqual(len(unpriceable), 1)
        self.assertIn("raises the risk the position side books", unpriceable[0])
        self.assertIn("$360.00 -> $600.00", unpriceable[0])

    def test_a_partial_close_of_both_legs_stays_balanced_and_is_free(self):
        row = normalize_orders([wire_spread_order("o1", "1.10", "1", intent="close")])[0]
        self.assertEqual(working_order_risk(row, vertical_book(qty=3)), 0.0)

    def test_a_put_vertical_needs_its_protective_long_above_the_short(self):
        # A real debit put vertical: long 100 at 2.00, short 96 at 0.80. The two legs
        # priced the same would net to a zero basis, which is not a debit and so has
        # no cost-basis risk figure at all -- the shape this repo never opens.
        book = normalize_positions([
            wire_position(14, "put", 100.0, "long", 2, price=2.00),
            wire_position(14, "put", 96.0, "short", 2, price=0.80),
        ])
        self.assertIsNone(
            self.close_risk(wire_leg_close("c1", 14, "put", 100.0, "sell_to_close", qty="2"), book)
        )
        # Both failure modes are symmetric with the call case: selling the protective
        # long leaves a naked short, buying the short back raises the booked basis
        # from $240.00 to $400.00.
        order = normalize_orders([wire_leg_close("c2", 14, "put", 96.0, "buy_to_close", qty="2")])[0]
        self.assertTrue(close_leaves_no_uncovered_short(order, book))
        self.assertIsNone(working_order_risk(order, book))
        self.assertEqual(
            booked_risk_change(order, book), {f"SPY {book[0]['expiry']} put": (240.0, 400.0)}
        )

    def test_a_residual_long_on_the_credit_side_does_not_count_as_cover(self):
        # Long 104 / short 101 bounds the loss at the strike width, not at cost
        # basis -- which is the only number the position side reports. Bounded by
        # something nobody is measuring is not the same as budgeted.
        book = normalize_positions([
            wire_position(14, "call", 104.0, "long", 2),
            wire_position(14, "call", 101.0, "short", 2),
            wire_position(14, "call", 99.0, "long", 2),
        ])
        self.assertIsNone(
            self.close_risk(wire_leg_close("c1", 14, "call", 99.0, "sell_to_close", qty="2"), book)
        )

    def test_a_close_of_a_symbol_the_book_does_not_show_is_unverifiable(self):
        self.assertIsNone(
            self.close_risk(wire_leg_close("c1", 14, "call", 999.0, "sell_to_close"), vertical_book())
        )

    def test_an_intent_pointing_the_wrong_way_is_not_a_close(self):
        # `sell_to_close` against a leg the account is short opens a bigger short.
        self.assertIsNone(
            self.close_risk(wire_leg_close("c1", 14, "call", 104.0, "sell_to_close"), vertical_book())
        )

    def test_closing_more_contracts_than_are_held_is_unverifiable(self):
        self.assertIsNone(
            self.close_risk(
                wire_leg_close("c1", 14, "call", 101.0, "sell_to_close", qty="9"),
                vertical_book(qty=3),
            )
        )

    def test_only_the_unfilled_part_of_a_close_is_matched(self):
        # 4 of 7 already filled: the book shows 3 left and the remaining 3 flatten it.
        # Matching all 7 against a 3-contract book would read as closing more than is
        # held and stand the entry side down on an order that is perfectly fine.
        row = wire_spread_order("o1", "1.10", "7", intent="close", filled_qty="4")
        self.assertEqual(self.close_risk(row, vertical_book(qty=3)), 0.0)

    def test_a_stock_close_cannot_be_matched_because_the_book_is_options_only(self):
        row = wire_leg_close("c1", 14, "call", 101.0, "sell_to_close")
        row["symbol"] = "SPY"
        row["asset_class"] = "us_equity"
        self.assertIsNone(self.close_risk(row, vertical_book()))

    def test_a_duplicate_position_row_is_refused_rather_than_guessed_at(self):
        book = vertical_book() + vertical_book()
        self.assertIsNone(
            self.close_risk(wire_leg_close("c1", 14, "call", 104.0, "buy_to_close"), book)
        )

    def test_an_untouched_structure_elsewhere_in_the_book_is_not_dragged_in(self):
        # A naked short on another expiry is somebody else's problem for this order:
        # closing SPY 14-DTE says nothing about it, and refusing on its account would
        # freeze every close on the book.
        book = vertical_book() + normalize_positions([
            wire_position(30, "call", 110.0, "short", 1)
        ])
        self.assertEqual(
            self.close_risk(wire_spread_order("o1", "1.10", "3", intent="close"), book),
            0.0,
        )

    def test_the_predicate_and_the_price_agree(self):
        order = normalize_orders([wire_spread_order("o1", "1.10", "3", intent="close")])[0]
        self.assertTrue(close_leaves_no_uncovered_short(order, vertical_book()))
        self.assertFalse(close_leaves_no_uncovered_short(order, []))

    def test_an_unmatched_close_is_named_in_the_total_not_dropped_from_it(self):
        order = normalize_orders([
            wire_leg_close("c1", 14, "call", 101.0, "sell_to_close")
        ])[0]
        total, unpriceable, breakdown = working_risk([order], vertical_book())
        self.assertEqual(total, 0.0)
        self.assertEqual(breakdown, {})
        self.assertEqual(len(unpriceable), 1)
        self.assertIn("protective leg", unpriceable[0])


if __name__ == "__main__":
    unittest.main()
