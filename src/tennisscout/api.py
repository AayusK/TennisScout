"""FastAPI model service (spec, "Stack").

    uvicorn tennisscout.api:app --reload

Endpoints:
    GET /health
    GET /players/search?q=sinner
    GET /predict?a=Jannik%20Sinner&b=Carlos%20Alcaraz&surface=Clay&best_of=5
    GET /backtest   (headline metrics of the serving model)
"""

from __future__ import annotations

import json

from fastapi import FastAPI, HTTPException, Query

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


@app.get("/backtest")
def backtest_summary():
    conn = _conn()
    raw = db.get_meta(conn, "backtest_result")
    if not raw:
        raise HTTPException(404, "no backtest yet")
    r = json.loads(raw)
    return {k: r[k] for k in ("validation_years", "test_years", "selected_config", "test_headline", "test_calibration")}
