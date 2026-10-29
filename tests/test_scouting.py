"""The real SDK against a mocked network: checks the request we send and how responses are read."""

import json
from dataclasses import asdict

import anthropic
import httpx2
import pytest

from tennisscout import backtest, cli, config, scouting
from tennisscout.elo import EloConfig


def sdk_client(status=200, body=None, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(request)
        return httpx2.Response(status, json=body)

    return anthropic.Anthropic(api_key="test", max_retries=0,
                               http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))


def message(stop_reason="end_turn", text="report"):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": scouting.MODEL,
            "content": [{"type": "text", "text": text}] if text else [],
            "stop_reason": stop_reason, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 5}}


def test_request_shape_and_text():
    seen = []
    assert scouting.write_report({"p_a": 0.61}, client=sdk_client(body=message(text=" report "), seen=seen)) == "report"
    req = seen[0]
    body = json.loads(req.content)
    assert req.url.path == "/v1/messages"
    assert "server-side-fallback-2026-07-01" in req.headers["anthropic-beta"]
    assert body["model"] == "claude-opus-5-5" and body["fallbacks"] == "default"
    assert body["output_config"] == {"effort": "low"} and "thinking" not in body
    assert json.loads(body["messages"][0]["content"]) == {"p_a": 0.61}


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_incomplete_report_raises(stop):
    with pytest.raises(scouting.ReportUnavailable, match=stop):
        scouting.write_report({}, client=sdk_client(body=message(stop, text="")))


def test_api_error_raises():
    err = {"type": "error", "error": {"type": "invalid_request_error", "message": "Your credit balance is too low"}}
    with pytest.raises(scouting.ReportUnavailable, match="credit balance"):
        scouting.write_report({}, client=sdk_client(400, err))


def test_key_from_env(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_KEY", raising=False)
    with pytest.raises(scouting.ReportUnavailable, match="ANTHROPIC_API_KEY"):
        scouting.write_report({})
    monkeypatch.setenv("ANTHROPIC_KEY", "sk-test")
    assert scouting._client().api_key == "sk-test"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-standard")
    assert scouting._client().api_key == "sk-standard"


@pytest.fixture
def db_with_ratings(loaded, monkeypatch):
    conn, *_ = loaded
    cfg = EloConfig(name="elo_selected")
    preds, model = backtest.replay(backtest.load_matches(conn), cfg, 2000)
    backtest.save(conn, cfg, preds, model, {"validation_years": [0, 0], "test_years": [0, 0],
                                            "selected_config": asdict(cfg), "test_headline": {}, "test_calibration": []})
    monkeypatch.setattr(config, "DB_PATH", type(config.DB_PATH)(conn.execute("PRAGMA database_list").fetchone()[2]))


def test_cli_report_end_to_end(db_with_ratings, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(scouting, "_client", lambda: sdk_client(body=message(text="**Win probability**: ..."), seen=seen))
    cli.main(["predict", "Alex Alpha", "Ben Beta", "--report"])
    assert capsys.readouterr().out.strip() == "**Win probability**: ..."
    sent = json.loads(json.loads(seen[0].content)["messages"][0]["content"])
    assert sent["player_a"]["name"] == "Alex Alpha" and sent["player_b"]["name"] == "Ben Beta"
    assert sent["p_a"] + sent["p_b"] == pytest.approx(1)


def test_cli_report_error_exits_cleanly(db_with_ratings, monkeypatch, capsys):
    err = {"type": "error", "error": {"type": "invalid_request_error", "message": "Your credit balance is too low"}}
    monkeypatch.setattr(scouting, "_client", lambda: sdk_client(400, err))
    with pytest.raises(SystemExit) as e:
        cli.main(["predict", "Alex Alpha", "Ben Beta", "--report"])
    assert e.value.code == 1
    assert "report unavailable" in capsys.readouterr().err
