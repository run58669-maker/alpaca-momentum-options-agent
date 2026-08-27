"""Tests for realized-P&L reconciliation: the circuit breaker's data feed.

No API keys, no network, stdlib only -- same as the rest of the suite.
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

from agent import reconcile_realized_pnl, run_once  # noqa: E402
from journal import Journal  # noqa: E402
from mcp_client import AlpacaMCPClient, AlpacaMCPError, MockAlpacaMCPClient, normalize_fills  # noqa: E402
from pnl import contract_multiplier, new_realized_events, realized_events  # noqa: E402
from strategy import MomentumRiskCapStrategy  # noqa: E402

OPT = "SPY260910C00108700"
OPT2 = "SPY260910C00110900"


def fill(activity_id, symbol, side, qty, price, when="2026-08-24T14:30:00Z", activity_type="FILL"):
    """One FILL activity in Alpaca's wire shape: qty and price are STRINGS."""
    return {
        "activity_type": activity_type,
        "id": activity_id,
        "symbol": symbol,
        "side": side,
        "qty": str(qty),
        "price": str(price),
        "transaction_time": when,
        "order_id": "order-" + activity_id,
        "type": "fill",
    }


class TestNormalizeFills(unittest.TestCase):
    def test_parses_the_wire_shape_and_coerces_the_string_numbers(self):
        rows = normalize_fills([fill("a", OPT, "buy", 5, "1.60")])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["qty"], 5.0)
        self.assertEqual(rows[0]["price"], 1.60)
        self.assertIsInstance(rows[0]["qty"], float)
        self.assertEqual(rows[0]["symbol"], OPT)

    def test_drops_activities_that_are_not_fills(self):
        payload = [
            fill("div", "SPY", "buy", 1, "0.50", activity_type="DIV"),
            fill("fee", "SPY", "sell", 1, "0.01", activity_type="FEE"),
            fill("real", OPT, "buy", 1, "1.00"),
        ]
        self.assertEqual([r["id"] for r in normalize_fills(payload)], ["real"])

    def test_drops_rows_that_cannot_be_priced_or_sided(self):
        payload = [
            fill("no-price", OPT, "buy", 1, "not-a-number"),
            fill("no-qty", OPT, "buy", "", "1.00"),
            fill("zero-qty", OPT, "buy", 0, "1.00"),
            fill("bad-side", OPT, "exercise", 1, "1.00"),
            fill("no-symbol", "", "buy", 1, "1.00"),
            fill("good", OPT, "buy", 1, "1.00"),
        ]
        self.assertEqual([r["id"] for r in normalize_fills(payload)], ["good"])

    def test_sorts_oldest_first_so_opens_are_seen_before_closes(self):
        payload = [
            fill("z", OPT, "sell", 1, "2.00", when="2026-08-24T15:00:00Z"),
            fill("a", OPT, "buy", 1, "1.00", when="2026-08-24T14:00:00Z"),
        ]
        self.assertEqual([r["id"] for r in normalize_fills(payload)], ["a", "z"])

    def test_non_list_payload_is_empty_not_an_exception(self):
        self.assertEqual(normalize_fills({"activities": []}), [])
        self.assertEqual(normalize_fills(None), [])


class TestContractMultiplier(unittest.TestCase):
    def test_option_symbol_is_a_hundred_shares(self):
        self.assertEqual(contract_multiplier(OPT), 100)

    def test_equity_symbol_is_one_share(self):
        self.assertEqual(contract_multiplier("SPY"), 1)


class TestRealizedEvents(unittest.TestCase):
    def test_long_round_trip_at_a_loss_uses_the_contract_multiplier(self):
        events = realized_events(normalize_fills([
            fill("open", OPT, "buy", 5, "1.60", when="2026-08-24T14:00:00Z"),
            fill("close", OPT, "sell", 5, "0.70", when="2026-08-24T15:00:00Z"),
        ]))
        self.assertEqual(len(events), 1)
        # (0.70 - 1.60) * 5 contracts * 100 shares = -450, not -4.50.
        self.assertEqual(events[0]["realized_pnl"], -450.0)
        self.assertEqual(events[0]["closing_activity_id"], "close")
        self.assertEqual(events[0]["qty"], 5.0)
        self.assertEqual(events[0]["multiplier"], 100)

    def test_short_leg_opened_by_a_sell_realizes_the_opposite_sign(self):
        # The short leg of a debit vertical is opened with a sell. Buying it back
        # cheaper is a PROFIT; treating the sell as a close would report a loss.
        events = realized_events(normalize_fills([
            fill("open", OPT2, "sell", 5, "1.00", when="2026-08-24T14:00:00Z"),
            fill("close", OPT2, "buy", 5, "0.40", when="2026-08-24T15:00:00Z"),
        ]))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["realized_pnl"], 300.0)
        self.assertEqual(events[0]["closing_side"], "buy")

    def test_fifo_matches_the_oldest_lot_first(self):
        events = realized_events(normalize_fills([
            fill("open1", OPT, "buy", 1, "1.00", when="2026-08-24T14:00:00Z"),
            fill("open2", OPT, "buy", 1, "3.00", when="2026-08-24T14:30:00Z"),
            fill("close", OPT, "sell", 1, "2.00", when="2026-08-24T15:00:00Z"),
        ]))
        # FIFO closes the $1.00 lot: +$100. LIFO would report -$100.
        self.assertEqual([e["realized_pnl"] for e in events], [100.0])

    def test_one_close_spanning_two_lots_is_one_event_summing_both(self):
        events = realized_events(normalize_fills([
            fill("open1", OPT, "buy", 2, "1.00", when="2026-08-24T14:00:00Z"),
            fill("open2", OPT, "buy", 3, "2.00", when="2026-08-24T14:30:00Z"),
            fill("close", OPT, "sell", 5, "1.50", when="2026-08-24T15:00:00Z"),
        ]))
        self.assertEqual(len(events), 1)
        # 2 * (1.50-1.00) * 100 = +100; 3 * (1.50-2.00) * 100 = -150.
        self.assertEqual(events[0]["realized_pnl"], -50.0)
        self.assertEqual(events[0]["qty"], 5.0)

    def test_partial_close_leaves_the_rest_open(self):
        events = realized_events(normalize_fills([
            fill("open", OPT, "buy", 5, "1.00", when="2026-08-24T14:00:00Z"),
            fill("close", OPT, "sell", 2, "0.50", when="2026-08-24T15:00:00Z"),
        ]))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["qty"], 2.0)
        self.assertEqual(events[0]["realized_pnl"], -100.0)

    def test_oversized_close_flips_the_remainder_into_a_new_position(self):
        events = realized_events(normalize_fills([
            fill("open", OPT, "buy", 2, "1.00", when="2026-08-24T14:00:00Z"),
            fill("flip", OPT, "sell", 5, "2.00", when="2026-08-24T15:00:00Z"),
            fill("cover", OPT, "buy", 3, "1.00", when="2026-08-24T16:00:00Z"),
        ]))
        # The flip closes 2 long (+$200) and opens 3 short; covering those 3 at
        # $1.00 after selling at $2.00 is another +$300.
        self.assertEqual([e["realized_pnl"] for e in events], [200.0, 300.0])
        self.assertEqual([e["qty"] for e in events], [2.0, 3.0])

    def test_symbols_never_match_against_each_other(self):
        events = realized_events(normalize_fills([
            fill("open", OPT, "buy", 1, "1.00", when="2026-08-24T14:00:00Z"),
            fill("other", OPT2, "sell", 1, "5.00", when="2026-08-24T15:00:00Z"),
        ]))
        # The long leg is still open and the short leg just opened: nothing realized.
        self.assertEqual(events, [])

    def test_opens_alone_realize_nothing(self):
        self.assertEqual(realized_events(normalize_fills([fill("a", OPT, "buy", 1, "1.00")])), [])


class TestNewRealizedEvents(unittest.TestCase):
    def fills(self):
        return normalize_fills([
            fill("open-old", OPT, "buy", 5, "1.60", when="2026-08-20T14:00:00Z"),
            fill("close-old", OPT, "sell", 5, "1.00", when="2026-08-21T15:00:00Z"),
            fill("open-new", OPT, "buy", 5, "2.00", when="2026-08-23T14:00:00Z"),
            fill("close-today", OPT, "sell", 5, "1.00", when="2026-08-24T15:00:00Z"),
        ])

    def test_only_todays_closes_count_against_todays_budget(self):
        events = new_realized_events(self.fills(), set(), date(2026, 8, 24))
        self.assertEqual([e["closing_activity_id"] for e in events], ["close-today"])
        self.assertEqual(events[0]["realized_pnl"], -500.0)

    def test_the_lookback_still_has_to_reach_the_opening_fill(self):
        # Same close, but the window starts after its open: the sell then looks like
        # a NEW short position and no loss is reported at all. This is why the
        # lookback is 30 days and not "today".
        truncated = [f for f in self.fills() if f["id"] in ("close-today",)]
        self.assertEqual(new_realized_events(truncated, set(), date(2026, 8, 24)), [])

    def test_already_journalled_closes_are_not_counted_twice(self):
        events = new_realized_events(self.fills(), {"close-today"}, date(2026, 8, 24))
        self.assertEqual(events, [])


class TestJournalIdempotency(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = Journal(Path(self.tmp.name) / "decisions.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def test_logged_closing_ids_reads_back_what_reconciliation_wrote(self):
        self.assertEqual(self.journal.logged_closing_ids(), set())
        self.journal.log(symbol=OPT, realized_pnl=-450.0, closing_activity_id="close-1")
        self.journal.log(symbol="SPY", action="hold", contracts=0)  # a decision, no id
        self.assertEqual(self.journal.logged_closing_ids(), {"close-1"})

    def test_realized_loss_today_counts_losses_and_ignores_gains(self):
        self.journal.log(symbol=OPT, realized_pnl=-450.0, closing_activity_id="a")
        self.journal.log(symbol=OPT, realized_pnl=300.0, closing_activity_id="b")
        self.assertEqual(self.journal.realized_loss_today(), 450.0)


class FakeSession:
    """Stands in for an MCP ClientSession, recording the args it was called with."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    async def call_tool(self, tool, args):
        self.calls.append((tool, args))
        return self.pages.pop(0) if self.pages else []


def real_client_with(pages):
    """A real AlpacaMCPClient without the env-var check, wired to a fake session."""
    client = object.__new__(AlpacaMCPClient)
    client._session = FakeSession(pages)
    return client


class TestRealClientGetFills(unittest.TestCase):
    def test_sends_the_verified_activities_shape(self):
        client = real_client_with([[fill("a", OPT, "buy", 1, "1.00")]])
        fills = asyncio.run(client.get_fills("2026-07-25"))
        tool, args = client._session.calls[0]
        self.assertEqual(tool, "get_account_activities_by_type")
        self.assertEqual(args["activity_type"], "FILL")
        self.assertEqual(args["after"], "2026-07-25")
        self.assertEqual(args["direction"], "asc")
        self.assertEqual(args["page_size"], 100)
        self.assertNotIn("page_token", args)  # only sent from page 2 on
        self.assertEqual(len(fills), 1)

    def test_pages_with_the_last_activity_id_until_a_short_page(self):
        full = [fill(f"f{i}", OPT, "buy", 1, "1.00") for i in range(100)]
        client = real_client_with([full, [fill("tail", OPT, "sell", 1, "2.00")]])
        fills = asyncio.run(client.get_fills("2026-07-25"))
        self.assertEqual(len(fills), 101)
        self.assertEqual(len(client._session.calls), 2)
        self.assertEqual(client._session.calls[1][1]["page_token"], "f99")

    def test_a_history_too_long_to_page_raises_instead_of_truncating(self):
        full = [fill(f"f{i}", OPT, "buy", 1, "1.00") for i in range(100)]
        client = real_client_with([list(full) for _ in range(AlpacaMCPClient.MAX_ACTIVITY_PAGES)])
        with self.assertRaises(AlpacaMCPError):
            asyncio.run(client.get_fills("2026-07-25"))

    def test_unwraps_the_trust_boundary_envelope(self):
        wrapped = {"_alpaca_mcp_security": "untrusted", "data": [fill("a", OPT, "buy", 1, "1.00")]}
        client = real_client_with([wrapped])
        self.assertEqual(len(asyncio.run(client.get_fills("2026-07-25"))), 1)


def today_at(hour, minute=0):
    return datetime.now(timezone.utc).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    ).isoformat()


class TestCircuitBreakerEndToEnd(unittest.TestCase):
    """The point of the whole change: the breaker can now actually fire."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "decisions.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def passes(self, seed_fills, n=1, max_daily_loss_pct=0.03):
        async def go():
            journal = Journal(self.path)
            strategy = MomentumRiskCapStrategy(max_daily_loss_pct=max_daily_loss_pct)
            async with MockAlpacaMCPClient(seed_fills=seed_fills) as client:
                return [await run_once(client, strategy, journal, "SPY") for _ in range(n)]

        return asyncio.run(go())

    def big_loss(self):
        # 50 contracts from $9.00 to $1.00 = -$40,000 on $100,000 of mock equity.
        return [
            fill("open", OPT, "buy", 50, "9.00", when=today_at(14)),
            fill("close", OPT, "sell", 50, "1.00", when=today_at(15)),
        ]

    def test_a_real_realized_loss_halts_new_trades(self):
        record = self.passes(self.big_loss())[0]
        self.assertEqual(record["action"], "hold")
        self.assertIn("circuit breaker", record["reason"])
        self.assertEqual(record["max_loss"], 0.0)

    def test_without_the_loss_the_same_pass_trades(self):
        record = self.passes([])[0]
        self.assertEqual(record["action"], "buy_call_spread")

    def test_the_loss_is_journalled_once_no_matter_how_many_passes(self):
        records = self.passes(self.big_loss(), n=3)
        on_disk = [json.loads(l) for l in self.path.read_text(encoding="utf-8").strip().splitlines()]
        realized = [r for r in on_disk if "realized_pnl" in r]
        self.assertEqual(len(realized), 1)
        self.assertEqual(realized[0]["realized_pnl"], -40000.0)
        # Reported to the caller only on the pass that discovered it.
        self.assertEqual([len(r["closed_positions"]) for r in records], [1, 0, 0])

    def test_a_close_from_a_previous_day_does_not_spend_todays_budget(self):
        stale = [
            fill("open", OPT, "buy", 50, "9.00", when="2026-08-20T14:00:00Z"),
            fill("close", OPT, "sell", 50, "1.00", when="2026-08-21T15:00:00Z"),
        ]
        record = self.passes(stale)[0]
        self.assertEqual(record["action"], "buy_call_spread")
        self.assertEqual(record["closed_positions"], [])

    def test_reconciliation_asks_for_a_window_that_covers_the_open(self):
        captured = {}

        class RecordingClient(MockAlpacaMCPClient):
            async def get_fills(self, after):
                captured["after"] = after
                return await super().get_fills(after)

        async def go():
            journal = Journal(self.path)
            async with RecordingClient() as client:
                await reconcile_realized_pnl(client, journal, lookback_days=30)

        asyncio.run(go())
        expected = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
        self.assertEqual(captured["after"], expected)


if __name__ == "__main__":
    unittest.main()
