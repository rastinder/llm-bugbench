"""Minimal benchmark agent running strictly inside /work in the bench container.
Communicates with services:8000 for LLM inference.
All tool operations are confined to /work.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

WORK_DIR = Path("/work").resolve()

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read file contents within /work, or list contents if path is a directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to file or directory in /work"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and directories in /work.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to directory in /work (default '.')"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "replace_file_content",
            "description": "Replace exact target block with replacement content in a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to file in /work"},
                    "target_content": {"type": "string", "description": "Exact text to find"},
                    "replacement_content": {"type": "string", "description": "Replacement text"}
                },
                "required": ["path", "target_content", "replacement_content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write entire content to a file in /work.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to file in /work"},
                    "content": {"type": "string", "description": "Complete file content"}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Call this when the bug fix is complete.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "Summary of the fix made"}
                },
                "required": ["summary"]
            }
        }
    }
]


def resolve_safe(rel_path: str) -> Path:
    p = (WORK_DIR / rel_path).resolve()
    if not str(p).startswith(str(WORK_DIR)):
        raise ValueError(f"Path escape disallowed: {rel_path}")
    return p


def execute_tool(name: str, args: dict) -> str:
    name = name.split(".")[-1]
    try:
        if name == "read_file":
            p_str = args.get("path", ".")
            target = resolve_safe(p_str)
            if not target.exists():
                return f"Error: file not found: {p_str}"
            if target.is_dir():
                items = sorted([f.name + ("/" if f.is_dir() else "") for f in target.iterdir()])
                return f"Directory listing of '{p_str}':\n" + "\n".join(items)
            return target.read_text(encoding="utf-8", errors="replace")

        elif name == "list_dir":
            p_str = args.get("path", ".")
            target = resolve_safe(p_str)
            if not target.exists():
                return f"Error: directory not found: {p_str}"
            if not target.is_dir():
                return f"Error: '{p_str}' is not a directory"
            items = sorted([f.name + ("/" if f.is_dir() else "") for f in target.iterdir()])
            return f"Directory listing of '{p_str}':\n" + "\n".join(items)

        elif name == "replace_file_content":
            p_str = args.get("path", "")
            target = resolve_safe(p_str)
            if not target.exists():
                return f"Error: file not found: {p_str}"
            content = target.read_text(encoding="utf-8", errors="replace")
            t = args.get("target_content", "")
            r = args.get("replacement_content", "")
            if not t:
                return "Error: target_content cannot be empty"
            if t not in content:
                return f"Error: target_content not found in {p_str}"
            if content.count(t) > 1:
                return f"Error: target_content matches multiple times in {p_str}. Provide more surrounding context."
            new_content = content.replace(t, r, 1)
            target.write_text(new_content, encoding="utf-8")
            return f"Success: replaced target_content in {p_str}"

        elif name == "write_file":
            p_str = args.get("path", "")
            target = resolve_safe(p_str)
            target.parent.mkdir(parents=True, exist_ok=True)
            content = args.get("content", "")
            target.write_text(content, encoding="utf-8")
            return f"Success: wrote {len(content)} chars to {p_str}"

        elif name == "finish":
            return f"Finished: {args.get('summary', 'Done')}"

        else:
            return f"Error: unknown tool {name}"
    except Exception as e:
        return f"Error executing {name}: {e}"


def call_gateway(gateway_url: str, model: str, messages: list[dict], use_tools: bool = True) -> dict:
    url = f"{gateway_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": 2048,
    }
    if use_tools:
        payload["tools"] = TOOLS
        payload["tool_choice"] = "auto"

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def extract_embedded_tool_calls(content: str) -> list[dict]:
    """Extract tool calls if model outputted them as text or JSON instead of API tool_calls."""
    calls = []
    patterns = [
        r'```(?:json)?\s*(\{\s*"name":\s*"(?:replace_file_content|write_file|read_file|finish|list_dir)".*?\})\s*```',
        r'(?:<tool_call>|"tool_call":\s*)(\{.*?\})(?:</tool_call>)?',
    ]
    for pat in patterns:
        for m in re.finditer(pat, content, re.DOTALL):
            try:
                obj = json.loads(m.group(1))
                if "name" in obj:
                    calls.append({"function": {"name": obj["name"], "arguments": json.dumps(obj.get("arguments", obj.get("parameters", {})))}})
            except Exception:
                pass
    return calls


def run_loop(model: str, gateway_url: str, prompt: str, max_turns: int = 10) -> dict:
    messages = [
        {
            "role": "system",
            "content": (
                "You are an expert engineer in a containerized environment fixing a defect.\n"
                "You have access to tools to read and modify files in /work.\n"
                "Work only on the defect. Make minimal targeted changes.\n"
                "When finished, call finish() with a summary of the fix."
            )
        },
        {"role": "user", "content": prompt}
    ]

    print(f"Starting agent with model={model} gateway={gateway_url}", flush=True)
    start_time = time.monotonic()
    turn = 0
    finished = False

    while turn < max_turns and not finished:
        turn += 1
        print(f"--- Turn {turn}/{max_turns} ---", flush=True)
        try:
            resp = call_gateway(gateway_url, model, messages, use_tools=True)
        except Exception as e:
            print(f"Gateway error with tools: {e}", file=sys.stderr)
            try:
                resp = call_gateway(gateway_url, model, messages, use_tools=False)
            except Exception as e2:
                print(f"Fatal gateway error: {e2}", file=sys.stderr)
                break

        choice = resp.get("choices", [{}])[0]
        msg = choice.get("message", {})
        messages.append(msg)

        tool_calls = msg.get("tool_calls", [])
        content = msg.get("content", "") or ""
        if content:
            print(f"[Model Content]: {content[:200]}...", flush=True)

        if not tool_calls and content:
            tool_calls = extract_embedded_tool_calls(content)

        if not tool_calls:
            print("No tool calls issued. Ending turn.", flush=True)
            break

        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            raw_args = fn.get("arguments", "{}")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except Exception:
                args = {}
            print(f"Tool call: {name}({str(args)[:80]})", flush=True)

            res = execute_tool(name, args)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", f"call_{turn}"),
                "name": name,
                "content": res
            })
            if name == "finish":
                finished = True
                break

    latency = round(time.monotonic() - start_time, 2)
    return {"turns": turn, "latency_s": latency, "finished": finished}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="space-bunny-alpha")
    ap.add_argument("--gateway", default="http://services:8000/v1")
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--max-turns", type=int, default=10)
    args = ap.parse_args()

    res = run_loop(args.model, args.gateway, args.prompt, args.max_turns)
    print(f"Agent finished: {json.dumps(res)}")


if __name__ == "__main__":
    main()
