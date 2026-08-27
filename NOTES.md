# Research Notes — Alpaca AI Trading Agents Hackathon

## Hackathon (lablab.ai)
Source: https://lablab.ai/ai-hackathons/alpaca-ai-trading-agents-hackathon , https://x.com/lablabai/status/2089757334746677309

- Fully online, 2026-08-28 → 2026-09-04, submission deadline 2026-09-04 15:00 UTC.
- Prize pool: $6,000 (an older X post said $5,000; page currently shows $6,000 — use $6,000).
- Tracks: options alpha, volatility trading, hedging, portfolio overlays.
- **HARD REQUIREMENT: strategies must incorporate options trading** — a pure stock-momentum bot is NOT compliant on its own. It needs to translate its signal into an options trade (e.g. buy calls/puts, covered call, cash-secured put) to qualify.
- Must use Alpaca's Trading API **and** either the MCP server or the CLI.
- **Final submission requires a NEW dedicated Alpaca paper trading account** (not a pre-existing one) — do this only when actually submitting, not now.
- Paper trading only, simulated funds, real market data, no card required, 18+.
- General lablab.ai submission deliverables: working prototype reachable by URL, pitch video ≤5 min (MP4), slide deck (PDF), public GitHub repo.
- **Judging criteria (read off the official page 2026-08-27 04:05 JST, HTTP 200 -- supersedes the
  guessed rubric that used to sit on this line)**: 1. **P&L Performance** (the agent's actual
  trading performance in the paper environment) 2. Technology Implementation 3. Creativity &
  Originality 4. Presentation & Execution 5. Social engagement. The old line here said
  "Originality /5, Presentation /5, Business Value /5, borrowed from a comparable event, page
  403'd" -- **that was wrong on both counts**: the page returns 200, and business value is not a
  criterion while P&L is the first one. It never 403'd in a way that mattered; the content is
  client-rendered in the Next.js RSC flight payload, so a tag-stripper sees a 1KB shell.
  Full checklist + evidence paths: `SUBMISSION.md`.
- **Consequence**: the submitted **Alpaca paper account ID is a judged field** -- judges look at the
  trading activity on it. The agent must trade **live on the fresh account across 08-28 -> 09-04**,
  so getting a key is the critical path with an 08-28 15:00 UTC deadline, not a step before the
  09-04 submission deadline.
- Prizes: 1st $2,500 / 2nd $1,500 / 3rd $1,000; separate Social Engagement prize 2 teams x $500 +
  1-month Algo Trader Plus per member. Paid in USD by AlpacaDB to **individuals** (W-9/W-8BEN +
  photo ID + bank details required before payment; within 90 days of event end).
- **Competition account starting balance must be set to $100,000**; a **one-page write-up** covering
  AI logic, risk gates and Alpaca infrastructure is a listed requirement (neither was in these notes
  before). Submission form fields: title, short + long description, tech/category tags, cover image,
  video presentation, slide presentation, public GitHub repo, demo platform, application URL,
  Alpaca paper account ID, up to 5 social post links.
- https://lablab.ai/hackathon-rules is a content-free SPA shell anonymously (200, but renders to one
  line) -- the general rule book stays **unverified**.

## Alpaca MCP Server
Source: https://github.com/alpacahq/alpaca-mcp-server , https://docs.alpaca.markets/us/docs/alpaca-mcp-server

- Official repo: `alpacahq/alpaca-mcp-server`. **V2 is a full rewrite (FastMCP + OpenAPI); V1 tool names do not exist in V2.** This project targets V2 tool names.
- Requires Python 3.10+ and `uv`. Run via `uvx alpaca-mcp-server` (stdio transport by default; `--transport streamable-http --port N` also supported).
- Env vars:
  - `ALPACA_API_KEY` (required)
  - `ALPACA_SECRET_KEY` (required)
  - `ALPACA_PAPER_TRADE` (default `true` — leave it true, never set false in this project)
  - `ALPACA_TOOLSETS` (optional comma list to restrict tools, e.g. `account,trading,assets,stock-data,options-data`)
- No separate "paper base URL" var — the server routes internally based on `ALPACA_PAPER_TRADE`.
- Relevant tools for this project: `get_account_info`, `get_clock`, `get_stock_bars`, `get_stock_latest_bar`, `get_option_chain`, `get_option_contracts`, `place_option_order`, `place_stock_order`, `get_all_positions`, `get_orders`, `cancel_all_orders`.
- Claude Code CLI wiring: `claude mcp add alpaca --scope user --transport stdio uvx alpaca-mcp-server --env ALPACA_API_KEY=... --env ALPACA_SECRET_KEY=...`
- Programmatic (non-Claude-Desktop) connection: standard MCP Python SDK stdio client — `mcp.client.stdio.stdio_client(StdioServerParameters(command="uvx", args=["alpaca-mcp-server"], env={...}))` + `mcp.ClientSession`. The docs don't show this explicitly but it follows the standard MCP stdio pattern used by every other client config shown (Claude Desktop/Cursor/VS Code all spawn the same `uvx alpaca-mcp-server` stdio process).
- Paper keys are free — sign up at https://alpaca.markets (Sign Up → Trading API / Paper Trading). Not signing up automatically per task constraints; noted in NEXT.md.

## 3 most important facts
1. Options trading is a **hard requirement** for this hackathon, not optional — a stock-only bot needs to be reframed as an options strategy (calls/puts/covered calls) to be eligible.
2. Alpaca MCP Server v2 (current) has different tool names than v1 — any code/docs referencing v1 tool names is stale; this project uses v2 names (`place_option_order`, `get_option_chain`, etc.).
3. A brand-new dedicated paper account is required at final-submission time — the account used for day-to-day dev/testing should not be assumed to be the one judged; re-create/verify right before submitting.

## Verified wire contract (2026-08-24, read from upstream source — not guessed)

Source: `alpacahq/alpaca-mcp-server` @ `main`, git tree sha `803b07a3`, fetched anonymously
over the GitHub raw/API endpoints. Files cited below are paths in that repo.

| What | Verified fact | Where |
|---|---|---|
| Stock bars params | `symbols` (PLURAL, comma-separated), `timeframe`, `days`, `limit`, `feed`, `sort` | `src/alpaca_mcp_server/market_data_overrides.py:101` |
| Stock bars response | `{"bars": {"SPY": [{t,o,h,l,c,v}, ...]}}` — per-symbol dict, not a flat list | same, `/v2/stocks/bars` passthrough; `tests/test_paper_integration.py:207` |
| Option chain param | `underlying_symbol` | `tests/test_paper_integration.py:334` |
| Option chain response | `{"snapshots": {"<OCC symbol>": {latestTrade, latestQuote, greeks, impliedVolatility}}}` | `tests/test_paper_integration.py:351` |
| Contract metadata | The chain carries **no** `type` / `strike` / `expiry` field. They exist only inside the OCC symbol key and must be parsed out. | same |
| Option order params | `qty` is a **string**; single leg needs `symbol` + `side`; `time_in_force` is `"day"` only; `position_intent` recommended | `src/alpaca_mcp_server/overrides.py:258` |
| Multi-leg orders | Supported: `legs=[{symbol, ratio_qty (string), side, position_intent}]`, max 4 legs, `order_class="mleg"` auto-inferred; `limit_price` on the parent is the net debit (positive) or credit (negative) | same, docstring |
| Security envelope | Tools classified `external_text` wrap payloads as `{"_alpaca_mcp_security": ..., "data": <payload>}` | `tests/test_paper_integration.py:49` |
| Chain narrowing params | `type`, `strike_price_gte`, `strike_price_lte`, `expiration_date`, `limit` — the full chain response is described upstream as "very large" | `src/alpaca_mcp_server/tool_registry.py:346` |

Four of our assumptions were wrong and are now fixed in `src/mcp_client.py`:
`symbol`→`symbols`, flat-list bars→per-symbol dict, chain rows carrying their own
strike/expiry/type→parsed from the OCC symbol, and `qty` int→str. A fifth
(`_call` returning `result.content`, a list of content blocks, where callers expected a
dict) is fixed by `unwrap_payload`.

**Still unverified** (needs live paper keys, not readable from source): whether the
free-tier data entitlement returns option snapshots at all, actual fill behaviour, and
whether `get_option_chain` without narrowing params is too large to pass through an LLM
context. Upstream's own tests skip the beta market-data ones when the CI credential
lacks the entitlement (`_skip_if_market_data_unavailable`), which is a warning sign.

## Trade structure: debit vertical (2026-08-24)

Why not a naked long call/put: the hackathon's tracks are options alpha / volatility /
hedging / portfolio overlays, and a naked long has an undefined-until-expiry cost story
(max loss = 100% of premium, and sizing has to guess at it). A debit vertical fixes both:

- Max loss = net debit x 100 x contracts, known before the order is sent.
- `size_position` budgets against the debit rather than the premium, so 1% of equity
  buys a position whose worst case really is ~1% of equity.
- The order goes out as one `mleg` **limit** order priced at that debit. A market
  multi-leg could fill worse than the number the strategy sized against.

Short-leg rule (`MomentumRiskCapStrategy.select_spread`): same type, same expiry,
strictly farther OTM, strictly cheaper (net debit >= $0.01), and among those the strike
whose width from the long leg is nearest `spread_width_pct * spot`. Ties break on symbol
so chain order cannot change the pick. No such leg -> naked long, stated in the reason.

**Unverified / arbitrary**: `spread_width_pct=0.03` is a placeholder, not a tuned or
backtested number. The short leg is chosen on width alone — no delta, no bid-ask spread,
no open interest — so on a real chain it can land on an illiquid strike. And the whole
structure has never been sent to Alpaca; `tests/test_spread.py` asserts the argument dict
matches the upstream signature, which is not the same as a fill.

**Mock pricing (`MockAlpacaMCPClient`)**: the synthetic premium is intrinsic + a time
value that decays as a gaussian in (strike - spot), sigma = 2% of spot scaled by
sqrt(dte/14). Before 2026-08-24 the time value was flat across strikes, which made every
vertical's net debit collapse to the intrinsic difference — a $2.20-wide spread priced at
$0.02. The decay only makes `--dry` output *shaped* like a real chain. It is not a pricing
model and no number out of it means anything about real premiums.

## Realized P&L reconciliation (2026-08-24)

The circuit breaker (`decide()` -> `realized_loss_today >= max_daily_loss_pct * equity`)
had no data source: `Journal.realized_loss_today()` sums `realized_pnl` fields, and
nothing wrote one. It was decoration — the agent could lose the account and keep trading.

Feed: `get_account_activities_by_type(activity_type="FILL", after=..., direction="asc",
page_size=100)`. Wire shape verified 2026-08-24 against the OpenAPI spec vendored at
`src/alpaca_mcp_server/specs/trading-api.json` in alpacahq/alpaca-mcp-server @ main
(tree sha 803b07a3):

| Fact | Consequence in this repo |
| --- | --- |
| 200 body is a bare JSON **array** | `normalize_fills` takes a list, not `{"activities": ...}` |
| `qty` / `price` are **strings** | coerced with a float parse; unparseable rows dropped |
| `side` is only `buy` / `sell`, no `position_intent` | open vs close must be inferred by matching fills |
| `page_size` max 100, `page_token` = last activity's id | `get_fills` pages; >10 pages raises rather than truncating |
| `OptionContract.multiplier` is 100 ("if a contract is traded at $1.50 and the multiplier is 100") | option P&L is `delta_price x qty x 100` |

Matching (`src/pnl.py`): FIFO per symbol, both directions. A sell with no open long lot
opens a **short** lot — the short leg of every debit vertical is opened that way — and the
later buy that covers it realizes `entry - exit`. One event per closing fill, summing all
the lots that fill closed. Two filters gate what gets journalled: the close must have
happened **today (UTC)**, and its `closing_activity_id` must not already be in the journal.
Without the second one, reconciling every pass would re-book the same loss until the
breaker tripped on a phantom.

**Unverified / open**: no fill has ever come back from Alpaca — the shape is read out of
the spec, and `tests/test_pnl.py` asserts against the spec's own example payloads. Whether
an option FILL's `qty` is contracts (assumed) or shares is not stated in the spec text I
read; if it were shares, every option P&L here would be 100x too large. Fees and
commissions are separate non-FILL activities and are not subtracted. Nothing in the agent
ever closes a position, so on a live paper account this reconciler mostly waits.

**Options level**: the spec's `max_options_trading_level` enum reads
`0=disabled, 1=Covered Call/Cash-Secured Put, 2=Long Call/Put, 3=Spreads/Straddles` — the
debit vertical this agent sends needs **level 3** on the paper account. Worth checking on
the account created for the submission before demo day.

## Exit path (2026-08-25)

Until now nothing in this repo ever closed a position. The entry side opened debit
verticals and the reconciler waited for fills that could only arrive from expiry,
assignment or a human. `src/exits.py` closes that hole.

Wire shape verified 2026-08-25 against the same vendored spec
(`src/alpaca_mcp_server/specs/trading-api.json`, alpacahq/alpaca-mcp-server @ main)
plus upstream `src/alpaca_mcp_server/tool_registry.py` and `overrides.py`:

| Fact | Consequence in this repo |
| --- | --- |
| `get_all_positions` = operationId `getAllOpenPositions` -> `GET /v2/positions`, 200 body a bare array of `Position` | `normalize_positions` takes a list |
| Every numeric `Position` field is a **string**; `side` is the enum `long`/`short` | float-coerced; direction read from `side`, never from the sign of `qty` |
| `asset_class` distinguishes `us_option` from `us_equity` | non-option rows are dropped before any closing order is built |
| `Position` carries no strike/expiry/type | parsed out of the OCC symbol, same as the chain |
| `qty_available` = "total shares available minus open orders / locked" | 0 available means a close is already working -> skip, do not send a second one |
| `place_option_order` accepts `type` "market" or "limit", `time_in_force` "day" only, and `legs` (max 4) with per-leg `side` + `position_intent` | closes go out as `sell_to_close` / `buy_to_close`, multi-leg as one `mleg` order |

Rules, checked in order: **time stop** (`close_before_dte`, default 1) fires first and
fires regardless of P&L; then **stop loss** (50% of cost); then **take profit** (75%).
Time stop first is deliberate -- on the last day the position comes off either way, and
the journalled reason has to name the rule that actually fired.

Structures, not legs: positions are grouped by (underlying, expiry, type) and the P&L
test runs on the group's net cost basis. Closing one leg of a vertical would turn a
capped-loss spread into an uncapped naked short, so the close goes out as a single
`mleg` order and one blocked leg blocks the whole structure. Close quantity is the
**smallest** leg, so an already-lopsided structure is not over-sold into a new short --
but see "The exit path could strand a naked short" below for what the smallest leg does
*not* protect.

Exits are market orders while entries are limit orders. That asymmetry is intentional:
the entry's maximum loss has to be known before the money goes out, but on the way out
the position already exists, and a resting limit that misses simply leaves it open into
expiry -- exactly the risk the time stop exists to remove. The cost is exit slippage,
which is not measured anywhere yet.

**Unverified / open**:
- No position has ever come back from a real Alpaca account. The shape is from the spec.
- Grouping by (underlying, expiry, type) merges two verticals opened on different days
  at the same expiry into one structure. Same directional bet, same expiry, so closing
  them together is defensible -- but it is a simplification, not a claim about intent.
  Separating them needs order/lot provenance the positions endpoint does not carry.
- The percentage rules need a positive net cost basis. Credit structures get the time
  stop and nothing else, rather than an invented percentage.
- Nothing reads unrealized P&L into the circuit breaker; an open loser still does not
  slow the entry side down.
- Exercise and assignment (`exercise_options_position`) are not handled at all -- the
  time stop is what keeps the agent away from that boundary.
- The mock book in `MockAlpacaMCPClient._seed_positions()` was written so each rule
  fires exactly once. It is a demonstration fixture, not observed data.

## The exit path could strand a naked short (2026-08-26)

`close_qty` takes the smallest leg. The docstring's reason -- "closing the common part
keeps the remainder balanced" -- only holds when the **long** leg is the larger one.
When the short leg is larger (a partially filled entry, a long leg closed by hand), the
common part *is* the covered part: close it and every long is gone while the excess
short stays on the book, with no cap on it at all. The exit path is the one place in
this agent whose job is to remove risk, and it had a case where it manufactured the
worst kind.

Fix (`src/exits.py`): `ExitPolicy.evaluate` asks `uncovered_short_contracts(legs)` first,
ahead of the time stop and both P&L rules. If the group holds more short contracts than
long ones, the decision becomes a buy-back of the excess on the most exposed short leg
(lowest strike for calls, highest for puts) and nothing else. The covered remainder is
still a spread; it comes off later through the normal path as one `mleg` order, which is
the only way it can come off without being lopsided.

The count is deliberately **not** the strike-geometry test `portfolio._shorts_are_debit_covered`
runs. That one requires the long on the debit side, because it is pricing the book off
cost basis and only the debit geometry is priceable that way. Here the question is
narrower -- is anything uncapped -- and a credit vertical is capped at the strike width.
Refusing to price it is right; buying a leg of it back would not be.

Three stand-downs rather than a guess, each with its own journalled reason:
- the excess is larger than any single short leg (1 long vs 2+2 short leaves 3 uncovered):
  a close order carries **one quantity for all its legs**, so 2-and-1 is not one order,
  and sending 2 would leave the rest naked;
- the chosen leg's `qty_available` is below the excess: a close is already working on it;
- no short leg has a readable strike: which one to buy back first is not decidable.

**Evidence**: tests 317 -> **333 green** (`scratch/tests_20260826_1120.txt`). Removing the
gate turns **6 red** (`scratch/mutation_naked_short_20260826_1122.txt`), including the
end-to-end one where a 2-long/5-short book sends no order at all and leaves 3 uncapped
shorts sitting there. One-screen demo: `scratch/demo_naked_short_20260826_1126.txt`.

**Unverified / open**:
- The buy-back costs a pass. A short-heavy book one day from expiry buys back the excess
  this pass and only then gets its time stop, so the covered remainder comes off one pass
  later than it otherwise would. With the default 1-day `close_before_dte` and a
  minute-scale loop that is slack, but it is a real delay and not free.
- Nothing here stops the *entry* side from creating the imbalance; it comes from partial
  fills and manual intervention, and this only cleans it up afterwards.
- The excess is bought back at market, like every other close in this module.

## Idempotency key on closing orders (2026-08-25)

`qty_available` stops a double close only *after* the first order is working. The window
it does not cover is the one upstream warns about by name: `place_option_order` can time
out **after** the request reached Alpaca (`overrides.py::_post_order` catches
`httpx.ReadTimeout` and says so), so the agent cannot tell "never sent" from "sent, reply
lost". Retrying then flattens the position twice, and the second fill opens a fresh
position the wrong way round -- on the short leg of a vertical, an uncapped one.

Read 2026-08-25 from upstream `src/alpaca_mcp_server/overrides.py` and the vendored
`trading-api.json`:

| Fact | Consequence in this repo |
| --- | --- |
| `place_option_order` takes `client_order_id` and copies it into the `POST /v2/orders` body (overrides.py:266, 334) | every close carries one |
| Its own timeout message: a retry with the same `client_order_id` is refused by the API rather than duplicated | that refusal is what makes the retry safe |
| `client_order_id` is a string, `maxLength` 128 (`Order.client_order_id`) | keys are 39 chars; the cap is asserted in tests |
| Parent-level field, not per-leg (`OrderLeg` has its own, unused here) | one key per `mleg` close, on the parent |

Key = `mrcap-close-<UTC date>-<16 hex of sha256(day, qty, leg symbols + sides)>`, computed
by `make_close_client_order_id`, so it can be recomputed from the same inputs with nothing
stored. Two properties fall out of that, both wanted:

- **A repeat of the identical close is refused.** Within one day a repeat of the same
  (legs, qty) is always a duplicate: a filled close removes the position, a working close
  zeroes `qty_available` (which `ExitPolicy` skips on), and a partial fill leaves a
  *smaller* closable qty -- a different key, correctly allowed through.
- **Tomorrow's key differs.** Closes go `time_in_force="day"`, so an unfilled one is dead
  by the bell and the next session has to be able to retry it.

The date is the UTC date. The US options regular session (13:30-20:00/21:00 UTC) never
crosses midnight UTC, so no single session is ever split across two keys.

The agent journals the key next to the order and records `order_rejected` as its own
field, so "the exit policy said close" and "the position actually came off" stay
separable in the record rather than being buried in a raw response body.

**Entry keys: done 2026-08-26**, see "Idempotency key on opening orders" below. The
earlier objection recorded here -- that a content-derived key silently becomes a "one
identical entry per UTC day" rule -- still holds; what changed is that the rule is now
the cheaper of the two errors.

**Unverified / open**:
- Alpaca's actual rejection for a duplicate `client_order_id` has never been observed.
  The mock returns upstream's `{"error": {message, http_status, detail}}` envelope with
  422 (the documented bucket for a rejected body); the status and message text are
  **modelled, not measured**. First paper-key session must check the real one.
- The key dedupes a retry inside one process, not a restart: an agent that dies after
  sending and comes back with no journal recomputes the same key only because the
  position is still on the book with the same qty. It is not a stored order log.
- Nothing retries yet. The key makes a retry safe; no code path takes one.


## Idempotency key on opening orders (2026-08-26)

The closing side got a key because of the timeout window: `place_option_order` can time
out *after* the order reached Alpaca. The entry side has that window too, but its live
hole is a different one and it is bigger.

An entry that is `accepted` but unfilled **is not a position**. `open_risk` counts
positions, so the portfolio cap cannot charge for it. On the next pass the same momentum
reading produces the same decision, and a second identical order goes out on top of the
first -- two structures' worth of risk inside a cap that was told about neither. Nothing
local can catch this: only Alpaca knows the first order exists, so only Alpaca can refuse
the second, and it will only do that if both carry the same `client_order_id`.

Key = `mrcap-open-<UTC date>-<16 hex of sha256(day, leg symbols + sides)>`, computed by
`make_open_client_order_id`. Both entry paths carry it -- the `mleg` debit vertical and
the naked-long fallback -- parent-level, as on the closing side.

**`qty` is deliberately not in the key**, and that is the one real difference from the
closing key. A close of a smaller qty is a genuinely different order: a partial fill left
less to close. A re-entry sized smaller because portfolio headroom shrank is the same
attempt wearing a smaller hat, and hashing qty would mint it a fresh key and let it
through -- which is precisely the pass this is meant to stop.

The price is stated plainly: **one identical opening structure per UTC day**. Adding to a
winner intraday, or re-entering the same strikes after a stop, is refused until tomorrow.
That is a policy choice made with the current risk plumbing in view -- an agent that can
re-send the same structure is an agent whose account-level cap has a hole in it, and a
missed add costs less than an uncounted double position. The half of that argument about
working orders being invisible to the budget no longer holds as of 2026-08-26 (see
"Working orders in the risk budget" below); the key stays anyway, because two orders on
*different* strikes from one signal would still both go out, and neither is on the book
until it fills.

A refusal is recorded as risk **not taken**: `run_once` journals `order_rejected=True`,
sets `contracts` and `max_loss` to 0, and writes a reason naming the key. The journal's
risk fields are supposed to describe risk the account actually has; an order the API
rejected bought nothing, and booking its max loss anyway would over-state the book by a
position that does not exist. The attempted structure is still recoverable -- `action`
holds what was tried and the reason string carries the full entry signal.

**Unverified / open**:
- Same as the closing key: Alpaca's real duplicate-`client_order_id` refusal has never
  been observed. The 422 envelope is modelled from upstream's error shape, not measured.
- **The dedupe is per key, not per intent.** Two *different* strikes on the same signal
  are two different keys and both go out. That gap is the portfolio cap's job, not this
  one's -- but the cap still cannot see either order until it fills.
- The refusal is trusted as proof the first order exists. A 422 for some *other* reason
  carrying the same shape would read the same way to this code; the detail message is not
  parsed.
- Still not a stored order log. A restart with no journal recomputes the same key only
  because the key is derived from the day and the legs, not from anything persisted.

## Bid-ask liquidity screen (2026-08-25)

Contract selection ranked on strike distance and width-from-target only. Neither
number says anything about whether the contract can be *traded* at the price it is
being sized against. On a real chain the far-OTM strikes this strategy sells are
exactly the ones quoted 0.03/0.09 — a 100% round trip on a leg whose whole job is to
cap the loss.

**The measurement.** `strategy.relative_spread(row)` = `(ask - bid) / midpoint`, or
`None` when the row carries no usable two-sided quote. Relative, not absolute: a
$0.04 market is tight on a $6.00 contract and ruinous on a $0.06 one.

**Where the numbers come from.** `latestQuote.bp` / `latestQuote.ap` on each chain
snapshot — the same fields `_snapshot_price` already fell back to for a midpoint, so
the field names were verified 2026-08-24 (see the wire-contract table above), not
guessed this round. `normalize_chain` now carries them through as `bid` / `ask`, and
only when **both** sides are present, the bid is positive and `ask >= bid`. Half a
market or a crossed market yields no width at all rather than a fictional one.

**The rule.** `MomentumRiskCapStrategy.too_wide(c)` is the single refusal test, used
by both legs: refuse when the contract has a quote and that quote is wider than
`max_spread_pct` (default **0.10**, CLI `--max-spread-pct`, `0` disables). It runs on
the long leg *after* the expiry window and *before* the strike ranking, so a wide
market can never win on being nearest the money; on the short leg it runs before the
width ranking, so a stale strike can never win on sitting at the target width.

**Unquoted rows are kept, not refused.** A missing quote is missing data, not evidence
of a wide market. Refusing them would make the agent hold forever on any feed that
does not carry option quotes, which is a much worse failure than trading one
unmeasured contract. The cost is that an unmeasured pick must never be silent: the
decision reason always states the chosen contract's own market, and says
`its own market is unquoted (unscreened)` when there is none, plus a count of how many
candidates went unscreened. A mutant that removes just that sentence is killed.

**What it changes, honestly.** On the shipped `--dry` chain the screen rejects 12 of
56 rows but **does not change the pick** — the strikes it rejects were already losing
on strike distance and target width. The pick only flips on a chain where a stale
market sits on the strike that wins every other criterion, which is a real situation
but not one the mock produces. `scratch/repro_liquidity_screen_20260825_1345.py`
prints both, side by side, screen on vs off; the constructed half is labelled as
constructed. The mock chain was **not** tuned to make the screen look better.

**Mock quote shape.** The `--dry` chain now emits `bid`/`ask` at
`max($0.01, 1.5% of price)` either side. The floor is the point: market makers work in
ticks, so the width in *dollars* has a floor and only grows proportionally once the
premium is large — which is why cheap far-OTM strikes come out proportionally wide.
It is a shape, not a quote model.

### Unverified / open
- **`0.10` is a placeholder, not a tuned number.** Nothing has been measured against
  real SPY quotes. Same status as the 2%/10-bar momentum default.
- No real `latestQuote` has ever been observed. The field names are verified from
  upstream source; the *values* a paper account actually receives are not. Alpaca's
  free tier serves an `indicative` options feed rather than OPRA — whether that feed
  quotes both sides on every strike is **unchecked**, and if it does not, this screen
  degrades to the unquoted path (trades, says it was unscreened) rather than to a halt.
  First paper-key session must look at a real chain and count how many rows carry a
  two-sided quote.
- Spread width is a *proxy* for liquidity. Open interest and volume are the other two,
  and neither is in the chain snapshot (`get_option_contracts` would be needed).
- When the screen rejects every short-leg candidate the agent still falls back to the
  **naked long**, which has a *larger* max loss than the spread it refused. That is the
  pre-existing fallback, unchanged this round, but the screen makes it fire more often.
  Whether "no liquid short leg" should instead mean "don't take the trade" is an open
  design question — see NEXT.md.
- Exits do not screen at all: `ExitPolicy` closes at market regardless of how wide the
  market is. Refusing to exit an illiquid position would be worse than paying the
  spread, but the *cost* of that spread is still not measured anywhere.

## Pass ordering: exits, then reconcile, then entry (2026-08-25)

Fixes the second [high] finding of `CODEX_REVIEW_20260825.md` ("本轮退出实现的亏损不
进入本轮熔断").

**What was wrong.** `run_once` called `reconcile_realized_pnl` *before* `manage_exits`.
The circuit breaker reads `journal.realized_loss_today()`, which only counts losses
reconciliation has already written down. So a stop loss that fired and filled at 09:30
was invisible to the breaker until the 09:31 pass — and the entry decision taken in
between was sized against a realized loss of $0. Every individual order still respected
the 1% risk cap; the account's daily loss budget did not. That is the failure mode a
judge looks for in a "risk management" track: a limit that is enforced per-order and
bypassed per-account.

**Order now:** clock/account/bars/chain → `manage_exits` → `reconcile_realized_pnl` →
`realized_loss_today()` → `strategy.decide` → entry order. The exits write to the
activities tape; reconciliation reads it after they have written.

**Entry gate.** Reordering alone is not enough, because a close is not a fill.
`unconfirmed_closes()` names every structure whose close came back anything other than
`status == "filled"` — still working (`new`/`accepted`/`pending_new`/`partially_filled`),
refused, or with no response at all. If that list is non-empty the entry side stands
down for the pass: `action` is forced to `hold`, `contracts` to 0, `max_loss` to 0.0, and
the record carries a `blocked_by_exits` field naming the structures, with the entry
signal preserved at the tail of the reason string. The next pass either sees the fill
reconciled or sees the order gone.

A **refusal** counts as unconfirmed on purpose. The duplicate-`client_order_id` 422 means
an identical close is already out there in a state this agent cannot see; any other
refusal means the exit policy asked for risk to come off and it did not. Neither is a
book worth adding to.

**Exits themselves are still never gated** — not by the breaker, not by this. Blocking
new risk must not block taking risk off, or one stuck close freezes the whole book.

### Evidence
- `tests/test_exits.py::TestExitLossReachesThisPassesBreaker` builds a book with one
  long put at -83% and a tape that a filled close actually writes to
  (`TapeWritingMockClient`). One pass: the stop loss fires, the $750 loss is reconciled,
  and the breaker (at 0.5% of $100k) holds the entry — all in that same pass. The
  control case (breaker at the 3% default) trades, so the hold is the budget being
  spent, not the exit path freezing the agent.
- Mutation-checked, both directions, 2026-08-25:
  reverting the order → 3 failures, and the agent opens `buy_call_spread` on top of the
  $750 loss (`scratch/mutation_reconcile_before_exits_20260825_1715.txt`); removing the
  `blocked_by_exits` gate → 1 failure, an opening order goes out while three closes sit
  at `accepted` (`scratch/mutation_no_entry_gate_20260825_1715.txt`).

### Unverified / open
- **`--dry` cannot show this.** `MockAlpacaMCPClient.get_fills` returns a frozen canned
  list, so its closes never reach the tape and the reorder changes nothing there. The
  behaviour only exists in tests, via `TapeWritingMockClient`. Codex's Next ③ (widen the
  mock to pending / partial / canceled / rejected / lost-response) would fix that.
- **No status polling.** The agent reads the status in the order response and never
  looks again. A close that fills 200ms later is treated as unconfirmed for the whole
  pass — safe, but it costs an entry. Codex's fourth [high] finding (a submitted /
  confirmed state machine, plus exercise and assignment) is still open.
- **Real fill latency is unmeasured.** How often a paper market close comes back
  `accepted` rather than `filled` decides whether this gate costs one pass occasionally
  or blocks the entry side all day. Nothing has been observed on a real account.
- The other three [high] findings are untouched: portfolio-level risk budget across
  passes (no entry-side idempotency key, no position/open-order lookup before opening),
  structure identity by opening order rather than by (underlying, expiry, type), and the
  submitted-vs-confirmed state machine above.

## Portfolio risk budget: the account cap that `risk_pct` never was (2026-08-25)

Fixes the first [high] finding of `CODEX_REVIEW_20260825.md` ("重复运行可无限叠加风险，
1% 风险上限只是单笔上限").

**What was wrong.** `risk_pct` (1%) was applied to each decision in isolation, against
current equity, with no reference to what was already open. Run the agent on a one-minute
schedule and every single order is inside the limit while the account accumulates 1% per
pass — ten passes, ten compliant tickets, 10% of the account at risk. The cap was never an
account limit; it was a per-ticket limit that read like one.

**The number.** `src/portfolio.py::open_risk` groups open positions with the same
`exits.group_structures` the exit path uses, and prices each structure's worst case off its
**net cost basis** — but only when that basis is a *debit*. What was paid for a debit
structure is exactly what it can lose: both legs of a debit vertical expire worthless in the
worst case, and a long option's floor is zero.

**Where the cap is applied.** `decide()` takes `open_risk` and holds outright when it is at
or above `max_portfolio_risk_pct` (default 5%) of equity. Below that, the position is sized
against `min(per-trade budget, remaining headroom)`, so a book at $4,800 of a $5,000 cap buys
one contract instead of five rather than refusing. Every reason string names which of the two
budgets was binding and what the other one was, so the journal answers "why this size" and
not just "why this trade". Gate order is breaker → portfolio cap → signal: a day that is
already over says so, rather than blaming a book that would be irrelevant either way.

**Net-credit structures stand the entry down.** A credit vertical's worst case is set by
strike width, and a naked short's by nothing at all; neither is recoverable from cost basis.
This strategy never opens one, but an assignment or a hand-placed order can put one on the
book. Rather than guess, `structure_risk` returns `None`, `open_risk` names the structure in
its `unpriceable` list, and `run_once` stands the entry side down exactly as it does for an
unconfirmed exit — a total that silently omits a term is a headroom figure that is fiction.
The record carries `open_risk`, `open_risk_by_structure` and `unpriceable_risk` on every pass,
hold or not, so the number the sizing was done against is recoverable from the journal rather
than only re-derivable from a book that has since moved.

**Unverified / open.**
- **Working entry orders are not counted.** The number comes from *positions*. An opening
  order that is `accepted` but unfilled is risk that is committed and invisible here. Closing
  that gap needs `get_orders` (a real v2 tool, per the tool list above) whose request/response
  shape has not been read out of upstream source yet. **The double-send half is fixed
  2026-08-26** — see "Idempotency key on opening orders": a stuck entry order can no longer be
  re-sent on the next pass, because the repeat carries the same key and Alpaca refuses it. What
  is still true is that the stuck order's risk is invisible to the cap while it works.
- **The book is re-read after the exits**, which costs one extra `get_all_positions` per pass.
  If a fill has not propagated to `/v2/positions` yet, the just-closed structure is still
  counted — risk is over-stated, and the agent trades smaller than it could. That is the safe
  direction, and it self-corrects on the next pass.
- **5% is an untuned placeholder**, exactly like `momentum_threshold` and `spread_width_pct`.
  It is not backtested; it is a number that makes the mechanism demonstrable.
- **Unrealized P&L is still not in any cap.** A book of open positions down 40% shows the same
  `open_risk` as the day it was opened, because cost basis does not move. The breaker sees
  realized losses only; this cap sees committed dollars only. Nothing yet sees a losing
  position that has not been closed.

## Working orders in the risk budget (2026-08-26)

The portfolio cap added 2026-08-25 measures the book off `GET /v2/positions`. An entry
that is `accepted` but unfilled is not in that response, so until this change the cap
was computed against a book that was missing everything currently in flight. The
idempotency key above stops the *same* structure being sent twice; it says nothing about
what the one live copy is risking, and nothing at all about a second order on different
strikes from the same signal.

Wire shape verified 2026-08-26 against `alpacahq/alpaca-mcp-server` @ `main` --
`src/alpaca_mcp_server/tool_registry.py` (tool `get_orders` = operationId `getAllOrders`)
and the vendored `src/alpaca_mcp_server/specs/trading-api.json`:

| Fact | Consequence in this repo |
| --- | --- |
| `GET /v2/orders`, 200 body a bare array of `Order` | `normalize_orders` takes a list |
| `status` defaults to `open`; enum `open` / `closed` / `all` | sent explicitly, so a future default change cannot turn this into a history query |
| `limit` defaults to **50**, max 500 | sent as 500, and a full page raises rather than silently truncating -- a truncated book under-states risk |
| `nested=true` "will roll up multi-leg orders under the legs field of primary order" | sent, otherwise a two-leg spread returns as two top-level rows and gets counted twice |
| On the spec's own `MultilegOptionsOrderResponse`, the parent's `symbol`, `side` and `asset_class` are **empty strings** | option-ness is decided off the legs, never off the parent's `asset_class` |
| Parent `qty` counts structures; each leg carries its own `qty` / `ratio_qty`; parent `limit_price` is the net debit | risk = `limit_price x 100 x qty`, parent-level only -- and only for the leg geometries `priced_as_net_debit` admits (see Pricing below); `ratio_qty` is carried through per leg so the two can be told apart |
| `filled_qty` is a separate field from `qty` | charge `qty - filled_qty`: the filled part is already a position and already counted there |
| `OrderStatus` has 17 values, only some clearly terminal | `TERMINAL_ORDER_STATUSES` drops `filled` / `canceled` / `expired` / `rejected` / `replaced`; everything else (including `done_for_day`, `held`, `calculated`) is counted |

Pricing (`portfolio.working_order_risk`), same debit-only rule the position side uses:

- **A working close is 0.0 only when the book clears it on two counts.** The
  reasoning for 0.0 -- it removes exposure the position side has already counted, so
  adding its notional would bill the account twice -- holds only if the close flattens
  the *structure*. A lone `sell_to_close` on the long leg of a debit vertical does the
  opposite: when it fills, what is left is a bare short call with no ceiling on the
  loss, while the position side still reports the pre-close net debit and the order
  side reports 0.0. The exposure is in the market and in neither number.
  `close_leaves_no_uncovered_short` subtracts the order's legs from the position book
  and requires that every short still standing in a group the close touched has a long
  beside it on the *debit* side (calls: long strike below the short; puts: above). The
  close is `None` -- stand down -- if the residual has a naked short, if a leg names a
  symbol the book does not show, if the intent points the wrong way (`sell_to_close`
  against a leg the account is short opens a bigger short), if it closes more contracts
  than are held, if a contract appears twice in the book, or if the order is not an
  option (`normalize_positions` keeps only options, so a stock close can never match).
  Demo: `scratch/demo_close_legs_20260826_0625.txt` -- same book, same cap, closing both
  legs leaves the agent free to open 5 spreads, closing only the long one holds it at 0.
  The second count (added 2026-08-26 09:35) is that the close must not *raise* the
  number the position side books. Coverage and cost are different questions: buying
  the short leg back leaves a lone long -- nothing naked -- but takes with it the
  credit that was netting the structure's basis down, so the account's own figure goes
  up the moment the order fills while the order itself is charged nothing.
  `booked_risk_change` pro-rates each residual leg's cost basis by its residual
  quantity (a leg's basis divided across its contracts is what one of them cost),
  runs the group's current and residual net basis through the same `structure_risk`
  the position side uses, and `close_does_not_raise_booked_risk` requires no group to
  rise. A group left with no contracts is 0.0, not unpriceable -- flat is a known
  number. Either side failing to be a debit is `None`: there is no cost-basis figure
  to compare against, and inventing one is the guess this module refuses everywhere
  else. Because "unpriceable" now covers two different situations, `working_risk`
  names which one it hit, with the dollars, so an operator reading the journal can
  tell "we could not read this order" from "we read it and it makes the book worse".
  Demo: `scratch/demo_close_risk_rise_20260826_0932.txt` -- one book, three closes:
  both legs together is charged $0.00 and the entry side proceeds, the protective leg
  alone cannot be matched, the short leg alone is `$360.00 -> $600.00` and stands down.
- **A debit limit price is the whole loss -- but only for two leg geometries**, and that
  is the entire reason a working order can be budgeted at all. `priced_as_net_debit`
  admits a single-leg `buy_to_open` limit order and a 1:1 debit vertical (same
  underlying, expiry and type, one `buy_to_open` + one `sell_to_open`, long strike on
  the debit side, both `ratio_qty` exactly 1). Everything else -- ratio legs, three or
  more legs, a roll's mixed open/close intents, calendars, mismatched types, an
  unparseable strike, a bare `sell_to_open` -- returns `None` and stands the entry down.
  Codex's counter-example (2026-08-26 02:18): buy one 101 call, sell two 104s for a
  $1.00 net debit and the loss above 104 has no ceiling, while the old rule reported
  $100. A roll fails the other way: the closing leg's credit *shrinks* the parent's net
  debit, so the smaller the number, the larger the new position it hides. A 2:2 ratio is
  arguably still n identical verticals, but the spec does not pin down how a multiplied
  strategy's parent limit price converts to dollars, so it stands down too.
- **No limit price is unmeasured risk, not zero risk.** A market or stop order fills at
  whatever the book gives it. Same for a net credit (worst case is strike geometry) and
  for a non-option order (no cost model here). All three return `None`, land in
  `unpriceable`, and stand the entry side down -- the same rule the position side follows,
  for the same reason: headroom computed from a total known to be short a term is not
  headroom.

`run_once` journals the two halves separately (`held_risk`, `working_risk`,
`working_risk_by_order`) because they fail differently: a wrong held number means the
position math is wrong, a wrong in-flight number usually means an order settled between
the two reads.

**Unverified / open**:
- No order has ever come back from Alpaca. The shape is the spec's; `tests/test_working_orders.py` asserts against payloads built from the spec's own examples.
- **The naked-long fallback sends a market order**, which is unpriceable by the rule above -- so while one of those is working, the agent stands down entirely. Correct under the rule and a real behaviour change worth watching; the fix is to send that fallback as a limit order too, not to soften the rule.
- ~~**Ratio spreads are assumed to price as `limit_price x 100 x parent qty`.**~~ Closed 2026-08-26 04:10, and it was worse than "assumed": a ratio spread's maximum loss is not the net debit at all, so the old code reported an unbounded structure as a small dollar figure. `normalize_orders` now keeps each leg's `ratio_qty` and parsed OCC geometry, and `priced_as_net_debit` prices only the whitelisted topologies above. What is still open is the *other* direction: a legitimate debit structure outside the whitelist (two long legs on different strikes, a 2:2 vertical) now stands the agent down rather than being priced. That costs trades, not money.
- **Equity orders are not counted at all** (they are unpriceable, which stands the entry down rather than under-counting). Same blind spot the position side has: `normalize_positions` drops non-option rows.
- ~~**KNOWN GAP -- this fix is half of the answer.**~~ Closed 2026-08-26 09:35. "No
  short is left uncovered" and "the risk booked after the fill is not higher than the
  risk booked before it" are two questions; from 06:16 to 09:35 only the first was
  checked, so buying back the short leg of a debit vertical passed while *raising* the
  account's own number. On the mock's book the 20-DTE SPY 101/104 vertical was booked
  at **$440.00** with both legs on and **$800.00** with the short bought back -- a
  **$360.00 rise charged as $0.00**, whole-book `open_risk` $2,290 -> $2,650
  (`scratch/close_raises_risk_20260826_0616.txt`). Both questions are now asked; the
  `expectedFailure` that pinned the gap passes as an ordinary test. Raised by Codex in
  its 06:1x review and reproduced here independently before that review landed.
  What remains open is the milder half of the same idea: a close that raises the booked
  risk is refused outright rather than **charged the difference**. Refusing is the safe
  direction and it is what a risk cap should do by default, but it means a legitimate
  leg-by-leg unwind stands the agent down for as long as it is working.
- **The coverage matcher is greedy, and the greedy is exhaustively verified.** Shorts are
  matched hardest-first (calls: the lowest strike, since it has the fewest longs able to
  cover it; puts: the highest) and each takes the least flexible long that still
  qualifies. `scratch/_verify_greedy_cover.py` brute-forces every assignment for every
  book of up to 3 longs and 3 shorts over 4 strikes, calls and puts: **2,380 books, 0
  mismatches** (`scratch/greedy_cover_bruteforce_20260826_0615.txt`). A wrong greedy would
  have cost false stand-downs rather than false trades, but it would still have been a
  wrong answer.
- **A residual *credit* vertical stands the agent down too.** Long 104 / short 101 does
  bound the loss -- at the strike width -- but cost basis is the only number the position
  side reports, and it does not describe that loss. Bounded by something nobody is
  measuring is not the same as budgeted, so the close is `None`. Over-conservative on
  purpose; the fix is a strike-width risk model on the position side, not a softer rule
  here.
- **A close that fills between the two reads now stands the agent down for a pass.**
  Orders are read first, so the close is still `working`; positions are read second, so
  the legs it closed are already gone and it matches nothing. That is a false stand-down
  in the safe direction, and it is the visible cost of not having a submitted-risk
  ledger (high #4). Watch for it on the live account: it should be rare and it should
  clear on the next pass.
- **Found while wiring this up, not fixed: `exits.close_qty` can leave a naked short
  itself.** It closes `min(leg qty)` so a multi-leg close is "never lopsided", and its
  docstring argues that keeps the remainder balanced. That holds when the *long* leg is
  the larger one. When the **short** leg is larger -- a partial fill on the long, or a
  long leg closed by hand -- closing the common part leaves the excess short standing
  alone, uncapped. The new budget check catches the situation on the next pass (the
  residual short has no long to cover it, so the entry side stands down) but nothing
  stops the exit from creating it. The fix belongs in `exits.py`: close the excess short
  first, or refuse the structure. Next round.
- **Stock closes stand the agent down**, for the same reason equity orders are
  unpriceable above: the book this matches against is options-only.
- **Structures are grouped `(underlying, expiry, type)`**, the same simplification
  `exits.group_structures` makes, so two verticals sharing an expiry are one group here.
  Coverage is counted in contracts across the merged group, which is right for whether a
  short is naked and wrong about which vertical a given long belongs to.
- **Two reads, one book, and the order of them is load-bearing.** Orders are fetched first, then positions. A fill that lands between the two calls is then counted twice -- still working in the first snapshot, already a position in the second -- which over-states risk and can only hold the agent back. **The reverse order loses it entirely**: not a position yet when positions are read, `filled` and dropped by the time orders are read, so the structure is in neither number and the agent sizes a new trade on top of a position it does not know it owns. This repo shipped the losing order for about twenty minutes on 2026-08-26; Codex's 02:18 review caught it and `tests/test_working_orders.py::TestFillBetweenTheTwoReads` now fails if the two calls are swapped back (`working_risk` reads 0.0 against an actual $550). An order *submitted* between the two calls is still missed for one pass, and the real fix for both is a submitted-risk ledger or the trade-updates stream rather than two REST snapshots hoping to be atomic -- high #4's territory.

## Reading the whole order book, and standing down when it cannot be read (2026-08-26)

`GET /v2/orders` returns at most 500 rows per request. Until this change the client asked
for 500 and raised `AlpacaMCPError` when it got 500 back, on the correct reasoning that a
silently truncated book under-states risk. The reasoning was right and the response was
wrong twice over: 501 working orders are not an error, they are 501 working orders; and the
exception left `run_once` and ended the process, so the one pass on which risk cannot be
measured was also the pass on which nothing could be closed. An unattended loop that stops
on a full page is an agent that stops holding positions it can no longer manage.

**Paging.** A full page is followed with the spec's `before_order_id` cursor
(`scratch/mcp_spec_trading-api.json`, `getAllOrders`): "Return orders submitted before the
order with this ID (exclusive). Mutually exclusive with `after_order_id`. Do not combine
with `after`/`until`." The id cursor is used rather than the `after`/`until` timestamps
because the spec marks the two mutually exclusive, and two orders submitted in the same
instant cannot be separated by a timestamp. `direction="desc"` is now sent explicitly for
the same reason `status` is: the cursor walks backwards from the newest order and that only
holds if the page it walks off is newest-first.

| Decision | Why |
| --- | --- |
| Page fullness measured on the **raw** page, before normalization | `normalize_orders` drops terminal statuses. A full page of `filled` orders normalizes to nothing and still has working orders behind it; stopping there would report an empty book |
| Rows deduplicated by `id` across pages | The cursor is exclusive, so a repeated id means the server echoed rather than advanced. Counting it twice over-states risk; the row is the same row either way |
| `MAX_ORDER_PAGES = 10` (5,000 orders) | A bound on the walk. Past it the book is reported unreadable rather than truncated |
| A non-list payload is an error, not an empty book | `[]` and "the response could not be read" differ by exactly the amount of risk that is invisible |

**Stand-down.** What genuinely cannot be resolved -- more than `MAX_ORDER_PAGES` full pages,
a full page whose last row has no `id` to cursor from, a payload that is not a list -- raises
`OrderBookIncomplete`, a subclass of `AlpacaMCPError` that exists so the caller can tell
"this account's book is partial" apart from "the transport broke". `run_once` catches it,
sets `working_orders` to empty, and appends the message to the same `unpriceable` list an
unpriceable structure goes into. That list already stands the entry side down, so the gap
gets the existing mechanism rather than a second one beside it. Three consequences, all
deliberate:

- **The exits have already run** by the time the order book is read (pass ordering, above),
  so a pass with an unreadable book still closes what the exit policy asked for. Closing
  only ever takes risk off; it is opening that needs a complete book.
- **`working_risk` is 0.0 on such a pass and that zero is not a measurement.** The record
  carries `order_book_gap` -- `None` on a normal pass -- precisely because the total cannot
  distinguish "nothing in flight" from "the in-flight term is missing".
- **The journal line still gets written.** Under the old behaviour the crash left no record
  of the pass at all, which is the worst possible artifact for an unattended run.

**Evidence.** Tests 333 -> **347 green** (`scratch/tests_20260826_1320.txt`). Restoring the
old single-shot `get_orders` *and* the uncaught call in `run_once` turns **11 of the 14 new
tests red**, `RESTORED ok: True` (`scratch/mutation_paging_20260826_1315.txt`); the three
that stay green are the controls (a short first page, and the unchanged trading path). The
one-screen demo is `scratch/demo_order_paging_20260826_1325.txt`: 501 orders counted over 2
calls with `before_order_id=o499`, then an unreadable book where 3 positions still close,
the entry side reads `action=hold contracts=0 max_loss=$0.00`, and `order_book_gap` names
the reason. `--dry` end-to-end unchanged (`scratch/alpaca_dry_20260826_1320.txt`, `exit=0`,
first pass still `buy_call_spread SPY contracts=5 max_loss=$255.00 open_risk=$740.00`).

**Unverified / open.**
- **No page of orders has ever come back from Alpaca.** Both the cursor parameter and the
  desc ordering are read off the vendored spec; the tool is generated from that spec, but
  whether the MCP server forwards `before_order_id` and `direction` has not been observed
  on a live account. If it silently ignores `before_order_id`, page 2 repeats page 1 and
  the dedupe makes the walk terminate at `MAX_ORDER_PAGES` with `OrderBookIncomplete` --
  a stand-down, not a wrong number, which is the failure to have.
- **Paging is not atomic.** Orders submitted while the walk is in progress can be missed or
  seen twice. At 500 rows a page this only matters on a book far larger than this strategy
  produces, and the real answer is the trade-updates stream (high #4).
- **`MAX_ORDER_PAGES = 10` is a placeholder**, chosen as "far past anything this agent can
  produce", not measured.
- **A stand-down is not free.** A book that stays unreadable stops the agent opening
  anything, indefinitely and silently apart from the journal. Nothing alerts on a run of
  `order_book_gap` passes yet.

## Pricing the naked-long fallback (2026-08-26)

When `select_spread` finds no second leg, the agent buys the bare long option. Until
today it sent that as a market order. Two consequences, pointing the same way.

**The journalled maximum loss described a fill that had not happened.** On a long
option the premium *is* the maximum loss, and `decide()` computes
`max_loss = net_debit * 100 * contracts` from `last_price` before the order leaves.
A market order fills at whatever the book gives it, so a fill above `last_price` raises
the real maximum loss above the figure the risk budget approved — and nothing
downstream re-derives it from the fill. The spread path never had this problem: it has
always gone out as an `mleg` limit at the net debit, for exactly this reason. The
single-leg path was the asymmetry, not the rule.

**The agent could not price the order it had itself just sent.**
`portfolio.working_order_risk` returns `None` for an order with no limit price — that
is unmeasured risk, not zero risk — and `None` on any working order stands the whole
entry side down. So the market-order fallback produced a working order that blocked
the next pass until it filled.

**What changed.** `place_option_order` takes `limit_price` (per share, positional,
same shape as `place_option_spread_order`) and sends `type: "limit"` with
`limit_price` formatted to two decimals as a string, as upstream types it. `agent.py`
passes `decision.net_debit`, which for a lone long is the bare premium — deliberately
the same number `max_loss` was computed from, not the ask and not a padded price.
A non-positive limit raises `AlpacaMCPError` in both the real and the mock client
rather than being sent: a `buy_to_open` at zero is not a price, and dropping the field
instead would silently restore the market order. The mock echoes `limit_price` back on
the order so a `--dry` journal shows the price the order actually carried.

| Path | Order type | Limit | `working_order_risk` while unfilled |
|---|---|---|---|
| Debit vertical | `mleg` limit | net debit | premium x 100 x unfilled qty |
| Naked long (before) | market | none | `None` -> entry side stands down |
| Naked long (after) | limit | premium sized against | premium x 100 x unfilled qty |

**Accepted cost.** A limit at the sized price does not chase. If the market has moved
up between the chain snapshot and the order, the fallback ends the pass unfilled — no
position, rather than a position bought above the budget that approved it. There is no
re-price loop and no marketable-limit padding; both are policy choices that belong with
the open fallback-policy question in NEXT.md item 4b.

**Unverified / open.**
- No order of any kind has ever been sent to a live Alpaca account. The wire shape is
  read from the vendored spec and upstream `overrides.py`, not observed.
- `last_price` is the sizing input, and it is a *last trade*, not a quote. On a thin
  option it can sit well below the ask, in which case the limit simply never fills.
  Sizing off the ask (and computing `max_loss` from the ask) is the honest fix and is
  not done.
- Unfilled is not journalled as a distinct outcome: the record shows the order and its
  limit, but there is no counter for "the fallback did not fill today", so a fallback
  that never fills is invisible outside the raw journal.
- The closing side is still unpriced. `place_option_close_order` and the spread close
  both go out at market, which is the long-standing "exits do not screen" item in
  NEXT.md 4b — a close at market takes risk *off*, so it is a slippage question rather
  than a maximum-loss one, but it is not fixed.

## The naked-long fallback policy (2026-08-26)

`select_spread` returning `None` had one meaning in the code and two meanings in
reality, and the agent treated both the same way: buy the long leg naked.

The two cases:

| Case | What happened | What the naked long is |
|---|---|---|
| The chain offers no cheaper, farther-OTM strike of the same type and expiry | Nothing was refused | The only structure available |
| Every candidate was dropped by the bid-ask screen | The strategy refused each one as too illiquid to trade | A *bigger* unhedged position taken right after a risk refusal |

The second case is escalation after a refusal. It is not a cap breach — both
structures are sized against the same budget by `size_position`, so both stay under
it — but on the same signal and a $1,000 per-trade budget the naked long takes $904
(90% of budget, 2 contracts at the $4.52 offer) where the spread it replaced took
$770 (77%, 5 spreads at a $1.54 net debit). Measured, not assumed:
`scratch/measure_naked_fallback_20260826_1710.txt`; the figures were 90%/75% when
both legs were priced off last trades and moved to 90%/77% when entry pricing went
to the traded side of the book (2026-08-26 22:35, "Pricing the entry at the market" below). The gap narrowed
and the conclusion did not, which is the part the policy rests on.

**The policy, as of 2026-08-26: refusal means hold.** `select_spread` returns a third
value, the number of otherwise-eligible short legs the screen refused, and `decide()`
stands the pass down when that count is non-zero and nothing survived. The count is
what distinguishes the two rows above; they are indistinguishable from the return
value alone.

Paths that still trade the naked long, because neither involves a refusal:

- a chain with no second leg at all (`n_refused == 0`);
- `--spread-width-pct 0`, where the operator asked for a naked long outright — this is
  the mode `DEMO_SCRIPT.md`'s shot-3 capture runs in;
- `--max-spread-pct 0`, which disables the screen, so it can refuse nothing.

### The third option, and why it is not one

The standing alternative was "widen `spread_width_pct` and look again". It cannot
work: the width only *ranks* candidates. Every strike beyond the long one that forms
a debit is already eligible at any width, so a second look at a wider target sees the
same candidate set. Measured across 0.03 → 1.00 on a refused chain: still `None` every
time (`test_widening_the_target_width_never_rescues_a_refused_leg`). On a *tradable*
chain the same sweep does change the pick (103 at 0.03, 115 at 0.15), which is the
control that shows the sweep was live and not a broken harness.

### Accepted cost

A chain whose farther strikes are all wide now produces no trade that pass, even
though the long leg passed its own screen. On a real SPY chain the near strikes this
strategy sells are the liquid ones, so this should be rare — but it has never been
measured against a live chain, and if `--max-spread-pct` is later tuned tighter than
0.10 it becomes commoner, because the same cap gates both legs.

### Unverified

- The 90%-vs-77% numbers come from constructed chains, not market data. The direction
  (naked costs more per contract, so the same budget goes further into it) follows from
  the sizing arithmetic; the exact ratio does not generalise.
- Nothing here measures how *often* the refusal case fires, because no chain has ever
  been read from a live account.

## Pricing the entry at the market (2026-08-26)

Every entry price the strategy used came from `last_price`, the record of somebody
else's trade at some earlier moment. Three things were computed from it and all three
were the wrong number:

| Computed from `last_price` | What it should be |
|---|---|
| The order's `limit_price` | The price the order can actually trade at |
| `contracts`, via `size_position` | Contracts affordable at that price |
| `max_loss`, journalled as the risk of the pass | The most the filled structure can lose |

`marketable_price(row, side)` replaces it: a leg being **bought is priced at the ask**,
a leg being **sold at the bid**. `last_price` is used only when the chain carries no
two-sided quote, because there is nothing better to price such a row off — and the
decision reason says per leg which of the two happened, so an unquoted price is never
silently mixed in with a tradable one.

The `--dry` run shows what this was worth. Same synthetic chain, same signal, before
and after (`scratch/alpaca_dry_20260826_1733.txt` vs `..._2230.txt`):

```
- limit_price '0.51'   max_loss $255.00     <- long last 1.46 - short last 0.95
+ limit_price '0.54'   max_loss $270.00     <- long ask  1.48 - short bid  0.94
                                               market is $1.44/$1.48 and $0.94/$0.96
```

The old order was a **$0.51 limit sent into a $0.54 market**: three cents under the
price at which the two legs can be traded, so it sits unfilled while the journal
records $255 of risk taken. Both halves are the same defect — a price nobody offered.

### What did not change

An unquoted row prices exactly as it did before. `normalize_chain` only attaches
`bid`/`ask` when both sides are present, positive and uncrossed, so a one-sided or
crossed quote never reaches the pricing function
(`test_a_one_sided_quote_never_reaches_this_function` pins that across both modules).

### Second-order effect worth naming

A pair that is a debit at last prices can be flat or a credit at the market. Those
used to pass the `net_debit < 0.01` test and go out as a "debit" spread priced off
numbers neither leg trades at; now they are refused
(`test_a_pair_that_is_only_a_debit_at_last_prices_is_refused`).

### Also fixed here

`max_loss` is rounded to cents. `net_debit` is already whole cents, so cents x 100 x an
integer is exact in decimal and the rounding removes only binary float noise — a $904.00
risk cap was being journalled as `903.9999999999999`.

### Accepted cost

Buying at the offer and selling at the bid is the **worst** fill of the quoted range, so
sizes come down and some signals that used to buy 5 contracts now buy 4. That is the
intended direction: the cap is supposed to bound the loss of a structure that fills, and
a mid-price limit that does not fill takes no risk but also earns nothing. Nothing here
chases — a limit at the offer is marketable at the moment it is priced, not thereafter.

### Unverified

- No quote has ever been read from a live account. The `bid`/`ask` wire fields
  (`latestQuote.bp` / `.ap`) are read out of the upstream source and the vendored spec,
  same provenance as the rest of "Verified wire contract".
- Nothing measures how far a real SPY option's ask sits above its last trade, so the
  size reduction this causes in production is unquantified.
- **Exits are still market orders** and still price nothing. This step changed the entry
  side only; the slippage an exit pays remains unmeasured.

## What an exit crosses, measured (2026-08-27)

The entry was repriced at the market on 2026-08-26. The exit side was still a bare
market order, and the asymmetry was the obvious next question: every exit rule fires on
`unrealized_pl`, which the position book derives from `current_price` -- a mark -- and
then the order that follows takes whatever the other side of the quote is paying.

**The order is unchanged.** It still goes out at market, for the reason it always did:
a resting limit that misses leaves the position open into expiry, which is exactly the
risk the time stop exists to remove. What changed is that the cost of that choice is a
number in the journal instead of an unexamined asymmetry.

`exits.closing_crossing_cost(legs, qty, quotes)` prices the same legs the close carries:

| field | meaning |
|---|---|
| `mark_proceeds` | dollars to flatten at `current_price` -- the basis the exit rule decided on |
| `quoted_proceeds` | dollars to flatten crossing the quote: longs sold at the bid, shorts bought back at the ask |
| `crossing_cost` | `mark_proceeds - quoted_proceeds` |
| `widest_leg_spread_pct` | the widest leg's `(ask-bid)/mid` |
| `unquoted` | legs the chain carried no two-sided quote for |

It rides into the journal as `crossing` on every `exit_close` record, and into the
decision reason as one sentence. `exit_hold` records carry `crossing: null` -- nothing
is being sent, so there is nothing being crossed.

### Constraints this had to satisfy

- **Nothing computed here may stand an exit down.** A $0.10/$3.00 book produces a bigger
  number and the same order (`test_a_wide_book_does_not_stop_the_exit`). Refusing to
  close because the book is wide would reintroduce the expiry risk the time stop removes.
- **A chain call that fails cannot block a close.** `agent.closing_quotes` journals
  `exit_quotes_unavailable` and returns what it has; the close goes out with its legs in
  `unquoted`.
- **No half-quoted arithmetic.** One unquoted leg withholds the whole structure's
  comparison. Half a vertical priced at the quote and half at the mark is not a number
  anything can be concluded from.
- **Only pay for quotes actually read.** One chain call per underlying, and only when
  something on it is closing; a pass that holds everything makes none.
- `crossing_cost` is a subtraction, not an absolute value. A stale last trade under a
  firm book makes it negative -- the book paying better than the mark is a real thing to
  see in the journal, not one to clamp to zero.

### Evidence

- Tests 390 -> **410 green** (`tests/test_exit_crossing.py`,
  `scratch/tests_20260827_0030.txt`).
- Mutation (`scratch/_mutate_exit_crossing.py`, restores itself, 410 green after --
  `scratch/mutation_exit_crossing_20260827_0035.txt`):
  - A, price the close at the marks instead of the quote -> **8 red**
  - B, report the arithmetic with a leg unquoted -> **4 red**
  - C, let a failed chain call out of `closing_quotes` -> **1 error**

### Unverified / still open

1. **No quote has ever come back from a live account.** `bid`/`ask` reach this through
   `normalize_chain`, same evidence grade as the rest of the wire contract: upstream
   source + vendored spec.
2. **This is not slippage.** It measures the book at decision time. Nothing yet compares
   `crossing_cost` to the fill the close actually got -- that needs the FILL activity
   matched back to the closing order, which the reconciler already reads.
3. **Two extra reads per closing pass at worst** (one chain per underlying held).
   Positions and orders are already read on the same pass; this adds a third source
   whose staleness relative to them is not controlled.

## The demo book had to be quotable, not just decidable (2026-08-27)

The crossing measurement above shipped in a state where `--dry` could not show a single
number: all three closes reported `no two-sided quote`. The cause was in the mock, not
the measurement. `MockAlpacaMCPClient` built its seeded positions at hand-picked strikes
and expiries (SPY 100/103 at 14 DTE, a QQQ 480 call at 1 DTE) chosen so each structure
would land squarely on one exit rule, while its option chain was generated on a
completely separate grid -- `CHAIN_EXPIRY_DAYS` x `CHAIN_STRIKE_OFFSETS` around a spot
of ~108.7. `closing_quotes` keys on the OCC symbol, so nothing the mock held was ever in
the chain the mock quoted.

### What changed

- `_bars` / `_chain` split out of `get_stock_bars` / `get_option_chain` as plain
  synchronous methods, so the position seed can be built off the same spot the chain is
  centred on. The async methods are now one-line wrappers.
- `CHAIN_EXPIRY_DAYS` gains a **1-day** expiry (now `1, 3, 10, 17, 45`). The time-stop
  rule fires at `dte <= 1`, so without a 1-DTE row in the chain the time-stop position
  cannot be both quotable and past the threshold. 1 and 3 are below the 7-21 day entry
  window and 45 above it, so contract selection still has to reject as well as choose.
- `_seed_on_chain(symbol, dte, kind, offset, side, qty, pnl_pct)` addresses a row of the
  mock's own chain by grid coordinates, takes the leg's mark from that row's **mid**, and
  back-solves the entry from the P&L the demo wants: `entry = mid / (1 + pnl_pct)`.
  Applying the same ratio to every leg gives the *structure* that percentage, because a
  vertical's P&L percentage is `net_current / net_entry - 1` and a common factor survives
  the subtraction. It asserts the symbol it built matches the chain row it read from --
  two spellings of the same OCC symbol drifting apart is exactly the failure being fixed.
- `_seed_position` is unchanged and still takes explicit prices; tests that seed a book
  deliberately off the mock's own grid keep using it.

The book still lands on all five rules -- take profit (+90.5%), stop loss (-63.9%), time
stop (1 DTE), hold (+4.7%, 45 DTE), skip (`qty_available` 0) -- and every leg of it now
resolves to a two-sided quote.

### Second-order effect worth naming

Pricing the book off real chain mids changed what it is worth: open risk went from
$2,290 to **$2,118** (IWM $265 + QQQ $404 + SPY put $441 + SPY 09-12 vertical $580 + SPY
10-10 vertical $428). The first attempt used a $7.58 put and took the book to $3,951,
which under the default 5% portfolio cap left too little headroom and **stood the entry
side down in `--dry`** -- the demo would have lost its entry half to gain its exit half.
Moved to the -2% strike instead. Three tests pinned the old $2,290 and were updated;
`tests/test_working_orders.py` also had three tests aimed at a structure (SPY 101/104 at
20 DTE) the book no longer holds, retargeted to the real one (108.7/113.1 at 45 DTE) via
named `HELD_*` constants.

`test_a_working_close_does_not_inflate_the_budget` asserted only `working_risk == 0.0`,
which an **unmatched** close also produces -- it would have kept passing vacuously. It
now also asserts `unpriceable_risk == []`, so 0.0 has to mean "matched the book leg for
leg and found it flat".

### Evidence

- `tests/test_seed_book_is_quotable.py` (5 tests): every seeded leg is a chain row; the
  book still exercises every rule; every close the policy decides gets a real
  `crossing_cost`; at least one of them is multi-leg (so the bid/ask netting is
  exercised, not just the lookup); every leg's mark equals its chain mid.
- Suite 410 -> **415 green** (`scratch/tests_20260827_0245.txt`).
- Mutation (`scratch/_mutate_seed_grid.py`, restores itself; restored run 415 OK --
  `scratch/mutation_seed_grid_20260827_0240.txt`): putting the time-stop leg back
  off-grid turns **10 fail + 1 error**; putting the take-profit vertical back off-grid
  turns **7 fail + 3 error**.
- `--dry` (`scratch/alpaca_dry_20260827_0245.txt`, exit 0) now prints a number on all
  three closes, where before it printed `no two-sided quote` on all three:
  - time stop, QQQ: marks $444.00, quotes $438.00, crossing **$6.00**, widest leg 2.7%
  - stop loss, SPY put: marks $159.00, quotes $156.00, crossing **$3.00**, widest 3.8%
  - take profit, SPY vertical: marks $1,105.00, quotes $1,075.00, crossing **$30.00**,
    widest 3.2%
  The entry side still trades on the same pass (`buy_call_spread SPY contracts=5`).

### Unverified / still open

1. **These are the mock's quotes.** The measurement is now demonstrable end to end, but
   every number above comes from a synthetic chain shaped by hand (`half_width =
   max(0.01, 1.5% of premium)`). It shows the plumbing works and the arithmetic nets
   correctly; it says nothing about what real books are worth crossing.
2. **The back-solved entry prices are not a history.** `entry = mid / (1 + pnl_pct)` puts
   the position at the P&L the demo needs; it does not model a path that got there. The
   SPY 09-05 put at a $1.47 entry against a $0.53 mark is a plausible-looking loser, not
   a replayed one.
3. **Cent rounding moves the target.** Both legs round to cents, so the structure lands
   near the requested percentage, not on it (+92% asked, +90.5% shown). Every caller aims
   well past its threshold rather than at it; a caller that aimed at the boundary would
   be fragile.
