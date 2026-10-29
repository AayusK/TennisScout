import json
from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient

from tennisscout import backtest, config
from tennisscout.elo import EloConfig


@pytest.fixture
def client(loaded, tmp_path, monkeypatch):
    conn, *_ = loaded
    cfg = EloConfig(name="elo_selected")
    preds, model = backtest.replay(backtest.load_matches(conn), cfg, 2000)
    backtest.save(conn, cfg, preds, model, {"validation_years": [0, 0], "test_years": [0, 0],
                                            "selected_config": asdict(cfg), "test_headline": {}, "test_calibration": []})
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    monkeypatch.setattr(config, "DB_PATH", type(config.DB_PATH)(db_path))
    from tennisscout.api import app

    return TestClient(app)


def test_predict_ok(client):
    r = client.get("/predict", params={"a": "Alex Alpha", "b": "Ben Beta", "surface": "Hard"})
    assert r.status_code == 200
    body = r.json()
    assert 0 < body["p_a"] < 1
    assert body["p_a"] + body["p_b"] == pytest.approx(1)
    assert body["player_a"]["name"] == "Alex Alpha"
    assert body["data_as_of"]
    assert any("Surface Elo only" in c for c in body["caveats"])


def test_ambiguous_name_is_refused_with_candidates(client):
    r = client.get("/predict", params={"a": "John Smith", "b": "Alex Alpha"})
    assert r.status_code == 409
    assert len(r.json()["detail"]["candidates"]) == 2


def test_internal_id_disambiguates(client):
    pid = client.get("/players/search", params={"q": "John Smith"}).json()[0]["player_id"]
    r = client.get("/predict", params={"a": pid, "b": "Alex Alpha", "surface": "Clay"})
    assert r.status_code == 200


def test_unknown_player(client):
    assert client.get("/predict", params={"a": "Nobody Here", "b": "Alex Alpha"}).status_code == 404


def test_health(client):
    assert client.get("/health").json()["ok"] is True
    assert json.dumps(client.get("/backtest").json())
