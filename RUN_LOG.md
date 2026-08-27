# Run Log

Environment: Windows 11, `py --version` -> Python 3.14.3. No `ALPACA_API_KEY` /
`ALPACA_SECRET_KEY` set, no `mcp` package installed — proves `--dry` truly needs
neither.

## Command

```
py src/agent.py --dry --symbol SPY --iterations 3
```

## Output

```
[1/3] buy_call SPY contracts=5 momentum=7.43%
    reason: SPY momentum 7.43% over last 10 bars >= threshold; buying 5 call contract(s) of SPY20260906C00102400 (strike 102.4, exp 2026-09-06), sized at 1.0% of $100,000.00 equity.
    order: {'id': 'mock-1', 'symbol': 'SPY20260906C00102400', 'side': 'buy', 'qty': 5, 'status': 'filled', 'filled_at': '2026-08-23T11:45:59.538204+00:00'}
[2/3] buy_call SPY contracts=5 momentum=7.43%
    reason: SPY momentum 7.43% over last 10 bars >= threshold; buying 5 call contract(s) of SPY20260906C00102400 (strike 102.4, exp 2026-09-06), sized at 1.0% of $100,000.00 equity.
    order: {'id': 'mock-2', 'symbol': 'SPY20260906C00102400', 'side': 'buy', 'qty': 5, 'status': 'filled', 'filled_at': '2026-08-23T11:45:59.554244+00:00'}
[3/3] buy_call SPY contracts=5 momentum=7.43%
    reason: SPY momentum 7.43% over last 10 bars >= threshold; buying 5 call contract(s) of SPY20260906C00102400 (strike 102.4, exp 2026-09-06), sized at 1.0% of $100,000.00 equity.
    order: {'id': 'mock-3', 'symbol': 'SPY20260906C00102400', 'side': 'buy', 'qty': 5, 'status': 'filled', 'filled_at': '2026-08-23T11:45:59.576693+00:00'}
```

Resulting `journal/decisions.jsonl` (3 lines, one per pass — trimmed to the first
record here, all three are structurally identical):

```json
{"ts": "2026-08-23T11:45:59.538215+00:00", "symbol": "SPY", "market_open": true, "equity": 100000.0, "momentum_pct": 0.0743, "action": "buy_call", "contracts": 5, "reason": "SPY momentum 7.43% over last 10 bars >= threshold; buying 5 call contract(s) of SPY20260906C00102400 (strike 102.4, exp 2026-09-06), sized at 1.0% of $100,000.00 equity.", "order": {"id": "mock-1", "symbol": "SPY20260906C00102400", "side": "buy", "qty": 5, "status": "filled", "filled_at": "2026-08-23T11:45:59.538204+00:00"}}
```

## Secondary checks run (not re-pasted in full, see terminal history)

- `py src/agent.py --dry --symbol AAPL --momentum-threshold 0.5 --iterations 1`
  -> correctly returns `hold` with reason "AAPL momentum 6.48% ... is below
  threshold 50.00%" — confirms the hold path and threshold gating work.
- Manual `Journal.realized_loss_today()` check: logged a record with
  `realized_pnl=-5000`, got back `5000.0` — confirms the circuit-breaker's loss
  accounting is correct before wiring it into a real losing day.

`journal/decisions.jsonl` is gitignored (runtime output); the repo only ships
`journal/.gitkeep` so the directory exists.

---

## 2026-08-24 — after contract selection landed

`py src/agent.py --dry --symbol SPY --iterations 2 --journal-path <temp>` (exit 0):

```
[1/2] buy_call SPY contracts=5 momentum=7.43%
    reason: SPY momentum 7.43% over last 10 bars >= threshold; buying 5 call contract(s) of SPY20260910C00108700 (strike 108.7, exp 2026-09-10), sized at 1.0% of $100,000.00 equity. Selection: picked SPY20260910C00108700 -- strike 108.7 is $0.02 from spot $108.72, 17 days to expiry (window 7-21d), out of 14 candidate(s).
    order: {'id': 'mock-1', 'symbol': 'SPY20260910C00108700', 'side': 'buy', 'qty': 5, 'status': 'filled', 'filled_at': '2026-08-24T02:05:38.808462+00:00'}
```

The mock chain now carries 56 rows (4 expiries x 7 strikes x call/put). Two of the
four expiries (3 and 45 days out) sit outside the default 7-21 day window, which is
why the reason reports 14 candidates rather than 28 — the window filter is visibly
doing work in the demo, not just in tests.

Test suite: `py -m unittest discover -s tests -v` -> `Ran 36 tests in 0.165s` / `OK`
/ exit 0, again with no `ALPACA_API_KEY` set and no `mcp` package installed.

## 2026-08-24 13:0x — wire-shape adapters against verified upstream source

Read `alpacahq/alpaca-mcp-server` @ main (tree sha `803b07a3`) directly instead of
trusting the mock's field names. Four assumptions were wrong (see NOTES.md
"Verified wire contract"); all four are fixed in `src/mcp_client.py` behind three
pure functions — `parse_occ_symbol`, `normalize_chain`, `normalize_bars` — plus
`unwrap_payload` for the CallToolResult / security-envelope layer.

```
py -m unittest discover -s tests -v   -> Ran 64 tests in 0.143s / OK / exit 0
py src/agent.py --dry --iterations 1  -> exit 0
    buying 5 call contract(s) of SPY260910C00108700 (strike 108.7, exp 2026-09-10)
py scratch/mutants_20260824_1322.py   -> 7 mutants, 0 survivors, source restored bit-identical
```

The `--dry` symbol changed from `SPY20260910C00108700` to `SPY260910C00108700`: the mock
was emitting a 4-digit year, which is not a valid OCC symbol. A test now asserts every
mock chain symbol round-trips through `parse_occ_symbol` and agrees with the fields the
mock reports next to it.

Not covered by any test: the argument dicts the real `AlpacaMCPClient` sends. Those rest
on the source citations in NOTES.md alone, since exercising them needs live paper keys.

## 2026-08-24 15:0x — defined-risk structure: debit vertical spreads

The strategy no longer buys naked options by default. `select_spread` picks a
farther-OTM, cheaper, same-expiry leg to sell against the long leg (target width
`spread_width_pct * spot`, default 3%); `decide` sizes on the resulting net debit and
reports `max_loss` = debit x 100 x contracts; `place_option_spread_order` sends both
legs as one `mleg` limit order at that debit. No second leg -> naked long, said out
loud in the reason. `--spread-width-pct 0` forces the naked path.

```
py -m unittest discover -s tests -v   -> Ran 88 tests in 0.146s / OK / exit 0   (was 64)
py src/agent.py --dry --iterations 1  -> exit 0
    buy_call_spread contracts=5 max_loss=$255.00
    long SPY260910C00108700 / short SPY260910C00110900, $0.51 net debit, mleg limit
py src/agent.py --dry --spread-width-pct 0   -> exit 0
    buy_call contracts=5 max_loss=$730.00      (same signal, naked, 2.9x the risk)
py scratch/mutants_20260824_1535.py   -> 11 mutants, 0 survivors, sources restored bit-identical
```

Two things this round changed beyond the feature itself:

- The real client's outgoing argument dict is now tested (`test_real_client_sends_the_
  verified_multileg_shape`), by injecting a fake `_call`. Last round's "not covered by
  any test" note is closed for both the single-leg and multi-leg order shapes. It still
  proves only that we send what upstream's signature says, not that a fill comes back.
- The mock's premium formula gained a moneyness decay. With a flat time value every
  strike of one expiry had identical extrinsic, so the `--dry` spread priced at $0.02
  on a $2.20 width — a demo-breaking number. See NOTES.md for why that is a shape, not
  a pricing model.

A redundant guard (skip the long leg's own symbol) was deleted after a mutation test
showed removing it changed nothing: the farther-OTM and cheaper-than-long filters
already exclude it.

## 2026-08-24 17:00-17:45 — realized P&L reconciliation (the circuit breaker's feed)

The daily-loss circuit breaker read `Journal.realized_loss_today()`, which sums
`realized_pnl` fields, and nothing in the codebase wrote one. It could not fire. This
round gave it a real feed.

New `src/pnl.py` (FIFO lot matching, both directions), `normalize_fills` +
`AlpacaMCPClient.get_fills` in `src/mcp_client.py` (paged
`get_account_activities_by_type`), `Journal.logged_closing_ids()` for de-duplication,
and `agent.reconcile_realized_pnl()` running before every decision.

```
py -m unittest discover -s tests -v   -> Ran 117 tests, OK   (88 -> 117; +29 in tests/test_pnl.py)
py src/agent.py --dry --iterations 2  -> exit 0
    reconciled: SPY260910C00108700 qty=5 closed at $0.70, realized P&L $-450.00   (pass 1 only)
    both passes then traded: buy_call_spread contracts=5 max_loss=$255.00
py src/agent.py --dry --max-daily-loss-pct 0.001   -> exit 0
    hold -- "circuit breaker: today's realized loss $450.00 >= max_daily_loss_pct 0.1%
    of equity ($100.00); no new risk today."      <- the breaker firing, for the first time
py scratch/mutants_20260824_1725.py   -> 14 mutants, 0 survivors, sources restored bit-identical
```

Wire shape came from the OpenAPI spec vendored in the upstream MCP server
(`specs/trading-api.json`), not from guesswork — the four facts that mattered are
tabulated in NOTES.md. The one that would have silently broken everything: a FILL
carries only `side` (buy/sell) and no `position_intent`, so open-vs-close has to be
inferred by matching fills against each other, and the short leg of a debit vertical
opens with a *sell*.

Two properties are load-bearing enough to have their own mutants: the 100x contract
multiplier (without it a $450 loss reads as $4.50 and the breaker sleeps through a
blow-up) and de-duplication by `closing_activity_id` (without it every pass re-books
the same loss until the breaker trips on a phantom).

One mutant — never removing a fully-closed lot from the FIFO queue — hangs the suite
instead of failing it, so the mutation runner now times each suite run out at 60s and
counts a hang as killed.

## 2026-08-25 09:00-09:55 -- exit path (loop round 42)

Built `src/exits.py`: the first thing in this repo that takes risk off. Open option
positions are grouped into structures by (underlying, expiry, type) and closed on a
time stop (default 1 DTE), a 50% stop loss or a 75% take profit, checked in that
order. A vertical closes as one `mleg` order with `sell_to_close` / `buy_to_close`
per leg; a structure with 0 `qty_available` is skipped because a close is already
working. Exits run before the entry decision and are not gated by the circuit breaker.

Wire shape for `GET /v2/positions` and for multi-leg closing orders read out of the
vendored OpenAPI spec plus upstream `tool_registry.py` / `overrides.py` before writing
any of it (5 anonymous read-only GETs, no credentials).

Evidence:
- `py -m unittest discover -s tests` -> Ran 162 tests, OK (was 117; 45 new in
  `tests/test_exits.py`). Output: `scratch/alpaca_tests_20260825_0945.txt`.
- `py src/agent.py --dry --iterations 2` -> all three exit rules fire on pass 1,
  nothing double-closes on pass 2. Output: `scratch/alpaca_dry_20260825_0945.txt`,
  journal `scratch/dry_exits_20260825_0945.jsonl`.
- 12 mutants, 0 survivors, source restored bit-identical:
  `scratch/alpaca_mutants_exits_20260825_0945.txt`.

Still open (NOTES.md "Exit path"): no `client_order_id` on closes, exit slippage not
measured, unrealized P&L still outside the breaker, no exercise/assignment handling,
and no position has ever come back from a real account.


## 2026-08-25 11:00-11:45 — idempotency key on closing orders

`place_option_order` can time out *after* the request reached Alpaca (upstream
`_post_order` catches `httpx.ReadTimeout` and says the order MAY have been placed), so
"never sent" and "sent, reply lost" look identical to the agent. Retrying then closes the
position twice and the second fill opens a fresh one the wrong way round. `qty_available`
only covers the window after the first order is already working.

Every close now carries `client_order_id` =
`mrcap-close-<UTC date>-<16 hex of sha256(day, qty, leg symbols+sides)>`, computed by
`make_close_client_order_id` and passed by the agent, which journals it alongside the
order plus an `order_rejected` field. Entries deliberately did not get one — a repeat
entry can be intentional, so an entry key needs an attempt identity rather than the
order's contents (NEXT.md item 5).

Read from upstream before writing: `overrides.py` (`client_order_id` param → POST body,
lines 266/334) and the vendored `trading-api.json` (`maxLength` 128). 1 anonymous
read-only GET, no credentials.

Evidence:
- `py -m unittest discover -s tests` -> Ran 177 tests, OK (was 162; 15 new).
  Output: `scratch/alpaca_tests_20260825_1115.txt`.
- `py src/agent.py --dry --iterations 2` -> each close prints its key; behaviour
  otherwise unchanged. Output: `scratch/alpaca_dry_20260825_1115.txt`.
- `py scratch/repro_duplicate_close_20260825_1120.py` -> the retry is refused with
  http_status 422 and one order reaches the wire, not two.
  Output: `scratch/alpaca_duplicate_close_20260825_1120.txt`.
- 10 mutants, 0 survivors, source restored bit-identical:
  `scratch/alpaca_mutants_idem_20260825_1135.txt`.

Still open (NOTES.md "Idempotency key on closing orders"): the real duplicate rejection
has never been observed (mock's 422 is modelled), the key survives a retry but not a
restart, nothing retries yet, and entries are still unkeyed.

## 2026-08-25 13:00–13:50 — bid-ask liquidity screen on both legs

Contract selection ranked on strike distance and width-from-target only; neither says
whether the contract can be traded at the price it is sized against. Added a screen.

- `normalize_chain` carries `latestQuote.bp`/`ap` through as `bid`/`ask` — both sides,
  positive bid, `ask >= bid`, or neither is carried.
- `strategy.relative_spread(row)` = `(ask-bid)/mid`, `None` when unmeasurable.
- `MomentumRiskCapStrategy.too_wide()` is the one refusal test, used by the long leg
  (after the expiry window, before strike ranking) and the short leg (before width
  ranking). `--max-spread-pct`, default 0.10, `0` disables.
- Unquoted rows are kept and flagged `unscreened` in the reason; the reason always
  states the chosen contract's own market.
- Mock `--dry` chain now quotes `max($0.01, 1.5%)` either side, so cheap far-OTM
  strikes come out proportionally wide, like real ones.

Evidence: `py -m unittest discover -s tests` → **208 tests / OK** (was 177; +31 in
`tests/test_liquidity.py`). Mutation check `scratch/mutants_liquidity_20260825_1330.py`
→ **17 mutants, 0 survivors, restored bit-identical: True**
(`scratch/alpaca_mutants_liquidity_20260825_1340.txt`). Screen visible in `--dry`:
`3 rejected as too wide` on the long leg, `1 wider strike(s) rejected on spread` on the
short. Before/after table: `py scratch/repro_liquidity_screen_20260825_1345.py`.

Honest: on the shipped mock chain the screen prunes 12 of 56 rows but does **not**
change the pick — the rejects were already losing on distance/width. The pick only
flips on a chain with a stale market on the otherwise-winning strike, which the repro
constructs and labels as constructed. The 0.10 cap is untuned. No real quote has ever
been observed. Falling back to the naked long when no short leg is liquid enough is
unchanged and still has a *larger* max loss than the spread it refused — flagged as an
open policy question in NEXT.md.


## 2026-08-25 15:05 JST - demo script (round 45, 0 network requests)

Wrote **`DEMO_SCRIPT.md`** (repo root): 6 shots, 3:00 target against the hackathon's
5:00 cap. Shot 1 README pitch -> 2 `--dry` whole loop (exits first, then the sized
spread) -> 3 the reason string read line by line -> 4a circuit breaker refusing the
same 7.4% signal / 4b duplicate-close 422 -> 5 the journal with the *holds* in it ->
6 `208 tests / OK`.

Every quoted **On screen** block was captured today before it was quoted, not
recalled:
`scratch/demo_shot_dry_20260825_1505.txt`, `..._breaker_...`, `..._idem_...`,
`..._liq_...`, `..._tests_...`. The two blocks that are abridged for length say so
in the line above them. Shot 5's one-liner was executed and its output matches the
script byte for byte.

Two honesty sections are part of the deliverable, not decoration: a standing caption
rule (`MOCK DATA - no Alpaca account attached yet` on shots 2-6, and "mock" said out
loud once per shot) and a **"what this video does NOT claim"** list - no order has
ever reached Alpaca, the thresholds are untuned placeholders, the screen does not
change the pick on the shipped mock chain, and there is no P&L curve / win rate /
Sharpe because there is no backtest.

Also fixed: README's Tests section still said **36 tests** (the count as of 2026-08-24
morning; actual 208) - shot 6 points a camera at that number, so a stale one is a
defect in the deliverable. The coverage sentence now names the liquidity screen,
exits, the idempotency key and FIFO P&L too. `NEXT.md`'s old "1-minute demo video
outline" is replaced by a pointer to `DEMO_SCRIPT.md`; it predated exits and promised
a live Alpaca run that still has not happened.

Open before recording (in `DEMO_SCRIPT.md`): the NEXT 4b naked-long fallback policy -
shot 2 says "maximum loss known before the order is sent", true on every path, but
the fallback path's known maximum is *larger* than the spread it refused.
