# Alpaca Momentum Options Agent (name TBD)

One-line pitch: an autonomous paper-trading agent that reads recent price momentum
through Alpaca's official MCP server, turns a strong signal into a sized options
trade (buy calls on strength, puts on weakness), and writes a plain-English reason
for every single decision — including every time it decides to do nothing — to an
auditable JSONL journal.

Built for the [Alpaca AI Trading Agents Hackathon](https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon)
(lablab.ai, 2026-08-28 → 2026-09-04). Paper trading only. No real-money code path
exists anywhere in this repo.

## Architecture

```
src/agent.py        entry point: argparse, the decision loop, wires everything together
src/mcp_client.py    talks to Alpaca's official MCP server (alpacahq/alpaca-mcp-server, v2)
                      over stdio via the `mcp` Python package; also ships a MockAlpacaMCPClient
                      with deterministic synthetic data for --dry runs (no keys, no network)
src/strategy.py      MomentumRiskCapStrategy: momentum signal -> call/put/hold, with
                      equity-based position sizing and a daily-loss circuit breaker
src/journal.py       append-only JSONL logger; every decision (trade or hold) is logged
                      with its reason; also computes today's realized loss for the
                      circuit breaker
journal/             decisions.jsonl lands here at runtime (gitignored)
```

Decision loop per pass: `get_clock` -> `get_account_info` -> `get_stock_bars` ->
`get_option_chain` -> `strategy.decide(...)` -> (if not hold) `place_option_order`
-> `journal.log(...)`.

## Why options, not just stocks

This hackathon requires strategies to incorporate options trading (see `NOTES.md`).
The momentum signal is computed on the underlying's price bars (simple and
explainable), but the resulting trade is always an options trade: a long call on
bullish momentum, a long put on bearish momentum, sized as a fraction of account
equity and capped in contract count. See `NEXT.md` for the planned extension to
defined-risk spreads / covered calls for the actual submission.

## Setup

```
pip install -r requirements.txt
```

Real run needs `uv`/`uvx` on PATH (spawns `uvx alpaca-mcp-server`) and free Alpaca
paper API keys (see `NEXT.md`):

```
set ALPACA_API_KEY=...
set ALPACA_SECRET_KEY=...
py src/agent.py --symbol SPY --iterations 1
```

## Dry run (no keys needed)

```
py src/agent.py --dry --symbol SPY --iterations 3
```

Uses `MockAlpacaMCPClient`: synthetic price bars with a small upward drift, a
synthetic two-strike option chain, and in-memory fake order fills. Exercises the
full loop end to end — strategy math, sizing, journaling — with zero external
dependencies. See `RUN_LOG.md` for a captured run.

## Risk controls

- `--risk-pct` (default 1%) caps how much equity a single trade risks.
- `--max-contracts` (default 5) is a hard ceiling regardless of sizing math.
- `--max-daily-loss-pct` (default 3%) is a circuit breaker: once today's realized
  loss (read back from the journal) reaches this fraction of equity, the agent
  forces `hold` on every subsequent pass for the rest of the day.
- `ALPACA_PAPER_TRADE` is hardcoded to `"true"` in `AlpacaMCPClient` — there is no
  flag or env var in this codebase that can flip it to live trading.

## CLI flags

Run `py src/agent.py --help` for the full list (`--symbol`, `--iterations`,
`--lookback`, `--momentum-threshold`, `--risk-pct`, `--max-contracts`,
`--max-daily-loss-pct`, `--journal-path`, `--dry`).
