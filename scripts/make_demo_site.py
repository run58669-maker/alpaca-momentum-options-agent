"""Render the static demo page the submission form's "Application URL" points at.

The hackathon form asks for a demo platform and a reachable URL. This agent is a
command-line process, not a web app, so what gets hosted is a read-only console:
the decision record and the reasons behind it, exactly as the agent wrote them.

Same rule as `make_cover.py` / `make_slides.py` / `make_demo_video.py`: nothing on
the page is retyped. Headline strings are imported from `make_cover`, every block
of agent output is read verbatim out of a capture file that is committed to this
repo, and the numbers that appear in more than one place are cross-checked before
a byte is written -- a drifted count raises SystemExit instead of being published.

Output: docs/index.html (self-contained, no network assets, no wall-clock stamp,
so regenerating to the same path is byte-identical).

    python scripts/make_demo_site.py
"""

import html
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from make_cover import (  # noqa: E402
    EYEBROW,
    FACTS,
    FOOTER,
    GATES,
    STAGES,
    SUBTITLE,
    TITLE,
)

REPO = "https://github.com/run58669-maker/alpaca-momentum-options-agent"
BLOB = REPO + "/blob/main/"

CAP_DRY = "scratch/demo_shot_dry_20260828_0205.txt"
CAP_JOURNAL = "scratch/demo_shot_journal_20260828_0206.txt"
CAP_TESTS = "scratch/demo_shot_tests_20260828_2205.txt"

BANNER = "MOCK DATA -- no Alpaca account attached yet"


def read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def check_test_count():
    """The test count lives in make_cover.FACTS and in the tests capture. Agree or die."""
    capture = read(CAP_TESTS)
    m = re.search(r"Ran (\d+) tests", capture)
    if not m:
        raise SystemExit(CAP_TESTS + ": no 'Ran N tests' line")
    captured = m.group(1)
    for label, value in FACTS:
        if "TEST SUITE" in label:
            if captured not in value:
                raise SystemExit(
                    "test count drift: capture says %s, make_cover.FACTS says %r"
                    % (captured, value)
                )
            return captured
    raise SystemExit("make_cover.FACTS has no test-suite row to cross-check")


def reason_lines():
    """The verbatim reason strings from the dry pass, in the order the agent printed them."""
    out = []
    for line in read(CAP_DRY).splitlines():
        s = line.strip()
        if s.startswith(("exit_skip:", "exit_close:", "exit_hold:", "reason:")):
            out.append(s)
    if len(out) < 5:
        raise SystemExit(
            "%s: expected the exit/entry reason block, found %d lines" % (CAP_DRY, len(out))
        )
    return out


def journal_record():
    record = json.loads(read(CAP_JOURNAL))
    for key in ("reason", "equity", "open_risk", "action"):
        if key not in record:
            raise SystemExit("%s: missing %r" % (CAP_JOURNAL, key))
    return record


CSS = """
:root{--bg:#0a0e14;--panel:#121821;--line:#202a38;--ink:#f2f5f8;--muted:#8a97a6;
--dim:#5f6c7a;--amber:#f2c14e;--teal:#4fd1a5}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.6 ui-sans-serif,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:960px;margin:0 auto;padding:40px 22px 80px}
.eyebrow{color:var(--teal);letter-spacing:.16em;font-size:11px;font-weight:700}
h1{font-size:34px;line-height:1.15;margin:14px 0 8px}
.sub{color:var(--muted);font-size:17px;margin:0 0 22px}
.banner{background:#2a2110;border:1px solid var(--amber);color:var(--amber);
border-radius:8px;padding:11px 14px;font-weight:700;font-size:13px;letter-spacing:.06em}
h2{font-size:13px;letter-spacing:.14em;color:var(--teal);margin:38px 0 12px;font-weight:700}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px 18px}
.stages{display:flex;flex-wrap:wrap;gap:8px;padding:0;margin:0;list-style:none}
.stages li{background:var(--panel);border:1px solid var(--line);border-radius:999px;
padding:7px 14px;font-size:12px;letter-spacing:.08em;font-weight:700}
.gates{display:grid;grid-template-columns:repeat(auto-fit,minmax(215px,1fr));gap:8px;
padding:0;margin:0;list-style:none}
.gates li{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--teal);
border-radius:6px;padding:9px 12px;font-size:13px}
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:10px}
.facts .k{color:var(--dim);font-size:11px;letter-spacing:.12em;font-weight:700}
.facts .v{margin-top:4px;font-size:14px}
pre{margin:0;overflow-x:auto;font:12.5px/1.65 ui-monospace,"Cascadia Mono",Consolas,monospace;
color:#dbe4ec;white-space:pre-wrap;word-break:break-word}
.reasons{list-style:none;padding:0;margin:0}
.reasons li{border-bottom:1px solid var(--line);padding:11px 0;
font:12.5px/1.6 ui-monospace,"Cascadia Mono",Consolas,monospace;color:#dbe4ec;
white-space:pre-wrap;word-break:break-word}
.reasons li:last-child{border-bottom:0}
.k{color:var(--dim)}
a{color:var(--teal)}
.links{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:8px;
padding:0;margin:0;list-style:none}
.links li{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:10px 12px;
font-size:13px}
footer{margin-top:44px;border-top:1px solid var(--line);padding-top:16px;
color:var(--dim);font-size:12px}
.prov{color:var(--dim);font-size:12px;margin-top:8px}
""".strip()


def build():
    tests = check_test_count()
    record = journal_record()
    reasons = reason_lines()
    tests_tail = "\n".join(read(CAP_TESTS).rstrip().splitlines()[-3:])
    pretty = json.dumps(record, indent=2)

    e = html.escape
    p = []
    p.append("<!doctype html>")
    p.append('<html lang="en"><head><meta charset="utf-8">')
    p.append('<meta name="viewport" content="width=device-width,initial-scale=1">')
    p.append("<title>" + e(TITLE) + "</title>")
    p.append("<style>" + CSS + "</style></head><body><div class=wrap>")

    p.append("<div class=eyebrow>" + e(EYEBROW) + "</div>")
    p.append("<h1>" + e(TITLE) + "</h1>")
    p.append("<p class=sub>" + e(SUBTITLE) + "</p>")
    p.append("<div class=banner>" + e(BANNER) + "</div>")

    p.append("<h2>ONE AUTONOMOUS PASS, IN ORDER</h2>")
    p.append("<ul class=stages>" + "".join("<li>" + e(s) + "</li>" for s in STAGES) + "</ul>")

    p.append("<h2>" + e(str(len(GATES))) + " GATES, EVERY ONE ENFORCED BEFORE AN ORDER IS SENT</h2>")
    p.append("<ul class=gates>" + "".join("<li>" + e(g) + "</li>" for g in GATES) + "</ul>")

    p.append("<h2>AT A GLANCE</h2>")
    p.append("<div class=facts>")
    for label, value in FACTS:
        p.append(
            "<div class=panel><div class=k>" + e(label) + "</div><div class=v>" + e(value)
            + "</div></div>"
        )
    p.append("</div>")

    p.append("<h2>THE DECISION RECORD IT WROTE</h2>")
    p.append("<div class=panel><pre>" + e(pretty) + "</pre></div>")
    p.append(
        '<p class=prov>Verbatim from <a href="' + BLOB + CAP_JOURNAL + '">' + e(CAP_JOURNAL)
        + "</a>. Every pass is journalled, including the ones that decide not to trade: this one "
        "is a no-trade, and the <code>reason</code> field says which gate stopped it.</p>"
    )

    p.append("<h2>WHY IT DID THAT, IN ITS OWN WORDS</h2>")
    p.append("<div class=panel><ul class=reasons>")
    for line in reasons:
        p.append("<li>" + e(line) + "</li>")
    p.append("</ul></div>")
    p.append(
        '<p class=prov>Verbatim from <a href="' + BLOB + CAP_DRY + '">' + e(CAP_DRY)
        + "</a>, a <code>--dry</code> pass against the offline mock. Sizing, the strike choice, "
        "the spread that caps the loss and each exit are all explained by the agent at the moment "
        "it acts.</p>"
    )

    p.append("<h2>OFFLINE TEST SUITE</h2>")
    p.append("<div class=panel><pre>" + e(tests_tail) + "</pre></div>")
    p.append(
        '<p class=prov>Verbatim from <a href="' + BLOB + CAP_TESTS + '">' + e(CAP_TESTS)
        + "</a>. " + e(tests) + " tests, no API keys, no network.</p>"
    )

    p.append("<h2>THE REST OF THE SUBMISSION</h2>")
    p.append("<ul class=links>")
    for label, href in [
        ("Source repository (MIT)", REPO),
        ("One-page write-up", BLOB + "WRITEUP.md"),
        ("Demo video (3:02, 1080p)", BLOB + "assets/demo.mp4"),
        ("Slide deck (8 pages, PDF)", BLOB + "assets/slides.pdf"),
        ("Engineering notes", BLOB + "NOTES.md"),
        ("Shot-by-shot demo script", BLOB + "DEMO_SCRIPT.md"),
    ]:
        p.append('<li><a href="' + href + '">' + e(label) + "</a></li>")
    p.append("</ul>")

    p.append("<h2>HONEST STATUS</h2>")
    p.append(
        "<div class=panel>The agent has never read a live quote. Everything above came from the "
        "offline mock of Alpaca's MCP server, and every figure on this page is mock data. The "
        "brand-new dedicated paper account the rules require has not been created yet, so there "
        "is no P&amp;L to show and none is claimed anywhere on this page, in the deck, on the "
        "cover or in the video.</div>"
    )

    p.append("<footer>" + e(FOOTER) + "</footer>")
    p.append("</div></body></html>")
    return "\n".join(p) + "\n"


def main():
    out = ROOT / "docs" / "index.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(build(), encoding="utf-8", newline="\n")
    print("wrote %s (%d bytes)" % (out, out.stat().st_size))


if __name__ == "__main__":
    main()
