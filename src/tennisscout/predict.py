"""Serve pre-match probabilities from the latest replayed ratings.

Name lookup never silently picks a player: an ambiguous name returns the
candidates and the caller must choose by internal ID (spec, "Entity resolution
is the trap"). Every answer states the data cut-off, because ratings are only
as current as the last match the source has.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date

from . import config, db, markov, ml_layer
from .backtest import safe_logit
from .features import NAMES, FeatureState, edition_country, player_info
from .elo import OVERALL, EloConfig, EloModel, confidence
from .normalize import norm_name
from .serve_return import ServeReturnModel, SRConfig


class AmbiguousPlayer(Exception):
    def __init__(self, query: str, candidates: list[dict]):
        super().__init__(f"{query!r} matches {len(candidates)} players")
        self.query = query
        self.candidates = candidates


class UnknownPlayer(Exception):
    pass


@dataclass
class PlayerRef:
    player_id: str
    name: str
    ioc: str | None
    hand: str | None
    birthdate: str | None
    last_match: str | None
    matches: int


@dataclass
class MatchPrediction:
    player_a: PlayerRef
    player_b: PlayerRef
    surface: str
    indoor: bool
    best_of: int
    p_a: float
    p_b: float
    baseline_p_a: float  # same ratings, no calibration scale and no best-of-5 adjustment
    confidence: str
    ratings: dict
    model: str
    model_config: dict
    prediction_time: str
    data_as_of: str | None
    caveats: list[str] = field(default_factory=list)
    components: dict = field(default_factory=dict)  # every model's probability and the serve/return inputs

    def to_dict(self) -> dict:
        return asdict(self)


def _player_ref(conn: sqlite3.Connection, model: str, pid: str) -> PlayerRef:
    p = conn.execute("SELECT * FROM player WHERE player_id = ?", (pid,)).fetchone()
    if not p:
        raise UnknownPlayer(pid)
    r = conn.execute(
        "SELECT n_matches, last_date FROM elo_rating WHERE model = ? AND player_id = ? AND bucket = ?", (model, pid, OVERALL)
    ).fetchone()
    return PlayerRef(pid, p["display_name"], p["ioc"], p["hand"], p["birthdate"], r["last_date"] if r else None, r["n_matches"] if r else 0)


def search(conn: sqlite3.Connection, query: str, model: str | None = None, limit: int = 10) -> list[PlayerRef]:
    model = model or db.get_meta(conn, "elo_model", "elo_selected")
    q = norm_name(query)
    if not q:
        return []
    ids = [r[0] for r in conn.execute("SELECT DISTINCT player_id FROM player_name WHERE name_norm = ?", (q,))]
    if not ids:
        like = "%" + "%".join(q.split()) + "%"
        ids = [r[0] for r in conn.execute("SELECT DISTINCT player_id FROM player_name WHERE name_norm LIKE ? LIMIT 200", (like,))]
    refs = [_player_ref(conn, model, pid) for pid in ids]
    refs.sort(key=lambda r: (r.matches == 0, -(int(r.last_match[:4]) if r.last_match else 0), -r.matches))
    return refs[:limit]


def resolve(conn: sqlite3.Connection, query: str, model: str | None = None) -> PlayerRef:
    model = model or db.get_meta(conn, "elo_model", "elo_selected")
    if query.startswith("P") and query[1:].isdigit():
        return _player_ref(conn, model, query)
    hits = [h for h in search(conn, query, model, limit=50) if h.matches > 0]
    exact = [h for h in hits if norm_name(h.name) == norm_name(query)]
    pool = exact or hits
    if not pool:
        raise UnknownPlayer(query)
    if len(pool) > 1:
        raise AmbiguousPlayer(query, [asdict(h) for h in pool])
    return pool[0]


def load_model(conn: sqlite3.Connection, model: str | None = None) -> EloModel:
    model = model or db.get_meta(conn, "elo_model", "elo_selected")
    cfg_json = db.get_meta(conn, f"model_config:{model}")
    if not cfg_json:
        raise RuntimeError(f"No ratings for model {model!r}; run `tennisscout backtest` first.")
    m = EloModel(EloConfig(**json.loads(cfg_json)))
    for r in conn.execute("SELECT player_id, bucket, rating, n_matches, last_date FROM elo_rating WHERE model = ?", (model,)):
        m.rating[(r["player_id"], r["bucket"])] = r["rating"]
        m.n[(r["player_id"], r["bucket"])] = r["n_matches"]
        if r["last_date"]:
            m.last_date[r["player_id"]] = r["last_date"]
    return m


def load_sr(conn: sqlite3.Connection) -> ServeReturnModel | None:
    name = db.get_meta(conn, "sr_model")
    if not name:
        return None
    m = ServeReturnModel(SRConfig(**json.loads(db.get_meta(conn, f"model_config:{name}"))))
    m.mu = json.loads(db.get_meta(conn, f"sr_mu:{name}"))
    for r in conn.execute("SELECT player_id, kind, mean, var, n_matches FROM sr_rating WHERE model = ?", (name,)):
        m.mean[(r["player_id"], r["kind"])] = r["mean"]
        m.var[(r["player_id"], r["kind"])] = r["var"]
        m.n[r["player_id"]] = r["n_matches"]
    return m


def live_features(conn: sqlite3.Connection, a: str, b: str, today: date, surface: str | None = None) -> dict[str, float]:
    """The stacking features as of ``today``, computed the same way as in the replay (features.py)."""
    st = FeatureState(player_info(conn), edition_country(conn))
    rows = conn.execute(
        """SELECT m.match_id, m.event_date_lo, m.winner_id, m.loser_id, COALESCE(m.surface, e.surface)
           FROM match m JOIN tournament_edition e USING (edition_id)
           WHERE (m.winner_id IN (?, ?) OR m.loser_id IN (?, ?)) AND m.identity_status = 'ok'
             AND m.outcome NOT IN ('walkover', 'default', 'unknown') ORDER BY m.seq""", (a, b, a, b))
    for mid, d, w, l, surf in rows:
        st.update(_Row(mid, d, w, l, surf))
    return dict(zip(NAMES, st.features(_Row("", today.isoformat(), a, b, surface), a, b)))


class _Row:
    def __init__(self, match_id, d, w, l, surface=None):
        self.match_id, self.date, self.winner_id, self.loser_id, self.surface = match_id, d, w, l, surface


def predict(
    conn: sqlite3.Connection,
    player_a: str,
    player_b: str,
    surface: str = "Hard",
    indoor: bool = False,
    best_of: int = 3,
    model: str | None = None,
) -> MatchPrediction:
    model = model or db.get_meta(conn, "elo_model", "elo_selected")
    a, b = resolve(conn, player_a, model), resolve(conn, player_b, model)
    if a.player_id == b.player_id:
        raise ValueError("both names resolve to the same player")
    elo = load_model(conn, model)
    cfg = elo.cfg
    # Players never seen get a debut rating inside predict(); that is fine for a
    # one-off answer because nothing here is written back.
    pred = elo.predict(a.player_id, b.player_id, surface, "I" if indoor else "O", best_of)
    base_cfg = cfg.with_(pred_scale=1.0, bo5_adjust=False)
    base_model = EloModel(base_cfg)
    base_model.rating, base_model.n = elo.rating, elo.n
    base = base_model.predict(a.player_id, b.player_id, surface, "I" if indoor else "O", best_of)

    components = {"elo_selected": pred.p_a}
    sr = load_sr(conn)
    if sr:
        # Best-of-5 today means a Slam, which plays a 10-point tiebreak at 6-6 in the decider.
        p_sr, pa, pb = sr.predict(a.player_id, b.player_id, surface, "I" if indoor else "O", best_of,
                                  "G" if best_of == 5 else None, None, date.today().year)
        coef = json.loads(db.get_meta(conn, "blend_coef"))
        z = coef[0] * safe_logit(pred.p_a) + coef[1] * safe_logit(p_sr)
        components.update({
            "serve_return": p_sr,
            "elo_sr_blend": 1 / (1 + math.exp(-z)),
            "serve_point_win": [pa, pb],
            "hold": [markov.hold(pa), markov.hold(pb)],
            "serve_return_matches": [sr.n.get(a.player_id, 0), sr.n.get(b.player_id, 0)],
        })
        stack = json.loads(db.get_meta(conn, "stack") or "null")
        if stack and stack["features"]:
            feats = live_features(conn, a.player_id, b.player_id, date.today(), surface)
            z = stack["coef"][0] * safe_logit(components["elo_sr_blend"])
            z += sum(c * feats[f] for c, f in zip(stack["coef"][1:], stack["features"]))
            components["stacked"] = 1 / (1 + math.exp(-z))
            components["context_features"] = {f: feats[f] for f in stack["features"]}
            gbm_text = db.get_meta(conn, "gbm_model")
            if gbm_text and ml_layer.available():
                import lightgbm as lgb
                import numpy as np

                x = ml_layer.row(components["stacked"], components["elo_selected"], components["serve_return"],
                                 components["elo_sr_blend"], [feats[f] for f in NAMES], best_of,
                                 "G" if best_of == 5 else None, surface)
                components["gbm"] = float(ml_layer.predict(lgb.Booster(model_str=gbm_text), np.array([x]))[0])
        # Set scores and tiebreaks use their own (validation-chosen) form-on-the-day width.
        sr.form_sd_override = json.loads(db.get_meta(conn, "scoreline_form_sd") or "null")
        rule = markov.final_set_rule("G" if best_of == 5 else None, None, date.today().year, best_of)
        dist: dict[str, float] = {}
        tiebreaks = 0.0
        for d, w in sr.form_nodes():
            qa, qb = sr.point_probs(a.player_id, b.player_id, surface, "I" if indoor else "O", d)
            for k, v in markov.set_scores(qa, qb, best_of, rule).items():
                dist[k] = dist.get(k, 0.0) + w * v
            sets = sum(v * sum(map(int, k.split("-"))) for k, v in markov.set_scores(qa, qb, best_of, rule).items())
            tiebreaks += w * sets * markov.reach_tiebreak(qa, qb)
        components["set_scores"] = dist
        components["expected_tiebreaks"] = tiebreaks
    serving = db.get_meta(conn, "serving_model", "elo_selected")
    serving = serving if serving in components else "elo_selected"

    as_of = db.get_meta(conn, "latest_event_date")
    caveats = []
    if as_of:
        lag = (date.today() - date.fromisoformat(as_of)).days
        if lag > 14:
            caveats.append(f"Ratings include results only up to {as_of} ({lag} days ago); later form is not reflected.")
    conf = confidence(pred)
    if conf == "low":
        caveats.append("Low confidence: at least one player has few tour-level matches (Challenger/ITF results are not in the data).")
    for ref in (a, b):
        if ref.last_match and as_of and (date.fromisoformat(as_of) - date.fromisoformat(ref.last_match)).days > 180:
            caveats.append(f"{ref.name} has no tour-level match in the data since {ref.last_match}.")
    if sr and min(components["serve_return_matches"]) < 15:
        caveats.append("Serve/return estimates for at least one player rest mostly on the pool prior (few stat lines).")
    caveats.append(("Surface Elo + serve/return Markov" if sr else "Surface Elo only")
                   + ": no fatigue, conditions, news or market inputs yet.")

    return MatchPrediction(
        player_a=a,
        player_b=b,
        surface=surface,
        indoor=indoor,
        best_of=best_of,
        p_a=components[serving],
        p_b=1 - components[serving],
        baseline_p_a=base.p_a,
        confidence=conf,
        ratings={
            "bucket": pred.bucket,
            "overall": [pred.overall_a, pred.overall_b],
            "surface": [pred.surface_a, pred.surface_b],
            "blended": [pred.rating_a, pred.rating_b],
            "matches_overall": [pred.n_overall_a, pred.n_overall_b],
            "matches_surface": [pred.n_surface_a, pred.n_surface_b],
        },
        model=serving,
        model_config=asdict(cfg),
        prediction_time=config.utcnow(),
        data_as_of=as_of,
        caveats=caveats,
        components=components,
    )
