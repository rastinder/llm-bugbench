#!/usr/bin/env python3
"""Tier-2 anonymisation: strip the remaining *context* that identifies the operator.

The credential scrub removes secrets and direct identifiers. This pass removes
*contextual* identifiers that survive it: session titles, project names, brand names,
customer names, the operator's city and the specific vendor being automated.

Use this for a public release. Use tasks.public.jsonl (credential scrub only) when the
benchmark is shared privately with a trusted evaluator who benefits from realistic
context.
"""
from __future__ import annotations

import re
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]

sys.path.insert(0, str(ROOT / "/src"))
from bugbench.models import Task, load_tasks, save_tasks  # noqa: E402

# context words -> replacement. Matched case-insensitively, whole-token where it matters.
CONTEXT = [
    # brand / operator / customer identity
    (r"brar[\s_-]?build[\s_-]?tech", "ACME"),
    (r"brarbuildtech", "ACME"),
    (r"rastinder", "the operator"),
    (r"ait[\s_-]?shirts", "example.invalid"),
    (r"betterbrampton", "example-campaign.invalid"),
    (r"mycondovalue", "example-property"),
    (r"kingerhomes", "example-property"),
    (r"paul[\s_-]?tarriwal", "the candidate"),
    (r"brampton", "CITY"),
    # the platform/vendor being automated -- a benchmark should not name its target
    (r"indeed", "JOBBOARD"),
    (r"linkedin", "JOBBOARD"),
    # the operator's own tooling -- reveals the environment
    (r"cloakbrowser", "BROWSERPOOL"),
    (r"opencode", "the agent harness"),
    (r"litellm", "the gateway"),
    (r"herdr", "the orchestrator"),
    (r"ruflo", "the swarm tool"),
    (r"claude[\s_-]?flow", "the swarm tool"),
    (r"sunnypilot", "ADAS"),
    (r"openpilot", "ADAS"),
    (r"autopilot[\s_-]?jobs", "job-automation"),
    (r"whatsedit", "video-tool"),
    (r"razorbill", "the payment provider"),
    (r"m365", "OFFICE"),
    (r"comfyui", "the image pipeline"),
    (r"kimi", "the assistant"),
    (r"telegram", "the messenger"),
    (r"discord", "the messenger"),
    (r"ollama", "the local runtime"),
    (r"llama[\s_\.\-]?cpp", "the local runtime"),
    (r"vllm", "the local runtime"),
    (r"cloudflare", "the edge CDN"),
    (r"whatsapp", "the messenger"),
]

CONTEXT_RX = [(re.compile(p, re.I), r) for p, r in CONTEXT]

PROJECT_MAP = {
    "ras": "project-a",
    "litellm-task": "project-b",
    "marketplace-monitor": "project-c",
    "websites-ruflo": "project-d",
}

FIELDS = ("goal", "original_goal", "buggy", "reference_fix", "file_name",
          "category_reason", "source_title")


def scrub(task: Task) -> Task:
    d = task.to_dict()
    for f in FIELDS:
        v = d.get(f)
        if not isinstance(v, str) or not v:
            continue
        for rx, rep in CONTEXT_RX:
            v = rx.sub(rep, v)
        # typos/separators the regexes miss: brar-build-t, brar_buildt, brarbuildt
        v = re.sub(r"brar[\s_-]*build[\s_-]*t?e?c?h?", "ACME", v, flags=re.I)
        d[f] = v
    d["project"] = PROJECT_MAP.get(d.get("project", ""), "project-x")
    d["source_title"] = ""            # session titles leak the whole engagement
    d["source_session"] = ""
    d["anonymised"] = True
    return Task.from_dict(d)


def main() -> int:
    src = str(ROOT / "/data/tasks.public.jsonl")
    dst = str(ROOT / "/data/tasks.public.anonymised.jsonl")
    tasks = load_tasks(src)
    out = [scrub(t) for t in tasks]
    save_tasks(out, dst)
    print(f"wrote {len(out)} anonymised tasks -> {dst}")

    # verify
    import re as _re
    bad = 0
    words = ["brarbuild", "indeed", "brampton", "aitshirts", "rastinder",
             "cloakbrowser", "opencode", "litellm", "sunnypilot", "ruflo", "herdr"]
    for t in out:
        blob = " ".join(str(getattr(t, f, "") or "") for f in FIELDS).lower()
        for w in words:
            if w in blob:
                print(f"  RESIDUAL {t.task_id}: {w}")
                bad += 1
        if t.source_title or t.source_session:
            print(f"  RESIDUAL {t.task_id}: session metadata")
            bad += 1
    if bad:
        print(f"\n{bad} residuals -- not publishable as-is")
        return 1
    print("verified: no operator context survives")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
