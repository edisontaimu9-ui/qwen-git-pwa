#!/usr/bin/env python3
"""Local backend for BataCode. Python stdlib only.

Serves ./public, proxies chat to Ollama, and runs git inside a workspace
folder. Binds to 127.0.0.1 only.
"""
import http.client
import json
import mimetypes
import os
import re
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("PWA_PORT", "8787"))
OLLAMA = urlparse(os.environ.get("OLLAMA_URL", "http://localhost:11434"))
WORKSPACE = Path(os.environ.get("PWA_WORKSPACE", str(Path.home() / "batacode-workspace"))).resolve()
PUBLIC = (Path(__file__).parent / "public").resolve()
ALLOWED_HOSTS = {"localhost:%d" % PORT, "127.0.0.1:%d" % PORT}
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
URL_RE = re.compile(r"^(https://[^\s/]+/[^\s]+|git@[A-Za-z0-9.-]+:[^\s]+)$")
MAX_BODY = 3_000_000
MAX_FILE = 300_000
MAX_OUT = 60_000

mimetypes.add_type("application/manifest+json", ".webmanifest")


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


# ---------- git helpers ----------

def git(args, cwd, timeout=300):
    env = dict(
        os.environ,
        GIT_TERMINAL_PROMPT="0",
        GIT_ALLOW_PROTOCOL="https:ssh",
        GIT_SSH_COMMAND="ssh -o BatchMode=yes",
        LC_ALL="C",
    )
    try:
        p = subprocess.run(["git"] + args, cwd=str(cwd), env=env,
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "git timed out"}
    out = (p.stdout + p.stderr)
    if len(out) > MAX_OUT:
        out = out[:MAX_OUT] + "\n[truncated]"
    return {"ok": p.returncode == 0, "output": out}


def repo_path(name):
    if not NAME_RE.match(name or ""):
        raise ApiError(400, "bad repo name")
    p = WORKSPACE / name
    if not (p / ".git").exists():
        raise ApiError(404, "repo not found")
    return p


def safe_join(root, rel, allow_root=False):
    rel = rel or ""
    if "\x00" in rel or rel.startswith("/"):
        raise ApiError(400, "bad path")
    if not rel and not allow_root:
        raise ApiError(400, "path required")
    p = (root / rel).resolve()
    if p != root and root not in p.parents:
        raise ApiError(400, "path escapes repo")
    if ".git" in p.relative_to(root).parts:
        raise ApiError(400, ".git is off limits")
    return p


# ---------- routes ----------

def r_tags(h, q):
    proxy(h, "GET", "/api/tags", None)


def r_chat(h, q):
    d = h.read_json()
    if not isinstance(d.get("model"), str) or not isinstance(d.get("messages"), list):
        raise ApiError(400, "model and messages required")
    allowed = {k: d[k] for k in ("model", "messages", "stream", "options") if k in d}
    proxy(h, "POST", "/api/chat", json.dumps(allowed).encode())


def r_repos(h, q):
    items = []
    if WORKSPACE.exists():
        for p in sorted(WORKSPACE.iterdir()):
            if p.is_dir() and (p / ".git").exists() and NAME_RE.match(p.name):
                b = git(["rev-parse", "--abbrev-ref", "HEAD"], p, 20)
                items.append({"name": p.name, "branch": b["output"].strip() if b["ok"] else "?"})
    h.send_json(200, {"repos": items})


def r_clone(h, q):
    d = h.read_json()
    url = str(d.get("url", "")).strip()
    if not URL_RE.match(url):
        raise ApiError(400, "use an https:// or git@host:path URL")
    name = str(d.get("name", "")).strip()
    if not name:
        name = re.sub(r"\.git$", "", url.rstrip("/").split("/")[-1].split(":")[-1])
    if not NAME_RE.match(name):
        raise ApiError(400, "bad folder name")
    dest = WORKSPACE / name
    if dest.exists():
        raise ApiError(409, "folder already exists")
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    h.send_json(200, git(["clone", "--", url, name], WORKSPACE, 900))


def r_status(h, q):
    h.send_json(200, git(["status", "-sb"], repo_path(q.get("repo"))))


def r_diff(h, q):
    h.send_json(200, git(["diff", "HEAD"], repo_path(q.get("repo"))))


def r_commit(h, q):
    d = h.read_json()
    p = repo_path(d.get("repo"))
    msg = str(d.get("message", "")).strip()
    if not msg or len(msg) > 500:
        raise ApiError(400, "message required (max 500 chars)")
    a = git(["add", "-A"], p)
    if not a["ok"]:
        h.send_json(200, a)
        return
    h.send_json(200, git(["commit", "-m", msg], p))


def r_pull(h, q):
    p = repo_path(h.read_json().get("repo"))
    h.send_json(200, git(["pull", "--ff-only"], p, 600))


def r_push(h, q):
    p = repo_path(h.read_json().get("repo"))
    up = git(["rev-parse", "--abbrev-ref", "@{u}"], p, 20)
    args = ["push"] if up["ok"] else ["push", "-u", "origin", "HEAD"]
    h.send_json(200, git(args, p, 600))


def r_branch(h, q):
    d = h.read_json()
    p = repo_path(d.get("repo"))
    name = str(d.get("name", "")).strip()
    if not name or name.startswith("-"):
        raise ApiError(400, "bad branch name")
    chk = git(["check-ref-format", "--branch", name], p, 20)
    if not chk["ok"]:
        raise ApiError(400, "invalid branch name")
    h.send_json(200, git(["switch", "-c", name], p))


def r_files(h, q):
    root = repo_path(q.get("repo"))
    d = safe_join(root, q.get("path", ""), allow_root=True)
    if not d.is_dir():
        raise ApiError(400, "not a directory")
    items = []
    for e in sorted(d.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        if e.name == ".git":
            continue
        items.append({"name": e.name, "dir": e.is_dir()})
        if len(items) >= 500:
            break
    h.send_json(200, {"items": items})


def r_file_get(h, q):
    root = repo_path(q.get("repo"))
    p = safe_join(root, q.get("path", ""))
    if not p.is_file():
        raise ApiError(404, "file not found")
    if p.stat().st_size > MAX_FILE:
        raise ApiError(413, "file too large (max %d bytes)" % MAX_FILE)
    try:
        text = p.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise ApiError(415, "not a text file")
    h.send_json(200, {"content": text})


def r_file_post(h, q):
    d = h.read_json()
    root = repo_path(d.get("repo"))
    p = safe_join(root, str(d.get("path", "")))
    content = d.get("content")
    if not isinstance(content, str) or len(content) > 1_000_000:
        raise ApiError(400, "content must be a string under 1 MB")
    if p.is_dir():
        raise ApiError(400, "path is a directory")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    h.send_json(200, {"ok": True})


ROUTES = {
    ("GET", "/api/tags"): r_tags,
    ("POST", "/api/chat"): r_chat,
    ("GET", "/api/repos"): r_repos,
    ("POST", "/api/clone"): r_clone,
    ("GET", "/api/status"): r_status,
    ("GET", "/api/diff"): r_diff,
    ("POST", "/api/commit"): r_commit,
    ("POST", "/api/pull"): r_pull,
    ("POST", "/api/push"): r_push,
    ("POST", "/api/branch"): r_branch,
    ("GET", "/api/files"): r_files,
    ("GET", "/api/file"): r_file_get,
    ("POST", "/api/file"): r_file_post,
}


def proxy(h, method, path, body):
    try:
        conn = http.client.HTTPConnection(OLLAMA.hostname, OLLAMA.port or 11434, timeout=900)
        conn.request(method, path, body=body,
                     headers={"Content-Type": "application/json"} if body else {})
        resp = conn.getresponse()
    except OSError:
        raise ApiError(502, "Ollama unreachable. Is the SSH tunnel up?")
    try:
        h.send_response(resp.status)
        h.send_header("Content-Type", resp.getheader("Content-Type", "application/json"))
        h.send_header("Cache-Control", "no-store")
        h.send_header("Connection", "close")
        h.end_headers()
        h.close_connection = True
        while True:
            chunk = resp.read1(65536)
            if not chunk:
                break
            h.wfile.write(chunk)
            h.wfile.flush()
    finally:
        conn.close()


# ---------- HTTP handler ----------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "batacode"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def send_json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if code >= 400:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ApiError(413, "body too large")
        raw = self.rfile.read(n) if n else b"{}"
        try:
            d = json.loads(raw)
        except ValueError:
            raise ApiError(400, "bad JSON")
        if not isinstance(d, dict):
            raise ApiError(400, "JSON object expected")
        return d

    def guard(self, method):
        if self.headers.get("Host", "") not in ALLOWED_HOSTS:
            raise ApiError(403, "bad host")
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc not in ALLOWED_HOSTS:
            raise ApiError(403, "bad origin")
        if method == "POST" and self.headers.get("X-PWA") != "1":
            raise ApiError(403, "missing header")

    def static(self, path):
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        p = (PUBLIC / rel).resolve()
        if PUBLIC not in p.parents or not p.is_file():
            raise ApiError(404, "not found")
        data = p.read_bytes()
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def handle_req(self, method):
        try:
            self.guard(method)
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path.startswith("/api/"):
                fn = ROUTES.get((method, u.path))
                if not fn:
                    raise ApiError(404, "not found")
                fn(self, q)
            elif method == "GET":
                self.static(u.path)
            else:
                raise ApiError(405, "method not allowed")
        except ApiError as e:
            self.send_json(e.code, {"error": e.msg})
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as e:  # keep server alive
            sys.stderr.write("error: %r\n" % (e,))
            try:
                self.send_json(500, {"error": "server error"})
            except OSError:
                self.close_connection = True

    def do_GET(self):
        self.handle_req("GET")

    def do_POST(self):
        self.handle_req("POST")


def main():
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    print("BataCode: http://localhost:%d" % PORT)
    print("Workspace:    %s" % WORKSPACE)
    print("Ollama:       %s" % OLLAMA.geturl())
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
