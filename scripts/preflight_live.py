"""Preflight the live MCP transport without an Alpaca account.

Spawns the pinned Alpaca MCP server over stdio with placeholder credentials, completes
the handshake, and checks every tool name and argument name `AlpacaMCPClient` sends
against the server's advertised inputSchema. Nothing is ordered and no account is
touched -- the placeholder keys are never used for a request.

Run this after bumping `AlpacaMCPClient.SERVER_SPEC`, and once before the first live
session, so a wire-shape mismatch surfaces here instead of mid-position.

    py scripts/preflight_live.py

Exit 0 = every call this repo makes matches the server. Exit 1 = at least one mismatch.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from mcp_client import AlpacaMCPClient  # noqa: E402

# Every tool this repo calls, with the argument names it sends. Transcribed from the
# `_call` sites in AlpacaMCPClient; `place_option_order` is the union of the single-leg
# and multi-leg calls, which share one tool.
CALLS = {
    "get_clock": [],
    "get_account_info": [],
    "get_stock_bars": ["symbols", "timeframe", "days", "limit"],
    "get_option_chain": ["underlying_symbol"],
    "get_all_positions": [],
    "get_orders": ["status", "nested", "limit", "direction", "before_order_id"],
    "get_account_activities_by_type": ["activity_type", "after", "direction",
                                       "page_size", "page_token"],
    "place_option_order": ["symbol", "side", "qty", "type", "limit_price",
                           "time_in_force", "position_intent", "client_order_id",
                           "order_class", "legs"],
}


async def main() -> int:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    spec = AlpacaMCPClient.SERVER_SPEC
    env = {
        **os.environ,
        "ALPACA_API_KEY": "PREFLIGHT_PLACEHOLDER",
        "ALPACA_SECRET_KEY": "PREFLIGHT_PLACEHOLDER",
        "ALPACA_PAPER_TRADE": "true",
    }
    params = StdioServerParameters(
        command="uvx", args=["--from", spec, "alpaca-mcp-server"], env=env
    )
    print(f"Spawning {spec} ...")
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"HANDSHAKE OK  server={init.serverInfo.name} protocol={init.protocolVersion}")
            tools = {t.name: t for t in (await session.list_tools()).tools}
            print(f"TOOLS ADVERTISED: {len(tools)}")

            failures = []
            for tool, args in sorted(CALLS.items()):
                if tool not in tools:
                    failures.append(f"{tool}: not advertised by the server")
                    print(f"  [MISS] {tool}")
                    continue
                schema = tools[tool].inputSchema or {}
                accepts = set((schema.get("properties") or {}).keys())
                required = set(schema.get("required") or [])
                unknown = sorted(set(args) - accepts)
                missing = sorted(required - set(args))
                if unknown:
                    failures.append(f"{tool}: sends argument(s) the server rejects: {unknown}")
                if missing:
                    failures.append(f"{tool}: omits required argument(s): {missing}")
                print(f"  [{'BAD ' if unknown or missing else 'OK  '}] {tool}: "
                      f"sends={sorted(args)} required={sorted(required)}")

    if failures:
        print(f"\nPREFLIGHT FAIL ({len(failures)}):")
        for line in failures:
            print(f"  - {line}")
        return 1
    print(f"\nPREFLIGHT PASS: {len(CALLS)} tools, "
          f"{sum(len(a) for a in CALLS.values())} argument names, all match {spec}.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
