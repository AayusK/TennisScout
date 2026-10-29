import json
from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient

from tennisscout import backtest, config
from tennisscout.elo import EloConfig
from tennisscout.serve_return import SRConfig


@pytest.fixture
def client(loaded, tmp_path, monkeypatch):
    conn, *_ = loaded
    cfg = EloConfig(name="elo_selected")
    preds, model = backtest.replay(backtest.load_matches(conn), cfg, 2000)
    backtest.save(conn, cfg, preds, model, {"validation_years": [0, 0], "test_years": [0, 0],
                                            "selected_config": asdict(cfg), "test_headline": {}, "test_calibration": []})
    sr_preds, sr_model = backtest.replay_sr(backtest.load_matches(conn), SRConfig(), 2000)
    blend = backtest.blend(preds, sr_preds, [0.6, 0.3])
    backtest.save_sr(conn, SRConfig(), sr_model, {"serve_return": sr_preds, "elo_sr_blend": blend}, [0.6, 0.3], "elo_sr_blend")
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
    assert body["model"] == "elo_sr_blend"
    assert body["p_a"] == pytest.approx(body["components"]["elo_sr_blend"])
    assert {"elo_selected", "serve_return", "serve_point_win", "hold"} <= set(body["components"])


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


def test_upcoming_rows(client, loaded):
    from tennisscout import predict, upcoming

    conn, *_ = loaded
    a, b = predict.resolve(conn, "Alex Alpha").player_id, predict.resolve(conn, "Ben Beta").player_id
    conn.executemany("INSERT OR IGNORE INTO player_external_id VALUES ('api_tennis', ?, ?)", [("1", a), ("2", b)])
    base = {"event_date": "2026-10-30", "event_time": "12:00", "tournament_name": "Basel", "tournament_key": 9,
            "tournament_round": "ATP Basel - Quarter-finals", "event_status": "", "event_winner": None, "event_qualification": "False"}
    fixtures = [
        {**base, "event_key": 10, "first_player_key": 1, "second_player_key": 2, "event_first_player": "A. Alpha", "event_second_player": "B. Beta"},
        {**base, "event_key": 11, "first_player_key": 1, "second_player_key": 3, "event_first_player": "A. Alpha", "event_second_player": "X. New"},
        {**base, "event_key": 12, "first_player_key": 1, "second_player_key": 2, "event_first_player": "A. Alpha",
         "event_second_player": "B. Beta", "event_status": "Finished", "event_winner": "First Player"},
    ]
    out = upcoming.rows(conn, fixtures, {9: {"tournament_sourface": "Hard (Indoor)"}})
    assert [r["event_key"] for r in out] == [10, 11]  # the finished match is dropped
    assert out[0]["surface"] == "Hard" and out[0]["indoor"] and out[0]["round"] == "QF"
    assert 0 < out[0]["p_a"] < 1 and out[0]["player_a_id"] == a
    assert out[1]["error"] == "player not in the registry yet" and "p_a" not in out[1]
