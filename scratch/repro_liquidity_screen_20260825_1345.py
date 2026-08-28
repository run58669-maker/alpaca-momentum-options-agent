"""Show the liquidity screen changing a pick, on a hand-built chain and on the mock one.

Two runs of the same chain through the same strategy, differing only in
--max-spread-pct. Anything that differs between them is the screen's doing.

The constructed chain is labelled as constructed: it puts a stale market on the strike
that wins on every other criterion, which is the case the screen exists for. The mock
chain is printed too, unmodified, to show what the screen does on the shipped --dry
data -- which is less than you might hope, and is reported honestly rather than tuned.
"""
import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from mcp_client import MockAlpacaMCPClient  # noqa: E402
from strategy import MomentumRiskCapStrategy, relative_spread  # noqa: E402

AS_OF = date(2026, 8, 24)


def row(kind, strike, dte, price, bid, ask):
    expiry = (AS_OF + timedelta(days=dte)).isoformat()
    letter = "C" if kind == "call" else "P"
    return {"symbol": f"X{expiry[2:].replace('-', '')}{letter}{int(strike * 1000):08d}",
            "type": kind, "strike": strike, "expiry": expiry,
            "last_price": price, "bid": bid, "ask": ask}


def show(chain, title):
    print(f"\n{title}")
    print(f"  {'strike':>7} {'last':>6} {'bid':>6} {'ask':>6} {'width':>7}")
    for c in sorted(chain, key=lambda r: r["strike"]):
        w = relative_spread(c)
        print(f"  {c['strike']:>7.1f} {c['last_price']:>6.2f} {c['bid']:>6.2f} "
              f"{c['ask']:>6.2f} {w:>6.1%}")


def compare(chain, spot, want="call"):
    for cap in (0.0, 0.10):
        s = MomentumRiskCapStrategy(spread_width_pct=0.03, max_spread_pct=cap)
        long_leg, note = s.select_contract(chain, want, spot, as_of=AS_OF)
        label = "screen OFF" if cap == 0 else f"screen ON  (cap {cap:.0%})"
        if long_leg is None:
            print(f"\n  {label}: HOLD -- {note}")
            continue
        short, snote = s.select_spread(chain, long_leg, spot)
        debit = round(long_leg["last_price"] - (short["last_price"] if short else 0.0), 2)
        print(f"\n  {label}")
        print(f"    long  : {long_leg['symbol']} strike {long_leg['strike']:.1f} "
              f"({relative_spread(long_leg):.1%} wide)")
        print(f"    short : "
              + (f"{short['symbol']} strike {short['strike']:.1f} "
                 f"({relative_spread(short):.1%} wide)" if short else "none -- naked long"))
        print(f"    debit : ${debit:.2f}/share")


# ---------------------------------------------------------------- constructed chain
CONSTRUCTED = [
    row("call", 100.5, 14, 4.00, 2.40, 5.60),   # nearest spot -- but 80% wide
    row("call", 102.0, 14, 3.00, 2.99, 3.01),
    row("call", 105.0, 14, 1.60, 0.96, 2.24),   # exactly the target width -- but 80% wide
    row("call", 106.0, 14, 1.40, 1.39, 1.41),
]
print("=" * 78)
print("CONSTRUCTED chain (stale markets placed on the strikes that win on other criteria)")
print("=" * 78)
show(CONSTRUCTED, "chain")
compare(CONSTRUCTED, spot=100.0)

# ---------------------------------------------------------------------- mock chain
client = MockAlpacaMCPClient()
mock_chain = asyncio.run(client.get_option_chain("SPY"))
bars = asyncio.run(client.get_stock_bars("SPY", limit=11))
spot = bars[-1]["c"]
s = MomentumRiskCapStrategy()
long_leg, _ = s.select_contract(mock_chain, "call", spot)
expiry = long_leg["expiry"]
print("\n" + "=" * 78)
print(f"MOCK --dry chain, calls expiring {expiry}, spot ${spot:.2f} (unmodified)")
print("=" * 78)
show([c for c in mock_chain if c["type"] == "call" and c["expiry"] == expiry], "chain")
compare(mock_chain, spot=spot)
kept, rejected, unquoted = s.screen_liquidity(mock_chain)
print(f"\n  whole mock chain: {len(mock_chain)} rows, {rejected} rejected as too wide, "
      f"{unquoted} unquoted")
print("  NOTE: on this chain the screen prunes candidates but does not change the winner --")
print("        the strikes it rejects were already losing on strike distance / target width.")
