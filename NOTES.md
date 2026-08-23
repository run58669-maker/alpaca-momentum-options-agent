# Research Notes — Alpaca AI Trading Agents Hackathon

## Hackathon (lablab.ai)
Source: https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon , https://x.com/lablabai/status/2089757334746677309

- Fully online, 2026-08-28 → 2026-09-04, submission deadline 2026-09-04 15:00 UTC.
- Prize pool: $6,000 (an older X post said $5,000; page currently shows $6,000 — use $6,000).
- Tracks: options alpha, volatility trading, hedging, portfolio overlays.
- **HARD REQUIREMENT: strategies must incorporate options trading** — a pure stock-momentum bot is NOT compliant on its own. It needs to translate its signal into an options trade (e.g. buy calls/puts, covered call, cash-secured put) to qualify.
- Must use Alpaca's Trading API **and** either the MCP server or the CLI.
- **Final submission requires a NEW dedicated Alpaca paper trading account** (not a pre-existing one) — do this only when actually submitting, not now.
- Paper trading only, simulated funds, real market data, no card required, 18+.
- General lablab.ai submission deliverables: working prototype reachable by URL, pitch video ≤5 min (MP4), slide deck (PDF), public GitHub repo.
- Judging (from a comparable lablab AI Trading Agents hackathon): Originality /5, Presentation /5, Business Value /5. This event's exact rubric wasn't reachable (page 403'd WebFetch); re-check https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon directly closer to submission, and https://lablab.ai/hackathon-rules for the general rule book.

## Alpaca MCP Server
Source: https://github.com/alpacahq/alpaca-mcp-server , https://docs.alpaca.markets/us/docs/alpaca-mcp-server

- Official repo: `alpacahq/alpaca-mcp-server`. **V2 is a full rewrite (FastMCP + OpenAPI); V1 tool names do not exist in V2.** This project targets V2 tool names.
- Requires Python 3.10+ and `uv`. Run via `uvx alpaca-mcp-server` (stdio transport by default; `--transport streamable-http --port N` also supported).
- Env vars:
  - `ALPACA_API_KEY` (required)
  - `ALPACA_SECRET_KEY` (required)
  - `ALPACA_PAPER_TRADE` (default `true` — leave it true, never set false in this project)
  - `ALPACA_TOOLSETS` (optional comma list to restrict tools, e.g. `account,trading,assets,stock-data,options-data`)
- No separate "paper base URL" var — the server routes internally based on `ALPACA_PAPER_TRADE`.
- Relevant tools for this project: `get_account_info`, `get_clock`, `get_stock_bars`, `get_stock_latest_bar`, `get_option_chain`, `get_option_contracts`, `place_option_order`, `place_stock_order`, `get_all_positions`, `get_orders`, `cancel_all_orders`.
- Claude Code CLI wiring: `claude mcp add alpaca --scope user --transport stdio uvx alpaca-mcp-server --env ALPACA_API_KEY=... --env ALPACA_SECRET_KEY=...`
- Programmatic (non-Claude-Desktop) connection: standard MCP Python SDK stdio client — `mcp.client.stdio.stdio_client(StdioServerParameters(command="uvx", args=["alpaca-mcp-server"], env={...}))` + `mcp.ClientSession`. The docs don't show this explicitly but it follows the standard MCP stdio pattern used by every other client config shown (Claude Desktop/Cursor/VS Code all spawn the same `uvx alpaca-mcp-server` stdio process).
- Paper keys are free — sign up at https://alpaca.markets (Sign Up → Trading API / Paper Trading). Not signing up automatically per task constraints; noted in NEXT.md.

## 3 most important facts
1. Options trading is a **hard requirement** for this hackathon, not optional — a stock-only bot needs to be reframed as an options strategy (calls/puts/covered calls) to be eligible.
2. Alpaca MCP Server v2 (current) has different tool names than v1 — any code/docs referencing v1 tool names is stale; this project uses v2 names (`place_option_order`, `get_option_chain`, etc.).
3. A brand-new dedicated paper account is required at final-submission time — the account used for day-to-day dev/testing should not be assumed to be the one judged; re-create/verify right before submitting.
