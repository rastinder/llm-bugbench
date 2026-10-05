import json, urllib.request, time, sys, re, shutil
from pathlib import Path

sys.path.insert(0, ".")
sys.path.insert(0, "src")
from bugbench.attempt import extract_patch, apply_patch_to_tree
from scripts.run_host_campaign import grade_task, WORK

LITELLM_URL = "https://aitshirts.in/litellm/v1/chat/completions"
LITELLM_KEY = "sk-litellm-vps-2026"
HEADERS = {
    "Authorization": f"Bearer {LITELLM_KEY}",
    "Content-Type": "application/json",
    "User-Agent": "curl/8.5.0"
}

tasks = json.load(open("data/host_tasks.json"))
sources = {t["task_id"]: t for t in json.load(open("data/host_tasks_sources.json"))}


def apply_search_replace(original: str, reply: str) -> str:
    pattern = re.compile(r'<{5,}\s*SEARCH\s*\n(.*?)\n={5,}\s*\n(.*?)\n>{5,}\s*REPLACE', re.S)
    matches = pattern.findall(reply)
    if matches:
        curr = original
        for s, r in matches:
            if s in curr:
                curr = curr.replace(s, r, 1)
            elif s.strip() in curr:
                curr = curr.replace(s.strip(), r.strip(), 1)
        if curr != original:
            return curr
    return original


def apply_model_fix(module: str, original: str, reply: str) -> str:
    sr_res = apply_search_replace(original, reply)
    if sr_res != original:
        return sr_res

    patch = extract_patch(reply)
    if patch:
        tmp_box = Path("/tmp/litellm_patch_box")
        shutil.rmtree(tmp_box, ignore_errors=True)
        tmp_box.mkdir(parents=True, exist_ok=True)
        (tmp_box / module).parent.mkdir(parents=True, exist_ok=True)
        (tmp_box / module).write_text(original)
        if apply_patch_to_tree(tmp_box, patch):
            mod = (tmp_box / module).read_text()
            shutil.rmtree(tmp_box, ignore_errors=True)
            return mod
        shutil.rmtree(tmp_box, ignore_errors=True)

    fences = re.findall(r'```(?:python|py)?\s*\n(.*?)```', reply, re.S)
    for code in fences:
        if len(code) >= len(original) * 0.7:
            try:
                import ast
                ast.parse(code)
                if code.strip() != original.strip():
                    return code
            except SyntaxError:
                pass

    return original


def eval_model(model_name: str, out_file: str):
    print(f"\n==========================================")
    print(f"Evaluating {model_name} on {len(tasks)} tasks...")
    print(f"==========================================")
    rows = []

    for i, task in enumerate(tasks, 1):
        tid = task["task_id"]
        full = {**task, **sources[tid]}
        repo = Path(task["repo"])
        module = task["module"]
        before = full["before_source"]

        prompt = f"""You are fixing a real bug in a real codebase.

The file `{module}` has a defect. Find and fix it.

Here is `{module}`:
```python
{before}
```

Rules:
- Reply with the fix using a SEARCH/REPLACE block or a unified diff.
- Do not refactor or reformat. Keep changes minimal.

Format:
<<<<<<< SEARCH
exact lines to replace
=======
replacement lines
>>>>>>> REPLACE
"""
        req_data = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": "You are an automated code repair assistant. Output the SEARCH/REPLACE block fixing the defect."},
                {"role": "user", "content": prompt}
            ],
            "max_tokens": 1200,
            "temperature": 0.0
        }
        
        t0 = time.time()
        try:
            req = urllib.request.Request(LITELLM_URL, headers=HEADERS, data=json.dumps(req_data).encode())
            resp = urllib.request.urlopen(req, timeout=120)
            res = json.loads(resp.read())
            reply = res["choices"][0]["message"].get("content", "") or ""
            latency = time.time() - t0
            after = apply_model_fix(module, before, reply)
            if after != before:
                work = WORK / f"eval_{model_name.replace('/', '_')}_{tid[-16:]}"
                grades = grade_task(full, repo, after, work)
                fixed = grades.get(tid, False)
                outcome = "scored"
            else:
                fixed = False
                outcome = "invalid_response"
        except Exception as e:
            latency = time.time() - t0
            reply = f"ERROR: {e}"
            fixed = False
            outcome = "transport"

        row = {
            "model": model_name,
            "task_id": tid,
            "outcome": outcome,
            "ok": outcome == "scored",
            "latency_s": round(latency, 2),
            "bugs_fixed": 1 if fixed else 0,
            "bugs_total": 1,
            "reply_snippet": reply[:200]
        }
        rows.append(row)
        print(f"[{i}/{len(tasks)}] {tid}: outcome={outcome} fixed={fixed} ({latency:.1f}s)", flush=True)

    with open(out_file, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    fixed_count = sum(r["bugs_fixed"] for r in rows)
    print(f"\nResult for {model_name}: {fixed_count}/{len(tasks)} = {fixed_count/len(tasks):.1%}")
    return rows


if __name__ == "__main__":
    eval_model("openrouter-space-bunny-alpha", "data/results/host-space-bunny.jsonl")
    eval_model("openrouter-glm-5.2", "data/results/host-glm-5.2.jsonl")
