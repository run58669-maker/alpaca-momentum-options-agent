"""Render the submission slide deck.

The lablab.ai form has a "Slide presentation" field. This renders it the same way
`make_cover.py` renders the cover: from the repository's own facts, deterministically, so a
number that changes in the source does not quietly rot on a slide. Palette, fonts and the
nine gate labels are imported from `make_cover` rather than retyped, so the deck and the
cover cannot drift apart.

Every claim on every slide is sourced:
  * pass order + its rationale   -> WRITEUP.md section 1 (= src/agent.py::run_once)
  * entry rule and its defaults  -> src/strategy.py::MomentumRiskCapStrategy.__init__
  * exit rule and its defaults   -> src/exits.py::ExitPolicy.__init__
  * the nine gates               -> WRITEUP.md section 2 table, one row each
  * pinned server / tool list    -> src/mcp_client.py, scripts/preflight_live.py
  * the quoted decision line     -> WRITEUP.md section 1, from a real --dry pass
  * the honest-status slide      -> WRITEUP.md section 4

There is no P&L, equity curve or win rate anywhere in the deck. The agent has never read a
live quote, and a slide is not the place to imply otherwise.

Usage:  py -3 scripts/make_slides.py [-o assets/slides.pdf] [--png-dir assets/slides]
"""

import argparse
import calendar
import pathlib
import sys
import time
import unittest.mock

from PIL import Image, ImageDraw, PdfImagePlugin

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from make_cover import (  # noqa: E402
    AMBER, BG, DIM, GATES, HAIRLINE, INK, MUTED, PANEL, S, STAGES, TEAL,
    draw_len, font, tracked,
)

W, H = 1600, 900
M = 84 * S  # page margin
REPO = "github.com/run58669-maker/alpaca-momentum-options-agent"

# Fixed PDF timestamp: the hackathon window opens 2026-08-28 15:00 UTC. A constant here is
# what makes the output reproducible; see main().
PDF_DATE = time.struct_time((2026, 8, 28, 15, 0, 0, 4, 240, 0))

GATE_RULES = [
    "risk_pct 1% of equity per ticket",
    "max_portfolio_risk_pct 5%, sized on what is left",
    "working unfilled orders charged from submission",
    "max_daily_loss_pct 3% halts new entries",
    "max_contracts 5 per order, hard ceiling",
    "max_spread_pct 10% of the contract's own mid",
    "opening orders are limits at the sized price",
    "refuse a close that leaves an uncovered short",
    "one unreadable term stands the entry side down",
]

QUOTE_HEAD = "buy_call_spread SPY contracts=5 momentum=7.43% max_loss=$270.00 open_risk=$693.00"
QUOTE_BODY = [
    "…sized against the binding budget (per-trade, $1000.00; per-trade cap 1.0% of",
    "$100,000.00 = $1000.00, portfolio headroom $4307.00 after $693.00 already at risk)…",
    "sold SPY260912C00110900 against it: $2.20 wide vs $3.26 target, cutting cost from",
    "$1.48 to a $0.54 net debit and capping max loss at $54.00 per spread… long leg at",
    "the offer $1.48 (last $1.46), short leg at the bid $0.94 (last $0.95).",
]


# --- primitives -------------------------------------------------------------------------

def new_slide():
    img = Image.new("RGB", (W * S, H * S), BG)
    return img, ImageDraw.Draw(img)


def chrome(d, kicker, page, total):
    """Header eyebrow + footer rule, identical on every slide."""
    d.rectangle([M, 54 * S + 5 * S, M + 3 * S, 54 * S + 17 * S], fill=AMBER)
    tracked(d, (M + 14 * S, 54 * S), kicker, font("segoeui.ttf", 14), AMBER, tracking=2.0)
    fy = H * S - 62 * S
    d.line([M, fy, W * S - M, fy], fill=HAIRLINE, width=1 * S)
    f_foot = font("consola.ttf", 14)
    d.text((M, fy + 16 * S), REPO, font=f_foot, fill=DIM)
    num = "%02d / %02d" % (page, total)
    d.text((W * S - M - draw_len(num, f_foot), fy + 16 * S), num, font=f_foot, fill=DIM)


def heading(d, y, text, sub=None):
    d.text((M, y), text, font=font("segoeuib.ttf", 44), fill=INK)
    y += 66 * S
    if sub:
        d.text((M, y), sub, font=font("segoeuisl.ttf", 22), fill=MUTED)
        y += 44 * S
    return y


def wrap(text, fnt, width):
    """Greedy wrap to `width` device px. Returns a list of lines."""
    lines, cur = [], ""
    for word in text.split(" "):
        trial = word if not cur else cur + " " + word
        if draw_len(trial, fnt) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def bullets(d, y, items, size=22, color=MUTED, line=34, gap=18, indent=26):
    """One dot per item; long items wrap, and the wrapped lines do not get a dot."""
    f = font("segoeuisl.ttf", size)
    # Capped rather than full-bleed: a 1400px line at this size is too long a measure to read.
    width = min(W * S - M - indent * S - M, 1230 * S)
    for text in items:
        for i, ln in enumerate(wrap(text, f, width)):
            if i == 0:
                d.ellipse([M + 2 * S, y + 11 * S, M + 9 * S, y + 18 * S], fill=TEAL)
            d.text((M + indent * S, y), ln, font=f, fill=color)
            y += line * S
        y += gap * S
    return y


def stage_row(d, y, stages, highlight_last=True, arrows=True):
    f_stage = font("segoeuib.ttf", 21)
    chip_h, pad_x, gap, arrow_w = 52 * S, 20 * S, 15 * S, 26 * S
    x = M
    for i, stage in enumerate(stages):
        w = draw_len(stage, f_stage) + pad_x * 2
        last = i == len(stages) - 1
        lit = last and highlight_last
        d.rounded_rectangle([x, y, x + w, y + chip_h], radius=8 * S,
                            fill=PANEL, outline=AMBER if lit else HAIRLINE, width=1 * S)
        d.text((x + pad_x, y + (chip_h - f_stage.size * 1.32) / 2), stage,
               font=f_stage, fill=INK if lit else MUTED)
        x += w
        if not last and not arrows:
            x += 24 * S
        if not last and arrows:
            cy = y + chip_h / 2
            d.line([x + gap, cy, x + gap + arrow_w - 7 * S, cy], fill=HAIRLINE, width=2 * S)
            d.polygon([(x + gap + arrow_w, cy), (x + gap + arrow_w - 8 * S, cy - 4 * S),
                       (x + gap + arrow_w - 8 * S, cy + 4 * S)], fill=DIM)
            x += gap + arrow_w + gap
    return y + chip_h


def panel(d, top, bot, accent=TEAL):
    d.rounded_rectangle([M, top, W * S - M, bot], radius=12 * S,
                        fill=PANEL, outline=HAIRLINE, width=1 * S)
    d.rounded_rectangle([M, top, M + 4 * S, bot], radius=2 * S, fill=accent)


# --- slides -----------------------------------------------------------------------------

def slide_title(page, total):
    img, d = new_slide()
    wash = Image.new("RGB", (W * S, H * S), BG)
    wd = ImageDraw.Draw(wash)
    for i in range(80):
        lift = (1 - i / 79) ** 2
        wd.ellipse([M - 300 * S - i * 11 * S, -400 * S - i * 8 * S,
                    M + 980 * S + i * 11 * S, 380 * S + i * 8 * S],
                   fill=(int(10 + 14 * lift), int(14 + 20 * lift), int(20 + 30 * lift)))
    img = Image.blend(img, wash, 1.0)
    d = ImageDraw.Draw(img)
    chrome(d, "ALPACA AI TRADING AGENTS HACKATHON  ·  LABLAB.AI  ·  2026", page, total)

    y = 250 * S
    d.text((M, y), "Momentum Risk-Cap", font=font("segoeuib.ttf", 76), fill=INK)
    d.text((M, y + 96 * S), "Options Agent", font=font("segoeuib.ttf", 76), fill=INK)
    y += 216 * S
    d.text((M, y), "An autonomous Alpaca agent that turns price momentum into",
           font=font("segoeuisl.ttf", 27), fill=MUTED)
    d.text((M, y + 42 * S), "defined-risk options spreads.",
           font=font("segoeuisl.ttf", 27), fill=MUTED)
    y += 122 * S
    stage_row(d, y, ["PAPER TRADING ONLY", "OFFICIAL MCP SERVER", "417 OFFLINE TESTS"],
              highlight_last=False, arrows=False)
    return img


def slide_pass(page, total):
    img, d = new_slide()
    chrome(d, "AI LOGIC  ·  ONE AUTONOMOUS PASS", page, total)
    y = heading(d, 150 * S, "The order of operations is the logic",
                "src/agent.py::run_once — one pass, one underlying, four stages, always this order.")
    y = stage_row(d, y + 6 * S, STAGES) + 56 * S
    bullets(d, y, [
        "Exits run first and are deliberately not gated by the daily-loss breaker: an agent "
        "that is losing money must still be able to close.",
        "Reconciliation runs after the exits, so a stop that fills on this pass is a loss the "
        "breaker already sees when the entry side is asked for a decision — reconciling first "
        "made every self-inflicted loss arrive one pass late.",
        "The order book is read before positions on purpose: a fill landing between the two "
        "calls is double-counted rather than missed.",
    ])
    return img


def slide_entry(page, total):
    img, d = new_slide()
    chrome(d, "AI LOGIC  ·  ENTRY", page, total)
    y = heading(d, 150 * S, "From a price move to a defined-risk structure",
                "src/strategy.py::MomentumRiskCapStrategy.decide — defaults shown, all configurable.")
    y = stage_row(d, y + 6 * S,
                  ["MOMENTUM ≥ 2%", "CHAIN 7-21 DTE", "LIQUIDITY SCREEN",
                   "RANK BY STRIKE", "SELL THE FAR LEG"]) + 56 * S
    bullets(d, y, [
        "Momentum over the last 10 daily closes; act only at |momentum| ≥ 2%. Positive → "
        "call structure, negative → put structure. Otherwise hold.",
        "Candidates are ranked by distance from spot, tie-broken by distance from mid-window "
        "and then by symbol, so the pick never depends on the order the chain came back in.",
        "A farther-OTM leg of the same type and expiry is sold against the long leg — a debit "
        "vertical, sized so the width is nearest 3% of spot.",
        "It falls back to a naked long only if the chain genuinely offers nothing cheaper, and "
        "the log says which of the two happened.",
    ])
    return img


def slide_exit(page, total):
    img, d = new_slide()
    chrome(d, "AI LOGIC  ·  EXIT", page, total)
    y = heading(d, 150 * S, "Three ways out, decided before entry",
                "src/exits.py::ExitPolicy — evaluated on every pass, ahead of everything else.")
    top = y + 10 * S
    bot = top + 210 * S
    panel(d, top, bot, accent=AMBER)
    cols = [("TAKE PROFIT", "+75%", "of cost basis"),
            ("STOP LOSS", "−50%", "of cost basis"),
            ("TIME STOP", "1 DTE", "win or lose, no expiry risk")]
    col_w = (W * S - 2 * M) / 3
    for i, (label, big, note) in enumerate(cols):
        cx = M + 34 * S + i * col_w
        tracked(d, (cx, top + 34 * S), label, font("segoeui.ttf", 14), DIM, tracking=2.0)
        d.text((cx, top + 66 * S), big, font=font("segoeuib.ttf", 54), fill=INK)
        d.text((cx, top + 148 * S), note, font=font("segoeuisl.ttf", 19), fill=MUTED)
    bullets(d, bot + 54 * S, [
        "Percentage tests apply only to structures with a positive net debit basis; credit or "
        "zero-basis structures get the time stop and no invented percentage.",
        "The time stop is checked ahead of both P&L rules on purpose: assignment is the "
        "outcome it exists to prevent.",
    ])
    return img


def slide_gates(page, total):
    img, d = new_slide()
    chrome(d, "RISK GATES", page, total)
    y = heading(d, 150 * S, "Nine gates, every one before an order is sent",
                "WRITEUP.md §2 — one row each, enforced in src/strategy.py, src/exits.py, src/portfolio.py.")
    top = y + 4 * S
    bot = top + 26 * S + 9 * 46 * S + 8 * S
    panel(d, top, bot)
    f_num = font("consola.ttf", 15)
    f_gate = font("segoeuib.ttf", 20)
    f_rule = font("segoeuisl.ttf", 20)
    for i, (gate, rule) in enumerate(zip(GATES, GATE_RULES)):
        gy = top + 26 * S + i * 46 * S
        d.text((M + 30 * S, gy + 4 * S), "%02d" % (i + 1), font=f_num, fill=DIM)
        d.text((M + 66 * S, gy), gate, font=f_gate, fill=INK)
        d.text((M + 380 * S, gy), rule, font=f_rule, fill=MUTED)
    return img


def slide_infra(page, total):
    img, d = new_slide()
    chrome(d, "ALPACA INFRASTRUCTURE", page, total)
    y = heading(d, 150 * S, "Alpaca's official MCP server, pinned",
                "src/mcp_client.py::AlpacaMCPClient — uvx alpaca-mcp-server==2.3.0 over stdio.")
    top = y + 4 * S
    bot = top + 176 * S
    panel(d, top, bot)
    cells = [("SERVER", "2.3.0", "pinned, not 'latest'"),
             ("TOOLS CALLED", "8", "incl. mleg option orders"),
             ("ARGUMENT NAMES", "25", "checked vs inputSchema"),
             ("PREFLIGHT", "PASS", "zero mismatches, no account")]
    col_w = (W * S - 2 * M) / 4
    for i, (label, big, note) in enumerate(cells):
        cx = M + 32 * S + i * col_w
        tracked(d, (cx, top + 30 * S), label, font("segoeui.ttf", 13), DIM, tracking=1.8)
        d.text((cx, top + 58 * S), big, font=font("segoeuib.ttf", 42), fill=TEAL)
        d.text((cx, top + 122 * S), note, font=font("segoeuisl.ttf", 18), fill=MUTED)
    bullets(d, bot + 50 * S, [
        "The wire contract was read from upstream source, not guessed: qty as a string, "
        "calendar days spanning the trading days asked for, cursor-paged orders and activities.",
        "scripts/preflight_live.py handshakes the real server with placeholder keys and checks "
        "every tool and argument name this repo sends against the server's own inputSchema — "
        "without touching an account.",
        "A byte-compatible mock backs --dry, so the whole decision path runs with no keys and "
        "no network: 417 tests.",
    ])
    return img


def slide_journal(page, total):
    img, d = new_slide()
    chrome(d, "EXPLAINABILITY", page, total)
    y = heading(d, 150 * S, "Every decision names its own numbers",
                "Not a report generated afterwards — the reason string is what the sizing code produced.")
    top = y + 4 * S
    bot = top + 250 * S
    panel(d, top, bot, accent=AMBER)
    d.text((M + 34 * S, top + 32 * S), QUOTE_HEAD, font=font("consolab.ttf", 19), fill=AMBER)
    f_q = font("segoeuisl.ttf", 20)
    for i, line in enumerate(QUOTE_BODY):
        d.text((M + 34 * S, top + 84 * S + i * 32 * S), line, font=f_q, fill=MUTED)
    bullets(d, bot + 54 * S, [
        "Every pass is appended to journal/decisions.jsonl — the no-trade passes included.",
        "So any P&L a judge sees on the account traces back to the sentence that caused it.",
    ])
    return img


def slide_status(page, total):
    img, d = new_slide()
    chrome(d, "HONEST STATUS", page, total)
    y = heading(d, 150 * S, "What has not happened yet",
                "WRITEUP.md §4. Stated here for the same reason it is stated there.")
    top = y + 4 * S
    bot = top + 132 * S
    panel(d, top, bot, accent=AMBER)
    d.text((M + 34 * S, top + 34 * S), "The agent has never read a live quote.",
           font=font("segoeuib.ttf", 32), fill=INK)
    d.text((M + 34 * S, top + 82 * S),
           "Every number in this deck comes from the mock or from a unit test.",
           font=font("segoeuisl.ttf", 21), fill=MUTED)
    bullets(d, bot + 50 * S, [
        "The dedicated paper account the rules require was not yet created when this deck was "
        "rendered, so the $100,000 starting balance is untested against a real account.",
        "First live action is a read-only connectivity pass — clock, account, chain — not an order.",
        "No P&L, equity curve or win rate appears anywhere in this deck, on purpose.",
    ])
    return img


SLIDES = [slide_title, slide_pass, slide_entry, slide_exit,
          slide_gates, slide_infra, slide_journal, slide_status]


def render_all():
    total = len(SLIDES)
    return [fn(i + 1, total).resize((W, H), Image.LANCZOS) for i, fn in enumerate(SLIDES)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default="assets/slides.pdf")
    ap.add_argument("--png-dir", default="assets/slides")
    args = ap.parse_args()

    pages = render_all()
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # PIL stamps the PDF's /CreationDate and /ModDate from time.gmtime() with no override
    # hook, which would make two runs of this script differ in bytes for no reason worth
    # having. Pin them, so "regenerate and diff" stays a usable check on the deck.
    frozen = time.gmtime(calendar.timegm(PDF_DATE))
    with unittest.mock.patch.object(PdfImagePlugin.time, "gmtime", lambda *a: frozen):
        pages[0].save(out, "PDF", resolution=150.0, save_all=True, append_images=pages[1:])
    print("%s  %d B  %d pages  %s" % (out, out.stat().st_size, len(pages), pages[0].size))

    png_dir = pathlib.Path(args.png_dir)
    png_dir.mkdir(parents=True, exist_ok=True)
    for i, page in enumerate(pages):
        p = png_dir / ("slide-%02d.png" % (i + 1))
        page.save(p)
        print("  %s  %d B" % (p, p.stat().st_size))


if __name__ == "__main__":
    main()
