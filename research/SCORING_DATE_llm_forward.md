# Scoring date for the forward-forecast test (added 2026-11-12, before any score was computed)

PREREG_llm_forward.md fixes the rules but not *when* to score. Fixed now, before running `--score`:

* **Final scoring: on or after 2026-11-21**, the day after the last listed end date (668591, 2026-11-20).
  Run `--resolve` then `--score` once; the result is final.
* At 2026-11-12, 112 of 122 primary rows have resolved. The 10 still unresolved are listed below. Anything still
  unresolved on 2026-11-21 is "not scored" and listed by id in the report, as the prereg says.
* No interim score is computed or looked at before then.

Unresolved on 2026-11-12: 634974, 634976 (Alaska Senate, ranked-choice count), 635016, 635012 (Alaska governor),
907687 (governorship count), 682708, 682705, 682706 (next Israeli PM), 776464 (S&P 500 company buys Bitcoin by
2026-12-31), 668591 (GTA 6 delay, ends 2026-11-20).
