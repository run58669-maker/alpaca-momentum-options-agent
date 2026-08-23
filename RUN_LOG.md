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
