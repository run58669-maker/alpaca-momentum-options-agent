"""Tests for the exit path: position wire shape, structure grouping, exit rules, close routing.

The `Position` shape asserted here was read on 2026-08-25 out of the OpenAPI spec that
alpacahq/alpaca-mcp-server vendors (`GET /v2/positions` -> a bare array of `Position`,
every number a string, `side` one of long/short, `asset_class` "us_option" for options)
and the closing-order shape out of upstream `overrides.py::place_option_order`. If these
are wrong the agent sends closing orders that are rejected -- or worse, accepted for the
wrong quantity -- so they are pinned.

Stdlib only:
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

from agent import manage_exits, run_once, unconfirmed_closes  # noqa: E402
from exits import (  # noqa: E402
    ExitPolicy,
    close_qty,
    days_to_expiry,
    group_structures,
    most_exposed_short,
    uncovered_short_contracts,
)
from journal import Journal  # noqa: E402
from mcp_client import (  # noqa: E402
    CLIENT_ORDER_ID_MAX_LEN,
    AlpacaMCPClient,
    AlpacaMCPError,
    MockAlpacaMCPClient,
    make_close_client_order_id,
    normalize_positions,
)
from strategy import MomentumRiskCapStrategy  # noqa: E402

TODAY = date(2026, 8, 25)


def wire_position(symbol="SPY260908C00100000", side="long", qty="5", entry="2.00",
                  current="3.60", asset_class="us_option", **overrides) -> dict:
    """One row of the real `GET /v2/positions` array."""
    sign = 1 if side == "long" else -1
    cost_basis = sign * float(entry) * abs(float(qty)) * 100
    unrealized = sign * (float(current) - float(entry)) * abs(float(qty)) * 100
    row = {
        "asset_id": "0000-0000",
        "symbol": symbol,
        "asset_class": asset_class,
        "side": side,
        "qty": qty,
        "qty_available": qty,
        "avg_entry_price": entry,
        "current_price": current,
        "cost_basis": f"{cost_basis:.2f}",
        "market_value": f"{sign * float(current) * abs(float(qty)) * 100:.2f}",
        "unrealized_pl": f"{unrealized:.2f}",
        "unrealized_plpc": f"{(unrealized / abs(cost_basis)) if cost_basis else 0:.4f}",
    }
    row.update(overrides)
    return row


def leg(symbol="SPY260908C00100000", side="long", qty=5.0, available=None,
        cost_basis=1000.0, unrealized=0.0, expiry="2026-09-08", kind="call",
        strike=100.0, underlying="SPY") -> dict:
    """An already-normalized position, in the shape the exit policy consumes."""
    return {
        "symbol": symbol, "underlying": underlying, "type": kind, "strike": strike,
        "expiry": expiry, "side": side, "qty": qty,
        "qty_available": qty if available is None else available,
        "avg_entry_price": 2.0, "current_price": 2.0,
        "cost_basis": cost_basis, "unrealized_pl": unrealized,
    }


class TestNormalizePositions(unittest.TestCase):
    def test_parses_the_wire_shape_and_coerces_the_string_numbers(self):
        rows = normalize_positions([wire_position()])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["underlying"], "SPY")
        self.assertEqual(row["type"], "call")
        self.assertEqual(row["expiry"], "2026-09-08")
        self.assertEqual(row["strike"], 100.0)
        self.assertEqual(row["side"], "long")
        self.assertEqual(row["qty"], 5.0)
        self.assertEqual(row["cost_basis"], 1000.0)
        self.assertEqual(row["unrealized_pl"], 800.0)
        self.assertIsInstance(row["qty"], float)

    def test_short_quantities_are_absolute_and_direction_comes_from_side(self):
        # Alpaca signs short quantities negative; a negative qty must never reach an
        # order body, and `side` -- not the sign -- is what says which way it is.
        rows = normalize_positions([wire_position(side="short", qty="-5", qty_available="-5")])
        self.assertEqual(rows[0]["side"], "short")
        self.assertEqual(rows[0]["qty"], 5.0)
        self.assertEqual(rows[0]["qty_available"], 5.0)
        self.assertEqual(rows[0]["cost_basis"], -1000.0)

    def test_equity_positions_are_not_option_structures(self):
        self.assertEqual(normalize_positions([wire_position(symbol="SPY", asset_class="us_equity")]), [])

    def test_asset_class_decides_what_is_an_option_not_the_symbol_shape(self):
        # The equity row above is also dropped by the OCC parse, which would hide a
        # broken asset_class filter. This row parses fine and must still be dropped:
        # only `us_option` rows are structures this exit policy may send orders for.
        self.assertEqual(normalize_positions([wire_position(asset_class="crypto")]), [])

    def test_drops_rows_whose_symbol_is_not_a_readable_contract(self):
        self.assertEqual(normalize_positions([wire_position(symbol="NOT-AN-OCC")]), [])

    def test_drops_flat_and_unsided_rows(self):
        self.assertEqual(normalize_positions([wire_position(qty="0")]), [])
        self.assertEqual(normalize_positions([wire_position(side="")]), [])

    def test_missing_qty_available_reads_as_all_of_it_not_none_of_it(self):
        row = wire_position()
        del row["qty_available"]
        self.assertEqual(normalize_positions([row])[0]["qty_available"], 5.0)

    def test_zero_qty_available_is_preserved_it_is_the_in_flight_signal(self):
        rows = normalize_positions([wire_position(qty_available="0")])
        self.assertEqual(rows[0]["qty_available"], 0.0)

    def test_output_is_sorted_by_symbol_not_input_order(self):
        rows = normalize_positions([
            wire_position(symbol="SPY260908C00103000"),
            wire_position(symbol="SPY260908C00100000"),
        ])
        self.assertEqual([r["symbol"] for r in rows],
                         ["SPY260908C00100000", "SPY260908C00103000"])

    def test_wrong_shaped_payload_is_empty_not_an_exception(self):
        self.assertEqual(normalize_positions({"positions": []}), [])
        self.assertEqual(normalize_positions(None), [])
        self.assertEqual(normalize_positions(["nonsense"]), [])


class TestGroupStructures(unittest.TestCase):
    def test_both_legs_of_a_vertical_land_in_one_structure(self):
        legs = [leg(symbol="SPY260908C00100000"),
                leg(symbol="SPY260908C00103000", side="short", cost_basis=-400.0)]
        structures = group_structures(legs)
        self.assertEqual(len(structures), 1)
        self.assertEqual(structures[0]["cost_basis"], 600.0)
        self.assertEqual(structures[0]["id"], "SPY 2026-09-08 call")

    def test_different_expiries_are_different_structures(self):
        structures = group_structures([leg(), leg(symbol="SPY260915C00100000", expiry="2026-09-15")])
        self.assertEqual(len(structures), 2)

    def test_calls_and_puts_never_merge(self):
        structures = group_structures([leg(), leg(symbol="SPY260908P00100000", kind="put")])
        self.assertEqual(len(structures), 2)

    def test_different_underlyings_never_merge(self):
        structures = group_structures([leg(), leg(symbol="QQQ260908C00100000", underlying="QQQ")])
        self.assertEqual(len(structures), 2)

    def test_unrealized_pl_is_the_sum_of_the_legs(self):
        structures = group_structures([
            leg(symbol="SPY260908C00100000", unrealized=800.0),
            leg(symbol="SPY260908C00103000", side="short", cost_basis=-400.0, unrealized=-250.0),
        ])
        self.assertEqual(structures[0]["unrealized_pl"], 550.0)

    def test_grouping_does_not_depend_on_input_order(self):
        a = [leg(symbol="SPY260908C00103000"), leg(symbol="SPY260908C00100000")]
        self.assertEqual(group_structures(a), group_structures(list(reversed(a))))


class TestCloseQtyAndDte(unittest.TestCase):
    def test_close_qty_is_the_smallest_leg_so_a_close_is_never_lopsided(self):
        self.assertEqual(close_qty([leg(qty=5.0), leg(qty=3.0)]), 3)

    def test_days_to_expiry_counts_forward(self):
        self.assertEqual(days_to_expiry("2026-09-08", TODAY), 14)

    def test_unreadable_expiry_is_none_not_zero(self):
        # 0 would read as "expiring today" and close the position on a parse bug.
        self.assertIsNone(days_to_expiry("not-a-date", TODAY))


class TestExitPolicy(unittest.TestCase):
    def setUp(self):
        self.policy = ExitPolicy(take_profit_pct=0.75, stop_loss_pct=0.50, close_before_dte=1)

    def structure(self, unrealized=0.0, cost=1000.0, expiry="2026-09-08", legs=None):
        legs = legs or [leg(expiry=expiry, cost_basis=cost, unrealized=unrealized)]
        return {
            "id": "SPY 2026-09-08 call", "underlying": "SPY", "expiry": expiry,
            "type": "call", "legs": legs, "cost_basis": cost, "unrealized_pl": unrealized,
        }

    def test_take_profit_fires_at_the_threshold(self):
        d = self.policy.evaluate(self.structure(unrealized=750.0), TODAY)
        self.assertEqual(d.action, "close")
        self.assertIn("take profit", d.reason)

    def test_just_under_the_take_profit_threshold_holds(self):
        d = self.policy.evaluate(self.structure(unrealized=749.0), TODAY)
        self.assertEqual(d.action, "hold")

    def test_stop_loss_fires_at_the_threshold(self):
        d = self.policy.evaluate(self.structure(unrealized=-500.0), TODAY)
        self.assertEqual(d.action, "close")
        self.assertIn("stop loss", d.reason)

    def test_just_inside_the_stop_holds(self):
        self.assertEqual(self.policy.evaluate(self.structure(unrealized=-499.0), TODAY).action, "hold")

    def test_time_stop_closes_a_winner_too(self):
        d = self.policy.evaluate(self.structure(unrealized=100.0, expiry="2026-08-26"), TODAY)
        self.assertEqual(d.action, "close")
        self.assertIn("time stop", d.reason)

    def test_time_stop_beats_take_profit_when_both_apply(self):
        # Order matters: on the last day the position comes off for the expiry reason,
        # and the journalled reason has to say which rule actually fired.
        d = self.policy.evaluate(self.structure(unrealized=900.0, expiry="2026-08-26"), TODAY)
        self.assertIn("time stop", d.reason)
        self.assertNotIn("take profit", d.reason)

    def test_an_already_expired_position_is_still_closed(self):
        d = self.policy.evaluate(self.structure(expiry="2026-08-20"), TODAY)
        self.assertEqual(d.action, "close")

    def test_a_credit_structure_gets_no_invented_percentage(self):
        # Net credit -> "down 50% of what it cost" has no meaning; only the time stop
        # is allowed to act, and pnl_pct must be absent rather than a made-up number.
        d = self.policy.evaluate(self.structure(unrealized=-5000.0, cost=-200.0), TODAY)
        self.assertEqual(d.action, "hold")
        self.assertIsNone(d.pnl_pct)

    def test_a_credit_structure_still_gets_the_time_stop(self):
        d = self.policy.evaluate(self.structure(cost=-200.0, expiry="2026-08-26"), TODAY)
        self.assertEqual(d.action, "close")

    def test_a_close_already_working_is_skipped_not_sent_twice(self):
        d = self.policy.evaluate(
            self.structure(unrealized=-900.0, legs=[leg(unrealized=-900.0, available=0.0)]), TODAY)
        self.assertEqual(d.action, "skip")
        self.assertIn("already working", d.reason)

    def test_one_blocked_leg_blocks_the_whole_structure(self):
        # Closing only the free leg of a vertical would leave a naked short behind.
        legs = [leg(symbol="SPY260908C00100000", unrealized=-900.0),
                leg(symbol="SPY260908C00103000", side="short", cost_basis=-400.0, available=0.0)]
        d = self.policy.evaluate(self.structure(unrealized=-900.0, cost=600.0, legs=legs), TODAY)
        self.assertEqual(d.action, "skip")

    def test_the_decision_carries_the_numbers_it_reasoned_from(self):
        d = self.policy.evaluate(self.structure(unrealized=750.0), TODAY)
        self.assertEqual((d.qty, d.cost_basis, d.unrealized_pl, d.dte), (5, 1000.0, 750.0, 14))
        self.assertAlmostEqual(d.pnl_pct, 0.75)

    def test_thresholds_are_arguments_not_constants(self):
        loose = ExitPolicy(take_profit_pct=2.0, stop_loss_pct=0.9, close_before_dte=0)
        self.assertEqual(loose.evaluate(self.structure(unrealized=750.0), TODAY).action, "hold")


class FakeSession:
    """Stands in for an MCP ClientSession, recording the args it was called with."""

    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = []

    async def call_tool(self, tool, args):
        self.calls.append((tool, args))
        return self.results.pop(0) if self.results else {}


def real_client_with(results=None):
    """A real AlpacaMCPClient without the env-var check, wired to a fake session."""
    client = object.__new__(AlpacaMCPClient)
    client._session = FakeSession(results)
    return client


class TestRealClientCloseOrder(unittest.TestCase):
    def test_a_single_long_leg_goes_out_as_sell_to_close(self):
        client = real_client_with()
        asyncio.run(client.place_option_close_order([leg()], 3))
        tool, args = client._session.calls[0]
        self.assertEqual(tool, "place_option_order")
        self.assertEqual(args["side"], "sell")
        self.assertEqual(args["position_intent"], "sell_to_close")
        self.assertEqual(args["qty"], "3")  # upstream types qty as a string
        self.assertEqual(args["type"], "market")
        self.assertEqual(args["time_in_force"], "day")  # options accept nothing else
        self.assertNotIn("legs", args)

    def test_a_single_short_leg_goes_out_as_buy_to_close(self):
        client = real_client_with()
        asyncio.run(client.place_option_close_order([leg(side="short")], 2))
        _, args = client._session.calls[0]
        self.assertEqual((args["side"], args["position_intent"]), ("buy", "buy_to_close"))

    def test_a_vertical_goes_out_as_one_mleg_order_with_opposite_intents(self):
        client = real_client_with()
        legs = [leg(symbol="SPY260908C00100000"),
                leg(symbol="SPY260908C00103000", side="short")]
        asyncio.run(client.place_option_close_order(legs, 5))
        _, args = client._session.calls[0]
        self.assertEqual(args["order_class"], "mleg")
        self.assertEqual(args["qty"], "5")
        self.assertEqual(
            [(g["symbol"], g["side"], g["position_intent"], g["ratio_qty"]) for g in args["legs"]],
            [("SPY260908C00100000", "sell", "sell_to_close", "1"),
             ("SPY260908C00103000", "buy", "buy_to_close", "1")],
        )
        self.assertNotIn("symbol", args)  # parent symbol/side are not used for mleg

    def test_no_legs_raises_rather_than_sending_an_empty_order(self):
        with self.assertRaises(AlpacaMCPError):
            asyncio.run(real_client_with().place_option_close_order([], 1))

    def test_get_all_positions_normalizes_the_wire_array(self):
        client = real_client_with([[wire_position(),
                                    wire_position(symbol="SPY", asset_class="us_equity")]])
        rows = asyncio.run(client.get_all_positions())
        self.assertEqual(client._session.calls[0], ("get_all_positions", {}))
        self.assertEqual([r["symbol"] for r in rows], ["SPY260908C00100000"])

    def test_get_all_positions_peels_the_trust_boundary_envelope(self):
        client = real_client_with([{"_alpaca_mcp_security": "x", "data": [wire_position()]}])
        self.assertEqual(len(asyncio.run(client.get_all_positions())), 1)


class TestExitsEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "decisions.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def records(self):
        return [json.loads(l) for l in self.path.read_text(encoding="utf-8").strip().splitlines()]

    def run_exits(self, passes=1):
        async def go():
            journal = Journal(self.path)
            async with MockAlpacaMCPClient() as client:
                out = [await manage_exits(client, journal, ExitPolicy()) for _ in range(passes)]
                return out, client._orders

        return asyncio.run(go())

    def test_the_mock_book_exercises_every_rule_exactly_once(self):
        (results,), _ = self.run_exits()
        reasons = " | ".join(r["reason"] for r in results)
        for rule in ("time stop", "stop loss", "take profit", "already working"):
            self.assertEqual(reasons.count(rule), 1, f"{rule} in: {reasons}")
        self.assertEqual(len([r for r in results if r["action"] == "exit_close"]), 3)
        self.assertEqual(len([r for r in results if r["action"] == "exit_hold"]), 1)
        self.assertEqual(len([r for r in results if r["action"] == "exit_skip"]), 1)

    def test_holds_and_skips_are_journalled_but_send_no_order(self):
        (results,), _ = self.run_exits()
        for record in results:
            self.assertEqual(record["order"] is not None, record["action"] == "exit_close")
            for field in ("structure", "symbols", "qty", "dte", "cost_basis",
                          "unrealized_pl", "pnl_pct", "reason"):
                self.assertIn(field, record)
        self.assertEqual(len(self.records()), 5)

    def test_a_closed_structure_is_not_closed_again_on_the_next_pass(self):
        (first, second), orders = self.run_exits(passes=2)
        self.assertEqual(len([r for r in first if r["action"] == "exit_close"]), 3)
        self.assertEqual(len([r for r in second if r["action"] == "exit_close"]), 0)
        self.assertEqual(len(orders), 3)  # three closes total, not six

    def test_the_vertical_is_closed_as_one_two_leg_order(self):
        (results,), _ = self.run_exits()
        vertical = [r for r in results if r["action"] == "exit_close" and len(r["symbols"]) == 2]
        self.assertEqual(len(vertical), 1)
        self.assertEqual(len(vertical[0]["order"]["legs"]), 2)
        self.assertEqual(
            sorted(g["position_intent"] for g in vertical[0]["order"]["legs"]),
            ["buy_to_close", "sell_to_close"],
        )

    def test_exits_still_run_when_the_circuit_breaker_has_tripped(self):
        # The breaker stops new risk. Taking risk off is the opposite of new risk --
        # a breaker that also froze the exits would trap the losers that tripped it.
        async def go():
            journal = Journal(self.path)
            async with MockAlpacaMCPClient() as client:
                strategy = MomentumRiskCapStrategy(max_daily_loss_pct=0.000001)
                return await run_once(client, strategy, journal, "SPY", 30, ExitPolicy())

        record = asyncio.run(go())
        self.assertEqual(record["action"], "hold")
        self.assertIn("circuit breaker", record["reason"])
        self.assertEqual(len([r for r in record["exits"] if r["action"] == "exit_close"]), 3)

    def test_no_exit_policy_means_no_exit_records_at_all(self):
        async def go():
            journal = Journal(self.path)
            async with MockAlpacaMCPClient() as client:
                return await run_once(client, MomentumRiskCapStrategy(), journal, "SPY")

        self.assertEqual(asyncio.run(go())["exits"], [])
        self.assertEqual([r for r in self.records() if str(r.get("action", "")).startswith("exit_")], [])

    def test_every_mock_position_symbol_is_a_readable_contract(self):
        # The mock book is only evidence if it is shaped like the real one.
        async def go():
            async with MockAlpacaMCPClient() as client:
                return await client.get_all_positions()

        rows = asyncio.run(go())
        self.assertEqual(len(rows), 7)
        for row in rows:
            self.assertIn(row["type"], ("call", "put"))
            self.assertGreater(row["strike"], 0)
            self.assertGreaterEqual(
                datetime.strptime(row["expiry"], "%Y-%m-%d").date(),
                (datetime.now(timezone.utc) - timedelta(days=1)).date(),
            )


class TestCloseOrderIdempotencyKey(unittest.TestCase):
    """`client_order_id` on every close.

    Upstream `place_option_order` takes `client_order_id` and forwards it into the
    POST /v2/orders body (overrides.py:266,334), and its own timeout message says a
    retry carrying the same value is refused by the API rather than duplicated. That
    is the only thing that makes retrying a timed-out close safe: without it a retry
    can flatten the position twice, and the second fill opens a fresh position the
    wrong way round. Read 2026-08-25 off alpacahq/alpaca-mcp-server @ main.
    """

    def test_the_same_close_on_the_same_day_gets_the_same_key(self):
        legs = [leg(), leg(symbol="SPY260908C00103000", side="short")]
        self.assertEqual(
            make_close_client_order_id(legs, 5, day="2026-08-25"),
            make_close_client_order_id(legs, 5, day="2026-08-25"),
        )

    def test_a_different_day_gets_a_different_key_so_tomorrow_can_retry(self):
        # Closes go out time_in_force="day"; an unfilled one is dead by the bell, so
        # a key that never changed would block the retry forever.
        legs = [leg()]
        self.assertNotEqual(
            make_close_client_order_id(legs, 5, day="2026-08-25"),
            make_close_client_order_id(legs, 5, day="2026-08-26"),
        )

    def test_a_different_quantity_gets_a_different_key(self):
        # A partial fill leaves a smaller closable qty; that remainder is a new order.
        legs = [leg()]
        self.assertNotEqual(
            make_close_client_order_id(legs, 5, day="2026-08-25"),
            make_close_client_order_id(legs, 3, day="2026-08-25"),
        )

    def test_different_symbols_get_different_keys(self):
        self.assertNotEqual(
            make_close_client_order_id([leg()], 5, day="2026-08-25"),
            make_close_client_order_id([leg(symbol="SPY260908C00103000")], 5, day="2026-08-25"),
        )

    def test_side_is_part_of_the_key_not_just_the_symbol(self):
        # Same contract, opposite direction -> opposite order. Colliding these would
        # let a buy_to_close suppress a sell_to_close of the same symbol.
        self.assertNotEqual(
            make_close_client_order_id([leg()], 5, day="2026-08-25"),
            make_close_client_order_id([leg(side="short")], 5, day="2026-08-25"),
        )

    def test_leg_order_is_part_of_the_key(self):
        # ExitPolicy always hands over legs sorted by symbol, so a differing order is a
        # differing call and must not silently reuse the key.
        a, b = leg(), leg(symbol="SPY260908C00103000", side="short")
        self.assertNotEqual(
            make_close_client_order_id([a, b], 5, day="2026-08-25"),
            make_close_client_order_id([b, a], 5, day="2026-08-25"),
        )

    def test_the_key_fits_the_api_limit_and_is_url_safe(self):
        key = make_close_client_order_id([leg()], 5, day="2026-08-25")
        self.assertLessEqual(len(key), CLIENT_ORDER_ID_MAX_LEN)  # maxLength 128
        self.assertRegex(key, r"^[A-Za-z0-9_.-]+$")
        self.assertIn("2026-08-25", key)  # readable in a journal without recomputing

    def test_defaulting_the_day_uses_today_utc(self):
        today = datetime.now(timezone.utc).date().isoformat()
        self.assertEqual(
            make_close_client_order_id([leg()], 5),
            make_close_client_order_id([leg()], 5, day=today),
        )

    def test_a_single_leg_close_carries_the_key_to_the_wire(self):
        client = real_client_with()
        asyncio.run(client.place_option_close_order([leg()], 3, "key-abc"))
        _, args = client._session.calls[0]
        self.assertEqual(args["client_order_id"], "key-abc")

    def test_an_mleg_close_carries_the_key_on_the_parent(self):
        client = real_client_with()
        legs = [leg(), leg(symbol="SPY260908C00103000", side="short")]
        asyncio.run(client.place_option_close_order(legs, 5, "key-xyz"))
        _, args = client._session.calls[0]
        self.assertEqual(args["client_order_id"], "key-xyz")
        self.assertEqual(args["order_class"], "mleg")
        for sent_leg in args["legs"]:
            self.assertNotIn("client_order_id", sent_leg)  # parent-level only

    def test_a_close_without_an_explicit_key_still_gets_one(self):
        # No caller can send an unkeyed close, however it reaches the client.
        client = real_client_with()
        asyncio.run(client.place_option_close_order([leg()], 3))
        _, args = client._session.calls[0]
        self.assertEqual(args["client_order_id"], make_close_client_order_id([leg()], 3))


class TestDuplicateCloseIsRefused(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "decisions.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_replaying_the_identical_close_is_rejected_not_duplicated(self):
        # The retry-after-timeout case: the position is still on the book (the first
        # response never arrived), so the exit policy would send the close again.
        async def go():
            async with MockAlpacaMCPClient() as client:
                legs = [leg()]
                first = await client.place_option_close_order(legs, 5)
                second = await client.place_option_close_order(legs, 5)
                return first, second, client._orders

        first, second, orders = asyncio.run(go())
        self.assertEqual(first["status"], "filled")
        self.assertNotIn("error", first)
        self.assertEqual(second["error"]["http_status"], 422)
        self.assertIn("client_order_id", second["error"]["detail"]["message"])
        self.assertEqual(len(orders), 1)  # the duplicate never became an order

    def test_a_smaller_remainder_is_allowed_through(self):
        async def go():
            async with MockAlpacaMCPClient() as client:
                await client.place_option_close_order([leg()], 5)
                return await client.place_option_close_order([leg()], 2)

        self.assertNotIn("error", asyncio.run(go()))

    def test_the_agent_journals_the_key_it_sent(self):
        async def go():
            journal = Journal(self.path)
            async with MockAlpacaMCPClient() as client:
                return await manage_exits(client, journal, ExitPolicy())

        records = asyncio.run(go())
        closes = [r for r in records if r["action"] == "exit_close"]
        self.assertEqual(len(closes), 3)
        for rec in closes:
            self.assertEqual(rec["client_order_id"], rec["order"]["client_order_id"])
            self.assertTrue(rec["client_order_id"])
            self.assertIs(rec["order_rejected"], False)
        # Three distinct structures -> three distinct keys, no cross-suppression.
        self.assertEqual(len({r["client_order_id"] for r in closes}), 3)
        for rec in records:
            if rec["action"] != "exit_close":
                self.assertIsNone(rec["client_order_id"])
                self.assertIsNone(rec["order_rejected"])

    def test_a_rejected_close_is_journalled_as_not_closed(self):
        # A book where the same structure is still open on the second pass -- the
        # position lookup is stubbed to keep returning it, as it would if the first
        # close timed out on the way back.
        async def go():
            journal = Journal(self.path)
            async with MockAlpacaMCPClient() as client:
                book = await client.get_all_positions()
                stuck = [p for p in book if p["underlying"] == "QQQ"]
                client.get_all_positions = lambda: _ready(stuck)
                first = await manage_exits(client, journal, ExitPolicy())
                second = await manage_exits(client, journal, ExitPolicy())
                return first, second, client._orders

        first, second, orders = asyncio.run(go())
        self.assertIs(first[0]["order_rejected"], False)
        self.assertIs(second[0]["order_rejected"], True)
        self.assertEqual(second[0]["client_order_id"], first[0]["client_order_id"])
        self.assertEqual(len(orders), 1)  # one order on the wire, not two


class TapeWritingMockClient(MockAlpacaMCPClient):
    """A mock whose filled closes land on the activities tape, the way a real one does.

    The stock mock returns a canned, frozen fill list, so reconciling before the
    exits and reconciling after them read the same two rows -- which makes the
    ordering this class exists to pin invisible in `--dry`. On a live account a
    close that fills IS an activity, and it is there the moment it fills. This is
    the smallest client that behaves that way: each leg of a filled close appends
    the FILL row that leg would have produced, priced at the position's own last
    price.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._fill_seq = 0

    async def place_option_close_order(self, legs, qty, client_order_id=None):
        order = await super().place_option_close_order(legs, qty, client_order_id)
        if order.get("error") or order.get("status") != "filled":
            return order
        for leg_row in legs:
            self._fill_seq += 1
            self._seed_fills = self._seed_fills + [{
                "activity_type": "FILL",
                "id": f"tape-close-{self._fill_seq}",
                "symbol": leg_row["symbol"],
                "side": "sell" if leg_row["side"] == "long" else "buy",
                "qty": str(qty),
                "price": f"{leg_row['current_price']:.2f}",
                "order_id": order["id"],
                "transaction_time": datetime.now(timezone.utc).isoformat(),
            }]
        return order


class UnfilledCloseMockClient(MockAlpacaMCPClient):
    """A mock whose closes come back accepted but not filled -- the normal live case.

    A market close is not instantaneous just because it is a market order: outside
    RTH, on a halted underlying, or simply in the milliseconds after submission, the
    order sits in `accepted` and the position is still on the book.
    """

    async def place_option_close_order(self, legs, qty, client_order_id=None):
        order = await super().place_option_close_order(legs, qty, client_order_id)
        if order.get("error"):
            return order
        order["status"] = "accepted"
        order.pop("filled_at", None)
        return order


def opening_buy(symbol, qty, price) -> dict:
    """The FILL row that opened a position, timestamped early today."""
    return {
        "activity_type": "FILL", "id": f"tape-open-{symbol}", "symbol": symbol,
        "side": "buy", "qty": str(qty), "price": f"{price:.2f}", "order_id": "tape-order-open",
        "transaction_time": datetime.now(timezone.utc).replace(
            hour=0, minute=1, second=0, microsecond=0).isoformat(),
    }


class TestExitLossReachesThisPassesBreaker(unittest.TestCase):
    """The loss an exit books on this pass must be in the breaker's number on this pass.

    Reconciliation used to run *before* `manage_exits`, so a stop loss that fired and
    filled at 09:30 was invisible to the breaker until the 09:31 pass -- and the entry
    decision in between was sized against a realized loss of $0. That is the exact
    shape of "the risk limit was respected on every individual order and exceeded by
    the account": each pass reads a number the pass itself has already invalidated.
    """

    LOSER = "SPY260908P00096000"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "decisions.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def book(self):
        # Long put, paid $3.00, now worth $0.50: -83% of cost, so the stop loss fires.
        # Closing 3 contracts at $0.50 realizes -(3.00 - 0.50) * 3 * 100 = -$750.
        return [wire_position(symbol=self.LOSER, side="long", qty="3",
                              entry="3.00", current="0.50")]

    def run_pass(self, client_cls=TapeWritingMockClient, max_daily_loss_pct=0.005):
        async def go():
            journal = Journal(self.path)
            async with client_cls(
                seed_positions=self.book(),
                seed_fills=[opening_buy(self.LOSER, 3, 3.00)],
            ) as client:
                strategy = MomentumRiskCapStrategy(max_daily_loss_pct=max_daily_loss_pct)
                record = await run_once(client, strategy, journal, "SPY", 30, ExitPolicy())
                return record, client._orders

        return asyncio.run(go())

    def test_the_exit_fills_and_its_loss_is_reconciled_in_the_same_pass(self):
        record, _ = self.run_pass()
        self.assertEqual(len(record["exits"]), 1)
        self.assertEqual(record["exits"][0]["action"], "exit_close")
        self.assertIn("stop loss", record["exits"][0]["reason"])
        self.assertEqual(len(record["closed_positions"]), 1)
        self.assertEqual(record["closed_positions"][0]["realized_pnl"], -750.0)

    def test_that_loss_stops_the_entry_on_the_same_pass(self):
        record, orders = self.run_pass()
        self.assertEqual(record["action"], "hold")
        self.assertIn("circuit breaker", record["reason"])
        self.assertIsNone(record["order"])
        self.assertEqual(record["max_loss"], 0.0)
        # One order on the wire this pass: the close. Nothing was opened on top of it.
        self.assertEqual(len(orders), 1)

    def test_control_the_same_pass_does_trade_when_the_loss_is_inside_the_budget(self):
        # Same book, same $750 exit loss, breaker at 3% of $100k = $3,000. The hold
        # above has to come from the budget being spent, not from the exit path
        # freezing the agent whenever it closes something.
        record, _ = self.run_pass(max_daily_loss_pct=0.03)
        self.assertEqual(len(record["closed_positions"]), 1)
        self.assertNotEqual(record["action"], "hold")
        self.assertIsNotNone(record["order"])


class TestUnconfirmedExitsStandTheEntryDown(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "decisions.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def run_pass(self, client_cls):
        async def go():
            journal = Journal(self.path)
            async with client_cls() as client:
                record = await run_once(client, MomentumRiskCapStrategy(), journal,
                                        "SPY", 30, ExitPolicy())
                return record, client._orders

        return asyncio.run(go())

    def test_a_close_that_is_only_accepted_blocks_the_entry(self):
        record, orders = self.run_pass(UnfilledCloseMockClient)
        self.assertEqual(record["action"], "hold")
        self.assertEqual(record["contracts"], 0)
        self.assertEqual(record["max_loss"], 0.0)
        self.assertIsNone(record["order"])
        self.assertEqual(len(record["blocked_by_exits"]), 3)  # the mock book has 3 closes
        for note in record["blocked_by_exits"]:
            self.assertIn("'accepted', not filled", note)
        self.assertIn("not confirmed filled", record["reason"])
        # The entry signal is still on the record: standing down is not the same as
        # not having seen anything.
        self.assertIn("Entry signal was:", record["reason"])
        self.assertEqual(len(orders), 3)  # three closes, and no opening order

    def test_the_dry_book_where_every_close_fills_is_not_blocked(self):
        # The `--dry` demo path: all three closes come back filled, so the entry side
        # is free to act. If this ever starts blocking, the demo is dead.
        record, _ = self.run_pass(MockAlpacaMCPClient)
        self.assertEqual(record["blocked_by_exits"], [])
        self.assertNotEqual(record["action"], "hold")
        self.assertIsNotNone(record["order"])

    def test_exits_still_go_out_while_the_entry_is_blocked(self):
        # Blocking new risk must never block taking risk off -- otherwise one stuck
        # close freezes the whole book.
        record, _ = self.run_pass(UnfilledCloseMockClient)
        self.assertEqual(len([r for r in record["exits"] if r["action"] == "exit_close"]), 3)


class TestUnconfirmedCloses(unittest.TestCase):
    def test_only_a_filled_close_counts_as_confirmed(self):
        self.assertEqual(unconfirmed_closes([
            {"action": "exit_close", "structure": "A", "order": {"status": "filled"}},
        ]), [])

    def test_working_refused_and_missing_orders_are_all_unconfirmed(self):
        notes = unconfirmed_closes([
            {"action": "exit_close", "structure": "A", "order": {"status": "accepted"}},
            {"action": "exit_close", "structure": "B", "order": {"status": "partially_filled"}},
            {"action": "exit_close", "structure": "C", "order": {"error": {"message": "boom"}}},
            {"action": "exit_close", "structure": "D", "order": None},
            {"action": "exit_close", "structure": "E", "order": {}},
        ])
        self.assertEqual(len(notes), 5)
        self.assertIn("A: close is 'accepted', not filled", notes)
        self.assertIn("B: close is 'partially_filled', not filled", notes)
        self.assertIn("C: close was refused (boom)", notes)
        self.assertIn("D: close decided but no order response came back", notes)
        self.assertIn("E: close is 'unknown', not filled", notes)

    def test_holds_and_skips_are_not_pending_closes(self):
        self.assertEqual(unconfirmed_closes([
            {"action": "exit_hold", "structure": "A", "order": None},
            {"action": "exit_skip", "structure": "B", "order": None},
        ]), [])


async def _ready(value):
    return value

if __name__ == "__main__":
    unittest.main()


class TestUncoveredShorts(unittest.TestCase):
    """`close_qty` closes the *common* part, which is exactly the covered part.

    An untouched vertical has equal legs, so the two coincide and nothing here fires.
    They come apart when the short leg is the larger one -- a partially filled entry,
    or a long leg closed by hand -- and then closing the common part takes the whole
    cap off and leaves the excess short standing alone, unbounded. The exit path is
    the one place that is supposed to be *removing* risk, so it buys that excess back
    before it consults any rule about whether the structure has run its course.
    """

    def setUp(self):
        self.policy = ExitPolicy()

    def vertical(self, long_qty=5.0, short_qty=5.0, expiry="2026-09-08", extra=None,
                 short_available=None, kind="call"):
        long_strike, short_strike = (100.0, 103.0) if kind == "call" else (103.0, 100.0)
        letter = "C" if kind == "call" else "P"
        legs = [
            leg(symbol=f"SPY260908{letter}00{int(long_strike)}000", kind=kind,
                strike=long_strike, qty=long_qty, cost_basis=200.0 * long_qty),
            leg(symbol=f"SPY260908{letter}00{int(short_strike)}000", kind=kind,
                strike=short_strike, side="short", qty=short_qty,
                available=short_available, cost_basis=-80.0 * short_qty),
        ] + (extra or [])
        return {
            "id": f"SPY {expiry} {kind}", "underlying": "SPY", "expiry": expiry,
            "type": kind, "legs": legs,
            "cost_basis": round(sum(l["cost_basis"] for l in legs), 2),
            "unrealized_pl": round(sum(l["unrealized_pl"] for l in legs), 2),
        }

    def test_a_balanced_vertical_has_nothing_uncovered(self):
        self.assertEqual(uncovered_short_contracts(self.vertical()["legs"]), 0)

    def test_a_long_heavy_book_has_nothing_uncovered(self):
        # The excess is long. Its floor is zero; nothing to buy back.
        legs = self.vertical(long_qty=5.0, short_qty=2.0)["legs"]
        self.assertEqual(uncovered_short_contracts(legs), 0)

    def test_the_short_excess_is_counted(self):
        legs = self.vertical(long_qty=2.0, short_qty=5.0)["legs"]
        self.assertEqual(uncovered_short_contracts(legs), 3)

    def test_a_credit_vertical_is_not_treated_as_naked(self):
        # Long 103 / short 100 calls: capped at the strike width, not at cost basis.
        # `portfolio` refuses to *price* it; that is not a reason to unwind a leg of it.
        legs = [leg(symbol="SPY260908C00103000", strike=103.0, qty=5.0, cost_basis=400.0),
                leg(symbol="SPY260908C00100000", strike=100.0, side="short", qty=5.0,
                    cost_basis=-1000.0)]
        self.assertEqual(uncovered_short_contracts(legs), 0)

    def test_the_call_short_bought_back_first_is_the_lowest_strike(self):
        legs = [leg(symbol="SPY260908C00104000", strike=104.0, side="short"),
                leg(symbol="SPY260908C00107000", strike=107.0, side="short")]
        self.assertEqual(most_exposed_short(legs)["strike"], 104.0)

    def test_the_put_short_bought_back_first_is_the_highest_strike(self):
        legs = [leg(symbol="SPY260908P00104000", kind="put", strike=104.0, side="short"),
                leg(symbol="SPY260908P00097000", kind="put", strike=97.0, side="short")]
        self.assertEqual(most_exposed_short(legs)["strike"], 104.0)

    def test_an_all_long_group_has_no_short_to_buy_back(self):
        self.assertIsNone(most_exposed_short([leg()]))

    def test_the_excess_short_is_bought_back_and_the_covered_part_is_left_alone(self):
        d = self.policy.evaluate(self.vertical(long_qty=2.0, short_qty=5.0), TODAY)
        self.assertEqual(d.action, "close")
        self.assertIn("naked short", d.reason)
        # Only the excess, only the short leg -- the remaining 2x2 is still a spread
        # and comes off through the normal path, as one multi-leg order.
        self.assertEqual(d.qty, 3)
        self.assertEqual([l["symbol"] for l in d.legs], ["SPY260908C00103000"])

    def test_that_close_is_what_the_old_close_qty_would_have_stranded(self):
        legs = self.vertical(long_qty=2.0, short_qty=5.0)["legs"]
        # The old behaviour: close min(leg qty) = 2 of each, leaving 0 long / 3 short.
        self.assertEqual(close_qty(legs), 2)
        self.assertEqual(uncovered_short_contracts(legs), 3)

    def test_it_fires_ahead_of_take_profit(self):
        # A structure at +90% still comes off the normal way -- but not before the
        # unbounded part is gone, and the journalled reason has to say which rule ran.
        structure = self.vertical(long_qty=2.0, short_qty=5.0)
        structure["cost_basis"] = 1000.0
        structure["unrealized_pl"] = 900.0
        d = self.policy.evaluate(structure, TODAY)
        self.assertIn("naked short", d.reason)
        self.assertNotIn("take profit", d.reason)

    def test_it_fires_ahead_of_the_time_stop(self):
        d = self.policy.evaluate(
            self.vertical(long_qty=2.0, short_qty=5.0, expiry="2026-08-26"), TODAY)
        self.assertEqual(d.action, "close")
        self.assertIn("naked short", d.reason)
        self.assertNotIn("time stop", d.reason)

    def test_a_working_close_on_the_short_leg_is_not_doubled_up(self):
        d = self.policy.evaluate(
            self.vertical(long_qty=2.0, short_qty=5.0, short_available=0.0), TODAY)
        self.assertEqual(d.action, "skip")
        self.assertIn("already working", d.reason)

    def test_an_excess_bigger_than_any_single_short_leg_stands_down(self):
        # 1 long vs 2 + 2 short: 3 uncovered, and no single leg holds 3. A close
        # order carries one quantity for all its legs, so buying back 2 and 1 is not
        # one order -- and sending 2 would leave the rest naked.
        extra = [leg(symbol="SPY260908C00107000", strike=107.0, side="short", qty=2.0,
                     cost_basis=-100.0)]
        d = self.policy.evaluate(
            self.vertical(long_qty=1.0, short_qty=2.0, extra=extra), TODAY)
        self.assertEqual(d.action, "skip")
        self.assertIn("one quantity per leg", d.reason)

    def test_an_unreadable_short_strike_stands_down(self):
        legs = [leg(qty=1.0),
                leg(symbol="SPY260908C00103000", side="short", qty=3.0, strike=None)]
        structure = dict(self.vertical(), legs=legs)
        d = self.policy.evaluate(structure, TODAY)
        self.assertEqual(d.action, "skip")
        self.assertIn("readable strike", d.reason)

    def test_the_covered_remainder_closes_normally_on_the_next_pass(self):
        # After the buy-back fills, the book is 2 long / 2 short and the ordinary
        # rules run again -- the time stop here -- as a single two-leg close.
        d = self.policy.evaluate(
            self.vertical(long_qty=2.0, short_qty=2.0, expiry="2026-08-26"), TODAY)
        self.assertEqual((d.action, d.qty, len(d.legs)), ("close", 2, 2))
        self.assertIn("time stop", d.reason)


class RecordingClient:
    """Just enough client for `manage_exits`: a fixed book, and closes recorded."""

    def __init__(self, positions):
        self.positions = positions
        self.closes = []

    async def get_all_positions(self):
        return self.positions

    async def get_option_chain(self, underlying):
        # Empty on purpose: these tests are about which order goes out, not about what
        # it crosses. An empty chain leaves every leg `unquoted`, which is the honest
        # answer here -- see tests/test_exit_crossing.py for the priced case.
        return []

    async def place_option_close_order(self, legs, qty, client_order_id=None):
        self.closes.append({"symbols": [l["symbol"] for l in legs], "qty": qty})
        return {"id": f"mock-{len(self.closes)}", "status": "filled", "qty": qty,
                "client_order_id": client_order_id}


class TestNakedShortEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "decisions.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_order_that_goes_out_is_the_buy_back_only(self):
        far = (TODAY + timedelta(days=30)).isoformat()
        book = [
            leg(symbol="SPY260925C00100000", strike=100.0, qty=2.0, expiry=far,
                cost_basis=400.0),
            leg(symbol="SPY260925C00103000", strike=103.0, side="short", qty=5.0,
                expiry=far, cost_basis=-400.0),
        ]
        client = RecordingClient(book)
        records = asyncio.run(manage_exits(client, Journal(self.path), ExitPolicy()))
        self.assertEqual(len(client.closes), 1)
        self.assertEqual(client.closes[0], {"symbols": ["SPY260925C00103000"], "qty": 3})
        self.assertEqual(records[0]["action"], "exit_close")
        self.assertEqual(records[0]["symbols"], ["SPY260925C00103000"])
        self.assertEqual(records[0]["qty"], 3)
        self.assertIn("naked short", records[0]["reason"])
