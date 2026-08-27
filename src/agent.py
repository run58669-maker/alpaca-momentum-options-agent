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
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from exits import ExitPolicy, closing_crossing_cost, crossing_note, group_structures
from journal import Journal
from mcp_client import (
    AlpacaMCPClient,
    MockAlpacaMCPClient,
    OrderBookIncomplete,
    make_close_client_order_id,
    make_open_client_order_id,
)
from pnl import new_realized_events
from portfolio import open_risk, working_risk
from strategy import MomentumRiskCapStrategy

DEFAULT_JOURNAL_PATH = Path(__file__).parent.parent / "journal" / "decisions.jsonl"
# Must reach back past the opening fill of anything closed today; 30 calendar days
# covers every expiry this strategy opens (7-21 DTE).
DEFAULT_PNL_LOOKBACK_DAYS = 30
# The only order status that means "this close is done and its fills are on the tape".
# Everything else Alpaca can return -- new, accepted, pending_new, partially_filled,
# canceled, rejected, expired -- leaves either the position or its P&L unsettled.
CONFIRMED_ORDER_STATUSES = frozenset({"filled"})


async def reconcile_realized_pnl(client, journal: Journal, lookback_days: int) -> list[dict]:
    """Journal one `realized_pnl` record per position closed today, once each.

    This is the circuit breaker's only data source. It runs before every decision
    so the breaker is looking at losses that have already happened, not at the
    losses of the previous pass.
    """
    after = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).date().isoformat()
    fills = await client.get_fills(after)
    events = new_realized_events(fills, journal.logged_closing_ids(), datetime.now(timezone.utc).date())
    return [journal.log(**event) for event in events]


async def closing_quotes(client, journal: Journal, underlyings: set[str]) -> dict[str, dict]:
    """Two-sided quotes keyed by OCC symbol, for measuring what an exit crosses.

    A chain call that fails is journalled and skipped, not raised: this is measurement
    hanging off the exit path, and an exit that cannot be measured still has to go out.
    The legs it could not price then show up in the close's `unquoted` list.
    """
    quotes: dict[str, dict] = {}
    for underlying in sorted(underlyings):
        try:
            chain = await client.get_option_chain(underlying)
        except Exception as exc:  # must not block an exit -- see docstring
            journal.log(
                action="exit_quotes_unavailable",
                underlying=underlying,
                reason=(
                    f"could not read the {underlying} chain to price this pass's exits "
                    f"({type(exc).__name__}: {exc}); closes still go out at market, "
                    "with what they cross left unmeasured."
                ),
            )
            continue
        for row in chain:
            if "bid" in row and "ask" in row:
                quotes[row["symbol"]] = {"bid": row["bid"], "ask": row["ask"]}
    return quotes


async def manage_exits(client, journal: Journal, policy: ExitPolicy) -> list[dict]:
    """Close whatever the exit policy says should come off, and journal every verdict.

    Runs before the entry decision and, unlike the entry decision, is NOT gated by
    the circuit breaker: the breaker exists to stop new risk being taken, and taking
    risk off is the opposite of that. A tripped breaker that also froze the exits
    would leave the losing positions that tripped it open.

    Holds are journalled too. A position that stayed open is a decision, and the
    journal is meant to answer "why is this still on the book" as well as "why did
    you buy this".
    """
    positions = await client.get_all_positions()
    decisions = [(s, policy.evaluate(s)) for s in group_structures(positions)]
    # Quotes are fetched only for the underlyings something is actually being closed
    # on: a pass that holds everything must not pay for a chain call it will not read.
    quotes = await closing_quotes(
        client, journal, {s["underlying"] for s, d in decisions if d.action == "close"}
    )
    records = []
    for structure, decision in decisions:
        order = None
        client_order_id = None
        crossing = None
        reason = decision.reason
        if decision.action == "close":
            crossing = closing_crossing_cost(decision.legs, decision.qty, quotes)
            reason = f"{reason} {crossing_note(crossing)}"
            # Journalled alongside the order so the key that was sent is recoverable
            # from the record, not only recomputable from the position that is by then
            # gone -- that is what makes "did this close already go out?" answerable.
            client_order_id = make_close_client_order_id(decision.legs, decision.qty)
            order = await client.place_option_close_order(
                decision.legs, decision.qty, client_order_id
            )
        records.append(
            journal.log(
                action=f"exit_{decision.action}",
                structure=decision.structure,
                symbols=[leg["symbol"] for leg in decision.legs],
                qty=decision.qty,
                client_order_id=client_order_id,
                dte=decision.dte,
                cost_basis=decision.cost_basis,
                unrealized_pl=decision.unrealized_pl,
                pnl_pct=None if decision.pnl_pct is None else round(decision.pnl_pct, 4),
                reason=reason,
                # What the market order this pass sends is being sent into, recorded
                # next to the decision it belongs to rather than inferred later from a
                # fill price: the fill answers what was paid, not what was on offer.
                crossing=crossing,
                order=order,
                # A refused order is not a close. Recorded as its own field rather than
                # left inside the raw response, so "the exit policy said close" and
                # "the position actually came off" stay separable in the journal.
                order_rejected=None if order is None else bool(order.get("error")),
            )
        )
    return records


def unconfirmed_closes(exit_records: list[dict]) -> list[str]:
    """The closes decided this pass whose fills are not known to be on the tape.

    A close that is still working (or was refused) means two things at once: the
    position is still on the book, and the loss it is about to realize is not in
    today's realized-loss total. Opening a new position on top of that spends a
    risk budget computed from an incomplete number -- which is how a tripped
    circuit breaker gets walked past. So each one is named here and the entry side
    stands down until the next pass, by which time the fill has either landed (and
    is reconciled) or the order is gone.

    A rejection counts as unconfirmed on purpose. The duplicate-key refusal means
    an identical close is already out there in a state this agent cannot see; any
    other refusal means the exit policy asked for risk to come off and it did not.
    Neither is a book worth adding to.
    """
    pending = []
    for record in exit_records:
        if record.get("action") != "exit_close":
            continue
        structure = record.get("structure")
        order = record.get("order")
        if order is None:
            pending.append(f"{structure}: close decided but no order response came back")
            continue
        error = order.get("error")
        if error:
            pending.append(f"{structure}: close was refused ({error.get('message', 'no message')})")
            continue
        status = str(order.get("status") or "unknown").lower()
        if status not in CONFIRMED_ORDER_STATUSES:
            pending.append(f"{structure}: close is '{status}', not filled")
    return pending


async def run_once(client, strategy: MomentumRiskCapStrategy, journal: Journal, symbol: str,
                   pnl_lookback_days: int = DEFAULT_PNL_LOOKBACK_DAYS,
                   exit_policy: ExitPolicy | None = None) -> dict:
    clock = await client.get_clock()
    account = await client.get_account_info()
    equity = float(account["equity"])

    bars = await client.get_stock_bars(symbol, limit=strategy.lookback + 1)
    chain = await client.get_option_chain(symbol)
    exits = await manage_exits(client, journal, exit_policy) if exit_policy else []
    # Reconciliation runs *after* the exits, not before. A stop loss that fills on
    # this pass is a loss that has already happened by the time the entry side is
    # asked for a decision, and the circuit breaker only ever sees what has been
    # reconciled -- so reconciling first meant every loss the agent itself booked
    # arrived one pass late, and the trade that the breaker existed to stop went out
    # in between.
    closed = await reconcile_realized_pnl(client, journal, pnl_lookback_days)
    realized_loss = journal.realized_loss_today()

    # The book is read in two calls -- working orders, then positions -- and this
    # order is load-bearing, not incidental. A fill that lands between them is
    # counted twice: still working in the first snapshot, already a position in the
    # second. Reading positions first inverts that into a miss: the fill is not a
    # position yet when positions are read, and by the time orders are read it is
    # `filled` and dropped, so the risk appears in neither and the agent trades on
    # top of a structure it does not know it owns. Double-counting costs a trade;
    # missing costs an unbudgeted position, so the double-count is the side to be on.
    #
    # Orders first: an entry that is working but unfilled is not a position, so the
    # position side cannot see it, and until something does, the account cap can be
    # walked past by anything sitting at `accepted` -- the idempotency key stops the
    # same structure going out twice, not the risk of the one copy that is genuinely
    # live. Its worst case is knowable before the fill (a debit limit price is the
    # whole loss), so it is charged from the moment it is submitted.
    #
    # A book that cannot be read whole is not a fatal error here. It used to be: the
    # exception left `run_once` and ended the process, so the one condition under
    # which the agent must not open a position also stopped it closing any. The gap
    # is caught, named, and handed to the same stand-down path an unpriceable
    # structure takes -- the exits above have already gone out by this point.
    try:
        working_orders = await client.get_orders()
        order_book_gap = None
    except OrderBookIncomplete as exc:
        working_orders = []
        order_book_gap = f"working order book: {exc}"
    # Positions are re-read *after* the exits, not taken from the copy `manage_exits`
    # worked from: anything that just closed is risk that is no longer committed, and
    # sizing against the pre-exit book would refuse trades the account has room for.
    positions = await client.get_all_positions()
    held_risk, unpriceable, risk_by_structure = open_risk(group_structures(positions))
    # The same snapshot feeds both sides on purpose. A working *close* is only worth
    # zero if it can be shown to leave the structure flat, and that can only be shown
    # against the book -- a close of the long leg alone leaves a naked short whose
    # loss is unbounded while both sides of this arithmetic still read the old debit.
    in_flight, working_unpriceable, risk_by_order = working_risk(working_orders, positions)
    committed = round(held_risk + in_flight, 2)
    unpriceable = unpriceable + working_unpriceable
    # An unreadable order book fails the budget the same way an unpriceable
    # structure does -- the total is short by an unknown amount -- so it joins the
    # same list rather than getting a second stand-down mechanism beside it. The
    # in-flight term is 0.0 on this pass and that zero is not a measurement.
    if order_book_gap:
        unpriceable = unpriceable + [order_book_gap]

    decision = strategy.decide(
        symbol=symbol,
        bars=bars,
        option_chain=chain,
        equity=equity,
        realized_loss_today=realized_loss,
        open_risk=committed,
    )

    # Reconciliation above is only complete if every close it should have seen has
    # actually filled. When one has not, today's realized loss is a number that is
    # still moving, and no new risk goes out on top of it this pass.
    blocked_by_exits = unconfirmed_closes(exits)
    # `committed` is only a portfolio limit if it counts the whole portfolio. A
    # structure whose maximum loss is not readable off its cost basis leaves the
    # total short by an unknown amount, so the entry side stands down for the same
    # reason it does on an unconfirmed exit: the budget is being read off a number
    # that is missing a term.
    stood_down = bool(blocked_by_exits or unpriceable)

    order = None
    entry_client_order_id = None
    if decision.action != "hold" and not stood_down:
        # Order the exact contract(s) the strategy reasoned about, so the filled
        # symbols always match the ones named in the journalled reason.
        if decision.short_contract is not None:
            legs = [
                {"symbol": decision.contract["symbol"], "side": "buy"},
                {"symbol": decision.short_contract["symbol"], "side": "sell"},
            ]
        else:
            legs = [{"symbol": decision.contract["symbol"], "side": "buy"}]
        # Computed here rather than left to the client to default, so the key that
        # was sent is journalled next to the order -- "did this entry already go
        # out?" has to be answerable from the record.
        entry_client_order_id = make_open_client_order_id(legs)
        if decision.short_contract is not None:
            order = await client.place_option_spread_order(
                decision.contract["symbol"],
                decision.short_contract["symbol"],
                decision.contracts,
                decision.net_debit,
                entry_client_order_id,
            )
        else:
            order = await client.place_option_order(
                decision.contract["symbol"], "buy", decision.contracts,
                # The same per-share number `decision.max_loss` was computed from
                # (for a lone long, `net_debit` is the bare premium), so the order
                # cannot fill above the loss the risk budget approved.
                decision.net_debit,
                entry_client_order_id,
            )

    # A refused order bought nothing. The risk fields describe risk actually taken,
    # so they go to zero here for the same reason they do on a stand-down -- an
    # order the API rejected is not a position, and a journal that books its max
    # loss anyway is a journal that over-states the book.
    entry_rejected = bool(order and order.get("error"))

    reason = decision.reason
    if blocked_by_exits:
        reason = (
            f"not opening: {len(blocked_by_exits)} exit(s) this pass are not confirmed filled "
            f"({'; '.join(blocked_by_exits)}). Until they are, the position is still on the book "
            f"and its P&L is not in today's realized loss (${realized_loss:.2f}), so the circuit "
            f"breaker is reading an incomplete number. Standing down until the next pass. "
            f"Entry signal was: {decision.reason}"
        )
    elif order_book_gap:
        reason = (
            f"not opening: the working-order book could not be read whole "
            f"({order_book_gap}). Orders that are working but unfilled are risk this pass "
            f"cannot see, so the ${committed:.2f} it measured counts positions only and the "
            f"headroom it implies is not real. Exits still ran this pass -- they only take "
            f"risk off. Standing down on the entry side until the book reads whole. "
            f"Entry signal was: {decision.reason}"
        )
    elif unpriceable:
        reason = (
            f"not opening: {len(unpriceable)} open structure(s) or working order(s) have a "
            f"maximum loss this agent cannot compute ({'; '.join(unpriceable)}), so the "
            f"${committed:.2f} of portfolio "
            f"risk it did measure is an undercount and the headroom it implies is not real. "
            f"Standing down until the book is priceable again. "
            f"Entry signal was: {decision.reason}"
        )
    elif entry_rejected:
        reason = (
            f"not opened: the API refused this order "
            f"({order['error'].get('detail', {}).get('message') or order['error'].get('message')}). "
            f"The idempotency key {entry_client_order_id} identifies this structure on this UTC "
            f"day, so a refusal means an identical opening order is already out there -- working, "
            f"filled, or in a state this pass cannot see. Nothing was bought and no risk was "
            f"added. Entry signal was: {decision.reason}"
        )

    record = journal.log(
        symbol=symbol,
        market_open=clock.get("is_open"),
        equity=equity,
        momentum_pct=round(decision.momentum_pct, 4),
        action="hold" if stood_down else decision.action,
        contracts=0 if (stood_down or entry_rejected) else decision.contracts,
        # The worst case in dollars, computed before the order went out. On a hold
        # this is 0.0 -- no new risk was taken. Same on a refused order.
        max_loss=0.0 if (stood_down or entry_rejected) else round(decision.max_loss, 2),
        # The key this entry was sent under, or None when nothing was sent.
        client_order_id=entry_client_order_id,
        # Kept separate from the raw response for the same reason the exit side
        # does it: "the strategy wanted this trade" and "the trade happened" have
        # to stay separable in the record.
        order_rejected=entry_rejected,
        # What the rest of the book already has at risk, and which structures it is
        # in. Written on every pass, hold or not: the number the sizing was done
        # against has to be recoverable from the record, not only re-derivable from
        # a book that has moved on since.
        open_risk=committed,
        open_risk_by_structure=risk_by_structure,
        # Split out from the total because "held" and "in flight" fail differently:
        # a wrong held number means the position math is wrong, a wrong in-flight
        # number usually means an order settled between the two reads.
        held_risk=held_risk,
        working_risk=in_flight,
        working_risk_by_order=risk_by_order,
        unpriceable_risk=unpriceable,
        # None on a normal pass. When set, `working_risk` above is 0.0 because the
        # book was unreadable, not because nothing was in flight -- the two look
        # identical in the total and must not look identical in the record.
        order_book_gap=order_book_gap,
        # Empty on a normal pass. Named structures when the entry side was stood
        # down by an exit that had not settled, so "why did it not trade" is
        # answerable from the record rather than only from the reason string.
        blocked_by_exits=blocked_by_exits,
        reason=reason,
        order=order,
    )
    record["closed_positions"] = closed
    record["exits"] = exits
    return record


async def main_async(args: argparse.Namespace) -> None:
    client = MockAlpacaMCPClient() if args.dry else AlpacaMCPClient()
    strategy = MomentumRiskCapStrategy(
        lookback=args.lookback,
        momentum_threshold=args.momentum_threshold,
        risk_pct=args.risk_pct,
        max_contracts=args.max_contracts,
        max_daily_loss_pct=args.max_daily_loss_pct,
        max_portfolio_risk_pct=args.max_portfolio_risk_pct,
        spread_width_pct=args.spread_width_pct,
        max_spread_pct=args.max_spread_pct,
    )
    journal = Journal(args.journal_path)
    exit_policy = None if args.no_exits else ExitPolicy(
        take_profit_pct=args.take_profit_pct,
        stop_loss_pct=args.stop_loss_pct,
        close_before_dte=args.close_before_dte,
    )

    async with client:
        for i in range(args.iterations):
            record = await run_once(client, strategy, journal, args.symbol,
                                    args.pnl_lookback_days, exit_policy)
            # Printed in the order they run: exits go out first, then reconciliation
            # reads the tape they just wrote to.
            for ex in record["exits"]:
                print(f"    {ex['action']}: {ex['reason']}")
                if ex["order_rejected"]:
                    print(f"      REJECTED (not closed): {ex['order']['error']['message']}"
                          f" -- client_order_id {ex['client_order_id']}")
                elif ex["client_order_id"]:
                    print(f"      idempotency key: {ex['client_order_id']}")
            for closed in record["closed_positions"]:
                print(f"    reconciled: {closed['symbol']} qty={closed['qty']:g} closed at"
                      f" ${closed['exit_price']:.2f}, realized P&L ${closed['realized_pnl']:.2f}")
            print(f"[{i + 1}/{args.iterations}] {record['action']} {record['symbol']}"
                  f" contracts={record['contracts']} momentum={record['momentum_pct']:.2%}"
                  f" max_loss=${record['max_loss']:.2f}"
                  f" open_risk=${record['open_risk']:.2f}")
            print(f"    reason: {record['reason']}")
            if record["order_rejected"]:
                print(f"      REJECTED (not opened): {record['order']['error']['message']}"
                      f" -- client_order_id {record['client_order_id']}")
            elif record["client_order_id"]:
                print(f"      idempotency key: {record['client_order_id']}")
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
    p.add_argument("--max-portfolio-risk-pct", type=float, default=0.05, dest="max_portfolio_risk_pct",
                    help="Account-level cap: total maximum loss of all open positions, as a "
                         "fraction of equity (default: 0.05 = 5%%). --risk-pct caps one order; "
                         "this caps the book, so repeated passes cannot stack risk_pct N times. "
                         "Untuned placeholder -- see NEXT.md.")
    p.add_argument("--spread-width-pct", type=float, default=0.03, dest="spread_width_pct",
                    help="Target width of the debit vertical, as a fraction of spot "
                         "(default: 0.03 = 3%%). 0 disables spreads and trades the long leg naked.")
    p.add_argument("--max-spread-pct", type=float, default=0.10, dest="max_spread_pct",
                    help="Liquidity screen: refuse any contract quoting a bid-ask spread wider "
                         "than this fraction of its own midpoint (default: 0.10 = 10%%). "
                         "0 disables the screen. Untuned placeholder, like the momentum "
                         "threshold -- see NEXT.md.")
    p.add_argument("--pnl-lookback-days", type=int, default=DEFAULT_PNL_LOOKBACK_DAYS, dest="pnl_lookback_days",
                    help="How far back to pull FILL activities when reconciling realized P&L "
                         "(default: %(default)s). Must reach back to the opening fill of anything closed "
                         "today, or that close is mistaken for a new short position.")
    p.add_argument("--take-profit-pct", type=float, default=0.75, dest="take_profit_pct",
                    help="Close an open structure once it is up this fraction of what it cost "
                         "(default: 0.75 = 75%%).")
    p.add_argument("--stop-loss-pct", type=float, default=0.50, dest="stop_loss_pct",
                    help="Close an open structure once it is down this fraction of what it cost "
                         "(default: 0.50 = 50%%).")
    p.add_argument("--close-before-dte", type=int, default=1, dest="close_before_dte",
                    help="Close any structure this many days from expiry or nearer, win or lose, "
                         "rather than carry expiry/assignment risk (default: 1).")
    p.add_argument("--no-exits", action="store_true", dest="no_exits",
                    help="Skip exit management entirely (open-only). Positions are then left to "
                         "expire, so this is for debugging the entry path, not for a real run.")
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
