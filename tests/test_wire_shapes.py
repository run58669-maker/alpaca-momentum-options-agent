"""Tests for the alpaca-mcp-server v2 wire-shape adapters.

The fixtures here mirror the real response shapes, verified 2026-08-24 against the
upstream source of alpacahq/alpaca-mcp-server @ main (tree sha 803b07a3) -- see the
citations in src/mcp_client.py's module docstring. If these shapes are wrong the
agent silently holds forever on a live account, so they are pinned by tests.

Stdlib only:
    py -m unittest discover -s tests -v
"""

from __future__ import annotations

import re
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from mcp_client import (  # noqa: E402
    normalize_bars,
    normalize_chain,
    parse_occ_symbol,
    unwrap_payload,
)
from strategy import MomentumRiskCapStrategy  # noqa: E402

AS_OF = date(2026, 8, 24)


def snapshot(trade_price=None, bid=None, ask=None, delta=None, iv=None) -> dict:
    """One entry of the real `{"snapshots": {occ_symbol: ...}}` payload."""
    snap: dict = {}
    if trade_price is not None:
        snap["latestTrade"] = {"t": "2026-08-24T15:30:00Z", "p": trade_price, "s": 3}
    if bid is not None or ask is not None:
        snap["latestQuote"] = {"t": "2026-08-24T15:30:00Z", "bp": bid, "bs": 10, "ap": ask, "as": 12}
    if delta is not None:
        snap["greeks"] = {"delta": delta, "gamma": 0.03, "theta": -0.11, "vega": 0.08, "rho": 0.02}
    if iv is not None:
        snap["impliedVolatility"] = iv
    return snap


class TestParseOccSymbol(unittest.TestCase):
    def test_parses_a_call(self):
        self.assertEqual(
            parse_occ_symbol("SPY260910C00108700"),
            {"underlying": "SPY", "expiry": "2026-09-10", "type": "call", "strike": 108.7},
        )

    def test_parses_a_put_with_a_four_letter_root(self):
        parsed = parse_occ_symbol("AAPL250321P00150000")
        self.assertEqual(
            (parsed["underlying"], parsed["type"], parsed["strike"], parsed["expiry"]),
            ("AAPL", "put", 150.0, "2025-03-21"),
        )

    def test_strike_is_thousandths_not_dollars(self):
        # 00000750 is $0.75, not $750 -- getting this wrong misprices every pick.
        self.assertEqual(parse_occ_symbol("SPY260910C00000750")["strike"], 0.75)

    def test_rejects_non_occ_and_impossible_dates(self):
        bad_symbols = [
            "NOTASYMBOL",
            "",
            "SPY260910X00108700",  # neither C nor P
            "SPY261310C00108700",  # month 13
            "SPY260932C00108700",  # day 32
            "SPY26091C00108700",   # 5-digit date
            "SPY260910C0010870",   # 7-digit strike
        ]
        for bad in bad_symbols:
            with self.subTest(bad=bad):
                self.assertIsNone(parse_occ_symbol(bad))


class _Result:
    """Minimal CallToolResult stand-in."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Block:
    def __init__(self, text):
        self.text = text


class TestUnwrapPayload(unittest.TestCase):
    def test_plain_dict_passes_through(self):
        self.assertEqual(unwrap_payload({"a": 1}), {"a": 1})

    def test_peels_the_trust_boundary_envelope(self):
        wrapped = {"_alpaca_mcp_security": {"note": "untrusted"}, "data": {"snapshots": {}}}
        self.assertEqual(unwrap_payload(wrapped), {"snapshots": {}})

    def test_reads_structured_content(self):
        result = _Result(data=None, structuredContent={"x": 2}, content=[])
        self.assertEqual(unwrap_payload(result), {"x": 2})

    def test_falls_back_to_json_text_blocks(self):
        result = _Result(data=None, structuredContent=None, content=[_Block('{"y": 3}')])
        self.assertEqual(unwrap_payload(result), {"y": 3})

    def test_envelope_is_peeled_from_text_blocks_too(self):
        result = _Result(
            data=None,
            structuredContent=None,
            content=[_Block('{"_alpaca_mcp_security": 1, "data": {"z": 4}}')],
        )
        self.assertEqual(unwrap_payload(result), {"z": 4})

    def test_non_json_text_and_empty_content_do_not_raise(self):
        self.assertEqual(
            unwrap_payload(_Result(data=None, structuredContent=None, content=[_Block("boom")])),
            "boom",
        )
        self.assertIsNone(unwrap_payload(_Result(data=None, structuredContent=None, content=[])))


class TestNormalizeChain(unittest.TestCase):
    def test_derives_type_strike_expiry_from_the_symbol_key(self):
        # The real chain carries none of these as fields -- only as the OCC key.
        rows = normalize_chain({"snapshots": {"SPY260910C00108700": snapshot(trade_price=2.35)}})
        self.assertEqual(
            rows,
            [{"symbol": "SPY260910C00108700", "type": "call", "strike": 108.7,
              "expiry": "2026-09-10", "last_price": 2.35}],
        )

    def test_falls_back_to_quote_midpoint_when_there_is_no_trade(self):
        rows = normalize_chain({"snapshots": {"SPY260910P00105000": snapshot(bid=1.0, ask=1.4)}})
        self.assertEqual(rows[0]["last_price"], 1.2)

    def test_prefers_the_trade_price_over_the_quote(self):
        rows = normalize_chain(
            {"snapshots": {"SPY260910C00108700": snapshot(trade_price=2.35, bid=1.0, ask=1.4)}}
        )
        self.assertEqual(rows[0]["last_price"], 2.35)

    def test_drops_rows_that_cannot_be_priced_or_parsed(self):
        rows = normalize_chain({"snapshots": {
            "SPY260910C00108700": snapshot(trade_price=2.35),
            "SPY260910C00109000": snapshot(),                # no price at all
            "SPY260910C00109500": snapshot(trade_price=0),   # zero price
            "SPY260910C00110000": snapshot(bid=0, ask=1.4),  # half a quote
            "GARBAGE": snapshot(trade_price=1.0),            # not an OCC symbol
        }})
        self.assertEqual([r["symbol"] for r in rows], ["SPY260910C00108700"])

    def test_carries_delta_and_iv_when_present(self):
        rows = normalize_chain(
            {"snapshots": {"SPY260910C00108700": snapshot(trade_price=2.35, delta=0.52, iv=0.19)}}
        )
        self.assertEqual((rows[0]["delta"], rows[0]["iv"]), (0.52, 0.19))

    def test_delta_and_iv_are_absent_rather_than_none_when_missing(self):
        row = normalize_chain({"snapshots": {"SPY260910C00108700": snapshot(trade_price=2.35)}})[0]
        self.assertNotIn("delta", row)
        self.assertNotIn("iv", row)

    def test_output_is_sorted_by_symbol_not_dict_order(self):
        snaps = {s: snapshot(trade_price=1.0) for s in
                 ["SPY260910C00110000", "SPY260910C00105000", "SPY260910C00108000"]}
        self.assertEqual(
            [r["symbol"] for r in normalize_chain({"snapshots": snaps})],
            ["SPY260910C00105000", "SPY260910C00108000", "SPY260910C00110000"],
        )

    def test_missing_or_wrong_shaped_payload_is_empty_not_an_exception(self):
        for payload in [{}, {"snapshots": None}, {"snapshots": []}, None, [], "error string"]:
            with self.subTest(payload=payload):
                self.assertEqual(normalize_chain(payload), [])


class TestNormalizeBars(unittest.TestCase):
    RAW = {"bars": {"SPY": [
        {"t": "2026-08-20T04:00:00Z", "o": 99.0, "h": 101.0, "l": 98.5, "c": 100.0, "v": 1000},
        {"t": "2026-08-21T04:00:00Z", "o": 100.0, "h": 103.0, "l": 99.5, "c": 102.0, "v": 1200},
    ]}, "next_page_token": None}

    def test_unwraps_the_per_symbol_dict(self):
        self.assertEqual([b["c"] for b in normalize_bars(self.RAW, "SPY")], [100.0, 102.0])

    def test_symbol_is_matched_case_insensitively(self):
        self.assertEqual(len(normalize_bars(self.RAW, "spy")), 2)

    def test_a_symbol_that_is_not_in_the_payload_is_empty(self):
        self.assertEqual(normalize_bars(self.RAW, "QQQ"), [])

    def test_sorts_oldest_first_regardless_of_input_order(self):
        reversed_raw = {"bars": {"SPY": list(reversed(self.RAW["bars"]["SPY"]))}}
        self.assertEqual([b["c"] for b in normalize_bars(reversed_raw, "SPY")], [100.0, 102.0])

    def test_accepts_an_already_flat_list(self):
        self.assertEqual(len(normalize_bars(self.RAW["bars"]["SPY"], "SPY")), 2)

    def test_drops_rows_without_a_numeric_close(self):
        payload = {"bars": {"SPY": [{"t": "x"}, {"c": "not a number"}, "junk"]}}
        self.assertEqual(normalize_bars(payload, "SPY"), [])

    def test_wrong_shaped_payload_is_empty_not_an_exception(self):
        for payload in [None, "error string", {"bars": 7}]:
            with self.subTest(payload=payload):
                self.assertEqual(normalize_bars(payload, "SPY"), [])


class TestMockEmitsRealSymbols(unittest.TestCase):
    def test_every_mock_chain_symbol_is_a_valid_occ_symbol(self):
        import asyncio

        from mcp_client import MockAlpacaMCPClient

        async def go():
            async with MockAlpacaMCPClient() as c:
                return await c.get_option_chain("SPY")

        chain = asyncio.run(go())
        self.assertTrue(chain)
        for row in chain:
            parsed = parse_occ_symbol(row["symbol"])
            with self.subTest(symbol=row["symbol"]):
                self.assertIsNotNone(parsed, "mock emitted a symbol the real parser rejects")
                # The symbol must agree with the fields the mock reports alongside it.
                self.assertEqual(parsed["type"], row["type"])
                self.assertEqual(parsed["strike"], row["strike"])
                self.assertEqual(parsed["expiry"], row["expiry"])


class TestRealWireShapeReachesTheStrategy(unittest.TestCase):
    """The point of the adapters: a real-shaped chain must survive into a real pick."""

    def _raw_chain(self) -> dict:
        raw: dict = {"snapshots": {}}
        for dte, strike in [(3, 108.0), (14, 105.0), (14, 108.0), (14, 112.0), (45, 108.0)]:
            expiry = (AS_OF + timedelta(days=dte)).strftime("%y%m%d")
            raw["snapshots"][f"SPY{expiry}C{int(strike * 1000):08d}"] = snapshot(trade_price=2.0)
        return raw

    def test_normalized_chain_feeds_select_contract(self):
        chain = normalize_chain(self._raw_chain())
        self.assertEqual(len(chain), 5)

        picked, note = MomentumRiskCapStrategy().select_contract(chain, "call", spot=108.5, as_of=AS_OF)
        expected = f"SPY{(AS_OF + timedelta(days=14)).strftime('%y%m%d')}C00108000"
        self.assertEqual(picked["symbol"], expected)
        self.assertIn("14 days to expiry", note)
        # The 3d and 45d rows sit outside the 7-21d window, so only 3 candidates remain.
        self.assertIn("out of 3 candidate(s)", note)

    def test_a_decision_off_the_real_shape_names_the_contract_it_orders(self):
        chain = normalize_chain(self._raw_chain())
        bars = [{"c": c} for c in [100, 101, 102, 103, 104, 105, 106, 107, 108, 108.5]]
        decision = MomentumRiskCapStrategy().decide(
            symbol="SPY", bars=bars, option_chain=chain, equity=100_000.0,
            realized_loss_today=0.0, as_of=AS_OF,
        )
        self.assertEqual(decision.action, "buy_call")
        self.assertIn(decision.contract["symbol"], decision.reason)


class TestLivePreflightStaysHonest(unittest.TestCase):
    """scripts/preflight_live.py is the only proof the live wire shapes are real.

    It can only prove what it checks, so these guard the two ways it goes stale:
    an unpinned server (it would verify a different build than the one that runs)
    and a new `_call` site that never made it into the preflight's table.
    """

    def _preflight_calls(self):
        import importlib.util

        path = Path(__file__).parent.parent / "scripts" / "preflight_live.py"
        spec = importlib.util.spec_from_file_location("preflight_live", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.CALLS

    def test_the_server_version_is_pinned(self):
        from mcp_client import AlpacaMCPClient

        self.assertRegex(AlpacaMCPClient.SERVER_SPEC, r"^alpaca-mcp-server==\d+\.\d+\.\d+$")

    def test_the_preflight_covers_every_tool_the_client_calls(self):
        source = (Path(__file__).parent.parent / "src" / "mcp_client.py").read_text(
            encoding="utf-8"
        )
        # Only the real client reaches the network; the mock has no _call sites.
        live = source[source.index("class AlpacaMCPClient"):]
        called = set(re.findall(r'_call\(\s*"([a-z_]+)"', live))
        self.assertTrue(called, "no _call sites found -- the regex has drifted")
        self.assertEqual(called - set(self._preflight_calls()), set())


if __name__ == "__main__":
    unittest.main()
