"""Prediction-market archiver (spec, "Polymarket and Kalshi").

Historical APIs will not reconstruct quotes and sizes that were never
stored, so the spec says to start recording live books as early as possible.
This module does the simplest thing that preserves information: it stores the
*raw* JSON of each listing and order book with its collection timestamp, one
JSON line per request. Parsing into bids/asks/sizes and matching markets to
our match IDs (both players, tournament, round, date, market type; ambiguous
stays unmatched) happens later, from the archive, so a parsing mistake never
loses data.

Historical side (for the audit): ``fetch_listings`` stores every Kalshi ATP
match market (live and historical endpoints) and every closed Polymarket
tennis event, with the collection time; ``kalshi_events`` / ``polymarket_events``
parse them into one record per match; ``kalshi_candles`` and
``polymarket_prices`` pull price history for a market.
Kalshi series tickers can be overridden with ``TENNISSCOUT_KALSHI_SERIES``.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import time
from datetime import date, datetime
from pathlib import Path

from . import config, http

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
POLY_GAMMA = "https://gamma-api.polymarket.com"
POLY_CLOB = "https://clob.polymarket.com"


def _archive_path() -> Path:
    return config.DATA_DIR / "archive" / "markets" / f"{config.utcnow()[:10]}.jsonl"  # plain text survives a killed run


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


def snapshot_kalshi(series: list[str]):
    for s in series:
        params = {"series_ticker": s, "status": "open", "limit": 1000}
        listing = _record("kalshi", "/markets", params, http.get(f"{KALSHI_BASE}/markets", params))
        yield listing
        markets = (listing["body"] or {}).get("markets", []) if isinstance(listing["body"], dict) else []
        for m in markets:
            ticker = m.get("ticker")
            if ticker:
                ep = f"/markets/{ticker}/orderbook"
                yield _record("kalshi", ep, {}, http.get(KALSHI_BASE + ep))


def snapshot_polymarket(tag_slug: str = "tennis"):
    params = {"tag_slug": tag_slug, "closed": "false", "limit": 200}
    listing = _record("polymarket", "/events", params, http.get(f"{POLY_GAMMA}/events", params))
    yield listing
    events = listing["body"] if isinstance(listing["body"], list) else []
    for ev in events:
        if "/" in (ev.get("title") or ""):  # doubles
            continue
        for m in ev.get("markets", []):
            if m.get("sportsMarketType") != "moneyline" or m.get("closed"):
                continue
            for tok in json.loads(m.get("clobTokenIds") or "[]"):
                yield _record("polymarket", "/book", {"token_id": tok}, http.get(f"{POLY_CLOB}/book", {"token_id": tok}))


def archive_once() -> dict:
    """One raw snapshot; each record is written as soon as it is fetched, so an interrupted run keeps what it got."""
    series = [s for s in os.environ.get("TENNISSCOUT_KALSHI_SERIES", "KXATPMATCH").split(",") if s]
    out = _archive_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    n = ok = 0
    with open(out, "a", encoding="utf-8") as fh:
        for source in (snapshot_kalshi(series), snapshot_polymarket()):
            for r in source:
                fh.write(json.dumps(r) + "\n")
                fh.flush()
                n += 1
                ok += bool(r["status"] and 200 <= r["status"] < 300)
    return {"records": n, "ok": ok, "path": str(out)}


def _top(levels, n: int = 5) -> list:
    return levels[:n] if isinstance(levels, list) else []


def compact_snapshot(depth: int = 5) -> list[dict]:
    """ATP singles only: best prices, top-of-book depth with sizes, last trade, volume. Small enough for git."""
    t = config.utcnow()
    out = []
    for series in [s for s in os.environ.get("TENNISSCOUT_KALSHI_SERIES", "KXATPMATCH").split(",") if s]:
        r = http.get(f"{KALSHI_BASE}/markets", {"series_ticker": series, "status": "open", "limit": 1000})
        for m in (r.json().get("markets") or []) if r.ok else []:
            book = http.get(f"{KALSHI_BASE}/markets/{m['ticker']}/orderbook")
            ob = (book.json().get("orderbook_fp") or {}) if book.ok else {}
            # Kalshi books list resting bids for YES and for NO; best levels are at the end.
            out.append({"t": t, "x": "kalshi", "market": m["ticker"], "event": m.get("event_ticker"),
                        "player": m.get("yes_sub_title"), "start": m.get("occurrence_datetime"),
                        "bid": m.get("yes_bid_dollars"), "ask": m.get("yes_ask_dollars"), "last": m.get("last_price_dollars"),
                        "volume": m.get("volume_fp"), "oi": m.get("open_interest_fp"),
                        "yes_bids": (ob.get("yes_dollars") or [])[-depth:], "no_bids": (ob.get("no_dollars") or [])[-depth:],
                        "book_status": book.status})
    events = _paged(f"{POLY_GAMMA}/events/keyset", "events", {"tag_slug": "tennis", "closed": "false", "limit": 500},
                    cursor_param="after_cursor", cursor_key="next_cursor")
    for ev in events:
        title = ev.get("title") or ""
        if not (ev.get("slug") or "").startswith("atp-") or " vs" not in title or "/" in title:
            continue  # head-to-head ATP singles only
        for m in ev.get("markets", []):
            if m.get("sportsMarketType") != "moneyline" or m.get("closed"):
                continue
            names = json.loads(m.get("outcomes") or "[]")
            for name, tok in zip(names, json.loads(m.get("clobTokenIds") or "[]")):
                book = http.get(f"{POLY_CLOB}/book", {"token_id": tok})
                b = book.json() if book.ok else {}
                bids = sorted(b.get("bids") or [], key=lambda x: -float(x["price"]))
                asks = sorted(b.get("asks") or [], key=lambda x: float(x["price"]))
                out.append({"t": t, "x": "polymarket", "market": tok, "event": ev.get("slug"), "title": ev.get("title"),
                            "player": name, "start": m.get("gameStartTime"), "last": b.get("last_trade_price"),
                            "bids": _top(bids, depth), "asks": _top(asks, depth), "book_status": book.status})
    return out


def archive_compact(out_dir: Path) -> dict:
    """One gzip file per run: <out_dir>/YYYY/MM/DD/HHMM.jsonl.gz (never rewritten, so git stays small)."""
    rows = compact_snapshot()
    t = config.utcnow()
    path = Path(out_dir) / t[:4] / t[5:7] / t[8:10] / f"{t[11:13]}{t[14:16]}.jsonl.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")
    return {"records": len(rows), "path": str(path), "books_ok": sum(1 for r in rows if r["book_status"] == 200)}


def probe() -> list[dict]:
    return [
        http.probe(f"{KALSHI_BASE}/series?limit=1"),
        http.probe(f"{POLY_GAMMA}/markets?limit=1"),
        http.probe(f"{POLY_CLOB}/"),
    ]


# -- historical listings -----------------------------------------------------------

LISTING_DIR = config.RAW_DIR / "markets"
# 2025 markets say "...match originally scheduled for Sep 2, 2025 after..." with no tournament or round.
KALSHI_RULES = re.compile(
    r"If (?P<yes>.+?) wins the (?P<a>.+?) vs (?P<b>.+?) professional tennis match "
    r"(?:in the (?P<year>\d{4}) (?P<rest>.+?)|originally scheduled for .+?) after a ball"
)
KALSHI_ROUNDS = {
    "round of 128": "R128", "round of 64": "R64", "round of 32": "R32", "round of 16": "R16",
    "quarterfinal": "QF", "quarterfinals": "QF", "semifinal": "SF", "semifinals": "SF", "final": "F",
    "round robin": "RR",
}
MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}


def _paged(url: str, key: str, params: dict, cursor_param: str = "cursor", cursor_key: str = "cursor") -> list[dict]:
    out, cursor = [], None
    while True:
        r = http.get(url, {**params, **({cursor_param: cursor} if cursor else {})}, timeout=60)
        if not r.ok:
            raise RuntimeError(f"{r.url}: {r.error}")
        d = r.json()
        out += d.get(key) or []
        nxt = d.get(cursor_key)
        if not d.get(key) or not nxt or nxt == cursor:
            return out
        cursor = nxt
        time.sleep(0.5)


def fetch_listings() -> dict:
    """Store raw historical listings for both exchanges, stamped with the collection time."""
    LISTING_DIR.mkdir(parents=True, exist_ok=True)
    collected_at = config.utcnow()
    counts = {}
    for series in [s for s in os.environ.get("TENNISSCOUT_KALSHI_SERIES", "KXATPMATCH").split(",") if s]:
        markets = _paged(f"{KALSHI_BASE}/historical/markets", "markets", {"series_ticker": series, "limit": 1000})
        markets += _paged(f"{KALSHI_BASE}/markets", "markets", {"series_ticker": series, "limit": 1000})
        _write(f"kalshi_{series}", collected_at, markets)
        counts[f"kalshi_{series}"] = len(markets)
    events = _paged(f"{POLY_GAMMA}/events/keyset", "events", {"tag_slug": "tennis", "closed": "true", "limit": 500},
                    cursor_param="after_cursor", cursor_key="next_cursor")
    _write("polymarket_tennis_closed", collected_at, events)
    counts["polymarket_tennis_closed"] = len(events)
    return counts


def _write(name: str, collected_at: str, rows: list) -> None:
    with gzip.open(LISTING_DIR / f"{name}.json.gz", "wt", encoding="utf-8") as fh:
        json.dump({"collected_at": collected_at, "rows": rows}, fh)


def load_listing(name: str) -> dict:
    with gzip.open(LISTING_DIR / f"{name}.json.gz", "rt", encoding="utf-8") as fh:
        return json.load(fh)


def kalshi_events(markets: list[dict]) -> list[dict]:
    """One record per match event: both players' full names, tournament, round, date, per-player tickers."""
    by_event: dict[str, dict] = {}
    for m in markets:
        hit = KALSHI_RULES.search(m.get("rules_primary") or "")
        if not hit:
            continue
        rest = hit["rest"] or ""
        words = next((w for w in sorted(KALSHI_ROUNDS, key=len, reverse=True) if rest.lower().endswith(w)), "")
        rnd = KALSHI_ROUNDS.get(words)
        if "qualification" in rest.lower():
            rnd = "Q"  # our tour data has no qualifying, so these must never link to a main-draw meeting
        elif "group" in rest.lower():
            rnd = "RR"
        tourney = rest[: len(rest) - len(words)].strip()
        ev = by_event.setdefault(m["event_ticker"], {
            "exchange": "kalshi", "event": m["event_ticker"], "date": _kalshi_date(m["event_ticker"]),
            "scheduled_start": m.get("occurrence_datetime"), "tournament": tourney, "round": rnd,
            "players": {}, "winner": None,
        })
        ev["players"][hit["yes"].strip()] = {"ticker": m["ticker"], "close_time": m.get("close_time"),
                                             "volume": m.get("volume_fp")}
        if m.get("result") == "yes":
            ev["winner"] = hit["yes"].strip()
    return [e for e in by_event.values() if len(e["players"]) == 2]


def _kalshi_date(event_ticker: str) -> str | None:
    # KXATPMATCH-26JAN23ZVENOR -> 2026-01-23
    hit = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", event_ticker)
    if not hit or hit[2] not in MONTHS:
        return None
    return date(2000 + int(hit[1]), MONTHS[hit[2]], int(hit[3])).isoformat()


def polymarket_events(events: list[dict]) -> list[dict]:
    """Head-to-head moneyline events: 'Tournament: First Last vs First Last'."""
    out = []
    for e in events:
        title = e.get("title") or ""
        if ": " not in title or " vs " not in title or "/" in title:  # '/' marks doubles pairs
            continue
        tourney, pair = title.rsplit(": ", 1)
        a, _, b = pair.partition(" vs ")
        mk = next((m for m in e.get("markets", []) if m.get("sportsMarketType") == "moneyline"), None)
        if not mk:
            continue
        tokens = json.loads(mk.get("clobTokenIds") or "[]")
        start = (mk.get("gameStartTime") or "").replace(" ", "T").replace("+00", "Z") or None
        prices = json.loads(mk.get("outcomePrices") or "[]")
        winner = None
        if len(prices) == 2 and {float(x) for x in prices} == {0.0, 1.0}:
            winner = (a, b)[[float(x) for x in prices].index(1.0)].strip()
        out.append({
            "exchange": "polymarket", "event": e.get("slug"), "date": (start or e.get("startDate") or "")[:10] or None,
            "scheduled_start": start, "tournament": tourney.split(",")[0].strip(), "round": None,
            "players": {a.strip(): {"token": tokens[0] if tokens else None}, b.strip(): {"token": tokens[1] if len(tokens) > 1 else None}},
            "winner": winner,
        })
    return out


def kalshi_candles(ticker: str, start: datetime, end: datetime, historical: bool, interval_min: int = 1) -> list[dict]:
    path = f"/historical/markets/{ticker}/candlesticks" if historical else f"/series/{ticker.split('-')[0]}/markets/{ticker}/candlesticks"
    r = http.get(KALSHI_BASE + path, {"start_ts": int(start.timestamp()), "end_ts": int(end.timestamp()), "period_interval": interval_min})
    return (r.json().get("candlesticks") or []) if r.ok else []


def polymarket_prices(token: str, start: datetime, end: datetime, fidelity_min: int = 1) -> list[dict]:
    r = http.get(f"{POLY_CLOB}/prices-history", {"market": token, "startTs": int(start.timestamp()), "endTs": int(end.timestamp()), "fidelity": fidelity_min})
    return (r.json().get("history") or []) if r.ok else []
