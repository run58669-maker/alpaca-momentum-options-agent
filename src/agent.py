"""
Agent entry point.

Each pass: fetch clock + account + bars + option chain from the Alpaca MCP server,
ask the strategy for a decision, log the decision (with reasoning) to the journal,
and place the order if the decision isn't "hold". Paper trading only — the mock
client never touches a real endpoint, and the real client hardcodes ALPACA_PAPER_TRADE=true.

Usage:
    py src/agent.py --dry                       # no API keys needed, mocked MCP responses
    py src/agent.py --symbol SPY --iterations 3  # real run, needs ALPACA_API_KEY / ALPACA_SECRET_KEY
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from journal import Journal
from mcp_client import AlpacaMCPClient, MockAlpacaMCPClient
from strategy import MomentumRiskCapStrategy

DEFAULT_JOURNAL_PATH = Path(__file__).parent.parent / "journal" / "decisions.jsonl"


async def run_once(client, strategy: MomentumRiskCapStrategy, journal: Journal, symbol: str) -> dict:
    clock = await client.get_clock()
    account = await client.get_account_info()
    equity = float(account["equity"])

    bars = await client.get_stock_bars(symbol, limit=strategy.lookback + 1)
    chain = await client.get_option_chain(symbol)
    realized_loss = journal.realized_loss_today()

    decision = strategy.decide(
        symbol=symbol,
        bars=bars,
        option_chain=chain,
        equity=equity,
        realized_loss_today=realized_loss,
    )

    order = None
    if decision.action != "hold":
        side = "buy"
        contract_type = "call" if decision.action == "buy_call" else "put"
        contract = next(c for c in chain if c["type"] == contract_type)
        order = await client.place_option_order(contract["symbol"], side, decision.contracts)

    record = journal.log(
        symbol=symbol,
        market_open=clock.get("is_open"),
        equity=equity,
        momentum_pct=round(decision.momentum_pct, 4),
        action=decision.action,
        contracts=decision.contracts,
        reason=decision.reason,
        order=order,
    )
    return record


async def main_async(args: argparse.Namespace) -> None:
    client = MockAlpacaMCPClient() if args.dry else AlpacaMCPClient()
    strategy = MomentumRiskCapStrategy(
        lookback=args.lookback,
        momentum_threshold=args.momentum_threshold,
        risk_pct=args.risk_pct,
        max_contracts=args.max_contracts,
        max_daily_loss_pct=args.max_daily_loss_pct,
    )
    journal = Journal(args.journal_path)

    async with client:
        for i in range(args.iterations):
            record = await run_once(client, strategy, journal, args.symbol)
            print(f"[{i + 1}/{args.iterations}] {record['action']} {record['symbol']}"
                  f" contracts={record['contracts']} momentum={record['momentum_pct']:.2%}")
            print(f"    reason: {record['reason']}")
            if record["order"]:
                print(f"    order: {record['order']}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Momentum + risk-cap options agent on Alpaca (paper only).")
    p.add_argument("--dry", action="store_true", help="Use mocked MCP responses, no API keys / network needed.")
    p.add_argument("--symbol", default="SPY", help="Underlying symbol to trade (default: SPY).")
    p.add_argument("--iterations", type=int, default=1, help="Number of decision passes to run (default: 1).")
    p.add_argument("--lookback", type=int, default=10, help="Bars of lookback for momentum (default: 10).")
    p.add_argument("--momentum-threshold", type=float, default=0.02, dest="momentum_threshold",
                    help="Minimum |momentum| to act on, e.g. 0.02 = 2%% (default: 0.02).")
    p.add_argument("--risk-pct", type=float, default=0.01, dest="risk_pct",
                    help="Fraction of equity risked per trade (default: 0.01 = 1%%).")
    p.add_argument("--max-contracts", type=int, default=5, dest="max_contracts",
                    help="Hard cap on contracts per order (default: 5).")
    p.add_argument("--max-daily-loss-pct", type=float, default=0.03, dest="max_daily_loss_pct",
                    help="Circuit breaker: halt new trades once today's realized loss reaches this "
                         "fraction of equity (default: 0.03 = 3%%).")
    p.add_argument("--journal-path", default=str(DEFAULT_JOURNAL_PATH), dest="journal_path",
                    help="Where to append JSONL decision records.")
    return p


def main() -> None:
    args = build_parser().parse_args()
    if not args.dry and not (__import__("os").environ.get("ALPACA_API_KEY")):
        print("No --dry flag and ALPACA_API_KEY is not set. Either export ALPACA_API_KEY / "
              "ALPACA_SECRET_KEY (paper keys, free at https://alpaca.markets) or add --dry.",
              file=sys.stderr)
        raise SystemExit(1)
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
