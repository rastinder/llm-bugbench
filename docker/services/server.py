"""Services container server:
1. Port 8000: OpenAI-compatible LLM Gateway forwarding to LiteLLM proxy
2. Port 5000: Hidden Test Grader and Dummy mock services
3. Port 8888: Lightweight HTTP/HTTPS CONNECT proxy for opencode cloud models
"""
from __future__ import annotations

import json
import os
import select
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, "/srv")
from sandbox import build_sandbox, grader_env

LITELLM_BASE = os.environ.get("LITELLM_BASE", "https://aitshirts.in/litellm/v1").rstrip("/")
LITELLM_KEY = os.environ.get("LITELLM_KEY", "sk-litellm-vps-2026")

TASKS_PATH = Path("/srv/data/host_tasks.json")
SOURCES_PATH = Path("/srv/data/host_tasks_sources.json")

REPO_MAP = {
    "/home/ras/.local/bin": Path("/srv/repos/bin"),
    "/home/ras/pr-ai/quality": Path("/srv/repos/quality"),
    "/home/ras/marketplace-monitor": Path("/srv/repos/marketplace-monitor"),
    "/home/ras/llm-bugbench": Path("/srv/repos/llm-bugbench"),
}

MODEL_ALIASES = {
    "space-bunny-alpha": "openrouter-space-bunny-alpha",
    "openrouter-space-bunny-alpha": "openrouter-space-bunny-alpha",
    "gemini-3.8-high": "gemini-3.7-flash",
    "gemini-3.8-flash-high": "gemini-3.7-flash",
    "gemini-3.7-flash": "gemini-3.7-flash",
    "gemini-3.6-flash": "gemini-3.6-flash",
    "glm-5.2": "openrouter-glm-5.2",
    "openrouter-glm-5.2": "openrouter-glm-5.2",
    "glm-5.3": "openrouter-glm-5.3-flash",
    "openrouter-glm-5.3": "openrouter-glm-5.3-flash",
    "qwen-3.8": "openrouter-qwen-3.8",
    "openrouter-qwen-3.8": "openrouter-qwen-3.8",
    "northmini-code": "openrouter-north-mini-code",
    "north-mini-code": "openrouter-north-mini-code",
    "openrouter-north-mini-code": "openrouter-north-mini-code",
    "mimo-2.6": "mimo-v26-9b-toolcall",
    "mimo-2.6-flash": "mimo-v26-9b-toolcall",
    "auto": "auto",
}


def normalize_path(path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        parts = path.split("/")
        return "/" + "/".join(parts[3:])
    return path


def run_connect_proxy(port: int = 8888):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", port))
    srv.listen(50)
    print(f"Forwarding HTTP/CONNECT proxy running on 0.0.0.0:{port}")
    while True:
        cl, _ = srv.accept()
        threading.Thread(target=handle_proxy_client, args=(cl,), daemon=True).start()


def handle_proxy_client(cl):
    try:
        req = b""
        while b"\r\n\r\n" not in req:
            chunk = cl.recv(4096)
            if not chunk:
                break
            req += chunk
        lines = req.split(b"\r\n")
        first = lines[0].decode(errors="ignore")
        parts = first.split()
        if len(parts) < 2:
            return
        method, target = parts[0], parts[1]
        if method == "CONNECT":
            host, port = target.split(":")
            rem = socket.create_connection((host, int(port)), timeout=30)
            cl.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            pipe_sockets(cl, rem)
        else:
            if target.startswith("http://"):
                target = target[7:]
            host = target.split("/")[0]
            port = 80
            if ":" in host:
                host, port = host.split(":")
                port = int(port)
            rem = socket.create_connection((host, port), timeout=30)
            rem.sendall(req)
            pipe_sockets(cl, rem)
    except Exception:
        pass
    finally:
        cl.close()


def pipe_sockets(s1, s2):
    socks = [s1, s2]
    while True:
        r, _, _ = select.select(socks, [], [], 60)
        if not r:
            break
        for s in r:
            d = s.recv(16384)
            if not d:
                return
            (s2 if s is s1 else s1).sendall(d)


class LLMGatewayHandler(BaseHTTPRequestHandler):
    """Port 8000: OpenAI-compatible proxy."""

    def log_message(self, format, *args):
        sys.stderr.write(f"[gateway:8000] {args[0]} {args[1]}\n")

    def do_GET(self):
        path = normalize_path(self.path)
        if path == "/v1/models" or path == "/models":
            models = [
                {"id": k, "object": "model", "owned_by": "gateway"}
                for k in MODEL_ALIASES
            ]
            resp = json.dumps({"object": "list", "data": models}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"ok","gateway":"llm-proxy"}')

    def do_POST(self):
        path = normalize_path(self.path)
        if not (path.startswith("/v1/chat/completions") or path.startswith("/chat/completions")):
            self.send_response(404)
            self.end_headers()
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body)
        except Exception as e:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())
            return

        requested_model = payload.get("model", "")
        mapped_model = MODEL_ALIASES.get(requested_model, requested_model)
        payload["model"] = mapped_model

        def send_upstream(p):
            u_req = urllib.request.Request(
                f"{LITELLM_BASE}/chat/completions",
                data=json.dumps(p).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {LITELLM_KEY}",
                    "Content-Type": "application/json",
                    "User-Agent": "curl/8.5.0",
                },
                method="POST",
            )
            with urllib.request.urlopen(u_req, timeout=180) as resp:
                return resp.status, resp.headers.get("Content-Type", "application/json"), resp.read()

        try:
            status, ctype, data = send_upstream(payload)
        except urllib.error.HTTPError as e:
            if "glm-5.3" in requested_model or "glm-5.3" in mapped_model:
                try:
                    payload["model"] = "openrouter-glm-5.2"
                    status, ctype, data = send_upstream(payload)
                except Exception:
                    err_data = e.read()
                    self.send_response(e.code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(err_data)))
                    self.end_headers()
                    self.wfile.write(err_data)
                    return
            else:
                err_data = e.read()
                self.send_response(e.code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err_data)))
                self.end_headers()
                self.wfile.write(err_data)
                return
        except Exception as e:
            if "glm-5.3" in requested_model or "glm-5.3" in mapped_model:
                try:
                    payload["model"] = "openrouter-glm-5.2"
                    status, ctype, data = send_upstream(payload)
                except Exception as e2:
                    err_msg = json.dumps({"error": f"Gateway error: {e2}"}).encode()
                    self.send_response(502)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(err_msg)))
                    self.end_headers()
                    self.wfile.write(err_msg)
                    return
            else:
                err_msg = json.dumps({"error": f"Gateway error: {e}"}).encode()
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err_msg)))
                self.end_headers()
                self.wfile.write(err_msg)
                return

        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class GraderHandler(BaseHTTPRequestHandler):
    """Port 5000: Hidden Test Grader and Dummy Mock Services."""

    tasks_cache: dict = {}

    @classmethod
    def load_tasks(cls):
        if TASKS_PATH.exists() and SOURCES_PATH.exists():
            tasks = json.loads(TASKS_PATH.read_text())
            sources = {t["task_id"]: t for t in json.loads(SOURCES_PATH.read_text())}
            cls.tasks_cache = {t["task_id"]: {**t, **sources.get(t["task_id"], {})} for t in tasks}

    def log_message(self, format, *args):
        sys.stderr.write(f"[grader:5000] {args[0]} {args[1]}\n")

    def do_GET(self):
        path = normalize_path(self.path)
        if path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok","tasks_loaded":' + str(len(self.tasks_cache)).encode() + b'}')
            return

        if path.startswith("/dummy"):
            resp = json.dumps({"ok": True, "dummy": "response", "path": path}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        path = normalize_path(self.path)
        if path.startswith("/dummy"):
            resp = json.dumps({"ok": True, "dummy": "post_received"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
            return

        if path == "/grade":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
            except Exception as e:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
                return

            task_id = data.get("task_id")
            source = data.get("source", "")
            task = self.tasks_cache.get(task_id)
            if not task:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(json.dumps({"error": f"Unknown task {task_id}"}).encode())
                return

            res = self.execute_grade(task, source)
            resp = json.dumps(res).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
            return

        self.send_response(404)
        self.end_headers()

    def execute_grade(self, task: dict, source: str) -> dict:
        work = Path(f"/tmp/grade_{task['task_id']}_{int(time.time()*1000)}")
        shutil.rmtree(work, ignore_errors=True)
        try:
            repo_path = REPO_MAP.get(task.get("repo", ""), Path("/srv/repos/llm-bugbench"))
            if repo_path.exists():
                d = build_sandbox(repo_path, work)
            else:
                work.mkdir(parents=True, exist_ok=True)
                d = work

            mod_file = d / task["module"]
            mod_file.parent.mkdir(parents=True, exist_ok=True)
            mod_file.write_text(source)

            test_rel = task.get("test_rel", "test_recorded.py")
            test_file = d / test_rel
            test_file.parent.mkdir(parents=True, exist_ok=True)
            test_file.write_text(task["test_source"])

            for pycache in d.rglob("__pycache__"):
                shutil.rmtree(pycache, ignore_errors=True)

            focus_ids = task.get("focus_ids", [])
            if focus_ids:
                node_ids = [f"{test_rel}::{fid}" for fid in focus_ids]
            else:
                node_ids = [test_rel]

            cmd = [sys.executable, "-m", "pytest", *node_ids, "-q", "--no-header", "-p", "no:cacheprovider"]
            env = grader_env({"PYTHONPATH": str(d)})
            proc = subprocess.run(cmd, cwd=d, capture_output=True, text=True, timeout=90, env=env)

            fixed = (proc.returncode == 0)
            return {
                "task_id": task["task_id"],
                "fixed": fixed,
                "returncode": proc.returncode,
                "stdout": proc.stdout[-1500:],
                "stderr": proc.stderr[-1500:],
            }
        except subprocess.TimeoutExpired:
            return {"task_id": task["task_id"], "fixed": False, "returncode": -1, "stdout": "", "stderr": "pytest timeout"}
        except Exception as e:
            return {"task_id": task["task_id"], "fixed": False, "returncode": -2, "stdout": "", "stderr": str(e)}
        finally:
            shutil.rmtree(work, ignore_errors=True)


def main():
    GraderHandler.load_tasks()
    print(f"Loaded {len(GraderHandler.tasks_cache)} tasks for grading.")

    gw = HTTPServer(("0.0.0.0", 8000), LLMGatewayHandler)
    t_gw = threading.Thread(target=gw.serve_forever, daemon=True)
    t_gw.start()

    gr = HTTPServer(("0.0.0.0", 5000), GraderHandler)
    t_gr = threading.Thread(target=gr.serve_forever, daemon=True)
    t_gr.start()

    t_proxy = threading.Thread(target=run_connect_proxy, args=(8888,), daemon=True)
    t_proxy.start()

    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
