"""The seeded demo book has to be quotable, not just decidable.

Every exit rule was already demonstrable under `--dry`: the mock's positions were
picked to land squarely on take-profit / stop-loss / time-stop / hold / skip. But they
were invented at arbitrary strikes and expiries, and the crossing-cost measurement
added on 2026-08-27 looks each closing leg up in the option chain **by OCC symbol**.
Nothing the mock held was ever in the mock's own chain, so every close reported
`no two-sided quote` and the one number the risk story turns on -- what the market
order crosses -- could not be shown at all.

These tests pin both halves at once: the book still lands on every rule, AND every leg
of it resolves to a two-sided quote. Either half alone is a demo that does not run.

Stdlib only:
    py -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from exits import ExitPolicy, closing_crossing_cost, group_structures  # noqa: E402
from mcp_client import MockAlpacaMCPClient  # noqa: E402


def book_and_chains():
    async def go():
        async with MockAlpacaMCPClient() as client:
            positions = await client.get_all_positions()
            chains = {
                symbol: await client.get_option_chain(symbol)
                for symbol in sorted({p["underlying"] for p in positions})
            }
            return positions, chains

    return asyncio.run(go())


def quotes_for(chains) -> dict[str, dict[str, float]]:
    return {
        row["symbol"]: {"bid": row["bid"], "ask": row["ask"]}
        for chain in chains.values()
        for row in chain
    }


class TestSeededBookIsQuotable(unittest.TestCase):
    def setUp(self):
        self.positions, self.chains = book_and_chains()
        self.quotes = quotes_for(self.chains)
        self.decisions = [
            (s, ExitPolicy().evaluate(s)) for s in group_structures(self.positions)
        ]

    def test_every_seeded_leg_is_a_row_of_the_mock_chain(self):
        missing = [p["symbol"] for p in self.positions if p["symbol"] not in self.quotes]
        self.assertEqual(missing, [], f"seeded legs absent from the mock chain: {missing}")

    def test_the_book_still_exercises_every_exit_rule(self):
        # Moving the book onto the chain grid must not cost the demo a rule. Rules are
        # identified by the sentence the policy writes, because that sentence is what a
        # reviewer watching `--dry` actually sees.
        reasons = " || ".join(d.reason for _, d in self.decisions)
        for phrase in ("take profit:", "stop loss:", "time stop:", "0 contracts available"):
            self.assertIn(phrase, reasons)
        self.assertIn("hold", {d.action for _, d in self.decisions})

    def test_every_close_the_policy_decides_gets_a_real_crossing_number(self):
        closes = [(s, d) for s, d in self.decisions if d.action == "close"]
        self.assertGreater(len(closes), 0)
        for structure, decision in closes:
            with self.subTest(structure=structure["id"]):
                crossing = closing_crossing_cost(decision.legs, decision.qty, self.quotes)
                self.assertEqual(crossing["unquoted"], [])
                self.assertIsNotNone(crossing["quoted_proceeds"])
                self.assertIsNotNone(crossing["crossing_cost"])
                self.assertIsNotNone(crossing["widest_leg_spread_pct"])

    def test_a_multi_leg_close_is_among_them(self):
        # A one-leg close proves the lookup works; it does not prove the arithmetic
        # nets a long sold into the bid against a short bought back at the ask. Only a
        # vertical does, so the demo book has to keep producing one.
        multi = [d for _, d in self.decisions if d.action == "close" and len(d.legs) > 1]
        self.assertTrue(multi, "no multi-leg close in the seeded book")
        crossing = closing_crossing_cost(multi[0].legs, multi[0].qty, self.quotes)
        self.assertGreater(crossing["crossing_cost"], 0.0)
        self.assertLess(crossing["quoted_proceeds"], crossing["mark_proceeds"])

    def test_entry_prices_are_back_solved_not_invented(self):
        # Each leg's mark is its chain mid; anything else means the demo's P&L is being
        # compared against a price no one was quoting.
        for position in self.positions:
            with self.subTest(symbol=position["symbol"]):
                quote = self.quotes[position["symbol"]]
                mid = round((quote["bid"] + quote["ask"]) / 2, 2)
                self.assertEqual(position["current_price"], mid)


if __name__ == "__main__":
    unittest.main()
