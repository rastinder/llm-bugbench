"""Pre-registered statistics for the execution panel.

Design decisions here are fixed in advance, because choosing the analysis after seeing the
numbers is how a benchmark manufactures its result. Specifically:

  PRIMARY   paired permutation test on per-bug score differences, permuting *within bug*.
            All models see identical bugs, so pairing removes the bug, file and codebase
            main effects outright; what remains is model x bug interaction. A paired
            t/Wilcoxon is the wrong tool because per-bug partial credit produces heavy
            ties that signed-rank handles badly, while a permutation test makes no
            distributional assumption at all.

  MULTIPLICITY  Holm correction across the 10 pairwise comparisons of a 5-model cohort.
            Uncorrected win rates inflate with the number of comparisons.

  EFFECT    Cliff's delta, which is interpretable at a glance and needs no significance
            claim to be useful.

  SENSITIVITY  cluster bootstrap over codebooks, reported beside the primary. When the two
            disagree the pre-registered rule is to trust the paired result and report the
            discrepancy, not to switch to whichever is prettier.

  VERDICTS   TIED and INCONCLUSIVE are different answers. An underpowered comparison that
            cannot bound the gap must not be reported as a tie, because "they are equal"
            is a much stronger and more actionable claim than "we could not tell".
            Equivalence testing against a smallest-effect-of-interest delta0 is what
            separates them: TIED means the gap is bounded below delta0, INCONCLUSIVE means
            it could not be bounded.

Everything is implemented here rather than pulled from scipy, because the exact test and
correction must be pinned in-repo: a library upgrade must not be able to silently change
the benchmark's verdict.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import dataclass, field

#: Smallest effect worth acting on. Pre-registered, not chosen after seeing results: a
#: per-bug score difference below this is not a difference a model choice should turn on.
DEFAULT_DELTA0 = 0.10

#: Permutation count. Exact enumeration is used when the bug count is small enough to
#: afford it; otherwise this many random sign-flips.
EXACT_MAX = 20
N_PERMUTATIONS = 20_000
SEED = 20260930          # fixed so the p-value is reproducible


@dataclass
class Verdict:
    """The outcome of one pairwise comparison."""

    model_a: str
    model_b: str
    verdict: str                  # "A_WINS" | "B_WINS" | "TIED" | "INCONCLUSIVE"
    delta: float = 0.0
    p_value: float = 1.0
    p_adjusted: float = 1.0
    cliffs_delta: float = 0.0
    n_bugs: int = 0
    mean_a: float = 0.0
    mean_b: float = 0.0
    ci_low: float = 0.0
    ci_high: float = 1.0
    cluster_ci: tuple[float, float] | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        if self.cluster_ci:
            d["cluster_ci"] = list(self.cluster_ci)
        return d


def paired_differences(a: dict[str, float], b: dict[str, float]) -> dict[str, float]:
    """Per-bug differences over the intersection of bugs both models attempted.

    Intersecting is not optional. Comparing models over different bug sets measures which
    model got the luckier denominator, which is the exact defect that made the historical
    552-row archive unrankable.
    """
    shared = sorted(set(a) & set(b))
    return {bug: a[bug] - b[bug] for bug in shared}


def _exact_sign_test_p(diffs: list[float]) -> float:
    """Two-sided permutation p-value by full enumeration over sign flips."""
    nonzero = [d for d in diffs if d != 0.0]
    if not nonzero:
        return 1.0
    observed = abs(sum(nonzero))
    total = 0
    count = 0
    for mask in range(1 << len(nonzero)):
        s = 0
        for i, d in enumerate(nonzero):
            s += d if not (mask >> i) & 1 else -d
        total += 1
        if abs(s) >= observed - 1e-12:
            count += 1
    return count / total


def permutation_p(diffs: list[float], rng: random.Random) -> float:
    """Two-sided paired permutation p-value.

    Null hypothesis: the sign of each bug's difference is exchangeable, i.e. which model
    happened to do better on a given bug carries no information.
    """
    nonzero = [d for d in diffs if d != 0.0]
    if not nonzero:
        return 1.0
    if len(nonzero) <= EXACT_MAX:
        return _exact_sign_test_p(nonzero)

    observed = abs(sum(nonzero))
    extreme = 0
    n = len(nonzero)
    for _ in range(N_PERMUTATIONS):
        s = 0.0
        for d in nonzero:
            s += d if rng.random() < 0.5 else -d
        if abs(s) >= observed - 1e-12:
            extreme += 1
    # add-one smoothing keeps the p-value strictly positive, so it can be corrected
    return (extreme + 1) / (N_PERMUTATIONS + 1)


def cliffs_delta(a: dict[str, float], b: dict[str, float]) -> float:
    """Cliff's delta over the shared bugs: P(a>b) - P(a<b).

    Reported because it is meaningful without any significance claim: a model can be
    reliably a little better (delta 0.3) on every bug without any single bug being
    decisive, which a mean and a p-value together would hide.
    """
    shared = sorted(set(a) & set(b))
    if not shared:
        return 0.0
    gt = lt = 0
    for bug in shared:
        if a[bug] > b[bug]:
            gt += 1
        elif a[bug] < b[bug]:
            lt += 1
    return (gt - lt) / len(shared)


def bootstrap_ci(diffs: dict[str, float], iterations: int = 4000,
                 seed: int = SEED, alpha: float = 0.05) -> tuple[float, float]:
    """Percentile bootstrap CI for the mean paired difference."""
    vals = list(diffs.values())
    if len(vals) < 2:
        return (float(vals[0]), float(vals[0])) if vals else (0.0, 0.0)
    rng = random.Random(seed)
    means = []
    n = len(vals)
    for _ in range(iterations):
        means.append(sum(vals[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    lo = means[int(alpha / 2 * iterations)]
    hi = means[min(int((1 - alpha / 2) * iterations), iterations - 1)]
    return (lo, hi)


def cluster_bootstrap_ci(diffs: dict[str, float], bug_to_codebase: dict[str, str],
                         iterations: int = 4000, seed: int = SEED,
                         alpha: float = 0.05) -> tuple[float, float]:
    """Bootstrap resampling *codebases*, not bugs.

    Bugs inside one codebase are correlated -- same author, same style, same modules -- so
    resampling bugs as if independent understates the interval. This is the sensitivity
    analysis the primary result is reported beside.
    """
    groups: dict[str, list[float]] = defaultdict(list)
    for bug, d in diffs.items():
        groups[bug_to_codebase.get(bug, "?")].append(d)
    if len(groups) < 2:
        return bootstrap_ci(diffs, iterations, seed, alpha)

    rng = random.Random(seed)
    keys = sorted(groups)
    means = []
    for _ in range(iterations):
        picked = [keys[rng.randrange(len(keys))] for _ in keys]
        vals = [v for k in picked for v in groups[k]]
        means.append(sum(vals) / len(vals))
    means.sort()
    lo = means[int(alpha / 2 * iterations)]
    hi = means[min(int((1 - alpha / 2) * iterations), iterations - 1)]
    return (lo, hi)


def tost(ci_low: float, ci_high: float, delta0: float) -> bool:
    """Equivalence test: is the whole CI inside (-delta0, +delta0)?

    True means the gap is *bounded* below the smallest effect worth acting on -- that is a
    real TIED. False means it could not be bounded, which is INCONCLUSIVE and must not be
    reported as a tie.
    """
    return ci_low > -delta0 and ci_high < delta0


def holm(p_values: dict[str, float], alpha: float = 0.05) -> dict[str, float]:
    """Holm-Bonferroni adjusted p-values, monotonicity-enforced.

    With five models there are ten comparisons; reporting ten uncorrected p-values would
    manufacture roughly one spurious winner per campaign at alpha=0.05.
    """
    items = sorted(p_values.items(), key=lambda kv: kv[1])
    m = len(items)
    adjusted: dict[str, float] = {}
    running = 0.0
    for i, (key, p) in enumerate(items):
        val = min(1.0, (m - i) * p)
        running = max(running, val)          # enforce monotonicity
        adjusted[key] = running
    return adjusted


def compare(a_name: str, b_name: str, a: dict[str, float], b: dict[str, float],
            bug_to_codebase: dict[str, str], delta0: float = DEFAULT_DELTA0,
            alpha: float = 0.05, rng: random.Random | None = None) -> Verdict:
    """One pairwise comparison, with the verdict rule applied.

    Order of decision is fixed: significance first (is there a difference at all), then
    equivalence (is it smaller than delta0). That ordering is what keeps "TIED" from
    becoming a synonym for "not enough data".
    """
    rng = rng or random.Random(SEED)
    diffs = paired_differences(a, b)
    if not diffs:
        return Verdict(a_name, b_name, "INCONCLUSIVE",
                       notes=["no bugs are shared between these two models"])

    vals = list(diffs.values())
    p = permutation_p(vals, rng)
    delta = sum(vals) / len(vals)
    ci = bootstrap_ci(diffs)
    cci = cluster_bootstrap_ci(diffs, bug_to_codebase)
    cd = cliffs_delta(a, b)
    v = Verdict(
        a_name, b_name, "INCONCLUSIVE",
        delta=delta, p_value=p, cliffs_delta=cd,
        n_bugs=len(diffs), mean_a=sum(a[x] for x in diffs) / len(diffs),
        mean_b=sum(b[x] for x in diffs) / len(diffs),
        ci_low=ci[0], ci_high=ci[1], cluster_ci=cci,
    )

    if p < alpha and not (ci[0] <= 0 <= ci[1]):
        v.verdict = "A_WINS" if delta > 0 else "B_WINS"
        if (ci[0] > 0) != (cci[0] > 0) or (ci[1] > 0) != (cci[1] > 0):
            v.notes.append(
                "paired and cluster-bootstrap intervals disagree on the sign; "
                "trusting the paired result per the pre-registered rule"
            )
        return v

    if tost(ci[0], ci[1], delta0):
        v.verdict = "TIED"
        v.notes.append(f"gap bounded below delta0={delta0}")
        return v

    v.verdict = "INCONCLUSIVE"
    v.notes.append(
        f"could not bound the gap below delta0={delta0}; this is NOT a tie"
    )
    return v


def compare_all(scores: dict[str, dict[str, float]],
                bug_to_codebase: dict[str, str],
                delta0: float = DEFAULT_DELTA0,
                alpha: float = 0.05) -> list[Verdict]:
    """Every pairwise comparison, Holm-corrected, deterministically ordered."""
    names = sorted(scores)
    verdicts: list[Verdict] = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            v = compare(a, b, scores[a], scores[b], bug_to_codebase, delta0, alpha)
            verdicts.append(v)

    adjusted = holm({f"{v.model_a}|{v.model_b}": v.p_value for v in verdicts}, alpha)
    for v in verdicts:
        v.p_adjusted = adjusted[f"{v.model_a}|{v.model_b}"]
        # Re-apply the verdict with the corrected alpha: an uncorrected "win" that does not
        # survive multiplicity is not a finding.
        if v.p_adjusted >= alpha and v.verdict in {"A_WINS", "B_WINS"}:
            v.notes.append(
                f"win does not survive Holm correction (adjusted p={v.p_adjusted:.4g})"
            )
            v.verdict = ("TIED" if tost(v.ci_low, v.ci_high, delta0) else "INCONCLUSIVE")
    return verdicts
