"""Open-Meteo weather joins (spec, "Conditions").

Two different uses, two different endpoints:

* past-match conditions: the historical reanalysis archive
  (``archive-api.open-meteo.com``). Good for describing previous performances.
* conditions for a prediction: the forecast that existed at prediction time
  (``historical-forecast-api.open-meteo.com``). A backtest must use this one,
  never the weather that later occurred.

Values are city-level outdoor estimates, not courtside measurements, and are
meaningless for indoor sessions. Raw responses are stored with their
collection time so a re-run can show exactly what was known.

Status at checkpoint 1: written against Open-Meteo's documented API but not
exercised; the hosts are blocked by this environment's network policy.
"""

from __future__ import annotations

import gzip
import json
from datetime import date
from pathlib import Path

from . import config, http

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
HISTORICAL_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
HOURLY_VARS = ("temperature_2m", "relative_humidity_2m", "wind_speed_10m", "precipitation")


def fetch_hourly(lat: float, lon: float, start: date, end: date, kind: str = "archive") -> dict:
    url = ARCHIVE_URL if kind == "archive" else HISTORICAL_FORECAST_URL
    params = {
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "hourly": ",".join(HOURLY_VARS),
        "timezone": "UTC",
    }
    cached = _stored().get(json.dumps({"url": url, **params}, sort_keys=True))
    if cached:
        return cached
    r = http.get(url, params)
    record = {
        "kind": kind,
        "request": {"url": url, **params},
        "collected_at": config.utcnow(),
        "status": r.status,
        "error": r.error,
        "body": r.json() if r.ok else None,
    }
    _store(record)
    return record


_STORED: dict | None = None


def _stored() -> dict:
    """Successful responses already on disk, keyed by request, so re-runs do not spend the daily quota."""
    global _STORED
    if _STORED is None:
        _STORED = {}
        for f in sorted((config.DATA_DIR / "weather").glob("*.jsonl.gz")):
            with gzip.open(f, "rt", encoding="utf-8") as fh:
                try:
                    for line in fh:
                        rec = json.loads(line)
                        if rec.get("status") == 200:
                            _STORED[json.dumps(rec["request"], sort_keys=True)] = rec
                except (EOFError, ValueError):  # a run killed mid-write leaves a truncated tail
                    pass
    return _STORED


def _store(record: dict) -> Path:
    out = config.DATA_DIR / "weather" / f"{record['collected_at'][:10]}.jsonl.gz"
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "at", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return out


def probe() -> list[dict]:
    q = "?latitude=40.75&longitude=-73.85&start_date=2024-09-01&end_date=2024-09-01&hourly=temperature_2m"
    return [http.probe(ARCHIVE_URL + q), http.probe(HISTORICAL_FORECAST_URL + q)]
