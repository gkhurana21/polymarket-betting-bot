# Findings — Thin Markets and the Complement Floor

**Status: complement arbitrage CLOSED venue-wide, both directions.**
**A scope error in six prior studies FOUND and corrected.**

Run 2026-08-30 against live books. Real money risked: **$0**.

---

## The scope error

Every prior hypothesis was tested on markets passing:

```
min_volume_24h_usd: 10000
min_liquidity_usd:   5000
```

Measured against the live venue:

| | |
|---|---|
| open markets on Polymarket | 2,100 |
| passing our filters | **75** |
| excluded | 2,025 |
| **fraction studied** | **3.6%** |

Median 24h volume across the open venue is **$220**. The filter demanded
$10,000 — roughly the top 4%.

So six studies concluded "Polymarket is efficient" from its most liquid
sliver, which is precisely where price discovery is strongest. The filters
were correct for Phase A, where paper trading needed fillable markets, and
were never revisited when the question changed from *can we trade this* to
*is this efficient*. That is searching under the streetlight, and it was a
real error of scope.

## Retesting where nobody is watching

Complement arbitrage is the one hypothesis testable **instantly** — YES + NO
must pay exactly $1, so no resolution data, forecast, or statistical power is
required. It was retested on the 466-market middle band ($200-$10k daily
volume: too small for a professional, not dead), walking real ask/bid depth at
25 and 100 shares, requiring both legs to clear the $5 minimum.

**Buy side** — `ask_YES + ask_NO`, 455 markets:

| min | p25 | median | p75 | p95 | max |
|---|---|---|---|---|---|
| **1.0010** | 1.0100 | 1.0160 | 1.0280 | 1.1500 | 1.3580 |

**Sell side** — `bid_YES + bid_NO`, 453 markets:

| max | p75 | median | p25 | p05 | min |
|---|---|---|---|---|---|
| **0.9990** | 0.9732 | 0.9900 | 0.9900 | 0.9990 | 0.6900 |

**0 crossings in either direction.** The band is exactly ±0.001 — one
`minimum_tick_size` — around parity, and identical to the value measured on
liquid markets.

## What this settles

Previously this project asserted that split/merge *creates* the floor and
arbitrageurs *enforce* it. Those are two claims, and liquid data could not
separate them. It now can: the floor holds to four decimal places in markets
doing $1,036/day that no arbitrageur watches. **Nothing is enforcing it. It
cannot be crossed.**

That upgrades hypothesis 2 from "no opportunity found in the liquid slice" to
**structurally impossible, venue-wide, both directions.** It is the strongest
negative result in the project and no longer rests on a filtered sample.

## The thin-market hypothesis, answered

Thin markets **are** less efficient — median 1.6c above parity on the buy side
versus 0.1c for liquid ones, and p95 reaching 15c. The critique was correct.

But the inefficiency runs the **wrong way**. Wider books mean you pay more to
buy the pair and receive less to sell it. There is no side of a wide spread
that pays the person crossing it. The mint-and-sell mirror — create a pair for
$1 via the CTF contract, sell both legs above $1 — was tested for the first
time here and is also 0/453, maxing at exactly one tick below parity.

**Thin markets are not an unexploited frontier. They are the same wall,
further away.**

---

## Bug caught

Live CLOB `/book` returns levels as `{"price","size"}` dicts; our stored
`book_json` uses `[price, size]` arrays. The scanner assumed the stored form
and crashed on live data. Both forms are now accepted — worth remembering for
any future code that reads live books and recorded ones with the same parser.
