# TennisScout

A research app for pre-match tennis win probabilities, built from
[`tennis-scouting-spec.md`](tennis-scouting-spec.md). You enter two players and get a calibrated
probability with its data cut-off, its confidence and its caveats. The LLM scouting report comes
later in the MVP order, on top of verified numbers.

**Status: checkpoint 1** (spec "MVP order" step 1 plus the "First milestone: data audit").
See [`docs/checkpoint-1.md`](docs/checkpoint-1.md) for what was built, what the audit found, and
what is blocked.

| | |
|---|---|
| Backtest report | [`docs/backtest.md`](docs/backtest.md) (generated) |
| Data audit | [`docs/data-audit.md`](docs/data-audit.md) (generated) |

## Quick start

```bash
pip install -e ".[dev]"
```

```bash
tennisscout fetch          # clone TML-Database + Match Charting Project into data/raw, write snapshot manifests
```

```bash
tennisscout all -v         # ingest -> backtest + model selection -> audit -> docs/*.md  (~75 s)
```

```bash
tennisscout predict "Jannik Sinner" "Carlos Alcaraz" --surface Clay --best-of 5
```

```bash
tennisscout serve          # FastAPI on :8000 — /predict, /players/search, /backtest, /health
```

```bash
pytest                     # 41 tests on synthetic fixtures; no real data needed
```

Ambiguous names are refused rather than guessed. `tennisscout predict Zverev Sinner` lists the
three Zverevs with their internal IDs, and you pass one (`P007638`) instead.

## Layout

```
src/tennisscout/
  sources.py          source registry (license, role) + snapshot manifests (revision, sha256, collected_at)
  registry.py         permanent internal IDs (append-only, committed under registry/)
  ingest_tml.py       TML-Database -> players, tournaments, editions, matches; identity + timestamp fields
  ingest_charting.py  Match Charting Project -> strict name resolution and match-level linking
  elo.py              overall + surface Elo, blend, best-of-5 conversion, ranking-range prior
  backtest.py         chronological replay, validation-only model selection, baselines, breakdowns
  metrics.py          Brier, log loss, accuracy, calibration, ECE
  audit.py            first-milestone data audit (live probes of weather/market hosts)
  predict.py, api.py  name resolution + prediction service (CLI and FastAPI)
  weather.py          Open-Meteo archive / historical-forecast client (not yet exercised: host blocked)
  markets.py          raw Kalshi/Polymarket book archiver (not yet exercised: hosts blocked)
  report.py           renders docs/backtest.md and docs/data-audit.md
  schema.sql          SQLite DDL kept Postgres-compatible (Supabase later)
metadata/venues.csv        hand-written venue seed, versioned by year (UNVERIFIED, tagged as such)
metadata/player_aliases.csv  reviewed name -> ATP ID aliases (empty; audit lists candidates)
registry/                  internal ID registries: players, tournaments, editions, matches
data/                      (git-ignored) raw mirrors, SQLite DB, per-match audit output
```

## Data and licensing

| Source | Role | License |
|---|---|---|
| [TML-Database](https://github.com/Tennismylife/TML-Database) | Primary: ATP tour-level results 1968–Jan 2026, ATP IDs, serve stats, indoor flag | Personal / educational / non-commercial research only; **no redistribution of the raw data**; commercial use and monetized predictions need written permission |
| [Match Charting Project](https://github.com/JeffSackmann/tennis_MatchChartingProject) | Second source: exact dates, start times, point-by-point for a sample of matches | CC BY-NC-SA 4.0 (attribution, non-commercial) |
| [JeffSackmann/tennis_atp](https://github.com/JeffSackmann/tennis_atp) | The spec's original backbone | Returned 404 on 2026-10-29, which is why the pipeline has a second path |

Raw data stays in `data/` (git-ignored) because of these terms. What is committed is our own
material: code, the ID registries (internal ID ↔ external ID only), curated metadata, and
aggregate reports. This is a research tool. Marketing its output as betting picks would bring
in sports-wagering advertising law (spec, "Legal and product caution").
