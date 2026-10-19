#!/usr/bin/env python3
"""Original explanatory figures for the VLA / world-model survey.

Figure 1: where prediction sits in the control loop (four families, 2x2 panels).
Figure 2: coarse evidence map (5 families x 5 evidence dimensions).

Both are qualitative diagrams.  No paper score is plotted and no number is
invented.  Layout is checked geometrically (text width / height per box) so that
overlap and overflow are caught without human or model vision.
"""
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
FIG = HERE / "figures"
FIG.mkdir(exist_ok=True)

FONT = "DejaVu Sans, Helvetica, Arial, sans-serif"
INK = "#1c2530"
MUTED = "#55606d"
BLUE = "#2f5d8a"
LBLUE = "#e6eef8"
AMBER = "#fff8e6"
AMBER_S = "#b98a20"
GREY = "#f2f4f7"
WHITE = "#ffffff"

problems = []


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def tw(s, size):
    """Conservative estimate of rendered text width in px."""
    return len(s) * size * 0.58


def txt(x, y, s, size=18, weight="normal", fill=INK, anchor="middle", style="normal"):
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-family="{FONT}" font-size="{size}" '
            f'font-weight="{weight}" font-style="{style}" fill="{fill}" '
            f'text-anchor="{anchor}">{esc(s)}</text>')


def box(x, y, w, h, lines, size=18, fill=WHITE, stroke="#8a97a8", rx=8,
        weight="normal", tcol=INK, sw=1.6, dash=""):
    for ln in lines:
        if tw(ln, size) + 22 > w:
            problems.append(f"width overflow: {ln!r} needs {tw(ln, size)+22:.0f}px, box {w}px")
    lh = size * 1.34
    if len(lines) * lh + 12 > h:
        problems.append(f"height overflow: {lines!r} needs {len(lines)*lh+12:.0f}px, box {h}px")
    out = [f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{rx}" fill="{fill}" '
           f'stroke="{stroke}" stroke-width="{sw}"'
           + (f' stroke-dasharray="{dash}"' if dash else "") + "/>"]
    n = len(lines)
    y0 = y + h / 2 - (n - 1) * lh / 2 + size * 0.34
    for i, ln in enumerate(lines):
        out.append(txt(x + w / 2, y0 + i * lh, ln, size=size, weight=weight, fill=tcol))
    return "\n".join(out)


def arrow(x1, y1, x2, y2, color=BLUE, sw=2.2, dashed=False):
    dash = ' stroke-dasharray="7,6"' if dashed else ""
    return (f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{color}" '
            f'stroke-width="{sw}"{dash} marker-end="url(#ah)"/>')


HEAD = ('<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
        'markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{BLUE}"/></marker></defs>')


# ---------------------------------------------------------------- Figure 1
def figure1():
    W, H = 1250, 1085
    PW, PH = 575, 400
    px = [40, 630]
    py = [130, 560]

    p = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
         HEAD, f'<rect width="{W}" height="{H}" fill="{WHITE}"/>']
    p.append(txt(40, 46, "Figure 1. Where prediction sits in the control loop", 26, "bold", INK, "start"))
    p.append(txt(40, 78, "Four families that are often compared as if they were one. Boxes show what is computed; "
                         "the dashed note shows what is supervised.", 15, "normal", MUTED, "start"))
    p.append(txt(40, 100, "Solid arrows are data flow, not compute cost. Conceptual diagram: no published score "
                          "is encoded.", 15, "normal", MUTED, "start"))

    heads = [
        ("A. Direct action policy (VLA)", "OpenVLA, pi0, SmolVLA, GR00T N1"),
        ("B. Action plus auxiliary future", "WorldVLA and similar joint models"),
        ("C. World model used as planner", "DINO-WM, V-JEPA 2-AC"),
        ("D. World model used offline", "DreamGen, Dreamer 4, Cosmos, World-Gymnast"),
    ]
    for i, (t, s) in enumerate(heads):
        x, y = px[i % 2], py[i // 2]
        p.append(f'<rect x="{x}" y="{y}" width="{PW}" height="{PH}" rx="12" fill="{WHITE}" '
                 f'stroke="#c6cfda" stroke-width="1.6"/>')
        p.append(f'<rect x="{x}" y="{y}" width="{PW}" height="62" rx="12" fill="{LBLUE}"/>')
        p.append(txt(x + 18, y + 28, t, 19, "bold", BLUE, "start"))
        p.append(txt(x + 18, y + 50, s, 14, "normal", MUTED, "start", style="italic"))

    BW = PW - 36
    # ---- A
    x, y = px[0], py[0]
    bx, cx = x + 18, x + 18 + BW / 2
    rows = [
        (y + 78, 48, ["observation o_t  +  language instruction"]),
        (y + 138, 48, ["pretrained vision-language trunk"]),
        (y + 198, 48, ["action head: chunk a_t ... a_t+H"]),
        (y + 258, 48, ["chunk executed, then re-observe"]),
    ]
    for (ry, rh, lines) in rows:
        p.append(box(bx, ry, BW, rh, lines, 18))
    for i in range(3):
        p.append(arrow(cx, rows[i][0] + rows[i][1], cx, rows[i + 1][0] - 3))
    p.append(box(bx, y + 318, BW, 62, ["Predicts: actions only.",
                                      "No future observation is a target."], 16, AMBER, AMBER_S, dash="6,5"))

    # ---- B
    x, y = px[1], py[0]
    bx, cx = x + 18, x + 18 + BW / 2
    p.append(box(bx, y + 78, BW, 48, ["observation + instruction"], 18))
    p.append(box(bx, y + 138, BW, 56, ["one autoregressive transformer over tokens"], 18))
    sw = (BW - 22) / 2
    p.append(box(bx, y + 214, sw, 48, ["action tokens"], 17))
    p.append(box(bx + sw + 22, y + 214, sw, 48, ["future-frame tokens"], 17))
    p.append(arrow(cx, y + 194, bx + sw / 2, y + 211))
    p.append(arrow(cx, y + 194, bx + sw + 22 + sw / 2, y + 211))
    p.append(box(bx, y + 274, BW, 48, ["actions drive the robot; frames stay auxiliary"], 17))
    p.append(arrow(bx + sw / 2, y + 262, bx + sw / 2, y + 271))
    p.append(arrow(bx + sw + 22 + sw / 2, y + 262, bx + sw + 22 + sw / 2, y + 271, dashed=True))
    p.append(box(bx, y + 318, BW, 62, ["Predicts: actions and future frames.",
                                      "Frames are co-trained, never executed."], 16, AMBER, AMBER_S, dash="6,5"))

    # ---- C
    x, y = px[0], py[1]
    bx, cx = x + 18, x + 18 + BW / 2
    cys = [y + 74, y + 126, y + 178, y + 230, y + 282]
    p.append(box(bx, cys[0], BW, 42, ["encode observation and goal: z_0, z_g"], 17))
    p.append(box(bx, cys[1], BW, 42, ["sample K candidate action sequences"], 17))
    p.append(box(bx, cys[2], BW, 42, ["roll the model forward: z_1 ... z_T"], 17))
    p.append(box(bx, cys[3], BW, 42, ["score by distance to the goal, refine"], 17))
    p.append(box(bx, cys[4], BW, 42, ["execute first k actions, then re-plan"], 17))
    for i in range(4):
        p.append(arrow(cx, cys[i] + 42, cx, cys[i + 1] - 3))
    p.append(box(bx, y + 336, BW, 48, ["Predicts: future states; the prediction is the controller."],
                 15, AMBER, AMBER_S, dash="6,5"))

    # ---- D
    x, y = px[1], py[1]
    bx, cx = x + 18, x + 18 + BW / 2
    dys = [y + 74, y + 124, y + 174, y + 224, y + 274]
    p.append(box(bx, dys[0], BW, 38, ["a little teleoperated data + cheap video"], 16))
    p.append(box(bx, dys[1], BW, 38, ["action-conditioned video world model"], 16))
    p.append(box(bx, dys[2], BW, 38, ["synthetic futures / imagined rollouts"], 16))
    p.append(box(bx, dys[3], BW, 38, ["inverse-dynamics labels (D1) or reward (D2)"], 15))
    p.append(box(bx, dys[4], BW, 38, ["policy trained offline, deployed later"], 16))
    for i in range(4):
        p.append(arrow(cx, dys[i] + 38, cx, dys[i + 1] - 3))
    p.append(box(bx, y + 324, BW, 60, ["Predicts: futures as data or as a training environment.",
                                       "The model never touches the actuators."], 15, AMBER, AMBER_S, dash="6,5"))

    p.append(f'<line x1="40" y1="985" x2="1210" y2="985" stroke="#dfe4ea" stroke-width="1.2"/>')
    p.append(txt(40, 1013, "The question that separates the families is not what the network looks like, but what it is "
                           "trained to output", 15, "normal", MUTED, "start"))
    p.append(txt(40, 1036, "and whether that output is optimised during control, executed directly, or consumed offline "
                           "to manufacture training signal.", 15, "normal", MUTED, "start"))
    p.append(txt(40, 1066, "Conceptual diagram prepared for this survey. A realistic-looking predicted video is not "
                           "evidence of successful control.", 13, "normal", "#8a97a8", "start"))
    p.append("</svg>")
    return "\n".join(p)


# ---------------------------------------------------------------- Figure 2
def wrap(s, n):
    words, out, cur = s.split(), [], ""
    for wd in words:
        if len(cur) + len(wd) + 1 <= n:
            cur = (cur + " " + wd).strip()
        else:
            out.append(cur)
            cur = wd
    if cur:
        out.append(cur)
    return out


def figure2():
    W = 1300
    cols = ["VLA\npolicy", "Action +\nfuture", "World model\nas planner",
            "World model\nas data", "World model\nas trainer"]
    rows = [
        ("Prediction accuracy measured",
         ["no future", "pixel FVD (sim)", "no direct metric", "downstream proxy", "game return only"]),
        ("Closed loop in simulation",
         ["LIBERO suites", "LIBERO + grasp", "6 planning envs.", "policy on synthetic", "Minecraft"]),
        ("Real robot, one laboratory",
         ["yes, several", "narrow, sim-heavy", "yes, two arm types", "humanoid, 22 tasks", "automated, one arm"]),
        ("Real robot, several laboratories",
         ["yes (RoboArena)", "not located", "two labs (V-JEPA 2)", "not located", "not located"]),
        ("Blind or randomised trials",
         ["yes (one lab)", "not located", "not located", "not located", "not located"]),
    ]

    dark = {"LIBERO suites", "LIBERO + grasp", "6 planning envs.", "Minecraft", "yes, several",
            "yes, two arm types", "humanoid, 22 tasks", "automated, one arm",
            "yes (RoboArena)", "yes (one lab)"}
    mid = {"pixel FVD (sim)", "downstream proxy", "game return only", "policy on synthetic",
           "narrow, sim-heavy", "two labs (V-JEPA 2)"}
    light = {"no future", "no direct metric", "not located"}

    LX, LY = 40, 150
    CW, GAP = 186, 8
    X0 = 330
    RH, RGAP = 74, 8
    TOP = LY + 62
    LAST_BOTTOM = TOP + 62 + (len(rows) - 1) * (RH + RGAP) + RH
    FY = LAST_BOTTOM + 30
    H = FY + 76

    p = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
         HEAD, f'<rect width="{W}" height="{H}" fill="{WHITE}"/>']
    p.append(txt(40, 46, "Figure 2. What has been measured, by family", 26, "bold", INK, "start"))
    p.append(txt(40, 78, "A reading of the sixteen papers cited in this report, not a leaderboard. "
                         "Cells are qualitative; they carry no score and no cross-row comparison.",
                 14, "normal", MUTED, "start"))

    lx = LX
    for fill, stroke, lab in [("#2f5d8a", "#2f5d8a", "reported directly"),
                              ("#dbe7f4", "#8fb0d2", "reported, narrow or indirect"),
                              ("#ffffff", "#8a97a8", "not located in our corpus")]:
        p.append(f'<rect x="{lx}" y="{LY - 12}" width="20" height="15" rx="3" fill="{fill}" stroke="{stroke}"/>')
        p.append(txt(lx + 27, LY, lab, 14, "normal", MUTED, "start"))
        lx += 27 + 7.2 * len(lab) + 34

    for j, cname in enumerate(cols):
        cx = X0 + j * (CW + GAP)
        p.append(f'<rect x="{cx}" y="{TOP}" width="{CW}" height="54" rx="6" fill="#eef1f5" stroke="#c9d0d8"/>')
        for i, ln in enumerate(cname.split("\n")):
            p.append(txt(cx + CW / 2, TOP + 22 + i * 19, ln, 15, "bold", INK))

    for i, (rname, cells) in enumerate(rows):
        ry = TOP + 62 + i * (RH + RGAP)
        rl = wrap(rname, 27)
        p.append(f'<rect x="{LX}" y="{ry}" width="270" height="{RH}" rx="6" fill="{GREY}" stroke="#dfe4ea"/>')
        for k, ln in enumerate(rl):
            p.append(txt(LX + 12, ry + RH / 2 - (len(rl) - 1) * 9 + k * 19 + 5, ln, 15, "normal", INK, "start"))
        for j, cval in enumerate(cells):
            cx = X0 + j * (CW + GAP)
            if cval in dark:
                fill, stroke, tcol, wt = "#2f5d8a", "#2f5d8a", "#ffffff", "bold"
            elif cval in mid:
                fill, stroke, tcol, wt = "#dbe7f4", "#8fb0d2", "#22384d", "normal"
            elif cval in light:
                fill, stroke, tcol, wt = "#ffffff", "#8a97a8", "#7b8794", "normal"
            else:
                problems.append(f"unclassified cell: {cval!r}")
                fill, stroke, tcol, wt = "#ffffff", "#8a97a8", INK, "normal"
            if tw(cval, 13) + 12 > CW:
                problems.append(f"cell text too wide: {cval!r}")
            p.append(f'<rect x="{cx}" y="{ry}" width="{CW}" height="{RH}" rx="6" fill="{fill}" stroke="{stroke}"/>')
            p.append(txt(cx + CW / 2, ry + RH / 2 + 5, cval, 13, wt, tcol))

    fy = FY
    p.append(txt(40, fy, "An empty cell means that we did not locate such a measurement in the sixteen papers we read; "
                         "it is not evidence that no such measurement exists.", 13, "normal", MUTED, "start"))
    p.append(txt(40, fy + 22, "\"Simulation\" and \"real robot\" cells describe protocol breadth, not success rates: "
                              "no success number is comparable across rows or columns.", 13, "normal", MUTED, "start"))
    p.append(txt(40, fy + 48, "Prepared for this survey from the cited papers. Conceptual reading aid.", 13,
                 "normal", "#8a97a8", "start"))
    p.append("</svg>")
    return "\n".join(p)


def render(name, svg_text):
    svg_path = FIG / f"{name}.svg"
    png_path = FIG / f"{name}.png"
    svg_path.write_text(svg_text, encoding="utf-8")
    cmd = ["rsvg-convert", "-w", "2500", "-o", str(png_path), str(svg_path)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        problems.append(f"rsvg-convert failed for {name}: {r.stderr[-500:]}")
    return svg_path, png_path


for nm, fn in (("fig1_prediction_in_loop", figure1), ("fig2_evidence_map", figure2)):
    render(nm, fn())

if problems:
    print("LAYOUT PROBLEMS:")
    for pr in problems:
        print(" -", pr)
    sys.exit(1)
print("figures written to", FIG)
for f in sorted(FIG.iterdir()):
    print(f"  {f.name}  {f.stat().st_size} bytes")
