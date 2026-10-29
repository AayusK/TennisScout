# Checkpoint 1: data layer, surface Elo baseline, data audit

Scope, from the spec: MVP step 1 ("historical data loaded, plus surface Elo predictions and a
backtest") and the first milestone ("a 100 to 200 match audit of identity, timestamps, conditions,
and exchange history … plus a pre-match Elo baseline scored on data that was actually available
before each match"). The generated reports have the full numbers: [backtest](backtest.md),
[audit](data-audit.md).

## What was built

- **Own permanent IDs (architecture change 1).** Internal player (`P…`), tournament (`T…`),
  edition (`E…`) and match (`M…`) IDs live in append-only registries under `registry/`. ATP IDs
  and source keys are mappings onto them. Rebuilding the database from scratch reproduces the
  same IDs, and a test checks this.
- **Availability time (architecture change 2).** Every match row carries `event_date_lo/hi`
  with a precision flag, `published_at` (NULL, because the source has none), `collected_at`,
  `available_at` and `availability_basis`. Every row also traces back to a source snapshot
  (git revision plus a per-file SHA-256).
- **Identity checks.** A row is trusted only if its ATP ID exists *and* its display name agrees
  with that ID's player record. Rows that fail are kept but excluded from ratings and scoring.
  Names from failed rows never enter the lookup index.
- **Second source.** Match Charting Project charts are linked to our matches only when exactly
  one tour match fits both players, the round and the date window. Ambiguous names and fuzzy
  variants count only if that match-level join is unique, and homonyms playing each other link
  the match but leave the players unassigned.
- **Surface Elo and backtest.** The model has overall plus per-surface ratings, a blend, the
  Kovalchik K-factor, an optional best-of-5 conversion, an optional ranking-range prior for
  debutants, and a prediction-time scale. The backtest replays history chronologically and makes
  every model choice on 2014–2018 only; test scores cover 2019–Jan 2026.
- **Service.** `tennisscout predict A B` and FastAPI `/predict` take names or internal IDs,
  refuse ambiguous names, and report the data cut-off, the confidence and the caveats.
- **Written but not yet exercised** (hosts blocked here, see below): an Open-Meteo client
  (reanalysis for past matches, historical forecasts for backtests) and a raw Kalshi/Polymarket
  book archiver.

## Results

**Backtest (test window 2019–2026, 19,021 matches):**

| Model | Accuracy | Brier | Log loss | ECE |
|---|---:|---:|---:|---:|
| Ranking logistic (fitted on validation) | 63.9% | 0.2203 | 0.6301 | 0.026 |
| Elo 50/50 blend (FiveThirtyEight-style) | 64.2% | 0.2213 | 0.6339 | 0.054 |
| **Elo selected on validation** | **64.3%** | **0.2179** | **0.6241** | 0.026 |

- **What got in.** Validation adopted three changes: a ×1.3 prediction scale (the plain blend
  was overconfident), a best-of-5 conversion, and merging indoor hard into hard. The spec's
  separate indoor-hard rating did *not* earn its place.
- **What stayed out.** The 50/50 surface weight stayed; 0.25, 0.75 and the extremes were worse.
  The ranking-range debut prior was neutral and was left out.
- **A trap worth knowing.** The first version of that prior seeded debutants at the established
  pool's mean. Because Elo is zero-sum, that inflated the whole pool towards a rating of 3,000.
  It now uses an offset from the pool, and a test guards against the regression.
- **Calibration.** It holds within about 4 points in every decile; mild overconfidence remains
  at 0.8–0.9.
- **Confidence grades sort calibration, not accuracy.** "High" calibrates within 1.4 points.
  "Low" is 5.3 points off even though its accuracy is fine.
- **The tour got harder to predict.** Accuracy falls from about 68% (2014–17) to about 64%
  (2018 on), and the higher-ranked player's win rate falls in step. The drop is in the tour, not
  the pipeline.
- **No market comparison yet.** It needs the Kalshi/Polymarket data, which this environment
  cannot reach.

**Audit (150 completed singles, 2024–2025, stratified by level):**

| Supported from existing history | Not supported; needs a paid feed or forward collection |
|---|---|
| Both players via internal IDs: 150/150 | Match-level event time: 0/150 (TML dates the *tournament week*; median window 9 days) |
| ID and name agree: 150/150 | Publication timestamps: 0% anywhere in the source |
| Opponent, score, round: 150/150; serve stats 149/150 | Exact date: only via a linked chart, 24%; start time 11% |
| Pre-match ranks: 150/150 | Match-level roof / indoor status: 0% (tournament-level flag only, 96%) |
| Surface: 150/150 | Weather: 0/129 outdoor matches (Open-Meteo blocked here; coordinates ready for 128) |
| Altitude: 95% (unverified seed) | Polymarket/Kalshi history: not checkable here (hosts blocked) |
| Pre-match Elo prediction: 150/150 | Point-by-point: 24% (MCP sample) |

Population-wide findings:

- **Five TML rows with ID/name conflicts** (four distinct cases), the same class of error as
  the spec's Atmane/Altmaier example. For instance, a 2020 Sofia row files Marc-Andrea Huesler
  under John Millman's ATP ID, and a 1984 row files Lendl under Connors's. All are quarantined,
  along with 101 placeholder rows and 3 rows with missing or unknown IDs.
- **Homonyms.** 16 names that appear in matches belong to more than one ATP ID (e.g. John
  Harrison, Antonio Gonzalez). Lookups return every candidate.
- **Bad dates.** 18 TML editions have dates contradicted by exact-dated charts. Shanghai 2025 is
  dated a week early.
- **Cross-source linking.** 6,835 of 7,566 charts link, 2 stay ambiguous, and 280 linked charts
  disagree with TML about the surface (267 are "Hard" vs "Carpet" at old indoor events).
- **Stale data.** The TML GitHub mirror ends on **2026-01-21** (its live updates moved to a
  website with no API). Served ratings are about nine months stale and every prediction says so.

## What the audit decides

1. **Pre-match surface Elo:** supported. It is the trustworthy core, as the spec expected.
2. **Serve/return and the Markov model (checkpoint 2):** supported. TML has match-level serve
   stats for 99% of recent matches, and MCP gives point-level data for a sample.
3. **Fatigue and travel:** only approximately. "Days since last match" is known only to the
   week. Exact dates need a feed with match start times.
4. **Conditions:** weather joins are blocked here and city-level at best. Roof status per match
   does not exist in any source we have.
5. **In-play price paths:** not possible from this history (no point timestamps). The spec
   already rules these out.
6. **Markets:** unknown until the hosts are reachable. Archiving has to start forward anyway.
7. **Live predictions:** need a current results feed (Sportradar, API-Tennis or similar). The
   free mirror is stale, and neither source covers Challengers, which is also why the
   sparse-data path matters for players like the spec's Dietrich.

## Blocked, needs you

- **Network allowlist.** This environment's policy returns 403 for `archive-api.open-meteo.com`,
  `historical-forecast-api.open-meteo.com`, `api.elections.kalshi.com`,
  `gamma-api.polymarket.com` and `clob.polymarket.com`. Add them under the environment's
  Network access → Allowed domains (https://code.claude.com/docs/en/cloud-environments#network-access)
  so the weather joins, the market probe and the archiver can run. The archiver also needs
  somewhere persistent to run on a schedule; this container is ephemeral.
- **A live results feed decision** (paid) before the app can predict current matches.
- **Licensing.** TML forbids commercial use (including monetized predictions) and
  redistribution. MCP is CC BY-NC-SA. This stays a research tool unless that changes.
- **Venue seed review.** `metadata/venues.csv` was written from general knowledge, so its
  coordinates and elevations are city-level approximations tagged
  `manual_seed_unverified`. It needs checking before altitude is used as a feature.

## Next checkpoint

MVP step 2: per-player serve and return point-win rates from the TML stat lines, shrunk towards
priors by surface and ranking range, then a point → game → set → match Markov chain. It goes in
only if it beats this Elo baseline on the same chronological holdout.
