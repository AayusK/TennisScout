# TennisScout

A research app for pre-match tennis win probabilities, built from
[`tennis-scouting-spec.md`](tennis-scouting-spec.md). You enter two players and get a calibrated
probability with its data cut-off, its confidence and its caveats. The LLM scouting report comes
later in the MVP order, on top of verified numbers.

**Status: checkpoint 3** (fresh 2026 results and Challenger data via API-Tennis, a forward holdout,
context features and an ML layer, a market comparison and a live order-book archive), on top of
checkpoints 1 and 2. See [`docs/checkpoint-3.md`](docs/checkpoint-3.md),
[`docs/checkpoint-2.md`](docs/checkpoint-2.md) and [`docs/checkpoint-1.md`](docs/checkpoint-1.md).

| | |
|---|---|
| Backtest report | [`docs/backtest.md`](docs/backtest.md) (generated) |
| Data audit | [`docs/data-audit.md`](docs/data-audit.md) (generated) |
| Model vs markets | [`docs/markets.md`](docs/markets.md) (generated) |

## Quick start

```bash
pip install -e ".[dev,ml]"   # ml = optional LightGBM layer; without it the logistic stack is served
```

```bash
tennisscout fetch          # clone TML-Database + Match Charting Project into data/raw, write snapshot manifests
```

```bash
tennisscout fetch-markets  # store Kalshi + Polymarket tennis market listings (the audit links them to matches)
```

```bash
API_TENNIS_KEY=... tennisscout fetch-api-tennis   # ATP results after Jan 2026 + Challenger since 2019
```

```bash
tennisscout all -v         # ingest -> backtest + model selection -> audit (weather, markets) -> docs/*.md
```

```bash
tennisscout predict "Jannik Sinner" "Carlos Alcaraz" --surface Clay --best-of 5
```

```bash
ANTHROPIC_API_KEY=... tennisscout predict "Jannik Sinner" "Carlos Alcaraz" --surface Clay --report   # Claude writes the scouting report from the prediction (pip install -e '.[llm]'; ANTHROPIC_KEY also works)
```

```bash
API_TENNIS_KEY=... tennisscout upcoming --days 2   # scheduled ATP matches: served prediction + live Kalshi price
```

```bash
tennisscout card "Matteo Berrettini" "Ugo Humbert" --event "Paris Masters" --round R32   # HTML scouting card -> data/out/cards/
```

```bash
API_TENNIS_KEY=... tennisscout cards --days 1 --templates matchup,tiebreak   # a card for every scheduled ATP match + index.html
```

```bash
tennisscout serve          # FastAPI on :8000 — /predict, /card, /cards, /upcoming, /players/search, /backtest, /health
```

```bash
tennisscout verify-venues  # check metadata/venues.csv elevations and timezones against Open-Meteo
```

```bash
tennisscout archive-markets --compact --out archive/markets   # one compact ATP order-book snapshot
```

```bash
pytest                     # tests on synthetic fixtures; no real data or network needed
```

Ambiguous names are refused rather than guessed. `tennisscout predict Zverev Sinner` lists the
three Zverevs with their internal IDs, and you pass one (`P007638`) instead.

## Scouting cards

`tennisscout card` (and `GET /card`) renders one match as a self-contained, 1200 px wide HTML page
laid out like a shareable report graphic; `--png` also writes a screenshot (needs `pip install playwright`).
Three templates share one payload (`tennisscout card ... --json` prints it):

| Template | What it shows |
|---|---|
| `matchup` (default) | Style radars, archetype and tags, gravity, the showdown, two key questions, record against this style, the court, head to head, the blend |
| `tiebreak` | Three-set and tiebreak rates at this court speed, the tiebreak file (last five each), going the distance, court speed by edition, model totals |
| `aura` | Tale of the tape plus an editorial layer you write (`--notes notes.json`: `{"a": {"meter": 95, "meter_label": "FULL AURA", "tagline": "...", "receipts": ["**Bold lead.** detail", ...]}, "b": {..., "tone": "cringe"}, "read": "...", "kill": "..."}`); without notes the receipts come from the data. Labelled "Vibes-based. Not in the model." |

Where the numbers come from:

* **Win chance**: the served model, exactly as `/predict` returns it. Nothing on the card changes it.
* **Court speed** (`court_speed.py`): per tournament edition, serve points won and aces beyond what the
  same players manage elsewhere that season (player-season effects, ridge-shrunk toward the event's
  previous editions). 50 is the average tour event and 15 points is one standard deviation; this week's
  edition is used once it has results, else the event's last three editions.
* **Style** (`profiles.py`): serve and return ranks from the opponent-adjusted serve/return ratings;
  power, consistency, grind, variety, net, wings, rally length and first-serve outcomes from Match Charting
  Project stat lines (`charting_stats.py`) in the last three years, each shrunk toward the pool average.
  Ranks are percentiles among men with 5+ charted matches. Archetypes and tags are written-out rules on
  those ranks. Gravity is a two-way fit of short-point share (1–3 shots) on court speed and both players.
* **Expected wins** (form, against this style): the served model's stored pre-match predictions.
* **The Blend** is a description, not a validated model: the served chance plus a court tilt, from
  this court's share of short points and both men's short-point and rally win rates, through the Markov chain.

## Scheduled jobs (GitHub Actions, run from the default branch)

| Workflow | When | What |
|---|---|---|
| `ci.yml` | every push | `pytest` |
| `market-archive.yml` | every 15 min | compact Kalshi/Polymarket ATP order-book snapshot, committed to the `market-archive` branch |
| `nightly-refresh.yml` | 06:17 UTC | fetch -> ingest -> backtest -> report; commits `registry/` (new permanent IDs) and `docs/`. Set the `API_TENNIS_KEY` repository secret for 2026 results |

## Layout

```
src/tennisscout/
  sources.py          source registry (license, role) + snapshot manifests (revision, sha256, collected_at)
  registry.py         permanent internal IDs (append-only, committed under registry/)
  ingest_tml.py       TML-Database -> players, tournaments, editions, matches; identity + timestamp fields
  ingest_charting.py  Match Charting Project -> strict name resolution and match-level linking
  elo.py              overall + surface Elo, blend, best-of-5 conversion, ranking-range prior
  serve_return.py     opponent-adjusted serve/return ratings (logit scale, one-step Bayesian updates)
  markov.py           point -> game -> tiebreak -> set -> match, deciding-set rules, set-score distribution
  api_tennis.py       API-Tennis fetcher (raw monthly files); ingest_api_tennis.py maps it onto our IDs
  features.py         pre-match context features (age, rest, load, head-to-head, ...) in replay order
  ml_layer.py         optional LightGBM correction on top of the stacked model (chosen inside validation)
  market_eval.py      market-only vs model vs combined at fixed checkpoints; fee-aware trading check
  backtest.py         chronological replay, validation-only model selection, baselines, Elo/Markov blend
  metrics.py          Brier, log loss, accuracy, calibration, ECE
  audit.py            first-milestone data audit (live probes of weather/market hosts)
  predict.py, api.py  name resolution + prediction service (CLI and FastAPI)
  weather.py          Open-Meteo reanalysis + archived-forecast client
  markets.py          Kalshi/Polymarket listings, parsers, price history, raw order-book archiver
  report.py           renders docs/backtest.md and docs/data-audit.md
  court_speed.py      court speed index per tournament edition (serve points won and aces beyond the players' usual)
  charting_stats.py   Match Charting Project per-match stat tables -> per-player lines
  profiles.py         style ranks, archetypes, tags, gravity and phase rates for the cards
  matchup.py          everything one scouting card shows; card.py renders it (matchup, tiebreak, aura templates)
  schema.sql          SQLite DDL kept Postgres-compatible (Supabase later)
metadata/venues.csv        hand-written venue seed, versioned by year (UNVERIFIED, tagged as such)
metadata/player_aliases.csv  reviewed name -> ATP ID aliases (empty; audit lists candidates)
registry/                  internal ID registries: players, tournaments, editions, matches
.claude/skills/ponytail/   coding-style skill used in this repo (MIT, from DietrichGebert/ponytail)
data/                      (git-ignored) raw mirrors, SQLite DB, per-match audit output
```

## Data and licensing

| Source | Role | License |
|---|---|---|
| [TML-Database](https://github.com/Tennismylife/TML-Database) | Primary: ATP tour-level results 1968–Jan 2026, ATP IDs, serve stats, indoor flag | Personal / educational / non-commercial research only; **no redistribution of the raw data**; commercial use and monetized predictions need written permission |
| [Match Charting Project](https://github.com/JeffSackmann/tennis_MatchChartingProject) | Second source: exact dates, start times, point-by-point for a sample of matches | CC BY-NC-SA 4.0 (attribution, non-commercial) |
| [JeffSackmann/tennis_atp](https://github.com/JeffSackmann/tennis_atp) | The spec's original backbone | Returned 404 on 2026-10-29, which is why the pipeline has a second path |
| [Open-Meteo](https://open-meteo.com/) | Hourly reanalysis weather; archived forecasts for leak-free pre-match conditions | Free API for non-commercial use, data CC BY 4.0; daily request cap (shared egress IPs can hit it) |
| Kalshi / Polymarket public APIs | Match-market listings, 1-minute price history, live order books | Exchange API terms apply; raw responses stay in `data/`, compact ATP snapshots go to `archive/` |
| [API-Tennis](https://api-tennis.com/) | ATP results after the TML cutoff, Challenger since 2019, start times, serve stats | Paid API (key in `API_TENNIS_KEY`, never committed); raw responses stay in `data/` |

Raw data stays in `data/` (git-ignored) because of these terms. What is committed is our own
material: code, the ID registries (internal ID ↔ external ID only), curated metadata, and
aggregate reports. This is a research tool. Marketing its output as betting picks would bring
in sports-wagering advertising law (spec, "Legal and product caution").
