# DEMO_SCRIPT.md — pitch video, shot by shot

Target length **3:00** (hackathon cap is 5:00 — under is better than over).
Written 2026-08-25, **all shots re-captured 2026-08-27 15:05-15:15 JST and
re-verified 2026-08-28 02:05-02:07 JST** (5 of 6 blocks byte-identical; shot 6 was
stale at 415 tests and is now 417 — see the re-verification note at the end). Every line
quoted under **On screen** below is a paste from a capture file dated `20260827`, not
a retyped number; the capture file is named next to each shot so the script can be
re-verified before recording instead of trusted. The dates and OCC symbols in those
captures are generated relative to the run day — see pre-flight step 4.

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
3. 🚨 **Delete `journal/decisions.jsonl` before shot 2 AND again before shot 4a.**
   This is not cosmetic tidying — shot 4a does not work without it. The mock account
   seeds one closing fill (`mockfill-close-1`, a $450 realized loss); the reconciler
   records it **once ever**, keyed on that activity id, so once any run has booked it
   the journal never books it again. `realized_loss_today()` then returns `0.00`, the
   circuit breaker does not trip, and shot 4a quietly opens a spread instead of
   refusing — no error, no warning, just the wrong take. Measured 2026-08-27 15:05:
   with the day's existing journal, `--max-daily-loss-pct 0.001` traded normally
   (`scratch/demo_shot_breaker_20260827_1505.txt`); with the journal removed, the same
   command printed the breaker line (`scratch/demo_shot_breaker_freshjournal_20260827_1511.txt`).
   Shot 2 consumes the fill, so shot 4a needs its own fresh journal.
4. **The captures below age out daily — on the UTC clock, not the JST one.** The mock
   chain's expiries are generated relative to `today` (`CHAIN_EXPIRY_DAYS = (1, 3, 10,
   17, 45)` in `src/mcp_client.py`), so every date, OCC symbol and idempotency key in
   the blocks below moves when the date does. The dollar figures and percentages hold;
   the symbols do not. **Re-run every command on the recording day** and diff against
   the blocks. If something drifted, fix the script, not the recording.

   🚨 **`today` is the UTC date** — every date in this repo comes from
   `datetime.now(timezone.utc).date()` (`src/agent.py:54,56`, `src/exits.py:149`,
   `src/journal.py:49`, `src/mcp_client.py:226,259,1015`, `src/strategy.py:266`).
   JST is UTC+9, so **the chain rolls over at 09:00 JST, not at midnight JST.** A
   session recorded between 00:00 and 09:00 JST is stamped with the *previous* JST
   calendar day, and that is correct, not a bug — do not "fix" it. Measured
   2026-08-28 02:05 JST: a fresh `--dry` run was byte-identical to the 2026-08-27
   15:10 JST capture apart from one `filled_at` wall-clock field, because both fell
   inside the same UTC day (2026-08-27). Practical consequence: **if the recording
   runs past 09:00 JST, every date and OCC symbol shifts mid-session** — either finish
   before 09:00 JST or re-capture everything after it, never straddle.
5. Nothing here needs keys or network. The whole video can be shot offline.
6. Re-recording order is load-bearing: 2 → 3 → (wipe journal) → 4a → 4b → 5 → 6.
   Shot 5 reads the last journal record, which is shot 4a's breaker record.

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

**On screen** (verbatim and complete, `scratch/demo_shot_dry_20260827_1510.txt` —
nothing elided; the last two lines wrap on any terminal, which is what shot 3 is for):
```
    exit_skip: IWM 2026-09-06 put: IWM260906P00106500 has 0 contracts available (a close is already working); not sending a second closing order.
    exit_close: time stop: QQQ 2026-08-28 call expires in 1 day(s) (<= close_before_dte 1); closing 2 contract(s) rather than carrying expiry/assignment risk. Unrealized P&L $40.00. Exit pricing: the marks value flattening this at $444.00, the quotes at $438.00 (longs sold into the bid, shorts bought back at the ask) -- crossing costs $6.00, widest leg 2.7% wide. Sent at market anyway: a resting limit that misses leaves the position open into expiry.
      idempotency key: mrcap-close-2026-08-27-030d882774d3ef17
    exit_close: stop loss: SPY 2026-09-06 put is down -63.9% of its $441.00 cost ($-282.00), at or past stop_loss_pct 50%; closing 3 contract(s). Exit pricing: the marks value flattening this at $159.00, the quotes at $156.00 (longs sold into the bid, shorts bought back at the ask) -- crossing costs $3.00, widest leg 3.8% wide. Sent at market anyway: a resting limit that misses leaves the position open into expiry.
      idempotency key: mrcap-close-2026-08-27-fc433033611baac6
    exit_close: take profit: SPY 2026-09-13 call is up 90.5% of its $580.00 cost ($525.00), at or past take_profit_pct 75%; closing 5 contract(s). Exit pricing: the marks value flattening this at $1105.00, the quotes at $1075.00 (longs sold into the bid, shorts bought back at the ask) -- crossing costs $30.00, widest leg 3.2% wide. Sent at market anyway: a resting limit that misses leaves the position open into expiry.
      idempotency key: mrcap-close-2026-08-27-71341a103c5eb6c5
    exit_hold: SPY 2026-10-11 call: 4.7% of cost, 45 day(s) to expiry -- inside [-50%, +75%] and more than 1 day(s) from expiry; leaving it open.
    reconciled: SPY260910C00108700 qty=5 closed at $0.70, realized P&L $-450.00
[1/1] buy_call_spread SPY contracts=5 momentum=7.43% max_loss=$270.00 open_risk=$693.00
    reason: SPY momentum 7.43% over last 10 bars >= threshold; buying 5 call debit spread(s), long SPY260913C00108700 (strike 108.7, exp 2026-09-13), sized against the binding budget (per-trade, $1000.00; per-trade cap 1.0% of $100,000.00 = $1000.00, portfolio headroom $4307.00 after $693.00 already at risk), max loss $270.00. Selection: picked SPY260913C00108700 -- strike 108.7 is $0.02 from spot $108.72, 17 days to expiry (window 7-21d), out of 11 candidate(s); its market is $1.44/$1.48 = 2.7% wide vs the 10% cap, 3 rejected as too wide. Risk: sold SPY260913C00110900 (strike 110.9, same 2026-09-13 expiry) against it: $2.20 wide vs $3.26 target, cutting cost from $1.48 to a $0.54 net debit and capping max loss at $54.00 per spread, short leg quotes 2.1% wide (1 wider strike(s) rejected on spread). Pricing: long leg at the offer $1.48 (last $1.46), short leg at the bid $0.94 (last $0.95).
      idempotency key: mrcap-open-2026-08-27-149aa9c109f09b3a
    order: {'id': 'mock-4', 'client_order_id': 'mrcap-open-2026-08-27-149aa9c109f09b3a', 'order_class': 'mleg', 'qty': 5, 'limit_price': '0.54', 'legs': [{'symbol': 'SPY260913C00108700', 'ratio_qty': '1', 'side': 'buy', 'position_intent': 'buy_to_open'}, {'symbol': 'SPY260913C00110900', 'ratio_qty': '1', 'side': 'sell', 'position_intent': 'sell_to_open'}], 'status': 'filled', 'filled_at': '2026-08-27T06:02:15.981595+00:00'}
```

**Narration:**
> One command, no keys, mock data. Read it top down, because that is the order the
> agent thinks in. **Exits run first** — before it looks for a new trade at all, it
> asks what should come off. A position one day from expiry: closed, because
> carrying it into expiry is assignment risk nobody signed up for. One down 64
> percent: stopped out. One up 91 percent: taken. One inside both bands: left alone,
> and it says why. Only then does it look for something new — and what it opens is a
> debit spread, five contracts, **maximum loss two hundred seventy dollars,
> computed before the order is sent**.

**Point the cursor at:** `max_loss=$270.00` while saying the last sentence.

---

## Shot 3 — the reason string (1:00-1:35)

**On screen:** the same terminal, `reason:` line selected and re-flowed so the whole
string is visible (widen the window, or paste it into an editor at ~10 lines). Copied
verbatim out of `scratch/demo_shot_dry_20260827_1510.txt`:

```
reason: SPY momentum 7.43% over last 10 bars >= threshold; buying 5 call debit spread(s), long SPY260913C00108700 (strike 108.7, exp 2026-09-13), sized against the binding budget (per-trade, $1000.00; per-trade cap 1.0% of $100,000.00 = $1000.00, portfolio headroom $4307.00 after $693.00 already at risk), max loss $270.00. Selection: picked SPY260913C00108700 -- strike 108.7 is $0.02 from spot $108.72, 17 days to expiry (window 7-21d), out of 11 candidate(s); its market is $1.44/$1.48 = 2.7% wide vs the 10% cap, 3 rejected as too wide. Risk: sold SPY260913C00110900 (strike 110.9, same 2026-09-13 expiry) against it: $2.20 wide vs $3.26 target, cutting cost from $1.48 to a $0.54 net debit and capping max loss at $54.00 per spread, short leg quotes 2.1% wide (1 wider strike(s) rejected on spread). Pricing: long leg at the offer $1.48 (last $1.46), short leg at the bid $0.94 (last $0.95).
```

**Narration:**
> This is the whole pitch in one string. It names the signal and the threshold it
> cleared. It names the contract it chose **and the ten it passed over**. It gives
> that contract's own bid and ask — one forty-four bid, one forty-eight ask, 2.7
> percent wide against a 10 percent cap — because a strike you cannot get out of is
> not a position, it is a trap, and three candidates were dropped for exactly that.
> Then it names the leg it **sold** to cap the loss, and shows the arithmetic: a
> dollar forty-eight of premium cut to a fifty-four cent net debit. None of that
> sentence is generated after the fact. It is the same object the order was built
> from.

---

## Shot 4 — the two failures that kill trading bots (1:35-2:20)

### 4a — the circuit breaker

**Command:**
```
py src/agent.py --dry --max-daily-loss-pct 0.001
```

**On screen** (verbatim, `scratch/demo_shot_breaker_freshjournal_20260827_1511.txt`)
— the same exit lines as shot 2, then:
```
[1/1] hold SPY contracts=0 momentum=7.43% max_loss=$0.00 open_risk=$693.00
    reason: circuit breaker: today's realized loss $450.00 >= max_daily_loss_pct 0.1% of equity ($100.00); no new risk today.
```
🚨 **Wipe `journal/decisions.jsonl` immediately before this command** — see
pre-flight step 3. Without that wipe this shot silently shows a normal entry instead
of a refusal.

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

**On screen** (verbatim, `scratch/demo_shot_idem_20260827_1512.txt`; attempt 1's
`response` line is truncated by the repro script itself, not by this document):
```
--- attempt 1 ---
  decision       : exit_close
  client_order_id: mrcap-close-2026-08-27-030d882774d3ef17
  order_rejected : False
  response       : {"id": "mock-1", "client_order_id": "mrcap-close-2026-08-27-030d882774d3ef17", "qty": 2, "type": "market", "status": "filled", "legs": [{"symbol": "QQQ260828C00107700", "ratio_qty": "1", "side": "sell
--- attempt 2 ---
  decision       : exit_close
  client_order_id: mrcap-close-2026-08-27-030d882774d3ef17
  order_rejected : True
  response       : {"error": {"message": "API rejected the order", "http_status": 422, "detail": {"code": 42210000, "message": "client_order_id must be unique: mrcap-close-2026-08-27-030d882774d3ef17"}}}

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

**On screen** (verbatim, `scratch/demo_shot_journal_20260827_1513.txt` — the last
record after shot 4a, complete, no fields dropped):
```json
{
  "ts": "2026-08-27T06:02:29.788062+00:00",
  "symbol": "SPY",
  "market_open": true,
  "equity": 100000.0,
  "momentum_pct": 0.0743,
  "action": "hold",
  "contracts": 0,
  "max_loss": 0.0,
  "client_order_id": null,
  "order_rejected": false,
  "open_risk": 693.0,
  "open_risk_by_structure": {
    "IWM 2026-09-06 put": 265.0,
    "SPY 2026-10-11 call": 428.0
  },
  "held_risk": 693.0,
  "working_risk": 0.0,
  "working_risk_by_order": {},
  "unpriceable_risk": [],
  "order_book_gap": null,
  "blocked_by_exits": [],
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

**On screen** (verbatim, `scratch/demo_shot_tests_20260828_0207.txt`):
```
----------------------------------------------------------------------
Ran 417 tests in 2.213s

OK
```

**Narration:**
> 417 tests, standard library only — no keys, no network, nothing to install. Clone
> it and everything you just watched runs on your machine in about two seconds.

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

- ~~**RE-CAPTURE EVERY SHOT.**~~ **Done 2026-08-27 15:05-15:15.** Every `On screen`
  block above was re-run and re-pasted from a capture file dated `20260827`; none was
  hand-edited. The entry-pricing change (ask when buying, bid when selling) has landed
  in the quoted numbers — `max_loss` is `$270.00`, the net debit `$0.54`, the premium
  `$1.48` — and both narration lines that spoke the old figures were re-written. The
  exit reasons also gained an `Exit pricing: ...` clause that no previous version of
  this script showed at all.
  **Still to re-verify on the recording day:** the dates and OCC symbols only, because
  the mock chain is generated relative to `today` (pre-flight step 4).

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
- ~~**Recording tool** not chosen.~~ **Chosen 2026-08-28 02:10 — render the frames,
  do not screen-grab them.** ffmpeg 8.1 is on this machine
  (`Gyan.FFmpeg` via winget) and it does expose `gdigrab`, so screen capture is
  technically available — but it is the wrong tool here and is **not** to be used
  unattended:
  1. `gdigrab` records whatever is actually on 小Q's desktop, including anything of
     hers that happens to be on screen. A headless loop round must not point a camera
     at her machine.
  2. It needs a live interactive desktop session, so it cannot run from a scheduled
     round at all.
  3. It is not reproducible — a re-record is never the same bytes, so the "re-run and
     diff" check that every other artifact in this repo gets (cover, slides) would be
     lost for the single most judge-visible file.

  **The pipeline instead:** render each shot's terminal text to 1080p PNG frames with
  PIL (same approach as `scripts/make_cover.py` / `scripts/make_slides.py`, both of
  which already re-render byte-identically), then mux with
  `ffmpeg -loop 1 -i frame.png -c:v libx264 -pix_fmt yuv420p`. This is deterministic,
  fully offline, needs no desktop, and the "On screen" blocks above become the literal
  input — so the video cannot drift from the captures the way a hand-typed take can.
  **Proven end to end 2026-08-28 02:09**, not assumed: `scratch/probe.mp4`, ffprobe
  reports `codec_name=h264 width=1920 height=1080 nb_frames=60 duration=2.000000`.
  `libx264` is present in this build (`ffmpeg -encoders`).

  Still open, and both are judgement calls that want a human: **narration** (SAPI TTS
  vs a real voice vs on-screen captions only — untested either way) and whether a
  rendered-terminal video reads as honest to a judge or as evasive. Font must still be
  large enough that the shot-3 reason string is legible at 1080p.

---

## Re-verification log — 2026-08-28 02:05-02:07 JST

Every shot re-run from a clean state in the load-bearing order (2 → 3 → wipe → 4a →
4b → 5 → 6) and diffed against the 2026-08-27 captures. Result: **5 of 6 blocks
byte-identical, 1 stale.**

| shot | fresh capture | vs 2026-08-27 |
|---|---|---|
| 2 / 3 | `scratch/demo_shot_dry_20260828_0205.txt` | identical except one `filled_at` wall clock |
| 4a | `scratch/demo_shot_breaker_freshjournal_20260828_0206.txt` | **byte-identical** |
| 4b | `scratch/demo_shot_idem_20260828_0206.txt` | **byte-identical** |
| 5 | `scratch/demo_shot_journal_20260828_0206.txt` | identical except the `ts` wall clock |
| 6 | `scratch/demo_shot_tests_20260828_0207.txt` | **STALE — `Ran 415 tests` → `Ran 417 tests`** |

**The one real defect:** shot 6's on-screen block *and its narration line* both still
said 415. `WRITEUP.md` was corrected to 417 on 2026-08-28 00:08 but this file was
missed, so recording from it would have put a wrong number on camera and in the
spoken track — the one error class this script's own closing section says costs more
than all six shots earn. Both are now 417, quoted from a capture taken today.

The same stale 415 was also live in `SUBMISSION_COPY.md` (prose + the evidence table
row that goes into the judge-facing form); both corrected, the evidence row re-dated
to today's own measurement (`417 passed, 98 subtests passed in 4.60s`).

`NEXT.md` and `NOTES.md` also contain 415, and those were **deliberately left alone** —
they are dated historical records of what the suite measured at the time, and
rewriting them would falsify a log rather than fix an error.

No dates or OCC symbols had drifted, because 02:05 JST on 08-28 is still 2026-08-27 in
UTC (pre-flight step 4). **This does not mean the symbols are stable** — they will all
move at 09:00 JST.
