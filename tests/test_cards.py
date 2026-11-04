"""Court speed, charting stat lines, style profiles, scoreline helpers and the card renderer.

All on synthetic data: no real mirrors, database or network.
"""

from __future__ import annotations

import csv
import json
import random
from datetime import date
from pathlib import Path

import pytest

from tennisscout import card, charting_stats, court_speed, db, markov, matchup, profiles
from tennisscout.ingest_api_tennis import _serve_extra


# ------------------------------------------------------------------------------------------- markov

@pytest.mark.parametrize("pa,pb", [(0.64, 0.64), (0.71, 0.58), (0.55, 0.62)])
def test_set_games_agrees_with_set_win(pa, pb):
    for first in (True, False):
        dist = markov.set_games(pa, pb, first)
        assert sum(dist.values()) == pytest.approx(1.0)
        won = sum(v for (a, b), v in dist.items() if a > b)
        assert won == pytest.approx(markov.set_win(pa, pb), abs=1e-9)  # who serves first does not change the winner
        assert all(max(a, b) in (6, 7) for a, b in dist)


def test_games_distribution_shape():
    d = markov.games_distribution(0.66, 0.62, 3)
    assert sum(d.values()) == pytest.approx(1.0)
    assert min(d) == 12 and max(d) == 39  # 6-0 6-0 up to 7-6 6-7 7-6
    even = sum(k * v for k, v in markov.games_distribution(0.65, 0.65, 3).items())
    lopsided = sum(k * v for k, v in markov.games_distribution(0.75, 0.55, 3).items())
    assert even > lopsided  # closer matches run longer


# ------------------------------------------------------------------------------------------- score helpers

def test_score_sets_and_tiebreaks():
    s = matchup.score_sets("7-6(5) 3-6 6-4 RET")
    assert s == [(7, 6, 5), (3, 6, None), (6, 4, None)]
    assert [matchup.is_tiebreak(x) for x in s] == [True, False, False]
    assert matchup.score_sets("6-4 [10-8]") == [(6, 4, None), (10, 8, None)]
    assert matchup.fmt_set((7, 6, 5), flip=True) == "6-7(5)"


def test_serve_extra_reads_api_tennis_counts():
    stats = [
        {"player_key": 1, "stat_period": "match", "stat_name": "Aces", "stat_value": "9", "stat_won": None, "stat_total": None},
        {"player_key": 1, "stat_period": "match", "stat_name": "Double Faults", "stat_value": "2", "stat_won": None, "stat_total": None},
        {"player_key": 1, "stat_period": "match", "stat_name": "Service games won", "stat_value": "90%", "stat_won": 9, "stat_total": 10},
        {"player_key": 1, "stat_period": "match", "stat_name": "Break Points Saved", "stat_value": "50%", "stat_won": 1, "stat_total": 2},
        {"player_key": 1, "stat_period": "set1", "stat_name": "Aces", "stat_value": "4", "stat_won": None, "stat_total": None},
        {"player_key": 2, "stat_period": "match", "stat_name": "Break Points Saved", "stat_value": "0%", "stat_won": 0, "stat_total": 0},
    ]
    assert _serve_extra(stats, 1) == (9, 2, 10, 1, 2)
    assert _serve_extra(stats, 2)[3:] == (0, 0)  # 0/0 still means no break point faced


# ------------------------------------------------------------------------------------------- court speed

def _speed_db(tmp_path: Path) -> "db.sqlite3.Connection":
    """Two seasons, three events. Event FAST gives servers more points and aces than the same men get elsewhere."""
    conn = db.connect(tmp_path / "s.db")
    db.init(conn)
    rng = random.Random(7)
    players = [f"P{i:02d}" for i in range(16)]
    serve = {p: rng.gauss(0, 0.2) for p in players}
    events = {"FAST": 0.35, "MID": 0.0, "SLOW": -0.35}
    conn.executemany("INSERT INTO player (player_id, display_name, name_norm) VALUES (?,?,?)", [(p, p, p.lower()) for p in players])
    seq = 0
    for year in (2023, 2024):
        for ev, eff in events.items():
            ed = f"{ev}-{year}"
            conn.execute("INSERT OR IGNORE INTO tournament VALUES (?,?,?)", (ev, None, ev))
            conn.execute("""INSERT INTO tournament_edition (edition_id, tournament_id, source_key, year, name, level, surface, indoor,
                            start_date, date_precision, snapshot_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                         (ed, ev, ed, year, ev.title(), "A", "Hard", "O", f"{year}-0{1 + list(events).index(ev)}-01", "day", "t"))
            for k in range(40):
                w, l = rng.sample(players, 2)
                row = {}
                for side, s, r in (("w", w, l), ("l", l, w)):
                    n = 80
                    p = 1 / (1 + pow(2.718281828, -(0.55 + eff + serve[s] - serve[r] * 0.5)))
                    won = sum(rng.random() < p for _ in range(n))
                    aces = sum(rng.random() < 0.08 * pow(2.718281828, 2 * eff) for _ in range(n))
                    row.update({f"{side}_svpt": n, f"{side}_1stin": 50, f"{side}_1stwon": min(won, 50),
                                f"{side}_2ndwon": won - min(won, 50), f"{side}_ace": aces})
                seq += 1
                conn.execute(f"""INSERT INTO match (match_id, edition_id, seq, round, winner_id, loser_id, outcome, event_time_precision,
                                 collected_at, availability_basis, identity_status, source, source_key, snapshot_id,
                                 w_svpt, w_1stin, w_1stwon, w_2ndwon, w_ace, l_svpt, l_1stin, l_1stwon, l_2ndwon, l_ace, event_date_lo)
                                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                             (f"m{seq}", ed, seq, "R32", w, l, "completed", "day", "t", "t", "ok", "tml", f"m{seq}", "t",
                              row["w_svpt"], row["w_1stin"], row["w_1stwon"], row["w_2ndwon"], row["w_ace"],
                              row["l_svpt"], row["l_1stin"], row["l_1stwon"], row["l_2ndwon"], row["l_ace"],
                              f"{year}-0{1 + list(events).index(ev)}-02"))
    conn.commit()
    return conn


def test_court_speed_orders_events(tmp_path):
    conn = _speed_db(tmp_path)
    out = court_speed.compute(conn)
    assert out["editions"] == 6
    speed = {r[0]: r[1] for r in conn.execute("SELECT edition_id, speed FROM edition_speed")}
    for y in (2023, 2024):
        assert speed[f"FAST-{y}"] > speed[f"MID-{y}"] > speed[f"SLOW-{y}"]
    hist = court_speed.history(conn, "FAST-2024")
    assert [h["year"] for h in hist] == [2023, 2024]
    assert court_speed.label(20) == "slow" and court_speed.label(50) == "medium" and court_speed.label(80) == "fast"


def test_find_event_uses_this_week_or_history(tmp_path):
    conn = _speed_db(tmp_path)
    court_speed.compute(conn)
    ev = matchup.find_event(conn, "Fast", date(2024, 1, 3))
    assert ev.edition_id == "FAST-2024" and ev.speed_source == "this week" and ev.surface == "Hard" and not ev.indoor
    later = matchup.find_event(conn, "Fast", date(2025, 3, 3))
    assert later.edition_id is None and later.speed_source == "previous editions"
    assert later.speed == pytest.approx(sum(h["speed"] for h in later.history[-3:]) / len(later.history[-3:]))
    unknown = matchup.find_event(conn, "Nowhere Open", date(2025, 3, 3), surface="Clay")
    assert unknown.speed is None and unknown.surface == "Clay"


# ------------------------------------------------------------------------------------------- charting lines

def _write(path: Path, cols: list[str], rows: list[list]):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        w.writerows(rows)


def test_charting_collect_maps_server_and_returner(tmp_path):
    mid = "c1"
    _write(tmp_path / "charting-m-stats-Overview.csv",
           "match_id,player,set,serve_pts,aces,dfs,first_in,first_won,second_in,second_won,bk_pts,bp_saved,return_pts,return_pts_won,winners,winners_fh,winners_bh,unforced,unforced_fh,unforced_bh".split(","),
           [[mid, "Al Pha", "Total", 60, 5, 2, 40, 30, 18, 9, 3, 2, 55, 20, 20, 12, 8, 15, 9, 6],
            [mid, "Al Pha", "1", 30, 3, 1, 20, 15, 9, 4, 1, 1, 27, 10, 10, 6, 4, 7, 4, 3]])
    _write(tmp_path / "charting-m-stats-Rally.csv",
           "match_id,server,returner,row,pts,pl1_won,pl1_winners,pl1_forced,pl1_unforced,pl2_won,pl2_winners,pl2_forced,pl2_unforced".split(","),
           [[mid, "Al Pha", "Be Ta", "1-3", 70, 40, 0, 0, 0, 30, 0, 0, 0],      # both servers together: ignored
            [mid, "Al Pha", "Be Ta", "1-3-1", 35, 25, 0, 0, 0, 10, 0, 0, 0],    # Al serving
            [mid, "Be Ta", "Al Pha", "1-3-2", 35, 22, 0, 0, 0, 13, 0, 0, 0],    # Be serving: server listed first
            [mid, "Al Pha", "Be Ta", "10-1", 5, 2, 0, 0, 0, 3, 0, 0, 0]])
    _write(tmp_path / "charting-m-stats-ShotDirOutcomes.csv",
           "match_id,player,row,shots,pt_ending,winners,induced_forced,unforced,shots_in_pts_won,shots_in_pts_lost".split(","),
           [[mid, "Al Pha", "B-XC", 40, 6, 2, 1, 3, 20, 20]])
    out = charting_stats.collect(tmp_path, {mid: {}})
    al, be = out[(mid, "Al Pha")], out[(mid, "Be Ta")]
    assert al["serve_pts"] == 60 and al["winners"] == 20  # the set-1 row is not added again
    assert (al["r13_sv_pts"], al["r13_sv_won"]) == (35, 25)
    assert (al["r13_rt_pts"], al["r13_rt_won"]) == (35, 13)
    assert (be["r13_sv_pts"], be["r13_sv_won"], be["r13_rt_won"]) == (35, 22, 10)
    assert (al["r10_sv_pts"], al["r10_sv_won"], be["r10_rt_won"]) == (5, 2, 3)
    assert (al["bxc_shots"], al["bxc_win"], al["bxc_forced"], al["bxc_ue"]) == (40, 2, 1, 3)


# ------------------------------------------------------------------------------------------- profiles

def _pool(n_players=12, lines_each=6) -> profiles.Pool:
    rng = random.Random(3)
    lines, rates = {}, {}
    for i in range(n_players):
        pid = f"P{i}"
        power = 0.08 + 0.01 * i  # P11 hits hardest
        ls = []
        for k in range(lines_each):
            gs = 200
            c = {"gs_shots": gs, "gs_win": int(gs * power * 0.6), "gs_forced": int(gs * power * 0.4), "gs_ue": int(gs * (0.05 + 0.004 * i)),
                 "shots": 260, "serve_pts": 70, "return_pts": 70, "net_pts": 5 + (i % 3), "fside": 120, "bside": 100,
                 "r13_sv_pts": 40, "r13_sv_won": 28, "r46_sv_pts": 20, "r46_sv_won": 11, "r79_sv_pts": 6, "r79_sv_won": 3,
                 "r13_rt_pts": 40, "r13_rt_won": 12, "r46_rt_pts": 20, "r46_rt_won": 9, "r79_rt_pts": 6, "r79_rt_won": 3,
                 "fxc_shots": 50, "fxc_win": 3, "fxc_forced": 2, "fxc_ue": 3, "bxc_shots": 50, "bxc_win": 1, "bxc_forced": 1,
                 "bxc_ue": 2 + (i % 4), "sv1_pts": 40, "sv1_aces": 4, "sv1_unret": 6, "sv1_forced": 4, "rt1_pts": 40, "rt1_inplay": 26}
            ls.append({"chart": f"c{i}-{k}", "pid": pid, "opp": f"P{(i + k + 1) % n_players}", "date": "2025-05-01",
                       "surface": "Hard", "match_id": None, "c": c, "speed": 40 + 3 * k})
        lines[pid] = ls
        rs = {m: profiles.Rate() for m in profiles.CHART_MEASURES}
        for ln in ls:
            for m, (num, den, _) in profiles.CHART_MEASURES.items():
                rs[m].add(num(ln["c"]), den(ln["c"]))
        rates[pid] = rs
    charted = set(lines)
    prior_tot = {m: profiles.Rate() for m in profiles.CHART_MEASURES}
    for p in charted:
        for m in profiles.CHART_MEASURES:
            prior_tot[m].add(rates[p][m].num, rates[p][m].den)
    prior = {m: (r.raw() if r.den else 0.0) for m, r in prior_tot.items()}
    sr = {p: {"serve": 0.02 * i, "return": -0.01 * i} for i, p in enumerate(lines)}
    pool = profiles.Pool(date(2026, 1, 1), lines, rates, prior, charted, sr, set(lines), {})
    profiles._fit_gravity(pool)
    return pool


class _NoCount:
    """Stands in for a connection: profile() only asks for a match count."""

    def execute(self, *_):
        class R:
            def fetchone(self):
                return (30,)
        return R()


def test_profile_ranks_and_archetypes():
    pool = _pool()
    top, low = profiles.profile(_NoCount(), pool, "P11"), profiles.profile(_NoCount(), pool, "P0")
    assert top.axes["Power"] > 85 and low.axes["Power"] < 15
    assert top.axes["Serve"] > low.axes["Serve"]
    assert top.axes["Consistency"] < low.axes["Consistency"]  # P11 also misses the most
    assert top.archetype in profiles.ARCHETYPES and top.spike in profiles.AXES
    assert "Error-prone" in top.weaknesses
    assert profiles.pct_of(5, [1, 2, 3, 4, 6, 7, 8, 9]) == 50.0


def test_shrinkage_pulls_thin_samples_to_the_pool():
    r = profiles.Rate(num=10, den=20)  # 50% on 20 shots
    assert r.shrunk(0.10, 400) == pytest.approx((10 + 40) / 420)


def test_wing_diagonals_follow_handedness():
    pool = _pool()
    pa, pb = profiles.profile(_NoCount(), pool, "P3"), profiles.profile(_NoCount(), pool, "P8")
    A = {"player_id": "P3", "short": "Righty", "hand": "R"}
    lefty = {"player_id": "P8", "short": "Lefty", "hand": "L"}
    righty = {"player_id": "P8", "short": "Other", "hand": "R"}
    opp = matchup.wing_qs(pa, pb, A, lefty, date(2023, 1, 1))
    same = matchup.wing_qs(pa, pb, A, righty, date(2023, 1, 1))
    assert {(q["left"]["label"].split()[-1], q["right"]["label"].split()[-1]) for q in opp} == {("backhand", "forehand"), ("forehand", "backhand")}
    assert {(q["left"]["label"].split()[-1], q["right"]["label"].split()[-1]) for q in same} == {("backhand", "backhand"), ("forehand", "forehand")}


# ------------------------------------------------------------------------------------------- court records

def _m(i, won, speed, sv=(10, 2, 3), opp=(10, 1, 4), surface="Hard", indoor="I", d="2025-10-01"):
    return matchup.M(f"m{i}", i, d, "Ev", "e", 2025, "R32", "A", surface, indoor, 3, won, "X", "X", "6-4 6-4", "completed",
                     speed, sv, opp)


def test_court_records_label():
    ev = matchup.Event("Paris", "2026-11-04", surface="Hard", indoor=True, speed=60.0)
    better = [_m(i, True, 62, sv=(10, 1, 1), opp=(10, 0, 5)) for i in range(8)] + \
             [_m(100 + i, False, 30, sv=(10, 0, 4), opp=(10, 4, 5), indoor="O") for i in range(20)]
    out = matchup.court_records(better, ev, date(2023, 11, 4))
    assert (out["w"], out["l"], out["n"]) == (8, 0, 8)
    assert out["label"] == "BETTER HERE" and out["delta"] > 0
    few = matchup.court_records(better[:3] + better[8:], ev, date(2023, 11, 4))
    assert few["label"] == "TOO FEW TO CALL"


# ------------------------------------------------------------------------------------------- rendering

def _payload() -> dict:
    def prof(arch, icon, g):
        axes = {a: 50.0 for a in profiles.AXES}
        axes["Serve"] = 94.0
        return {"player_id": "P1", "axes": axes, "axis_n": {a: 40 for a in profiles.AXES},
                "measures": {"fs_sv": 0.73, "rally": 0.52, "unret1": 0.44, "inplay1": 0.6, "power": 0.12, "grind": 0.5},
                "measure_pct": {}, "archetype": arch, "icon": icon, "mixed": ["Shotmaker"], "weaknesses": ["Weak backhand"],
                "spike": "Serve", "n_charts": 12, "gravity": g, "sr_matches": 40}

    q = {"kind": "FIRST STRIKE", "question": "Can Humbert handle Berrettini's serve?",
         "left": {"side": "b", "what": "return", "label": "Humbert return", "rank": 35.0},
         "right": {"side": "a", "what": "serve", "label": "Berrettini serve", "rank": 85.0},
         "edge": {"label": "EDGE", "side": "a"}, "detail": "Detail line.", "importance": 50, "favours": "a"}
    court = {"w": 6, "l": 4, "n": 10, "here": {"hold": 0.9, "brk": 0.15, "service_games": 100, "return_games": 100},
             "other": {"hold": 0.88, "brk": 0.17, "service_games": 300, "return_games": 300}, "delta": 0.0, "label": "NO CLEAR CHANGE"}
    recent = [{"won": True, "opp": "Shelton", "event": "Paris", "round": "R64", "date": "2026-11-03", "level": "M",
               "sets": ["6-7(5)", "7-6(8)", "7-6(4)"], "tb": [True, True, True], "tiebreaks": 3, "retired": False, "games": 39}]
    tb = {"tb_52": (10, 30), "tb_bucket_52": (4, 8), "bo3": 25, "bo3_three": 9, "bo3_tb": 10, "fast_hold": 0.87, "fast_n": 20,
          "band": 12, "band_three": 5, "band_tb": 4}
    return {
        "event": {"name": "Paris Masters", "date": "2026-11-04", "round": "R32", "surface": "Hard", "indoor": True, "best_of": 3,
                  "level": "M", "edition_id": "e", "event_key": "k", "speed": 68.3, "speed_source": "this week",
                  "history": [{"year": 2024, "speed": 61.7, "current": False}, {"year": 2025, "speed": 56.0, "current": False},
                              {"year": 2026, "speed": 68.3, "current": True}], "speed_label": "medium-fast", "court": "indoor hard"},
        "as_of": "2026-11-04", "window_start": "2023-11-04",
        "players": [{"player_id": "P1", "name": "Matteo Berrettini", "short": "Berrettini", "ioc": "ITA", "hand": "R", "height_cm": 196,
                     "rank": 43, "career_high": 6, "titles": 10, "best_slam": "Final", "rank_last_known": 40, "rank_last_known_date": "2026-01-10"},
                    {"player_id": "P2", "name": "Ugo Humbert", "short": "Humbert", "ioc": "FRA", "hand": "L", "height_cm": 188,
                     "rank": 38, "career_high": 13, "titles": 7, "best_slam": "4th round", "rank_last_known": 38, "rank_last_known_date": "2026-01-10"}],
        "profiles": [prof("Big Server", "🚀", 2.9), prof("Gunslinger", "🔫", 3.0)],
        "prediction": {"data_as_of": "2026-11-04", "components": {"set_scores": {"2-0": 0.3, "2-1": 0.2, "1-2": 0.2, "0-2": 0.3},
                                                                  "expected_games": 23.4, "expected_tiebreaks": 0.7}},
        "model": {"p_a": 0.493, "name": "stacked", "confidence": "high"},
        "blend": {"p_a": 0.49, "tilt_a": -0.003},
        "tilt": {"bucket_speed": 59.7, "short_here": 0.62, "short_medium": 0.6, "short_bucket": 0.61, "d_short": 0.01, "tilt_a": -0.003,
                 "short_win": [0.7, 0.71], "long_win": [0.55, 0.56]},
        "questions": [q, dict(q, kind="RALLY", question="Will Berrettini's backhand hold up crosscourt?",
                              left={"side": "a", "what": "crosscourt backhand", "label": "Berrettini crosscourt backhand", "rank": 4.0},
                              right={"side": "b", "what": "crosscourt forehand", "label": "Humbert crosscourt forehand", "rank": 68.0}, edge={"label": "EDGE", "side": "b"})],
        "against": [[{"label": "v the 10 most like him", "icon": None, "weak": False, "w": 8, "l": 2, "expected": 6.1, "n": 10}],
                    [{"label": "v Big Servers", "icon": "🚀", "weak": False, "w": 7, "l": 8, "expected": 8.6, "n": 15, "near": (3, 1)}]],
        "form": [{"w": 4, "l": 4, "n": 8, "expected": 3.9, "retired_last": None},
                 {"w": 2, "l": 4, "n": 6, "expected": 2.9, "retired_last": {"event": "Basel", "round": "QF", "opp": "Fritz", "date": "2026-10-30"}}],
        "tournament_record": [(6, 4), (9, 3)],
        "court": [court, dict(court, label="BETTER HERE", delta=10.0)],
        "h2h": {"a": 3, "b": 1, "meetings": [{"date": "2025-10-15", "year": 2025, "event": "Stockholm", "round": "QF", "surface": "Hard",
                                             "indoor": "I", "speed": 55.0, "a_won": False, "score": "7-6(5) 6-3",
                                             "a_sets": ["6-7(5)", "3-6"], "best_of": 3}]},
        "tiebreaks": {"a": tb, "b": tb, "tiebreaks_last10": 7, "games_last10": 24.1},
        "tour": {"tb_share": 0.39, "tb_share_bucket": 0.48, "hold_bucket": 0.853},
        "recent": [recent, recent], "last_five": [(3, 2), (1, 4)], "market": None,
        "pool": {"charted_players": 180, "min_charts": 5},
        "headline": "Berrettini's serve ranks among the tour's best, and Humbert's return does not.",
    }


@pytest.mark.parametrize("template", ["matchup", "tiebreak", "aura"])
def test_render_templates(template):
    page = card.render(_payload(), template)
    assert page.startswith("<!doctype html>") and "Berrettini" in page and "Humbert" in page
    assert "THE BLEND" in page or template == "aura"
    assert "<script" in page and "</html>" in page
    if template == "matchup":
        assert "Can Humbert handle Berrettini&#x27;s serve?" in page and "BETTER HERE" in page and "Retired from his last match" in page
    if template == "tiebreak":
        assert "THE TIEBREAK FILE" in page and "after <b>56 and 61.7".replace("61.7", "62") in page
    if template == "aura":
        assert "Vibes-based. Not in the model." in page and "RECEIPTS (FROM THE DATA)" in page


def test_aura_notes_are_escaped_and_bolded():
    notes = {"a": {"meter": 90, "meter_label": "FULL AURA", "tagline": "Calm.", "receipts": ["**Says nothing.** <script>x</script>"]},
             "read": "Aura is **quiet**."}
    page = card.render(_payload(), "aura", notes)
    assert "<b>Says nothing.</b> &lt;script&gt;" in page and "FULL AURA" in page and "<b>quiet</b>" in page


def test_index_page_lists_matches():
    rows = [({"start": "2026-11-04T12:00:00Z", "tournament": "Paris", "round": "R32", "player_a": "A", "player_b": "B", "p_a": 0.6}, "x"),
            ({"start": "2026-11-04T14:00:00Z", "tournament": "Paris", "round": "R32", "player_a": "C", "player_b": "D",
              "error": "player not in the registry yet"}, None)]
    page = card.index_page(rows, ["matchup", "tiebreak"])
    assert 'href="x-matchup.html"' in page and "player not in the registry yet" in page and "60%" in page


def test_payload_is_json_serialisable():
    json.dumps(_payload())
