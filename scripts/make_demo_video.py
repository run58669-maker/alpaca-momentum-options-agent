"""Render the pitch video from the repository's own capture files.

DEMO_SCRIPT.md is the storyboard; this script is the camera. It does not record a
screen (see DEMO_SCRIPT.md "Recording tool" -- `gdigrab` would capture whatever else is
on the desktop, needs a live interactive session, and is not reproducible). Instead it
renders each shot's terminal text into 1080p frames the same deterministic way
`make_cover.py` and `make_slides.py` render their artifacts, then lets ffmpeg stitch
them. Two runs produce the same bytes, so "regenerate and diff" stays a usable check on
the most visible submission artifact.

Sources, none of them retyped:
  * the terminal text  -> the `scratch/demo_shot_*.txt` capture files named below
  * the narration      -> DEMO_SCRIPT.md's `**Narration:**` blockquotes
  * the shot durations -> the timecodes in DEMO_SCRIPT.md's own shot headings
  * shot 1's screen    -> README.md's opening pitch

Before rendering, every `On screen` block in DEMO_SCRIPT.md is checked line-by-line
against the capture file that shot names. A drifted number fails the render instead of
being filmed; that check is the whole reason the captures are read from disk here.

Audio: none. The narration is burned in as captions. Whether the final cut gets TTS, a
human read, or stays captions-only is a human call and is deliberately not made here.

Usage:  py -3 scripts/make_demo_video.py [-o assets/demo.mp4] [--frames-only]
"""

import argparse
import pathlib
import re

import subprocess
import sys

from PIL import Image, ImageDraw

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from make_cover import (  # noqa: E402
    AMBER, BG, DIM, HAIRLINE, INK, MUTED, PANEL, S, TEAL, draw_len, font, tracked,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent
W, H = 1920, 1080
M = 72                      # frame margin, layout px (device px = layout px * S)
FPS = 30
REPO = "github.com/run58669-maker/alpaca-momentum-options-agent"
MOCK_CAPTION = "MOCK DATA — no Alpaca account attached yet"
TAIL_SECONDS = 3.0          # DEMO_SCRIPT.md shot 6: repo URL held three seconds in silence

# shot key -> (heading prefix in DEMO_SCRIPT.md, capture file, command shown, mock caption?)
# Shot 1 has no capture: its screen is README.md itself. Shot 3 re-flows one line out of
# shot 2's capture, so it points at the same file.
SHOTS = [
    ("1", "## Shot 1 — the claim", None, None, False),
    ("2", "## Shot 2 — one command, the whole loop",
     "scratch/demo_shot_dry_20260827_1510.txt", "py src/agent.py --dry", True),
    ("3", "## Shot 3 — the reason string",
     "scratch/demo_shot_dry_20260827_1510.txt", None, True),
    ("4a", "### 4a — the circuit breaker",
     "scratch/demo_shot_breaker_freshjournal_20260827_1511.txt",
     "py src/agent.py --dry --max-daily-loss-pct 0.001", True),
    ("4b", "### 4b — the duplicate close",
     "scratch/demo_shot_idem_20260827_1512.txt",
     "py scratch/repro_duplicate_close_20260825_1120.py", True),
    ("5", "## Shot 5 — the journal",
     "scratch/demo_shot_journal_20260827_1513.txt",
     "py -c \"...\"   # the last journal record", True),
    ("6", "## Shot 6 — close",
     "scratch/demo_shot_tests_20260828_0207.txt", "py -m unittest discover -s tests", True),
]
TITLES = {
    "1": "The claim", "2": "One command, the whole loop", "3": "The reason string",
    "4a": "The circuit breaker", "4b": "The duplicate close", "5": "The journal",
    "6": "417 tests, standard library only",
}


# --- reading the sources ----------------------------------------------------------------

def split_sections(text):
    """Map every `##`/`###` heading line to the body that follows it."""
    out, key, buf = {}, None, []
    for line in text.splitlines():
        if line.startswith("## ") or line.startswith("### "):
            if key is not None:
                out[key] = "\n".join(buf)
            key, buf = line.rstrip(), []
        elif key is not None:
            buf.append(line)
    if key is not None:
        out[key] = "\n".join(buf)
    return out


def find_section(sections, prefix):
    for head, body in sections.items():
        if head.startswith(prefix):
            return head, body
    raise SystemExit("DEMO_SCRIPT.md has no heading starting %r" % prefix)


def fenced_blocks(body):
    """Every ```-fenced block in a section, as a list of lines (fence lines dropped)."""
    blocks, cur = [], None
    for line in body.splitlines():
        if line.startswith("```"):
            if cur is None:
                cur = []
            else:
                blocks.append(cur)
                cur = None
        elif cur is not None:
            cur.append(line)
    return blocks


def narration(body):
    """The `**Narration:**` blockquote, as one paragraph with markdown emphasis stripped."""
    lines, taking = [], False
    for line in body.splitlines():
        if line.startswith("**Narration:**"):
            taking = True
        elif taking:
            if line.startswith(">"):
                lines.append(line[1:].strip())
            elif line.strip():
                break
    para = " ".join(x for x in lines if x)
    return re.sub(r"\*\*(.+?)\*\*", r"\1", para).replace("*", "").replace("`", "")


def sentences(para):
    """Split narration into caption-sized chunks; very short tails ride with the previous."""
    out = []
    for part in re.split(r"(?<=[.!?])\s+", para):
        part = part.strip()
        if not part:
            continue
        if out and len(part) < 30:
            out[-1] += " " + part
        else:
            out.append(part)
    return out


def _span(head):
    m = re.search(r"\((\d+):(\d\d)-(\d+):(\d\d)\)", head)
    if not m:
        raise SystemExit("no timecode in heading: %s" % head)
    a = int(m.group(1)) * 60 + int(m.group(2))
    b = int(m.group(3)) * 60 + int(m.group(4))
    return float(b - a)


def timeline(sections):
    """Shot durations, in seconds, read out of the headings' own timecodes."""
    spans = {}
    for key, prefix, _cap, _cmd, _mock in SHOTS:
        if prefix.startswith("### "):
            continue
        spans[key] = _span(find_section(sections, prefix)[0])
    # Shot 4's heading covers 4a and 4b together; split it by narration length.
    n_a = len(narration(find_section(sections, "### 4a")[1]))
    n_b = len(narration(find_section(sections, "### 4b")[1]))
    total4 = _span(find_section(sections, "## Shot 4 —")[0])
    spans["4a"] = total4 * n_a / (n_a + n_b)
    spans["4b"] = total4 * n_b / (n_a + n_b)
    return spans


def read_capture(rel):
    return [ln.rstrip("\r\n") for ln in (ROOT / rel).read_text(encoding="utf-8").splitlines()]


def verify_against_capture(key, block, cap_rel):
    """Every quoted On-screen line must still exist in the capture. Drift fails the render."""
    cap = set()
    for ln in read_capture(cap_rel):
        cap.add(ln.strip())
    missing = [ln for ln in block if ln.strip() and ln.strip() not in cap]
    if missing:
        print("DRIFT in shot %s vs %s -- %d quoted line(s) not in the capture:"
              % (key, cap_rel, len(missing)))
        for ln in missing[:5]:
            print("   %s" % ln[:160])
        raise SystemExit(2)
    print("  shot %-2s  %2d line(s) verified against %s" % (key, len(block), cap_rel))


# --- drawing ------------------------------------------------------------------------------

def wrap_mono(lines, fnt, width_px):
    """Soft-wrap terminal text; continuations keep the original indent plus two spaces.

    Breaks at the last space that fits, so a wrapped line reads as words rather than as
    `the mar` / `ks value`. A single token longer than the room is still chopped -- the
    captures contain such tokens (OCC symbols, hashes) and they have to go somewhere.
    """
    cols = max(20, int(width_px // draw_len("M", fnt)))
    out = []
    for raw in lines:
        if len(raw) <= cols:
            out.append(raw)
            continue
        indent = " " * (len(raw) - len(raw.lstrip(" ")) + 2)
        rest, first = raw, True
        while rest:
            room = cols if first else cols - len(indent)
            if len(rest) <= room:
                chunk, rest = rest, ""
            else:
                cut = rest.rfind(" ", 1, room + 1)
                cut = cut if cut > 0 else room
                chunk, rest = rest[:cut].rstrip(), rest[cut:].lstrip(" ")
            out.append(chunk if first else indent + chunk)
            first = False
    return out


def wrap_prop(text, fnt, width_px):
    lines, cur = [], ""
    for word in text.split(" "):
        trial = word if not cur else cur + " " + word
        if draw_len(trial, fnt) <= width_px or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def fit_mono(lines, width_px, height_px):
    """Largest monospace size whose wrapped text still fits the panel."""
    for size in (21, 19, 17, 15, 14, 13, 12, 11, 10):
        f = font("consola.ttf", size)
        wrapped = wrap_mono(lines, f, width_px)
        lh = int(size * 1.42) * S
        if len(wrapped) * lh <= height_px:
            return f, wrapped, lh
    f = font("consola.ttf", 10)
    return f, wrap_mono(lines, f, width_px), int(10 * 1.42) * S


def render_frame(shot_key, screen_lines, command, mock, caption):
    img = Image.new("RGB", (W * S, H * S), BG)
    d = ImageDraw.Draw(img)

    d.rectangle([M * S, 44 * S + 4 * S, M * S + 3 * S, 44 * S + 16 * S], fill=AMBER)
    tracked(d, (M * S + 14 * S, 44 * S), "MOMENTUM RISK-CAP OPTIONS AGENT",
            font("segoeui.ttf", 13), AMBER, tracking=2.0)
    tag = "SHOT %s / 6" % shot_key
    f_tag = font("consola.ttf", 13)
    d.text(((W - M) * S - draw_len(tag, f_tag), 44 * S), tag, font=f_tag, fill=DIM)
    d.line([M * S, 74 * S, (W - M) * S, 74 * S], fill=HAIRLINE, width=1 * S)
    d.text((M * S, 92 * S), TITLES[shot_key], font=font("segoeuib.ttf", 30), fill=INK)

    top = 150
    if command:
        f_cmd = font("consolab.ttf", 18)
        d.text((M * S, top * S), "$ ", font=f_cmd, fill=TEAL)
        d.text((M * S + draw_len("$ ", f_cmd), top * S), command, font=f_cmd, fill=INK)
        top += 40

    pan_top, pan_bot = top * S, (H - 250) * S
    d.rectangle([M * S, pan_top, (W - M) * S, pan_bot], fill=PANEL)
    d.rectangle([M * S, pan_top, M * S + 3 * S, pan_bot], fill=TEAL)
    pad = 22 * S
    f_scr, wrapped, lh = fit_mono(screen_lines, (W - 2 * M) * S - 2 * pad - 3 * S,
                                  pan_bot - pan_top - 2 * pad)
    y = pan_top + pad
    for ln in wrapped:
        d.text((M * S + pad + 3 * S, y), ln, font=f_scr,
               fill=MUTED if ln.startswith(" ") else INK)
        y += lh

    if mock:
        cy = (H - 232) * S
        d.rectangle([M * S, cy, (W - M) * S, cy + 30 * S], fill=(38, 30, 12))
        d.text((M * S + 12 * S, cy + 5 * S), MOCK_CAPTION,
               font=font("segoeuib.ttf", 15), fill=AMBER)

    f_cap = font("segoeui.ttf", 25)
    y = (H - 178) * S
    for ln in wrap_prop(caption, f_cap, (W - 2 * M - 240) * S)[:4]:
        d.text(((W * S - draw_len(ln, f_cap)) / 2, y), ln, font=f_cap, fill=INK)
        y += 38 * S

    d.text((M * S, (H - 44) * S), REPO, font=font("consola.ttf", 13), fill=DIM)
    return img.resize((W, H), Image.LANCZOS)


def render_tail():
    img = Image.new("RGB", (W * S, H * S), BG)
    d = ImageDraw.Draw(img)
    f = font("consolab.ttf", 34)
    d.text(((W * S - draw_len(REPO, f)) / 2, (H / 2 - 40) * S), REPO, font=f, fill=INK)
    sub = "Paper trading only. No live-trading code path exists in this repository."
    f2 = font("segoeuisl.ttf", 22)
    d.text(((W * S - draw_len(sub, f2)) / 2, (H / 2 + 24) * S), sub, font=f2, fill=MUTED)
    return img.resize((W, H), Image.LANCZOS)


# --- assembly ------------------------------------------------------------------------------

def build():
    script = (ROOT / "DEMO_SCRIPT.md").read_text(encoding="utf-8")
    sections = split_sections(script)
    spans = timeline(sections)

    print("verifying On-screen blocks against the captures")
    plan = []
    for key, prefix, cap_rel, command, mock in SHOTS:
        _, body = find_section(sections, prefix)
        if key == "1":
            screen = [ln.rstrip() for ln in
                      (ROOT / "README.md").read_text(encoding="utf-8").splitlines()[:14]]
            print("  shot 1   screen is README.md's opening %d lines" % len(screen))
        else:
            # The On-screen paste is the last fenced block in the section; an earlier one
            # is the command. Shot 3's block is one line pulled out of shot 2's capture.
            screen = fenced_blocks(body)[-1]
            verify_against_capture(key, screen, cap_rel)
            if key == "3":
                screen = wrap_prop(screen[0].strip(), font("consola.ttf", 17),
                                   (W - 2 * M - 60) * S)
        caps = sentences(narration(body))
        weight = sum(len(c) for c in caps)
        for cap in caps:
            plan.append((key, screen, command, mock, cap, spans[key] * len(cap) / weight))
    return plan


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default="assets/demo.mp4")
    ap.add_argument("--frames-dir", default="scratch/demo_frames")
    ap.add_argument("--frames-only", action="store_true")
    args = ap.parse_args()

    plan = build()
    frames_dir = ROOT / args.frames_dir
    # Empty the directory rather than remove it: on Windows a shell sitting in it holds a
    # lock on the directory itself, and the render should not depend on nobody standing there.
    frames_dir.mkdir(parents=True, exist_ok=True)
    for stale in frames_dir.iterdir():
        stale.unlink()

    # Each still is held for a whole number of frames, so the planned timeline and the
    # encoded one are the same number. The concat *demuxer* was measured not to honour its
    # `duration` directives on stills (probe: 3.907+2.744+5.630 = 12.28s planned came out
    # 16.20s), which is why the segments go to the concat *filter* instead, one
    # `-loop 1 -t frames/FPS` input each.
    shots, total_frames = [], 0
    for i, (key, screen, command, mock, cap, secs) in enumerate(plan):
        path = frames_dir / ("frame-%03d.png" % i)
        render_frame(key, screen, command, mock, cap).save(path)
        nf = max(int(2.0 * FPS), int(round(secs * FPS)))
        shots.append((path, nf))
        total_frames += nf
        print("  frame %03d  shot %-2s  %5.2fs  %s" % (i, key, nf / FPS, cap[:64]))
    tail = frames_dir / "frame-tail.png"
    render_tail().save(tail)
    shots.append((tail, int(round(TAIL_SECONDS * FPS))))
    total_frames += shots[-1][1]
    planned = total_frames / FPS
    print("%d frames, %.2fs of video planned" % (len(shots), planned))
    if args.frames_only:
        return

    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for path, nf in shots:
        cmd += ["-loop", "1", "-framerate", str(FPS), "-t", "%.6f" % (nf / FPS),
                "-i", path.name]
    cmd += [
        "-filter_complex",
        "".join("[%d:v]" % i for i in range(len(shots)))
        + "concat=n=%d:v=1:a=0[v]" % len(shots),
        "-map", "[v]",
        # CFR: a variable-rate stream of stills plays back unpredictably in browser players.
        "-fps_mode", "cfr", "-r", str(FPS),
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
        # bitexact + stripped metadata: no encoder string, no timestamps, so two runs of
        # this script produce byte-identical mp4s and "rerender and diff" still works.
        "-fflags", "+bitexact", "-flags:v", "+bitexact", "-map_metadata", "-1",
        "-movflags", "+faststart", str(out),
    ]
    subprocess.run(cmd, cwd=str(frames_dir), check=True)

    # The planned length is only worth printing if it is also the encoded length.
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=nb_frames,duration", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, check=True).stdout.strip()
    got_dur, got_frames = probe.split(",")[0], int(probe.split(",")[1])
    if got_frames != total_frames:
        raise SystemExit("timeline drift: planned %d frames, encoded %d"
                         % (total_frames, got_frames))
    print("%s  %d B  %d frames  %ss (planned %.2fs)"
          % (out, out.stat().st_size, got_frames, got_dur, planned))


if __name__ == "__main__":
    main()
