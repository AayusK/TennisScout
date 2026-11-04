"""Matchup card: one self-contained HTML page per prediction, styled as a shareable report graphic.

Every number on the card comes from the prediction dict or the match table. Nothing is
estimated here; a section with no data says so instead of filling the space.
"""

from __future__ import annotations

import html
import sqlite3

ACCENT_A, ACCENT_B = "#b6d334", "#4fa3d9"


def _e(x) -> str:
    return html.escape(str(x))


def _pct(x, nd=0) -> str:
    return f"{100 * x:.{nd}f}%"


def _last(name: str) -> str:
    return name.split()[-1] if name else "?"


def history(conn: sqlite3.Connection, a: str, b: str, n: int = 5) -> dict:
    """Head-to-head and each player's last n completed/retired matches, newest first."""
    q = """SELECT m.winner_id, m.loser_id, m.winner_src_name, m.loser_src_name, m.score, m.round, m.outcome,
                  e.name, e.start_date, COALESCE(m.surface, e.surface)
           FROM match m JOIN tournament_edition e USING (edition_id)
           WHERE m.outcome IN ('completed', 'retired') AND {}
           ORDER BY m.seq DESC LIMIT ?"""

    def rows(where, args, lim):
        return [dict(zip(("w", "l", "wn", "ln", "score", "round", "outcome", "event", "date", "surface"), r))
                for r in conn.execute(q.format(where), (*args, lim))]

    def form(pid):
        out = []
        for r in rows("(m.winner_id = ? OR m.loser_id = ?)", (pid, pid), n):
            won = r["w"] == pid
            out.append({"won": won, "opp": r["ln"] if won else r["wn"], "score": r["score"] or "",
                        "event": r["event"], "round": r["round"], "date": r["date"], "ret": r["outcome"] == "retired"})
        return out

    h2h = rows("((m.winner_id = ? AND m.loser_id = ?) OR (m.winner_id = ? AND m.loser_id = ?))", (a, b, b, a), 50)
    return {
        "form": [form(a), form(b)],
        "h2h": [sum(r["w"] == a for r in h2h), sum(r["w"] == b for r in h2h)],
        "h2h_last": h2h[0] if h2h else None,
    }


def _bar(label: str, va: float, vb: float, fmt=_pct) -> str:
    tot = (va + vb) or 1
    return f"""<div class="duel"><div class="duel-l">{fmt(va)}</div><div class="duel-m">
<div class="duel-lab">{_e(label)}</div><div class="track"><span style="width:{100 * va / tot:.1f}%;background:{ACCENT_A}"></span>
<span style="width:{100 * vb / tot:.1f}%;background:{ACCENT_B}"></span></div></div><div class="duel-r">{fmt(vb)}</div></div>"""


def _sets_html(score: str) -> str:
    return "".join(f'<span class="set{" tb" if "(" in s or s.startswith(("7-6", "6-7")) else ""}">{_e(s)}</span>'
                   for s in score.split() if s[:1].isdigit())


def _form_html(name: str, rows: list[dict], color: str) -> str:
    if not rows:
        return f'<div class="form"><h4 style="color:{color}">{_e(name)}</h4><p class="muted">No completed matches in the data.</p></div>'
    items = "".join(
        f"""<div class="frow"><span class="wl {'w' if r['won'] else 'l'}">{'W' if r['won'] else 'L'}</span>
<div class="opp"><b>{_e(_last(r['opp'] or '?'))}</b><small>{_e(r['event'])} {_e(r['round'] or '')} · {_e(r['date'] or '')}</small></div>
<div class="sets">{_sets_html(r['score'])}{' <span class="muted">ret.</span>' if r['ret'] else ''}</div></div>"""
        for r in rows)
    tb = sum(1 for r in rows for s in r["score"].split() if s.startswith(("7-6", "6-7")))
    return f'<div class="form"><h4 style="color:{color}">{_e(name)} <small>{tb} tiebreak set{"s" if tb != 1 else ""}</small></h4>{items}</div>'


def _headline(p: dict, names: tuple[str, str]) -> str:
    fav = 0 if p["p_a"] >= 0.5 else 1
    pf = max(p["p_a"], p["p_b"])
    c = p["components"]
    s = f"{names[fav]} is the {'clear ' if pf >= 0.7 else 'slight ' if pf < 0.58 else ''}favorite at {_pct(pf)}."
    if "hold" in c:
        h = c["hold"]
        big = 0 if h[0] >= h[1] else 1
        s += f" {names[big]} holds {_pct(h[big])} on the model's serve numbers, {names[1 - big]} {_pct(h[1 - big])}."
    return s


def render(p: dict, hist: dict | None = None, event: str | None = None) -> str:
    a, b = p["player_a"], p["player_b"]
    na, nb = _last(a["name"]), _last(b["name"])
    c, r = p["components"], p["ratings"]
    fav = 0 if p["p_a"] >= 0.5 else 1
    win_name, win_p = (na, nb)[fav], max(p["p_a"], p["p_b"])
    setting = f"{p['surface']}{' · indoors' if p['indoor'] else ''} · best of {p['best_of']}"
    hist = hist or {"form": [[], []], "h2h": [0, 0], "h2h_last": None}
    h2h = hist["h2h"]
    h2h_txt = ("First meeting" if not sum(h2h) else
               f"{na if h2h[0] > h2h[1] else nb} leads {max(h2h)}–{min(h2h)}" if h2h[0] != h2h[1] else f"Level {h2h[0]}–{h2h[1]}")

    def pcard(ref, idx, color):
        hand = {"R": "righty", "L": "lefty"}.get(ref.get("hand") or "", "")
        meta = " · ".join(x for x in (ref.get("ioc"), hand, f"{ref['matches']} tour matches") if x)
        return f"""<div class="pcard" style="--c:{color}"><h2>{_e(_last(ref['name']))}</h2><div class="meta">{_e(ref['name'])} · {_e(meta)}</div>
<div class="tiles"><div class="tile"><b>{r['blended'][idx]:.0f}</b><span>{_e(p['surface'])} Elo (blended)</span></div>
<div class="tile"><b>{r['overall'][idx]:.0f}</b><span>overall Elo, {r['matches_overall'][idx]} matches</span></div>
{f'<div class="tile"><b>{_pct(c["hold"][idx], 1)}</b><span>modelled hold rate</span></div>' if "hold" in c else ''}</div></div>"""

    duels = [_bar(f"{p['surface']} Elo", r["surface"][0], r["surface"][1], lambda v: f"{v:.0f}")]
    if "serve_point_win" in c:
        duels.append(_bar("Serve points won", *c["serve_point_win"], lambda v: _pct(v, 1)))
        duels.append(_bar("Hold rate", *c["hold"], lambda v: _pct(v, 1)))
        duels.append(_bar("Return points won", 1 - c["serve_point_win"][1], 1 - c["serve_point_win"][0], lambda v: _pct(v, 1)))

    layers = [("Plain Elo", p["baseline_p_a"]), ("Calibrated Elo", c.get("elo_selected"))]
    layers += [(n, c[k]) for n, k in (("Serve/return Markov", "serve_return"), ("Elo + S/R blend", "elo_sr_blend"),
                                       ("Context stack", "stacked"), ("GBM", "gbm")) if k in c]
    layer_html = "".join(
        f'<div class="layer{" served" if abs(v - p["p_a"]) < 1e-12 else ""}"><span>{_e(n)}</span><b>{_pct(v, 1)}</b></div>'
        for n, v in layers if v is not None)

    sets_html = ""
    if c.get("set_scores"):
        ss = sorted(c["set_scores"].items(), key=lambda kv: (-int(kv[0].split("-")[0]), kv[0]))
        top = max(v for _, v in ss) or 1
        sets_html = "".join(
            f'<div class="col"><em>{_pct(v)}</em><i style="height:{90 * v / top:.0f}px;background:{ACCENT_A if int(k.split("-")[0]) > int(k.split("-")[1]) else ACCENT_B}"></i><small>{_e(na if int(k.split("-")[0]) > int(k.split("-")[1]) else nb)} {_e(k)}</small></div>'
            for k, v in ss)
        tb = c.get("expected_tiebreaks")
        sets_html = f"""<section><div class="sh"><h3>THE SCORELINE</h3><span>set-score chances, anchored to the served number</span></div>
<div class="cols">{sets_html}</div>{f'<p class="note">Expected tiebreaks: <b>{tb:.2f}</b> per match.</p>' if tb is not None else ''}</section>"""

    last = hist["h2h_last"]
    last_txt = f"Last: <b>{_e(last['event'])} {_e(last['date'] or '')}</b> — {_e(_last(last['wn']))} {_e(last['score'] or '')}" if last else "No meetings in the data."

    caveats = "".join(f"<li>{_e(x)}</li>" for x in p["caveats"])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(na)} v {_e(nb)} · TennisScout</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;600;800&family=JetBrains+Mono&display=swap" rel="stylesheet">
<style>
:root{{--bg:#1b211c;--ink:#f3f2ea;--paper:#f6f4ec;--pink:#1e2420;--muted:#9aa39a;--a:{ACCENT_A};--b:{ACCENT_B};--line:#2f3830}}
*{{box-sizing:border-box}}body{{margin:0;background:radial-gradient(circle at 80% 0,#2a3328,var(--bg) 60%);color:var(--ink);font-family:Outfit,system-ui,sans-serif}}
.wrap{{max-width:1080px;margin:0 auto;padding:32px 16px 48px}}
header{{display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap;align-items:center}}
.brand{{display:flex;gap:16px;align-items:center}}.logo{{width:72px;height:72px;border-radius:18px;background:#111;display:grid;place-items:center;box-shadow:4px 4px 0 #000}}
.logo i{{width:46px;height:46px;border-radius:50%;background:var(--a);display:block}}
.brand small{{color:var(--a);font-weight:600}}.brand h1{{margin:0;font-size:clamp(30px,6vw,52px);font-weight:800;letter-spacing:-.5px}}.brand h1 em{{color:var(--a);font-style:normal}}
.event{{text-align:right;font-size:18px;line-height:1.4}}.event b{{font-size:24px}}.event span{{color:#cfd3c8}}
.players{{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin:28px 0}}
.pcard{{background:var(--paper);color:#151915;border-radius:18px;padding:22px;border-left:10px solid var(--c);box-shadow:6px 6px 0 #000}}
.pcard h2{{margin:0;font-size:36px;font-weight:800}}.meta{{color:#666;font-size:14px;margin:4px 0 14px}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:10px}}.tile{{border:1px solid #ddd;border-radius:12px;padding:10px}}
.tile b{{display:block;font-size:26px;color:var(--c);filter:brightness(.8)}}.tile span{{font-size:12px;color:#555}}
.board{{background:var(--paper);color:#161a16;border-radius:20px;padding:26px;box-shadow:6px 6px 0 #000}}
section{{border-top:1px solid #d9d6c9;padding:18px 0}}section:first-child{{border:0;padding-top:0}}
.sh{{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:baseline}}.sh h3{{margin:0 0 10px;font-size:13px;letter-spacing:2px;color:#5a7a10}}.sh span{{font-size:13px;color:#666}}
.big{{font-size:clamp(20px,3vw,28px);font-weight:800;line-height:1.25;margin:4px 0 8px}}.note{{font-size:14px;color:#444;margin:8px 0 0}}
.duel{{display:grid;grid-template-columns:70px 1fr 70px;gap:12px;align-items:center;margin:10px 0}}.duel-l{{font-weight:800;color:#5a7a10}}.duel-r{{font-weight:800;color:#2a74a8;text-align:right}}
.duel-lab{{font-size:13px;text-align:center;margin-bottom:4px}}.track{{display:flex;height:12px;border-radius:99px;overflow:hidden;background:#e4e1d4}}
.layers{{display:flex;flex-wrap:wrap;gap:8px}}.layer{{border:1px solid #ccc;border-radius:99px;padding:6px 12px;font-size:14px}}.layer b{{margin-left:6px}}.layer.served{{background:#151915;color:var(--a);border-color:#151915}}
.forms{{display:grid;grid-template-columns:1fr 1fr;gap:24px}}.form h4{{margin:0 0 8px;font-size:18px}}.form h4 small{{color:#777;font-weight:400;font-size:12px}}
.frow{{display:grid;grid-template-columns:28px 1fr auto;gap:10px;align-items:center;padding:6px 0;border-bottom:1px solid #e6e3d7}}
.wl{{width:24px;height:24px;border-radius:6px;display:grid;place-items:center;color:#fff;font-weight:800;font-size:13px}}.wl.w{{background:#3f9a4b}}.wl.l{{background:#c94a3c}}
.opp b{{display:block}}.opp small{{color:#777;font-size:11px}}.sets{{display:flex;gap:4px;flex-wrap:wrap;justify-content:flex-end}}
.set{{font-family:'JetBrains Mono',monospace;font-size:12px;background:#ebe7da;border-radius:6px;padding:3px 6px}}.set.tb{{background:var(--a)}}
.cols{{display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap}}.col{{display:flex;flex-direction:column;align-items:center;gap:4px;min-width:64px}}.col i{{width:52px;border-radius:6px 6px 0 0}}.col em{{font-style:normal;font-weight:800;font-size:13px}}.col small{{font-size:11px;color:#555}}
.split{{display:grid;grid-template-columns:1fr 1fr;gap:24px}}.h2h{{font-size:24px;font-weight:800;color:#5a7a10;margin:0 0 6px}}
ul.cav{{margin:0;padding-left:18px;font-size:13px;color:#555}}
.blend{{margin-top:18px;background:#0f120f;color:#fff;border-radius:14px;padding:14px 20px;display:flex;gap:12px;flex-wrap:wrap;align-items:center;font-size:18px}}
.blend small{{letter-spacing:3px;color:var(--a);font-size:12px}}.blend b{{font-size:22px}}.blend .conf{{margin-left:auto;background:var(--a);color:#111;border-radius:99px;padding:4px 14px;font-weight:800;font-size:13px;text-transform:uppercase}}
footer{{margin-top:22px;font-size:12px;color:var(--muted)}}
@media (max-width:720px){{.players,.forms,.split{{grid-template-columns:1fr}}.event{{text-align:left}}}}
</style></head><body><div class="wrap">
<header><div class="brand"><div class="logo"><i></i></div><div><small>TennisScout · ATP</small><h1>MATCHUP <em>Report</em></h1></div></div>
<div class="event"><b>{_e(event or f'{na} v {nb}')}</b><br><span>{_e(setting)}</span><br><span>Head to head: {_e(h2h_txt)}</span></div></header>
<div class="players">{pcard(a, 0, ACCENT_A)}{pcard(b, 1, ACCENT_B)}</div>
<div class="board">
<section><div class="sh"><h3>THE SHOWDOWN</h3><span>{_e(setting)}</span></div><p class="big">{_e(_headline(p, (na, nb)))}</p>
<div class="layers">{layer_html}</div><p class="note">Each chip is P({_e(na)} wins) under one layer; the dark chip is the served model.</p></section>
<section><div class="sh"><h3>THE KEY NUMBERS</h3><span><b style="color:#5a7a10">{_e(na)}</b> v <b style="color:#2a74a8">{_e(nb)}</b></span></div>{''.join(duels)}</section>
{sets_html}
<section><div class="sh"><h3>RECENT FORM</h3><span>last five completed matches in the data · tiebreak sets highlighted</span></div>
<div class="forms">{_form_html(na, hist['form'][0], '#5a7a10')}{_form_html(nb, hist['form'][1], '#2a74a8')}</div></section>
<section class="split"><div><div class="sh"><h3>HEAD TO HEAD</h3></div><p class="h2h">{_e(h2h_txt)}</p><p class="note">{last_txt}</p></div>
<div><div class="sh"><h3>CAVEATS</h3><span>confidence: {_e(p['confidence'])}</span></div><ul class="cav">{caveats}</ul></div></section>
<div class="blend"><small>THE BLEND</small><span>{_e(p['model'])} × {_e(p['surface'])}{' indoor' if p['indoor'] else ''}</span><span>=</span><b>{_e(win_name)} {_pct(win_p)}</b><span class="conf">{_e(p['confidence'])} confidence</span></div>
</div>
<footer>Data as of {_e(p['data_as_of'])} · predicted {_e(p['prediction_time'])} · Model output for research, not a betting pick.</footer>
</div></body></html>"""


def card(conn: sqlite3.Connection, prediction: dict, event: str | None = None) -> str:
    return render(prediction, history(conn, prediction["player_a"]["player_id"], prediction["player_b"]["player_id"]), event)
