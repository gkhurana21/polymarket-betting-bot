# Pre-registration — LLM-with-retrieval forward forecasts (H005)

**Registered 2026-10-08, before any market in the primary set resolves.**
The git commit that adds this file is the timestamp. Nothing below may be
changed after the first primary-set market resolves (2026-10-10); any later
analysis is labelled exploratory.

Data: `data/llm_forward.db`, table `fwd`, 124 rows, all asked 2026-09-24
17:40–18:47 UTC by `research/llm_forward.py` (`gemini-3.1-pro-preview`,
Google Search grounding, temperature 0). Scorer: `research/score_forward.py`.

---

## Known defects, declared up front

These were found during review on 2026-10-08. They are not fixable for this
batch, so they are stated here instead of discovered in the result.

1. **The market price leaked through search.** 87 of 124 `key_fact` strings
   cite prediction-market or betting odds. On those rows the model mostly
   restated the market. The headline comparison therefore measures "market
   price plus LLM noise" more than independent forecasting.
2. **Grounding is not recorded per row.** `grounded` is 0 on all 124 rows
   (the canary fix landed after the run), and the canary itself is weak: the
   prompt contains `Today is {today}`, so a model can pass it by echoing the
   prompt. No row is excluded on grounding; all are scored.
3. **The selection cannot be reproduced.** The input `/tmp/near_markets.json`
   is gone. `p_market` is the stored `price` field from that file; its exact
   definition (mid, last trade, or Gamma outcome price) cannot be recovered.
4. **The outcomes are highly correlated.** 112 rows resolve on US midterm
   results, which share one national swing. The effective sample size is a
   handful of independent events, not 124.

Because of 1 and 4, the expected result is **"not demonstrated"**. The test is
still run because it is nearly free and the result is publishable either way.

---

## Population

* **Excluded — resolved before registration:** rows with `end_date` earlier
  than 2026-10-08. These are `601819` and `601826` (2026 Brazilian election).
  Reported separately, never in any test.
* **Primary set:** every other row (122).
* **Not scored:** rows that are unresolved or VOID at scoring time. They are
  listed by id in the report, with the reason.

## Resolution rule

From `GET https://gamma-api.polymarket.com/markets/{market_id}`:

* The returned `id` must equal the requested id, else the row is an error.
  (Gamma has silently ignored filters before; the id is checked, not trusted.)
* **Resolved** only if `closed` is true **and** `umaResolutionStatus` is
  `"resolved"`.
* Then, with `outcomes` = `["Yes","No"]`: YES if `outcomePrices` is
  `["1","0"]`, NO if `["0","1"]`. Any other final price (for example a 50/50)
  is **VOID**.
* Anything else is **unresolved**.

## Primary test

Per row, with `y` = 1 for YES and 0 for NO:

    d = (p_model − y)² − (p_market − y)²

`D` = mean of `d` over the primary set. **Negative `D` means the model beat
the market.**

**Inference: cluster bootstrap, 10,000 resamples, seed 20261008.** Clusters
are fixed now and resample whole:

| Cluster | Members |
|---|---|
| `midterms` | every question containing `House seat`, plus 562830, 562831, 562794, 634974, 634976, 634892, 562828, 634893, 907687, 635016, 634879, 635012, 634878, 907688, 907690 |
| `nobel` | 637031, 637022, 637025 |
| `israel_pm` | 682708, 682705, 682706 |
| `bernie` | 593972, 593974 |
| singletons | 776464, 668591 |

With six clusters, one of which holds 112 rows (97 House seats + 15 other midterm markets), this interval will be
wide. That is the honest width: the midterm rows are close to one bet on the
national environment.

## Pre-registered subgroup

Rows whose `key_fact` does **not** match, case-insensitively:

    prediction market|kalshi|polymarket|odds|betting

These are the forecasts that did not visibly read the market price (37 of
124 at registration). `D` is reported for this subgroup and its complement.

## Decision rule

**Retrieval edge demonstrated** only if both hold:

1. The cluster-bootstrap 95% CI for `D` on the primary set lies entirely
   below 0, **and**
2. `D` on the no-odds subgroup is below 0 (point estimate).

Otherwise the verdict is **not demonstrated**, and LLM-with-retrieval is
closed *in this form*. A rerun would need: no date in the prompt, a
verifiable canary (for example the previous day's index close), betting and
odds sources excluded, sources and canary stored per row, and at least 30
independent events.

## Reported but not decisive

* An iid bootstrap over rows (anti-conservative; shown only for contrast).
* Log-loss for model and market, with probabilities clipped to [0.01, 0.99].
* Brier for the model and the market on the same rows.
* The 6 rows resolving 2026-10-10 and 2026-10-27, reported on their own as
  well as inside the primary set.
