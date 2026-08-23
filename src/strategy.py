"""
Explainable momentum strategy with a hard risk cap.

Deliberately simple and auditable — every number the strategy produces comes with
a plain-English reason string, because the journal logs that reason verbatim.

Rules (all thresholds are constructor args, not magic numbers buried in logic):
  1. Momentum = % change of close price over `lookback` bars.
  2. If |momentum| < `momentum_threshold` -> HOLD (signal too weak to act on).
  3. If momentum >= threshold -> bullish -> buy a call.
     If momentum <= -threshold -> bearish -> buy a put.
  4. Position size = risk_pct of account equity, converted to option contracts
     at the contract's last price (option prices are per-share; Alpaca contracts
     are 100 shares, hence the *100), floored at 0 and capped at `max_contracts`.
  5. If today's realized loss (from the journal) already exceeds `max_daily_loss_pct`
     of equity -> force HOLD regardless of signal (circuit breaker).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Decision:
    action: str  # "buy_call" | "buy_put" | "hold"
    contracts: int
    reason: str
    momentum_pct: float


class MomentumRiskCapStrategy:
    def __init__(
        self,
        lookback: int = 10,
        momentum_threshold: float = 0.02,
        risk_pct: float = 0.01,
        max_contracts: int = 5,
        max_daily_loss_pct: float = 0.03,
    ) -> None:
        self.lookback = lookback
        self.momentum_threshold = momentum_threshold
        self.risk_pct = risk_pct
        self.max_contracts = max_contracts
        self.max_daily_loss_pct = max_daily_loss_pct

    def compute_momentum(self, bars: list[dict]) -> float:
        closes = [b["c"] for b in bars[-self.lookback :]]
        if len(closes) < 2:
            return 0.0
        return (closes[-1] - closes[0]) / closes[0]

    def size_position(self, equity: float, option_price: float) -> int:
        if option_price <= 0:
            return 0
        risk_budget = equity * self.risk_pct
        contracts = int(risk_budget // (option_price * 100))
        return max(0, min(contracts, self.max_contracts))

    def decide(
        self,
        symbol: str,
        bars: list[dict],
        option_chain: list[dict],
        equity: float,
        realized_loss_today: float,
    ) -> Decision:
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
        contract = next(
            (c for c in option_chain if c["type"] == ("call" if bullish else "put")), None
        )
        if contract is None:
            return Decision(
                action="hold",
                contracts=0,
                reason=f"no suitable {'call' if bullish else 'put'} contract found in chain for {symbol}.",
                momentum_pct=momentum,
            )

        contracts = self.size_position(equity, contract["last_price"])
        if contracts == 0:
            return Decision(
                action="hold",
                contracts=0,
                reason=(
                    f"{symbol} momentum {momentum:.2%} {'bullish' if bullish else 'bearish'} "
                    f"but risk_pct {self.risk_pct:.1%} of equity (${equity * self.risk_pct:.2f}) "
                    f"buys 0 contracts at ${contract['last_price']:.2f}/share x100; skipping."
                ),
                momentum_pct=momentum,
            )

        return Decision(
            action="buy_call" if bullish else "buy_put",
            contracts=contracts,
            reason=(
                f"{symbol} momentum {momentum:.2%} over last {self.lookback} bars "
                f"{'>=' if bullish else '<='} threshold; buying {contracts} "
                f"{'call' if bullish else 'put'} contract(s) of {contract['symbol']} "
                f"(strike {contract['strike']}, exp {contract['expiry']}), "
                f"sized at {self.risk_pct:.1%} of ${equity:,.2f} equity."
            ),
            momentum_pct=momentum,
        )
