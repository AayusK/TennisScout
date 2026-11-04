"""FastAPI model service (spec, "Stack").

    uvicorn tennisscout.api:app --reload

Endpoints:
    GET /health
    GET /players/search?q=sinner
    GET /predict?a=Jannik%20Sinner&b=Carlos%20Alcaraz&surface=Clay&best_of=5
    GET /card?a=...&b=...&event=Paris%20Masters   (HTML matchup card; template=matchup|tiebreak|aura)
    GET /cards              (scheduled matches with links to their cards)
    GET /backtest   (headline metrics of the serving model)
"""

from __future__ import annotations

import json

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

from . import db, predict

app = FastAPI(title="TennisScout", version="0.1.0")


def _conn():
    conn = db.connect()
    db.init(conn)
    return conn


@app.get("/health")
def health():
    conn = _conn()
    return {
        "ok": True,
        "serving_model": db.get_meta(conn, "serving_model"),
        "data_as_of": db.get_meta(conn, "latest_event_date"),
        "matches": conn.execute("SELECT COUNT(*) FROM match").fetchone()[0],
    }


@app.get("/players/search")
def players_search(q: str = Query(..., min_length=2), limit: int = 10):
    conn = _conn()
    return [r.__dict__ for r in predict.search(conn, q, limit=limit)]


@app.get("/predict")
def predict_match(
    a: str = Query(..., description="Player name or internal ID (P000123)"),
    b: str = Query(..., description="Player name or internal ID"),
    surface: str = Query("Hard", pattern="^(Hard|Clay|Grass|Carpet)$"),
    indoor: bool = False,
    best_of: int = Query(3, ge=3, le=5),
):
    conn = _conn()
    try:
        return predict.predict(conn, a, b, surface, indoor, best_of).to_dict()
    except predict.AmbiguousPlayer as e:
        raise HTTPException(409, {"error": "ambiguous player name; pass an internal ID", "query": e.query, "candidates": e.candidates})
    except predict.UnknownPlayer as e:
        raise HTTPException(404, {"error": "unknown player", "query": str(e)})
    except ValueError as e:
        raise HTTPException(400, {"error": str(e)})


@app.get("/card", response_class=HTMLResponse)
def match_card(
    a: str = Query(..., description="Player name or internal ID"),
    b: str = Query(..., description="Player name or internal ID"),
    event: str = Query("", description="Tournament name; sets surface, roof and court speed"),
    on: str | None = Query(None, alias="date", description="Match date YYYY-MM-DD (default today)"),
    round: str | None = None,
    surface: str | None = Query(None, pattern="^(Hard|Clay|Grass|Carpet)$"),
    indoor: bool | None = None,
    best_of: int | None = Query(None, ge=3, le=5),
    template: str = Query("matchup", pattern="^(matchup|tiebreak|aura)$"),
    market: bool = False,
):
    """The matchup card as a self-contained HTML page (see `tennisscout card`)."""
    from datetime import date

    from . import card, matchup

    conn = _conn()
    try:
        day = date.fromisoformat(on) if on else date.today()
    except ValueError:
        raise HTTPException(400, {"error": "date must be YYYY-MM-DD"})
    ev = matchup.find_event(conn, event, day, surface, indoor, best_of, round)
    try:
        d = matchup.build(conn, a, b, ev, quotes=matchup.kalshi(conn) if market else None)
    except predict.AmbiguousPlayer as e:
        raise HTTPException(409, {"error": "ambiguous player name; pass an internal ID", "query": e.query, "candidates": e.candidates})
    except predict.UnknownPlayer as e:
        raise HTTPException(404, {"error": "unknown player", "query": str(e)})
    except ValueError as e:
        raise HTTPException(400, {"error": str(e)})
    return card.render(d, template)


@app.get("/cards", response_class=HTMLResponse)
def slate(days: int = Query(1, ge=0, le=7)):
    """Index of scheduled ATP matches with links to their cards."""
    from urllib.parse import urlencode

    from . import card, upcoming

    try:
        rows = upcoming.upcoming(_conn(), days)
    except RuntimeError as e:
        raise HTTPException(503, {"error": str(e)})
    by_name = {}
    links = []
    for r in rows:
        name = None
        if "error" not in r:
            name = str(len(by_name))
            by_name[name] = r
        links.append((r, name))

    def href(name, t):
        r = by_name[name]
        return "/card?" + urlencode({"a": r["player_a_id"], "b": r["player_b_id"], "event": r["tournament"], "date": r["start"][:10],
                                     "round": r["round"] or "", "surface": r["surface"], "indoor": str(r["indoor"]).lower(),
                                     "best_of": r["best_of"], "template": t})

    return card.index_page(links, ["matchup", "tiebreak", "aura"], href)


@app.get("/upcoming")
def upcoming_matches(days: int = Query(2, ge=0, le=7)):
    from . import upcoming

    try:
        return upcoming.upcoming(_conn(), days)
    except RuntimeError as e:  # no API key, or API-Tennis refused the call
        raise HTTPException(503, {"error": str(e)})


@app.get("/backtest")
def backtest_summary():
    conn = _conn()
    raw = db.get_meta(conn, "backtest_result")
    if not raw:
        raise HTTPException(404, "no backtest yet")
    r = json.loads(raw)
    return {k: r[k] for k in ("validation_years", "test_years", "selected_config", "test_headline", "test_calibration")}
