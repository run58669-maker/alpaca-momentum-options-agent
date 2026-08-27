"""
Client for talking to Alpaca's official MCP server (alpacahq/alpaca-mcp-server, v2).

Two implementations behind the same async interface:
  - AlpacaMCPClient  -> spawns `uvx alpaca-mcp-server` over stdio, needs real API keys.
  - MockAlpacaMCPClient -> in-process fake with canned responses, no keys/network needed.

Only the tools this project actually uses are wrapped: get_clock, get_account_info,
get_stock_bars, get_option_chain, place_option_order (opening and closing, single-leg
and multi-leg), get_all_positions, get_account_activities_by_type.

The wire shapes below were verified on 2026-08-24 against the upstream source of
alpacahq/alpaca-mcp-server @ main (tree sha 803b07a3), not guessed:
  - `get_stock_bars` takes `symbols` (comma-separated, PLURAL) + `timeframe`/`days`/`limit`
    and returns `{"bars": {"SPY": [{t,o,h,l,c,v}, ...]}}` -- src/alpaca_mcp_server/
    market_data_overrides.py:101, tests/test_paper_integration.py:207.
  - `get_option_chain` takes `underlying_symbol` and returns
    `{"snapshots": {"<OCC symbol>": {latestTrade, latestQuote, greeks, impliedVolatility}}}`.
    There is NO type/strike/expiry field on a snapshot -- those live in the OCC symbol
    and must be parsed out -- tests/test_paper_integration.py:334,351.
  - `place_option_order` wants `qty` as a STRING, plus `symbol`/`side` for single leg, and
    supports multi-leg via `legs=[{symbol, ratio_qty, side, position_intent}]` +
    `order_class="mleg"`; `limit_price` is also a STRING and, on a multi-leg parent, is the
    net debit (positive) or credit (negative) for the whole structure. `qty` on a multi-leg
    parent is the strategy multiplier -- each leg trades `qty * ratio_qty` contracts
    -- src/alpaca_mcp_server/overrides.py:258-305, tests/test_paper_integration.py:502.
  - Tools classified `external_text` wrap their payload in a trust-boundary envelope
    `{"_alpaca_mcp_security": ..., "data": <payload>}` -- tests/test_paper_integration.py:49.
Verified 2026-08-25, same repo @ main, for the exit path:
  - `get_all_positions` is operationId `getAllOpenPositions` -> `GET /v2/positions`,
    whose 200 body is a bare array of `Position`: every numeric field a string,
    `side` the enum long/short, `asset_class` "us_option" for contracts, and NO
    strike/expiry/type (parsed out of the OCC symbol, as with the chain)
    -- src/alpaca_mcp_server/tool_registry.py:106 + the vendored trading-api.json.
  - `place_option_order` accepts `type` "market" or "limit" for multi-leg as well as
    single-leg, `time_in_force` "day" only, and per-leg `side` + `position_intent`,
    so a spread can be closed as one order -- src/alpaca_mcp_server/overrides.py:258-305.
The normalizers in this module translate those wire shapes into the flat internal shapes
the strategy consumes, so the strategy never sees Alpaca's wire format.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from math import exp
from typing import Any


class AlpacaMCPError(RuntimeError):
    pass


class OrderBookIncomplete(AlpacaMCPError):
    """The working-order book could not be read whole.

    Separate from `AlpacaMCPError` because the caller's answer is different. A
    generic failure is a broken pass; this one is a *readable* account whose order
    book this client cannot promise it saw all of, and the only safe reading of a
    partial book is "risk is under-stated by an unknown amount". `run_once` catches
    it, journals the gap and stands the entry side down for the pass -- exits still
    run, because they only ever take risk off.
    """


# OCC option symbol, e.g. "SPY260910C00108700" -> SPY / 2026-09-10 / call / 108.70.
# Alpaca returns unpadded roots, so the root is "everything before the 6-digit date".
_OCC_RE = re.compile(r"^(?P<root>[A-Z]+)(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})(?P<cp>[CP])(?P<strike>\d{8})$")


def parse_occ_symbol(symbol: str) -> dict[str, Any] | None:
    """Split an OCC option symbol into its parts, or None if it isn't one.

    The option chain endpoint keys its snapshots by OCC symbol and carries no
    separate strike/expiry/type fields, so this parse is the only way to know what
    a chain row actually is.
    """
    m = _OCC_RE.match(str(symbol).strip().upper())
    if not m:
        return None
    try:
        expiry = datetime(2000 + int(m["yy"]), int(m["mm"]), int(m["dd"])).date()
    except ValueError:
        return None  # e.g. month 13 / day 32
    return {
        "underlying": m["root"],
        "expiry": expiry.isoformat(),
        "type": "call" if m["cp"] == "C" else "put",
        "strike": int(m["strike"]) / 1000.0,
    }


def unwrap_payload(result: Any) -> Any:
    """Turn an MCP CallToolResult into the plain payload the API returned.

    Handles, in order: fastmcp's `.data`, the MCP SDK's `.structuredContent`, and
    the JSON text blocks in `.content`. Then peels the server's trust-boundary
    envelope if present. Plain dicts/lists pass through untouched (mock client).
    """
    payload: Any = None
    if isinstance(result, (dict, list)):
        payload = result
    elif getattr(result, "data", None) is not None:
        payload = result.data
    elif getattr(result, "structuredContent", None) is not None:
        payload = result.structuredContent
    else:
        for block in getattr(result, "content", None) or []:
            text = getattr(block, "text", None)
            if text is None:
                continue
            try:
                payload = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                payload = text
            break
    if isinstance(payload, dict) and "_alpaca_mcp_security" in payload and "data" in payload:
        payload = payload["data"]
    return payload


def _snapshot_price(snapshot: dict[str, Any]) -> float | None:
    """Best available price for one chain snapshot: last trade, else quote midpoint."""
    trade = snapshot.get("latestTrade") or {}
    price = trade.get("p")
    if isinstance(price, (int, float)) and price > 0:
        return float(price)
    quote = snapshot.get("latestQuote") or {}
    bid, ask = quote.get("bp"), quote.get("ap")
    if isinstance(bid, (int, float)) and isinstance(ask, (int, float)) and bid > 0 and ask > 0:
        return (float(bid) + float(ask)) / 2
    return None


def _to_float(value: Any) -> float | None:
    """Alpaca sends qty/price as strings; anything unparseable is not a number."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_fills(payload: Any) -> list[dict[str, Any]]:
    """Activities array -> the flat fill rows the matcher consumes, oldest first.

    Non-FILL activities (dividends, fees, transfers) and rows without a usable
    symbol / side / positive qty / price are dropped: a fill that can't be priced
    can't produce an honest P&L number, and guessing one would feed the breaker
    a fiction. Partial fills are kept -- each is a real execution of its own qty.
    """
    if not isinstance(payload, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        if str(item.get("activity_type", "")).upper() != "FILL":
            continue
        side = str(item.get("side", "")).lower()
        if side not in ("buy", "sell"):
            continue
        symbol = str(item.get("symbol", "")).upper()
        qty = _to_float(item.get("qty"))
        price = _to_float(item.get("price"))
        if not symbol or qty is None or price is None or qty <= 0 or price < 0:
            continue
        rows.append(
            {
                "id": str(item.get("id", "")),
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "price": price,
                "transaction_time": str(item.get("transaction_time", "")),
                "order_id": str(item.get("order_id", "")),
            }
        )
    # Ascending by execution time so FIFO matching sees opens before closes; the
    # id is the tiebreaker because it is prefixed with the execution timestamp.
    rows.sort(key=lambda r: (r["transaction_time"], r["id"]))
    return rows



# `client_order_id` is capped at 128 chars by the API (trading-api.json:
# Order.client_order_id maxLength). The keys below are far shorter, but the cap is
# asserted in tests so a future longer prefix fails here instead of at Alpaca.
CLIENT_ORDER_ID_MAX_LEN = 128
CLOSE_ORDER_ID_PREFIX = "mrcap-close"
OPEN_ORDER_ID_PREFIX = "mrcap-open"


def make_open_client_order_id(legs: list[dict[str, Any]], day: str | None = None) -> str:
    """Idempotency key for one opening order: same structure, same UTC day -> same key.

    The hole this closes is not the timeout retry (nothing retries yet); it is the
    *next pass*. An entry that is `accepted` but unfilled is not a position, so
    `open_risk` cannot see it and the portfolio cap cannot charge for it. Twenty
    seconds later the same momentum reading produces the same decision and a second
    identical order goes out on top of the first -- two structures' worth of risk
    inside a cap that was told about neither. Alpaca refusing the duplicate key is
    the only place that can be caught, because Alpaca is the only party that knows
    the first order exists.

    Derived from the legs (symbols + sides) and the UTC date, so it recomputes from
    the same inputs with nothing stored.

    **`qty` is deliberately NOT in the key**, and this is the one real difference
    from `make_close_client_order_id`. A close of a smaller qty is a genuinely
    different order (a partial fill left less to close); a re-entry sized smaller
    because portfolio headroom shrank is the *same* attempt wearing a smaller hat,
    and including qty would mint it a fresh key and let it through.

    The cost is stated plainly: this makes "one identical opening structure per UTC
    day" a rule. Adding to a winner intraday, or re-entering the same strikes after
    a stop, is refused until tomorrow. That is a policy choice, not an oversight --
    with in-flight orders invisible to the risk budget, an agent that can re-send
    the same structure is an agent whose account-level cap has a hole in it, and a
    missed add costs less than an uncounted double position. Revisit once
    `get_orders` puts working orders into `open_risk` (NEXT.md).
    """
    day = day or datetime.now(timezone.utc).date().isoformat()
    body = "|".join(f"{leg['symbol']}:{str(leg['side']).lower()}" for leg in legs)
    digest = hashlib.sha256(f"{day}|{body}".encode()).hexdigest()[:16]
    return f"{OPEN_ORDER_ID_PREFIX}-{day}-{digest}"


def make_close_client_order_id(
    legs: list[dict[str, Any]], qty: int, day: str | None = None
) -> str:
    """Idempotency key for one closing order: same close today -> same key.

    Alpaca rejects a second order carrying a `client_order_id` it has already seen,
    which is the only thing that makes a retry safe: `place_option_order` can time out
    *after* the order reached Alpaca, and a blind retry would then flatten the position
    twice -- the second one opening a fresh position in the opposite direction.

    Derived from the contents of the close (leg symbols + their sides + contract count),
    so the key can be recomputed from the same inputs without storing anything. Two
    properties fall out of that and both are wanted:

      - A retry of the identical close is refused. Within one day a repeat of the same
        (legs, qty) is always a duplicate: a filled close removes the position, a
        working close zeroes `qty_available` (which ExitPolicy skips on), and a
        partial fill leaves a smaller `qty` -- a different key, correctly allowed.
      - Tomorrow's key differs. Closing orders go out `time_in_force="day"`, so an
        unfilled one is dead by the bell and the next session must be able to retry.

    `day` is the UTC date. The US options regular session (13:30-20:00/21:00 UTC) never
    crosses midnight UTC, so no single session is split across two keys.

    NOT a substitute for a stored order log: it dedupes a retry, not a restart that
    lost track of what it had already sent.
    """
    day = day or datetime.now(timezone.utc).date().isoformat()
    # Sides are part of the identity, not just symbols: the same two contracts closed
    # from a long structure and from a short one are different orders.
    body = "|".join(
        f"{leg['symbol']}:{str(leg['side']).lower()}" for leg in legs
    )
    digest = hashlib.sha256(f"{day}|{qty}|{body}".encode()).hexdigest()[:16]
    return f"{CLOSE_ORDER_ID_PREFIX}-{day}-{digest}"


def _close_leg(position: dict[str, Any]) -> dict[str, Any]:
    """One open option position -> the multi-leg order leg that flattens it."""
    long_side = str(position["side"]).lower() == "long"
    return {
        "symbol": position["symbol"],
        "ratio_qty": "1",
        "side": "sell" if long_side else "buy",
        "position_intent": "sell_to_close" if long_side else "buy_to_close",
    }


# Statuses after which an order can no longer take on risk. Everything *not* in
# this set is treated as still working, including the rare ones (`held`,
# `calculated`, `suspended`) and `done_for_day`, whose exact liveness the spec text
# leaves ambiguous. Counting a dead order over-states risk and costs a trade;
# missing a live one under-states it and lets an unbudgeted structure onto the
# book, so ambiguity resolves toward counting.
TERMINAL_ORDER_STATUSES = frozenset(
    {"filled", "canceled", "expired", "rejected", "replaced"}
)

# Position intents that add exposure. `sell_to_open` is here because it is a leg of
# a spread, not because this strategy ever opens one alone.
OPENING_INTENTS = frozenset({"buy_to_open", "sell_to_open"})


def normalize_orders(payload: Any) -> list[dict[str, Any]]:
    """`GET /v2/orders` array -> the working orders the portfolio budget has to count.

    Terminal orders are dropped; everything still live is kept, whatever its asset
    class, because a row this repo cannot price is a row the caller has to be told
    about rather than one it may quietly skip (see `working_order_risk`).

    Two shape facts drive the parsing, both read off the vendored spec's own
    examples rather than assumed:

    - A multi-leg parent carries `symbol`, `side` and `asset_class` as **empty
      strings** -- its identity lives entirely in `legs`. Deciding "is this an
      option order?" off the parent's `asset_class` would classify every spread as
      non-option, so it is decided off the legs (or, for a single-leg order, off
      the parent's own OCC symbol).
    - `qty` counts *structures* on an mleg parent while each leg carries its own
      `qty`/`ratio_qty`. The parent's `limit_price` is the net debit for one such
      structure, so parent qty is the only quantity that pairs with it.

    `remaining_qty` is qty minus `filled_qty`: a partial fill is already a position
    and is already counted there, so counting the whole order would book that part
    of the risk twice.

    Each leg keeps its `ratio_qty` and its parsed OCC geometry (underlying, expiry,
    type, strike), and the parent keeps its own `position_intent`. The risk layer
    needs them to tell a 1:1 debit vertical -- whose maximum loss really is the net
    debit -- apart from a ratio spread or a roll, whose is not. Dropping them here
    is what let `working_order_risk` price an unbounded structure as a small number.
    """
    if not isinstance(payload, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status", "")).lower()
        if status in TERMINAL_ORDER_STATUSES:
            continue
        raw_legs = item.get("legs") or []
        legs: list[dict[str, Any]] = []
        if isinstance(raw_legs, list):
            for leg in raw_legs:
                if not isinstance(leg, dict):
                    continue
                leg_symbol = str(leg.get("symbol", "")).upper()
                parsed_leg = parse_occ_symbol(leg_symbol) or {}
                legs.append(
                    {
                        "symbol": leg_symbol,
                        "side": str(leg.get("side", "")).lower(),
                        "asset_class": str(leg.get("asset_class", "")).lower(),
                        "position_intent": str(leg.get("position_intent", "")).lower(),
                        # Geometry, not decoration: without the ratio and the strikes
                        # a buy-1/sell-2 ratio spread is indistinguishable from a 1:1
                        # vertical, and only the latter's loss is the net debit.
                        "ratio_qty": _to_float(leg.get("ratio_qty")),
                        "underlying": parsed_leg.get("underlying"),
                        "expiry": parsed_leg.get("expiry"),
                        "option_type": parsed_leg.get("type"),
                        "strike": parsed_leg.get("strike"),
                    }
                )
        parent_symbol = str(item.get("symbol", "")).upper()
        parent_intent = str(item.get("position_intent", "")).lower()
        if legs:
            symbols = [leg["symbol"] for leg in legs]
            is_option = all(
                leg["asset_class"] == "us_option" or parse_occ_symbol(leg["symbol"])
                for leg in legs
            )
            intents = [leg["position_intent"] for leg in legs]
        else:
            symbols = [parent_symbol] if parent_symbol else []
            is_option = bool(parent_symbol) and (
                str(item.get("asset_class", "")).lower() == "us_option"
                or parse_occ_symbol(parent_symbol) is not None
            )
            intents = [parent_intent]
        qty = _to_float(item.get("qty"))
        filled = _to_float(item.get("filled_qty")) or 0.0
        remaining = None if qty is None else max(abs(qty) - abs(filled), 0.0)
        rows.append(
            {
                "id": str(item.get("id", "")),
                "client_order_id": str(item.get("client_order_id", "")),
                "status": status,
                "order_class": str(item.get("order_class", "")).lower(),
                "order_type": str(item.get("order_type") or item.get("type") or "").lower(),
                "symbols": symbols,
                "legs": legs,
                "position_intent": parent_intent,
                "is_option": bool(is_option),
                # Every intent absent is not the same as "this closes something":
                # an order whose intent this repo cannot read is treated as opening,
                # which over-states risk rather than letting an unbudgeted one past.
                "opening": any(i in OPENING_INTENTS or i == "" for i in intents),
                "limit_price": _to_float(item.get("limit_price")),
                "qty": None if qty is None else abs(qty),
                "filled_qty": abs(filled),
                "remaining_qty": remaining,
            }
        )
    rows.sort(key=lambda r: (r["id"], r["client_order_id"]))
    return rows


def normalize_positions(payload: Any) -> list[dict[str, Any]]:
    """`GET /v2/positions` array -> the flat option positions the exit policy reads.

    Only `us_option` rows whose symbol parses as OCC survive: the exit policy sizes
    and routes orders off strike/expiry/type, and a row those can't be read from is
    a row we must not send a closing order for.

    Direction comes from `side` (long/short), never from the sign of `qty` -- the
    quantity is taken as an absolute contract count so the caller cannot accidentally
    send a negative qty if Alpaca's sign convention for short options differs from
    what we assume. `qty_available` is carried through because it is what tells us a
    close is already in flight (see ExitPolicy: a position with 0 available is skipped
    rather than closed twice).
    """
    if not isinstance(payload, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        if str(item.get("asset_class", "")).lower() != "us_option":
            continue
        parsed = parse_occ_symbol(item.get("symbol", ""))
        if parsed is None:
            continue
        qty = _to_float(item.get("qty"))
        if qty is None or qty == 0:
            continue
        side = str(item.get("side", "")).lower()
        if side not in ("long", "short"):
            continue
        available = _to_float(item.get("qty_available"))
        rows.append(
            {
                "symbol": str(item["symbol"]).upper(),
                "underlying": parsed["underlying"],
                "type": parsed["type"],
                "strike": parsed["strike"],
                "expiry": parsed["expiry"],
                "side": side,
                "qty": abs(qty),
                # Missing qty_available is read as "all of it": the field is optional
                # in the schema, and treating absent as 0 would freeze every exit.
                "qty_available": abs(available) if available is not None else abs(qty),
                "avg_entry_price": _to_float(item.get("avg_entry_price")) or 0.0,
                "current_price": _to_float(item.get("current_price")) or 0.0,
                "cost_basis": _to_float(item.get("cost_basis")) or 0.0,
                "unrealized_pl": _to_float(item.get("unrealized_pl")) or 0.0,
            }
        )
    rows.sort(key=lambda r: r["symbol"])
    return rows

def normalize_chain(payload: Any) -> list[dict[str, Any]]:
    """`{"snapshots": {occ_symbol: snapshot}}` -> the flat rows the strategy expects.

    Rows whose symbol doesn't parse as OCC, or that have no usable price, are dropped:
    the strategy must never be handed a contract whose strike/expiry/price is a guess.
    Output is sorted by symbol so a chain's iteration order can't change the pick.
    """
    if not isinstance(payload, dict):
        return []
    snapshots = payload.get("snapshots")
    if not isinstance(snapshots, dict):
        return []
    rows: list[dict[str, Any]] = []
    for symbol, snapshot in snapshots.items():
        parsed = parse_occ_symbol(symbol)
        if parsed is None or not isinstance(snapshot, dict):
            continue
        price = _snapshot_price(snapshot)
        if price is None:
            continue
        row = {
            "symbol": str(symbol).upper(),
            "type": parsed["type"],
            "strike": parsed["strike"],
            "expiry": parsed["expiry"],
            "last_price": price,
        }
        quote = snapshot.get("latestQuote")
        if isinstance(quote, dict):
            bid, ask = quote.get("bp"), quote.get("ap")
            # Both sides, both positive, and ask >= bid. A one-sided or crossed quote
            # cannot produce an honest spread width, and carrying half of one would let
            # the liquidity screen compute a width off a number that isn't a width.
            if (
                isinstance(bid, (int, float))
                and isinstance(ask, (int, float))
                and bid > 0
                and ask >= bid
            ):
                row["bid"] = float(bid)
                row["ask"] = float(ask)
        greeks = snapshot.get("greeks")
        if isinstance(greeks, dict) and isinstance(greeks.get("delta"), (int, float)):
            row["delta"] = float(greeks["delta"])
        if isinstance(snapshot.get("impliedVolatility"), (int, float)):
            row["iv"] = float(snapshot["impliedVolatility"])
        rows.append(row)
    rows.sort(key=lambda r: r["symbol"])
    return rows


def normalize_bars(payload: Any, symbol: str) -> list[dict[str, Any]]:
    """`{"bars": {"SPY": [...]}}` -> the bar list the strategy expects.

    Also accepts an already-flat list (mock client) and the single-symbol
    `{"bars": [...]}` variant. Bars are returned oldest-first.
    """
    if isinstance(payload, list):
        bars = payload
    elif isinstance(payload, dict):
        bars = payload.get("bars", payload)
        if isinstance(bars, dict):
            bars = bars.get(str(symbol).upper(), [])
    else:
        return []
    if not isinstance(bars, list):
        return []
    clean = [b for b in bars if isinstance(b, dict) and isinstance(b.get("c"), (int, float))]
    return sorted(clean, key=lambda b: str(b.get("t", "")))


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
    async def place_option_order(
        self, symbol: str, side: str, qty: int, limit_price: float,
        client_order_id: str | None = None
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def place_option_spread_order(
        self, long_symbol: str, short_symbol: str, qty: int, limit_price: float,
        client_order_id: str | None = None
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def get_all_positions(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def get_orders(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def place_option_close_order(
        self, legs: list[dict[str, Any]], qty: int, client_order_id: str | None = None
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def get_fills(self, after: str) -> list[dict[str, Any]]: ...


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

    ACTIVITY_PAGE_SIZE = 100  # upstream maximum for get_account_activities_by_type
    MAX_ACTIVITY_PAGES = 10
    ORDER_PAGE_SIZE = 500  # upstream maximum for get_orders (default is 50)
    MAX_ORDER_PAGES = 10

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
        if getattr(result, "isError", False):
            raise AlpacaMCPError(f"{tool} failed: {getattr(result, 'content', result)}")
        return unwrap_payload(result)

    async def get_clock(self) -> dict[str, Any]:
        return await self._call("get_clock", {})

    async def get_account_info(self) -> dict[str, Any]:
        return await self._call("get_account_info", {})

    async def get_stock_bars(self, symbol: str, limit: int = 30) -> list[dict[str, Any]]:
        # `symbols` is plural and comma-separated upstream; `days` is the lookback window
        # used when `start` is omitted, so it must span enough CALENDAR days to contain
        # `limit` trading days (~5 per 7) -- 2*limit+5 has slack for weekends + holidays.
        payload = await self._call(
            "get_stock_bars",
            {"symbols": symbol, "timeframe": "1Day", "days": 2 * limit + 5, "limit": limit},
        )
        return normalize_bars(payload, symbol)

    async def get_option_chain(self, symbol: str) -> list[dict[str, Any]]:
        payload = await self._call("get_option_chain", {"underlying_symbol": symbol})
        return normalize_chain(payload)

    async def place_option_order(
        self, symbol: str, side: str, qty: int, limit_price: float,
        client_order_id: str | None = None
    ) -> dict[str, Any]:
        """Buy the naked long fallback as a limit order at the price it was sized against.

        `limit_price` is per share, and it is not a separate opinion about value: it is
        the same number `max_loss` was computed from. A market order here would leave the
        journalled maximum loss describing a fill that has not happened yet -- the premium
        is the whole loss on a long option, so paying more than the sized price raises the
        maximum loss above the figure the risk budget approved, and nothing downstream
        would notice. `portfolio.working_order_risk` reads the same asymmetry from the
        other end: a market order has no limit price, so it is unmeasured risk and the
        whole entry side stands down until it fills.

        The accepted cost is a fill the agent does not chase. A limit at the sized price
        does not fill into a market that has moved up, and the pass ends with no position
        instead of a position bought above budget.
        """
        if limit_price <= 0:
            raise AlpacaMCPError(
                f"place_option_order needs a positive debit limit, got {limit_price}"
            )
        # Keyed here when the caller does not supply one, so no path can send an
        # opening order without a key -- same rule as the closing side.
        client_order_id = client_order_id or make_open_client_order_id(
            [{"symbol": symbol, "side": side}]
        )
        return await self._call(
            "place_option_order",
            {
                "symbol": symbol,
                "side": side,
                "qty": str(qty),  # upstream types qty as str, not int
                "type": "limit",
                "limit_price": f"{limit_price:.2f}",  # per share, positive = debit paid
                "time_in_force": "day",  # options accept nothing else
                "position_intent": "buy_to_open" if side == "buy" else "sell_to_close",
                "client_order_id": client_order_id,
            },
        )

    async def place_option_spread_order(
        self, long_symbol: str, short_symbol: str, qty: int, limit_price: float,
        client_order_id: str | None = None
    ) -> dict[str, Any]:
        """Buy a debit vertical as one multi-leg order.

        Sent as a limit order at the net debit, never a market order: the whole point
        of the structure is that the maximum loss is known before the fill, and a
        market multi-leg fill could pay more than the debit the strategy sized against.
        """
        client_order_id = client_order_id or make_open_client_order_id(
            [{"symbol": long_symbol, "side": "buy"}, {"symbol": short_symbol, "side": "sell"}]
        )
        return await self._call(
            "place_option_order",
            {
                "qty": str(qty),  # strategy multiplier; each leg is qty * ratio_qty
                "type": "limit",
                "time_in_force": "day",
                "limit_price": f"{limit_price:.2f}",  # positive = net debit paid
                "order_class": "mleg",
                # Parent-level, as on the closing side: `OrderLeg` has its own
                # client_order_id field and it is not used here.
                "client_order_id": client_order_id,
                "legs": [
                    {
                        "symbol": long_symbol,
                        "ratio_qty": "1",
                        "side": "buy",
                        "position_intent": "buy_to_open",
                    },
                    {
                        "symbol": short_symbol,
                        "ratio_qty": "1",
                        "side": "sell",
                        "position_intent": "sell_to_open",
                    },
                ],
            },
        )

    async def get_all_positions(self) -> list[dict[str, Any]]:
        return normalize_positions(unwrap_payload(await self._call("get_all_positions", {})))

    async def get_orders(self) -> list[dict[str, Any]]:
        """Every order still working, so the risk budget can count what is in flight.

        Three arguments, each of which changes the answer rather than decorating it:

        - `status="open"` because the default is already open, and saying so keeps a
          future default change from silently turning this into a history query.
        - `nested=True` rolls a multi-leg order's legs under the parent. Without it
          the legs come back as top-level rows and a two-leg spread would be counted
          twice -- once per leg -- against a budget that only has room for it once.
        - `limit=500` is the documented maximum; the default is 50. A silently
          truncated page is an under-count of the book, which is the direction that
          lets risk through. 500 working orders is far past anything this strategy
          produces, but if a paper account ever did hold more, the total would be
          wrong with no sign of it -- so a full page is followed, not assumed to be
          the whole book.

        A full page is followed with the `before_order_id` cursor rather than being
        raised on. Until 2026-08-26 a 500-row page raised out of `run_once` and took
        the process with it, which is the wrong failure for a loop that is supposed
        to run unattended: the pass that cannot measure risk is exactly the pass that
        should still be able to *close* positions. Paging is by order id and not by
        `after`/`until` because the spec marks the two mutually exclusive, and a
        timestamp cursor cannot separate two orders submitted in the same instant.
        `direction="desc"` is sent explicitly for the same reason `status` is: the
        cursor walks backwards from the newest order, and that only holds if the
        page it walks off is newest-first.

        Three things still end the walk with `OrderBookIncomplete` rather than a
        number, because each of them means the count would be short by an unknown
        amount: a payload that is not a list, a full page whose last row has no `id`
        to cursor from, and more than `MAX_ORDER_PAGES` full pages.
        """
        raw: list[Any] = []
        seen: set[str] = set()
        cursor: str | None = None
        for _ in range(self.MAX_ORDER_PAGES):
            args: dict[str, Any] = {
                "status": "open",
                "nested": True,
                "limit": self.ORDER_PAGE_SIZE,
                "direction": "desc",
            }
            if cursor:
                args["before_order_id"] = cursor
            page = unwrap_payload(await self._call("get_orders", args))
            if not isinstance(page, list):
                raise OrderBookIncomplete(
                    f"get_orders returned {type(page).__name__}, not a list of orders: the "
                    "working-order book cannot be counted, and an uncounted book under-states "
                    "portfolio risk"
                )
            for item in page:
                # The cursor is exclusive, so an id seen twice means the server
                # repeated a row rather than advancing. Counting it twice would
                # over-state risk; dropping it is the same row either way.
                item_id = str(item.get("id", "")) if isinstance(item, dict) else ""
                if item_id and item_id in seen:
                    continue
                if item_id:
                    seen.add(item_id)
                raw.append(item)
            # Measured on the *raw* page, not on what survives normalization: a page
            # of 500 orders that are all `filled` is still a full page, and the rows
            # behind it are the ones that are still working.
            if len(page) < self.ORDER_PAGE_SIZE:
                return normalize_orders(raw)
            last_id = str(page[-1].get("id", "")) if isinstance(page[-1], dict) else ""
            if not last_id:
                raise OrderBookIncomplete(
                    "get_orders returned a full page whose last order has no id: there is no "
                    "cursor to read the rest of the book from, and a partial book under-states "
                    "portfolio risk"
                )
            cursor = last_id
        raise OrderBookIncomplete(
            f"get_orders returned more than {self.MAX_ORDER_PAGES * self.ORDER_PAGE_SIZE} "
            "working orders; the book is larger than this agent will page through, and a "
            "partial book under-states portfolio risk"
        )

    async def place_option_close_order(
        self, legs: list[dict[str, Any]], qty: int, client_order_id: str | None = None
    ) -> dict[str, Any]:
        """Flatten one open structure: sell_to_close the longs, buy_to_close the shorts.

        Market, not limit, and that is deliberate: an exit that does not fill is not
        an exit. The entry is a limit order because the max loss has to be known
        before the money goes out; on the way out the position already exists and a
        resting limit that misses simply leaves it open into expiry -- exactly the
        risk the time stop exists to remove.

        What that choice costs is not left implicit: `exits.closing_crossing_cost`
        prices the same legs off the quote before this is called, and the difference
        against the marks the exit rule fired on rides into the journal as `crossing`.
        The order is unpriced; the decision to send it unpriced is not.

        A one-leg structure goes out as a single-leg order; two or more legs go out
        as one `mleg` order so the vertical is never left half-closed (which would
        turn a defined-risk spread into a naked short).

        Every close carries a `client_order_id` -- computed here when the caller does
        not supply one, so no path can send a closing order without one. See
        `make_close_client_order_id` for why a duplicate must be Alpaca's to refuse.
        """
        if not legs:
            raise AlpacaMCPError("place_option_close_order called with no legs")
        client_order_id = client_order_id or make_close_client_order_id(legs, qty)
        if len(legs) == 1:
            leg = legs[0]
            long_side = str(leg["side"]).lower() == "long"
            return await self._call(
                "place_option_order",
                {
                    "symbol": leg["symbol"],
                    "side": "sell" if long_side else "buy",
                    "qty": str(qty),
                    "type": "market",
                    "time_in_force": "day",
                    "position_intent": "sell_to_close" if long_side else "buy_to_close",
                    "client_order_id": client_order_id,
                },
            )
        return await self._call(
            "place_option_order",
            {
                "qty": str(qty),  # strategy multiplier; each leg closes qty * ratio_qty
                "type": "market",
                "time_in_force": "day",
                "order_class": "mleg",
                "legs": [_close_leg(leg) for leg in legs],
                "client_order_id": client_order_id,
            },
        )

    async def get_fills(self, after: str) -> list[dict[str, Any]]:
        """Every FILL activity since `after` (YYYY-MM-DD), oldest first.

        Paged, not truncated: `page_size` is capped at 100 upstream, and a
        silently short history would mis-pair opens with closes and hand the
        circuit breaker a wrong number rather than an obviously missing one.
        `page_token` is the id of the last activity of the previous page.
        """
        fills: list[dict[str, Any]] = []
        page_token: str | None = None
        for _ in range(self.MAX_ACTIVITY_PAGES):
            args: dict[str, Any] = {
                "activity_type": "FILL",
                "after": after,
                "direction": "asc",
                "page_size": self.ACTIVITY_PAGE_SIZE,
            }
            if page_token:
                args["page_token"] = page_token
            page = normalize_fills(unwrap_payload(await self._call("get_account_activities_by_type", args)))
            fills.extend(page)
            if len(page) < self.ACTIVITY_PAGE_SIZE or not page[-1]["id"]:
                return fills
            page_token = page[-1]["id"]
        raise AlpacaMCPError(
            f"get_account_activities_by_type returned more than "
            f"{self.MAX_ACTIVITY_PAGES * self.ACTIVITY_PAGE_SIZE} fills since {after}; "
            "realized P&L would be computed from a partial history."
        )


class MockAlpacaMCPClient(BaseAlpacaMCPClient):
    """Deterministic fake used by `--dry`. No network, no keys, no `mcp` package needed.

    Generates a mildly upward-drifting synthetic price series so the momentum strategy
    has something real to compute against, and a synthetic option chain spanning four
    expiries (two of them deliberately outside the default 7-21 day window) x seven
    strikes around spot, so contract selection has something to choose between -- a
    two-row chain would make any selection logic look correct.
    """

    # Timestamped at "today" so the reconciler counts them against today's budget;
    # the OCC symbol is a real one so the 100x contract multiplier applies.
    _FILL_SYMBOL = "SPY260910C00108700"
    DEFAULT_SEED_FILLS = [
        {
            "activity_type": "FILL", "id": "mockfill-open-1", "symbol": _FILL_SYMBOL,
            "side": "buy", "qty": "5", "price": "1.60", "order_id": "mock-order-a",
            "transaction_time": datetime.now(timezone.utc).replace(hour=14, minute=32, second=0, microsecond=0).isoformat(),
        },
        {
            "activity_type": "FILL", "id": "mockfill-close-1", "symbol": _FILL_SYMBOL,
            "side": "sell", "qty": "5", "price": "0.70", "order_id": "mock-order-b",
            "transaction_time": datetime.now(timezone.utc).replace(hour=15, minute=1, second=0, microsecond=0).isoformat(),
        },
    ]

    # Five expiries. 1 and 3 sit below the default 7-21 day entry window and 45 above
    # it, so contract selection has to reject as well as choose. The 1-day expiry also
    # exists so the seeded book's time-stop position can be a real row of this chain
    # rather than a symbol nothing quotes -- see `_seed_positions`.
    CHAIN_EXPIRY_DAYS = (1, 3, 10, 17, 45)
    CHAIN_STRIKE_OFFSETS = (-0.06, -0.04, -0.02, 0.0, 0.02, 0.04, 0.06)

    def __init__(
        self,
        seed_price: float = 100.0,
        chain_lookback: int = 11,
        seed_fills: list[dict[str, Any]] | None = None,
        seed_positions: list[dict[str, Any]] | None = None,
        seed_orders: list[dict[str, Any]] | None = None,
    ) -> None:
        self._seed_price = seed_price
        self._chain_lookback = chain_lookback
        self._orders: list[dict[str, Any]] = []
        self._seed_fills = self.DEFAULT_SEED_FILLS if seed_fills is None else seed_fills
        self._seed_open = seed_positions
        self._seed_orders = seed_orders or []
        self._closed_symbols: set[str] = set()
        self._client_order_ids: set[str] = set()

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def get_clock(self) -> dict[str, Any]:
        return {"is_open": True, "timestamp": datetime.now(timezone.utc).isoformat()}

    async def get_account_info(self) -> dict[str, Any]:
        return {"equity": "100000.00", "cash": "50000.00", "buying_power": "100000.00"}

    async def get_stock_bars(self, symbol: str, limit: int = 30) -> list[dict[str, Any]]:
        return self._bars(symbol, limit)

    def _bars(self, symbol: str, limit: int = 30) -> list[dict[str, Any]]:
        """The synthetic series, synchronously.

        Split out of `get_stock_bars` so the seeded position book can be built off the
        same spot the chain is centred on without an event loop -- `_seed_positions`
        runs from `get_all_positions`, but the numbers it needs are pure arithmetic.
        """
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
        return self._chain(symbol)

    def _chain(self, symbol: str) -> list[dict[str, Any]]:
        """The synthetic chain, synchronously. See `_bars` for why the split exists."""
        # Centred on the same series the agent computes momentum from, so strikes and
        # spot are consistent with each other.
        bars = self._bars(symbol, limit=self._chain_lookback)
        spot = bars[-1]["c"]
        now = datetime.now(timezone.utc)
        chain: list[dict[str, Any]] = []
        for days in self.CHAIN_EXPIRY_DAYS:
            expiry = (now + timedelta(days=days)).strftime("%Y-%m-%d")
            for offset in self.CHAIN_STRIKE_OFFSETS:
                strike = round(spot * (1 + offset), 1)
                # Crude but shaped-like-real premium: intrinsic value plus a time value
                # that grows with the square root of time AND decays as the strike moves
                # away from spot. The decay matters: with a flat time value every strike
                # of one expiry has the same extrinsic, so a vertical spread's net debit
                # collapses to pure intrinsic and a 2-wide spread looks like it costs 2
                # cents. This is a shape, not a pricing model -- see NOTES.md.
                atm_time_value = spot * 0.012 * (days / 14) ** 0.5
                sigma = spot * 0.02 * (days / 14) ** 0.5
                moneyness = (strike - spot) / sigma
                time_value = round(atm_time_value * exp(-0.5 * moneyness * moneyness), 2)
                for kind, intrinsic in (("call", max(spot - strike, 0.0)), ("put", max(strike - spot, 0.0))):
                    letter = "C" if kind == "call" else "P"
                    price = max(round(intrinsic + time_value, 2), 0.01)
                    # Quote shape, not a quote model: market makers work in ticks, so the
                    # width in *dollars* has a floor (a penny either side) and only grows
                    # proportionally once the premium is large. That floor is why cheap
                    # far-OTM strikes come out proportionally wide -- which is exactly the
                    # real behaviour the liquidity screen exists to catch.
                    half_width = max(0.01, round(price * 0.015, 2))
                    chain.append(
                        {
                            # Real OCC symbols use a 6-digit YYMMDD date, not YYYYMMDD --
                            # emitting the real format keeps --dry output parseable by
                            # parse_occ_symbol and honest in the demo.
                            "symbol": f"{symbol}{expiry[2:].replace('-', '')}{letter}{int(strike * 1000):08d}",
                            "type": kind,
                            "strike": strike,
                            "expiry": expiry,
                            # Floored at a penny: real chains quote deep OTM strikes at
                            # 0.01, and a 0.00 price would make position sizing meaningless.
                            "last_price": price,
                            "bid": max(round(price - half_width, 2), 0.01),
                            "ask": round(price + half_width, 2),
                        }
                    )
        return chain

    def _duplicate_refusal(self, client_order_id: str) -> dict[str, Any] | None:
        """The refusal Alpaca is modelled as returning for a key it has already seen.

        Returns None and claims the key when it is new. Shared by every order path so
        the entry side is refused exactly the way the exit side is -- a duplicate that
        only one of them catches is not a duplicate rule.
        """
        if client_order_id in self._client_order_ids:
            # Modelled on the real refusal, not observed: upstream `_post_order` turns
            # any API error into {"error": {message, http_status, detail}}, and 422 is
            # the documented bucket for a rejected request body. See NOTES.md.
            return {
                "error": {
                    "message": "API rejected the order",
                    "http_status": 422,
                    "detail": {"code": 42210000,
                               "message": f"client_order_id must be unique: {client_order_id}"},
                }
            }
        self._client_order_ids.add(client_order_id)
        return None

    async def place_option_order(
        self, symbol: str, side: str, qty: int, limit_price: float,
        client_order_id: str | None = None
    ) -> dict[str, Any]:
        if limit_price <= 0:
            raise AlpacaMCPError(
                f"place_option_order needs a positive debit limit, got {limit_price}"
            )
        client_order_id = client_order_id or make_open_client_order_id(
            [{"symbol": symbol, "side": side}]
        )
        refusal = self._duplicate_refusal(client_order_id)
        if refusal is not None:
            return refusal
        order = {
            "id": f"mock-{len(self._orders) + 1}",
            "client_order_id": client_order_id,
            "symbol": symbol,
            "side": side,
            "qty": qty,
            # Echoed like the spread path's, so a --dry journal shows the price the
            # order actually carried rather than implying it went out unpriced.
            "limit_price": f"{limit_price:.2f}",
            "status": "filled",
            "filled_at": datetime.now(timezone.utc).isoformat(),
        }
        self._orders.append(order)
        return order

    async def place_option_spread_order(
        self, long_symbol: str, short_symbol: str, qty: int, limit_price: float,
        client_order_id: str | None = None
    ) -> dict[str, Any]:
        client_order_id = client_order_id or make_open_client_order_id(
            [{"symbol": long_symbol, "side": "buy"}, {"symbol": short_symbol, "side": "sell"}]
        )
        refusal = self._duplicate_refusal(client_order_id)
        if refusal is not None:
            return refusal
        order = {
            "id": f"mock-{len(self._orders) + 1}",
            "client_order_id": client_order_id,
            "order_class": "mleg",
            "qty": qty,
            "limit_price": f"{limit_price:.2f}",
            "legs": [
                {"symbol": long_symbol, "ratio_qty": "1", "side": "buy", "position_intent": "buy_to_open"},
                {"symbol": short_symbol, "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"},
            ],
            "status": "filled",
            "filled_at": datetime.now(timezone.utc).isoformat(),
        }
        self._orders.append(order)
        return order

    def _seed_position(
        self, root: str, dte: int, kind: str, strike: float, side: str,
        qty: int, entry: float, current: float, qty_available: int | None = None,
    ) -> dict[str, Any]:
        """One wire-shaped `Position` row, in Alpaca's format (every number a string)."""
        expiry = (datetime.now(timezone.utc) + timedelta(days=dte)).strftime("%y%m%d")
        letter = "C" if kind == "call" else "P"
        sign = 1 if side == "long" else -1
        cost_basis = sign * entry * qty * 100
        unrealized = sign * (current - entry) * qty * 100
        return {
            "symbol": f"{root}{expiry}{letter}{int(round(strike * 1000)):08d}",
            "asset_class": "us_option",
            "side": side,
            "qty": f"{sign * qty}",  # Alpaca signs short quantities negative
            "qty_available": f"{sign * (qty if qty_available is None else qty_available)}",
            "avg_entry_price": f"{entry:.2f}",
            "current_price": f"{current:.2f}",
            "cost_basis": f"{cost_basis:.2f}",
            "unrealized_pl": f"{unrealized:.2f}",
            "unrealized_plpc": f"{(unrealized / abs(cost_basis)) if cost_basis else 0:.4f}",
        }

    def _chain_row(self, symbol: str, dte: int, kind: str, offset: float) -> dict[str, Any]:
        """One row of this mock's own chain, addressed by grid coordinates.

        Raises rather than returning None: every caller is seeding a demo book off
        coordinates that are supposed to exist, and a silent miss is exactly the bug
        this method was added to stop.
        """
        expiry = (datetime.now(timezone.utc) + timedelta(days=dte)).strftime("%Y-%m-%d")
        strike = round(self._bars(symbol, limit=self._chain_lookback)[-1]["c"] * (1 + offset), 1)
        for row in self._chain(symbol):
            if row["expiry"] == expiry and row["type"] == kind and row["strike"] == strike:
                return row
        raise AlpacaMCPError(
            f"no {symbol} {kind} at {strike} expiring {expiry} in the mock chain; "
            f"seeded positions must land on CHAIN_EXPIRY_DAYS x CHAIN_STRIKE_OFFSETS"
        )

    def _seed_on_chain(
        self, symbol: str, dte: int, kind: str, offset: float, side: str,
        qty: int, pnl_pct: float, qty_available: int | None = None,
    ) -> dict[str, Any]:
        """A seeded position whose symbol is a row of this mock's own chain.

        Two things have to be true at once for `--dry` to demonstrate the whole exit
        path, and before 2026-08-27 only the first was: the position has to sit
        squarely on one exit rule, AND it has to be quotable, because the crossing-cost
        measurement looks each closing leg up in the chain by OCC symbol. Positions
        invented at arbitrary strikes and expiries were never in the chain, so every
        close reported `no two-sided quote` and the measurement was undemonstrable.

        So the mark is taken from the chain row itself (its mid) and the entry price is
        back-solved from the P&L the demo wants to show: `entry = mid / (1 + pnl_pct)`.
        Applying the same ratio to every leg gives the *structure* that same percentage,
        because a vertical's P&L percentage is `net_current / net_entry - 1` and a
        common factor survives the subtraction. Rounding both legs to cents moves the
        result by a fraction of a point, which is why the callers below aim well past
        each threshold rather than at it.
        """
        row = self._chain_row(symbol, dte, kind, offset)
        current = round((row["bid"] + row["ask"]) / 2, 2)
        entry = max(round(current / (1 + pnl_pct), 2), 0.01)
        seeded = self._seed_position(
            symbol, dte, kind, row["strike"], side, qty, entry, current,
            qty_available=qty_available,
        )
        # The OCC symbol is rebuilt by `_seed_position` from the same inputs; if the two
        # spellings ever drift the leg silently stops being quotable, which is the whole
        # failure this method exists to prevent.
        assert seeded["symbol"] == row["symbol"], (seeded["symbol"], row["symbol"])
        return seeded

    def _seed_positions(self) -> list[dict[str, Any]]:
        """A book that exercises every exit rule, so `--dry` can demonstrate all of them.

        Without this, the exit policy would be code no reviewer can watch run: the
        real account these were modelled on doesn't exist yet. Every row is a real
        row of this mock's chain (see `_seed_on_chain`), so the closes it produces can
        also be priced against a two-sided quote instead of reporting `unquoted`.
        """
        return [
            # 1. Debit call vertical, +90% of net debit -> take profit.
            self._seed_on_chain("SPY", 17, "call", -0.02, "long", 5, 0.92),
            self._seed_on_chain("SPY", 17, "call", 0.02, "short", 5, 0.92),
            # 2. Naked long put, -64% -> stop loss.
            self._seed_on_chain("SPY", 10, "put", -0.02, "long", 3, -0.64),
            # 3. Long call expiring tomorrow, barely profitable -> time stop.
            self._seed_on_chain("QQQ", 1, "call", -0.02, "long", 2, 0.10),
            # 4. Debit call vertical, +4.5% and 45 days left -> hold, nothing to do.
            self._seed_on_chain("SPY", 45, "call", 0.0, "long", 4, 0.045),
            self._seed_on_chain("SPY", 45, "call", 0.04, "short", 4, 0.045),
            # 5. Deep loser whose close is already working (qty_available 0) -> skipped,
            #    not closed a second time.
            self._seed_on_chain("IWM", 10, "put", -0.02, "long", 1, -0.80, qty_available=0),
        ]

    async def get_orders(self) -> list[dict[str, Any]]:
        """Working orders, empty unless the caller seeded some.

        Empty is the honest default: `--dry` sends its orders through this same mock,
        which fills them instantly, so nothing this run submitted is ever still
        working. Seeding is how a test or a demo puts an in-flight order on the book.
        """
        return normalize_orders(self._seed_orders)

    async def get_all_positions(self) -> list[dict[str, Any]]:
        rows = self._seed_positions() if self._seed_open is None else self._seed_open
        return [
            pos for pos in normalize_positions(rows) if pos["symbol"] not in self._closed_symbols
        ]

    async def place_option_close_order(
        self, legs: list[dict[str, Any]], qty: int, client_order_id: str | None = None
    ) -> dict[str, Any]:
        client_order_id = client_order_id or make_close_client_order_id(legs, qty)
        refusal = self._duplicate_refusal(client_order_id)
        if refusal is not None:
            return refusal
        order = {
            "id": f"mock-{len(self._orders) + 1}",
            "client_order_id": client_order_id,
            "qty": qty,
            "type": "market",
            "status": "filled",
            "legs": [_close_leg(leg) for leg in legs],
            "filled_at": datetime.now(timezone.utc).isoformat(),
        }
        if len(legs) == 1:
            order["symbol"] = legs[0]["symbol"]
            order["side"] = order["legs"][0]["side"]
        self._orders.append(order)
        # A filled close removes the position from the book, so a second pass in the
        # same run cannot close it again.
        self._closed_symbols.update(leg["symbol"] for leg in legs)
        return order

    async def get_fills(self, after: str) -> list[dict[str, Any]]:
        """A canned round trip that closed at a loss earlier today.

        `--dry` has to be able to demonstrate the circuit breaker, and a breaker
        that never sees a loss is indistinguishable from one that is broken. The
        two fills below are one 5-lot debit-vertical long leg bought at $1.60 and
        sold at $0.70: a realized loss of 0.90 * 5 * 100 = $450.
        """
        return normalize_fills(self._seed_fills)
