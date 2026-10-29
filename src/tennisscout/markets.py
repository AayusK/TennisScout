"""Prediction-market archiver (spec, "Polymarket and Kalshi").

Historical APIs will not reconstruct quotes and sizes that were never
stored, so the spec says to start recording live books as early as possible.
This module does the simplest thing that preserves information: it stores the
*raw* JSON of each listing and order book with its collection timestamp, one
JSON line per request. Parsing into bids/asks/sizes and matching markets to
our match IDs (both players, tournament, round, date, market type; ambiguous
stays unmatched) happens later, from the archive, so a parsing mistake never
loses data.

Status at checkpoint 1: written against the public endpoints as documented by
Kalshi and Polymarket but NOT exercised; both hosts are blocked by this
environment's network policy. Verify endpoint paths and the Kalshi series
tickers for ATP singles (env ``TENNISSCOUT_KALSHI_SERIES``) before relying on it.
Run it on a schedule (cron, a GitHub Actions workflow, a small VM) from a
network that allows these hosts.
"""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path

from . import config, http

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
POLY_GAMMA = "https://gamma-api.polymarket.com"
POLY_CLOB = "https://clob.polymarket.com"


def _archive_path() -> Path:
    return config.DATA_DIR / "archive" / "markets" / f"{config.utcnow()[:10]}.jsonl.gz"


def _record(source: str, endpoint: str, params: dict, resp: http.Response) -> dict:
    body = None
    if resp.ok:
        try:
            body = resp.json()
        except ValueError:
            body = resp.body.decode("utf-8", "replace")
    return {
        "source": source,
        "endpoint": endpoint,
        "params": params,
        "collected_at": config.utcnow(),
        "status": resp.status,
        "error": resp.error,
        "body": body,
    }


def _append(records: list[dict]) -> Path:
    out = _archive_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "at", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    return out


def snapshot_kalshi(series: list[str]) -> list[dict]:
    records = []
    for s in series:
        params = {"series_ticker": s, "status": "open", "limit": 1000}
        listing = _record("kalshi", "/markets", params, http.get(f"{KALSHI_BASE}/markets", params))
        records.append(listing)
        markets = (listing["body"] or {}).get("markets", []) if isinstance(listing["body"], dict) else []
        for m in markets:
            ticker = m.get("ticker")
            if ticker:
                ep = f"/markets/{ticker}/orderbook"
                records.append(_record("kalshi", ep, {}, http.get(KALSHI_BASE + ep)))
    return records


def snapshot_polymarket(tag_slug: str = "tennis") -> list[dict]:
    params = {"tag_slug": tag_slug, "closed": "false", "limit": 200}
    listing = _record("polymarket", "/events", params, http.get(f"{POLY_GAMMA}/events", params))
    records = [listing]
    events = listing["body"] if isinstance(listing["body"], list) else []
    for ev in events:
        for m in ev.get("markets", []):
            tokens = m.get("clobTokenIds")
            if isinstance(tokens, str):
                try:
                    tokens = json.loads(tokens)
                except ValueError:
                    tokens = []
            for tok in tokens or []:
                records.append(_record("polymarket", "/book", {"token_id": tok}, http.get(f"{POLY_CLOB}/book", {"token_id": tok})))
    return records


def archive_once() -> dict:
    series = [s for s in os.environ.get("TENNISSCOUT_KALSHI_SERIES", "KXATPMATCH").split(",") if s]
    records = snapshot_kalshi(series) + snapshot_polymarket()
    path = _append(records)
    return {"records": len(records), "ok": sum(1 for r in records if r["status"] and 200 <= r["status"] < 300), "path": str(path)}


def probe() -> list[dict]:
    return [
        http.probe(f"{KALSHI_BASE}/series?limit=1"),
        http.probe(f"{POLY_GAMMA}/markets?limit=1"),
        http.probe(f"{POLY_CLOB}/"),
    ]
