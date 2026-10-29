# Tennis Scouting Reports: Design and Feasibility

Enter two player names. The app pulls live data, builds a profile of each player that accounts for the surface and the conditions, runs a probability model, and writes a short scouting report. The report gives a win probability and explains why.

A useful research app is achievable, including player performance under different conditions and historical Polymarket and Kalshi prices. The hardest part is building a trustworthy, time-stamped dataset. The prediction model and the scouting-report interface are comparatively straightforward.

Whether the extra data improves predictions beyond market prices is something that has to be demonstrated through testing, not assumed.

## What the product does

1. Accept two player names (and, in practice, a specific match: tournament, round, and date).
2. Pull live and historical data for both players.
3. Build a profile of each player that accounts for surface and conditions.
4. Run a probability model.
5. Write a short scouting report: win probability, key matchup, X-factors, and confidence.

## Data layer

| Data | Possible sources | Practical approach | Main limitation |
| --- | --- | --- | --- |
| Historical match results and stats | Jeff Sackmann’s tennis_atp GitHub repo, with ATP, Challenger, and Futures matches back decades (licensed CC BY-NC-SA, so non-commercial use only) | Historical datasets plus a structured tennis API | Coverage varies by competition and season. A sole dependency on one repo is risky: the original JeffSackmann/tennis_atp URL returned 404 in a check. That does not establish why it is unavailable. |
| Point-by-point data | The Match Charting Project, from the same author | Same. The Match Charting Project remains accessible and explicitly carries a noncommercial, attribution/share-alike license. | Coverage is a sample of charted matches, not the full tour. |
| Opponents, results, scores, serve/return stats | Historical datasets plus a structured tennis API | Join on permanent internal IDs, not names | Coverage varies by competition and season |
| Live scores, draws, schedules, rankings | Sportradar, API-Tennis, or Goalserve (paid), or scraping ATP/ITF pages (check their terms of service) | Prefer APIs and downloadable datasets. Sportradar documents tennis timelines with point-by-point or game-by-game coverage depending on the event. Verify coverage for the tournaments you care about before choosing a provider. | Coverage and cost vary by event |
| News: injuries, withdrawals, coaching changes | A news API, plus an LLM that summarizes it | Store the raw item, the event time, the publication time, and the collection time | Late or wrong reports will leak into the model if timestamps are ignored |
| Conditions: indoor/outdoor, altitude, ball type | Tournament metadata | Maintain tournament and venue metadata, versioned by year | Roof status and court assignments can change within an event |
| Temperature, humidity, wind, rain | A weather API for outdoor events | Join venue coordinates and match times to historical weather. Open-Meteo provides historical weather reconstructed from observations and weather models. | Regional estimates do not capture exact conditions inside a stadium. Treat them as outdoor estimates, not courtside measurements. |
| Balls, court speed, interruptions | Tournament documents | Permitted scraping plus manual verification | Expect substantial historical gaps |
| Polymarket and Kalshi prices | Official market-data APIs | Archive live books and candles yourself; use historical endpoints for older Kalshi records | Availability, granularity, and liquidity vary. Charts do not reconstruct every quote that existed between trades. |

Prefer APIs and downloadable datasets wherever possible. Reserve scraping for specific missing fields where access and reuse are permitted. Check ATP and ITF terms of service before scraping their pages.

### One source issue

Do not depend on a single GitHub repo for the historical backbone. Mirror whatever you legally download, and keep a second path (paid feed or another licensed dataset) so a 404 or a license change does not stop the pipeline.

## Features to compute per player

- **Surface Elo.** A separate rating for hard, clay, grass, and indoor hard. This is the strongest single predictor.
- **Serve and return profile.** First-serve percentage, points won on first and second serve, return points won, break points saved and converted, ace rate, and double-fault rate.
- **Form.** Recent results weighted toward the latest matches, and the quality of opponents beaten.
- **Fatigue and travel.** Days since the last match, matches and sets played in the past 7 or 14 days, and time zones crossed. Example: Brooksby playing a final on Sunday and then opening in Sioux Falls on Tuesday.
- **Context.** Tournament level (Challenger vs ATP), handedness matchup (lefty vs righty), record in tiebreaks and deciding sets, and home crowd.
- **Head-to-head.** Use a light weight. Small samples are noisy.
- **Conditions, modeled as residuals.** Not raw “win percentage above 30°C.” See [Conditions](#conditions).

## Prediction engine

1. **Baseline.** Surface-weighted Elo gives a fast, well-calibrated starting probability.
2. **Point-level model.** Estimate each player’s chance of winning a point on serve against this specific opponent. A Markov chain then turns that into the chance of winning a game, a set, and the match. This also gives set scores and likely tiebreaks. Those score distributions are only as good as the serve-point estimates and the scoring rules you encode.
3. **ML layer.** Gradient boosting (XGBoost or LightGBM) trained on all the features above, predicting the error left over by the baseline. Add a component only when it improves chronological holdout results.
4. **LLM report.** An LLM gets the numbers plus the latest news and writes the scouting report: key matchup, X-factors, and confidence. The LLM explains verified model outputs and cited news. It does not invent probabilities. Uncertainty is stated explicitly.

Keep three model comparisons on the same matches and the same prediction timestamps:

- A tennis-only model.
- A market-only baseline (Polymarket and/or Kalshi).
- A combined model.

That is how you learn whether conditions and player features add information beyond the market. If you evaluate simulated trading, include executable prices, fees, spread, and available size. A mid-price that nobody could have hit is not a result.

## Two lessons from prior research

- **Entity resolution is the trap.** One stats site said Atmane led Taro Daniel 4-0, but it had mixed him up with Daniel Altmaier. Key everything on ATP player IDs, never on names. Use your own permanent player and match IDs. Map ATP, ITF, Sackmann, and provider identifiers onto them. Do not assume different datasets share an ID system. Match markets using both players, tournament, round, date, and market type. Leave ambiguous matches unresolved.
- **Challenger and college data is thin.** A player like Dietrich has very few tour-level matches. The model needs to fall back on prior estimates by ranking range, and it should show lower confidence when data is sparse.

## Measuring whether it works

Track Brier score, log loss, and a calibration curve. If the model says 70%, those players should win about 70% of the time.

Backtest by replaying past seasons, using only the data that was available before each match. That rule covers rankings, injuries, Elo, and weather forecasts. Using the weather that eventually occurred for “tomorrow’s” match gives the backtest information the real app would not have had. Use archived forecast runs for pre-match condition features. Open-Meteo forecast archives are one way to do that.

Preserve three timestamps on every fact:

- Event timestamp (when it happened).
- Publication timestamp, where the source has one.
- Collection timestamp (when you stored it).

This matters for scores, injury news, ranking updates, forecasts, and delayed feeds.

## Polymarket and Kalshi

Historical odds are available, but “historical odds” needs a precise definition. A single closing number is not the same as the book at first serve, and a 12-hour candle is not the price when a player was down a set.

### Kalshi

Kalshi documents historical markets, trades, and price candles. Older records move to separate historical endpoints, so querying only the live API will miss them. Candle responses include bid, ask, traded-price summaries, volume, and open interest.

### Polymarket

Polymarket documents historical outcome prices. A current limitation: price-history documentation says full history defaults to 12-hour buckets, while finer resolutions have a restricted historical window. That can be too coarse to reconstruct a short tennis match.

Polymarket provides live book and price updates that you can record going forward. Price charts and trade records do not necessarily reconstruct all the quotes and available quantities that existed between trades. Start archiving live data early.

### What to store for each market

- The exact match, selected player, market type, and settlement rules.
- Time-stamped bids, asks, last trades, and available size.
- Prices at defined checkpoints: 24 hours before, one hour before, and the last valid observation before first serve.
- The final match result, stored separately from the market’s settlement outcome.

### Score-conditional prices

“What was the price when this player was down a set and serving at 3–4?” requires score, server, and point timestamps joined to market observations. Final scores alone cannot answer that. Both feeds need timestamps precise enough to join. If either side is only match-level, do not pretend you have an in-play price path.

## Conditions

You can enrich historical matches with hourly temperature, humidity, wind, and precipitation using location and time. Treat reconstructed weather as an estimate of outdoor conditions, not a courtside measurement.

The tempting feature is “win percentage above 30°C.” The better question is:

> Did this player perform better or worse than expected in hot conditions, after accounting for opponent strength, surface, and tournament level?

Otherwise a player who faced weaker opponents in hot tournaments looks artificially heat-adapted.

Estimate condition effects across the whole player population, then allow individual adjustments only where the evidence supports them. Players with few relevant matches should stay close to the population estimate. Treat temperature and humidity as continuous variables rather than splitting limited data into dozens of tiny categories.

Distinguish two uses:

- **Past-match conditions.** Reconstructed weather can describe previous performances.
- **Conditions for a prediction.** A historical backtest must use the forecast that was available when the prediction would have been made, not the weather that later occurred.

Also keep surface, indoor/outdoor, and altitude as versioned tournament and venue metadata. Roof status and court assignment can change inside a single event, so a tournament-level “outdoor” flag is sometimes wrong for a specific match.

## Architecture changes

Three changes to the original stack, before any model work that depends on joined data.

1. **Own permanent IDs.** Internal player IDs and match IDs. External IDs (ATP, ITF, Sackmann, Sportradar, Polymarket, Kalshi) are mappings, not keys. Ambiguous joins stay unresolved and out of training rows.
2. **Availability time.** Every row that can affect a prediction stores event time, publication time when known, and collection time. Training and backtests filter on “known before the prediction timestamp.”
3. **Separate model comparisons.** Tennis-only, market-only, and combined, scored on the same matches and the same timestamps. Do not ship a combined model because it looks richer. Ship it if Brier score and log loss improve out of sample, and if calibration still holds.

The Elo, then serve/return, then Markov sequence stays. Add each piece only when chronological holdouts get better. The LLM sits on top of verified numbers and cited news. It does not fill gaps in the data layer.

## Stack

- Python ingestion jobs on a schedule, into Postgres or Supabase.
- A FastAPI model service.
- A Next.js front end with player cards, a probability bar, and the report.
- A nightly job to refresh rankings and Elo.
- A separate archive job for Polymarket and Kalshi books and candles, started as early as possible.
- Weather joins (historical reconstructions for past matches, archived forecasts for backtests and live predictions).

## MVP order

1. Sackmann-style historical data loaded, plus surface Elo predictions and a backtest. In parallel, the data audit below.
2. Serve and return stats and the Markov model.
3. A live data API for draws and schedules.
4. The LLM report.
5. Fatigue, travel, and news features.
6. Condition residuals and market features, only after the audit says the joins are trustworthy, and only if they beat the baselines.

Do not start with the LLM report. A confident paragraph on bad joins is worse than no report.

## First milestone: data audit

Before building the richer app, run a data audit on 100 to 200 completed singles matches.

For each match, try to link:

- Both players, via internal IDs, not names.
- Opponent, score, and round.
- Surface, indoor/outdoor, altitude.
- Reconstructed outdoor conditions where the event was outdoor.
- Whatever Polymarket and Kalshi history actually exists: resolution of the price series, whether a pre-first-serve checkpoint exists, and whether score timestamps exist for an in-play join.

Measure:

- Missing timestamps.
- Ambiguous identities (same display name, different player; same player, different source IDs).
- Markets that cannot be matched on both players, tournament, round, date, and market type.
- Price series that are too coarse for a tennis match (for example, 12-hour buckets).
- Condition fields that are missing or only tournament-level.

In parallel, start recording new market observations and new match timelines. Historical APIs will not reconstruct quotes and sizes that were never stored.

The audit decides which parts of the richer app you can support from existing history, and which parts need a paid feed or data you can only collect from now on.

## What you can and cannot claim

| Question the app might be asked | Needs | If you do not have it |
| --- | --- | --- |
| Who is favored on this surface before the match? | Surface Elo, rankings, form, a prediction timestamp | Do not quote a probability |
| How does each player serve and return against this opponent? | Serve and return rates with a fallback prior when samples are small | Show the prior and a lower confidence |
| Win probability and a short explanation | Calibrated model output plus cited news | The LLM must not invent the number |
| How did this player do in heat, wind, or altitude? | Condition residual after opponent, surface, and level | Do not report a raw hot-weather win percentage |
| What were the odds yesterday? | A defined checkpoint (24h, 1h, or last price before first serve) with bid, ask, and size | Do not call a daily candle “the odds” |
| What were the odds when he was down a set? | Point or game timeline joined to a fine-grained book | Say the join is not available |
| Would this have made money? | Executable prices, fees, spread, and size, known before the bet | A mid-price backtest is not a trading result |

## Legal and product caution

If you ever market this as betting picks, sports-wagering advertising laws apply.

Sackmann data, and the Match Charting Project, are noncommercial (CC BY-NC-SA, with attribution and share-alike). They cannot be used in a commercial product. A research or personal tool and a marketed betting product are not the same thing. Confirm the license on whatever snapshot you actually download before you ship anything someone pays for.

Scraping ATP or ITF pages is only acceptable where their terms allow it. Paid feeds (Sportradar and similar) exist specifically because public pages are a weak foundation for a live product.

## Sparse-data rule

When a player has few tour-level matches, or few matches on the surface, or few matches in the relevant conditions:

- Fall back to a prior estimated by ranking range (and by surface, when that prior exists).
- Shrink individual condition adjustments toward the population effect.
- Show lower confidence in the report.
- Keep head-to-head weight light. A 4–0 record on a mis-identified player is worse than ignoring head-to-head entirely.

## Report contents

The scouting report should include:

- Win probability for the match being asked about, with the prediction time.
- The baseline (surface Elo) probability and what the later layers changed.
- Key matchup: serve and return edges that actually move the Markov model.
- X-factors that are in the data: fatigue and travel, surface switch, handedness, indoor/outdoor, and any condition residual strong enough to leave the prior.
- News that was published before the prediction time, cited, not paraphrased into a new fact.
- Confidence, lower when identity was ambiguous, samples are small, or market history is coarse.
- If markets are included: the checkpoint price, not a vague “the market.”

## Out of scope until the audit passes

- Marketing the output as picks.
- In-play price paths without point timestamps.
- Fine-grained “this player in humidity above X on clay after a long flight” slices. Those cells will be almost empty.
- Training on any row whose facts were not knowable at the prediction timestamp.
- Using a name match as a join.

## Summary

Yes, this is feasible as a research app. Surface Elo plus a serve-point Markov model is a sound core. Conditions and prediction-market history are feasible only as time-stamped joins with honest gaps. The first deliverable is not the report. It is a 100 to 200 match audit of identity, timestamps, conditions, and exchange history, plus a live archive started now, plus a pre-match Elo baseline scored on data that was actually available before each match.
