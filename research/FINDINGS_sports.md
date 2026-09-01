# Findings — Sports Modelling (hypothesis 9)

**Status: the cheap versions are REJECTED. A properly powered model test is
BLOCKED on data availability, not closed.**

Run 2026-09-01. Real money risked: **$0**.

---

## Why sports was worth trying

Across eight hypotheses the recurring conclusion was that beating this venue
needs *better information*. That was then tested with the weakest possible
version — a general-purpose LLM carrying a January 2025 cutoff, forecasting
2026 events. It lost to the market by 14.9%, which says little about domain
models.

Sports is the one domain where genuine forecasting edge is buildable from
public data rather than requiring insider position: goal models are published,
historical results are free, and the corpus holds **1,029 resolved match
markets** — testable offline with no waiting.

## Test 1 — the draw bias. Rejected, n=298.

The famous anomaly: draws are underpriced because bettors prefer backing a team
to win. At executable ask prices:

| market type | n | mean ask | realised | edge | ROI |
|---|---|---|---|---|---|
| draw markets | 298 | 0.291 | 0.285 | −0.006 | −5.50% |
| both-teams-to-score | 130 | 0.492 | 0.485 | −0.007 | −4.82% |
| everything else | 1,923 | 0.235 | 0.225 | −0.010 | −5.69% |

All three are the same number. That is not three findings, it is the
half-spread paid for crossing, appearing uniformly. Sports markets are
calibrated exactly like every other market type.

Chronological split confirms noise: **+1.88% in-sample → −12.88% out-of-sample.**

## Test 2 — a Poisson goal model. Loses, n=53.

Time-weighted attack/defence ratings fit on 49,547 international results, with
strict no-look-ahead (ratings for a fixture on date D use only matches before
D).

| forecaster | Brier |
|---|---|
| always base rate | 0.1531 |
| **market mid** | **0.1497** |
| Poisson model | 0.1576 |

Model loses by 5.3%. Betting thresholds gave t = −0.23, +0.04, +0.30 on 28, 19
and 12 bets — noise.

**The interesting number is the market's own skill: 2.2% over base rate**,
against 29.6% on the full corpus. *Nobody* predicts draws, including the
market. Little information is priced in, but there is also little information
to extract — which is precisely why a goal model does not help.

## Test 3 — Polymarket vs the closing bookmaker line. Agrees, n=32.

A stronger question than "can my model win": does Polymarket agree with the
sharpest public estimate? Closing market-average odds, de-vigged.

```
bookmaker close minus Polymarket:  mean +1.52c  median +0.61c  stdev 5.40c
Polymarket mid   Brier 0.1404
bookmaker close  Brier 0.1430
```

They agree to within 0.61c median. No systematic gap. **n=32 is far too small
to be conclusive and is reported as a direction, not a verdict.**

## What blocks a real test

Sample size, caused by data availability:

| | |
|---|---|
| draw markets parsed | 507 |
| international (both teams in free dataset) | 94 → 53 usable |
| club fixtures matched to free league data | 54 → 32 with odds |
| **club markets with NO free data** | **~360** |

The unmatched names — Londrina, AC Goianiense, Grêmio Novorizontino, Operário
Ferroviário, Vila Nova, CR Brasil — are **Brazilian Série B**. football-data's
`BRA.csv` covers Série A only, and free sources do not reach lower divisions,
Moroccan, Maltese or second-tier Chinese football, which is what these markets
mostly are.

A properly powered test needs paid lower-division data. That is a purchase
decision, not a research one.

## Honest verdict

Three independent tests point the same way and none is individually
conclusive. The systematic mispricing (test 1) is dead at n=298, which IS well
powered. The model tests are underpowered and merely fail to contradict it.

**This is the first hypothesis blocked on data rather than closed by
measurement.** The prior remains poor for the reason stated before any of it
ran: Polymarket football prices track sportsbook odds, and sportsbook football
is among the most efficient betting markets that exists.

## Guard worth keeping

The fixture join verifies that the matched row's actual result agrees with how
the market resolved, and discards it otherwise. A name+date match that
disagrees on the outcome is the wrong fixture, and silently accepting it would
attach another match's odds — the same undetectable poisoning the Gamma
`condition_id` bug would have caused. It rejected 0 of 32 here, but the check
is what makes that zero meaningful.
