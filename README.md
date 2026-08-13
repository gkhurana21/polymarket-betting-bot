# Friday — Polymarket liquidity-rewards market maker

Friday earns Polymarket's **maker liquidity rewards** by resting two-sided
quotes inside each market's reward band. It does not predict outcomes and it
does not arbitrage.

## Why this shape

The original brief was a high-frequency YES/NO arbitrage engine capturing 2–3%
spreads. **That was measured and the premise is false.** Across 116 liquid
markets, every one quoted `ask(YES)+ask(NO) = 1.0010` and `bid+bid = 0.9990` —
exactly one `minimum_tick_size` (0.001) around parity. Buying both sides loses
0.10%; selling both loses 0.10%. Zero markets had arb. Multi-outcome groups hit
the same wall once complete sets were compared.

What does pay is the other side of that quote: Polymarket advertises a
`rewards` block on every market (60/60 sampled), paying **$1–116 USDC/day** to
makers resting ≥ `min_size` (200 shares) within `max_spread` (3.5–5.5¢) of mid.
The entity holding that one-tick quote is not arbitraging — it is being paid to
be the market maker. Friday does that instead.

## The two rules that matter most

**1. Rank by reward efficiency, not pool size.**
Rewards split pro-rata, so your share is `your_size / total_qualifying_size`.
The pool is the numerator and *resting depth inside the band* is the
denominator. Ranking by pool alone walks into the most crowded books.

**2. If the volatility floor exceeds the reward band, do not quote.**
The vol floor says "quote this wide or get run over"; the band says "quote
wider than this and earn nothing". When they conflict the market is too
volatile to earn in safely, and Friday stands down. Quoting inside the band
anyway — standing at a spread the volatility does not support for a few dollars
a day — is how a market maker dies.

A third, quieter rule: **reward efficiency and safety are opposed.** The
thinnest books pay the most per share precisely because nobody else will stand
there. A live scan found a $2/day market with 22 shares of resting depth;
posting 400 shares there makes you 95% of the book, so every fill is someone
hitting the only bid available with no liquidity to exit against. `max_our_book_share`
and `min_depth_shares` exist to refuse those.

## Economics, honestly

Quoting both sides at `min_size` costs `200×p + 200×(1−p) ≈ $200` per market,
so **$500 funds two markets**. Measured live, the selected pair implies roughly
**$2–4/day gross**. That is gross. One adverse fill of 5¢ on 200 shares costs
$10 — several days of earnings. **Net profitability is decided by the toxicity
and inventory controls, not by reward capture**, which is why they ship in v1.

## Usage

```bash
.venv/bin/python main.py scan              # rank markets, show selection
.venv/bin/python main.py quote             # one cycle, show what would rest
.venv/bin/python main.py serve             # continuous loop
.venv/bin/python main.py report --days 5   # NET pnl post-mortem
```

`dry_run: true` in `config.yaml` is the default and the only correct value
until the paper window has run. Live quoting additionally requires
`PRIVATE_KEY`; `main.py` refuses to start without it.

**Dry-run scope, stated plainly:** the paper phase measures quoting behaviour,
reward eligibility, and whether the band-vs-vol rule leaves anything quotable.
It does **not** produce fill P&L. Simulating passive fills would reintroduce
exactly the optimistic-fill error that invalidated the predecessor project
(a measured +21.8% became −18.1% once real obtainable prices were used).

## Capital discipline

1. Paper first.
2. **$500 live**, 2 markets at `min_size` both sides.
3. Scale to $1,000 only after **two weeks of positive net PnL after adverse
   selection** — never on gross reward income.
4. **Hard stop** if inventory losses exceed reward earnings in any rolling
   5-day window (enforced in `guard.py`, not left to discipline).
5. Above $1,000 is off the table until the economics are proven on real fills.

## Provenance

- `marketmaker.py` ports `~/trading/mm-harness/bot.py` — mark estimation,
  toxicity guard, inventory skew, markout, vol floor. Those were measured
  against a mock exchange, not guessed.
- `client.py` and the `OrderArgs` correctness rules come from
  `~/polymarket betting bot` (see its `FINDINGS.md`): side is BUY/SELL never
  YES/NO, size is shares never USD, and there is no `order_type` kwarg.

## Tests

```bash
for f in tests/test_*.py; do .venv/bin/python "$f"; done
```

Covers the band-refusal rule, one-sided-book mark safety, toxicity response,
sub-`min_size` pull, inventory limits, kill-switch latching, atomic-replace
no-ops, and that the post-mortem headline is NET rather than gross.
