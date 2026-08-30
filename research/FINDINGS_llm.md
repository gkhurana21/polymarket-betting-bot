# Findings — LLM Forecasting and Pair Adjudication

**Status: forecasting REJECTED. Adjudication VALIDATED and kept.**

Run 2026-08-30 with `gemini-3.1-pro-preview`. Real money risked: **$0**.

---

## 1. Forecasting: the model loses to the market

The bar was measured offline first, because accuracy is not edge — a model
that is 75% accurate on a corpus where NO wins 75% of the time has learned
nothing. Over 2,379 resolved markets the market mid price scores Brier
**0.1313**, against 0.1865 for always guessing the base rate.

Result on 220 markets scored against the market on the SAME items:

```
model  0.1571
market 0.1368
-> no edge (skill vs market -14.9%)

month    n    model   market   self-reported-known
2026-06  83  0.1408  0.1084    0.0%
2026-07  94  0.1548  0.1430    0.0%
2026-08  43  0.1935  0.1780    0.0%
```

The model loses in every month. The contamination check is clean: no
memorised cohort, no step change, zero self-report. This is a genuine
forecaster being beaten, not a memoriser being caught.

**Why it loses is not a mystery.** The model's stated training cutoff is
January 2025; these markets resolved May-August 2026. It forecasts 16-month-
old news against a market holding current information. A negative result here
rejects *LLM forecasting without retrieval* — it does not reject the idea that
an LLM with live search could do better. That was not testable here: we cannot
retroactively hand a model 2026 news.

Both model and market degrade over the corpus (market 0.108 -> 0.178), so
later markets are simply harder. That is why scoring is always against the
market on identical items, never against the global 0.1313.

## 2. Adjudication: works, and corrected a live conclusion

Judging whether two markets resolve identically is pure text comprehension. It
needs no current information, so the January 2025 cutoff costs nothing — unlike
forecasting.

Validated FIRST on 8 labelled pairs (`crossvenue/adjudicate.py`): **zero false
SAME**, the direction that loses money. It rejected the original false positive
at confidence 1.00. Its one "error" was better than the label: it refused
"Starship successful launch" as a match because *successful* is subjective and
may resolve differently across venues — which is exactly how a cross-venue
hedge silently fails.

Applied to the 27 token-matched cross-venue candidates:

| | |
|---|---|
| token matcher accepted | 27 |
| semantically confirmed | 14 |
| **rejected as different questions** | **8 (30%)** |
| positive-spread pairs before judging | 14 |
| **positive-spread pairs surviving** | **7** |

**Spread size is inversely related to match validity.** The largest apparent
arbitrages were the worst mismatches:

| gross | pairing |
|---|---|
| +34.0c | "R Senate, R House" vs "D-House, D-Senate" — opposite outcomes |
| +10.0c | "R Senate, D House" vs "D-House, D-Senate" — different Senate |
| +4.7c | "**US** recognize Palestine" vs "**Italy** recognize Palestine" |

A keyword `PREDICATE` filter cannot catch these: "R Senate" and "D Senate"
share every token. Only reading the questions works.

After fees only **4 of 7** remain positive; three go negative because fees peak
at mid price. Best surviving is +3.16c on capital of $1.041 — **3.04% on
capital, ~9.2% annualised at a 120-day hold**, 4.6% at 240 days. Capital is
BOTH legs (buy YES one venue, NO the other, ~$1 total), not one.

The cross-venue verdict is unchanged but better understood: not merely thin
margins, but **half the screen was noise that a token matcher structurally
cannot filter.**

---

## Bugs caught

**Gamma silently ignores condition-id filtering.** `?condition_ids=` returns
`[]`; `?condition_id=` returns an unrelated market rather than erroring — a
request for `0x001649...` came back as `0xa467b1...`. Taking `[0]` would have
attached plausible questions to the wrong outcomes across all 4,551 markets,
undetectably. Backfill now uses CLOB's by-id endpoint and **verifies the
returned id**; the completed run reports `mismatch=0` over 4,087 markets.

**A write transaction held across network I/O.** The backfill committed every
100 rows, holding a write lock for ~60s while making 100 HTTP calls, which
starved the forecast runner regardless of its busy_timeout. Killed the pilot
twice before the cause was clear. Both loops now commit per row.

**Detector threshold conflated excellence with memorisation.** A single
omniscience cut at Brier 0.05 flagged a synthetic honest forecaster scoring
0.0406 — it would have discarded exactly the result we were hunting. Split
into hard (0.02) and warning (0.06) tiers. It cannot simply be removed in
favour of the step-change test: a model whose cutoff falls after the corpus
knows every month and produces no step.

**Live CLOB books are dicts, stored snapshots are arrays.** `/book` returns
`{"price","size"}`; our `book_json` uses `[price, size]`. The scanner assumed
the stored form and crashed on live data.
