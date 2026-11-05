"""Matchup cards: self-contained HTML pages laid out as a shareable report graphic, 1200 px wide.

Three templates over one payload (matchup.build):

* ``matchup``   style radars, gravity, the showdown, key questions, against this style, the court,
                head to head and the blend
* ``tiebreak``  tiebreak and three-set tendencies, the tiebreak file, court speed by edition, model totals
* ``aura``      tale of the tape plus an editorial layer you write (meters, receipts, the read). The data
                parts fill themselves; the editorial parts are labelled as not in the model.

Nothing here computes a number: it formats what matchup.py produced. Brand strings come from
TENNISSCOUT_BRAND and TENNISSCOUT_HANDLE.
"""

from __future__ import annotations

import html
import math
import os
import re
from datetime import date

from .profiles import AXES

BRAND = os.environ.get("TENNISSCOUT_BRAND", "TennisScout")
HANDLE = os.environ.get("TENNISSCOUT_HANDLE", "")
FILL = {"a": "#a3cf3d", "b": "#5aaee6"}   # on dark
INK = {"a": "#5b8a0f", "b": "#2c86cc"}    # on cream
RED = "#d9442f"
MODEL_NAMES = {"stacked": "stacked model", "walk_forward": "walk-forward stack", "gbm": "GBM layer",
               "elo_sr_blend": "Elo + serve/return", "serve_return": "serve/return", "elo_selected": "Elo"}
IOC_ISO = {
    "ARG": "AR", "AUS": "AU", "AUT": "AT", "BEL": "BE", "BIH": "BA", "BLR": "BY", "BOL": "BO", "BRA": "BR", "BUL": "BG",
    "CAN": "CA", "CHI": "CL", "CHN": "CN", "COL": "CO", "CRO": "HR", "CYP": "CY", "CZE": "CZ", "DEN": "DK", "ECU": "EC",
    "EGY": "EG", "ESP": "ES", "EST": "EE", "FIN": "FI", "FRA": "FR", "GBR": "GB", "GEO": "GE", "GER": "DE", "GRE": "GR",
    "HKG": "HK", "HUN": "HU", "IND": "IN", "IRL": "IE", "ISR": "IL", "ITA": "IT", "JPN": "JP", "KAZ": "KZ", "KOR": "KR",
    "LAT": "LV", "LTU": "LT", "LUX": "LU", "MAR": "MA", "MDA": "MD", "MEX": "MX", "MON": "MC", "NED": "NL", "NOR": "NO",
    "NZL": "NZ", "PAR": "PY", "PER": "PE", "POL": "PL", "POR": "PT", "ROU": "RO", "RSA": "ZA", "RUS": "RU", "SLO": "SI",
    "SRB": "RS", "SUI": "CH", "SVK": "SK", "SWE": "SE", "TPE": "TW", "TUN": "TN", "TUR": "TR", "UKR": "UA", "URU": "UY",
    "USA": "US", "UZB": "UZ", "VEN": "VE", "BAR": "BB", "DOM": "DO", "LBN": "LB", "QAT": "QA", "THA": "TH", "INA": "ID",
}
WORDS = "zero one two three four five six seven eight nine ten".split()


# --------------------------------------------------------------------------------------------- text helpers

def _e(x) -> str:
    return html.escape(str(x), quote=True)


def _md(s: str) -> str:
    """Escape, then **bold** -> <b>."""
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", _e(s))


def _pct(x, nd=0) -> str:
    return "–" if x is None else f"{100 * x:.{nd}f}%"


def _sg(x, nd=1) -> str:
    if x is None:
        return "–"
    if round(x, nd) == 0:
        return f"{0:.{nd}f}"  # never "−0"
    return f"{x:+.{nd}f}".replace("-", "−")


def _num(x, nd=1) -> str:
    return "–" if x is None else f"{x:.{nd}f}"


def _word(n: int, cap: bool = False) -> str:
    w = WORDS[n] if 0 <= n < len(WORDS) else str(n)
    return w.capitalize() if cap else w


def _flag(ioc: str | None) -> str:
    iso = IOC_ISO.get((ioc or "").upper())
    return "".join(chr(0x1F1E6 + ord(c) - 65) for c in iso) if iso else ""


def _date_label(iso: str) -> str:
    d = date.fromisoformat(iso[:10])
    return f"{d:%a} {d.day} {d:%b}"


def _short_date(iso: str | None) -> str:
    if not iso:
        return ""
    d = date.fromisoformat(iso[:10])
    return f"{d:%b} {d.day}"


def _since(d: dict) -> str:
    return date.fromisoformat(d["window_start"]).strftime("%b %Y")


def _nm(d: dict, side: str) -> str:
    return d["players"][0 if side == "a" else 1]["short"]


def _cname(d: dict, side: str, text: str | None = None) -> str:
    return f'<span class="n{side}">{_e(text or _nm(d, side))}</span>'


def _band(d: dict) -> str:
    s = d["event"]["speed"]
    return "" if s is None else f"{max(0, round(s) - 10)}–{min(100, round(s) + 10)}"


def _speed_txt(d: dict) -> str:
    ev = d["event"]
    if ev["speed"] is None:
        return "SPEED n/a"
    approx = "~" if ev["speed_source"] == "previous editions" else ""
    return f'SPEED <b class="lime">{approx}{round(ev["speed"])}</b> {_e(ev["speed_label"])}'


def _h2h_txt(d: dict) -> str:
    h = d["h2h"]
    if not h["meetings"]:
        return "First meeting"
    if h["a"] == h["b"]:
        return f"Head to head: level {h['a']}–{h['b']}"
    lead = "a" if h["a"] > h["b"] else "b"
    return f"Head to head: {_nm(d, lead)} {max(h['a'], h['b'])}–{min(h['a'], h['b'])}"


def _fav(d: dict, key: str = "p_a") -> tuple[str, float]:
    p = d["blend" if key == "blend" else "model"]["p_a"]
    return ("a", p) if p >= 0.5 else ("b", 1 - p)


# --------------------------------------------------------------------------------------------- shared blocks

LOGO = """<svg viewBox="0 0 64 64" width="70" height="70" aria-hidden="true">
<circle cx="32" cy="32" r="25" fill="#cbe44a"/>
<path d="M13 22c9 1 15 6 17 13s9 11 21 9" fill="none" stroke="#18200f" stroke-width="4.5" stroke-linecap="round"/>
<path d="M18 49c2-8 9-12 17-12" fill="none" stroke="#18200f" stroke-width="4.5" stroke-linecap="round" opacity=".9"/>
</svg>"""


def _header(d: dict, title: str, right_lines: list[str]) -> str:
    sub = f"{_e(BRAND)}: ATP" + (f" · By {_e(HANDLE)}" if HANDLE else "")
    right = "".join(f"<div>{line}</div>" for line in right_lines)
    return f"""<header class="top"><div class="brand"><div class="logo">{LOGO}</div>
<div><div class="sub">{sub}</div><h1>{title}</h1></div></div><div class="ev">{right}</div></header>"""


def _event_lines(d: dict, with_h2h: bool = True, matchup_first: bool = False) -> list[str]:
    ev = d["event"]
    when = " · ".join(x for x in (ev.get("round") or "", _date_label(ev["date"]), ev["court"]) if x)
    lines = [f'<b class="evname">{_e(ev["name"])}</b>', _e(when), _speed_txt(d)]
    if matchup_first:
        lines.insert(0, f'<b class="evname">{_e(_nm(d, "a"))} v {_e(_nm(d, "b"))}</b>')
        lines[1] = _e(f"{ev['name']} · {ev.get('round') or ''}".strip(" ·") + f" · {_date_label(ev['date'])}")
        lines[2] = _speed_txt(d) + (" · indoors" if ev["indoor"] else " · outdoors")
        return lines[:3]
    if with_h2h:
        lines.append(_e(_h2h_txt(d)))
    return lines


def _chip(text: str, icon: str = "", cls: str = "") -> str:
    return f'<span class="chip {cls}">{_e(icon) + " " if icon else ""}{_e(text)}</span>'


def _radar(axes: dict, side: str, n_charts: int) -> str:
    """Seven style ranks (0-100) as a heptagon; dashed = tour middle (50)."""
    cx, cy, R = 170, 128, 80
    angles = [-math.pi / 2 + 2 * math.pi * i / len(AXES) for i in range(len(AXES))]

    def pt(i, r):
        return cx + r * math.cos(angles[i]), cy + r * math.sin(angles[i])

    rings = "".join(
        f'<polygon points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in (pt(i, R * f) for i in range(len(AXES))))}" class="ring"/>'
        for f in (0.25, 0.5, 0.75, 1.0))
    spokes = "".join(f'<line x1="{cx}" y1="{cy}" x2="{pt(i, R)[0]:.1f}" y2="{pt(i, R)[1]:.1f}" class="ring"/>' for i in range(len(AXES)))
    mid = " ".join(f"{x:.1f},{y:.1f}" for x, y in (pt(i, R * 0.5) for i in range(len(AXES))))
    shown = [(a, axes[a]) for a in AXES]
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in (pt(i, R * max(v, 4) / 100) for i, (_, v) in enumerate(shown)))
    dots = "".join(f'<circle cx="{pt(i, R * max(v, 4) / 100)[0]:.1f}" cy="{pt(i, R * max(v, 4) / 100)[1]:.1f}" r="4.2" '
                   f'fill="{FILL[side]}" stroke="#141a14" stroke-width="1.5"/>' for i, (_, v) in enumerate(shown))
    labels = ""
    for i, (a, v) in enumerate(shown):
        x, y = pt(i, R + 28)
        anchor = "middle" if abs(x - cx) < 20 else ("start" if x > cx else "end")
        if anchor == "start":
            x -= 14
        elif anchor == "end":
            x += 14
        faded = n_charts < 2 and a not in ("Serve", "Return")
        hot = v >= 75 and not faded
        val = "–" if faded else f"{v:.0f}"
        labels += (f'<text x="{x:.1f}" y="{y - 6:.1f}" text-anchor="{anchor}" class="ax">{a}</text>'
                   f'<text x="{x:.1f}" y="{y + 11:.1f}" text-anchor="{anchor}" class="axv" fill="{FILL[side] if hot else "#ffffff"}">{val}</text>')
    return f"""<svg class="radar" viewBox="0 0 340 258" width="340" height="258" role="img" aria-label="style ranks">
{rings}{spokes}<polygon points="{mid}" class="mid"/>
<polygon points="{poly}" fill="{FILL[side]}" fill-opacity=".55" stroke="{FILL[side]}" stroke-width="2.5" stroke-linejoin="round"/>
{dots}{labels}</svg>"""


def _player_top(d: dict, i: int) -> str:
    side = "ab"[i]
    P, pr = d["players"][i], d["profiles"][i]
    form = d["form"][i]
    spike = pr["spike"]
    n = pr["axis_n"][spike]
    unit = "matches" if spike in ("Serve", "Return") else "charted matches"
    aside = (f'<p class="spike">Biggest spike: <b>{_e(spike.lower())}</b>, ahead of {min(pr["axes"][spike], 99):.0f}% of the tour '
             f'<span class="dim">({n} {unit})</span></p>')
    if form.get("retired_last"):
        r = form["retired_last"]
        aside = (f'<p class="warn">Retired from his last match</p><p class="dim small">({_e(r["event"])} {_e(r["round"] or "")} v '
                 f'{_e(r["opp"])}, {_e(_short_date(r["date"]))}).</p>') + aside
    if pr["n_charts"] < 2:
        aside += '<p class="dim small">Too few charted matches for the rally axes; serve and return only.</p>'
    chips = "".join(_chip(m, cls="tag") for m in pr["mixed"]) + "".join(_chip(w, cls="weak") for w in pr["weaknesses"])
    if chips:
        aside += f'<p class="dim small mixed">Mixed with</p><div class="chips">{chips}</div>'
    return f"""<div class="pcol"><div class="pname"><h2>{_e(P['short'])}</h2>{_chip(pr['archetype'], pr['icon'], 'arch')}</div>
<div class="pbody">{_radar(pr['axes'], side, pr['n_charts'])}<div class="aside">{aside}</div></div></div>"""


def _gravity(d: dict) -> str:
    vals = []
    for i, side in enumerate("ab"):
        g = d["profiles"][i]["gravity"]
        if g is not None:
            vals.append((side, g))
    marks = ""
    tags = ""
    for k, (side, g) in enumerate(sorted(vals, key=lambda x: -x[1])):
        x = min(max(50 - g * 4.5, 3), 97)
        marks += f'<span class="gdot" style="left:{x:.1f}%;background:{FILL[side]}"></span>'
        tags += f'<span class="gtag" style="left:{x:.1f}%;transform:translateX({-100 if k == 0 else 0}%)">{_e(_nm(d, side))} {_sg(g)}</span>'
    if not vals:
        tags = '<span class="gtag" style="left:50%;transform:translateX(-50%)">no charted matches for either man</span>'
    return f"""<div class="gravity"><div class="gtags">{tags}</div><div class="gtrack">{marks}<span class="gmid"></span></div>
<div class="glabels"><span><b>Pulls points short</b> · into the First Strike</span><span class="gc">Gravity</span>
<span>toward The Grind · <b>Pulls points long</b></span></div></div>"""


def _blend_bar(d: dict) -> str:
    pa, pb = d["profiles"]
    side, p = _fav(d, "blend")
    tilt = d["blend"]["tilt_a"] if side == "a" else -d["blend"]["tilt_a"]
    ev = d["event"]
    speed = "n/a" if ev["speed"] is None else f"{round(ev['speed'])} ({ev['speed_label'].capitalize()})"
    court = (f'<span class="tilt">({_sg(100 * tilt)} from the court)</span>' if d["tilt"] else
             '<span class="tilt dimmed">(no court tilt: speed or charting missing)</span>')
    return f"""<div class="blend"><span class="bl">THE BLEND</span><span>({_e(pa['archetype'])} + {_e(pb['archetype'])}) × SPEED {_e(speed)}</span>
<span>=</span><b>{_e(_nm(d, side))} {p:.0%}</b>{court}</div>"""


def _footer(d: dict, notes: list[str]) -> str:
    chip = f'<div class="bchip">{LOGO.replace("70", "26")}<b>{_e(HANDLE or BRAND)}</b><span>research, not betting advice</span></div>'
    return f"""<footer><p class="notes">{" ".join(notes)}</p><div class="foot"><p class="credit">Style data: Match Charting Project
(CC BY-NC-SA 4.0). Results and serve stats: TML-Database, API-Tennis. Model and court speed: {_e(BRAND)}.
Data as of {_e(d['prediction']['data_as_of'] or '–')}.</p>{chip}</div></footer>"""


def _info_line(d: dict) -> str:
    parts = []
    f = d["form"]
    fs = []
    for i, side in enumerate("ab"):
        if f[i]["n"]:
            fs.append(f'{_cname(d, side)} {f[i]["w"]}–{f[i]["l"]} last {f[i]["n"]} ({_num(f[i]["expected"])} exp)'
                      + (", retired last match" if f[i].get("retired_last") else ""))
    if fs:
        parts.append('<b class="lab">FORM</b> ' + " · ".join(fs))
    tr = d["tournament_record"]
    if any(t and sum(t) for t in tr):
        recs = [f'{_cname(d, side)} {t[0]}–{t[1]}' for side, t in zip("ab", tr) if t and sum(t)]
        parts.append(f'<b class="lab">{_e(d["event"]["name"].upper())}</b> ' + " · ".join(recs))
    side, p = _fav(d)
    mk = d.get("market")
    if mk:
        ms, mp = ("a", mk["p_a"]) if mk["p_a"] >= 0.5 else ("b", 1 - mk["p_a"])
        bid, ask = (mk["bid_a"], mk["ask_a"]) if ms == "a" else (mk["bid_b"], mk["ask_b"])
        parts.append(f'<b class="lab">KALSHI</b> {_cname(d, ms)} {bid:.2f}/{ask:.2f} · {mp:.1%} no-vig')
    bside, bp = _fav(d, "blend")
    parts.append(f'<b class="lab">MODEL</b> {_cname(d, side)} {p:.1%} · {_e(d["model"]["confidence"])} confidence · Blend {bp:.0%}')
    return '<p class="info">' + "".join(f'<span class="seg">{x}</span>' for x in parts) + "</p>"


def _sec(title: str, right: str = "") -> str:
    return f'<div class="sh"><h3>{_e(title)}</h3><span>{right}</span></div>'


# --------------------------------------------------------------------------------------------- matchup sections

def _question(d: dict, q: dict) -> str:
    kind = q["kind"]
    edge = q["edge"]
    if edge["side"]:
        cls = f"edge e{edge['side']}" + (" slight" if edge["label"] != "EDGE" else "")
        pill = f'<span class="{cls}">{_e(edge["label"])} {_e(_nm(d, edge["side"]).upper())}</span>'
    else:
        pill = '<span class="edge even">EVEN</span>'
    lo, hi = sorted((q["left"], q["right"]), key=lambda s: s["rank"])
    weak = lo["rank"] < 40

    def text(s, is_weak):
        return f'{_cname(d, s["side"])} {_e(s["what"])} <b class="{"rv weakv" if is_weak else "rv"}">{s["rank"]:.0f}</b>'

    def dot(s, is_weak):
        x = min(max(s["rank"], 1.5), 98.5)
        return f'<span class="qdot" style="left:{x}%;background:{RED if is_weak else FILL[s["side"]]}"></span>'

    def label(x, inner):
        # translateX(-x%) keeps the label inside the track: left-aligned at 0, centred at 50, right-aligned at 100
        return f'<span class="qlab" style="left:{x:.1f}%;transform:translateX(-{x:.1f}%)">{inner}</span>'

    if hi["rank"] - lo["rank"] < 34:
        labels = label((lo["rank"] + hi["rank"]) / 2, f"{text(lo, weak)} · {text(hi, False)}")
    else:
        labels = label(lo["rank"], text(lo, weak)) + label(hi["rank"], text(hi, False))
    seg_l, seg_r = lo["rank"], hi["rank"]
    seg_color = FILL[edge["side"]] if edge["side"] else "#bbb"
    return f"""<div class="q"><div class="qhead"><span class="kind k-{'fs' if kind == 'FIRST STRIKE' else 'ra'}">{_e(kind)}</span>
<span class="qq">{_e(q['question'])}</span>{pill}</div>
<div class="qtrack"><span class="qseg" style="left:{seg_l}%;width:{max(seg_r - seg_l, 0.5)}%;background:{seg_color}"></span>
<span class="qmid"></span>{dot(lo, weak)}{dot(hi, False)}{labels}</div><p class="qd">{_e(q['detail'])}</p></div>"""


def _against(d: dict) -> str:
    cols = []
    for i, side in enumerate("ab"):
        other = "b" if side == "a" else "a"
        rows = ""
        for r in d["against"][i]:
            if not r["n"]:
                rows += f'<div class="arow"><span class="al{" weak" if r["weak"] else ""}">{_e(r["label"])}</span><span class="dim">no matches</span></div>'
                continue
            diff = r["w"] - r["expected"]
            good = diff >= 0
            span = max(r["n"], 1)
            ex = 100 * r["expected"] / span
            ac = 100 * r["w"] / span
            lo_, hi_ = sorted((ex, ac))
            near = f' <span class="dim">· here {r["near"][0]}–{r["near"][1]}</span>' if r.get("near") else ""
            icon = f'{_e(r["icon"])} ' if r.get("icon") else ""
            rows += f"""<div class="arow"><span class="al{' weak' if r['weak'] else ''}">{icon}{_e(r['label'])}{near}</span>
<span class="abar"><span class="afill" style="left:{lo_:.0f}%;width:{max(hi_ - lo_, 2):.0f}%;background:{'#3f9a3a' if good else RED}"></span>
<span class="aexp" style="left:{ex:.0f}%"></span></span>
<span class="aval" style="color:{'#3f8f35' if good else RED}">{r['w']} v {r['expected']:.1f} exp</span><b class="arec">{r['w']}–{r['l']}</b></div>"""
        cols.append(f'<div class="acol"><p class="ahead">{_cname(d, side)} v {_e(_nm(d, other))}’s style</p>{rows}</div>')
    return f'<div class="against">{cols[0]}{cols[1]}</div>'


def _court_text(d: dict) -> str:
    ev, t = d["event"], d["tilt"]
    pa, pb = d["profiles"]
    A, B = _cname(d, "a"), _cname(d, "b")
    out = []
    if ev["speed"] is None:
        return f'<p class="cp">No speed reading for {_e(ev["name"])} yet: no stat lines from this event or its recent editions.</p>'
    if t:
        diff = round(100 * (t["short_here"] - t["short_medium"]))
        out.append(f"<b>{_e(ev['name'])}’s {_e(ev['speed_label'])} court:</b> {100 * t['short_here']:.0f} of every 100 points end within "
                   f"three shots ({_sg(diff, 0)} on a medium court).")
        out.append(f"<b>Short points on serve:</b> {A} wins {pa['measures']['fs_sv']:.0%}, {B} {pb['measures']['fs_sv']:.0%}.")
        tilt = t["tilt_a"]
        if abs(tilt) < 0.005:
            out.append("<b>Neutral ground for these two:</b> the speed moves it by less than half a point, so the matchup above decides it.")
        else:
            h, o = ("a", "b") if tilt > 0 else ("b", "a")
            ph = d["model"]["p_a"] if h == "a" else 1 - d["model"]["p_a"]
            rally = {"a": pa["measures"]["rally"], "b": pb["measures"]["rally"]}
            verb = "helps" if abs(tilt) >= 0.01 else "leans"
            move = f"{ph:.1%} → {ph + abs(tilt):.1%} to win ({_sg(100 * abs(tilt))})"
            if t["d_short"] > 0 and rally[o] > rally[h]:
                why = f"because fewer points reach the Rally, where {_cname(d, o)} wins {rally[o]:.1%} of them"
            elif t["d_short"] < 0 and rally[h] > rally[o]:
                why = f"because more points reach the Rally, where {_cname(d, h)} wins {rally[h]:.1%} of them"
            else:
                why = "through the short points on serve"
            out.append(f"The court {verb} {_cname(d, h)} {why}: {move}.")
    else:
        out.append(f"<b>{_e(ev['name'])}’s {_e(ev['speed_label'])} court</b> reads {round(ev['speed'])} on the speed scale.")
    ca, cb = d["court"]
    called = [(s, c) for s, c in (("a", ca), ("b", cb)) if c["label"] in ("BETTER HERE", "WORSE HERE")]
    if called:
        s, c = max(called, key=lambda x: abs(x[1]["delta"]))
        word = "better" if c["delta"] > 0 else "worse"
        out.append(f"On their records, {_cname(d, s)} is {abs(c['delta']):.0f} points {word} on hold plus break here than elsewhere.")
    elif ca["label"] == "TOO FEW TO CALL" and cb["label"] == "TOO FEW TO CALL":
        out.append("Their records on courts like this are too thin to check.")
    else:
        out.append("Neither man’s record on courts like this is clearly off his usual.")
    return '<p class="cp">' + " ".join(out) + "</p>"


def _court_table(d: dict) -> str:
    ca, cb = d["court"]

    def pill(c):
        cls = {"BETTER HERE": "good", "WORSE HERE": "bad"}.get(c["label"], "neutral")
        return f'<span class="cpill {cls}">{_e(c["label"])}</span>'

    def hb(c, k):
        h, o = c["here"][k], c["other"][k]
        return f'{_pct(h)} <span class="dim">({_pct(o)})</span>' if h is not None else '–'

    def delta(c):
        if c["delta"] is None:
            return "–"
        color = "#3f8f35" if c["label"] == "BETTER HERE" else (RED if c["label"] == "WORSE HERE" else "#222")
        return f'<span style="color:{color}">{_sg(c["delta"], 0)}</span>'

    rows = [("Record here", f"{ca['w']}–{ca['l']}", f"{cb['w']}–{cb['l']}"), ("Holds", hb(ca, "hold"), hb(cb, "hold")),
            ("Breaks", hb(ca, "brk"), hb(cb, "brk")), ("Hold + break v his usual", delta(ca), delta(cb))]
    body = "".join(f'<div class="crow"><b>{l}</b><span>{_e(m)}</span><b class="r">{r}</b></div>' for m, l, r in rows)
    return f"""<div class="ctable"><div class="chead"><span>{_cname(d, 'a')}{pill(ca)}</span>
<span>{pill(cb)}{_cname(d, 'b')}</span></div>{body}<p class="tiny">In brackets: his other matches since {_e(_since(d))}.</p></div>"""


def _h2h_block(d: dict) -> str:
    h = d["h2h"]
    ev = d["event"]
    ms = h["meetings"]
    if not ms:
        return f"""<div class="h2h">{_sec('HEAD TO HEAD')}<p class="h2ht">First meeting</p>
<p class="small">They have never played in the data, so there is no history to read. The court and the styles above are the whole story.</p></div>"""
    lead = "a" if h["a"] > h["b"] else ("b" if h["b"] > h["a"] else None)
    title = (f'{_cname(d, lead)} leads {max(h["a"], h["b"])}–{min(h["a"], h["b"])}' if lead else f"Level {h['a']}–{h['b']}")
    band = ""
    dots = ""
    placed: list[float] = []
    for m in sorted(ms, key=lambda m: m["date"]):
        if m["speed"] is None:
            continue
        x = min(max(m["speed"], 0), 100)
        lift = sum(1 for p in placed if abs(p - x) < 3)
        placed.append(x)
        dots += f'<span class="hdot" style="left:{x:.1f}%;bottom:{27 + 14 * lift}px;background:{FILL["a" if m["a_won"] else "b"]}"></span>'
    if ev["speed"] is not None:
        lo, hi = max(0, ev["speed"] - 10), min(100, ev["speed"] + 10)
        band = (f'<span class="hband" style="left:{lo:.1f}%;width:{hi - lo:.1f}%"></span>'
                f'<span class="htoday" style="left:{ev["speed"]:.1f}%">today {round(ev["speed"])}</span>')
    near = [m for m in ms if m["speed"] is not None and ev["speed"] is not None and abs(m["speed"] - ev["speed"]) <= 10]
    if near:
        wa = sum(m["a_won"] for m in near)
        wb = len(near) - wa
        ns = "a" if wa >= wb else "b"
        near_txt = f'At SPEED {_band(d)}: {_cname(d, ns)} {max(wa, wb)}–{min(wa, wb)}'
    else:
        near_txt = f"At SPEED {_band(d)}: none"
    last = max(ms, key=lambda m: m["date"])
    court = ("indoor " if last["indoor"] == "I" else "") + (last["surface"] or "?").lower()
    sp = f", {round(last['speed'])}" if last["speed"] is not None else ""
    winner = "a" if last["a_won"] else "b"
    sets = " ".join(last["a_sets"]) if last["a_won"] else " ".join(_flip(s) for s in last["a_sets"])
    ret = " ret." if last.get("retired") else ""
    last_txt = f'Last: <b>{_e(last["event"])} {last["year"]}</b> ({_e(court)}{sp}): {_cname(d, winner)} <span class="nw">{_e(sets)}{ret}</span>'
    axis = "".join(f'<span class="htick" style="left:{v}%">{v}</span>' for v in (20, 40, 60, 80, 100))
    return f"""<div class="h2h">{_sec('HEAD TO HEAD')}<p class="h2ht">{title}</p>
<div class="hplot">{band}{dots}<span class="haxis"></span>{axis}<span class="hlab l">slow</span><span class="hlab m">SPEED</span>
<span class="hlab r">fast</span></div><p class="small">{near_txt}</p><p class="small">{last_txt}</p></div>"""


def _flip(s: str) -> str:
    m = re.match(r"^(\d+)-(\d+)(.*)$", s)
    return f"{m.group(2)}-{m.group(1)}{m.group(3)}" if m else s


def render_matchup(d: dict) -> str:
    ev = d["event"]
    since = _since(d)
    qs = "".join(_question(d, q) for q in d["questions"])
    header = _header(d, "SCOUT Report", _event_lines(d))
    notes = [
        "<b>The Blend</b>: the served model’s win chance plus the court tilt from both men’s short-point and rally rates; "
        "a description, not a validated model and not a price.",
        f"<b>Key questions</b>: tour rank 0–100 (style ranks among {d['pool']['charted_players']} men with "
        f"{d['pool']['min_charts']}+ charted matches since {since}; serve and return among men with 10+ tour matches in the last year); red = the weak spot.",
        "<b>Against this style</b>: tour-level wins v what the model expected before each match.",
        "<b>The court</b>: same surface, SPEED within 10; hold + break v his other matches. <b>SPEED</b>: 50 = average tour event, "
        "15 = one standard deviation, from serve points won and aces beyond the players’ usual.",
        "<b>Gravity</b>: points per 100 ending within three shots in his matches, beyond court speed and opponents (+ short, − long).",
        "<b>Style shape</b>: tour rank 0–100, dashed = tour middle; serve and return from all tour matches with stats, the rest "
        "from charted matches (power = winners + forced errors per 100 drives, grind = rallies of 7+ shots won).",
    ]
    return _page(f"{_nm(d, 'a')} v {_nm(d, 'b')}", f"""{header}
<section class="players">{_player_top(d, 0)}{_player_top(d, 1)}</section>
{_gravity(d)}
<main class="board">
<section>{_sec('THE SHOWDOWN', f"{_e(ev['name'])} · {_speed_txt(d)}")}<p class="big">{_e(d['headline'])}</p>{_info_line(d)}</section>
<section>{_sec('THE KEY QUESTIONS', 'dots: where each man ranks on tour, 0 to 100')}{qs}</section>
<section>{_sec('AGAINST THIS STYLE', f'since {_e(since)} · record, then wins v what the model expected')}{_against(d)}</section>
<section>{_sec('THE COURT', f"here = {_e(ev['court'])} at SPEED {_band(d)}, since {_e(since)}")}{_court_text(d)}
<div class="split">{_court_table(d)}{_h2h_block(d)}</div></section>
{_blend_bar(d)}
</main>
{_footer(d, notes)}""")


# --------------------------------------------------------------------------------------------- tiebreak template

def _tile(big: str, text: str, side: str) -> str:
    return f'<div class="tile"><b style="color:{FILL[side]}">{big}</b><span>{text}</span></div>'


def _tb_player(d: dict, i: int) -> str:
    side = "ab"[i]
    P, pr = d["players"][i], d["profiles"][i]
    t = d["tiebreaks"]["a" if i == 0 else "b"]
    tour = d["tour"]
    ev = d["event"]
    since = _since(d)
    meta = []
    if P.get("height_cm"):
        inches = round(P["height_cm"] / 2.54)
        meta.append(f"{inches // 12}′{inches % 12}″")
    if P.get("rank"):
        meta.append(f"ranked {P['rank']} live")
    tiles = []
    if t["band"]:
        tiles.append(_tile(f"{t['band_three']} of {t['band']}",
                           f"went three on {_e(ev['court'])} at SPEED {_band(d)} since {_e(since)}, and {t['band_tb']} of {t['band']} had a tiebreak", side))
    if t["fast_n"] and t["fast_hold"] is not None:
        tiles.append(_tile(_pct(t["fast_hold"], 1), f"holds on fast courts (SPEED 57+) since {_e(since)}, {t['fast_n']} matches", side))
    n, k = t["tb_52"][1], t["tb_52"][0]
    if n:
        tiles.append(_tile(f"{k} of {n}", f"matches had a tiebreak over the last 52 weeks (the tour: {_pct(tour['tb_share'])})", side))
    nb, kb = t["tb_bucket_52"][1], t["tb_bucket_52"][0]
    if nb:
        tiles.append(_tile(f"{kb} of {nb}", f"{_e(ev['court'])} matches over the last 52 weeks had a tiebreak (the tour: {_pct(tour['tb_share_bucket'])})", side))
    return f"""<div class="tbp"><div class="pname"><h2>{_e(P['short'])}</h2>{_chip(pr['archetype'], pr['icon'], 'arch')}</div>
<p class="meta">{_e(", ".join(meta))}</p><div class="tiles">{''.join(tiles[:3])}</div></div>"""


def _tb_headline(d: dict) -> str:
    h = d["h2h"]
    out = []
    if h["meetings"]:
        last = max(h["meetings"], key=lambda m: m["date"])
        w = "a" if last["a_won"] else "b"
        nsets = len(last["a_sets"])
        games = sum(sum(int(x) for x in re.findall(r"^(\d+)-(\d+)", s)[0]) for s in last["a_sets"] if re.match(r"^\d+-\d+", s))
        how = ("in straight sets" if nsets == (last["best_of"] or 3) // 2 + 1 else
               "in a third set" if nsets == 3 else "in a fifth set" if nsets == 5 else f"in {nsets} sets")
        which = "lone" if len(h["meetings"]) == 1 else "last"
        if last.get("retired"):
            sets = " ".join(last["a_sets"]) if last["a_won"] else " ".join(_flip(s) for s in last["a_sets"])
            out.append(f"{_nm(d, w)} won their {which} meeting when {_nm(d, 'b' if w == 'a' else 'a')} retired at {sets}.")
        else:
            out.append(f"{_nm(d, w)} won their {which} meeting {how} and {games} games.")
    else:
        out.append(f"{_nm(d, 'a')} and {_nm(d, 'b')} have never met.")
    best = None
    for i, side in enumerate("ab"):
        k, n = d["tiebreaks"][side]["tb_bucket_52"]
        if n >= 5 and (best is None or k / n > best[2]):
            best = (side, (k, n), k / n)
    if best:
        side, (k, n), _ = best
        out.append(f"{_word(k, True)} of {_nm(d, side)}’s {_word(n)} {d['event']['court']} matches over the last year found a tiebreak.")
    return " ".join(out)


def render_tiebreak(d: dict) -> str:
    ev = d["event"]
    tb = d["tiebreaks"]
    tour = d["tour"]
    rows = []
    for i, side in enumerate("ab"):
        items = ""
        for m in d["recent"][i]:
            chips = "".join(f'<span class="set{" tb" + side if t else ""}">{_e(s)}</span>' for s, t in zip(m["sets"], m["tb"]))
            items += f"""<div class="trow"><span class="wl {'w' if m['won'] else 'l'}">{'W' if m['won'] else 'L'}</span>
<div class="topp"><b>{_e(m['opp'])}</b><small>{_e(m['event'])} {_e(m['round'] or '')} · {_e(_short_date(m['date']))}</small></div>
<div class="sets">{chips}{' <span class="dim">ret.</span>' if m['retired'] else ''}</div><b class="tbn">{m['tiebreaks']}</b></div>"""
        rows.append(f'<div class="tcol"><div class="thead">{_cname(d, side)}<span>TB</span></div>{items}</div>')
    h = d["h2h"]
    met = ""
    if h["meetings"]:
        last = max(h["meetings"], key=lambda m: m["date"])
        w = "a" if last["a_won"] else "b"
        sets = " ".join(last["a_sets"]) if last["a_won"] else " ".join(_flip(s) for s in last["a_sets"])
        ret = f" ({_e(_nm(d, 'b' if w == 'a' else 'a'))} retired)" if last.get("retired") else ""
        met = f" They last met in best-of-{last['best_of'] or 3}: <b>{_e(_nm(d, w))} {_e(sets)}</b>{ret} at {_e(last['event'])} {last['year']}."
    games = f"Their last 10 average <b>{_num(tb['games_last10'])} games</b>." if tb["games_last10"] else ""
    dist = []
    for i, side in enumerate("ab"):
        t = tb[side]
        dist.append(f"""<div class="dist d{side}"><b>{_e(_nm(d, side))}</b><p>{t['bo3']} best-of-3 matches over the last 52 weeks:
{t['bo3_three']} went three sets, {t['bo3_tb']} had a tiebreak.</p></div>""")
    hist = ev["history"]
    bars = ""
    if hist:
        top = max(h_["speed"] for h_ in hist) or 1
        for h_ in hist:
            hgt = 30 + 70 * h_["speed"] / max(top, 1)
            bars += f"""<div class="ebar{' cur' if h_['current'] else ''}"><em>{round(h_['speed'])}</em><i style="height:{hgt:.0f}px"></i><small>{h_['year']}</small></div>"""
    prev = [round(h_["speed"]) for h_ in hist if not h_["current"]][-3:][::-1]
    if ev["speed"] is None:
        court_txt = f"No speed reading for {_e(ev['name'])} yet."
    elif ev["speed_source"] == "this week":
        court_txt = f"{_e(ev['name'])} is playing <b>{round(ev['speed'])}</b> this week" + (
            f" after <b>{_e(', '.join(map(str, prev[:-1])) + (' and ' if len(prev) > 1 else '') + str(prev[-1]))}</b> the last {_word(len(prev))} {'years' if len(prev) > 1 else 'year'}." if prev else ".")
    else:
        court_txt = f"No results from this week yet; {_e(ev['name'])} played {', '.join(map(str, prev))} in its last editions, so the card uses <b>~{round(ev['speed'])}</b>."
    c = d["prediction"]["components"]
    ss = c.get("set_scores") or {}
    three = sum(v for k, v in ss.items() if sum(map(int, k.split("-"))) == 3) if d["event"]["best_of"] == 3 else None
    totals = []
    if c.get("expected_games") is not None:
        totals.append(f"Expected total <b>{c['expected_games']:.1f} games</b>")
    if three is not None:
        totals.append(f"Three sets <b>{three:.0%}</b>")
    if c.get("expected_tiebreaks") is not None:
        totals.append(f"Tiebreaks <b>{c['expected_tiebreaks']:.2f}</b> expected")
    side, p = _fav(d)
    header = _header(d, "SCOUT Report", _event_lines(d))
    notes = [
        "<b>Hold, tiebreak and three-set rates</b>: tour-level matches (no Challengers), retirements out where noted. "
        "<b>Last five</b> includes every level in the data.",
        "<b>Model totals</b>: the serve/return Markov chain anchored to the served win chance. <b>SPEED</b>: 50 = average tour event, "
        "15 = one standard deviation.",
    ]
    return _page(f"{_nm(d, 'a')} v {_nm(d, 'b')} tiebreaks", f"""{header}
<section class="tbplayers">{_tb_player(d, 0)}{_tb_player(d, 1)}</section>
<p class="tourline">The tour holds {_pct(tour['hold_bucket'], 1)} on {_e(ev['court'])} courts over the last year.</p>
<main class="board">
<section>{_sec('THE SHOWDOWN', f"{_e(ev['name'])} · {_speed_txt(d)}")}<p class="big">{_e(_tb_headline(d))}</p>
<p class="info"><span class="seg"><b class="lab">MODEL</b> {_cname(d, side)} {p:.1%} · {_e(MODEL_NAMES.get(d['model']['name'], d['model']['name']))}
· {_e(d['model']['confidence'])} confidence</span></p></section>
<section>{_sec('THE TIEBREAK FILE', 'last five for each man, every level · tiebreak sets in color')}
<div class="tbbig"><b class="tbnum">{tb['tiebreaks_last10']}</b><span>tiebreaks in their<br>last 10 matches</span><p class="cp">{games}{met}</p></div>
<div class="tfile">{rows[0]}{rows[1]}</div></section>
<section>{_sec('GOING THE DISTANCE', 'best-of-3, tour level, retirements out')}<div class="dists">{dist[0]}{dist[1]}</div></section>
<section>{_sec('THE COURT', f"{_e(ev['name'])} by edition · SPEED")}<div class="courtrow"><p class="cp">{court_txt}</p><div class="ebars">{bars}</div></div>
<p class="info"><span class="seg"><b class="lab">MODEL</b> {' · '.join(totals) or 'no scoreline model for this pair'}</span></p></section>
{_blend_bar(d)}
</main>
{_footer(d, notes)}""")


# --------------------------------------------------------------------------------------------- aura template

def _tone(notes: dict | None, side: str) -> str:
    """'aura' / 'cringe' are editorial calls, so they only appear when your notes make them."""
    if not notes:
        return f"plain{side}"
    return ((notes.get(side) or {}).get("tone")) or ("aura" if side == "a" else "cringe")


def _aura_card(d: dict, i: int, notes: dict | None) -> str:
    side = "ab"[i]
    P, pr = d["players"][i], d["profiles"][i]
    n = (notes or {}).get(side) or {}
    tone = _tone(notes, side)
    meta = []
    if P.get("rank"):
        meta.append(f"No. {P['rank']}")
    elif P.get("rank_last_known"):
        meta.append(f"No. {P['rank_last_known']} ({_short_date(P['rank_last_known_date'])})")
    meta.append({"L": "lefty", "R": "righty"}.get(P.get("hand") or "", ""))
    if P.get("career_high"):
        meta.append(f"career high No. {P['career_high']}")
    meta.append(f"{P['titles']} title{'s' if P['titles'] != 1 else ''}")
    meta = " · ".join(m for m in meta if m)
    receipts = n.get("receipts") or _data_receipts(d, i)
    label = "RECEIPTS" if n.get("receipts") else "RECEIPTS (FROM THE DATA)"
    items = "".join(f'<li><span class="rn">{k + 1}</span><p>{_md(r)}</p></li>' for k, r in enumerate(receipts[:4]))
    meter = ""
    if n.get("meter") is not None:
        fill = min(max(float(n["meter"]), 0), 100)
        meter = f"""<div class="meter"><span class="ml">{'CRINGE' if tone == 'cringe' else 'AURA'} METER</span>
<b class="mv">{_e(n.get('meter_label', ''))}</b><div class="mtrack"><span style="width:{fill:.0f}%"></span></div></div>"""
    tag = f'<p class="tagline">{_e(n["tagline"])}</p>' if n.get("tagline") else ""
    return f"""<div class="acard {tone}"><h2><span class="flag">{_flag(P.get('ioc'))}</span> {_e(P['name'])}</h2>
<p class="meta">{_e(meta)}</p>{_chip(pr['archetype'] + (' · lefty' if P.get('hand') == 'L' else ''), pr['icon'], 'arch light')}{tag}{meter}
<p class="rl">{tone.upper() + ' ' if tone in ('aura', 'cringe') else ''}{label}</p><ol class="receipts">{items}</ol></div>"""


def _data_receipts(d: dict, i: int) -> list[str]:
    P, pr = d["players"][i], d["profiles"][i]
    out = []
    if P.get("best_slam"):
        out.append(f"**The trophy case:** {P['titles']} tour title{'s' if P['titles'] != 1 else ''}; best Grand Slam: {P['best_slam'].lower()}.")
    spike = pr["spike"]
    out.append(f"**Biggest spike: {spike.lower()}**, ahead of {min(pr['axes'][spike], 99):.0f}% of the tour.")
    f = d["form"][i]
    if f["n"]:
        diff = f["w"] - f["expected"]
        out.append(f"**Form:** {f['w']}–{f['l']} in his last {f['n']} tour matches, {abs(diff):.1f} wins "
                   f"{'above' if diff >= 0 else 'below'} what the model expected.")
    t = d["tournament_record"][i]
    if t and sum(t):
        out.append(f"**At {d['event']['name']}:** {t[0]}–{t[1]} career.")
    c = d["court"][i]
    if c["label"] in ("BETTER HERE", "WORSE HERE"):
        out.append(f"**On courts like this:** hold plus break {_sg(c['delta'], 0)} points against his usual.")
    return out


def render_aura(d: dict, notes: dict | None = None) -> str:
    A, B = d["players"]
    tape = [("Best Grand Slam", A.get("best_slam") or "–", B.get("best_slam") or "–"),
            ("Career titles", str(A["titles"]), str(B["titles"])),
            ("Last five matches", f"{d['last_five'][0][0]}–{d['last_five'][0][1]}", f"{d['last_five'][1][0]}–{d['last_five'][1][1]}")]
    tone_a, tone_b = _tone(notes, "a"), _tone(notes, "b")
    rows = "".join(f'<div class="tape"><b class="t-{tone_a}">{_e(x)}</b><span>{_e(lbl)}</span><b class="t-{tone_b} r">{_e(y)}</b></div>' for lbl, x, y in tape)
    read = (notes or {}).get("read")
    kill = (notes or {}).get("kill")
    side, p = _fav(d)
    mk = d.get("market")
    market = (f'<div class="mrow"><span>{_e(A["short"])}</span><b>{_pct(mk["p_a"], 1)}</b></div>'
              f'<div class="mrow"><span>{_e(B["short"])}</span><b>{_pct(1 - mk["p_a"], 1)}</b></div>'
              f'<p class="tiny">Kalshi mid, margin removed</p>') if mk else ""
    model = (f'<div class="mrow"><span>{_e(A["short"])}</span><b>{_pct(d["model"]["p_a"], 1)}</b></div>'
             f'<div class="mrow"><span>{_e(B["short"])}</span><b>{_pct(1 - d["model"]["p_a"], 1)}</b></div>')
    h = d["h2h"]
    h2h = (f"{_nm(d, 'a' if h['a'] > h['b'] else 'b')} {max(h['a'], h['b'])}–{min(h['a'], h['b'])}" if h["a"] != h["b"]
           else ("First meeting" if not h["meetings"] else f"Level {h['a']}–{h['b']}"))
    header = _header(d, '<span class="nw">SCOUT <span class="lime">AURA</span> REPORT</span>', _event_lines(d, matchup_first=True))
    word = {"aura": "AURA", "cringe": "CRINGE"}
    left_label, right_label = word.get(tone_a, ""), word.get(tone_b, "")
    caps = (f"{left_label} V {right_label}" if left_label and right_label else f"{A['short'].upper()} V {B['short'].upper()}")
    read_html = f'<section class="read"><h3>THE AURA READ</h3><p>{_md(read)}</p></section>' if read else ""
    kill_html = f'<div class="kill"><h3>WHAT COULD KILL THE VIBES</h3><p>{_md(kill)}</p></div>' if kill else \
        f'<div class="kill"><h3>THE MODEL’S VIEW</h3><p>{_md(d["headline"])}</p></div>'
    notes_txt = ["<b>Editorial</b>: meters, receipts and the read are written by hand and are not in the model. "
                 "<b>Tale of the tape</b> and the numbers on the right come from the data."]
    return _page(f"{A['short']} v {B['short']} aura", f"""{header}
<div class="aurahead"><span class="caps">{_e(caps)} · WHO OWNS THE BUILDING</span><span class="vibes">Vibes-based. Not in the model.</span></div>
<section class="auras">{_aura_card(d, 0, notes)}{_aura_card(d, 1, notes)}</section>
<section class="tapebox"><div class="tape th"><b class="t-{tone_a}">{(left_label + ' · ') if left_label else ''}{_e(A['short'].upper())}</b>
<span>TALE OF THE TAPE</span><b class="t-{tone_b} r">{_e(B['short'].upper())}{(' · ' + right_label) if right_label else ''}</b></div>{rows}</section>
{read_html}
<section class="aurafoot">{kill_html}<div class="market"><h3>THE MODEL</h3>{model}{('<h3 class="mt">THE MARKET</h3>' + market) if market else ''}
<div class="mrow dim"><span>Head to head</span><span>{_e(h2h)}</span></div></div></section>
{_footer(d, notes_txt)}""")


# --------------------------------------------------------------------------------------------- page

def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)} · {_e(BRAND)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<style>{CSS}</style></head><body><div class="stage"><div class="card" id="card">{body}</div></div>
<script>(function(){{var c=document.getElementById('card'),s=c.parentNode;function f(){{var k=Math.min(1,(window.innerWidth-0)/1200);
c.style.transform='scale('+k+')';s.style.height=(c.offsetHeight*k)+'px';}}window.addEventListener('resize',f);f();}})();</script>
</body></html>"""


CSS = """
:root{--bg:#171d18;--ink:#f4f4ee;--cream:#f4f1e6;--muted:#a9b0a4;--lime:#cbe44a;--a:#a3cf3d;--b:#5aaee6;--ia:#5b8a0f;--ib:#2c86cc;
--red:#d9442f;--line:#d8d3c2;--panel:#e9e7de;--tan:#ecdcb8}
*{box-sizing:border-box;margin:0;padding:0}
html,body{background:#0f130f}
body{font-family:Outfit,Inter,system-ui,sans-serif;color:var(--ink);-webkit-font-smoothing:antialiased}
.stage{width:100%;overflow:hidden}
.card{width:1200px;transform-origin:top left;padding:34px 44px 30px;position:relative;overflow:hidden;
background:radial-gradient(1200px 700px at 105% -10%,rgba(120,140,110,.18),transparent 60%),
radial-gradient(900px 500px at 50% 115%,rgba(150,170,40,.22),transparent 70%),var(--bg)}
.card:before{content:"";position:absolute;right:-180px;top:-160px;width:760px;height:760px;border-radius:50%;
border:70px solid rgba(255,255,255,.035);pointer-events:none}
.lime{color:var(--lime)}.nw{white-space:nowrap}.dim{color:#8b9187}.small{font-size:15px}.tiny{font-size:12.5px;color:#666;margin-top:6px}
.na{color:var(--ia);font-weight:700}.nb{color:var(--ib);font-weight:700}
.players .na,.gravity .na,.tbplayers .na{color:var(--a)}.players .nb,.tbplayers .nb{color:var(--b)}
header.top{display:flex;justify-content:space-between;align-items:flex-start;gap:20px;position:relative}
.brand{display:flex;gap:18px;align-items:center}
.logo{width:92px;height:92px;border-radius:24px;background:#0b0d0b;display:grid;place-items:center;
box-shadow:6px 6px 0 #000;border:2px solid #2a2f2a}
.logo svg{width:68px;height:68px}
.brand .sub{color:var(--lime);font-weight:600;font-size:21px;letter-spacing:.2px}
.brand h1{font-size:58px;font-weight:800;letter-spacing:-.5px;line-height:1.02;margin-top:4px}
.brand h1 .nw{font-size:52px}
.ev{text-align:right;font-size:19px;line-height:1.4;color:#dcdfd5}
.ev .evname{font-size:26px;color:#fff;font-weight:700}
.players{display:grid;grid-template-columns:1fr 1fr;gap:26px;margin-top:16px;position:relative}
.pname{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.pname h2{font-size:48px;font-weight:800;letter-spacing:-.5px;line-height:1.05}
.chip{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:6px 14px;font-weight:600;font-size:15px;
border:1.5px solid #3c443d;background:#202720;color:#fff;white-space:nowrap}
.chip.arch{font-size:19px;padding:7px 16px}
.chip.weak{border-color:#c4533f;color:#ffb3a5;background:#3a201b}
.chip.light{background:#fff;color:#111;border:2px solid #111}
.pbody{display:flex;align-items:flex-start;gap:0;margin-top:6px}
.radar{flex:0 0 auto;margin-left:-28px}
.radar .ring{fill:none;stroke:#2f3a30;stroke-width:1.2}
.radar .mid{fill:none;stroke:#e9eee3;stroke-opacity:.55;stroke-dasharray:4 4;stroke-width:1.4}
.radar .ax{fill:#fff;font-size:13.5px;font-weight:700;font-family:Outfit,Inter,sans-serif}
.radar .axv{font-size:14px;font-weight:800;font-family:Outfit,Inter,sans-serif}
.aside{padding-top:34px;font-size:16.5px;line-height:1.33;max-width:240px;margin-left:-14px}
.aside p{margin-bottom:8px}.aside .spike b{font-weight:800}
.aside .mixed{margin-top:10px;margin-bottom:6px}.chips{display:flex;flex-wrap:wrap;gap:7px}
.warn{color:#ff6d58;font-weight:800}
.gravity{margin:0 0 14px;position:relative}
.gtags{position:relative;height:36px}
.gtag{position:absolute;bottom:4px;background:#0b0d0b;color:#fff;font-weight:700;font-size:15px;border-radius:9px;padding:5px 11px;
white-space:nowrap;border:1px solid #333}
.gtrack{position:relative;height:12px;border-radius:99px;background:linear-gradient(90deg,#86a929 0%,#c9d27a 30%,#efe2b8 50%,#a9cbe2 70%,#4fa1dc 100%)}
.gmid{position:absolute;left:50%;top:-6px;width:2px;height:24px;background:#efe2b8}
.gdot{position:absolute;top:50%;width:24px;height:24px;border-radius:50%;transform:translate(-50%,-50%);border:3px solid #10140f;
box-shadow:0 0 0 2px rgba(255,255,255,.75)}
.glabels{display:flex;justify-content:space-between;margin-top:9px;font-size:15px;color:#e6e8e0}
.glabels .gc{font-weight:700;color:#fff}
.board{background:var(--cream);color:#151815;border-radius:24px;padding:22px 28px 22px;box-shadow:8px 8px 0 #000;position:relative}
.board section{border-top:1.5px solid var(--line);padding:11px 0 9px}
.board section:first-child{border-top:0;padding-top:2px}
.sh{display:flex;justify-content:space-between;align-items:baseline;gap:14px}
.sh h3{font-size:14.5px;letter-spacing:3px;color:#5b7d12;font-weight:800;margin-bottom:8px}
.sh span{font-size:15px;color:#555}
.big{font-size:29px;font-weight:800;line-height:1.24;letter-spacing:-.2px;margin:2px 0 8px}
.info{font-size:15.5px;color:#222;display:flex;flex-wrap:wrap;column-gap:22px;row-gap:2px}.info .seg{white-space:nowrap}
.lab{color:#5b7d12;letter-spacing:1px;font-size:14px}
.q{background:var(--panel);border-radius:16px;padding:8px 16px 6px;margin:6px 0 5px}
.qhead{display:flex;align-items:center;gap:12px}
.kind{font-size:12.5px;font-weight:800;letter-spacing:1px;border-radius:99px;padding:4px 10px}
.k-fs{background:#6c9a19;color:#fff}.k-ra{background:var(--tan);color:#6f5317}
.qq{font-size:22px;font-weight:800;flex:1}
.edge{font-size:14px;font-weight:800;border-radius:99px;padding:5px 13px;letter-spacing:.5px;color:#fff}
.edge.ea{background:#6c9a19}.edge.eb{background:#3c95d6}.edge.even{background:#9a9a92}
.edge.slight.ea{background:#dcebc0;color:#4b7410}.edge.slight.eb{background:#d3e7f6;color:#226fae}
.qtrack{position:relative;height:44px;margin:0 4px}
.qtrack:before{content:"";position:absolute;left:0;right:0;top:32px;height:3px;background:#cfccc0;border-radius:3px}
.qmid{position:absolute;left:50%;top:25px;width:2px;height:16px;background:#bcb8aa}
.qseg{position:absolute;top:32px;height:3px;opacity:.75}
.qdot{position:absolute;top:33.5px;width:20px;height:20px;border-radius:50%;transform:translate(-50%,-50%);border:3px solid #fff;
box-shadow:0 0 0 1.5px rgba(0,0,0,.25)}
.qlab{position:absolute;top:1px;font-size:15.5px;white-space:nowrap;color:#333}
.qlab .rv{font-size:17px}.qlab .weakv{color:var(--red)}
.qd{font-size:14.5px;color:#333;margin-top:2px}
.against{display:grid;grid-template-columns:1fr 1fr;gap:26px}
.acol+.acol{border-left:1.5px solid var(--line);padding-left:24px}
.ahead{font-size:16px;margin-bottom:4px}
.arow{display:grid;grid-template-columns:1fr 70px 104px 56px;align-items:center;gap:10px;font-size:15px;padding:3px 0}
.arec{white-space:nowrap}
.al.weak{color:var(--red)}
.abar{position:relative;height:12px;background:#dedbd0;border-radius:3px}
.afill{position:absolute;top:0;bottom:0;border-radius:3px}
.aexp{position:absolute;top:-3px;width:2px;height:18px;background:#222}
.aval{font-weight:700;text-align:right}.arec{text-align:right;font-size:16px}
.board p{line-height:1.4}.board .cp{font-size:16.5px}
.board .big{font-size:27px;line-height:1.22}.board .qd{font-size:14.5px;line-height:1.35}.board .small{font-size:15px}
.board .tiny{font-size:12.5px}
.split{display:grid;grid-template-columns:1.15fr 1fr;gap:22px;margin-top:8px}
.ctable{background:var(--panel);border-radius:16px;padding:10px 16px}
.crow{display:grid;grid-template-columns:1fr 1.3fr 1fr;align-items:center;font-size:15px;padding:2px 0}
.crow b{font-size:18.5px}.crow span{text-align:center;color:#444}.crow .r{text-align:right}
.chead{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:2px 0 6px;white-space:nowrap}
.chead .na,.chead .nb{font-size:24px;font-weight:800}
.cpill{display:inline-block;font-size:11px;font-weight:800;letter-spacing:.6px;border-radius:99px;padding:3px 8px;margin:0 8px;vertical-align:middle}
.cpill.neutral{background:#dcd9cf;color:#666}.cpill.good{background:#6c9a19;color:#fff}.cpill.bad{background:var(--red);color:#fff}
.h2h{border-left:1.5px solid var(--line);padding-left:20px}
.h2h .sh h3{margin-bottom:2px}
.board .h2ht{font-size:23px;font-weight:800;color:#222}
.hplot{position:relative;height:74px;margin:4px 6px 2px}
.haxis{position:absolute;left:0;right:0;bottom:34px;height:2px;background:#cfccc0}
.htick{position:absolute;bottom:16px;transform:translateX(-50%);font-size:11.5px;color:#666}
.hlab{position:absolute;bottom:0;font-size:11.5px;color:#666;transform:translateX(-50%)}
.hlab.l{left:20%}.hlab.m{left:60%}.hlab.r{left:100%}
.hband{position:absolute;bottom:34px;height:40px;background:rgba(203,228,74,.32)}
.htoday{position:absolute;bottom:76px;transform:translateX(-50%);font-size:12px;color:#555;white-space:nowrap}
.hdot{position:absolute;width:15px;height:15px;border-radius:50%;transform:translateX(-50%);border:2px solid #fff;box-shadow:0 0 0 1px rgba(0,0,0,.2)}
.blend{margin-top:10px;background:#0d100d;color:#fff;border-radius:16px;padding:15px 22px;display:flex;align-items:center;gap:12px;
flex-wrap:wrap;font-size:19px}
.blend .bl{color:var(--lime);letter-spacing:4px;font-size:14px;font-weight:800}
.blend b{font-size:24px}.blend .tilt{color:#f39a84;font-weight:700}.blend .dimmed{color:#888;font-weight:500;font-size:15px}
.conf{margin-left:auto;background:var(--lime);color:#111;border-radius:99px;padding:5px 14px;font-weight:800;font-size:13px;
text-transform:uppercase;letter-spacing:.5px}
.conf.c-low{background:#f0b4a6}.conf.c-medium{background:#e9dc8c}
footer{margin-top:14px}
footer .notes{font-size:12.5px;line-height:1.5;color:#c9cec3}
.foot{display:flex;justify-content:space-between;align-items:center;gap:20px;margin-top:14px}
.credit{font-size:12.5px;color:#c9cec3;max-width:560px;line-height:1.45}
.bchip{display:flex;align-items:center;gap:10px;background:#fff;color:#111;border-radius:99px;padding:7px 18px 7px 8px;
box-shadow:4px 4px 0 #000;border:2px solid #111;font-size:15px}
.bchip span{color:#666;font-family:'JetBrains Mono',monospace;font-size:12.5px;border-left:1px solid #ccc;padding-left:10px}
.bchip svg{width:28px;height:28px;background:#0b0d0b;border-radius:8px}
/* tiebreak */
.tbplayers{display:grid;grid-template-columns:1fr 1fr;gap:26px;margin-top:18px;position:relative}
.tbp .meta{color:#dcdfd5;font-size:18px;margin:4px 0 12px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
.tile{border:1.5px solid #3a443b;background:rgba(255,255,255,.04);border-radius:14px;padding:12px 14px}
.tile b{display:block;font-size:32px;font-weight:800;line-height:1.1}
.tile span{font-size:13.5px;color:#d9dcd2;line-height:1.3;display:block;margin-top:4px}
.tourline{text-align:center;color:#e6e8e0;font-size:16px;margin:14px 0}
.tbbig{display:grid;grid-template-columns:auto auto 1fr;gap:14px;align-items:center;margin:4px 0 8px}
.tbbig .tbnum{font-size:64px;font-weight:900;line-height:1}.tbbig span{font-size:19px;font-weight:700;color:#444;line-height:1.15}
.tfile{display:grid;grid-template-columns:1fr 1fr;gap:26px}
.thead{display:flex;justify-content:space-between;font-size:19px;border-bottom:1.5px solid var(--line);padding-bottom:4px}
.thead span{font-size:12px;color:#777;font-weight:700}
.trow{display:grid;grid-template-columns:28px 1fr auto 26px;gap:10px;align-items:center;padding:6px 0;border-bottom:1px solid #e3dfd2}
.wl{width:26px;height:26px;border-radius:7px;display:grid;place-items:center;color:#fff;font-weight:800;font-size:13px}
.wl.w{background:#3e9a4b}.wl.l{background:#cf4b3c}
.topp b{display:block;font-size:16px}.topp small{font-size:11.5px;color:#777}
.sets{display:flex;gap:5px;flex-wrap:wrap;justify-content:flex-end}
.set{font-size:13px;background:#ece5d2;border-radius:7px;padding:4px 7px;font-weight:600;font-family:Outfit,Inter,sans-serif}
.set.tba{background:#86b21f;color:#fff}.set.tbb{background:#4aa3e0;color:#fff}
.tbn{text-align:right;font-size:17px}
.dists{display:grid;grid-template-columns:1fr 1fr;gap:20px}
.dist{background:#efe5cf;border-radius:12px;padding:12px 16px;border-left:6px solid var(--a)}.dist.db{border-left-color:var(--b)}
.board .dist p{font-size:15.5px}
.courtrow{display:grid;grid-template-columns:1fr auto;gap:20px;align-items:center}
.ebars{display:flex;gap:10px;align-items:flex-end}
.ebar{display:flex;flex-direction:column;align-items:center;gap:4px;width:58px}
.ebar i{display:block;width:54px;background:#e2d4b2;border-radius:4px 4px 0 0}.ebar.cur i{background:#6c9a19}
.ebar em{font-style:normal;font-weight:800;font-size:14px}.ebar small{font-size:12px;color:#555}
.ebar.cur small{color:#5b7d12;font-weight:800}
/* aura */
.aurahead{display:flex;justify-content:space-between;align-items:center;margin:26px 0 12px}
.caps{letter-spacing:4px;font-weight:700;font-size:14.5px;color:#dfe3d8}
.vibes{font-family:'JetBrains Mono',monospace;font-size:14px;color:var(--lime);border:1.5px dashed #6d7f2a;border-radius:99px;padding:6px 14px;background:#232b1d}
.auras{display:grid;grid-template-columns:1fr 1fr;gap:26px}
.acard{background:#f6f4ec;color:#141614;border-radius:22px;padding:22px 24px;box-shadow:7px 7px 0 #000;border-left:12px solid var(--a)}
.acard.cringe{border-left-color:var(--red)}.acard.plainb{border-left-color:var(--b)}
.plainb .rn{border-color:#3c95d6;background:#dcecf8;color:#1d5f94}
.acard h2{font-size:40px;font-weight:800;line-height:1.1}.acard .flag{font-size:30px;vertical-align:middle}
.acard .meta{color:#555;font-size:15px;margin:6px 0 10px}
.tagline{font-style:italic;font-weight:800;font-size:19px;margin:12px 0 4px;color:#4c6c0e}
.cringe .tagline{color:var(--red)}
.meter{display:grid;grid-template-columns:1fr auto;align-items:end;margin-top:14px}
.ml{letter-spacing:3px;font-size:12.5px;font-weight:800;color:#555}.mv{font-size:30px;font-weight:900}
.cringe .mv{color:var(--red)}
.mtrack{grid-column:1/3;height:24px;border-radius:99px;border:2.5px solid #111;margin-top:8px;overflow:hidden;background:#fff}
.mtrack span{display:block;height:100%;background:repeating-linear-gradient(90deg,#cfe66b 0 46px,#bcd552 46px 48px)}
.cringe .mtrack span{background:repeating-linear-gradient(135deg,#e2533f 0 10px,#c63f2d 10px 20px)}
.rl{letter-spacing:3px;font-size:12.5px;font-weight:800;color:#555;margin:16px 0 6px}
.receipts{list-style:none}
.receipts li{display:grid;grid-template-columns:30px 1fr;gap:10px;padding:9px 0;border-bottom:1px solid #ddd8c8;font-size:16.5px;line-height:1.38}
.receipts li:last-child{border-bottom:0}
.rn{width:24px;height:24px;border-radius:7px;border:2px solid #6c9a19;display:grid;place-items:center;font-size:13px;font-weight:800;
background:#eef6cf;color:#3d5c08;margin-top:2px}
.cringe .rn{border-color:var(--red);background:#fde3dd;color:#a12d1c}
.tapebox{margin-top:26px;border:1.5px solid #3a443b;border-radius:16px;padding:12px 22px;background:rgba(255,255,255,.03)}
.tape{display:grid;grid-template-columns:1fr 1.2fr 1fr;align-items:center;padding:8px 0;border-top:1px solid #2f3830;font-size:17px}
.tape:first-child{border-top:0}.tape span{text-align:center;color:#dfe3d8}.tape .r{text-align:right}
.tape b{font-size:22px}.th b{font-size:13px;letter-spacing:3px}.th span{letter-spacing:3px;font-size:13px;font-weight:700}
.t-aura,.t-plaina{color:var(--lime)}.t-cringe{color:#f07a64}.t-plainb{color:var(--b)}
.read{margin-top:22px;border:2px solid #8aa22f;border-radius:18px;padding:18px 26px;background:rgba(203,228,74,.06)}
.read h3,.kill h3,.market h3{letter-spacing:3px;font-size:14px;color:var(--lime);margin-bottom:8px}
.read p{font-size:20px;line-height:1.5}.read b{color:var(--lime)}
.aurafoot{display:grid;grid-template-columns:1.4fr 1fr;gap:22px;margin-top:22px}
.kill,.market{border:1.5px solid #3a443b;border-radius:16px;padding:16px 20px;background:rgba(255,255,255,.03)}
.kill h3{color:#f07a64}.kill p{font-size:16px;line-height:1.5;color:#e6e8e0}
.mrow{display:flex;justify-content:space-between;font-size:17px;padding:3px 0}.mrow b{font-family:'JetBrains Mono',monospace}
.market .mt{margin-top:10px}.market .tiny{color:#9aa096}
"""


def render(d: dict, template: str = "matchup", notes: dict | None = None) -> str:
    if template == "tiebreak":
        return render_tiebreak(d)
    if template == "aura":
        return render_aura(d, notes)
    return render_matchup(d)


def png(html_text: str, out_path: str, scale: float = 2.0) -> None:
    """Screenshot a card with Playwright (pip install playwright). TENNISSCOUT_CHROMIUM points at a Chromium
    binary when Playwright's own browser is not installed."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError("PNG export needs Playwright: pip install playwright (then `playwright install chromium`, "
                           "or set TENNISSCOUT_CHROMIUM to an existing Chromium binary)") from e
    exe = os.environ.get("TENNISSCOUT_CHROMIUM")
    with sync_playwright() as p:
        browser = p.chromium.launch(**({"executable_path": exe} if exe else {}))
        page = browser.new_page(viewport={"width": 1200, "height": 1500}, device_scale_factor=scale)
        page.set_content(html_text, wait_until="networkidle")
        page.locator("#card").screenshot(path=out_path)
        browser.close()


def index_page(links: list[tuple[dict, str | None]], templates: list[str], href=None) -> str:
    """Slate page: one row per scheduled match, linking to its cards. ``href(name, template)`` builds the link."""
    href = href or (lambda name, t: f"{name}-{t}.html")
    rows = ""
    for r, name in links:
        p = f"{r['p_a']:.0%}" if "p_a" in r else "–"
        cards = " ".join(f'<a href="{_e(href(name, t))}">{_e(t)}</a>' for t in templates) if name else \
            f'<span class="dim">{_e(r.get("error", ""))}</span>'
        rows += (f"<tr><td>{_e(r['start'][11:16])}</td><td>{_e(r['tournament'])} {_e(r['round'] or '')}</td>"
                 f"<td><b>{_e(r['player_a'])}</b> v <b>{_e(r['player_b'])}</b></td><td class=\"p\">{p}</td><td>{cards}</td></tr>")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Slate · {_e(BRAND)}</title><style>
body{{margin:0;background:#171d18;color:#f4f4ee;font-family:Outfit,Inter,system-ui,sans-serif}}
main{{max-width:1000px;margin:0 auto;padding:28px 16px}}h1{{font-size:34px;margin:0 0 4px}}p{{color:#a9b0a4;margin:0 0 18px}}
table{{width:100%;border-collapse:collapse;font-size:15px}}td{{padding:10px 8px;border-top:1px solid #2f3830;vertical-align:top}}
td.p{{font-weight:700;color:#cbe44a}}a{{color:#cbe44a;margin-right:10px}}.dim{{color:#8b9187}}
@media (max-width:640px){{td:nth-child(2){{display:none}}}}</style></head><body><main>
<h1>Scouting cards</h1><p>Scheduled ATP matches with the served model's chance for the first-named player. Research output, not betting advice.</p>
<table>{rows}</table></main></body></html>"""
