#!/usr/bin/env python3
"""Local backend for BataCode. Python stdlib only.

Serves ./public, proxies chat to Ollama, and runs git inside a workspace
folder. Binds to 127.0.0.1 only.
"""
import base64
import binascii
import http.client
import io
import json
import mimetypes
import os
import re
import subprocess
import sys
import zipfile
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
MAX_UPLOAD = 21_000_000        # request body (base64) for /api/upload
MAX_UPLOAD_FILE = 15_000_000   # decoded size of one uploaded file
MAX_ZIP_TOTAL = 100_000_000    # max bytes zipped or unzipped in one go
MAX_ZIP_FILES = 5000

mimetypes.add_type("application/manifest+json", ".webmanifest")


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


# ---------- git helpers ----------

def git_env():
    return dict(
        os.environ,
        GIT_TERMINAL_PROMPT="0",
        GIT_ALLOW_PROTOCOL="https:ssh",
        GIT_SSH_COMMAND="ssh -o BatchMode=yes",
        LC_ALL="C",
    )


def git(args, cwd, timeout=300):
    env = git_env()
    try:
        p = subprocess.run(["git"] + args, cwd=str(cwd), env=env,
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "git timed out"}
    out = (p.stdout + p.stderr)
    if len(out) > MAX_OUT:
        out = out[:MAX_OUT] + "\n[truncated]"
    return {"ok": p.returncode == 0, "output": out}


def git_bytes(args, cwd, timeout=60):
    """Run git and return raw stdout bytes (for -z output)."""
    try:
        p = subprocess.run(["git"] + args, cwd=str(cwd), env=git_env(),
                           capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ApiError(504, "git timed out")
    if p.returncode != 0:
        raise ApiError(500, "git failed: " + p.stderr.decode("utf-8", "replace")[:200])
    return p.stdout


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
    res = git(["clone", "--", url, name], WORKSPACE, 900)
    res["name"] = name
    h.send_json(200, res)


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


def zip_files(root, changed):
    """Relative paths to zip: changed files only, or every non-ignored file."""
    seen = {}
    if changed:
        out = git_bytes(["status", "--porcelain", "-z", "-uall"], root)
        toks = out.split(b"\0")
        i = 0
        while i < len(toks):
            t = toks[i]
            i += 1
            if len(t) < 4:
                continue
            xy = t[:2].decode("ascii", "replace")
            if "R" in xy or "C" in xy:
                i += 1  # skip the original path of a rename or copy
            if "D" in xy:
                continue
            try:
                seen[t[3:].decode("utf-8")] = True
            except UnicodeDecodeError:
                pass
    else:
        out = git_bytes(["ls-files", "-z", "--cached", "--others", "--exclude-standard"], root)
        for t in out.split(b"\0"):
            if not t:
                continue
            try:
                seen[t.decode("utf-8")] = True
            except UnicodeDecodeError:
                pass
    return list(seen)


def r_zip(h, q):
    root = repo_path(q.get("repo"))
    changed = q.get("changed") == "1"
    files = zip_files(root, changed)
    buf = io.BytesIO()
    total = n = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in files:
            p = root / rel
            if p.is_symlink() or not p.is_file():
                continue
            total += p.stat().st_size
            if total > MAX_ZIP_TOTAL:
                raise ApiError(413, "too large to zip (max 100 MB)")
            z.write(p, rel)
            n += 1
    if n == 0:
        raise ApiError(404, "no changed files" if changed else "no files")
    base = re.sub(r"[^A-Za-z0-9._-]", "", q.get("name", "")) or (
        q.get("repo") + ("-changes" if changed else ""))
    base = re.sub(r"\.zip$", "", base, flags=re.I).lstrip(".-") or "download"
    data = buf.getvalue()
    h.send_response(200)
    h.send_header("Content-Type", "application/zip")
    h.send_header("Content-Disposition", 'attachment; filename="%s.zip"' % base)
    h.send_header("Content-Length", str(len(data)))
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    h.wfile.write(data)


def extract_zip(data, root, dest_rel):
    dest = safe_join(root, dest_rel, allow_root=True)
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ApiError(400, "not a valid zip file")
    done = skipped = 0
    with z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        if len(infos) > MAX_ZIP_FILES:
            raise ApiError(413, "too many files in zip")
        if sum(i.file_size for i in infos) > MAX_ZIP_TOTAL:
            raise ApiError(413, "zip is too large when unpacked")
        for i in infos:
            name = i.filename.replace("\\", "/")
            if name.startswith("__MACOSX/") or name.endswith(".DS_Store"):
                skipped += 1
                continue
            rel = (dest_rel + "/" + name) if dest_rel else name
            try:
                p = safe_join(root, rel)
                if p != dest and dest not in p.parents:
                    raise ApiError(400, "outside target folder")
                if p.is_dir():
                    raise ApiError(400, "is a directory")
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(z.read(i))
                done += 1
            except (ApiError, OSError):
                skipped += 1
    return done, skipped


def r_upload(h, q):
    d = h.read_json(MAX_UPLOAD)
    root = repo_path(d.get("repo"))
    name = str(d.get("name", "")).strip()
    if (not name or "/" in name or "\\" in name or "\x00" in name
            or name in (".", "..") or name == ".git"):
        raise ApiError(400, "bad file name")
    dest = safe_join(root, str(d.get("dir", "")).strip().strip("/"), allow_root=True)
    if dest.exists() and not dest.is_dir():
        raise ApiError(400, "target folder is a file")
    try:
        data = base64.b64decode(str(d.get("data", "")), validate=True)
    except (binascii.Error, ValueError):
        raise ApiError(400, "bad file data")
    if len(data) > MAX_UPLOAD_FILE:
        raise ApiError(413, "file too large (max 15 MB)")
    dest_rel = "" if dest == root else dest.relative_to(root).as_posix()
    if d.get("extract") and name.lower().endswith(".zip"):
        done, skipped = extract_zip(data, root, dest_rel)
        msg = "%s: extracted %d file(s)" % (name, done)
        if skipped:
            msg += ", skipped %d" % skipped
        h.send_json(200, {"ok": True, "output": msg})
        return
    p = safe_join(root, (dest_rel + "/" + name) if dest_rel else name)
    if p.is_dir():
        raise ApiError(400, "a folder with that name exists")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    h.send_json(200, {"ok": True, "output": "%s: saved (%d bytes)" % (name, len(data))})


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
    ("GET", "/api/zip"): r_zip,
    ("POST", "/api/upload"): r_upload,
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

    def read_json(self, limit=MAX_BODY):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
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
