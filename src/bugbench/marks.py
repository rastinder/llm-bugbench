"""Human-readable benchmark markers: "how many bugs found out of how many".

Everything here is an integer count out of an integer total, on a 0-100% scale, so a
leaderboard can be read without understanding the scoring formula.

Definitions are deliberately strict and stated so they cannot be gamed by a loose
threshold:

  bug_found      the model named a line the real fix changed, AND the blind judge agreed
                  the mechanism matched.  (Both must hold.)
  bug_fixed      the blind judge said the candidate actually fixes the defect.
  same_fix       the candidate is byte-identical to the developer's historical fix.
  equiv_fix      token similarity to the historical fix >= 0.90.
  untouched      the candidate is ~identical to the buggy snippet (a null answer).
"""
from __future__ import annotations
from pathlib import Path

from collections import defaultdict
from difflib import SequenceMatcher

BUG_FOUND_THRESHOLD = 0.75   # combined = location_match + judge_mechanism
FIXED_THRESHOLD = 0.75
EQUIV_FIX_THRESHOLD = 0.90
UNTOUCHED_THRESHOLD = 0.97

#: a model must have scored at least this many of the 20 panel tasks to be ranked at all
MIN_COVERAGE = 15


def _norm(s: str) -> str:
    import re
    return re.sub(r"\s+", " ", (s or "").strip())


def _pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def mark_row(row: dict, task) -> dict:
    from .outcome import outcome_state

    state = outcome_state(row)
    d = row.get("diagnose") or {}
    r = row.get("repair") or {}
    cand = r.get("candidate") or ""
    buggy = getattr(task, "buggy", "") or ""
    ref = getattr(task, "reference_fix", "") or ""

    loc = float(d.get("location_match", 0.0) or 0.0)
    mech = float(d.get("judge_agreement", 0.0) or 0.0)
    found = (loc >= 1.0 and mech >= 0.5) or ((loc + mech) / 2.0) >= BUG_FOUND_THRESHOLD
    fixed = float(r.get("judge_agreement", 0.0) or 0.0) >= FIXED_THRESHOLD
    same = _norm(cand) == _norm(ref) and len(_norm(ref)) > 40
    equiv = float(r.get("patch_reproduction", 0.0) or 0.0) >= EQUIV_FIX_THRESHOLD
    try:
        untouched = (SequenceMatcher(None, _norm(cand), _norm(buggy)).ratio()
                     >= UNTOUCHED_THRESHOLD and len(_norm(buggy)) > 40)
    except Exception:
        untouched = False

    scored = state == "completed"
    return {
        "task_id": row.get("task_id"),
        "model": row.get("model"),
        "state": state,
        "scored": scored,
        "bug_found": bool(found) if scored else False,
        "bug_fixed": bool(fixed) if scored else False,
        "same_fix": bool(same) if scored else False,
        "equiv_fix": bool(equiv) if scored else False,
        "untouched": bool(untouched) if scored else False,
        # an unscored row has NO score -- it must not read as 0%
        "score_pct": round(100.0 * float(row.get("combined") or 0.0), 1) if scored else None,
        "diagnose_pct": round(100.0 * float(d.get("total", 0.0) or 0.0), 1) if scored else None,
        "repair_pct": round(100.0 * float(r.get("total", 0.0) or 0.0), 1) if scored else None,
        "latency_ms": r.get("latency_ms") or d.get("latency_ms"),
    }


def mark_all(rows: list[dict], tasks: dict) -> list[dict]:
    out = []
    for row in rows:
        t = tasks.get(row.get("task_id"))
        if t is not None:
            out.append(mark_row(row, t))
    return out


def clean_rows(rows: list[dict], tasks: dict,
               roots=("/root", "/home") + tuple(str(x) for x in [Path.home()])) -> tuple[list[dict], list[dict]]:
    """Split rows into trusted and quarantined.

    A row where the answer is byte-identical to the historical fix AND the real source
    file was readable on disk is a FILE-READ LEAK: an agentic lane copied the answer out
    of the repo. Those rows are excluded from the headline numbers, not silently averaged
    in, and are reported separately so the exclusion is visible.

    The leak test is additionally gated on the lane actually being able to read the
    filesystem (see `runners.lane_has_filesystem`, applied inside `check_row`). A plain
    HTTP chat lane has no filesystem access, so a byte-identical answer from it is
    capability or memorisation, not a leak -- without this gate the detector quarantined
    healthy API models and threw away the #2 and #3 results.
    """
    from .cheat import check_row
    trusted, quarantined = [], []
    for r in rows:
        t = tasks.get(r.get("task_id"))
        # An explicit `tainted` / `taint_reason` on the row is authoritative -- that is
        # where a prior run's verdict is recorded. The detector below can *add* a taint,
        # never clear one.
        if r.get("tainted") or r.get("taint_reason"):
            quarantined.append(r)
            continue
        if t is None:
            trusted.append(r)
            continue
        v = check_row(r, t, roots=roots)
        leak = v["signals"].get("file_read_leak", {})
        if leak.get("fired"):
            r["tainted"] = "file_read_leak"      # stamp it so the verdict persists
            r["taint_reason"] = leak.get("detail", "")
            quarantined.append(r)
            continue
        trusted.append(r)
    return trusted, quarantined


def common_task_set(rows: list[dict], min_models: int = 2) -> set[str]:
    """Task ids attempted by at least `min_models` models.

    Model runs used different `--limit` values, so each row's denominator (M) is the number
    of tasks THAT model saw -- not the number of bugs in the dataset. Because the runner
    shuffles with a fixed seed the sets are nested (limit-10 is a prefix of limit-20), so
    the counts are comparable, but only on the shared prefix. Anything outside it is not a
    fair comparison, so this returns the comparable set.
    """
    per: dict[str, set] = defaultdict(set)
    for r in rows:
        per[r["model"]].add(r["task_id"])
    counts: dict[str, int] = defaultdict(int)
    for ids in per.values():
        for t in ids:
            counts[t] += 1
    return {t for t, c in counts.items() if c >= min_models}


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Correct for N=20 where the normal approximation is not: a naive +-1.96*sqrt(p(1-p)/n)
    produces a lower bound below 0 and mis-ranks small differences. Returns fractions 0-1.
    """
    if n <= 0:
        return (0.0, 0.0)
    p = k / n
    d = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = (z / d) * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return (max(0.0, centre - half), min(1.0, centre + half))


def utility(capability: float, completed: int, attempted: int) -> float:
    """`capability * availability` -- the single number for "which model should I use".

    Product form on purpose: an endpoint that can only answer 20% of the time cannot be
    the best choice however good its answers are, and a model that answers everything at
    20% quality cannot either. Multiplying means both failure modes drag the score down,
    while each component stays independently visible in the board.
    """
    if attempted <= 0:
        return 0.0
    return round(capability * (completed / attempted), 1)


def _median(xs):
    v = sorted(x for x in xs if x is not None)
    if not v:
        return None
    m = len(v) // 2
    return v[m] if len(v) % 2 else int(round((v[m - 1] + v[m]) / 2))


def decision_board(rows: list[dict], tasks: dict,
                   panel: int = 20,
                   min_coverage: int = MIN_COVERAGE) -> tuple[list[dict], list[str]]:
    """The model-selection board. Returns `(entries, quarantined_models)`.

    Deliberately NOT a pairwise "tasks both models completed" comparison: that breaks
    transitivity (A>B, B>C, C>A) and produces a board nobody can read. Instead every model
    is scored on its own completed tasks, and its coverage/availability are reported
    beside it, so the two dimensions never get silently mixed.
    """
    from .outcome import outcome_state, classify_error

    trusted, quarantined = clean_rows(rows, tasks)
    tainted_models = sorted({r["model"] for r in quarantined})

    per: dict[str, dict] = defaultdict(
        lambda: {"attempted": 0, "completed": 0, "comb": [], "found": 0, "fixed": 0,
                 "lat": [], "states": []})
    for r in trusted:
        a = per[r["model"]]
        a["attempted"] += 1
        st = outcome_state(r)
        a["states"].append(st)
        if st != "completed":
            continue
        a["completed"] += 1
        t = tasks.get(r.get("task_id"))
        if t is None:
            continue
        m = mark_row(r, t)
        if m["score_pct"] is not None:
            a["comb"].append(m["score_pct"])
        a["found"] += m["bug_found"]
        a["fixed"] += m["bug_fixed"]
        lat = (r.get("repair") or {}).get("latency_ms") or \
              (r.get("diagnose") or {}).get("latency_ms")
        if lat:
            a["lat"].append(int(lat))

    out = []
    for model, a in per.items():
        n_done, n_try = a["completed"], a["attempted"]
        cap = round(sum(a["comb"]) / len(a["comb"]), 1) if a["comb"] else None
        avail = round(100.0 * n_done / n_try, 1) if n_try else 0.0
        cov = round(100.0 * n_done / panel, 1) if panel else 0.0
        u = utility(cap, n_done, n_try) if cap is not None else 0.0

        if n_done == 0:
            # the crucial distinction: a dead endpoint is NOT a 0% model
            err = next((classify_error(
                " ".join(str(r.get(k) or "") for k in
                         ("diagnose_error", "repair_error", "repair_given_error")))
                for r in trusted if r["model"] == model and outcome_state(r) == "infra_error"),
                None)
            status = "unavailable"
            reason = (f"{err.kind} (retryable)" if err and err.retryable
                      else f"{err.kind} (do not retry)" if err
                      else "no usable answers")
        elif n_done < min_coverage:
            status, reason = "insufficient_coverage", f"only {n_done}/{panel} tasks completed"
        else:
            status, reason = "ok", ""

        entry = {
            "model": model,
            "status": status,
            "reason": reason,
            "completed": n_done,
            "attempted": n_try,
            "coverage_pct": cov,
            "availability_pct": avail,
            "capability": cap,
            "utility": u,
            "bugs_found": a["found"],
            "bugs_fixed": a["fixed"],
            "found_str": f"{a['found']}/{n_done} ({_pct(a['found'], n_done)}%)",
            "fixed_str": f"{a['fixed']}/{n_done} ({_pct(a['fixed'], n_done)}%)",
            "median_latency_ms": _median(a["lat"]),
            "ranking_eligible": status == "ok",
        }
        if a["comb"]:
            # success rate = fraction of completed tasks counted as bug_fixed
            k = a["fixed"]
            lo, hi = wilson_ci(k, n_done)
            entry["success_rate"] = round(k / n_done, 4)
            entry["ci95"] = f"{lo*100:.1f}-{hi*100:.1f}"
        else:
            entry["success_rate"] = None
            entry["ci95"] = "n/a"
        out.append(entry)

    out.sort(key=lambda e: (e["ranking_eligible"], e["utility"], e["capability"] or 0),
             reverse=True)
    return out, tainted_models


def format_decision_board(board: list[dict], quarantined: list[str]) -> str:
    """Human-readable ranking for model selection."""
    L = ["MODEL SELECTION BOARD  (utility = capability x availability, 0-100)", ""]
    L.append(f"{'model':44s} {'util':>6s} {'cap':>6s} {'avail':>6s} {'cov':>5s} "
             f"{'found':>9s} {'fixed':>9s} {'lat':>7s} {'CI95':>10s}  status")
    L.append("-" * 122)
    for e in board:
        lat = f"{e['median_latency_ms']/1000:.1f}s" if e["median_latency_ms"] else "-"
        cap = f"{e['capability']:.1f}" if e["capability"] is not None else "-"
        L.append(f"{e['model'][:44]:44s} {e['utility']:6.1f} {cap:>6s} "
                 f"{e['availability_pct']:5.0f}% {e['coverage_pct']:4.0f}% "
                 f"{e['found_str']:>9s} {e['fixed_str']:>9s} {lat:>7s} "
                 f"{e['ci95']:>10s}  {e['status']}"
                 + (f" [{e['reason']}]" if e["reason"] else ""))
    if quarantined:
        L.append("")
        L.append("QUARANTINED - excluded from ranking (file-read leak / verbatim answer):")
        for m in quarantined:
            L.append(f"  {m}")
    return "\n".join(L)

def leaderboard_marks(rows: list[dict], tasks: dict,
                      restrict_to: set[str] | None = None) -> list[dict]:
    """Legacy `N/M` board, kept for the existing CLI path.

    Now state-aware: tainted rows are dropped and only `completed` rows contribute a
    count or a score, so this can no longer print a 0% for a dead endpoint either.
    Prefer `decision_board` for model selection.
    """
    trusted, _ = clean_rows(rows, tasks)
    rows = trusted
    shared_n = len(restrict_to) if restrict_to is not None else None
    if restrict_to is not None:
        rows = [r for r in rows if r["task_id"] in restrict_to]
    agg = defaultdict(lambda: {"found": 0, "fixed": 0, "same": 0, "equiv": 0,
                                "untouched": 0, "n": 0, "comb": [], "diag": [],
                                "rep": []})
    for m in mark_all(rows, tasks):
        if not m["scored"]:
            continue
        a = agg[m["model"]]
        a["n"] += 1
        a["found"] += m["bug_found"]
        a["fixed"] += m["bug_fixed"]
        a["same"] += m["same_fix"]
        a["equiv"] += m["equiv_fix"]
        a["untouched"] += m["untouched"]
        a["comb"].append(m["score_pct"])
        a["diag"].append(m["diagnose_pct"])
        a["rep"].append(m["repair_pct"])

    from .report import bootstrap_ci
    out = []
    for model, a in agg.items():
        n = a["n"]
        c, clo, chi = bootstrap_ci(a["comb"])
        d, _, _ = bootstrap_ci(a["diag"])
        p, _, _ = bootstrap_ci(a["rep"])
        den = shared_n if shared_n is not None else n
        out.append({
            "model": model,
            "tasks": den,
            "attempted": n,
            "bugs_found": a["found"],
            "found_str": f"{a['found']}/{den} ({_pct(a['found'], den)}%)",
            "bugs_fixed": a["fixed"],
            "fixed_str": f"{a['fixed']}/{den} ({_pct(a['fixed'], den)}%)",
            "same_fix": a["same"],
            "same_fix_str": f"{a['same']}/{den} ({_pct(a['same'], den)}%)",
            "equiv_fix": a["equiv"],
            "equiv_fix_str": f"{a['equiv']}/{den} ({_pct(a['equiv'], den)}%)",
            "untouched": a["untouched"],
            "untouched_str": f"{a['untouched']}/{den} ({_pct(a['untouched'], den)}%)",
            "score_pct": round(c, 1),
            "ci95": f"{clo}-{chi}",
            "diagnose_pct": round(d, 1),
            "repair_pct": round(p, 1),
        })
    out.sort(key=lambda r: (-r["score_pct"], -r["tasks"]))
    return out
