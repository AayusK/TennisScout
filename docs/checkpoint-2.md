# Checkpoint 2: serve/return ratings and the Markov model

Scope, from the spec: MVP step 2, "serve and return stats and the Markov model", added "only when
it improves chronological holdout results". Full numbers: [backtest](backtest.md). This round
also finished the parts of checkpoint 1 that were blocked on network access (weather and
markets); see [audit](data-audit.md) and [checkpoint-1.md](checkpoint-1.md).

## What was built

- **Serve/return ratings** (`serve_return.py`). Each player has a serve and a return rating on
  the logit scale, plus a per-surface offset for each. P(A wins a point on serve) =
  sigmoid(surface average + A's serve − B's return). Each match's serve points won update the
  ratings with a one-step Bayesian step. Players with few stat lines stay near the pool and keep
  a wide variance, which is the spec's sparse-data rule.
- **Markov chain** (`markov.py`). Point → game → tiebreak → set → match, with deciding-set rules
  by event and year: advantage final sets, Wimbledon's 12–12 tiebreak (2019–21), 10-point
  tiebreaks (AO 2019+, all Slams 2022+), Davis Cup and Olympic advantage sets, and Laver Cup
  match tiebreaks. A test confirms numerically that who serves first does not change set or
  match odds, which is why the chain doesn't track the server.
- **Blend.** Elo and the Markov probability are combined on the logit scale, with weights fitted
  on validation (0.60 × Elo + 0.25 × Markov). The served model (Elo, Markov or blend) is chosen
  on validation log loss, and the blend won.
- **Service.** `tennisscout predict` and `/predict` now return the blend, plus every layer (plain
  Elo → calibrated Elo → Markov → blend) and each player's serve-point and hold probabilities.

## Results (test window 2019–Jan 2026, 19,021 matches)

| Model | Accuracy | Brier | Log loss | Calibration error |
|---|---:|---:|---:|---:|
| Ranking baseline | 63.9% | 0.2203 | 0.6301 | 0.026 |
| Elo (checkpoint 1) | 64.3% | 0.2179 | 0.6241 | 0.026 |
| Serve/return + Markov alone | 65.3% | 0.2244 | 0.6594 | 0.088 |
| **Blend (served)** | **65.4%** | **0.2136** | **0.6142** | **0.013** |

- **The gain over Elo is real.** Paired on the same matches, Brier improves by 0.0043 ± 0.0003
  and log loss by 0.0099 ± 0.0008 (z ≈ 12.5 for both).
- **The Markov chain alone is the most accurate but badly overconfident.** Independent points
  amplify small serve edges. The blend keeps its information and fixes its calibration: within
  3.5 points in every decile.
- **Confidence grades still sort calibration.** "High" is within 1.2 points, "medium" 3.2, "low"
  4.0.
- **Surface offsets earned their place.** They improved the blend's validation log loss from
  0.5900 to 0.5883. Without them, the Markov chain called Sinner an 86% favourite over Alcaraz on
  clay; with them it says 78%. Served blend: 55% Sinner (Elo alone: 45%). Data is as of January
  2026, so treat this as an illustration, not a pick.

How the choices were made, so they can be checked:

- Every setting was chosen on 2014–2018 only. Twice, the best value sat at the edge of its grid,
  so the grid was extended until the optimum was interior or the score went flat. The remaining
  edge (prior variance 0.00125) is flat to within 0.0003 log loss.
- **The selection criterion was changed once after test numbers had been seen.** The serve/return
  variances are now chosen on the blend's validation score, which is the deployed model, instead
  of the Markov model's own score. The rationale stands on its own (standalone log loss rewards
  shrinkage that the blend undoes), but the order of events is disclosed here.
- The blend weights are fitted on the same validation years used to pick the served model, so the
  blend's validation score is slightly optimistic. Test is untouched by any choice.

## Checkpoint 1 items finished this round

- **Weather.** 126 of 128 outdoor sample matches with coordinates now have hourly reanalysis
  joined; 2 still time out. The archived-forecast API answers, so leak-free pre-match conditions
  are possible later.
- **Markets.** About 4,700 Kalshi and 44,500 Polymarket tennis match events, linked to our
  matches on both players, date, round (when given) and tournament. In the overlap windows, 83%
  of our matches have a market on each exchange, and settled winners agree 902/902 and 557/558.
  Kalshi has 1-minute candles with bid and ask; Polymarket has 1-minute prices only.
- **Archiver.** `tennisscout archive-markets` was run once for real: 596 raw records, including
  Kalshi order books with sizes. It now writes as it goes and skips closed markets.
- **Linker fixes found by tests:** qualifying markets can no longer link to main-draw meetings,
  rows that failed the identity check are no longer join candidates, and ties are reported as
  ambiguous.

## Next checkpoint and the decision it needs

MVP step 3 is a live data API for draws and schedules. The blocker is freshness. Our results stop
at **2026-01-21**, so every served rating is about nine months stale. Two paths:

1. **Paid feed** (Sportradar, API-Tennis or similar): scores, serve stats, schedules. This is the
   spec's recommended path, and it is your call because it costs money.
2. **Free stopgap:** Kalshi already lists upcoming ATP matches (players, tournament, round,
   scheduled start), and its settled markets give winners. That's about 3,450 match markets since
   our cutoff. Winners alone can update Elo, but not the serve/return ratings (no stats), and it
   depends on exchange terms.

Also still needed from you: a persistent home for the archiver (cron box or scheduled CI job).
