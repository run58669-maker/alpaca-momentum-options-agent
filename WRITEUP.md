# One-page write-up — Momentum Risk-Cap Options Agent

*Alpaca AI Trading Agents Hackathon (lablab.ai). Paper trading only.*
*Required by the event page: "a one-page write-up covering AI logic, risk gates, and Alpaca
infrastructure implementation" (see `SUBMISSION.md` for the sourcing of every requirement).*

---

## 1. AI logic — what the agent decides, and why each decision is legible

Each pass is one autonomous decision cycle over a single underlying (default SPY),
implemented in `src/agent.py::run_once`. The order of operations inside a pass is itself part
of the logic, not incidental:

**exits → reconcile realized P&L → read the book → entry decision.**

Exits run first and are deliberately *not* gated by the daily-loss breaker: an agent that is
losing money must still be able to close positions. Reconciliation runs after the exits, so a
stop loss that fills on this pass is a loss the breaker already sees when the entry side is
asked for a decision — reconciling first made every self-inflicted loss arrive one pass late,
which is exactly the pass the breaker existed to stop.

**Entry** (`src/strategy.py::MomentumRiskCapStrategy.decide`): momentum over the last
`lookback` daily closes; act only when `|momentum| ≥ momentum_threshold` (default 2%) —
positive → call structure, negative → put structure. Contract selection filters the chain to
a 7–21 DTE window, applies a bid-ask liquidity screen, then ranks by distance from spot,
tie-broken by distance from mid-window and then by symbol so the pick never depends on chain
order. A second, farther-OTM leg of the same type and expiry is sold against the long leg to
form a **debit vertical**, sized so the width is nearest `spread_width_pct` of spot. If and
only if the chain genuinely offers no cheaper strike does it fall back to a naked long — and
the log says which of the two happened, because "nothing to sell" and "the strategy refused
everything" are different facts.

**Exit** (`src/exits.py::ExitPolicy`): take profit at +75% of cost basis, stop loss at −50%,
and a time stop that closes anything within 1 day of expiry win or lose, rather than carrying
expiry/assignment risk. Percentage tests are applied only to structures with a positive net
debit basis; credit or zero-basis structures get the time stop and no invented percentage.

**Explainability is a hard requirement of the design, not a report generated afterwards.**
Every decision carries the prose reason that produced it, and every reason names its own
numbers. From an actual `--dry` pass:

> `buy_call_spread SPY contracts=5 momentum=7.43% max_loss=$270.00 open_risk=$693.00`
> "…sized against the binding budget (per-trade, $1000.00; per-trade cap 1.0% of $100,000.00 =
> $1000.00, portfolio headroom $4307.00 after $693.00 already at risk)… sold
> SPY260912C00110900 against it: $2.20 wide vs $3.26 target, cutting cost from $1.48 to a
> $0.54 net debit and capping max loss at $54.00 per spread… long leg at the offer $1.48
> (last $1.46), short leg at the bid $0.94 (last $0.95)."

Every pass is appended to a JSONL journal (`journal/decisions.jsonl`, `src/journal.py`), so
the P&L a judge sees on the account can be traced back to the sentence that caused it.

## 2. Risk gates — the part the build spent the most on

Nine gates, all enforced before an order is sent:

| Gate | Rule |
|---|---|
| Per-trade budget | `risk_pct` (default 1%) of equity per ticket |
| **Portfolio budget** | `max_portfolio_risk_pct` (default 5%): a new position is sized against what is left *after* open risk, so N passes cannot stack N × `risk_pct` |
| **In-flight risk** | working unfilled orders are charged from submission, not from fill — the order book is read *before* positions on purpose, so a fill landing between the two calls is double-counted rather than missed |
| Daily-loss breaker | halt new entries once today's realized loss reaches `max_daily_loss_pct` (default 3%) of equity |
| Contract cap | hard ceiling of `max_contracts` per order |
| Liquidity screen | refuse any contract quoting wider than `max_spread_pct` (default 10%) of its own midpoint — applied to the short leg too, since far-OTM strikes are exactly where quotes go wide and that is discovered at *exit* time otherwise |
| Limit-only entries | opening orders are limits at the sized price. A market fill would leave the journalled max loss describing a fill that has not happened; the agent takes a miss over an over-budget fill |
| **Naked-short guard** | a close that would leave an uncovered short (unbounded loss) is refused; `src/portfolio.py::close_leaves_no_uncovered_short` |
| **Stand-down on unknowns** | if any open structure is unpriceable, the working-order book cannot be read whole, or a close has not confirmed filled, the *entry* side stands down for the pass — exits still run |

The stand-down rule is the design's centre of gravity: the portfolio cap is only a cap if the
total it is measured against is complete, so an unreadable term stops new risk rather than
being silently treated as zero. Idempotency keys (`mrcap-open-…` / `mrcap-close-…`) prevent
duplicate structures across passes and restarts.

## 3. Alpaca infrastructure implementation

`src/mcp_client.py::AlpacaMCPClient` spawns Alpaca's **official MCP server**
(`alpacahq/alpaca-mcp-server`, v2) via `uvx alpaca-mcp-server` over stdio and drives it with
the MCP Python `ClientSession`. Tools used: `get_clock`, `get_account_info`,
`get_stock_bars`, `get_option_chain`, `get_all_positions`, `get_orders`,
`get_account_activities_by_type`, `place_option_order` (single-leg and `order_class: "mleg"`
multi-leg), plus the closing path. The wire contract for each was read from upstream source
(pinned tree sha in the module header), not guessed — argument shapes such as `qty` being a
string, `days` needing to span enough calendar days to contain `limit` trading days, and
cursor-paged order/activity endpoints are all handled explicitly, with normalisers between
the wire payloads and the strategy.

A byte-compatible mock of the same interface backs `--dry`, so the full decision path —
including exits, reconciliation and every risk gate — runs with no keys and no network. The
suite is **423 tests** (`py -3 -m pytest --collect-only -q`), most of them pinned to wire
shapes and gate behaviour rather than to the strategy's opinions.

## 4. Honest status

The agent has **never read a live quote**. Every number above comes from the mock or from
unit tests; the dedicated paper account the event requires was not yet created at the time of
writing, so nothing here has been validated against a real Alpaca account, including the
$100,000 starting balance the rules specify. First live action should be a read-only
connectivity pass (clock/account/chain), not an order.
