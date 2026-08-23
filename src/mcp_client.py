"""
Client for talking to Alpaca's official MCP server (alpacahq/alpaca-mcp-server, v2).

Two implementations behind the same async interface:
  - AlpacaMCPClient  -> spawns `uvx alpaca-mcp-server` over stdio, needs real API keys.
  - MockAlpacaMCPClient -> in-process fake with canned responses, no keys/network needed.

Only the tools this project actually uses are wrapped: get_clock, get_account_info,
get_stock_bars, get_option_chain, place_option_order, get_all_positions.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any


class AlpacaMCPError(RuntimeError):
    pass


class BaseAlpacaMCPClient(ABC):
    async def __aenter__(self) -> "BaseAlpacaMCPClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def get_clock(self) -> dict[str, Any]: ...

    @abstractmethod
    async def get_account_info(self) -> dict[str, Any]: ...

    @abstractmethod
    async def get_stock_bars(self, symbol: str, limit: int = 30) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def get_option_chain(self, symbol: str) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def place_option_order(self, symbol: str, side: str, qty: int) -> dict[str, Any]: ...

    @abstractmethod
    async def get_all_positions(self) -> list[dict[str, Any]]: ...


class AlpacaMCPClient(BaseAlpacaMCPClient):
    """Real client. Spawns the official Alpaca MCP server (v2) as a stdio subprocess.

    Requires ALPACA_API_KEY / ALPACA_SECRET_KEY in the environment and `uv` installed.
    ALPACA_PAPER_TRADE is left at its default ("true") — this project never trades live.
    """

    def __init__(self) -> None:
        api_key = os.environ.get("ALPACA_API_KEY")
        secret_key = os.environ.get("ALPACA_SECRET_KEY")
        if not api_key or not secret_key:
            raise AlpacaMCPError(
                "ALPACA_API_KEY / ALPACA_SECRET_KEY not set. "
                "Create free paper keys at https://alpaca.markets and export them, "
                "or run with --dry to use the mock client instead."
            )
        self._env = {
            **os.environ,
            "ALPACA_API_KEY": api_key,
            "ALPACA_SECRET_KEY": secret_key,
            "ALPACA_PAPER_TRADE": "true",
        }
        self._session = None
        self._stdio_ctx = None
        self._client_ctx = None

    async def connect(self) -> None:
        # Imported lazily so `--dry` runs never need the `mcp` package installed.
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(command="uvx", args=["alpaca-mcp-server"], env=self._env)
        self._stdio_ctx = stdio_client(params)
        read, write = await self._stdio_ctx.__aenter__()
        self._client_ctx = ClientSession(read, write)
        self._session = await self._client_ctx.__aenter__()
        await self._session.initialize()

    async def close(self) -> None:
        if self._client_ctx is not None:
            await self._client_ctx.__aexit__(None, None, None)
        if self._stdio_ctx is not None:
            await self._stdio_ctx.__aexit__(None, None, None)

    async def _call(self, tool: str, args: dict[str, Any]) -> Any:
        result = await self._session.call_tool(tool, args)
        if result.isError:
            raise AlpacaMCPError(f"{tool} failed: {result.content}")
        return result.content

    async def get_clock(self) -> dict[str, Any]:
        return await self._call("get_clock", {})

    async def get_account_info(self) -> dict[str, Any]:
        return await self._call("get_account_info", {})

    async def get_stock_bars(self, symbol: str, limit: int = 30) -> list[dict[str, Any]]:
        return await self._call("get_stock_bars", {"symbol": symbol, "limit": limit, "timeframe": "1Day"})

    async def get_option_chain(self, symbol: str) -> list[dict[str, Any]]:
        return await self._call("get_option_chain", {"underlying_symbol": symbol})

    async def place_option_order(self, symbol: str, side: str, qty: int) -> dict[str, Any]:
        return await self._call(
            "place_option_order",
            {"symbol": symbol, "side": side, "qty": qty, "type": "market", "time_in_force": "day"},
        )

    async def get_all_positions(self) -> list[dict[str, Any]]:
        return await self._call("get_all_positions", {})


class MockAlpacaMCPClient(BaseAlpacaMCPClient):
    """Deterministic fake used by `--dry`. No network, no keys, no `mcp` package needed.

    Generates a mildly upward-drifting synthetic price series so the momentum strategy
    has something real to compute against, and a two-strike synthetic option chain.
    """

    def __init__(self, seed_price: float = 100.0) -> None:
        self._seed_price = seed_price
        self._orders: list[dict[str, Any]] = []

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def get_clock(self) -> dict[str, Any]:
        return {"is_open": True, "timestamp": datetime.now(timezone.utc).isoformat()}

    async def get_account_info(self) -> dict[str, Any]:
        return {"equity": "100000.00", "cash": "50000.00", "buying_power": "100000.00"}

    async def get_stock_bars(self, symbol: str, limit: int = 30) -> list[dict[str, Any]]:
        bars = []
        price = self._seed_price
        start = datetime.now(timezone.utc) - timedelta(days=limit)
        # Simple synthetic uptrend with noise, seeded off the symbol name for repeatability.
        drift = 0.006 + (sum(ord(c) for c in symbol) % 5) * 0.001
        for i in range(limit):
            wobble = 0.004 * ((i * 7) % 3 - 1)
            price *= 1 + drift + wobble
            bars.append(
                {
                    "t": (start + timedelta(days=i)).isoformat(),
                    "o": round(price * 0.997, 2),
                    "h": round(price * 1.01, 2),
                    "l": round(price * 0.99, 2),
                    "c": round(price, 2),
                    "v": 1_000_000,
                }
            )
        return bars

    async def get_option_chain(self, symbol: str) -> list[dict[str, Any]]:
        last_close = (await self.get_stock_bars(symbol, limit=1))[0]["c"]
        strike_call = round(last_close * 1.02, 1)
        strike_put = round(last_close * 0.98, 1)
        expiry = (datetime.now(timezone.utc) + timedelta(days=14)).strftime("%Y-%m-%d")
        return [
            {
                "symbol": f"{symbol}{expiry.replace('-', '')}C{int(strike_call * 1000):08d}",
                "type": "call",
                "strike": strike_call,
                "expiry": expiry,
                "last_price": round(max(last_close - strike_call, 0) + 1.5, 2),
            },
            {
                "symbol": f"{symbol}{expiry.replace('-', '')}P{int(strike_put * 1000):08d}",
                "type": "put",
                "strike": strike_put,
                "expiry": expiry,
                "last_price": round(max(strike_put - last_close, 0) + 1.5, 2),
            },
        ]

    async def place_option_order(self, symbol: str, side: str, qty: int) -> dict[str, Any]:
        order = {
            "id": f"mock-{len(self._orders) + 1}",
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "status": "filled",
            "filled_at": datetime.now(timezone.utc).isoformat(),
        }
        self._orders.append(order)
        return order

    async def get_all_positions(self) -> list[dict[str, Any]]:
        return list(self._orders)
