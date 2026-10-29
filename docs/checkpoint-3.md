# Checkpoint 3: fresh results, a live schedule, a context layer and the markets

Scope, from the spec: MVP step 3 ("a live data API for draws and schedules"), plus the parts of
step 5 (fatigue and travel features) and step 6 (market comparison) that the data now supports.
Generated detail: [backtest](backtest.md), [markets](markets.md), [audit](data-audit.md).

## What was built

- **API-Tennis ingest** (`ingest_api_tennis.py`). ATP results after TML's cutoff (2026-01-21)
  and Challenger and qualifying results since 2019: 61,433 matches, 13,613 with serve stats, all
  under our permanent IDs. Players map on name and birthdate first; anyone unmatched gets a new
  ID rather than a guess. Of the 217 ATP matches that overlap TML, 216 land on the same TML match.
- **Forward holdout.** 2,509 tour main-draw matches from 2026-01-22 on. No choice ever used
  them; they are the closest thing to live performance.
- **Challenger data decision.** Challenger and qualifying results update the ratings (never
  scored). Decided on 2021–22 log loss only:
  - Challenger matches at K weight 1.5, an interior optimum (0.5: 0.6007, 1.0: 0.5990,
    1.5: 0.5989, 2.0: 0.5998; tour only: 0.6059).
  - Qualifying matches at K weight 0.25 (1.0: 0.59886, 0.5: 0.59875, 0.25: 0.59871). That grid is
    flat, so the exact weight barely matters.
- **Elo:** margin of victory (games share, squared) adopted. Rejected: layoff boosts, a Grand Slam
  K multiplier, and down-weighting retirements.
- **Context layer** (`features.py`). A logistic layer over the Elo + serve/return blend, with
  features chosen greedily on validation. Selected: age gap, experience gap, rest days, shrunk
  head-to-head and 14-day load, plus terms that rescale confidence: less confidence when either
  player is new, more indoors and in best-of-five. Rejected: left-handers, home country, surface
  switch, time-zone travel, surface/level/round scaling, a retired-or-walkover flag, and residual
  form.
- **Walk-forward refit.** The layer's weights are refit each January on the previous two years.
  The 2-year window was chosen on 2016–18.
- **Optional LightGBM layer** (`ml_layer.py`). It is tried inside validation every run and
  currently rejected (inner gain 0.0002 ± 0.0006).
- **Markov scorelines.** Form on the day (a match-level random effect). Next Gen short sets. A
  full set-score distribution. Point probabilities are now anchored to the served win
  probability, so set scores, straight sets and tiebreak odds agree with the headline number.
- **Live schedule.** `tennisscout upcoming` and `GET /upcoming` list scheduled ATP matches from
  API-Tennis with the served prediction and, when Kalshi lists the match, its current price (mid,
  margin removed). Players who were never ingested are listed without a number. Today's Basel
  and Vienna fixtures (12 matches) all mapped to our players and to Kalshi markets.
- **Markets.** Kalshi and Polymarket prices at t−24h, t−1h and the last price before the
  scheduled start, compared with the model on the same matches (`tennisscout evaluate-markets`).
  Order books are archived every 15 minutes (`.github/workflows/market-archive.yml`).
- **Venue table check** (`tennisscout verify-venues`). Every row was checked against the
  Open-Meteo 90 m DEM and timezone. 127 of 128 agreed at first: Sardinia 2020 sat on an inland
  point and was moved to the coastal resort. All 128 agree now. This confirms altitude and region,
  not that each point is the exact venue.

## Results

Tour main draw. The served model is the walk-forward context layer.

| Model | Test 2019–Jan 2026 (n=18,937) log loss | Accuracy | Brier | ECE | Forward 2026 (n=2,509) log loss | Accuracy | Brier | ECE |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Ranking logistic | 0.6298 | 63.9% | 0.2202 | 0.025 | – | – | – | – |
| Elo, selected | 0.6121 | 65.6% | 0.2127 | 0.011 | 0.6073 | 66.1% | 0.2106 | 0.016 |
| Elo + serve/return blend | 0.6056 | 66.2% | 0.2099 | 0.005 | 0.6056 | 66.4% | 0.2097 | 0.019 |
| Context layer, fixed weights | 0.5994 | 66.8% | 0.2072 | 0.009 | 0.5990 | 67.0% | 0.2069 | 0.020 |
| **Context layer, walk-forward (served)** | **0.5997** | **66.7%** | **0.2074** | **0.006** | **0.5965** | **67.1%** | **0.2061** | **0.011** |

- **The context layer is the biggest gain this round.** Over the blend, it improves log loss
  by 0.0062 ± 0.0008 on test and 0.0066 ± 0.0024 on forward.
- **Walk-forward is neutral on test** (−0.0003 ± 0.0004) and helps on forward (+0.0025 ± 0.0008,
  z = 3.1). It was adopted on validation; the forward gain suggests 2026 drifted from the years
  the fixed weights were fitted on. Even so, forward favourites win slightly more often than
  predicted (0.7–0.8 bin: 79% won vs 74% predicted, about 2 se). The 2027 refit will absorb it;
  tuning on forward would spoil the holdout.
- **Checkpoint 2's served blend scored 0.6142 on test** (on a slightly different match set), so
  the served model has improved by about 0.0145 since then. Part of that is the Challenger data
  and the MOV change to Elo; part is the layer.
- **Scorelines.** Anchoring the chain to the served probability improves straight-sets log loss
  on test from 0.6569 to 0.6527 (forward: 0.6566). Straight sets are now slightly under
  predicted (0.588 vs 0.605 observed on test). The chain over-counts tiebreaks, so one factor
  (0.927), fitted on validation, calibrates the count: test 0.484 predicted vs 0.481 observed,
  forward 0.497 vs 0.507. The form width (sd 0.15) was chosen on validation by straight-sets log
  loss plus the tiebreak count's likelihood.

## Model vs markets (Aug 2025 – Oct 2026, our matches with a linked market)

| | n | Market log loss | Model log loss | Market better by |
|---|---:|---:|---:|---:|
| Kalshi, 1 h before | 2,190 | 0.6018 | 0.6050 | 0.0032 ± 0.0039 |
| Kalshi, last price | 2,202 | 0.5985 | 0.6048 | 0.0064 ± 0.0039 |
| Polymarket, 24 h before | 1,926 | 0.5952 | 0.5985 | 0.0032 ± 0.0040 |
| Polymarket, 1 h before | 2,760 | 0.6001 | 0.6076 | 0.0075 ± 0.0034 |
| Polymarket, last price | 2,764 | 0.5985 | 0.6072 | 0.0088 ± 0.0037 |

- **The markets are a little sharper than the model**, more so close to the start, when late
  news is priced in. Only Polymarket near the start is clearly better (about 0.008 ± 0.0035 log
  loss). Kalshi's lead (0.003–0.006 ± 0.004) is within noise.
- **The model still carries independent information.** In a market + model combination fitted
  on the earlier half, the model keeps a weight of 0.3–0.6. On Kalshi the combination beats the
  market alone on the later half at t−1h and at the last price (0.0027 ± 0.0017 and 0.0019 ±
  0.0014), but not at t−24h. On Polymarket it never does.
- **The combination is not served.** The gain is small and not established. Standard errors are
  in [markets.md](markets.md).
- **Trading check** (Kalshi ask + fee, 1 h before): +$14 ± $16 over 1,148 one-contract bets at a
  2-point edge, about +1.2% per bet. That's within noise, and historical depth isn't stored, so it
  is not evidence of an exploitable edge.

## Data problems found and fixed this round

- **Surface labels.** API-Tennis surfaces such as "Hard (Indoor)" were stored raw. About 9,200
  matches (mostly Challenger, plus about 300 ATP indoor matches in 2026) fell into an "Unknown"
  rating bucket. They now parse to surface + indoor.
- **Tournament metadata for 2026 events.** API-Tennis keeps one surface per tournament key
  (Stuttgart and Mallorca, grass for years, were "Clay"). A bare-name fallback hit old events
  (Miami 1978 indoor, London 2000 indoor, Madrid 1994 as a 250). Surface, level and indoor now
  come from the same event's TML editions in the three prior seasons. Fixing this moved the
  forward blend from 0.6072 to 0.6058.
- **Re-ingest** failed on players first minted by API-Tennis, because the TML step rebuilds the
  player table. It is idempotent now.

## How the choices were made, so they can be checked

- Every model choice is made on 2014–2018 (validation), or 2021–22 for the data decision. Test
  and forward are scored afterwards.
- **Disclosed:** the context-scaling terms were first tried in a scratch experiment that also
  printed test numbers. All eight went into the candidate pool, none were pruned by test, and
  validation picked three (two of which did not help on test).
- **Disclosed:** the LightGBM adoption rule was tightened after seeing that its first adoption
  lost on test and forward. Before, it needed a 1e-4 lower inner log loss; now it needs a gain
  above two paired standard errors. The rule stands on its own: the best of 18 settings wins a
  small margin by chance.
- **Checked and left out:**
  - Rest and load from week-start dates for API rows too (TML only has week dates): no
    measurable effect (+0.0001 ± 0.0004 on forward).
  - Residual form (recent results minus the blend's expectation): no gain on validation; streaks
    regress.
  - Monthly instead of yearly refits of the layer: +0.0005 ± 0.0003 on 2016–18, nothing on test,
    and not worth a monthly serving cadence.
  - Down-weighting retirements in Elo (half or zero weight): worse on validation at every step;
    a retirement still says something about the players.
  - A Grand Slam K multiplier (1.25, 1.5): worse on validation at every step.

## What needs you

- **Merge PR #3 to main** for the 15-minute market archiver to run on GitHub. Scheduled
  workflows only run from the default branch. Until then the archive grows only while this
  session runs.
- **API-Tennis is a free trial.** When it ends, results and the live schedule stop updating.
  The client reads `API_TENNIS_KEY` from the environment.
- Optional: installing the Claude GitHub App on the repo would let a session react to CI and
  review events on the PR.
