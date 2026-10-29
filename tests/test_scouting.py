from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from tennisscout import scouting


class FakeMessages:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.kwargs = response, error, None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return self.response


def fake_client(**kw):
    return SimpleNamespace(messages=FakeMessages(**kw))


def test_report_sends_prediction_and_returns_text():
    c = fake_client(response=SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=" report ")]))
    assert scouting.write_report({"p_a": 0.61}, client=c) == "report"
    assert '"p_a": 0.61' in c.messages.kwargs["messages"][0]["content"]


def test_refusal_and_api_error_raise_unavailable():
    c = fake_client(response=SimpleNamespace(stop_reason="refusal", content=[]))
    with pytest.raises(scouting.ReportUnavailable):
        scouting.write_report({}, client=c)
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.BadRequestError("low credit", response=httpx2.Response(400, request=req), body=None)
    with pytest.raises(scouting.ReportUnavailable, match="400"):
        scouting.write_report({}, client=fake_client(error=err))
