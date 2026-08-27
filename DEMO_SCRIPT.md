# DEMO_SCRIPT.md — pitch video, shot by shot

Target length **3:00** (hackathon cap is 5:00 — under is better than over).
Written 2026-08-25. Every line quoted under **On screen** below was captured from a
real run on 2026-08-25 15:05 JST; the capture file is named next to each shot so the
script can be re-verified before recording instead of trusted.

**The one rule for this video:** every frame that shows fabricated data must say so
on the frame. The `--dry` mock account, its $100,000 equity, its positions and its
$450 realized loss are all synthetic. A judge who discovers that after being told
"here it is running" stops believing the rest. So the lower-third caption
`MOCK DATA — no Alpaca account attached yet` stays on screen for shots 2-6, and the
narration says "mock" out loud at least once per shot.

---

## Pre-flight (do all of this before recording)

1. `cd` to the repo root. Terminal **at least 150 columns wide** — the reason strings
   are one long line each and wrapping makes them unreadable on video.
2. Font size up until the smallest text is legible in a 1080p frame.
3. `py -m unittest discover -s tests` once to warm the interpreter, then delete
   `journal/decisions.jsonl` so the journal shot shows only this recording's records.
4. Re-run every command in this file and diff against the **On screen** blocks. If
   anything drifted, fix the script, not the recording.
5. Nothing here needs keys or network. The whole video can be shot offline.

---

## Shot 1 — the claim (0:00-0:20)

**On screen:** `README.md`, scrolled to the top so the one-line pitch fills the frame.

**Narration:**
> An autonomous agent that trades options on momentum through Alpaca's own MCP
> server. Three things separate it from a bot that just fires orders. Every decision
> — including every decision to do nothing — is written to an auditable journal with
> a plain-English reason. Every position has a known maximum loss before the order
> is sent. And it closes what it opens. Paper money only; there is no live-trading
> code path in this repository.

**Cut on:** the words "no live-trading code path", with the cursor on the README line
that says `ALPACA_PAPER_TRADE` is hardcoded to `"true"`.

---

## Shot 2 — one command, the whole loop (0:20-1:00)

**Command:**
```
py src/agent.py --dry
```

**On screen** (from `scratch/demo_shot_dry_20260825_1715.txt`; two `idempotency key:`
lines, the `reconciled:` line that follows the exits, and the tail of the `reason:`
string are elided here for length — the real run prints them, and shot 3 shows the
full reason):
```
    exit_skip: IWM 2026-08-30 put: IWM260830P00200000 has 0 contracts available (a close is already working); not sending a second closing order.
    exit_close: time stop: QQQ 2026-08-26 call expires in 1 day(s) (<= close_before_dte 1); closing 2 contract(s) rather than carrying expiry/assignment risk. Unrealized P&L $20.00.
      idempotency key: mrcap-close-2026-08-25-bf48354cad790b40
    exit_close: stop loss: SPY 2026-09-04 put is down -64.0% of its $750.00 cost ($-480.00), at or past stop_loss_pct 50%; closing 3 contract(s).
    exit_close: take profit: SPY 2026-09-08 call is up 91.7% of its $600.00 cost ($550.00), at or past take_profit_pct 75%; closing 5 contract(s).
    exit_hold: SPY 2026-09-14 call: 4.5% of cost, 20 day(s) to expiry -- inside [-50%, +75%] and more than 1 day(s) from expiry; leaving it open.
[1/1] buy_call_spread SPY contracts=5 momentum=7.43% max_loss=$255.00
    reason: SPY momentum 7.43% over last 10 bars >= threshold; buying 5 call debit spread(s), long SPY260911C00108700 ...
```

**Narration:**
> One command, no keys, mock data. Read it top down, because that is the order the
> agent thinks in. **Exits run first** — before it looks for a new trade at all, it
> asks what should come off. A position one day from expiry: closed, because
> carrying it into expiry is assignment risk nobody signed up for. One down 64
> percent: stopped out. One up 92 percent: taken. One inside both bands: left alone,
> and it says why. Only then does it look for something new — and what it opens is a
> debit spread, five contracts, **maximum loss two hundred fifty-five dollars,
> computed before the order is sent**.

**Point the cursor at:** `max_loss=$255.00` while saying the last sentence.

---

## Shot 3 — the reason string (1:00-1:35)

**On screen:** the same terminal, `reason:` line selected and re-flowed so the whole
string is visible (widen the window, or paste it into an editor at ~8 lines).

```
reason: SPY momentum 7.43% over last 10 bars >= threshold; buying 5 call debit spread(s),
long SPY260911C00108700 (strike 108.7, exp 2026-09-11), sized at 1.0% of $100,000.00 equity,
max loss $255.00. Selection: picked SPY260911C00108700 -- strike 108.7 is $0.02 from spot
$108.72, 17 days to expiry (window 7-21d), out of 11 candidate(s); its market is $1.44/$1.48
= 2.7% wide vs the 10% cap, 3 rejected as too wide. Risk: sold SPY260911C00110900 (strike
110.9, same 2026-09-11 expiry) against it: $2.20 wide vs $3.26 target, cutting cost from
$1.46 to a $0.51 net debit and capping max loss at $51.00 per spread, short leg quotes 2.1%
wide (1 wider strike(s) rejected on spread).
```

**Narration:**
> This is the whole pitch in one string. It names the signal and the threshold it
> cleared. It names the contract it chose **and the ten it passed over**. It gives
> that contract's own bid and ask — one forty-four bid, one forty-eight ask, 2.7
> percent wide against a 10 percent cap — because a strike you cannot get out of is
> not a position, it is a trap, and three candidates were dropped for exactly that.
> Then it names the leg it **sold** to cap the loss, and shows the arithmetic: a
> dollar forty-six of premium cut to a fifty-one cent net debit. None of that
> sentence is generated after the fact. It is the same object the order was built
> from.

---

## Shot 4 — the two failures that kill trading bots (1:35-2:20)

### 4a — the circuit breaker

**Command:**
```
py src/agent.py --dry --max-daily-loss-pct 0.001
```

**On screen** (verbatim, `scratch/demo_shot_breaker_20260825_1505.txt`) — the same
exit lines as shot 2, then:
```
[1/1] hold SPY contracts=0 momentum=7.43% max_loss=$0.00
    reason: circuit breaker: today's realized loss $450.00 >= max_daily_loss_pct 0.1% of equity ($100.00); no new risk today.
```

**Narration:**
> Same chain, same 7.4 percent signal — and it refuses. Today's realized loss,
> FIFO-matched from the account's own fill activities, has crossed the daily cap, so
> no new risk goes on today. But look at the exit lines above it: **the breaker
> stopped entries, not exits.** A breaker that also froze exits would trap exactly
> the losers that tripped it.

### 4b — the duplicate close

**Command:**
```
py scratch/repro_duplicate_close_20260825_1120.py
```

**On screen** (from `scratch/demo_shot_idem_20260825_1505.txt`; the `decision:` lines
and the tail of attempt 1's response are elided for length):
```
--- attempt 1 ---
  client_order_id: mrcap-close-2026-08-25-bf48354cad790b40
  order_rejected : False
--- attempt 2 ---
  client_order_id: mrcap-close-2026-08-25-bf48354cad790b40
  order_rejected : True
  response       : {"error": {"message": "API rejected the order", "http_status": 422,
                    "detail": {"code": 42210000, "message": "client_order_id must be unique: ..."}}}

orders actually on the wire: 1 (positions closed once)
```

**Narration:**
> An order call can time out *after* it reached the broker. The agent cannot tell
> "never sent" from "sent, reply lost" — so a blind retry flattens the position
> twice, and the second fill opens a brand new one the wrong way round. Every
> closing order carries an idempotency key. The retry comes back 422 and is
> journalled as rejected, not counted as a close. One order on the wire.

---

## Shot 5 — the journal (2:20-2:40)

**Command:**
```
py -c "import json;[print(json.dumps(json.loads(l),indent=2)) for l in list(open('journal/decisions.jsonl',encoding='utf-8'))[-1:]]"
```
(or open `journal/decisions.jsonl` in an editor and scroll — whichever reads better
on camera)

**On screen** (verbatim shape, from the records on disk at 15:05):
```json
{
  "ts": "2026-08-25T06:01:13.282544+00:00",
  "symbol": "SPY",
  "market_open": true,
  "equity": 100000.0,
  "momentum_pct": 0.0743,
  "action": "hold",
  "contracts": 0,
  "max_loss": 0.0,
  "reason": "circuit breaker: today's realized loss $450.00 >= max_daily_loss_pct 0.1% of equity ($100.00); no new risk today.",
  "order": null
}
```

**Narration:**
> Append-only JSONL, one record per decision — and here is the part most bots skip:
> **the holds are in there too.** `action: hold`, `order: null`, and the reason it
> stood down. You cannot audit a strategy from its winners. This is the file you
> would hand a risk desk.

---

## Shot 6 — close (2:40-3:00)

**Command:**
```
py -m unittest discover -s tests
```

**On screen** (verbatim, `scratch/tests_20260826_1520.txt`):
```
Ran 359 tests in 1.764s

OK
```

**Narration:**
> 359 tests, standard library only — no keys, no network, nothing to install. Clone
> it and everything you just watched runs on your machine in under a second.

**Last frame:** repo URL, full screen, held three seconds in silence.

---

## What this video does NOT claim (and must not imply)

Read this before improvising any narration. Each item is a sentence a judge could
catch, and one caught overclaim costs more than all six shots earn.

1. **No order has ever reached Alpaca.** Not one position, fill or quote in this
   repo has come back from a real account; every shape is built from the MCP
   server's source and the API spec. Do not say "trading", "live", "in production"
   or "we ran it" — say **"in the mock harness"**. If a paper key lands before the
   deadline, re-shoot shots 2 and 4a against the real account and delete this item.
2. **The numbers are placeholders.** 2%/10-bar momentum, the 10% spread cap, the
   50%/75% stop and target — none is tuned or backtested. Say "defaults", never
   "tuned", "optimized" or "backtested".
3. **The screen does not change the pick on the shipped mock chain.** It prunes 12
   of 56 rows, but the winner is the same either way; the flip only happens on the
   constructed chain in `scratch/repro_liquidity_screen_20260825_1345.py`. If that
   before/after gets cut in, label the constructed chain as constructed **on
   screen** — the repro script already prints `CONSTRUCTED` above it.
4. **No P&L curve, no win rate, no Sharpe.** There is no backtest behind this repo.
   If a slide wants a performance number, the honest answer is that there isn't one
   yet, and "here is what it would take to produce one" is a better ten seconds than
   a fabricated chart.

## Open before recording

- 🚨 **RE-CAPTURE EVERY SHOT. The quoted output below is stale as of 2026-08-26 22:35.**
  Entry pricing moved from `last_price` to the traded side of the book (ask when buying,
  bid when selling), so the numbers on screen and in the narration have changed:
  `max_loss=$255.00` -> `$270.00`, the `$0.51 net debit` -> `$0.54`, `$1.46 of premium`
  -> `$1.48`, and every decision reason now ends with a `Pricing: ...` clause naming
  which side of each leg's market it paid. **The numbers in the blocks below were not
  edited to match** -- they are captures, and a capture that was re-typed by hand is not
  a capture. Re-run each shot's command, paste the real output, then re-read the
  narration against it. Shot 2's spoken line "dollar forty-six of premium cut to a
  fifty-one cent net debit" has to be re-spoken with the new figures.
  See NOTES.md "Pricing the entry at the market".

- ~~**The naked-long fallback**~~ **Settled 2026-08-26 — no longer blocking.** When
  every candidate short leg is refused by the liquidity screen, the agent holds
  instead of buying the naked long. Shot 2's "maximum loss known before the order is
  sent" is safe to say as written. Two other paths still trade the naked long and
  neither is a screen refusal: a chain with no cheaper farther strike at all, and
  `--spread-width-pct 0` (which the shot-3 capture uses deliberately). Details and the
  measurement behind the call: `NEXT.md` item 4b, `NOTES.md` "The naked-long fallback
  policy". If a shot wants to show it, `scratch/demo_fallback_policy_20260826_1731.txt`
  puts all three outcomes on one screen — label the chains **CONSTRUCTED** on screen,
  the capture already prints that word.
- **Recording tool** not chosen. Whatever it is: 1080p, and terminal font large
  enough that the reason strings are readable.
