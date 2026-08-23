# Next

## Needs real API keys (not done — no signup performed per task constraints)

- Free paper trading keys: sign up at https://alpaca.markets, then Dashboard ->
  Paper Trading -> generate an API key + secret. No card required.
- Export `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`, install `uv` (`pip install uv` or
  https://docs.astral.sh/uv/getting-started/installation/) so `uvx alpaca-mcp-server`
  can be spawned, `pip install -r requirements.txt` for the `mcp` package, then:
  `py src/agent.py --symbol SPY --iterations 1`.
- **Hackathon rule**: the submission itself needs a *new, dedicated* paper account
  created specifically for it — don't reuse a personal dev account for the final
  demo/video.

## What to build next (priority order)

1. **Real options selection.** `get_option_chain` today just picks "first call" /
   "first put" from the mock's 2-entry list. Against the real MCP server, filter
   `get_option_chain` results by expiry window (e.g. 7-21 DTE) and pick actual
   ATM/near-ATM strikes by delta or strike distance from spot.
2. **Reframe the trade structure to match a track.** Current strategy is a naked
   long call/put. To fit the hackathon's tracks (options alpha / volatility /
   hedging / portfolio overlays) more convincingly, add at least one defined-risk
   structure — a covered call against an existing stock position, or a vertical
   spread (buy near strike, sell farther strike) to cap max loss and make the
   risk story stronger for judges.
3. **Realized P&L feed for the circuit breaker.** `Journal.realized_loss_today()`
   only sums `realized_pnl` fields the agent itself writes; nothing writes them
   yet. Add a step that reconciles closed positions via `get_account_activities`
   (or `get_all_positions` deltas) and logs `realized_pnl` when a position closes.
4. **Backtest / sanity-check the momentum threshold** against real historical
   bars (`get_stock_bars` with a longer lookback) before trusting the 2%/10-bar
   defaults — they're placeholders, not tuned.
5. **Scheduling.** Wrap `agent.py --iterations 1` in a loop with a market-hours
   check (`get_clock`) and a sleep interval, or a cron/Task Scheduler entry, so it
   runs unattended during the demo window instead of being invoked by hand.
6. **Deliverables for submission**: working prototype reachable by URL (needs
   hosting — a small always-on box or a scheduled cloud job that also serves the
   journal read-only), pitch video ≤5 min MP4, slide deck PDF, public GitHub repo
   with LICENSE (already done in this repo).

## 1-minute demo video outline

1. (0:00-0:10) Cold open on the problem: "autonomous agent that trades options on
   momentum, explains every decision, paper money only." Show the README pitch.
2. (0:10-0:25) Terminal: run `py src/agent.py --dry --iterations 1` live, point at
   the printed reason string as it appears — "this is not a black box."
3. (0:25-0:40) Cut to `journal/decisions.jsonl` opened in an editor — scroll
   through 2-3 records, call out the `reason` field and the circuit-breaker logic
   in `strategy.py` (screen share the `decide()` method for 5 seconds).
4. (0:40-0:55) Switch to a real run against Alpaca paper trading (pre-recorded if
   markets are closed at recording time): show `get_account_info` equity, the
   agent placing a real paper options order, then the paper account's order
   history in the Alpaca dashboard to prove it actually executed.
5. (0:55-1:00) Close: repo URL on screen, one line on what's next (spreads /
   hedging track).
