"""Tests for the naked-long fallback policy (NEXT.md item 4b, settled 2026-08-26).

The rule under test: when *every* candidate short leg is refused by the liquidity
screen, the pass stands down instead of buying the naked long. When the chain simply
offers no second leg -- nothing was refused -- the naked long still trades.

The distinction matters because the two look identical from the outside: both are
`select_spread` returning None. The count of refused legs is what tells them apart.

Stdlib only (unittest), no keys, no network:
    py -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from strategy import MomentumRiskCapStrategy  # noqa: E402

AS_OF = date(2026, 8, 24)
SPOT = 100.0
EQUITY = 100_000.0


def contract(kind, strike, dte, price, bid=None, ask=None):
    expiry = (AS_OF + timedelta(days=dte)).isoformat()
    letter = "C" if kind == "call" else "P"
    row = {
        "symbol": f"X{expiry[2:].replace('-', '')}{letter}{int(strike * 1000):08d}",
        "type": kind, "strike": strike, "expiry": expiry, "last_price": price,
    }
    if bid is not None and ask is not None:
        row["bid"], row["ask"] = bid, ask
    return row


def tight(kind, strike, dte, price):
    """A 1%-wide two-sided market around `price` -- passes any sane cap."""
    return contract(kind, strike, dte, price, round(price * 0.995, 2), round(price * 1.005, 2))


def wide(kind, strike, dte, price):
    """A 60%-wide market -- refused by the default 10% cap."""
    return contract(kind, strike, dte, price, round(price * 0.7, 2), round(price * 1.3, 2))


def bars(pct):
    """10 bars ending at SPOT with `pct` momentum across the window."""
    start = SPOT / (1 + pct)
    return [{"c": start + (SPOT - start) * i / 9} for i in range(10)]


LONG = tight("call", 100.0, 14, 4.50)


class TestRefusalCount(unittest.TestCase):
    """The third return value separates "none offered" from "all refused"."""

    def setUp(self):
        self.s = MomentumRiskCapStrategy(spread_width_pct=0.03, max_spread_pct=0.10)

    def test_all_candidates_refused_reports_the_count(self):
        chain = [LONG, wide("call", 103.0, 14, 3.00), wide("call", 108.0, 14, 2.00)]
        short, _, n_refused = self.s.select_spread(chain, LONG, spot=SPOT)
        self.assertIsNone(short)
        self.assertEqual(n_refused, 2)

    def test_a_chain_with_no_second_leg_refused_nothing(self):
        short, note, n_refused = self.s.select_spread([LONG], LONG, spot=SPOT)
        self.assertIsNone(short)
        self.assertEqual(n_refused, 0)
        self.assertIn("nothing was refused", note)

    def test_a_leg_that_is_not_a_debit_is_not_a_refusal(self):
        # More expensive than the long leg: not a debit vertical, so it never reached
        # the screen. Skipping it must not read as "the screen threw it out".
        chain = [LONG, tight("call", 103.0, 14, 5.50)]
        short, _, n_refused = self.s.select_spread(chain, LONG, spot=SPOT)
        self.assertIsNone(short)
        self.assertEqual(n_refused, 0)

    def test_a_successful_pick_still_reports_what_it_refused_on_the_way(self):
        chain = [LONG, wide("call", 103.0, 14, 3.00), tight("call", 104.0, 14, 2.90)]
        short, _, n_refused = self.s.select_spread(chain, LONG, spot=SPOT)
        self.assertEqual(short["strike"], 104.0)
        self.assertEqual(n_refused, 1)

    def test_screen_off_refuses_nothing_by_construction(self):
        off = MomentumRiskCapStrategy(spread_width_pct=0.03, max_spread_pct=0.0)
        chain = [LONG, wide("call", 103.0, 14, 3.00)]
        short, _, n_refused = off.select_spread(chain, LONG, spot=SPOT)
        self.assertEqual(short["strike"], 103.0)
        self.assertEqual(n_refused, 0)


class TestDecideStandsDownAfterARefusal(unittest.TestCase):
    def setUp(self):
        self.s = MomentumRiskCapStrategy(spread_width_pct=0.03, max_spread_pct=0.10)
        self.refused_chain = [
            LONG,
            wide("call", 103.0, 14, 3.00),
            wide("call", 108.0, 14, 2.00),
            wide("call", 115.0, 14, 1.00),
        ]

    def decide(self, chain, strategy=None):
        return (strategy or self.s).decide(
            "X", bars(0.05), chain, EQUITY, 0.0, as_of=AS_OF
        )

    def test_every_short_leg_refused_holds(self):
        d = self.decide(self.refused_chain)
        self.assertEqual(d.action, "hold")
        self.assertEqual(d.contracts, 0)
        self.assertEqual(d.max_loss, 0.0)

    def test_the_hold_reason_names_the_refusal_and_the_budget(self):
        d = self.decide(self.refused_chain)
        self.assertIn("every candidate short leg was refused", d.reason)
        self.assertIn("3 rejected wider than 10%", d.reason)
        self.assertIn("$1000.00 budget unhedged", d.reason)

    def test_the_hold_still_names_the_long_leg_it_would_have_bought(self):
        # The pass stood down, but the journal must still show what was on the table.
        d = self.decide(self.refused_chain)
        self.assertIsNotNone(d.contract)
        self.assertEqual(d.contract["strike"], 100.0)
        self.assertIsNone(d.short_contract)

    def test_a_chain_with_no_second_leg_still_buys_the_naked_long(self):
        d = self.decide([LONG])
        self.assertEqual(d.action, "buy_call")
        self.assertIsNone(d.short_contract)
        # 4.52, not the 4.50 last trade: buying the naked long means lifting the offer.
        self.assertEqual(d.net_debit, 4.52)
        self.assertGreater(d.contracts, 0)

    def test_spread_width_zero_still_buys_the_naked_long(self):
        # The operator asked for a naked long outright; no screen refusal happened,
        # so the policy must not silently disable that mode.
        naked = MomentumRiskCapStrategy(spread_width_pct=0.0, max_spread_pct=0.10)
        d = self.decide(self.refused_chain, strategy=naked)
        self.assertEqual(d.action, "buy_call")
        self.assertEqual(d.net_debit, 4.52)

    def test_one_tradable_leg_among_refused_ones_still_trades_the_spread(self):
        chain = self.refused_chain + [tight("call", 104.0, 14, 3.00)]
        d = self.decide(chain)
        self.assertEqual(d.action, "buy_call_spread")
        self.assertEqual(d.short_contract["strike"], 104.0)

    def test_the_put_side_stands_down_the_same_way(self):
        long_put = tight("put", 100.0, 14, 4.50)
        chain = [long_put, wide("put", 97.0, 14, 3.00), wide("put", 92.0, 14, 2.00)]
        d = self.s.decide("X", bars(-0.05), chain, EQUITY, 0.0, as_of=AS_OF)
        self.assertEqual(d.action, "hold")
        self.assertIn("2 rejected wider than 10%", d.reason)


class TestWhyNotTheOtherTwoOptions(unittest.TestCase):
    """The two rejected alternatives from NEXT.md 4b, pinned as measurements.

    Both sat written down as open options for days. They are recorded here as tests
    so the reasoning cannot quietly rot: one is a no-op, the other spends more.
    """

    def test_widening_the_target_width_never_rescues_a_refused_leg(self):
        # `spread_width_pct` ranks candidates, it does not gate them: every strike
        # beyond the long one is eligible at any width. So "widen and look again"
        # cannot find a leg the first look missed.
        chain = [LONG, wide("call", 103.0, 14, 3.00), wide("call", 115.0, 14, 1.00)]
        for width in (0.03, 0.08, 0.15, 0.50, 1.00):
            s = MomentumRiskCapStrategy(spread_width_pct=width, max_spread_pct=0.10)
            short, _, n_refused = s.select_spread(chain, LONG, spot=SPOT)
            self.assertIsNone(short, f"width {width} unexpectedly found a leg")
            self.assertEqual(n_refused, 2)

    def test_widening_only_re_ranks_when_legs_are_tradable(self):
        chain = [LONG, tight("call", 103.0, 14, 3.00), tight("call", 115.0, 14, 1.00)]
        picks = {}
        for width in (0.03, 0.15):
            s = MomentumRiskCapStrategy(spread_width_pct=width, max_spread_pct=0.10)
            short, _, _ = s.select_spread(chain, LONG, spot=SPOT)
            picks[width] = short["strike"]
        self.assertEqual(picks, {0.03: 103.0, 0.15: 115.0})

    def test_the_naked_fallback_would_have_spent_more_of_the_budget(self):
        # Why standing down is the answer rather than trading naked: same budget,
        # same signal, and the unhedged structure takes the larger dollar position.
        refused = [LONG, wide("call", 103.0, 14, 3.00)]
        tradable = [LONG, tight("call", 103.0, 14, 3.00)]
        naked_mode = MomentumRiskCapStrategy(spread_width_pct=0.0, max_spread_pct=0.10)
        naked = naked_mode.decide("X", bars(0.05), refused, EQUITY, 0.0, as_of=AS_OF)
        spread = MomentumRiskCapStrategy(
            spread_width_pct=0.03, max_spread_pct=0.10
        ).decide("X", bars(0.05), tradable, EQUITY, 0.0, as_of=AS_OF)
        budget = EQUITY * 0.01
        # Both sides moved when pricing went to the traded side of the book (the long
        # leg costs the offer, the short leg only fetches the bid), and the gap narrowed
        # from 90/75 to 90/77 -- the conclusion is the same, so it is the conclusion and
        # not the arithmetic that the policy rests on.
        self.assertEqual(naked.max_loss, 904.0)
        self.assertEqual(spread.max_loss, 770.0)
        self.assertGreater(naked.max_loss, spread.max_loss)
        # Both stay inside the budget -- the fallback was never a cap breach, which is
        # why this is a policy call and not a bug fix.
        self.assertLessEqual(naked.max_loss, budget)
        self.assertLessEqual(spread.max_loss, budget)


if __name__ == "__main__":
    unittest.main()
