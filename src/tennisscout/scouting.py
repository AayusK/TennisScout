"""Write the scouting report from a verified prediction (spec, step 4).

The LLM explains numbers the model already produced; it never makes up a
probability. Only the prediction dict goes in, so anything the report says has
to trace back to it. No news source is wired up yet, so the report says so
instead of guessing at injuries or form.
"""

from __future__ import annotations

import json
import os

MODEL = "claude-opus-5-5"

SYSTEM = """You write short pre-match tennis scouting reports for a research app.

You get one JSON object: a model's prediction with its layers, ratings, serve/return inputs and caveats.
Rules:
- Use only numbers that appear in the JSON. Never invent or adjust a probability, rating or statistic.
- No news, injury or market data is provided. Say that in one line; do not speculate about it.
- Write as analysis, not a betting pick.

Format (Markdown, under 250 words):
**Win probability**: both players' percentages, the model name and the prediction time.
**What the layers changed**: plain Elo baseline vs the final number, and which layer moved it.
**Key matchup**: serve and return edges (serve points won, hold rates) if present; otherwise say the serve/return layer is missing.
**X-factors**: only those in the data (surface, indoor, best-of, rating gaps, sample sizes).
**Confidence**: the confidence level and every caveat, in plain words."""


class ReportUnavailable(RuntimeError):
    pass


def _client():
    import anthropic

    key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_KEY")
    if not key:
        raise ReportUnavailable("set ANTHROPIC_API_KEY (or ANTHROPIC_KEY) to write reports")
    return anthropic.Anthropic(api_key=key)


def write_report(prediction: dict, client=None) -> str:
    import anthropic

    client = client or _client()
    try:
        r = client.messages.create(
            model=MODEL,
            max_tokens=4000,
            output_config={"effort": "low"},
            system=SYSTEM,
            messages=[{"role": "user", "content": json.dumps(prediction, default=str)}],
        )
    except anthropic.APIStatusError as e:
        raise ReportUnavailable(f"Anthropic API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise ReportUnavailable(f"could not reach the Anthropic API: {e}") from e
    if r.stop_reason == "refusal":
        raise ReportUnavailable("the model declined to write this report")
    return "".join(b.text for b in r.content if b.type == "text").strip()
