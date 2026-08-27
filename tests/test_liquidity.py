"""Tests for the bid-ask liquidity screen: the quote fields the chain carries,
`relative_spread`, `screen_liquidity`, and the effect of the screen on both legs.

The point of the screen is that it must be able to *change the pick*, not merely
prune candidates that were losing anyway -- several tests below assert the pick
flips when the screen is turned on and flips back when it is turned off.

Stdlib only (unittest), no keys, no network:
    py -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from mcp_client import normalize_chain  # noqa: E402
from strategy import MomentumRiskCapStrategy, relative_spread  # noqa: E402

AS_OF = date(2026, 8, 24)


def contract(kind: str, strike: float, dte: int, price: float,
             bid: float | None = None, ask: float | None = None) -> dict:
    """One chain row. Omitting bid/ask makes it an unquoted row, like a snapshot
    that carried a trade but no two-sided quote."""
    expiry = (AS_OF + timedelta(days=dte)).isoformat()
    letter = "C" if kind == "call" else "P"
    row = {
        "symbol": f"X{expiry[2:].replace('-', '')}{letter}{int(strike * 1000):08d}",
        "type": kind,
        "strike": strike,
        "expiry": expiry,
        "last_price": price,
    }
    if bid is not None and ask is not None:
        row["bid"], row["ask"] = bid, ask
    return row


def tight(kind: str, strike: float, dte: int, price: float) -> dict:
    """A row quoting a penny either side of `price` -- tight at any sane premium."""
    return contract(kind, strike, dte, price, round(price - 0.01, 2), round(price + 0.01, 2))


def wide(kind: str, strike: float, dte: int, price: float) -> dict:
    """A row quoting +/-40% of its price: the stale far-OTM market the screen exists for."""
    return contract(kind, strike, dte, price, round(price * 0.6, 2), round(price * 1.4, 2))


class TestChainCarriesTheQuote(unittest.TestCase):
    """`normalize_chain` must pass `latestQuote` bp/ap through, or nothing can be screened."""

    SYMBOL = "SPY260911C00108000"

    def snapshot(self, quote: dict | None) -> dict:
        snap = {"latestTrade": {"p": 1.50}}
        if quote is not None:
            snap["latestQuote"] = quote
        return {"snapshots": {self.SYMBOL: snap}}

    def test_carries_bid_and_ask_from_the_latest_quote(self):
        rows = normalize_chain(self.snapshot({"bp": 1.48, "ap": 1.52}))
        self.assertEqual(rows[0]["bid"], 1.48)
        self.assertEqual(rows[0]["ask"], 1.52)

    def test_a_one_sided_quote_carries_neither_side(self):
        # Half a market is not a market: carrying only the bid would let a caller
        # compute a "width" against a number that is not the other side.
        rows = normalize_chain(self.snapshot({"bp": 1.48}))
        self.assertNotIn("bid", rows[0])
        self.assertNotIn("ask", rows[0])

    def test_a_crossed_quote_is_dropped(self):
        rows = normalize_chain(self.snapshot({"bp": 1.60, "ap": 1.40}))
        self.assertNotIn("bid", rows[0])

    def test_a_zero_bid_is_dropped(self):
        rows = normalize_chain(self.snapshot({"bp": 0, "ap": 0.05}))
        self.assertNotIn("bid", rows[0])

    def test_quote_keys_are_absent_rather_than_none_when_there_is_no_quote(self):
        # Absent, not None: `relative_spread` distinguishes "no quote" from "bad quote",
        # and a None sitting in the row would make both look the same to a reader.
        rows = normalize_chain(self.snapshot(None))
        self.assertNotIn("bid", rows[0])
        self.assertNotIn("ask", rows[0])

    def test_the_row_is_still_priced_when_the_quote_is_unusable(self):
        rows = normalize_chain(self.snapshot({"bp": 1.60, "ap": 1.40}))
        self.assertEqual(rows[0]["last_price"], 1.50)


class TestRelativeSpread(unittest.TestCase):
    def test_width_is_measured_against_the_midpoint(self):
        self.assertAlmostEqual(relative_spread({"bid": 0.90, "ask": 1.10}), 0.20)

    def test_a_penny_market_is_tight_on_a_big_premium_and_ruinous_on_a_small_one(self):
        self.assertAlmostEqual(relative_spread({"bid": 5.99, "ask": 6.01}), 0.02 / 6.0)
        self.assertAlmostEqual(relative_spread({"bid": 0.05, "ask": 0.07}), 0.02 / 0.06)

    def test_an_unquoted_row_is_none_not_zero(self):
        self.assertIsNone(relative_spread({"last_price": 1.0}))

    def test_a_crossed_or_unparseable_quote_is_none(self):
        self.assertIsNone(relative_spread({"bid": 1.10, "ask": 0.90}))
        self.assertIsNone(relative_spread({"bid": "n/a", "ask": 1.0}))
        self.assertIsNone(relative_spread({"bid": 0.0, "ask": 0.0}))


class TestScreenLiquidity(unittest.TestCase):
    def setUp(self):
        self.s = MomentumRiskCapStrategy(max_spread_pct=0.10)

    def test_a_wide_market_is_rejected_and_a_tight_one_kept(self):
        kept, rejected, unquoted = self.s.screen_liquidity(
            [tight("call", 100.0, 14, 5.00), wide("call", 110.0, 14, 0.50)]
        )
        self.assertEqual([c["strike"] for c in kept], [100.0])
        self.assertEqual((rejected, unquoted), (1, 0))

    def test_exactly_at_the_cap_passes(self):
        # The cap is a limit, not a strict inequality: 10.0% wide is not "wider than 10%".
        at_cap = contract("call", 100.0, 14, 1.00, 0.95, 1.05)  # 0.10 / 1.00 = 10%
        kept, rejected, _ = self.s.screen_liquidity([at_cap])
        self.assertEqual(len(kept), 1)
        self.assertEqual(rejected, 0)

    def test_an_unquoted_row_is_kept_but_counted_separately(self):
        # Missing data is not evidence of a wide market. Dropping it would make the
        # agent refuse to trade at all on any feed that does not carry quotes.
        kept, rejected, unquoted = self.s.screen_liquidity([contract("call", 100.0, 14, 5.00)])
        self.assertEqual(len(kept), 1)
        self.assertEqual((rejected, unquoted), (0, 1))

    def test_zero_cap_disables_the_screen(self):
        s = MomentumRiskCapStrategy(max_spread_pct=0.0)
        kept, rejected, unquoted = s.screen_liquidity([wide("call", 110.0, 14, 0.50)])
        self.assertEqual(len(kept), 1)
        self.assertEqual((rejected, unquoted), (0, 0))


class TestScreenChangesTheLongLeg(unittest.TestCase):
    """The screen must outrank nearness-to-the-money, not just tidy up behind it."""

    def setUp(self):
        self.s = MomentumRiskCapStrategy(max_spread_pct=0.10)
        # 100.5 is nearest spot and would always win on distance -- but it is stale.
        self.chain = [
            wide("call", 100.5, 14, 4.00),
            tight("call", 102.0, 14, 3.00),
        ]

    def test_the_nearest_strike_loses_when_its_market_is_wide(self):
        picked, note = self.s.select_contract(self.chain, "call", spot=100.0, as_of=AS_OF)
        self.assertEqual(picked["strike"], 102.0)
        self.assertIn("1 rejected as too wide", note)

    def test_turning_the_screen_off_puts_the_nearest_strike_back(self):
        # Proves the flip above is the screen's doing and not some other ordering change.
        off = MomentumRiskCapStrategy(max_spread_pct=0.0)
        picked, note = off.select_contract(self.chain, "call", spot=100.0, as_of=AS_OF)
        self.assertEqual(picked["strike"], 100.5)
        self.assertIn("liquidity screen off", note)

    def test_the_note_states_the_chosen_contracts_own_market(self):
        _, note = self.s.select_contract(self.chain, "call", spot=100.0, as_of=AS_OF)
        self.assertIn("$2.99/$3.01", note)
        self.assertIn("0.7% wide vs the 10% cap", note)

    def test_a_chain_that_is_wide_everywhere_holds_instead_of_buying(self):
        chain = [wide("call", 100.5, 14, 4.00), wide("call", 102.0, 14, 3.00)]
        picked, note = self.s.select_contract(chain, "call", spot=100.0, as_of=AS_OF)
        self.assertIsNone(picked)
        self.assertIn("nothing liquid enough to buy", note)
        self.assertIn("all 2 call contract(s)", note)

    def test_an_unquoted_chain_still_trades_and_says_it_was_unscreened(self):
        chain = [contract("call", 100.5, 14, 4.00), contract("call", 102.0, 14, 3.00)]
        picked, note = self.s.select_contract(chain, "call", spot=100.0, as_of=AS_OF)
        self.assertEqual(picked["strike"], 100.5)
        # The pick's *own* market has to be called out, not just the tally of unquoted
        # candidates -- otherwise "2 unquoted" reads as if the winner had been measured.
        self.assertIn("its own market is unquoted (unscreened)", note)
        self.assertIn("2 unquoted and left unscreened", note)

    def test_the_screen_runs_after_the_expiry_window_not_before(self):
        # A tight contract outside the window must not rescue a chain whose in-window
        # rows are all wide -- that would trade an expiry the strategy rejected.
        chain = [wide("call", 100.5, 14, 4.00), tight("call", 100.5, 60, 6.00)]
        picked, note = self.s.select_contract(chain, "call", spot=100.0, as_of=AS_OF)
        self.assertIsNone(picked)
        self.assertIn("nothing liquid enough", note)


class TestScreenChangesTheShortLeg(unittest.TestCase):
    """The short leg is a position that must be bought back, so a wide market on it
    is worse than a wide market on the long leg -- and the far-OTM strikes the width
    target favours are exactly where quotes go wide."""

    def setUp(self):
        self.s = MomentumRiskCapStrategy(spread_width_pct=0.03, max_spread_pct=0.10)
        self.long_leg = tight("call", 100.0, 14, 5.00)
        self.chain = [
            self.long_leg,
            wide("call", 103.0, 14, 3.10),   # exactly the $3 target width, but stale
            tight("call", 104.0, 14, 2.80),  # $4 wide -- second best on width, tradable
        ]

    def test_the_target_width_strike_loses_when_its_market_is_wide(self):
        short, note, _ = self.s.select_spread(self.chain, self.long_leg, spot=100.0)
        self.assertEqual(short["strike"], 104.0)
        self.assertIn("1 wider strike(s) rejected on spread", note)

    def test_turning_the_screen_off_puts_the_stale_strike_back(self):
        off = MomentumRiskCapStrategy(spread_width_pct=0.03, max_spread_pct=0.0)
        short, _, _ = off.select_spread(self.chain, self.long_leg, spot=100.0)
        self.assertEqual(short["strike"], 103.0)

    def test_the_note_reports_the_short_legs_own_width(self):
        _, note, _ = self.s.select_spread(self.chain, self.long_leg, spot=100.0)
        self.assertIn("short leg quotes 0.7% wide", note)

    def test_no_tradable_short_leg_reports_the_refusal_count_not_a_naked_fallback(self):
        # Changed 2026-08-26 with the fallback policy: a screen refusal no longer
        # promises the naked long, it reports how many legs it refused so `decide`
        # can stand the pass down. See tests/test_naked_fallback_policy.py.
        chain = [self.long_leg, wide("call", 103.0, 14, 3.10)]
        short, note, n_refused = self.s.select_spread(chain, self.long_leg, spot=100.0)
        self.assertIsNone(short)
        self.assertEqual(n_refused, 1)
        self.assertIn("1 rejected on a bid-ask spread wider than 10%", note)
        self.assertNotIn("max loss = full premium", note)

    def test_an_unquoted_short_leg_is_sold_and_flagged_unscreened(self):
        chain = [self.long_leg, contract("call", 103.0, 14, 3.10)]
        short, note, _ = self.s.select_spread(chain, self.long_leg, spot=100.0)
        self.assertEqual(short["strike"], 103.0)
        self.assertIn("short leg is unquoted (unscreened)", note)

    def test_a_put_spread_screens_the_lower_strike_the_same_way(self):
        long_put = tight("put", 100.0, 14, 5.00)
        chain = [long_put, wide("put", 97.0, 14, 3.10), tight("put", 96.0, 14, 2.80)]
        short, _, _ = self.s.select_spread(chain, long_put, spot=100.0)
        self.assertEqual(short["strike"], 96.0)


class TestDecideEndToEnd(unittest.TestCase):
    def test_the_decision_orders_the_liquid_legs_its_reason_names(self):
        s = MomentumRiskCapStrategy(spread_width_pct=0.03, max_spread_pct=0.10)
        chain = [
            wide("call", 100.5, 14, 4.00),   # nearest spot, stale -> must not be bought
            tight("call", 102.0, 14, 3.00),  # the long leg the screen leaves standing
            wide("call", 105.0, 14, 1.60),   # target width from 102, stale -> not sold
            tight("call", 106.0, 14, 1.40),  # the short leg the screen leaves standing
        ]
        d = s.decide(
            symbol="X",
            bars=[{"c": c} for c in [90.0] * 9 + [100.0]],  # +11% momentum, bullish
            option_chain=chain,
            equity=100_000.0,
            realized_loss_today=0.0,
            as_of=AS_OF,
        )
        self.assertEqual(d.action, "buy_call_spread")
        self.assertEqual(d.contract["strike"], 102.0)
        self.assertEqual(d.short_contract["strike"], 106.0)
        self.assertIn(d.contract["symbol"], d.reason)
        self.assertIn(d.short_contract["symbol"], d.reason)
        self.assertIn("rejected as too wide", d.reason)

    def test_a_wide_chain_holds_and_takes_no_risk(self):
        s = MomentumRiskCapStrategy(max_spread_pct=0.10)
        d = s.decide(
            symbol="X",
            bars=[{"c": c} for c in [90.0] * 9 + [100.0]],
            option_chain=[wide("call", 100.5, 14, 4.00), wide("call", 102.0, 14, 3.00)],
            equity=100_000.0,
            realized_loss_today=0.0,
            as_of=AS_OF,
        )
        self.assertEqual(d.action, "hold")
        self.assertEqual(d.max_loss, 0.0)
        self.assertIn("nothing liquid enough to buy", d.reason)


class TestMockChainIsQuoted(unittest.TestCase):
    """The `--dry` chain has to carry quotes, or the screen is invisible in the demo."""

    def test_every_mock_row_has_a_usable_two_sided_quote(self):
        import asyncio

        from mcp_client import MockAlpacaMCPClient

        chain = asyncio.run(MockAlpacaMCPClient().get_option_chain("SPY"))
        self.assertTrue(chain)
        for row in chain:
            width = relative_spread(row)
            self.assertIsNotNone(width, row["symbol"])
            self.assertGreaterEqual(row["ask"], row["bid"])
            self.assertGreater(row["bid"], 0)

    def test_cheap_far_otm_rows_come_out_proportionally_wider_than_atm_rows(self):
        # Not decoration: this is the shape that makes the screen do anything at all.
        import asyncio

        from mcp_client import MockAlpacaMCPClient

        chain = asyncio.run(MockAlpacaMCPClient().get_option_chain("SPY"))
        cheapest = min(chain, key=lambda r: r["last_price"])
        dearest = max(chain, key=lambda r: r["last_price"])
        self.assertGreater(relative_spread(cheapest), relative_spread(dearest))

    def test_the_default_screen_rejects_some_but_not_all_of_the_mock_chain(self):
        import asyncio

        from mcp_client import MockAlpacaMCPClient

        chain = asyncio.run(MockAlpacaMCPClient().get_option_chain("SPY"))
        kept, rejected, unquoted = MomentumRiskCapStrategy().screen_liquidity(chain)
        self.assertGreater(rejected, 0)
        self.assertGreater(len(kept), 0)
        self.assertEqual(unquoted, 0)


if __name__ == "__main__":
    unittest.main()
