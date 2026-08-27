"""
Exit policy: when to close what is already open, and how to route the close.

The entry side of this agent (strategy.py) only ever opens risk. Everything here
takes it back off, on three rules that are checked in a fixed order:

  1. Time stop  -- expiry is `close_before_dte` days away or nearer. Closed
     regardless of P&L: a long option left to expire is worth whatever it is worth
     at the bell, and a short leg left to expire can be assigned. Neither is a
     decision the strategy made, so neither is allowed to happen.
  2. Stop loss  -- the structure is down `stop_loss_pct` of what it cost.
  3. Take profit -- the structure is up `take_profit_pct` of what it cost.

Time stop is checked first on purpose: on the last day before expiry the position
comes off whether it is a winner or a loser.

Positions are evaluated as *structures*, not as loose legs. A debit vertical is a
long leg and a short leg that only have a defined risk together; closing one and
leaving the other would convert a capped-loss spread into an uncapped naked short.
So legs are grouped by (underlying, expiry, type), the P&L test runs on the group's
net cost basis, and the close goes out as a single multi-leg order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any


@dataclass
class ExitDecision:
    action: str  # "close" | "hold" | "skip"
    reason: str
    structure: str  # human-readable id, e.g. "SPY 2026-09-08 call"
    legs: list[dict[str, Any]] = field(default_factory=list)
    qty: int = 0
    unrealized_pl: float = 0.0
    cost_basis: float = 0.0
    pnl_pct: float | None = None  # None when cost basis is 0 or a net credit
    dte: int | None = None


def group_structures(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group option positions into the structures they were opened as.

    Key is (underlying, expiry, type): the legs of a vertical share all three
    and nothing else in a normal book does. Two verticals opened on different days
    at the same expiry and type do merge into one group -- they are the same
    directional bet on the same expiry, and closing them together is not wrong, but
    see NOTES.md for why that is a simplification and not a claim.
    """
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for pos in positions:
        groups.setdefault((pos["underlying"], pos["expiry"], pos["type"]), []).append(pos)
    structures = []
    for (underlying, expiry, kind), legs in sorted(groups.items()):
        legs = sorted(legs, key=lambda p: p["symbol"])
        structures.append(
            {
                "id": f"{underlying} {expiry} {kind}",
                "underlying": underlying,
                "expiry": expiry,
                "type": kind,
                "legs": legs,
                "cost_basis": round(sum(leg["cost_basis"] for leg in legs), 2),
                "unrealized_pl": round(sum(leg["unrealized_pl"] for leg in legs), 2),
            }
        )
    return structures


def days_to_expiry(expiry: str, today: date) -> int | None:
    try:
        return (datetime.strptime(str(expiry), "%Y-%m-%d").date() - today).days
    except (TypeError, ValueError):
        return None


def close_qty(legs: list[dict[str, Any]]) -> int:
    """Contracts to close: the smallest leg, so a multi-leg close is never lopsided.

    Legs of an untouched vertical are equal. If they are not -- a partial fill, or a
    leg closed by hand -- closing the common part never over-sells the smaller leg
    into a new short position. It does not, however, make the remainder safe: when
    the *short* leg is the larger one, the common part is exactly the covered part,
    and closing it strands the excess short with nothing behind it. That case never
    reaches here -- `ExitPolicy.evaluate` buys the excess back first, see
    `uncovered_short_contracts` -- but the quantity this returns is not by itself a
    claim that what is left over is balanced.
    """
    return int(min(leg["qty"] for leg in legs))


def contracts(leg: dict[str, Any], field: str = "qty") -> int:
    """Whole contracts off a position row. The wire sends them as strings of floats."""
    return int(round(float(leg[field])))


def uncovered_short_contracts(legs: list[dict[str, Any]]) -> int:
    """Short contracts in this group that no long is standing behind at all.

    Counted, not matched by strike. A short option is capped as long as some long of
    the same underlying, expiry and type is held against it -- below it (debit
    vertical, capped at the premium paid) or above it (credit vertical, capped at the
    strike width). The caps differ; both exist. What has no cap is a short with no
    long left, so the only question here is arithmetic.

    This is deliberately weaker than `portfolio._shorts_are_debit_covered`, which
    requires the long on the *debit* side. That one is stricter because it prices the
    book off cost basis and only the debit geometry is priceable that way; refusing
    to price a credit vertical is right. Buying one back would not be: a credit
    vertical is a defined-risk structure, and this exit path has no business
    unwinding a leg of it.
    """
    longs = sum(contracts(leg) for leg in legs if leg["side"] == "long")
    shorts = sum(contracts(leg) for leg in legs if leg["side"] == "short")
    return max(0, shorts - longs)


def most_exposed_short(legs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The short leg to buy back first, or None when a short strike is unreadable.

    Every uncovered short is unbounded, so buying back any of them removes the same
    number of uncapped contracts; which leg to take first is a question of how much
    of that unbounded range is already in play. For calls that is the lowest strike,
    for puts the highest.
    """
    shorts = [leg for leg in legs if leg["side"] == "short"]
    if not shorts or any(leg.get("strike") is None for leg in shorts):
        return None
    return sorted(shorts, key=lambda leg: float(leg["strike"]))[
        -1 if shorts[0]["type"] == "put" else 0
    ]


class ExitPolicy:
    def __init__(
        self,
        take_profit_pct: float = 0.75,
        stop_loss_pct: float = 0.50,
        close_before_dte: int = 1,
    ) -> None:
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.close_before_dte = close_before_dte

    def evaluate(self, structure: dict[str, Any], today: date | None = None) -> ExitDecision:
        today = today or datetime.now(timezone.utc).date()
        legs = structure["legs"]
        cost_basis = structure["cost_basis"]
        unrealized = structure["unrealized_pl"]
        dte = days_to_expiry(structure["expiry"], today)
        qty = close_qty(legs)
        # A debit structure has a positive net cost basis; only then is "down 50% of
        # what it cost" a number that means anything. Credit structures (or a zero
        # basis) still get the time stop, but no percentage test is invented for them.
        pnl_pct = unrealized / cost_basis if cost_basis > 0 else None
        base = dict(
            structure=structure["id"], legs=legs, qty=qty,
            unrealized_pl=unrealized, cost_basis=cost_basis, pnl_pct=pnl_pct, dte=dte,
        )

        naked = uncovered_short_contracts(legs)
        if naked > 0:
            return self._buy_back_naked_shorts(structure, legs, naked, base)

        blocked = [leg["symbol"] for leg in legs if leg["qty_available"] <= 0]
        if blocked:
            return ExitDecision(
                action="skip",
                reason=(
                    f"{structure['id']}: {', '.join(blocked)} has 0 contracts available "
                    "(a close is already working); not sending a second closing order."
                ),
                **base,
            )

        if qty <= 0:
            return ExitDecision(
                action="skip",
                reason=f"{structure['id']}: no closable quantity.",
                **base,
            )

        if dte is not None and dte <= self.close_before_dte:
            return ExitDecision(
                action="close",
                reason=(
                    f"time stop: {structure['id']} expires in {dte} day(s) "
                    f"(<= close_before_dte {self.close_before_dte}); closing {qty} "
                    f"contract(s) rather than carrying expiry/assignment risk. "
                    f"Unrealized P&L ${unrealized:.2f}."
                ),
                **base,
            )

        if pnl_pct is not None and pnl_pct <= -self.stop_loss_pct:
            return ExitDecision(
                action="close",
                reason=(
                    f"stop loss: {structure['id']} is down {pnl_pct:.1%} of its "
                    f"${cost_basis:.2f} cost (${unrealized:.2f}), at or past "
                    f"stop_loss_pct {self.stop_loss_pct:.0%}; closing {qty} contract(s)."
                ),
                **base,
            )

        if pnl_pct is not None and pnl_pct >= self.take_profit_pct:
            return ExitDecision(
                action="close",
                reason=(
                    f"take profit: {structure['id']} is up {pnl_pct:.1%} of its "
                    f"${cost_basis:.2f} cost (${unrealized:.2f}), at or past "
                    f"take_profit_pct {self.take_profit_pct:.0%}; closing {qty} contract(s)."
                ),
                **base,
            )

        pnl_note = f"{pnl_pct:.1%} of cost" if pnl_pct is not None else f"${unrealized:.2f}"
        dte_note = f"{dte} day(s) to expiry" if dte is not None else "expiry unreadable"
        return ExitDecision(
            action="hold",
            reason=(
                f"{structure['id']}: {pnl_note}, {dte_note} -- inside "
                f"[-{self.stop_loss_pct:.0%}, +{self.take_profit_pct:.0%}] and more than "
                f"{self.close_before_dte} day(s) from expiry; leaving it open."
            ),
            **base,
        )

    def _buy_back_naked_shorts(
        self,
        structure: dict[str, Any],
        legs: list[dict[str, Any]],
        naked: int,
        base: dict[str, Any],
    ) -> ExitDecision:
        """Take the uncapped part off before anything else, or say why it cannot.

        Checked ahead of the time stop and both P&L rules on purpose: those three
        decide whether a *defined-risk* structure has run its course, and none of
        them is a reason to leave an unbounded short standing while they think about
        it. The close goes out for the excess contracts only -- the covered part is
        still a spread and comes off through the normal path on a later pass, as one
        multi-leg order, which is the only way it can come off without being
        lopsided.
        """
        target = most_exposed_short(legs)
        held = 0 if target is None else contracts(target)
        available = 0 if target is None else contracts(target, "qty_available")
        detail = (
            f"{structure['id']}: {naked} short contract(s) have no long behind them "
            f"(an uncovered short's loss is unbounded)"
        )
        if target is None:
            return ExitDecision(
                action="skip",
                reason=f"{detail}, and no short leg here has a readable strike, so "
                       "which one to buy back is not decidable; not sending a close.",
                **base,
            )
        if held < naked:
            return ExitDecision(
                action="skip",
                reason=(
                    f"{detail}, and the most exposed short leg {target['symbol']} holds "
                    f"only {held} of them; buying them back needs one quantity per leg, "
                    "which a single close order cannot carry. Not sending a partial "
                    "one that would leave the rest naked."
                ),
                **base,
            )
        if available < naked:
            return ExitDecision(
                action="skip",
                reason=(
                    f"{detail}, and {target['symbol']} has only {available} of its "
                    f"{held} contract(s) available (a close is already working); not "
                    "sending a second closing order."
                ),
                **base,
            )
        return ExitDecision(
            action="close",
            reason=(
                f"naked short: {detail}. Buying back {naked} {target['symbol']} before "
                f"the P&L and expiry rules are consulted; closing the balanced part "
                f"first ({close_qty(legs)} contract(s)) would have left this excess "
                "standing alone."
            ),
            **dict(base, legs=[target], qty=naked),
        )


CONTRACT_MULTIPLIER = 100


def closing_crossing_cost(
    legs: list[dict[str, Any]], qty: int, quotes: dict[str, dict[str, float]]
) -> dict[str, Any]:
    """What crossing the spread on the way out costs, measured against the same marks.

    Every rule above decides on `unrealized_pl`, which the position book derives from
    `current_price` -- a mark. The close goes out as a market order, so what it will
    actually get is the other side of the quote: a long leg is sold into the bid, a
    short leg is bought back at the ask. The gap between the two numbers is neither
    slippage (nothing has filled yet) nor a fee; it is the part of the mark that was
    never on offer to this structure at this moment.

    Reported, never enforced. Refusing to close because the book is wide is the exact
    outcome the time stop exists to prevent, so nothing computed here can stand an
    exit down. A leg with no two-sided quote is named in `unquoted` and the arithmetic
    is withheld -- `quoted_proceeds` and `crossing_cost` stay None rather than being
    completed off half a quote.

    Sign convention: proceeds are dollars received to flatten, so a short leg being
    bought back subtracts. `crossing_cost` is normally positive, but it is a
    subtraction, not an absolute value: a mark struck below the bid (a stale last
    trade under a firm book) makes it negative, and that is a real thing to see in the
    journal rather than one to clamp away.
    """
    unquoted = [leg["symbol"] for leg in legs if leg["symbol"] not in quotes]
    shares = qty * CONTRACT_MULTIPLIER
    mark = 0.0
    quoted = 0.0
    widest: float | None = None
    for leg in legs:
        long_leg = leg["side"] == "long"
        sign = 1.0 if long_leg else -1.0
        mark += sign * float(leg.get("current_price") or 0.0)
        if unquoted:
            continue
        quote = quotes[leg["symbol"]]
        quoted += sign * (quote["bid"] if long_leg else quote["ask"])
        mid = (quote["bid"] + quote["ask"]) / 2
        if mid > 0:
            width = (quote["ask"] - quote["bid"]) / mid
            widest = width if widest is None else max(widest, width)
    result: dict[str, Any] = {
        "mark_proceeds": round(mark * shares, 2),
        "quoted_proceeds": None,
        "crossing_cost": None,
        "widest_leg_spread_pct": None if widest is None else round(widest, 4),
        "unquoted": unquoted,
    }
    if not unquoted:
        result["quoted_proceeds"] = round(quoted * shares, 2)
        result["crossing_cost"] = round(result["mark_proceeds"] - result["quoted_proceeds"], 2)
    return result


def crossing_note(crossing: dict[str, Any]) -> str:
    """One sentence naming what the market order is being sent into."""
    if crossing["unquoted"]:
        return (
            f"Exit pricing: no two-sided quote for {', '.join(crossing['unquoted'])}, so what "
            f"crossing the spread costs against the ${crossing['mark_proceeds']:.2f} mark is "
            "unmeasured on this pass; the close still goes out at market."
        )
    return (
        f"Exit pricing: the marks value flattening this at ${crossing['mark_proceeds']:.2f}, "
        f"the quotes at ${crossing['quoted_proceeds']:.2f} (longs sold into the bid, shorts "
        f"bought back at the ask) -- crossing costs ${crossing['crossing_cost']:.2f}, widest "
        f"leg {crossing['widest_leg_spread_pct']:.1%} wide. Sent at market anyway: a resting "
        "limit that misses leaves the position open into expiry."
    )
