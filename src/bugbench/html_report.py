"""Build the HTML leaderboard page."""
from __future__ import annotations

import html
import json
from typing import Any

from .report import report as build_report


def _rows_table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "<p class='empty'>no data yet — run the benchmark first</p>"
    th = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    trs = []
    for r in rows:
        tds = "".join(f"<td>{html.escape(str(c))}</td>" for c in r)
        trs.append(f"<tr>{tds}</tr>")
    return f"<table><thead><tr>{th}</tr></thead><tbody>{''.join(trs)}</tbody></table>"


def render_html(rows: list[dict], tasks: dict | None = None,
                cheat: dict | None = None) -> str:
    rep = build_report(rows)
    marks = []
    if tasks:
        try:
            from .marks import leaderboard_marks
            marks = leaderboard_marks(rows, tasks)
        except Exception:
            marks = []

    def lb(entries):
        return _rows_table(
            ["#", "model", "tasks", "score", "95% CI", "solved", "partial", "missed",
             "errors"],
            [[i + 1, e["model"], e["tasks"], e["score"],
              f"{e['ci95_lo']} – {e['ci95_hi']}", e["solved"], e["partial"],
              e["missed"], f"{e.get('errors', 0)} ({e.get('error_rate', 0)*100:.0f}%)"]
             for i, e in enumerate(entries)])

    sig = _rows_table(
        ["model"] + sorted({k for s in rep["sub_signals"]
                            for k in s if not k.endswith("_n")}),
        [[s["model"]] + [s.get(k, "-") for k in
                         sorted({k for x in rep["sub_signals"]
                                 for k in x if not k.endswith("_n")})]
         for s in rep["sub_signals"]])

    cats = _rows_table(
        ["model", "category", "n", "score", "95% CI"],
        [[c["model"], c["category"], c["n"], c["score"],
          f"{c['ci95_lo']} – {c['ci95_hi']}"] for c in rep["category_breakdown"]])

    oc = rep["oracle_coverage"]
    marks_html = ""
    if marks:
        marks_html = """
<h2>How many bugs found — out of how many</h2>%s
<p class="note"><b>found</b> = named a line the real fix changed AND the judge agreed on
the mechanism. <b>fixed</b> = the blind judge said the defect is actually repaired.
<b>same</b> = byte-identical to the developer's historical fix (the memorisation signal).
<b>untouched</b> = the model returned the input unchanged (the null-answer signal).
All scores are 0&ndash;100%%.</p>""" % _rows_table(
            ["model", "tasks", "found", "fixed", "same fix", "untouched",
             "score", "95% CI"],
            [[m["model"], m["tasks"], m["found_str"], m["fixed_str"],
              m["same_fix_str"], m["untouched_str"], f"{m['score_pct']}%", m["ci95"]]
             for m in marks])

    cheat_html = ""
    if cheat:
        flagged = [m for m in cheat["per_model"] if m["flagged"]]
        rows_c = [[m["model"], f"{m['flagged']}/{m['rows']}",
                   f"{m['flag_rate']*100:.0f}%", ", ".join(f"{k}×{v}" for k, v in
                                                             m["by_signal"].items())]
                  for m in flagged]
        cheat_html = f"""
<h2>Integrity screen</h2>
<p class="note">Scored {cheat['rows_screened']} rows. <b>{cheat['total_flagged']}</b>
flagged. Signals: <code>verbatim_reproduction</code> (memorisation),
<code>echo_input</code> (returned the input), <code>degenerate_output</code>
(refusal or no code), <code>judge_gaming</code> (judge accepted an unmodified snippet).</p>
{_rows_table(["model", "flagged", "rate", "signals"], rows_c) if rows_c else "<p class='empty'>no model flagged</p>"}"""
    combined = rep["leaderboard_combined"]
    if combined:
        winner = combined[0]
        headline = (f"<b>{html.escape(winner['model'])}</b> leads with "
                    f"<b>{winner['score']}</b> "
                    f"(95% CI {winner['ci95_lo']}–{winner['ci95_hi']}, "
                    f"{winner['tasks']} tasks)")
    else:
        headline = "No results recorded yet."

    body_marks = marks_html if marks_html else ""
    body_cheat = cheat_html if cheat_html else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>bugbench — LLM bug-fixing leaderboard</title>
<style>
 :root {{ --bg:#0d1117; --fg:#e6edf3; --dim:#8b949e; --line:#30363d; --acc:#58a6ff; }}
 * {{ box-sizing:border-box }}
 body {{ margin:0; padding:32px; background:var(--bg); color:var(--fg);
   font:15px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace; }}
 h1 {{ font-size:22px; margin:0 0 4px; letter-spacing:.02em }}
 h2 {{ font-size:15px; margin:28px 0 8px; color:var(--acc);
   text-transform:uppercase; letter-spacing:.08em }}
 p.sub {{ color:var(--dim); margin:0 0 20px; max-width:70ch }}
 p.head {{ padding:12px 14px; border:1px solid var(--line); border-radius:6px;
   background:#161b22; margin:0 0 8px }}
 table {{ border-collapse:collapse; width:100%; margin-bottom:8px; }}
 th,td {{ border:1px solid var(--line); padding:6px 10px; text-align:left;
   font-size:13px }}
 th {{ background:#161b22; color:var(--dim); font-weight:600 }}
 td:first-child {{ color:var(--dim) }}
 .empty {{ color:var(--dim); font-style:italic }}
 .note {{ color:var(--dim); font-size:12px; margin-top:6px }}
</style></head><body>
<h1>bugbench</h1>
<p class="sub">Real bug-fix episodes mined from this machine's opencode history.
Each task is an exact <code>before</code>&nbsp;→&nbsp;<code>after</code> code-region pair:
the model sees only the goal and the buggy code, never the fix.
Two stages are scored separately &mdash; <b>diagnose</b> (find the defect) and
<b>repair</b> (fix it) &mdash; with every raw sub-signal shown so no single weighted
number can hide a regression. Intervals are seeded bootstrap 95% CI over tasks.</p>
<p class="head">{headline}</p>

<h2>Combined &mdash; diagnose + repair</h2>{lb(rep['leaderboard_combined'])}
<p class="note"><b>errors</b> counts tasks lost to endpoint failures (rate limits,
timeouts). A model with a high error rate is measuring availability, not skill — read its
score with that in mind. combined = 0.5&times;diagnose + 0.5&times;repair (frozen; no calibration data yet).</p>

<h2>Stage 1 &mdash; identify the bug</h2>{lb(rep['leaderboard_diagnose'])}
<h2>Stage 2 &mdash; fix the bug</h2>{lb(rep['leaderboard_repair'])}

<h2>Raw sub-signals</h2>{sig}
<p class="note"><code>patch_reproduction</code> measures resemblance to the developer's
historical edit, not correctness. <code>oracle_score</code> is populated only for tasks whose
execution test was validated (fails on buggy, passes on the reference fix).</p>

<h2>By category</h2>{cats}

{body_marks}
{body_cheat}
<h2>Grading coverage</h2>
<p class="note">Oracle coverage: <b>{oc['with_oracle']}/{oc['rows']}</b> scored rows have a
validated execution oracle ({oc['oracle_rate']*100:.0f}%). {html.escape(oc['note'])}.
Judge-scored rows: {oc['judge_scored']}.</p>
<p class="note">{rep['n_rows']} scored rows from {rep['n_models']} models.</p>
</body></html>"""
