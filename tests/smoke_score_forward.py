"""Validate the forward-forecast scorer BEFORE any primary-set outcome exists.

The scorer is committed together with its pre-registration, so it has to be
right before it sees data: a resolver that misreads a NO as a YES, or a
bootstrap that silently treats 112 correlated midterm rows as independent,
would decide the verdict. Every rule is checked here against synthetic Gamma
payloads, and the cluster/subgroup assignments against the real database --
without fetching a single outcome.
"""

import os
import sys

sys.path.insert(0, "research")
from score_forward import (ODDS_RE, cluster_bootstrap, cluster_of,   # noqa: E402
                           iid_bootstrap, load, log_loss, paired_d,
                           parse_resolution, _closed_ms)

fails = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global fails
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{'  ' + detail if detail else ''}")
    if not ok:
        fails += 1


def gamma(id_="1", closed=True, uma="resolved", outcomes='["Yes", "No"]',
          prices='["1", "0"]'):
    return {"id": id_, "closed": closed, "umaResolutionStatus": uma,
            "outcomes": outcomes, "outcomePrices": prices}


def run() -> int:
    # ── resolution rule ──
    check("YES", parse_resolution(gamma(), "1")[0] == "YES")
    check("NO", parse_resolution(gamma(prices='["0", "1"]'), "1")[0] == "NO")
    check("outcome order reversed is read by label, not position",
          parse_resolution(gamma(outcomes='["No", "Yes"]', prices='["0", "1"]'), "1")[0] == "YES")
    check("50/50 is VOID", parse_resolution(gamma(prices='["0.5", "0.5"]'), "1")[0] == "VOID")
    check("open market is UNRESOLVED", parse_resolution(gamma(closed=False), "1")[0] == "UNRESOLVED")
    check("closed but only proposed is UNRESOLVED",
          parse_resolution(gamma(uma="proposed"), "1")[0] == "UNRESOLVED")
    check("id mismatch is ERROR, never trusted",
          parse_resolution(gamma(id_="2"), "1")[0] == "ERROR")
    check("garbage outcomes is ERROR",
          parse_resolution(gamma(outcomes="not json"), "1")[0] == "ERROR")
    check("Gamma '+00' timestamp parses",
          _closed_ms({"closedTime": "2026-08-08 20:27:18+00"}) == 1786220838000,
          str(_closed_ms({"closedTime": "2026-08-08 20:27:18+00"})))

    # ── statistics ──
    check("model closer to outcome gives negative d", paired_d(0.9, 0.6, 1.0) < 0)
    check("market closer gives positive d", paired_d(0.4, 0.6, 1.0) > 0)
    lo, hi = cluster_bootstrap([[-0.01, -0.01], [-0.01]], n=500)
    check("constant d gives a zero-width CI", abs(lo + 0.01) < 1e-12 and abs(hi + 0.01) < 1e-12)
    a = cluster_bootstrap([[0.1], [-0.2, 0.0], [0.05]], n=500)
    b = cluster_bootstrap([[0.1], [-0.2, 0.0], [0.05]], n=500)
    check("bootstrap is deterministic under the fixed seed", a == b)
    # One giant correlated cluster must widen the interval versus iid.
    big = [0.02] * 100
    ds = big + [-0.05, 0.01, -0.03, 0.04, -0.02]
    clo, chi = cluster_bootstrap([big, [-0.05], [0.01], [-0.03], [0.04], [-0.02]])
    ilo, ihi = iid_bootstrap(ds)
    check("cluster CI is wider than iid when one cluster dominates",
          (chi - clo) > (ihi - ilo), f"cluster {chi - clo:.4f} vs iid {ihi - ilo:.4f}")
    check("log-loss clips at 0.01", abs(log_loss(0.0, 1.0) - log_loss(0.01, 1.0)) < 1e-12)

    # ── the real file: every row assigned, counts match the pre-registration ──
    # data/ is not in the repo, so a fresh clone (and CI) skips this block.
    if os.path.exists("data/llm_forward.db"):
        rows = load("data/llm_forward.db")
        check("124 rows on file", len(rows) == 124, str(len(rows)))
        excluded = sorted(r["id"] for r in rows if r["excluded"])
        check("excluded set is exactly the two Brazil rows",
              excluded == ["601819", "601826"], str(excluded))
        sizes: dict[str, int] = {}
        for r in rows:
            sizes[r["cluster"]] = sizes.get(r["cluster"], 0) + 1
        check("midterms cluster holds 112 rows", sizes.get("midterms") == 112, str(sizes))
        check("six clusters in the primary set",
              len({r["cluster"] for r in rows if not r["excluded"]}) == 6)
        n_odds = sum(r["cites_odds"] for r in rows)
        check("87 rows cite odds, 37 do not", n_odds == 87, str(n_odds))
    else:
        print("  SKIP  the stored forecasts (data/llm_forward.db is local only)")
    check("odds regex is case-insensitive", bool(ODDS_RE.search("Per KALSHI pricing")))
    try:
        cluster_of("999", "Some new market")
        check("unknown id raises", False)
    except ValueError:
        check("unknown id raises", True)

    print(f"\n{'ALL PASS' if fails == 0 else str(fails) + ' FAILURES'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run())
