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

from collections import defaultdict
from difflib import SequenceMatcher

BUG_FOUND_THRESHOLD = 0.75   # combined = location_match + judge_mechanism
FIXED_THRESHOLD = 0.75
EQUIV_FIX_THRESHOLD = 0.90
UNTOUCHED_THRESHOLD = 0.97


def _norm(s: str) -> str:
    import re
    return re.sub(r"\s+", " ", (s or "").strip())


def _pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def mark_row(row: dict, task) -> dict:
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

    return {
        "task_id": row.get("task_id"),
        "model": row.get("model"),
        "bug_found": bool(found),
        "bug_fixed": bool(fixed),
        "same_fix": bool(same),
        "equiv_fix": bool(equiv),
        "untouched": bool(untouched),
        "score_pct": round(100.0 * float(row.get("combined") or 0.0), 1),
        "diagnose_pct": round(100.0 * float(d.get("total", 0.0) or 0.0), 1),
        "repair_pct": round(100.0 * float(r.get("total", 0.0) or 0.0), 1),
    }


def mark_all(rows: list[dict], tasks: dict) -> list[dict]:
    out = []
    for row in rows:
        t = tasks.get(row.get("task_id"))
        if t is not None:
            out.append(mark_row(row, t))
    return out


def clean_rows(rows: list[dict], tasks: dict,
               roots=("/home/ras", "/root", "/home")) -> tuple[list[dict], list[dict]]:
    """Split rows into trusted and quarantined.

    A row where the answer is byte-identical to the historical fix AND the real source
    file was readable on disk is a FILE-READ LEAK: an agentic lane copied the answer out
    of the repo. Those rows are excluded from the headline numbers, not silently averaged
    in, and are reported separately so the exclusion is visible.
    """
    from .cheat import check_row
    trusted, quarantined = [], []
    for r in rows:
        t = tasks.get(r.get("task_id"))
        if t is None:
            trusted.append(r)
            continue
        v = check_row(r, t, roots=roots)
        (quarantined if v["signals"].get("file_read_leak", {}).get("fired")
         else trusted).append(r)
    return trusted, quarantined


def leaderboard_marks(rows: list[dict], tasks: dict) -> list[dict]:
    """`N bugs found out of M` + 0-100% scores, per model."""
    agg = defaultdict(lambda: {"found": 0, "fixed": 0, "same": 0, "equiv": 0,
                                "untouched": 0, "n": 0, "comb": [], "diag": [],
                                "rep": []})
    for m in mark_all(rows, tasks):
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
        out.append({
            "model": model,
            "tasks": n,
            "bugs_found": a["found"],
            "found_str": f"{a['found']}/{n} ({_pct(a['found'], n)}%)",
            "bugs_fixed": a["fixed"],
            "fixed_str": f"{a['fixed']}/{n} ({_pct(a['fixed'], n)}%)",
            "same_fix": a["same"],
            "same_fix_str": f"{a['same']}/{n} ({_pct(a['same'], n)}%)",
            "equiv_fix": a["equiv"],
            "equiv_fix_str": f"{a['equiv']}/{n} ({_pct(a['equiv'], n)}%)",
            "untouched": a["untouched"],
            "untouched_str": f"{a['untouched']}/{n} ({_pct(a['untouched'], n)}%)",
            "score_pct": round(c, 1),
            "ci95": f"{clo}-{chi}",
            "diagnose_pct": round(d, 1),
            "repair_pct": round(p, 1),
        })
    out.sort(key=lambda r: (-r["score_pct"], -r["tasks"]))
    return out
