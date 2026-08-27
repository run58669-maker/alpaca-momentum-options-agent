"""
Explainable momentum strategy with a hard risk cap.

Deliberately simple and auditable — every number the strategy produces comes with
a plain-English reason string, because the journal logs that reason verbatim.

Rules (all thresholds are constructor args, not magic numbers buried in logic):
  1. Momentum = % change of close price over `lookback` bars.
  2. If |momentum| < `momentum_threshold` -> HOLD (signal too weak to act on).
  3. If momentum >= threshold -> bullish -> buy a call.
     If momentum <= -threshold -> bearish -> buy a put.
  3b. Contract choice is explicit, not "first row of the chain": keep only contracts
     of the wanted type whose expiry falls in [min_dte, max_dte] days out, then take
     the strike nearest spot (near-ATM), tie-broken by the expiry nearest the middle
     of the window and then by contract symbol, so the pick is deterministic.
     Nothing left in the window -> HOLD with a reason naming what was available.
  3c. Defined risk: having picked the long leg, look for a second contract of the
     same type and expiry, farther out of the money, cheaper, whose distance from
     the long strike is nearest `spread_width_pct` of spot. Selling it turns the
     naked long into a debit vertical spread whose maximum loss is the net debit,
     known before the order is sent. No such leg -> fall back to the naked long
     and say so in the reason.
  3c-i. Except when every candidate second leg was *refused* by the liquidity screen
     (3d): then HOLD. The naked long costs the full premium per contract, so the same
     budget goes further into it than into the spread it replaces (measured: 90% of
     the budget vs 77%, `scratch/measure_naked_fallback_20260826_1710.txt` plus the
     repricing in `test_the_naked_fallback_would_have_spent_more_of_the_budget`). Answering
     "that leg is too illiquid to trade" by committing more of the budget to an
     unhedged position is escalation after a refusal. A chain that simply offers no
     second leg refused nothing, so that case still trades naked.
  3d. Liquidity screen: a contract whose bid-ask spread is wider than
     `max_spread_pct` of its own midpoint is dropped from both 3b and 3c before
     ranking. A quoted price is not a price you can get out at, and the strategy
     sizes and stops on prices it expects to trade against. Contracts the chain
     carries no two-sided quote for are *not* dropped -- a missing quote is missing
     data, not evidence of a wide market -- but the count is reported in the reason
     so an unscreened pick is never silent.
  4. Position size = risk_pct of account equity, converted to option contracts
     at the price actually paid per share -- the net debit for a spread, the
     premium for a naked long (option prices are per-share; Alpaca contracts are
     100 shares, hence the *100) -- floored at 0 and capped at `max_contracts`.
  5. If today's realized loss (from the journal) already exceeds `max_daily_loss_pct`
     of equity -> force HOLD regardless of signal (circuit breaker).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any


def _dte(contract: dict[str, Any], today: date) -> int | None:
    """Days to expiry, or None if the row's expiry is missing or unparseable."""
    try:
        return (datetime.strptime(str(contract["expiry"]), "%Y-%m-%d").date() - today).days
    except (KeyError, TypeError, ValueError):
        return None


def _strike(contract: dict[str, Any]) -> float | None:
    try:
        return float(contract["strike"])
    except (KeyError, TypeError, ValueError):
        return None


def relative_spread(contract: dict[str, Any]) -> float | None:
    """`(ask - bid) / midpoint` for one chain row, or None if it has no usable quote.

    Relative rather than absolute: a $0.04 market is tight on a $6.00 contract and
    ruinous on a $0.06 one. None means "cannot be measured" -- callers must not
    read it as zero.
    """
    try:
        bid = float(contract["bid"])
        ask = float(contract["ask"])
    except (KeyError, TypeError, ValueError):
        return None
    mid = (bid + ask) / 2
    if mid <= 0 or ask < bid:
        return None
    return (ask - bid) / mid


def marketable_price(contract: dict[str, Any], side: str) -> float | None:
    """Price per share this side of the trade has to pay, or None if unusable.

    `side` is "buy" or "sell". A buyer lifts the ask and a seller hits the bid.
    `last_price` is the price of somebody else's trade at some earlier moment, so an
    order priced off it is either unfillable (a debit limit under the offer) or sized
    and stopped against a debit smaller than the one the fill will actually cost.
    Rows the chain carries no two-sided quote for fall back to `last_price` because
    there is nothing better to price them off; the liquidity screen already reports
    such a pick as unscreened.
    """
    if side not in ("buy", "sell"):
        raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")
    if relative_spread(contract) is not None:
        return float(contract["ask" if side == "buy" else "bid"])
    try:
        return float(contract["last_price"])
    except (KeyError, TypeError, ValueError):
        return None


@dataclass
class Decision:
    action: str  # "buy_call" | "buy_put" | "hold"
    contracts: int
    reason: str
    momentum_pct: float
    # The contract this decision is about, or None on a hold. Callers must order
    # exactly this contract -- re-deriving it from the chain lets the order drift
    # away from the contract the logged reason names.
    contract: dict[str, Any] | None = None
    # Set only when the decision is a vertical spread: the leg sold against
    # `contract`. `net_debit` is the per-share price paid for the pair (the bare
    # premium when there is no short leg) and `max_loss` is the most this decision
    # can lose in dollars -- for a debit structure, price x 100 x contracts.
    short_contract: dict[str, Any] | None = None
    net_debit: float = 0.0
    max_loss: float = 0.0


class MomentumRiskCapStrategy:
    def __init__(
        self,
        lookback: int = 10,
        momentum_threshold: float = 0.02,
        risk_pct: float = 0.01,
        max_contracts: int = 5,
        max_daily_loss_pct: float = 0.03,
        max_portfolio_risk_pct: float = 0.05,
        min_dte: int = 7,
        max_dte: int = 21,
        spread_width_pct: float = 0.03,
        max_spread_pct: float = 0.10,
    ) -> None:
        self.lookback = lookback
        self.momentum_threshold = momentum_threshold
        self.risk_pct = risk_pct
        self.max_contracts = max_contracts
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_portfolio_risk_pct = max_portfolio_risk_pct
        self.min_dte = min_dte
        self.max_dte = max_dte
        self.spread_width_pct = spread_width_pct
        self.max_spread_pct = max_spread_pct

    def screen_liquidity(self, contracts: list[dict]) -> tuple[list[dict], int, int]:
        """Split a candidate list into (tradable, n_rejected, n_unquoted).

        Rejected = a measurable bid-ask spread wider than `max_spread_pct` of the
        midpoint. Unquoted rows are kept in `tradable` but counted separately so the
        caller can say how much of its pick was actually screened.
        `max_spread_pct <= 0` disables the screen entirely.
        """
        if self.max_spread_pct <= 0:
            return list(contracts), 0, 0
        kept, rejected, unquoted = [], 0, 0
        for c in contracts:
            if relative_spread(c) is None:
                unquoted += 1
                kept.append(c)
            elif self.too_wide(c):
                rejected += 1
            else:
                kept.append(c)
        return kept, rejected, unquoted

    def too_wide(self, contract: dict) -> bool:
        """True only when the contract has a quote and that quote is wider than the cap.

        Unquoted and screen-disabled both come back False, so a caller can use this as
        the single "refuse it" test without re-implementing either exemption.
        """
        if self.max_spread_pct <= 0:
            return False
        width = relative_spread(contract)
        # The cap is a limit, not a strict inequality, so a contract sitting exactly on
        # it must pass. Binary floats do not make that free: a 0.95/1.05 market measures
        # 0.10000000000000009, which without the tolerance would be rejected by a 0.10
        # cap. The epsilon is far below any width anyone would set a cap at.
        return width is not None and width > self.max_spread_pct + 1e-9

    def _liquidity_note(self, contract: dict, n_rejected: int, n_unquoted: int) -> str:
        """The clause appended to a pick's reason so the screen is auditable after the fact.

        Always states what the chosen contract's own market was, because "it passed"
        and "it was never measured" have to be told apart in the journal.
        """
        if self.max_spread_pct <= 0:
            return "; liquidity screen off (max_spread_pct=0)"
        width = relative_spread(contract)
        if width is None:
            own = "its own market is unquoted (unscreened)"
        else:
            own = (
                f"its market is ${contract['bid']:.2f}/${contract['ask']:.2f} = "
                f"{width:.1%} wide vs the {self.max_spread_pct:.0%} cap"
            )
        parts = [own]
        if n_rejected:
            parts.append(f"{n_rejected} rejected as too wide")
        if n_unquoted:
            parts.append(f"{n_unquoted} unquoted and left unscreened")
        return "; " + ", ".join(parts)

    def _pricing_note(self, long_contract: dict, short: dict | None) -> str:
        """Names, per leg, which side of that leg's market the debit was struck at.

        `marketable_price` falls back to `last_price` on an unquoted row, and the two
        cases have to be told apart after the fact: one is a price the order can trade
        against, the other is the record of somebody else's trade.
        """
        parts = []
        for c, side in ((long_contract, "buy"), (short, "sell")):
            label = "long" if side == "buy" else "short"
            if c is None:
                continue
            last = float(c["last_price"])
            if relative_spread(c) is None:
                parts.append(f"{label} leg unquoted, priced off last ${last:.2f}")
            else:
                book = "offer" if side == "buy" else "bid"
                price = float(c["ask" if side == "buy" else "bid"])
                parts.append(f"{label} leg at the {book} ${price:.2f} (last ${last:.2f})")
        return ", ".join(parts)

    def compute_momentum(self, bars: list[dict]) -> float:
        closes = [b["c"] for b in bars[-self.lookback :]]
        if len(closes) < 2:
            return 0.0
        return (closes[-1] - closes[0]) / closes[0]

    def size_position(self, equity: float, option_price: float,
                      budget: float | None = None) -> int:
        """Contracts affordable within `budget` dollars, defaulting to risk_pct of equity.

        The explicit budget is what lets the caller hand in the *smaller* of the
        per-trade cap and whatever is left of the portfolio-level cap, without this
        function needing to know which of the two was binding.
        """
        if option_price <= 0:
            return 0
        risk_budget = equity * self.risk_pct if budget is None else budget
        if risk_budget <= 0:
            return 0
        contracts = int(risk_budget // (option_price * 100))
        return max(0, min(contracts, self.max_contracts))

    def select_contract(
        self,
        chain: list[dict],
        want_type: str,
        spot: float,
        as_of: date | None = None,
    ) -> tuple[dict | None, str]:
        """Pick the contract to trade, or explain why none qualifies.

        Returns (contract, note). On success `note` describes the pick (it is folded
        into the decision reason); on failure `contract` is None and `note` is the
        hold reason.
        """
        today = as_of or datetime.now(timezone.utc).date()
        typed = [c for c in chain if c.get("type") == want_type]
        if not typed:
            return None, f"no suitable {want_type} contract found in chain"

        in_window, out_of_window = [], []
        for c in typed:
            dte, strike = _dte(c, today), _strike(c)
            if dte is None or strike is None:
                continue  # unparseable row: skip rather than trade something unknown
            if self.min_dte <= dte <= self.max_dte:
                in_window.append(c)
            else:
                out_of_window.append(dte)

        if not in_window:
            available = ", ".join(f"{d}d" for d in sorted(set(out_of_window))) or "none parseable"
            return None, (
                f"no {want_type} contract expires within the {self.min_dte}-{self.max_dte} day "
                f"window (chain offers: {available})"
            )

        # Liquidity screen runs after the expiry window and before ranking, so a wide
        # market can never win on being nearest the money.
        tradable, n_rejected, n_unquoted = self.screen_liquidity(in_window)
        if not tradable:
            return None, (
                f"all {n_rejected} {want_type} contract(s) in the {self.min_dte}-{self.max_dte} day "
                f"window quote wider than {self.max_spread_pct:.0%} of their midpoint; "
                f"nothing liquid enough to buy"
            )

        mid_dte = (self.min_dte + self.max_dte) / 2
        scored = []
        for c in tradable:
            dte, strike = _dte(c, today), _strike(c)  # already known parseable
            scored.append((abs(strike - spot), abs(dte - mid_dte), str(c.get("symbol", "")), dte, c))

        scored.sort(key=lambda t: t[:3])
        distance, _, _, dte, contract = scored[0]
        return contract, (
            f"picked {contract['symbol']} -- strike {contract['strike']} is "
            f"${distance:.2f} from spot ${spot:.2f}, {dte} days to expiry "
            f"(window {self.min_dte}-{self.max_dte}d), out of {len(scored)} candidate(s)"
            f"{self._liquidity_note(contract, n_rejected, n_unquoted)}"
        )

    def select_spread(
        self,
        chain: list[dict],
        long_contract: dict,
        spot: float,
    ) -> tuple[dict | None, str, int]:
        """Pick the leg to sell against `long_contract`, or explain why there is none.

        A debit vertical: same type, same expiry, farther out of the money, and
        cheaper than the long leg. Among those, take the one whose width from the
        long strike is nearest `spread_width_pct` of spot, tie-broken by symbol so
        the pick does not depend on chain order.

        Returns (short_contract, note, n_refused). `short_contract` is None when the
        chain has no usable second leg. `n_refused` counts the legs that were
        otherwise eligible and were dropped by the liquidity screen -- it is what
        lets the caller tell "the chain has nothing to sell" (0) apart from "the
        strategy refused everything the chain had" (>0), which is the difference
        between falling back to the naked long and standing down.
        """
        if self.spread_width_pct <= 0:
            return None, "spread_width_pct is 0, trading the long leg naked", 0
        long_strike = _strike(long_contract)
        long_price = marketable_price(long_contract, "buy")
        if long_strike is None or long_price is None:
            return None, "long leg has no usable strike/price, cannot build a spread", 0

        want_type = long_contract.get("type")
        target_width = spot * self.spread_width_pct
        candidates, wide = [], []
        for c in chain:
            if c.get("type") != want_type or c.get("expiry") != long_contract.get("expiry"):
                continue
            strike = _strike(c)
            price = marketable_price(c, "sell")
            if strike is None or price is None:
                continue
            # Farther OTM than the long leg: higher strike for calls, lower for puts.
            if want_type == "call" and strike <= long_strike:
                continue
            if want_type == "put" and strike >= long_strike:
                continue
            net_debit = round(long_price - price, 2)
            if net_debit < 0.01:  # not a debit spread; refuse rather than guess
                continue
            # A short leg is a position that has to be bought back. The far-OTM strikes
            # this loop favours are exactly where quotes go wide, so screen before
            # ranking rather than discovering it at exit time.
            if self.too_wide(c):
                wide.append(c)
                continue
            width = abs(strike - long_strike)
            candidates.append((abs(width - target_width), str(c.get("symbol", "")), width, c))

        if not candidates:
            blocked = (
                f" ({len(wide)} rejected on a bid-ask spread wider than "
                f"{self.max_spread_pct:.0%})" if wide else ""
            )
            tail = (
                "; nothing was refused, so the long leg trades naked (max loss = full premium)"
                if not wide else ""
            )
            return None, (
                f"no cheaper {want_type} strike beyond {long_strike} expiring "
                f"{long_contract.get('expiry')} to sell against it{blocked}{tail}"
            ), len(wide)

        candidates.sort(key=lambda t: t[:2])
        _, _, width, short = candidates[0]
        net_debit = round(long_price - marketable_price(short, "sell"), 2)
        short_width = relative_spread(short)
        liquidity = (
            f", short leg quotes {short_width:.1%} wide" if short_width is not None
            else ", short leg is unquoted (unscreened)"
        )
        if wide:
            liquidity += f" ({len(wide)} wider strike(s) rejected on spread)"
        return short, (
            f"sold {short['symbol']} (strike {short['strike']}, same {long_contract.get('expiry')} "
            f"expiry) against it: ${width:.2f} wide vs ${target_width:.2f} target, cutting cost from "
            f"${long_price:.2f} to a ${net_debit:.2f} net debit and capping max loss at "
            f"${net_debit * 100:.2f} per spread{liquidity}"
        ), len(wide)

    def decide(
        self,
        symbol: str,
        bars: list[dict],
        option_chain: list[dict],
        equity: float,
        realized_loss_today: float,
        open_risk: float = 0.0,
        as_of: date | None = None,
    ) -> Decision:
        """`open_risk` is the dollars already committed by positions still on the book.

        It is what makes `risk_pct` an account limit instead of a per-ticket one: a
        new position is sized against whatever is left of `max_portfolio_risk_pct`
        after the open book is subtracted, so N passes cannot stack N x risk_pct.
        """
        momentum = self.compute_momentum(bars)

        if realized_loss_today >= self.max_daily_loss_pct * equity:
            return Decision(
                action="hold",
                contracts=0,
                reason=(
                    f"circuit breaker: today's realized loss ${realized_loss_today:.2f} "
                    f">= max_daily_loss_pct {self.max_daily_loss_pct:.1%} of equity "
                    f"(${self.max_daily_loss_pct * equity:.2f}); no new risk today."
                ),
                momentum_pct=momentum,
            )

        portfolio_cap = self.max_portfolio_risk_pct * equity
        remaining = round(portfolio_cap - open_risk, 2)
        if remaining <= 0:
            return Decision(
                action="hold",
                contracts=0,
                reason=(
                    f"portfolio risk cap: ${open_risk:.2f} is already at risk in open "
                    f"positions and working orders, at or above max_portfolio_risk_pct "
                    f"{self.max_portfolio_risk_pct:.1%} of equity (${portfolio_cap:.2f}). "
                    f"The {self.risk_pct:.1%} per-trade cap is per order, not per account; "
                    f"nothing new goes on until something comes off."
                ),
                momentum_pct=momentum,
            )

        if abs(momentum) < self.momentum_threshold:
            return Decision(
                action="hold",
                contracts=0,
                reason=(
                    f"{symbol} momentum {momentum:.2%} over last {self.lookback} bars is below "
                    f"threshold {self.momentum_threshold:.2%}; signal too weak to trade."
                ),
                momentum_pct=momentum,
            )

        bullish = momentum > 0
        spot = bars[-1]["c"] if bars else 0.0
        contract, note = self.select_contract(
            option_chain, "call" if bullish else "put", spot, as_of=as_of
        )
        if contract is None:
            return Decision(
                action="hold",
                contracts=0,
                reason=f"{note} for {symbol}.",
                momentum_pct=momentum,
            )

        short, spread_note, n_refused = self.select_spread(option_chain, contract, spot)
        if short is None and n_refused:
            # Escalation-after-refusal guard. The naked long is defined risk, but it
            # costs the full premium per contract, so the same budget buys a bigger
            # dollar position in it than in the spread the screen just threw out. The
            # agent does not answer "too illiquid to hedge with" by putting more money
            # on the table unhedged. A chain that offered no second leg at all refuses
            # nothing and is not routed here -- it still trades naked.
            return Decision(
                action="hold",
                contracts=0,
                reason=(
                    f"{symbol} momentum {momentum:.2%} would buy a "
                    f"{'call' if bullish else 'put'}, but every candidate short leg was "
                    f"refused by the liquidity screen ({n_refused} rejected wider than "
                    f"{self.max_spread_pct:.0%} of their midpoint). Falling back to the naked "
                    f"long would commit more of the ${min(equity * self.risk_pct, remaining):.2f} "
                    f"budget unhedged than the spread it replaces, so this pass stands down "
                    f"instead. Selection: {note}. Risk: {spread_note}."
                ),
                momentum_pct=momentum,
                contract=contract,
            )
        # Price paid per share: the net debit of the pair, or the bare premium. Struck
        # at the offer (and the bid on the leg sold), not at last, so this same number
        # can go out as the order's limit and still be marketable.
        long_price = marketable_price(contract, "buy")
        net_debit = (
            round(long_price - marketable_price(short, "sell"), 2)
            if short is not None
            else float(long_price)
        )

        per_trade_budget = equity * self.risk_pct
        budget = min(per_trade_budget, remaining)
        binding = "per-trade" if per_trade_budget <= remaining else "portfolio headroom"
        contracts = self.size_position(equity, net_debit, budget)
        if contracts == 0:
            return Decision(
                action="hold",
                contracts=0,
                reason=(
                    f"{symbol} momentum {momentum:.2%} {'bullish' if bullish else 'bearish'} "
                    f"but the binding budget ({binding}, ${budget:.2f}) buys 0 "
                    f"{'spread' if short is not None else 'contract'}s at "
                    f"${net_debit:.2f}/share x100; skipping. "
                    f"Per-trade cap ${per_trade_budget:.2f} ({self.risk_pct:.1%} of equity), "
                    f"portfolio headroom ${remaining:.2f} "
                    f"(${open_risk:.2f} of ${portfolio_cap:.2f} already at risk)."
                ),
                momentum_pct=momentum,
                contract=contract,
                short_contract=short,
                net_debit=net_debit,
            )

        kind = "call" if bullish else "put"
        structure = f"{kind} debit spread" if short is not None else f"long {kind}"
        # Rounded because `net_debit` is already whole cents: cents x 100 x an integer
        # is an exact decimal, so the only thing rounding removes is binary float noise
        # (a $904.00 cap journalled as 903.9999999999999).
        max_loss = round(net_debit * 100 * contracts, 2)
        return Decision(
            action=(f"buy_{kind}_spread" if short is not None else f"buy_{kind}"),
            contracts=contracts,
            reason=(
                f"{symbol} momentum {momentum:.2%} over last {self.lookback} bars "
                f"{'>=' if bullish else '<='} threshold; buying {contracts} {structure}(s), "
                f"long {contract['symbol']} (strike {contract['strike']}, exp {contract['expiry']}), "
                f"sized against the binding budget ({binding}, ${budget:.2f}; per-trade cap "
                f"{self.risk_pct:.1%} of ${equity:,.2f} = ${per_trade_budget:.2f}, portfolio "
                f"headroom ${remaining:.2f} after ${open_risk:.2f} already at risk), "
                f"max loss ${max_loss:.2f}. "
                f"Selection: {note}. Risk: {spread_note}. "
                f"Pricing: {self._pricing_note(contract, short)}."
            ),
            momentum_pct=momentum,
            contract=contract,
            short_contract=short,
            net_debit=net_debit,
            max_loss=max_loss,
        )
