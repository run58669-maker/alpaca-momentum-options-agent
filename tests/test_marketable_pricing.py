"""Tests for pricing an entry off the side of the market it actually trades against.

The rule under test: a leg being bought is priced at the ask, a leg being sold at the
bid, and `last_price` is used only when the chain carries no two-sided quote. What
this replaces was pricing everything off `last_price` -- the record of somebody
else's trade -- which made the limit the agent sends and the `max_loss` it journals
two different numbers from the one it would actually pay.

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
from strategy import MomentumRiskCapStrategy, marketable_price  # noqa: E402

AS_OF = date(2026, 8, 24)
SPOT = 100.0
EQUITY = 100_000.0


def contract(kind, strike, dte, last, bid=None, ask=None):
    expiry = (AS_OF + timedelta(days=dte)).isoformat()
    letter = "C" if kind == "call" else "P"
    row = {
        "symbol": f"X{expiry[2:].replace('-', '')}{letter}{int(strike * 1000):08d}",
        "type": kind, "strike": strike, "expiry": expiry, "last_price": last,
    }
    if bid is not None and ask is not None:
        row["bid"], row["ask"] = bid, ask
    return row


def bars(pct):
    """10 bars ending at SPOT with `pct` momentum across the window."""
    start = SPOT / (1 + pct)
    return [{"c": start + (SPOT - start) * i / 9} for i in range(10)]


class TestMarketablePrice(unittest.TestCase):
    def test_a_buyer_pays_the_ask_and_a_seller_gets_the_bid(self):
        row = contract("call", 100.0, 14, 4.50, 4.40, 4.60)
        self.assertEqual(marketable_price(row, "buy"), 4.60)
        self.assertEqual(marketable_price(row, "sell"), 4.40)

    def test_neither_side_is_the_last_trade(self):
        # The whole point: `last_price` sits between the two and is neither of them.
        row = contract("call", 100.0, 14, 4.50, 4.40, 4.60)
        self.assertNotEqual(marketable_price(row, "buy"), row["last_price"])
        self.assertNotEqual(marketable_price(row, "sell"), row["last_price"])

    def test_an_unquoted_row_falls_back_to_last(self):
        row = contract("call", 100.0, 14, 4.50)
        self.assertEqual(marketable_price(row, "buy"), 4.50)
        self.assertEqual(marketable_price(row, "sell"), 4.50)

    def test_a_one_sided_quote_never_reaches_this_function(self):
        # A zero bid is not half a market, it is no market -- and this function does not
        # re-check that, because `normalize_chain` already refuses to carry such a quote
        # onto the row. Pinned across the two modules so neither half can drop it alone.
        payload = {"snapshots": {"SPY260907C00100000": {
            "latestTrade": {"p": 4.50},
            "latestQuote": {"bp": 0.0, "ap": 4.60},
        }}}
        row = normalize_chain(payload)[0]
        self.assertNotIn("bid", row)
        self.assertEqual(marketable_price(row, "buy"), 4.50)

    def test_a_crossed_quote_is_not_a_quote(self):
        row = contract("call", 100.0, 14, 4.50, 4.80, 4.60)
        self.assertEqual(marketable_price(row, "buy"), 4.50)

    def test_a_row_with_no_usable_price_at_all_is_none(self):
        row = contract("call", 100.0, 14, 4.50)
        del row["last_price"]
        self.assertIsNone(marketable_price(row, "buy"))

    def test_an_unknown_side_raises_rather_than_guessing(self):
        row = contract("call", 100.0, 14, 4.50, 4.40, 4.60)
        with self.assertRaises(ValueError):
            marketable_price(row, "short")


class TestSpreadPricing(unittest.TestCase):
    """`select_spread` strikes the debit at long-ask minus short-bid."""

    def setUp(self):
        self.s = MomentumRiskCapStrategy(spread_width_pct=0.03, max_spread_pct=0.10)
        self.long = contract("call", 100.0, 14, 4.50, 4.40, 4.60)

    def test_the_debit_is_the_ask_minus_the_bid(self):
        short = contract("call", 103.0, 14, 3.00, 2.90, 3.10)
        picked, note, _ = self.s.select_spread([self.long, short], self.long, SPOT)
        self.assertIs(picked, short)
        # 4.60 - 2.90 = 1.70. Off last prices it would have read 1.50, and the order
        # would have gone out as a $1.50 limit into a $1.70 market.
        self.assertIn("$1.70 net debit", note)
        self.assertIn("capping max loss at $170.00 per spread", note)

    def test_a_pair_that_is_only_a_debit_at_last_prices_is_refused(self):
        # Last prices say this is a $0.05 debit. The market says the leg being sold
        # fetches no more than the leg being bought costs, so there is no debit to pay
        # and `net_debit < 0.01` throws it out instead of guessing at a credit.
        short = contract("call", 103.0, 14, 4.45, 4.60, 4.70)
        picked, note, n_refused = self.s.select_spread([self.long, short], self.long, SPOT)
        self.assertIsNone(picked)
        self.assertEqual(n_refused, 0)  # refused on price, not by the liquidity screen
        self.assertIn("no cheaper call strike", note)

    def test_an_unquoted_short_leg_is_still_priced_off_its_last(self):
        short = contract("call", 103.0, 14, 3.00)
        picked, note, _ = self.s.select_spread([self.long, short], self.long, SPOT)
        self.assertIs(picked, short)
        self.assertIn("$1.60 net debit", note)  # 4.60 ask - 3.00 last
        self.assertIn("unquoted (unscreened)", note)


class TestDecidePricesWhatItWillPay(unittest.TestCase):
    def setUp(self):
        self.s = MomentumRiskCapStrategy(spread_width_pct=0.03, max_spread_pct=0.10)

    def decide(self, chain, strategy=None):
        return (strategy or self.s).decide(
            "X", bars(0.05), chain, EQUITY, 0.0, as_of=AS_OF
        )

    def test_the_naked_long_limit_is_the_offer(self):
        naked = MomentumRiskCapStrategy(spread_width_pct=0.0, max_spread_pct=0.10)
        long = contract("call", 100.0, 14, 4.50, 4.40, 4.60)
        d = self.decide([long], strategy=naked)
        self.assertEqual(d.action, "buy_call")
        # `net_debit` is what `agent.py` hands `place_option_order` as `limit_price`.
        # At the old 4.50 the order sat under a 4.60 offer and could not fill.
        self.assertEqual(d.net_debit, 4.60)
        self.assertGreaterEqual(d.net_debit, long["ask"])

    def test_the_size_comes_down_when_the_offer_is_above_last(self):
        # $1,000 per-trade budget: at the $4.50 last it buys 2 contracts ($900), at the
        # $5.10 offer only 1 ($510). Sizing off last would have put 2 on at $1,020.
        naked = MomentumRiskCapStrategy(spread_width_pct=0.0, max_spread_pct=0.20)
        long = contract("call", 100.0, 14, 4.50, 4.50, 5.10)
        d = self.decide([long], strategy=naked)
        self.assertEqual(d.contracts, 1)
        self.assertEqual(d.max_loss, 510.0)
        self.assertLessEqual(d.max_loss, EQUITY * 0.01)

    def test_max_loss_is_the_debit_it_will_pay_times_the_size(self):
        long = contract("call", 100.0, 14, 4.50, 4.40, 4.60)
        short = contract("call", 103.0, 14, 3.00, 2.90, 3.10)
        d = self.decide([long, short])
        self.assertEqual(d.action, "buy_call_spread")
        self.assertEqual(d.net_debit, 1.70)
        self.assertEqual(d.max_loss, d.net_debit * 100 * d.contracts)

    def test_max_loss_carries_no_float_noise(self):
        # 4.52 x 100 x 2 is $904.00 in decimal and 903.9999999999999 in binary. A risk
        # cap is read by people; it does not get to be journalled like that.
        naked = MomentumRiskCapStrategy(spread_width_pct=0.0, max_spread_pct=0.10)
        long = contract("call", 100.0, 14, 4.50, 4.48, 4.52)
        d = self.decide([long], strategy=naked)
        self.assertEqual(d.max_loss, 904.0)
        self.assertEqual(repr(d.max_loss), "904.0")

    def test_the_reason_names_which_side_of_each_market_it_paid(self):
        long = contract("call", 100.0, 14, 4.50, 4.40, 4.60)
        short = contract("call", 103.0, 14, 3.00, 2.90, 3.10)
        d = self.decide([long, short])
        self.assertIn("Pricing: long leg at the offer $4.60 (last $4.50)", d.reason)
        self.assertIn("short leg at the bid $2.90 (last $3.00)", d.reason)

    def test_the_reason_says_so_when_a_leg_had_no_quote_to_price_off(self):
        naked = MomentumRiskCapStrategy(spread_width_pct=0.0, max_spread_pct=0.10)
        d = self.decide([contract("call", 100.0, 14, 4.50)], strategy=naked)
        self.assertIn("Pricing: long leg unquoted, priced off last $4.50", d.reason)


if __name__ == "__main__":
    unittest.main()
