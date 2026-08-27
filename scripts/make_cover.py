"""Render the submission cover image.

The lablab.ai form has a "Cover image" field; the event page states no dimensions, so this
renders 1600x900 (16:9), which is what the event's own cards use and what downsizes cleanly
into a gallery thumbnail.

Every string on the canvas is a claim, so every string is sourced:
  * title / subtitle      -> SUBMISSION_COPY.md sections 1 and 2
  * the four pass stages  -> WRITEUP.md section 1 ("exits -> reconcile -> read the book -> entry")
  * the nine gate labels  -> WRITEUP.md section 2 table, one row each
  * the pinned server     -> src/mcp_client.py::AlpacaMCPClient.SERVER_SPEC
Nothing here is a number the agent has not actually produced: no P&L, no equity curve, no
win rate. The agent has never read a live quote (WRITEUP.md section 4), and a cover image
is not the place to imply otherwise.

Draws at 2x and downsamples, so the hairlines and rounded corners are antialiased.

Usage:  py -3 scripts/make_cover.py [-o assets/cover.png]
"""

import argparse
import pathlib

from PIL import Image, ImageDraw, ImageFont

W, H = 1600, 900
S = 2  # supersampling factor

BG = (10, 14, 20)
PANEL = (18, 24, 33)
HAIRLINE = (32, 42, 56)
INK = (242, 245, 248)
MUTED = (138, 151, 166)
DIM = (95, 108, 122)
AMBER = (242, 193, 78)
TEAL = (79, 209, 165)

FONTS = pathlib.Path(r"C:\Windows\Fonts")
EYEBROW = "ALPACA AI TRADING AGENTS HACKATHON  ·  LABLAB.AI  ·  2026"
TITLE = "Momentum Risk-Cap Options Agent"
SUBTITLE = "An autonomous Alpaca agent that turns price momentum into defined-risk options spreads."
STAGES = ["EXITS", "RECONCILE P&L", "READ THE BOOK", "ENTRY DECISION"]
GATES = [
    "Per-trade budget",
    "Portfolio budget",
    "In-flight risk",
    "Daily-loss breaker",
    "Contract cap",
    "Liquidity screen",
    "Limit-only entries",
    "Naked-short guard",
    "Stand-down on unknowns",
]
FACTS = [
    ("DEFINED-RISK STRUCTURE", "Debit vertical, 7-21 DTE, TP +75% / SL -50%"),
    ("OFFLINE TEST SUITE", "417 tests, no keys, no network"),
    ("DECISION JOURNAL", "Append-only JSONL, every no-trade included"),
]
FOOTER = "Alpaca official MCP server, pinned 2.3.0, over stdio   ·   github.com/run58669-maker/alpaca-momentum-options-agent"


def font(name, size):
    return ImageFont.truetype(str(FONTS / name), size * S)


def tracked(draw, xy, text, fnt, fill, tracking=0):
    """Draw text with letter spacing; returns the advance width (device px)."""
    x, y = xy
    for ch in text:
        if draw is not None:
            draw.text((x, y), ch, font=fnt, fill=fill)
        x += draw_len(ch, fnt) + tracking * S
    return x - xy[0]


_MEASURE = ImageDraw.Draw(Image.new("RGB", (1, 1)))


def draw_len(text, fnt):
    return _MEASURE.textlength(text, font=fnt)


def tracked_width(text, fnt, tracking=0):
    return sum(draw_len(c, fnt) for c in text) + tracking * S * len(text)


def render():
    img = Image.new("RGB", (W * S, H * S), BG)
    d = ImageDraw.Draw(img)

    f_eyebrow = font("segoeui.ttf", 15)
    f_title = font("segoeuib.ttf", 68)
    f_sub = font("segoeuisl.ttf", 27)
    f_label = font("segoeui.ttf", 14)
    f_stage = font("segoeuib.ttf", 21)
    f_gate = font("segoeui.ttf", 22)
    f_gatenum = font("consola.ttf", 15)
    f_foot = font("consola.ttf", 16)

    m = 84 * S  # page margin

    # --- ambient wash behind the headline, so the top third is not flat black -------------
    wash = Image.new("RGB", (W * S, H * S), BG)
    wd = ImageDraw.Draw(wash)
    for i in range(80):
        t = i / 79
        lift = (1 - t) ** 2
        wd.ellipse(
            [m - 300 * S - i * 11 * S, -460 * S - i * 8 * S,
             m + 980 * S + i * 11 * S, 330 * S + i * 8 * S],
            fill=(int(10 + 14 * lift), int(14 + 20 * lift), int(20 + 30 * lift)),
        )
    img = Image.blend(img, wash, 1.0)
    d = ImageDraw.Draw(img)

    y = m

    # --- eyebrow --------------------------------------------------------------------------
    d.rectangle([m, y + 6 * S, m + 3 * S, y + 18 * S], fill=AMBER)
    tracked(d, (m + 14 * S, y), EYEBROW, f_eyebrow, AMBER, tracking=1.6)
    y += 44 * S

    # --- title ----------------------------------------------------------------------------
    d.text((m, y), TITLE, font=f_title, fill=INK)
    y += 92 * S

    # --- subtitle -------------------------------------------------------------------------
    d.text((m, y), SUBTITLE, font=f_sub, fill=MUTED)
    y += 62 * S

    # --- one autonomous pass: four stages, in order ----------------------------------------
    tracked(d, (m, y), "ONE AUTONOMOUS PASS", f_label, DIM, tracking=2.2)
    y += 30 * S

    chip_h = 52 * S
    pad_x = 20 * S
    gap = 15 * S
    arrow_w = 26 * S
    x = m
    for i, stage in enumerate(STAGES):
        w = draw_len(stage, f_stage) + pad_x * 2
        last = i == len(STAGES) - 1
        d.rounded_rectangle([x, y, x + w, y + chip_h], radius=8 * S,
                            fill=PANEL, outline=AMBER if last else HAIRLINE, width=1 * S)
        d.text((x + pad_x, y + (chip_h - (f_stage.size * 1.32)) / 2), stage,
               font=f_stage, fill=INK if last else MUTED)
        x += w
        if not last:
            cy = y + chip_h / 2
            d.line([x + gap, cy, x + gap + arrow_w - 7 * S, cy], fill=HAIRLINE, width=2 * S)
            d.polygon([(x + gap + arrow_w, cy), (x + gap + arrow_w - 8 * S, cy - 4 * S),
                       (x + gap + arrow_w - 8 * S, cy + 4 * S)], fill=DIM)
            x += gap + arrow_w + gap
    y += chip_h + 22 * S

    d.text((m, y), "Exits run first and are never gated by the loss breaker: an agent that is losing money must still be able to close.",
           font=font("segoeuisl.ttf", 19), fill=DIM)
    y += 54 * S

    # --- the nine gates -------------------------------------------------------------------
    panel_top = y
    panel_bot = panel_top + 24 * S + 34 * S + 3 * 40 * S + 20 * S
    d.rounded_rectangle([m, panel_top, W * S - m, panel_bot], radius=12 * S,
                        fill=PANEL, outline=HAIRLINE, width=1 * S)
    d.rounded_rectangle([m, panel_top, m + 4 * S, panel_bot], radius=2 * S, fill=TEAL)

    px = m + 30 * S
    py = panel_top + 24 * S
    tracked(d, (px, py), "NINE GATES, EVERY ONE ENFORCED BEFORE AN ORDER IS SENT", f_label, TEAL, tracking=2.2)
    py += 34 * S

    col_w = (W * S - 2 * m - 60 * S) / 3
    for i, gate in enumerate(GATES):
        cx = px + (i % 3) * col_w
        cy = py + (i // 3) * 40 * S
        d.text((cx, cy + 3 * S), f"{i + 1:02d}", font=f_gatenum, fill=DIM)
        d.text((cx + 30 * S, cy), gate, font=f_gate, fill=INK)

    # --- fact strip -----------------------------------------------------------------------
    f_factlabel = font("segoeui.ttf", 13)
    f_factval = font("segoeui.ttf", 20)
    fx0 = m
    strip_y = panel_bot + 34 * S
    for i, (label, value) in enumerate(FACTS):
        cx = fx0 + i * col_w
        d.rectangle([cx, strip_y + 2 * S, cx + 2 * S, strip_y + 14 * S], fill=HAIRLINE)
        tracked(d, (cx + 12 * S, strip_y), label, f_factlabel, DIM, tracking=1.8)
        d.text((cx + 12 * S, strip_y + 24 * S), value, font=f_factval, fill=MUTED)

    # --- footer ---------------------------------------------------------------------------
    fy = H * S - m + 2 * S
    d.line([m, fy - 16 * S, W * S - m, fy - 16 * S], fill=HAIRLINE, width=1 * S)
    d.text((m, fy), FOOTER, font=f_foot, fill=DIM)

    return img.resize((W, H), Image.LANCZOS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default="assets/cover.png")
    args = ap.parse_args()
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    img = render()
    img.save(out, "PNG", optimize=True)
    print(f"wrote {out}  {img.size[0]}x{img.size[1]}  {out.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
