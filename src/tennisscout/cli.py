"""Command line entry point: `tennisscout <command>` (or `python -m tennisscout`)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import audit, backtest, db, ingest_charting, ingest_tml, markets, predict, report, sources


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
    a, b = res.player_a, res.player_b
    print(f"{a.name} vs {b.name} | {res.surface}{' indoor' if res.indoor else ''}, best of {res.best_of}")
    print(f"  P({a.name} wins) = {res.p_a:.1%}   (uncalibrated Elo blend: {res.baseline_p_a:.1%})")
    print(f"  confidence: {res.confidence}; data as of {res.data_as_of}; model {res.model}")
    r = res.ratings
    print(f"  ratings ({r['bucket']}): overall {r['overall'][0]:.0f} / {r['overall'][1]:.0f}, "
          f"surface {r['surface'][0]:.0f} / {r['surface'][1]:.0f}, matches {r['matches_overall'][0]} / {r['matches_overall'][1]}")
    for c in res.caveats:
        print(f"  ! {c}")


def cmd_search(args) -> None:
    for r in predict.search(_conn(), args.query, limit=args.limit):
        print(f"{r.player_id}  {r.name:30s} {r.ioc or '':4s} born {r.birthdate or '?':9s} last {r.last_match or '-':10s} {r.matches} matches")


def cmd_archive_markets(args) -> None:
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
    b = sub.add_parser("backtest", help="chronological Elo backtest + model selection")
    b.add_argument("-v", "--verbose", action="store_true")
    b.set_defaults(func=cmd_backtest)
    sub.add_parser("audit", help="run the data audit").set_defaults(func=cmd_audit)
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
    pr.set_defaults(func=cmd_predict)

    s = sub.add_parser("search", help="find players by name")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(func=cmd_search)

    sub.add_parser("archive-markets", help="store one raw snapshot of Kalshi/Polymarket tennis books").set_defaults(func=cmd_archive_markets)
    sv = sub.add_parser("serve", help="run the FastAPI service")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.set_defaults(func=cmd_serve)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
