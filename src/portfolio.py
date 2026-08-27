"""What the book already has at risk, so the entry side can size against the account
rather than against one order at a time.

`strategy.risk_pct` caps a *single* decision at 1% of equity. Run the agent every
minute and that cap is satisfied by every order individually while the account
accumulates as much risk as there are passes in the session -- the limit was never
per-account, only per-ticket. This module supplies the missing number: the dollars
already committed by positions that are still open, which the strategy subtracts
from a separate portfolio-level budget before it sizes anything new.

Risk is read off the structure's *net cost basis*, and only when that basis is a
debit. What was paid for a debit structure is exactly what it can lose -- both legs
of a debit vertical expire worthless in the worst case, and a long option's floor
is zero. A net credit is a different animal: its worst case is set by the distance
between the strikes (or, for a naked short, by nothing at all), and neither is
recoverable from cost basis. This strategy never opens one, so rather than guess a
number for a structure it did not create, `structure_risk` returns None and the
caller stands the entry side down -- see `unpriceable` in `open_risk`.
"""

from __future__ import annotations

from typing import Any


def structure_risk(structure: dict[str, Any]) -> float | None:
    """Dollars this structure can still lose, or None when that is not computable.

    A positive net cost basis is a debit: it is the whole loss. Zero or negative
    means short legs paid for the long ones, and the worst case then depends on
    strike geometry this function is deliberately not guessing at.
    """
    cost_basis = float(structure["cost_basis"])
    if cost_basis <= 0:
        return None
    return round(cost_basis, 2)


def open_risk(structures: list[dict[str, Any]]) -> tuple[float, list[str], dict[str, float]]:
    """(total dollars at risk, structures whose risk is not computable, per-structure breakdown).

    The three come back together because a total with an unpriceable structure left
    out of it is a number that reads as complete and is not. A caller that ignores
    the second element is under-counting its own book.
    """
    total = 0.0
    unpriceable: list[str] = []
    breakdown: dict[str, float] = {}
    for structure in structures:
        risk = structure_risk(structure)
        if risk is None:
            unpriceable.append(
                f"{structure['id']}: net cost basis ${float(structure['cost_basis']):.2f} is not "
                f"a debit, so the most it can lose is set by strike width, not by what was paid"
            )
            continue
        breakdown[structure["id"]] = risk
        total += risk
    return round(total, 2), unpriceable, breakdown


# One option contract delivers 100 shares; the spec states the multiplier explicitly
# ("if a contract is traded at $1.50 and the multiplier is 100").
CONTRACT_MULTIPLIER = 100


def priced_as_net_debit(order: dict[str, Any]) -> bool:
    """True only for the two shapes whose maximum loss really is the net debit paid.

    "Option order + opening intent + a positive limit price" does NOT imply "the most
    it can lose is what it pays". Buy one 101 call and sell two 104s for a $1.00 net
    debit and the loss above 104 is unbounded, while a naive net-debit reading books
    it as $100. A roll that closes an old structure and opens a new one has the same
    problem from the other side: the closing leg's credit shrinks the parent's net
    debit, so the *smaller* the number this function would return, the *larger* the
    new position it is hiding.

    So the whitelist is positive, not a blacklist of shapes known to be bad:

    - a single-leg `buy_to_open` limit order -- a long option's floor is zero, so the
      premium is the whole loss;
    - a 1:1 debit vertical -- two legs on the same underlying, expiry and option type,
      one `buy_to_open` and one `sell_to_open`, with the long strike on the side that
      makes the structure a debit (below the short for calls, above it for puts).
      Both legs expiring worthless is then the worst case, and that costs the debit.

    Everything else -- ratio legs, three or more legs, mixed open/close intents,
    calendars, straddles, an unreadable strike, a bare `sell_to_open` -- is False, and
    the caller stands the entry side down instead of sizing against a made-up number.
    A ratio other than exactly 1 stands down as well: the vendored spec does not pin
    down how a multiplied strategy's parent limit price converts to dollars, and this
    strategy only ever sends 1:1, so guessing buys nothing and can only under-state.
    """
    legs = order.get("legs") or []
    if not legs:
        return str(order.get("position_intent", "")).lower() == "buy_to_open"
    if len(legs) != 2:
        return False
    if any(leg.get("strike") is None or leg.get("expiry") is None for leg in legs):
        return False
    if any(leg.get("ratio_qty") != 1 for leg in legs):
        return False
    by_intent = {str(leg.get("position_intent", "")).lower(): leg for leg in legs}
    if set(by_intent) != {"buy_to_open", "sell_to_open"}:
        return False
    long_leg, short_leg = by_intent["buy_to_open"], by_intent["sell_to_open"]
    if long_leg["underlying"] != short_leg["underlying"]:
        return False
    if long_leg["expiry"] != short_leg["expiry"]:
        return False
    if long_leg["option_type"] != short_leg["option_type"]:
        return False
    if long_leg["option_type"] == "call":
        return long_leg["strike"] < short_leg["strike"]
    return long_leg["strike"] > short_leg["strike"]


# Which side of the book a closing leg has to be reducing for the close to be real.
# A `sell_to_close` on a leg the account is not long does not close anything: it
# opens a short. Matching intent against the position's side is what tells the two
# apart, and an order that fails the match is not evidence of exposure coming off.
CLOSING_INTENT_SIDES = {"sell_to_close": "long", "buy_to_close": "short"}


def closing_legs(order: dict[str, Any]) -> list[tuple[str, float, str]] | None:
    """[(OCC symbol, contracts closed, position intent)], or None when unreadable.

    A multi-leg parent's `qty` counts structures, so each leg closes
    `ratio_qty x parent qty` contracts; a single-leg order closes its own quantity.
    Only the unfilled part counts -- what already filled has already left the book.
    """
    remaining = order.get("remaining_qty")
    if remaining is None or remaining <= 0:
        return None
    legs = order.get("legs") or []
    if not legs:
        symbols = order.get("symbols") or []
        if len(symbols) != 1 or not symbols[0]:
            return None
        intent = str(order.get("position_intent", "")).lower()
        return [(symbols[0], float(remaining), intent)]
    out = []
    for leg in legs:
        symbol = leg.get("symbol")
        ratio = leg.get("ratio_qty")
        if not symbol or ratio is None or ratio <= 0:
            return None
        intent = str(leg.get("position_intent", "")).lower()
        out.append((symbol, float(ratio) * float(remaining), intent))
    return out


def _shorts_are_debit_covered(
    option_type: str, longs: list[tuple[float, float]], shorts: list[tuple[float, float]]
) -> bool:
    """Every residual short contract has a long beside it on the debit side.

    Debit side means: for calls the protecting long sits *below* the short strike,
    for puts *above* it. That is the only geometry whose worst case is the premium
    already paid -- which is the number the position side reports off cost basis.
    A long on the other side (a credit vertical) does bound the loss, but at the
    strike width rather than at cost basis, so this returns False for it too and the
    caller stands down rather than quoting a total that is short that term.

    Greedy, hardest short first: for calls the lowest-strike short is the one with
    the fewest longs able to cover it, so it is matched first and given the highest
    long that still qualifies, leaving the more flexible low strikes for later.
    """
    pool = [[strike, qty] for strike, qty in longs]
    if option_type == "call":
        def protects(long_strike, short_strike):
            return long_strike < short_strike
    else:
        def protects(long_strike, short_strike):
            return long_strike > short_strike
    for strike, qty in sorted(shorts, reverse=(option_type == "put")):
        need = qty
        candidates = [entry for entry in pool if entry[1] > 0 and protects(entry[0], strike)]
        candidates.sort(key=lambda entry: entry[0], reverse=(option_type == "call"))
        for entry in candidates:
            used = min(entry[1], need)
            entry[1] -= used
            need -= used
            if need <= 1e-9:
                break
        if need > 1e-9:
            return False
    return True


def _residual_book(
    order: dict[str, Any], positions: list[dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], dict[str, float], set[tuple[str, str, str]]] | None:
    """(positions by symbol, contracts left per symbol, groups touched), or None.

    None means the close cannot be reconciled against the book at all -- a symbol the
    book does not show, an intent pointing the wrong way, more contracts than are
    held, a duplicate position row, a non-option close (`normalize_positions` keeps
    only options, so a stock close never matches). Both checks below start here
    because both are questions about the same object: the book as it will read once
    this order fills.
    """
    closing = closing_legs(order)
    if closing is None:
        return None
    by_symbol: dict[str, dict[str, Any]] = {}
    for position in positions:
        symbol = position.get("symbol")
        if symbol in by_symbol:
            # Two rows for one contract cannot be reconciled leg for leg, and
            # guessing which one the close reduces is exactly the guess this
            # module exists to refuse.
            return None
        by_symbol[symbol] = position
    residual = {symbol: float(position["qty"]) for symbol, position in by_symbol.items()}
    touched: set[tuple[str, str, str]] = set()
    for symbol, qty, intent in closing:
        position = by_symbol.get(symbol)
        if position is None:
            return None
        if CLOSING_INTENT_SIDES.get(intent) != position["side"]:
            return None
        if qty > residual[symbol] + 1e-9:
            return None
        residual[symbol] -= qty
        touched.add((position["underlying"], position["expiry"], position["type"]))
    return by_symbol, residual, touched


def _legs_in_group(
    by_symbol: dict[str, dict[str, Any]], group: tuple[str, str, str]
) -> list[dict[str, Any]]:
    return [
        position
        for position in by_symbol.values()
        if (position["underlying"], position["expiry"], position["type"]) == group
    ]


def close_leaves_no_uncovered_short(
    order: dict[str, Any], positions: list[dict[str, Any]]
) -> bool:
    """True when this close matches the book leg for leg and leaves nothing naked.

    A closing order used to be charged 0.0 unconditionally, on the reasoning that it
    takes off exposure the position side has already counted. That reasoning holds
    only when the close flattens the *structure*. Send a lone `sell_to_close` on the
    long leg of a debit vertical and what is left when it fills is a bare short call
    -- unbounded -- while the account still reads the pre-close debit off cost basis
    and the order itself reads 0.0. The risk is nowhere.

    So this asks one question, and only one: after subtracting the order's legs from
    the book, is any short left in a group it touched without a long protecting it on
    the debit side? A close that passes has been shown not to *uncap* anything. It has
    not been shown to be free -- that is the separate question
    `close_does_not_raise_booked_risk` asks, and both have to answer yes before
    `working_order_risk` will charge 0.0.
    """
    reconciled = _residual_book(order, positions)
    if reconciled is None:
        return False
    by_symbol, residual, touched = reconciled
    for group in touched:
        longs: list[tuple[float, float]] = []
        shorts: list[tuple[float, float]] = []
        for position in _legs_in_group(by_symbol, group):
            qty = residual[position["symbol"]]
            if qty <= 1e-9:
                continue
            bucket = longs if position["side"] == "long" else shorts
            bucket.append((float(position["strike"]), qty))
        if not shorts:
            continue
        if not _shorts_are_debit_covered(group[2], longs, shorts):
            return False
    return True


def booked_risk_change(
    order: dict[str, Any], positions: list[dict[str, Any]]
) -> dict[str, tuple[float, float]] | None:
    """{group id: (dollars booked now, dollars booked once this close fills)}, or None.

    The position side prices a structure off its *net* cost basis, so a close does not
    only remove legs -- it removes whichever side of the netting those legs were on.
    Buy back the short leg of a debit vertical and the credit that was holding the net
    basis down goes with it: the account's own number rises the moment the close fills.
    On the mock's book that is $440.00 -> $800.00 (2026-08-26 06:16).

    The residual basis is pro-rated by contract: a leg's cost basis divided across its
    contracts is what one of them cost, so the residual quantity carries that share.
    A group left with no contracts at all is 0.0 rather than unpriceable -- flat is a
    known number, not a missing one.

    None when the close cannot be reconciled leg for leg, or when either side of the
    comparison is not a debit: a group whose net basis is zero or a credit has no
    cost-basis risk figure to compare against, and inventing one here is the guess
    this module refuses everywhere else.
    """
    reconciled = _residual_book(order, positions)
    if reconciled is None:
        return None
    by_symbol, residual, touched = reconciled
    change: dict[str, tuple[float, float]] = {}
    for group in sorted(touched):
        legs = _legs_in_group(by_symbol, group)
        before = structure_risk({"cost_basis": round(sum(leg["cost_basis"] for leg in legs), 2)})
        if before is None:
            return None
        remaining_legs = [leg for leg in legs if residual[leg["symbol"]] > 1e-9]
        if not remaining_legs:
            after: float | None = 0.0
        else:
            after = structure_risk({
                "cost_basis": round(
                    sum(
                        leg["cost_basis"] * residual[leg["symbol"]] / float(leg["qty"])
                        for leg in remaining_legs
                    ),
                    2,
                )
            })
            if after is None:
                return None
        change[f"{group[0]} {group[1]} {group[2]}"] = (before, after)
    return change


def close_does_not_raise_booked_risk(
    order: dict[str, Any], positions: list[dict[str, Any]]
) -> bool:
    """True when no group this close touches ends up booked higher than it starts.

    `close_leaves_no_uncovered_short` proves the close does not uncap a structure.
    This proves the other half a budget needs: that the number the position side
    reports after the fill is not larger than the one it reports now. Charging 0.0 for
    an order that raises the account's own risk figure is the same error as the one
    the naked-short check fixed, pointed the other way.
    """
    change = booked_risk_change(order, positions)
    if change is None:
        return False
    # Both sides are rounded to the cent, so a half-cent tolerance only absorbs the
    # pro-rating's own float noise -- never a real rise.
    return all(after - before <= 0.005 for before, after in change.values())


def working_order_risk(
    order: dict[str, Any], positions: list[dict[str, Any]]
) -> float | None:
    """Dollars a still-working order can lose once it fills, or None when unknowable.

    A working order is not a position, so `structure_risk` above cannot see it -- and
    that was the hole: an entry sitting at `accepted` was invisible to the portfolio
    budget until the moment it filled, which is the moment it is too late for the
    budget to have an opinion. Idempotency keys stop the *same* structure being sent
    twice; they do nothing about the risk of the one copy that is genuinely working.

    - A closing order is 0.0 only when it clears two checks against the book it claims
      to be closing. `close_leaves_no_uncovered_short`: nothing is left naked once it
      fills -- a close that takes the *protective* leg off a vertical does not remove
      exposure, it uncaps it. `close_does_not_raise_booked_risk`: the dollars the
      position side reports after the fill are not higher than before it -- buying the
      short leg back removes the credit that was netting the basis down, so the book's
      own figure rises while the order is charged nothing. Passing both means the
      order removes exposure already counted on the position side, and adding its
      notional on top would bill the account twice for a structure it is shedding.
      Failing either, or being unmatchable at all, is None.
    - A debit limit price is the whole loss for the two topologies `priced_as_net_debit`
      admits -- a lone long option and a 1:1 debit vertical -- and for those it is
      price x 100 x the quantity that has not filled yet. The filled part is already
      a position and is already counted there. Any other leg geometry returns None:
      a net debit on a ratio spread or a roll is not a maximum loss at all, and
      reading it as one reports an unbounded structure as a small dollar figure.
    - No limit price (a market or stop order) is not zero risk, it is *unmeasured*
      risk -- the fill price is whatever the book gives it. Same for a net credit,
      whose worst case is strike geometry, and for a non-option order, which this
      module has no cost model for at all. All three return None so the caller stands
      down rather than sizing against a total it knows is short a term.
    """
    if not order.get("opening", True):
        if not close_leaves_no_uncovered_short(order, positions):
            return None
        return 0.0 if close_does_not_raise_booked_risk(order, positions) else None
    if not order.get("is_option"):
        return None
    remaining = order.get("remaining_qty")
    if remaining is None:
        return None
    if remaining <= 0:
        return 0.0
    limit_price = order.get("limit_price")
    if limit_price is None or limit_price <= 0:
        return None
    if not priced_as_net_debit(order):
        return None
    return round(limit_price * CONTRACT_MULTIPLIER * remaining, 2)


def _close_gap(order: dict[str, Any], positions: list[dict[str, Any]]) -> str:
    """Why this close could not be shown to be free, in the words of the check it failed.

    "Unpriceable" covers two different situations here and the operator reading the
    journal has to be able to tell them apart: one is a close we could not read at
    all, the other is a close we read fine and which makes the book worse.
    """
    if not close_leaves_no_uncovered_short(order, positions):
        return (
            "cannot be matched to the book leg for leg, so it cannot be shown to leave the "
            "structure flat rather than leaving a short without its protective leg"
        )
    rises = [
        f"{group} ${before:.2f} -> ${after:.2f}"
        for group, (before, after) in (booked_risk_change(order, positions) or {}).items()
        if after - before > 0.005
    ]
    return (
        "leaves nothing naked but raises the risk the position side books ("
        + "; ".join(rises)
        + "), which charging it 0.00 would hide"
    )


def working_risk(
    orders: list[dict[str, Any]], positions: list[dict[str, Any]]
) -> tuple[float, list[str], dict[str, float]]:
    """(dollars in flight, orders whose risk is not computable, per-order breakdown).

    Same three-part return as `open_risk`, and for the same reason: the caller needs
    to know what the total left out, not just what it added up.

    `positions` is not optional: a closing order can only be shown to remove risk by
    being matched against the book it claims to be closing, and a default of "no
    positions" would send every close down the unverified path silently.
    """
    total = 0.0
    unpriceable: list[str] = []
    breakdown: dict[str, float] = {}
    for order in orders:
        risk = working_order_risk(order, positions)
        key = order.get("client_order_id") or order.get("id") or "<unidentified order>"
        if risk is None and not order.get("opening", True):
            unpriceable.append(
                f"{key}: working close on {', '.join(order.get('symbols') or ['?'])} "
                f"{_close_gap(order, positions)}"
            )
            continue
        if risk is None:
            unpriceable.append(
                f"{key}: working {order.get('order_type') or 'unknown-type'} order on "
                f"{', '.join(order.get('symbols') or ['?'])} has no readable maximum loss "
                f"(limit price {order.get('limit_price')}, option={bool(order.get('is_option'))}, "
                f"legs={len(order.get('legs') or []) or 1}, intents="
                f"{'/'.join(str(leg.get('position_intent') or '?') for leg in order.get('legs') or []) or (order.get('position_intent') or '?')})"
            )
            continue
        if risk:
            breakdown[key] = risk
        total += risk
    return round(total, 2), unpriceable, breakdown
