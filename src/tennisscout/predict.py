"""Serve pre-match probabilities from the latest replayed ratings.

Name lookup never silently picks a player: an ambiguous name returns the
candidates and the caller must choose by internal ID (spec, "Entity resolution
is the trap"). Every answer states the data cut-off, because ratings are only
as current as the last match the source has.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date

from . import config, db
from .elo import OVERALL, EloConfig, EloModel, confidence
from .normalize import norm_name


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
    model = model or db.get_meta(conn, "serving_model", "elo_selected")
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
    model = model or db.get_meta(conn, "serving_model", "elo_selected")
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
    model = model or db.get_meta(conn, "serving_model", "elo_selected")
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


def predict(
    conn: sqlite3.Connection,
    player_a: str,
    player_b: str,
    surface: str = "Hard",
    indoor: bool = False,
    best_of: int = 3,
    model: str | None = None,
) -> MatchPrediction:
    model = model or db.get_meta(conn, "serving_model", "elo_selected")
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
    caveats.append("Surface Elo only: no serve/return, fatigue, conditions, news or market inputs yet.")

    return MatchPrediction(
        player_a=a,
        player_b=b,
        surface=surface,
        indoor=indoor,
        best_of=best_of,
        p_a=pred.p_a,
        p_b=1 - pred.p_a,
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
        model=model,
        model_config=asdict(cfg),
        prediction_time=config.utcnow(),
        data_as_of=as_of,
        caveats=caveats,
    )
