"""
Realized P&L reconciliation from Alpaca FILL activities.

The circuit breaker in `MomentumRiskCapStrategy.decide()` halts new trades once
today's realized loss crosses a fraction of equity, and it reads that number from
`Journal.realized_loss_today()` -- which only sums `realized_pnl` fields that
something wrote. Until this module existed nothing wrote them, so the breaker
could never fire no matter how much the account lost.

The wire->flat normalization itself lives in `mcp_client.normalize_fills`, next to
the other normalizers; this module only does the accounting.

Wire shape verified 2026-08-24 against the OpenAPI spec vendored in
alpacahq/alpaca-mcp-server @ main (tree sha 803b07a3),
`src/alpaca_mcp_server/specs/trading-api.json`:
  - `GET /v2/account/activities/{activity_type}` -> MCP tool
    `get_account_activities_by_type`, path param `activity_type` (e.g. "FILL"),
    query params `date` / `until` / `after` / `direction` (asc|desc, default desc)
    / `page_size` (1-100, default 100) / `page_token`.
  - The 200 body is a bare JSON **array**, not an object with a key.
  - A FILL entry (schema `AccountTradingActivities`) is
    `{activity_type, id, order_id, order_status, symbol, side, qty, price,
      cum_qty, leaves_qty, transaction_time, type}` where `type` is
    "fill" | "partial_fill" and **qty / price are strings**.
  - `side` is only "buy" or "sell". There is NO position_intent on a fill, so
    open-vs-close has to be inferred by matching fills against each other.
  - `page_token` takes "the ID of the last activity from the last page".

Option premium uses the contract multiplier: the spec's `OptionContract.multiplier`
says "In standard contracts, the multiplier is always set to 100. For instance, if
a contract is traded at $1.50 and the multiplier is 100 [the premium is $150]".
"""

from __future__ import annotations

from datetime import date as date_cls
from typing import Any

from mcp_client import parse_occ_symbol


def contract_multiplier(symbol: str) -> int:
    """100 for an option symbol, 1 for a plain equity symbol.

    A fill's `price` is per share; an option contract covers 100 shares, so
    ignoring this understates every option P&L by a factor of 100 -- which would
    keep the circuit breaker asleep through a loss 100x its threshold.
    """
    return 100 if parse_occ_symbol(symbol) else 1


def realized_events(fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """FIFO-match fills per symbol into one realized-P&L event per closing fill.

    Both directions are matched, because a debit vertical opens one leg with a buy
    and the other with a *sell*: an unmatched sell opens a short lot, and the later
    buy that covers it realizes (entry - exit) instead of (exit - entry).

    A fill whose qty exceeds the open lots closes what it can and opens a new lot
    with the remainder. A fill that closes nothing produces no event.
    """
    open_lots: dict[str, list[dict[str, float]]] = {}
    events: list[dict[str, Any]] = []
    for fill in fills:
        symbol = fill["symbol"]
        lots = open_lots.setdefault(symbol, [])
        # +1 when this fill is long-side (buy), -1 when short-side (sell).
        direction = 1.0 if fill["side"] == "buy" else -1.0
        remaining = fill["qty"]
        multiplier = contract_multiplier(symbol)
        pnl = 0.0
        matched = 0.0
        entry_cost = 0.0
        while remaining > 0 and lots and lots[0]["direction"] != direction:
            lot = lots[0]
            take = min(remaining, lot["qty"])
            # Long lot closed by a sell: exit - entry. Short lot closed by a buy:
            # entry - exit. `lot["direction"]` is +1 for the long case.
            pnl += lot["direction"] * (fill["price"] - lot["price"]) * take * multiplier
            entry_cost += lot["price"] * take * multiplier
            matched += take
            remaining -= take
            lot["qty"] -= take
            if lot["qty"] <= 0:
                lots.pop(0)
        if matched > 0:
            events.append(
                {
                    "symbol": symbol,
                    "qty": matched,
                    "realized_pnl": round(pnl, 2),
                    "entry_cost": round(entry_cost, 2),
                    "exit_price": fill["price"],
                    "multiplier": multiplier,
                    "closed_at": fill["transaction_time"],
                    "closing_activity_id": fill["id"],
                    "closing_side": fill["side"],
                }
            )
        if remaining > 0:
            lots.append({"direction": direction, "qty": remaining, "price": fill["price"]})
    return events


def new_realized_events(
    fills: list[dict[str, Any]],
    logged_activity_ids: set[str],
    on_date: date_cls,
) -> list[dict[str, Any]]:
    """The events from `fills` that closed on `on_date` and aren't journalled yet.

    Two filters, both load-bearing:
      - `logged_activity_ids` keeps a re-run from logging the same close twice.
        The agent reconciles on every pass, so without it a $500 loss would look
        like $1,000 after two passes and trip the breaker on a phantom.
      - `on_date` (UTC) keeps last week's losses out of today's budget. The
        lookback window has to reach back far enough to see the *opening* fill of
        a position closed today, so it necessarily also returns older closes.
    """
    day = on_date.isoformat()
    return [
        event
        for event in realized_events(fills)
        if event["closed_at"].startswith(day)
        and event["closing_activity_id"] not in logged_activity_ids
    ]
