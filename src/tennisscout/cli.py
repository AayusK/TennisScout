"""Command line entry point: `tennisscout <command>` (or `python -m tennisscout`)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import api_tennis, audit, backtest, db, ingest_api_tennis, ingest_charting, ingest_tml, markets, predict, report, sources, venues


def _conn():
    conn = db.connect()
    db.init(conn)
    return conn


def cmd_fetch(args) -> None:
    names = args.sources or ["tml", "mcp"]
    for name in names:
        snap = sources.fetch(name, Path(args.path) if args.path else None)
        print(f"{snap.snapshot_id}: {len(snap.files)} files, collected {snap.collected_at}")


def cmd_ingest(args) -> None:
    conn = _conn()
    rep = ingest_tml.ingest(conn, sources.load_snapshot("tml"))
    print(json.dumps(rep.__dict__, indent=2))
    if (api_tennis.RAW / "tournaments.json.gz").exists():
        ingest_api_tennis.ingest(conn)
    else:
        print("no API-Tennis data; run `tennisscout fetch-api-tennis` to add 2026 and Challenger results")
    try:
        mcp = sources.load_snapshot("mcp")
    except FileNotFoundError:
        print("MCP snapshot missing; skipping charting link step")
        return
    out = ingest_charting.ingest(conn, mcp)
    print(json.dumps({k: out[k] for k in ("charts", "link_status", "final_player_status")}, indent=2))


def cmd_backtest(args) -> None:
    conn = _conn()
    res = backtest.run(conn, log=print if args.verbose else (lambda *_: None))
    print(json.dumps({"selected_config": res["selected_config"], "test_headline": res["test_headline"]}, indent=2))


def cmd_audit(args) -> None:
    conn = _conn()
    res = audit.run(conn)
    print(json.dumps(res["sample_summary"], indent=2))


def cmd_verify_venues(args) -> None:
    from datetime import datetime, timezone

    rows = venues.verify()
    with _conn() as conn:
        db.set_meta(conn, "venue_check", json.dumps({"run_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "rows": rows}))
    bad = [r for r in rows if r["status"] != "checked" or not (r["elevation_ok"] and r["region_ok"])]
    print(f"{len(rows) - len(bad)} of {len(rows)} venue rows agree with the DEM and timezone")
    for r in bad:
        print(" ", r)


def cmd_upcoming(args) -> None:
    from . import upcoming

    for r in upcoming.upcoming(_conn(), args.days):
        p = f"{r['p_a']:.1%}  [{r['model']}, {r['confidence']}]" if "p_a" in r else f"–  ({r['error']})"
        if "kalshi_p_a" in r:
            p += f"  Kalshi {r['kalshi_p_a']:.1%} (bid {r['kalshi_bid_a']:.2f} / ask {r['kalshi_ask_a']:.2f})"
        print(f"{r['start'][:16]}  {r['tournament']} {r['round']}: {r['player_a']} vs {r['player_b']}  P({r['player_a']}) = {p}")


def cmd_evaluate_markets(args) -> None:
    from . import market_eval

    out = market_eval.evaluate(_conn())
    for key, c in out["comparisons"].items():
        print(f"{key}: n={c['n']} market log loss {c['market']['log_loss']:.4f} model {c['model']['log_loss']:.4f}")


def cmd_report(args) -> None:
    for p in report.write(_conn()):
        print("wrote", p)


def cmd_all(args) -> None:
    cmd_ingest(args)
    cmd_backtest(args)
    cmd_audit(args)
    cmd_report(args)


def cmd_predict(args) -> None:
    conn = _conn()
    try:
        res = predict.predict(conn, args.player_a, args.player_b, args.surface, args.indoor, args.best_of)
    except predict.AmbiguousPlayer as e:
        print(f"{e.query!r} is ambiguous; pass one of these IDs instead:", file=sys.stderr)
        for c in e.candidates:
            print(f"  {c['player_id']}  {c['name']} ({c['ioc']}, born {c['birthdate']}, last match {c['last_match']}, {c['matches']} matches)", file=sys.stderr)
        sys.exit(2)
    except predict.UnknownPlayer as e:
        print(f"unknown player {e}", file=sys.stderr)
        sys.exit(2)
    if args.json:
        print(json.dumps(res.to_dict(), indent=2))
        return
    if args.report:
        from . import scouting
        try:
            print(scouting.write_report(res.to_dict()))
        except scouting.ReportUnavailable as e:
            print(f"report unavailable: {e}", file=sys.stderr)
            sys.exit(1)
        return
    a, b = res.player_a, res.player_b
    print(f"{a.name} vs {b.name} | {res.surface}{' indoor' if res.indoor else ''}, best of {res.best_of}")
    print(f"  P({a.name} wins) = {res.p_a:.1%}   [{res.model}]")
    c = res.components
    print(f"  layers: plain Elo {res.baseline_p_a:.1%} -> calibrated Elo {c['elo_selected']:.1%}"
          + (f" | serve/return Markov {c['serve_return']:.1%} -> blend {c['elo_sr_blend']:.1%}" if "serve_return" in c else ""))
    if "serve_point_win" in c:
        print(f"  serve points won {c['serve_point_win'][0]:.1%} / {c['serve_point_win'][1]:.1%}, "
              f"hold {c['hold'][0]:.1%} / {c['hold'][1]:.1%}")
    print(f"  confidence: {res.confidence}; data as of {res.data_as_of}; model {res.model}")
    r = res.ratings
    print(f"  ratings ({r['bucket']}): overall {r['overall'][0]:.0f} / {r['overall'][1]:.0f}, "
          f"surface {r['surface'][0]:.0f} / {r['surface'][1]:.0f}, matches {r['matches_overall'][0]} / {r['matches_overall'][1]}")
    for c in res.caveats:
        print(f"  ! {c}")


def cmd_search(args) -> None:
    for r in predict.search(_conn(), args.query, limit=args.limit):
        print(f"{r.player_id}  {r.name:30s} {r.ioc or '':4s} born {r.birthdate or '?':9s} last {r.last_match or '-':10s} {r.matches} matches")


def cmd_fetch_api_tennis(args) -> None:
    from datetime import date

    print("tournaments", api_tennis.fetch_tournaments())
    api_tennis.fetch_fixtures("atp", date(2026, 1, 1), date.today())
    api_tennis.fetch_fixtures("challenger", date(2019, 1, 1), date.today())
    print("player lists fetched", api_tennis.fetch_players())


def cmd_fetch_markets(args) -> None:
    print(json.dumps(markets.fetch_listings(), indent=2))


def cmd_archive_markets(args) -> None:
    if args.compact:
        print(json.dumps(markets.archive_compact(Path(args.out)), indent=2))
    else:
        print(json.dumps(markets.archive_once(), indent=2))


def cmd_serve(args) -> None:
    import uvicorn

    uvicorn.run("tennisscout.api:app", host=args.host, port=args.port)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="tennisscout", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="clone/update source mirrors and write snapshot manifests")
    f.add_argument("sources", nargs="*", choices=list(sources.SOURCES), help="default: tml mcp")
    f.add_argument("--path", help="use an existing local checkout instead of cloning")
    f.set_defaults(func=cmd_fetch)

    sub.add_parser("ingest", help="load mirrors into the database under permanent IDs").set_defaults(func=cmd_ingest)
    b = sub.add_parser("backtest", help="chronological backtest: Elo, serve/return Markov, blend; model selection on validation")
    b.add_argument("-v", "--verbose", action="store_true")
    b.set_defaults(func=cmd_backtest)
    sub.add_parser("audit", help="run the data audit").set_defaults(func=cmd_audit)
    up = sub.add_parser("upcoming", help="scheduled ATP matches from API-Tennis with predictions (needs API_TENNIS_KEY)")
    up.add_argument("--days", type=int, default=2)
    up.set_defaults(func=cmd_upcoming)
    sub.add_parser("evaluate-markets", help="model vs Kalshi/Polymarket at the price checkpoints (after fetch-markets)").set_defaults(func=cmd_evaluate_markets)
    sub.add_parser("verify-venues", help="check venue elevations and timezones against Open-Meteo (128 calls)").set_defaults(func=cmd_verify_venues)
    sub.add_parser("report", help="write docs/backtest.md and docs/data-audit.md").set_defaults(func=cmd_report)
    a = sub.add_parser("all", help="ingest, backtest, audit, report")
    a.add_argument("-v", "--verbose", action="store_true")
    a.set_defaults(func=cmd_all)

    pr = sub.add_parser("predict", help="pre-match win probability for two players")
    pr.add_argument("player_a")
    pr.add_argument("player_b")
    pr.add_argument("--surface", default="Hard", choices=["Hard", "Clay", "Grass", "Carpet"])
    pr.add_argument("--indoor", action="store_true")
    pr.add_argument("--best-of", type=int, default=3, choices=[3, 5])
    pr.add_argument("--json", action="store_true")
    pr.add_argument("--report", action="store_true", help="LLM scouting report from the prediction (needs ANTHROPIC_API_KEY)")
    pr.set_defaults(func=cmd_predict)

    s = sub.add_parser("search", help="find players by name")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(func=cmd_search)

    sub.add_parser("fetch-api-tennis", help="store API-Tennis ATP 2026 + Challenger results (needs API_TENNIS_KEY)").set_defaults(func=cmd_fetch_api_tennis)
    sub.add_parser("fetch-markets", help="store historical Kalshi/Polymarket tennis listings (used by the audit)").set_defaults(func=cmd_fetch_markets)
    am = sub.add_parser("archive-markets", help="store one snapshot of Kalshi/Polymarket tennis books")
    am.add_argument("--compact", action="store_true", help="ATP singles, top-5 depth, one small gzip per run")
    am.add_argument("--out", default="archive/markets", help="output directory for --compact")
    am.set_defaults(func=cmd_archive_markets)
    sv = sub.add_parser("serve", help="run the FastAPI service")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.set_defaults(func=cmd_serve)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
