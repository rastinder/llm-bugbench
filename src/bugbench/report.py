"""Leaderboard with bootstrap confidence intervals (council: no bare point estimates)."""
from __future__ import annotations

import random
from collections import defaultdict
from typing import Any

BOOTSTRAP_N = 1000
SEED = 12345


def _mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def bootstrap_ci(values: list[float], n: int = BOOTSTRAP_N,
                 seed: int = SEED) -> tuple[float, float, float]:
    """Return (point, lo95, hi95) using a seeded bootstrap so results are reproducible."""
    if not values:
        return 0.0, 0.0, 0.0
    point = _mean(values)
    if len(values) == 1:
        return point, point, point
    rng = random.Random(seed)
    m = len(values)
    means = []
    for _ in range(n):
        means.append(_mean(values[rng.randrange(m)] for _ in range(m)))
    means.sort()
    lo = means[int(0.025 * n)]
    hi = means[min(n - 1, int(0.975 * n))]
    return round(point, 4), round(lo, 4), round(hi, 4)


STAGES = ("diagnose", "repair", "repair_given")


def _metric(row: dict, name: str) -> float | None:
    if name == "combined":
        return row.get("combined")
    if name == "diagnosis_uplift":
        v = row.get("diagnosis_uplift")
        return None if v is None else float(v)
    if name in STAGES:
        v = row.get(name)
        return None if not v else v.get("total")
    v = row.get(name) or {}
    return v.get("total") if isinstance(v, dict) else None


def _row_errored(r: dict) -> bool:
    if metric_of(r) is None:
        return False
    for stage in STAGES:
        if (r.get(stage) or {}).get("error"):
            return True
    return False


def metric_of(r: dict):
    return r.get("combined") if ("diagnose" in r or "repair" in r) else r.get("combined")


def leaderboard(rows: list[dict], metric: str = "combined") -> list[dict]:
    by_model = defaultdict(list)
    for r in rows:
        v = _metric(r, metric)
        if v is not None:
            by_model[r["model"]].append((r.get("task_id"), float(v),
                                         _row_errored(r)))
    out = []
    for model, pairs in by_model.items():
        pairs.sort()
        vals = [v for _, v, _ in pairs]
        errs = sum(1 for _, _, e in pairs if e)
        point, lo, hi = bootstrap_ci(vals)
        n = len(vals)
        out.append({
            "model": model,
            "metric": metric,
            "tasks": n,
            "score": point,
            "ci95_lo": lo,
            "ci95_hi": hi,
            "solved": sum(1 for v in vals if v >= 0.999),
            "partial": sum(1 for v in vals if 0.001 <= v < 0.999),
            "missed": sum(1 for v in vals if v <= 0.001),
            # infrastructure failures must not be read as capability: a model that was
            # rate-limited on every task scores ~0 for availability, not for skill
            "errors": errs,
            "error_rate": round(errs / n, 3) if n else 0.0,
        })
    out.sort(key=lambda r: (-r["score"], -r["tasks"]))
    return out


def category_breakdown(rows: list[dict]) -> list[dict]:
    agg = defaultdict(lambda: defaultdict(list))
    for r in rows:
        cat = r.get("category") or "unclassified"
        v = _metric(r, "combined")
        if v is not None:
            agg[r["model"]][cat].append(float(v))
    out = []
    for model, cats in agg.items():
        for cat, vals in cats.items():
            p, lo, hi = bootstrap_ci(vals)
            out.append({"model": model, "category": cat, "n": len(vals),
                        "score": p, "ci95_lo": lo, "ci95_hi": hi})
    out.sort(key=lambda r: (r["model"], -r["score"]))
    return out


def sub_signals(rows: list[dict]) -> list[dict]:
    """Per-model breakdown of every raw signal so no single number hides a regression."""
    keys = [("diagnose", "total"), ("diagnose", "location_match"),
            ("diagnose", "judge_agreement"), ("diagnose", "category_match"),
            ("repair", "total"), ("repair", "patch_reproduction"),
            ("repair", "judge_agreement"), ("repair", "oracle_score")]
    agg = defaultdict(lambda: defaultdict(list))
    for r in rows:
        for stage, k in keys:
            v = (r.get(stage) or {}).get(k)
            if v is not None:
                agg[r["model"]][f"{stage}.{k}"].append(float(v))
    out = []
    for model, sigs in agg.items():
        row = {"model": model}
        for name, vals in sigs.items():
            row[name] = round(_mean(vals), 4)
            row[name + "_n"] = len(vals)
        out.append(row)
    out.sort(key=lambda r: r["model"])
    return out


def per_task(rows: list[dict]) -> list[dict]:
    out = []
    for r in sorted(rows, key=lambda r: (r.get("task_id", ""), r.get("model", ""))):
        out.append({
            "model": r.get("model"), "task_id": r.get("task_id"),
            "language": r.get("language"), "category": r.get("category"),
            "project": r.get("project"),
            "diagnose": (r.get("diagnose") or {}).get("total"),
            "repair": (r.get("repair") or {}).get("total"),
            "patch_reproduction": (r.get("repair") or {}).get("patch_reproduction"),
            "combined": r.get("combined"),
        })
    return out


def oracle_coverage(rows: list[dict]) -> dict:
    """How much of the task set has a validated execution oracle.

    Reported explicitly because the two subsets are NOT comparable: without an oracle the
    repair score is judge + patch-reproduction only, and averaging them together would
    overstate models on the non-executable tasks.
    """
    total = len(rows)
    with_oracle = sum(1 for r in rows
                      if (r.get("repair") or {}).get("oracle") == "exec_diff")
    judged = sum(1 for r in rows
                 if (r.get("repair") or {}).get("judge_agreement") is not None)
    n_tasks = len({r.get("task_id") for r in rows})
    n_models = len({r.get("model") for r in rows})
    return {
        "rows": total,
        "tasks": n_tasks,
        "models": n_models,
        "with_oracle": with_oracle,
        "oracle_rate": round(with_oracle / total, 4) if total else 0.0,
        "judge_scored": judged,
        "note": ("tasks without a validated oracle are scored judge + patch-reproduction "
                 "only; the two subsets should be read separately"),
    }


def report(rows: list[dict]) -> dict[str, Any]:
    return {
        "n_rows": len(rows),
        "n_models": len({r.get("model") for r in rows}),
        "leaderboard_combined": leaderboard(rows, "combined"),
        "leaderboard_diagnose": leaderboard(rows, "diagnose"),
        "leaderboard_repair": leaderboard(rows, "repair"),
        "leaderboard_repair_given": leaderboard(rows, "repair_given"),
        "diagnosis_uplift": leaderboard(rows, "diagnosis_uplift"),
        "category_breakdown": category_breakdown(rows),
        "sub_signals": sub_signals(rows),
        "oracle_coverage": oracle_coverage(rows),
    }
