"""Market-only vs model vs combined on the same matches and the same prediction times (spec, "Prediction engine").

For each of our tour matches with a linked exchange market and a scheduled
start, the market probability for player 1 is read at three checkpoints
(24 h before, 1 h before, last observation before the scheduled start) and
normalised across the two players to remove the margin. The model probability
is the served model's pre-match prediction for the same match. A combined
model (logistic on both logits) is fitted on the earlier half and scored on
the later half.

The trading check buys at the Kalshi ask one hour before the start when the
model's edge exceeds a threshold, pays Kalshi's taker fee, and settles at 0/1.
Historical depth is not stored, so available size is not verified.
"""

from __future__ import annotations

import gzip
import json
import math
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

from . import config, db, markets, metrics
from .audit import market_coverage
from .backtest import fit_logistic, paired_gain, safe_logit
from .ingest_charting import NameIndex

CACHE = config.RAW_DIR / "market_prices"
CHECKPOINTS = {"t-24h": 24 * 3600, "t-1h": 3600, "last": 0}
MARKET_KEYS = [f"{x}_{cp}" for x in ("kalshi", "polymarket") for cp in CHECKPOINTS]


def _ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T").replace("+00:00:00", "+00:00")).timestamp()


def _cached(name: str, fetch):
    path = CACHE / f"{name}.json.gz"
    if path.exists():
        with gzip.open(path, "rt") as fh:
            return json.load(fh)
    data = fetch()
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt") as fh:
        json.dump(data, fh)
    return data


def kalshi_series(ticker: str, start: float) -> list[tuple[float, float, float, float]]:
    """(time, bid, ask, mid) per 1-minute candle in the 26 h before the scheduled start (plus 5 min)."""
    lo = datetime.fromtimestamp(start - 26 * 3600, timezone.utc)
    hi = datetime.fromtimestamp(start + 300, timezone.utc)
    candles = _cached(f"kalshi/{ticker}", lambda: markets.kalshi_candles(ticker, lo, hi, True)
                      or markets.kalshi_candles(ticker, lo, hi, False))
    out = []
    for c in candles:
        bid, ask = (c.get("yes_bid") or {}).get("close"), (c.get("yes_ask") or {}).get("close")
        if bid is None or ask is None:
            continue
        bid, ask = float(bid), float(ask)
        if 0 < bid <= ask < 1:
            out.append((c["end_period_ts"], bid, ask, (bid + ask) / 2))
    return out


def polymarket_series(token: str, start: float) -> list[tuple[float, float]]:
    lo = datetime.fromtimestamp(start - 26 * 3600, timezone.utc)
    hi = datetime.fromtimestamp(start + 300, timezone.utc)
    pts = _cached(f"polymarket/{token}", lambda: markets.polymarket_prices(token, lo, hi))
    return [(p["t"], float(p["p"])) for p in pts if 0 < float(p["p"]) < 1]


def at(series: list[tuple], t: float):
    """Last observation at or before t."""
    best = None
    for row in series:
        if row[0] <= t:
            best = row
        else:
            break
    return best


def kalshi_fee(price: float) -> float:
    """Kalshi taker fee per contract: 7% of P(1-P), rounded up to the cent."""
    return math.ceil(0.07 * price * (1 - price) * 100 - 1e-9) / 100


def evaluate(conn: sqlite3.Connection, log=print, max_matches: int | None = None) -> dict:
    serving = db.get_meta(conn, "serving_model", "elo_sr_blend")
    preds = {r["match_id"]: r for r in conn.execute("SELECT * FROM prediction WHERE model = ?", (serving,))}
    elo = {r["match_id"]: r["p_player1"] for r in conn.execute("SELECT match_id, p_player1 FROM prediction WHERE model = 'elo_selected'")}
    starts = {r[0]: r[1] for r in conn.execute("SELECT match_id, start_time FROM match WHERE start_time IS NOT NULL")}
    dates = {r[0]: r[1] for r in conn.execute("SELECT match_id, event_date_lo FROM match")}
    cov = market_coverage(conn)
    names = NameIndex.from_db(conn)

    def side(name: str, p1: str, p2: str):
        c = (names.candidates(name) or names.fuzzy_candidates(name)) & {p1, p2}
        return next(iter(c)) if len(c) == 1 else None

    rows = []
    linked = sorted(cov["links"].items(), key=lambda kv: dates.get(kv[0], ""))
    for i, (mid, exch) in enumerate(linked):
        pr = preds.get(mid)
        if not pr:
            continue
        p1, p2 = pr["player1_id"], pr["player2_id"]
        sched = starts.get(mid) or (exch.get("polymarket") or {}).get("scheduled_start") or (exch.get("kalshi") or {}).get("scheduled_start")
        if not sched:
            continue
        t0 = _ts(sched)
        row = {"match_id": mid, "date": dates.get(mid), "y": pr["player1_won"], "model": pr["p_player1"], "elo": elo.get(mid),
               "start": sched, "start_source": "api_tennis" if mid in starts else "exchange"}
        k = exch.get("kalshi")
        if k:
            series = {side(n, p1, p2): kalshi_series(v["ticker"], t0) for n, v in k["players"].items()}
            if set(series) == {p1, p2}:
                for cp, dt in CHECKPOINTS.items():
                    a, b = at(series[p1], t0 - dt), at(series[p2], t0 - dt)
                    if a and b and t0 - dt - a[0] < 6 * 3600:
                        row[f"kalshi_{cp}"] = a[3] / (a[3] + b[3])
                        if cp == "t-1h":
                            row["kalshi_ask_p1"], row["kalshi_ask_p2"] = a[2], b[2]
        pm = exch.get("polymarket")
        if pm:
            series = {side(n, p1, p2): polymarket_series(v["token"], t0) for n, v in pm["players"].items() if v.get("token")}
            if set(series) == {p1, p2}:
                for cp, dt in CHECKPOINTS.items():
                    a, b = at(series[p1], t0 - dt), at(series[p2], t0 - dt)
                    if a and b and t0 - dt - a[0] < 6 * 3600:
                        row[f"polymarket_{cp}"] = a[1] / (a[1] + b[1])
        if any(key in row for key in MARKET_KEYS):
            rows.append(row)
        if (i + 1) % 200 == 0:
            log(f"  {i + 1}/{len(linked)} linked matches priced, {len(rows)} usable")
        if max_matches and len(rows) >= max_matches:
            break

    out = {"serving_model": serving, "n_rows": len(rows), "comparisons": {}}
    for market_key in MARKET_KEYS:
        same = [r for r in rows if r.get(market_key) is not None]
        if len(same) < 50:
            continue
        ys = [r["y"] for r in same]
        half = len(same) // 2
        fit_rows, test_rows = same[:half], same[half:]
        coef = fit_logistic([[safe_logit(r[market_key]), safe_logit(r["model"])] for r in fit_rows], [r["y"] for r in fit_rows])
        comb = [1 / (1 + math.exp(-(coef[0] * safe_logit(r[market_key]) + coef[1] * safe_logit(r["model"])))) for r in test_rows]
        ty = [r["y"] for r in test_rows]

        def recs(ps, rs):
            return [SimpleNamespace(p1=p, y=r["y"]) for p, r in zip(ps, rs)]

        out["comparisons"][market_key] = {
            "market_vs_model": paired_gain(recs([r["model"] for r in same], same), recs([r[market_key] for r in same], same)),
            "n": len(same),
            "market": metrics.summarize([r[market_key] for r in same], ys),
            "model": metrics.summarize([r["model"] for r in same], ys),
            "elo": metrics.summarize([r["elo"] for r in same if r["elo"] is not None], [r["y"] for r in same if r["elo"] is not None]),
            "later_half": {
                "n": len(test_rows), "combined_coef": coef,
                "market": metrics.summarize([r[market_key] for r in test_rows], ty),
                "model": metrics.summarize([r["model"] for r in test_rows], ty),
                "combined": metrics.summarize(comb, ty),
                "combined_vs_market": paired_gain(recs([r[market_key] for r in test_rows], test_rows), recs(comb, test_rows)),
            },
        }
    out["trading"] = trading(rows)
    path = config.OUTPUT_DIR / "market_eval_rows.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows))
    with conn:
        db.set_meta(conn, "market_eval", json.dumps(out))
    return out


def trading(rows: list[dict]) -> dict:
    """Flat one-contract bets at the Kalshi ask 1 h before start when model edge after fee exceeds a threshold."""
    res = {}
    for edge in (0.02, 0.05, 0.10):
        bets = []
        for r in rows:
            for p_model, ask, won in ((r["model"], r.get("kalshi_ask_p1"), r["y"]), (1 - r["model"], r.get("kalshi_ask_p2"), 1 - r["y"])):
                if ask is None:
                    continue
                cost = ask + kalshi_fee(ask)
                if p_model - cost >= edge:
                    bets.append(won - cost)
        n, pnl = len(bets), sum(bets)
        sd = (sum((b - pnl / n) ** 2 for b in bets) / (n - 1)) ** 0.5 if n > 1 else None
        res[f"edge>={edge}"] = {"bets": n, "pnl_per_contract_sum": round(pnl, 2), "roi": round(pnl / n, 4) if n else None,
                                "pnl_se": round(sd * n**0.5, 2) if sd else None}
    return res


__all__ = ["evaluate", "kalshi_fee", "at"]



def prefetch(conn: sqlite3.Connection, log=print) -> int:
    """Fill the price cache for every linked match with a scheduled start (reads the DB only)."""
    starts = {r[0]: r[1] for r in conn.execute("SELECT match_id, start_time FROM match WHERE start_time IS NOT NULL")}
    scored = {r[0] for r in conn.execute("SELECT DISTINCT match_id FROM prediction")}  # only matches we score
    cov = market_coverage(conn)
    n = 0
    for i, (mid, exch) in enumerate(sorted((k, v) for k, v in cov["links"].items() if k in scored)):
        sched = starts.get(mid) or (exch.get("polymarket") or {}).get("scheduled_start") or (exch.get("kalshi") or {}).get("scheduled_start")
        if not sched:
            continue
        t0 = _ts(sched)
        for v in (exch.get("kalshi") or {}).get("players", {}).values():
            kalshi_series(v["ticker"], t0)
            n += 1
        for v in (exch.get("polymarket") or {}).get("players", {}).values():
            if v.get("token"):
                polymarket_series(v["token"], t0)
                n += 1
        if (i + 1) % 250 == 0:
            log(f"prefetch: {i + 1}/{len(cov['links'])} matches, {n} series")
    return n
