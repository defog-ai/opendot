"""Write docs/demo.svg, the looping animation at the top of the README.

Run from the repository root: uv run python docs/make_demo_svg.py docs/demo.svg
"""

# ruff: noqa: E501  (SVG markup reads better on one line)

import sys
from html import escape

W, H = 820, 540
LOOP = 26.0  # seconds
FADE = 0.35

BG = "#f7f7f5"
CARD = "#ffffff"
INK = "#1d1d1f"
MUTED = "#6b6b70"
LINE = "#e3e3e0"
ACCENT = "#2f6fed"
GREEN = "#1f8a4c"
USER_BG = "#eef3ff"

css: list[str] = []
body: list[str] = []
counter = 0


def pct(t: float) -> str:
    return f"{max(0.0, min(100.0, t / LOOP * 100)):.3f}%"


def timed(t_in: float, t_out: float, slide: bool = True) -> str:
    """A class that is hidden, appears at t_in, and disappears at t_out."""
    global counter
    counter += 1
    name = f"a{counter}"
    y0 = "translateY(8px)" if slide else "none"
    frames = [
        f"0% {{opacity:0;transform:{y0}}}",
        f"{pct(t_in)} {{opacity:0;transform:{y0}}}",
        f"{pct(t_in + FADE)} {{opacity:1;transform:none}}",
        f"{pct(t_out)} {{opacity:1;transform:none}}",
        f"{pct(t_out + FADE)} {{opacity:0;transform:none}}",
        "100% {opacity:0}",
    ]
    css.append(f"@keyframes {name} {{{' '.join(frames)}}}")
    css.append(f".{name} {{opacity:0;animation:{name} {LOOP}s linear infinite}}")
    return name


def text(x, y, s, size=15, color=INK, weight=400, family="sans", anchor="start"):
    fam = (
        "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"
        if family == "mono"
        else ("-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif")
    )
    return (
        f'<text x="{x}" y="{y}" font-family="{fam}" font-size="{size}" fill="{color}" '
        f'font-weight="{weight}" text-anchor="{anchor}">{escape(s)}</text>'
    )


def avatar(x, y, label, fill):
    return f'<rect x="{x}" y="{y}" width="32" height="32" rx="8" fill="{fill}"/>' + text(
        x + 16, y + 21, label, 13, "#fff", 700, anchor="middle"
    )


def message(y, who, lines, t_in, t_out, *, bot=False, extra=""):
    cls = timed(t_in, t_out)
    fill = ACCENT if bot else "#8a5cf6"
    name = "OpenDot" if bot else "you"
    parts = [f'<g class="{cls}">', avatar(40, y, "O" if bot else "Y", fill)]
    parts.append(text(84, y + 13, name, 14, INK, 700))
    for i, line in enumerate(lines):
        parts.append(text(84, y + 34 + i * 21, line, 15))
    parts.append(extra)
    parts.append("</g>")
    body.append("".join(parts))


def status(y, label, t_in, t_out, done_at=None, done_text=""):
    cls = timed(t_in, t_out, slide=False)
    parts = [f'<g class="{cls}">']
    parts.append(f'<rect x="84" y="{y - 15}" width="3" height="20" fill="{LINE}"/>')
    parts.append(text(98, y, label, 13.5, MUTED, family="mono"))
    parts.append("</g>")
    body.append("".join(parts))
    if done_at is not None:
        cls2 = timed(done_at, t_out, slide=False)
        body.append(
            f'<g class="{cls2}">' + text(560, y, done_text, 13.5, GREEN, 600, "mono") + "</g>"
        )


def scene_title(label, t_in, t_out):
    cls = timed(t_in, t_out, slide=False)
    body.append(
        f'<g class="{cls}">' + text(W - 40, 38, label, 13, MUTED, 500, anchor="end") + "</g>"
    )


def hint(y, number, t_in, t_out):
    cls = timed(t_in, t_out, slide=False)
    body.append(
        f'<g class="{cls}">'
        + text(84, y, f"reply  approve {number}  or  deny {number}", 13.5, MUTED, family="mono")
        + "</g>"
    )


def note(y, label, t_in, t_out):
    cls = timed(t_in, t_out, slide=False)
    body.append(f'<g class="{cls}">' + text(84, y, label, 12, MUTED, 600) + "</g>")


def divider(y, label, t_in, t_out):
    cls = timed(t_in, t_out, slide=False)
    body.append(
        f'<g class="{cls}"><line x1="40" y1="{y}" x2="{W - 40}" y2="{y}" stroke="{LINE}"/>'
        f'<rect x="{W / 2 - 60}" y="{y - 11}" width="120" height="22" rx="11" fill="{CARD}" stroke="{LINE}"/>'
        + text(W / 2, y + 5, label, 12, MUTED, 600, anchor="middle")
        + "</g>"
    )


# ---- scene 1: fix a bug and open a pull request -----------------------------
S1_END = 12.6
scene_title("worker: Codex  ·  reviewer: Claude Code", 0.2, S1_END)
message(
    76,
    "you",
    ['@opendot signup fails when the email has a "+".', "Fix it and open a PR on acme/web."],
    0.4,
    S1_END,
)
note(151, "ON YOUR MACHINE, IN A LOCKED-DOWN CONTAINER", 1.4, S1_END)
status(174, "cloning acme/web", 1.6, S1_END, 2.2, "done")
status(200, "editing forms/signup.py, adding a test", 2.4, S1_END, 3.4, "done")
status(226, "running the test suite", 3.6, S1_END, 4.6, "212 passed")
status(252, "second model reviews the change", 4.8, S1_END, 5.8, "approved")
message(
    282,
    "bot",
    [
        'The email check rejected "+". I fixed the pattern and',
        "added a test. Open the pull request on acme/web?",
    ],
    6.2,
    S1_END,
    bot=True,
)
hint(364, 12, 6.2, S1_END)
message(388, "you", ["approve 12"], 7.8, S1_END)
message(450, "bot", ['Opened acme/web#481  "Accept + in signup emails"'], 9.0, S1_END, bot=True)

# ---- scene 2: watch a web page on a schedule --------------------------------
S2 = 13.2
S2_END = LOOP - 0.5
scene_title("worker: Claude Code  ·  reviewer: opencode", S2, S2_END)
message(
    76,
    "you",
    ["Every weekday at 9am, check example.com/pricing", "and tell me if anything changed."],
    S2 + 0.3,
    S2_END,
)
note(151, "ON YOUR MACHINE, IN A LOCKED-DOWN CONTAINER", S2 + 1.2, S2_END)
status(174, "opening the page in Chrome", S2 + 1.4, S2_END, S2 + 2.2, "done")
status(200, "reading the pricing table", S2 + 2.4, S2_END, S2 + 3.2, "done")
status(226, "second model reviews the schedule", S2 + 3.4, S2_END, S2 + 4.2, "approved")
message(
    256,
    "bot",
    ["New schedule: weekdays at 09:00, post only when", "the page changes. Save it?"],
    S2 + 4.6,
    S2_END,
    bot=True,
)
hint(338, 13, S2 + 4.6, S2_END)
message(362, "you", ["approve 13"], S2 + 6.0, S2_END)
divider(432, "next morning", S2 + 7.6, S2_END)
message(
    454,
    "bot",
    ["example.com/pricing changed: the Team plan went from $20 to $24."],
    S2 + 8.4,
    S2_END,
    bot=True,
)

svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-label="OpenDot fixes a bug and opens a pull request after you approve it, then sets up a daily check of a web page.">
<style>
{chr(10).join(css)}
@media (prefers-reduced-motion: reduce) {{ [class^="a"] {{ animation-play-state: paused; animation-delay: -11s; }} }}
</style>
<rect width="{W}" height="{H}" rx="14" fill="{BG}"/>
<rect x="12" y="12" width="{W - 24}" height="{H - 24}" rx="10" fill="{CARD}" stroke="{LINE}"/>
<circle cx="36" cy="33" r="5" fill="#ff5f57"/><circle cx="54" cy="33" r="5" fill="#febc2e"/><circle cx="72" cy="33" r="5" fill="#28c840"/>
{text(92, 38, "# team-ops", 14, INK, 700)}
<line x1="12" y1="56" x2="{W - 12}" y2="56" stroke="{LINE}"/>
{chr(10).join(body)}
</svg>
"""
with open(sys.argv[1], "w") as out:
    out.write(svg)
