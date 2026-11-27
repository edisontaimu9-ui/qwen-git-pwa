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
import shlex
import signal
import socket
import subprocess
import sys
import threading
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
        GIT_LITERAL_PATHSPECS="1",
    )


def git(args, cwd, timeout=300):
    env = git_env()
    try:
        p = subprocess.run(["git"] + args, cwd=str(cwd), env=env,
                           capture_output=True, text=True, errors="replace", timeout=timeout)
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



# ---------- lightweight memory (one small JSON file) ----------
MEMORY_FILE = WORKSPACE / ".batacode-memory.json"
MEM_MAX_NOTES = 50
MEM_NOTE_MAX = 300


def load_memory():
    try:
        notes = json.loads(MEMORY_FILE.read_text()).get("notes", [])
        return [str(n) for n in notes][-MEM_MAX_NOTES:]
    except (OSError, ValueError, AttributeError):
        return []


# ---------- routes ----------

def r_memory_get(h, q):
    h.send_json(200, {"notes": load_memory()})


def r_memory_post(h, q):
    d = h.read_json()
    notes = d.get("notes")
    if not isinstance(notes, list):
        raise ApiError(400, "notes list required")
    clean = [str(n).strip()[:MEM_NOTE_MAX] for n in notes if str(n).strip()][-MEM_MAX_NOTES:]
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    MEMORY_FILE.write_text(json.dumps({"notes": clean}, indent=1))
    h.send_json(200, {"notes": clean})


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
    h.send_json(200, {"repos": items, "workspace": str(WORKSPACE).replace(str(Path.home()), "~", 1)})


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
    d = h.read_json()
    p = repo_path(d.get("repo"))
    h.send_json(200, git(["pull", "--rebase" if d.get("rebase") else "--ff-only"], p, 600))


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


def r_context(h, q):
    root = repo_path(q.get("repo"))
    out = git_bytes(["ls-files", "-z", "--cached", "--others", "--exclude-standard"], root)
    names = []
    for t in out.split(b"\0"):
        if t:
            try:
                names.append(t.decode("utf-8"))
            except UnicodeDecodeError:
                pass
    st = git(["status", "-sb"], root, 20)
    h.send_json(200, {
        "tree": "\n".join(names[:300]),
        "more": max(0, len(names) - 300),
        "status": st["output"],
    })


TEMPLATES = {
    "blank": {"README.md": "# {name}\n"},
    "python": {
        "README.md": "# {name}\n",
        "main.py": 'def main():\n    print("Hello from {name}")\n\n\nif __name__ == "__main__":\n    main()\n',
        ".gitignore": "__pycache__/\n*.pyc\n.venv/\n",
    },
    "node": {
        "README.md": "# {name}\n",
        "index.js": 'console.log("Hello from {name}");\n',
        "package.json": '{\n  "name": "{name}",\n  "version": "0.1.0",\n  "main": "index.js"\n}\n',
        ".gitignore": "node_modules/\n",
    },
    "web": {
        "README.md": "# {name}\n",
        "index.html": '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width,initial-scale=1">\n<title>{name}</title>\n<link rel="stylesheet" href="style.css">\n</head>\n<body>\n<h1>{name}</h1>\n<script src="app.js"></script>\n</body>\n</html>\n',
        "style.css": "body { font-family: system-ui, sans-serif; margin: 1rem; }\n",
        "app.js": 'console.log("{name} loaded");\n',
    },
}


def r_create(h, q):
    d = h.read_json()
    name = str(d.get("name", "")).strip()
    if not NAME_RE.match(name):
        raise ApiError(400, "bad project name (letters, digits, . _ - only)")
    tpl = TEMPLATES.get(str(d.get("template", "blank")))
    if tpl is None:
        raise ApiError(400, "unknown template")
    dest = WORKSPACE / name
    if dest.exists():
        raise ApiError(409, "folder already exists")
    dest.mkdir(parents=True)
    init = git(["init"], dest, 60)
    if not init["ok"]:
        h.send_json(200, init)
        return
    git(["symbolic-ref", "HEAD", "refs/heads/main"], dest, 20)
    for fname, body in tpl.items():
        (dest / fname).write_text(body.replace("{name}", name), encoding="utf-8")
    h.send_json(200, {"ok": True, "name": name,
                      "output": "Created %s with %d file(s). Commit when ready." % (name, len(tpl))})


def r_delete(h, q):
    d = h.read_json()
    root = repo_path(d.get("repo"))
    p = safe_join(root, str(d.get("path", "")))
    if not p.is_file():
        raise ApiError(404, "file not found")
    p.unlink()
    h.send_json(200, {"ok": True})


def r_search(h, q):
    term = q.get("q", "")
    if not term or len(term) > 200:
        raise ApiError(400, "search text required (max 200 chars)")
    if q.get("repo"):
        names = [q["repo"]]
        repo_path(q["repo"])
    else:
        names = [p.name for p in sorted(WORKSPACE.iterdir())
                 if p.is_dir() and (p / ".git").exists() and NAME_RE.match(p.name)] if WORKSPACE.exists() else []
    mode = "-E" if q.get("regex") == "1" else "-F"
    results = []
    capped = False
    for name in names:
        root = repo_path(name)
        try:
            p = subprocess.run(
                ["git", "grep", "-n", "-I", "-i", mode, "--untracked", "--no-color", "-z", "-e", term],
                cwd=str(root), env=git_env(), capture_output=True, timeout=30)
        except subprocess.TimeoutExpired:
            raise ApiError(504, "search timed out in " + name)
        if p.returncode == 1:
            continue
        if p.returncode != 0:
            raise ApiError(400, "bad search pattern" if mode == "-E" else "search failed")
        for line in p.stdout[:2_000_000].split(b"\n"):
            parts = line.split(b"\0", 2)
            if len(parts) != 3 or not parts[1].isdigit():
                continue
            results.append({
                "repo": name,
                "path": parts[0].decode("utf-8", "replace"),
                "line": int(parts[1]),
                "text": parts[2].decode("utf-8", "replace").strip()[:200],
            })
            if len(results) >= 300:
                capped = True
                break
        if capped:
            break
    h.send_json(200, {"results": results, "capped": capped})


def r_tree(h, q):
    root = repo_path(q.get("repo"))
    out = git_bytes(["ls-files", "-z", "--cached", "--others", "--exclude-standard"], root)
    top = {}
    n = 0
    for t in out.split(b"\0"):
        if not t:
            continue
        try:
            parts = t.decode("utf-8").split("/")
        except UnicodeDecodeError:
            continue
        node = top
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = None
        n += 1
    lines = []

    def walk(node, prefix):
        items = sorted(node.items(), key=lambda kv: (kv[1] is None, kv[0].lower()))
        for i, (k, v) in enumerate(items):
            if len(lines) >= 1500:
                return
            last = i == len(items) - 1
            lines.append(prefix + ("\u2514\u2500\u2500 " if last else "\u251c\u2500\u2500 ") + k + ("/" if v is not None else ""))
            if v is not None:
                walk(v, prefix + ("    " if last else "\u2502   "))

    walk(top, "")
    h.send_json(200, {"tree": root.name + "/\n" + "\n".join(lines), "files": n, "truncated": len(lines) >= 1500})


HASH_RE = re.compile(r"^[0-9a-f]{7,40}$")


def git_text(args, cwd, ok=(0,), timeout=60):
    """Run git, return decoded stdout. Raises ApiError unless exit code is in ok."""
    try:
        p = subprocess.run(["git"] + args, cwd=str(cwd), env=git_env(),
                           capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ApiError(504, "git timed out")
    if p.returncode not in ok:
        raise ApiError(500, "git failed: " + p.stderr.decode("utf-8", "replace")[:200])
    out = p.stdout.decode("utf-8", "replace")
    if len(out) > MAX_OUT:
        out = out[:MAX_OUT] + "\n[truncated]"
    return out


def has_head(root):
    try:
        p = subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=str(root),
                           env=git_env(), capture_output=True, timeout=20)
    except subprocess.TimeoutExpired:
        return False
    return p.returncode == 0


def parse_status(root):
    """{path: XY} for every changed or untracked file."""
    out = git_bytes(["status", "--porcelain", "-z", "-uall"], root)
    toks = out.split(b"\0")
    res = {}
    i = 0
    while i < len(toks):
        t = toks[i]
        i += 1
        if len(t) < 4:
            continue
        xy = t[:2].decode("ascii", "replace")
        if "R" in xy or "C" in xy:
            i += 1  # original path of a rename or copy
        try:
            res[t[3:].decode("utf-8")] = xy
        except UnicodeDecodeError:
            pass
    return res


def clean_paths(raw):
    if not isinstance(raw, list) or not raw or len(raw) > 2000:
        raise ApiError(400, "paths required")
    for r in raw:
        if not isinstance(r, str) or not r or "\x00" in r or r.startswith("/"):
            raise ApiError(400, "bad path")
        parts = r.split("/")
        if ".." in parts or parts[0] == ".git":
            raise ApiError(400, "bad path")
    return raw


def r_gitinfo(h, q):
    root = repo_path(q.get("repo"))
    changes = []
    for path, xy in sorted(parse_status(root).items(), key=lambda kv: kv[0].lower()):
        code = "?" if xy == "??" else (xy[0] if xy[0] != " " else xy[1])
        changes.append({"path": path, "code": code})
    cur = git_text(["symbolic-ref", "--short", "-q", "HEAD"], root, ok=(0, 1)).strip()
    local, remote = [], []
    for ref in git_text(["for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes"], root).splitlines():
        if ref.startswith("refs/heads/"):
            local.append(ref[len("refs/heads/"):])
        elif ref.startswith("refs/remotes/") and not ref.endswith("/HEAD"):
            remote.append(ref[len("refs/remotes/"):])
    if cur and cur not in local:
        local.insert(0, cur)
    have = set(local)
    remote = [r for r in remote if r.split("/", 1)[-1] not in have]
    st = git_text(["status", "-sb", "--untracked-files=no"], root, timeout=30).splitlines()
    line = st[0][3:] if st and st[0].startswith("## ") else (st[0] if st else "")
    h.send_json(200, {"branch": cur, "local": local, "remote": remote, "changes": changes, "line": line})


def r_filediff(h, q):
    root = repo_path(q.get("repo"))
    rel = q.get("path", "")
    clean_paths([rel])
    chg = parse_status(root)
    if rel not in chg:
        raise ApiError(404, "no changes in that file")
    if chg[rel] == "??":
        out = git_text(["diff", "--no-index", "--no-color", "--", "/dev/null", rel], root, ok=(0, 1))
    elif has_head(root):
        out = git_text(["diff", "HEAD", "--no-color", "--", rel], root)
    else:
        out = git_text(["diff", "--cached", "--no-color", "--", rel], root)
    h.send_json(200, {"diff": out})


def r_commit_files(h, q):
    d = h.read_json()
    root = repo_path(d.get("repo"))
    msg = str(d.get("message", "")).strip()
    if not msg or len(msg) > 500:
        raise ApiError(400, "message required (max 500 chars)")
    paths = clean_paths(d.get("paths"))
    chg = parse_status(root)
    for p in paths:
        if p not in chg:
            raise ApiError(400, "not a changed file: " + p)
    a = git(["add", "-A", "--"] + paths, root)
    if not a["ok"]:
        h.send_json(200, a)
        return
    args = ["commit", "-m", msg]
    if has_head(root):
        args += ["--"] + paths  # commit only the selected files
    h.send_json(200, git(args, root))


def r_discard(h, q):
    d = h.read_json()
    root = repo_path(d.get("repo"))
    paths = clean_paths(d.get("paths"))
    chg = parse_status(root)
    head = has_head(root)
    done = 0
    notes = []
    for rel in paths:
        xy = chg.get(rel)
        if xy is None:
            notes.append("skipped (no changes): " + rel)
            continue
        if xy == "??":
            p = root / rel
            try:
                if not p.is_symlink():
                    safe_join(root, rel)
                    if not p.is_file():
                        raise ApiError(400, "not a file")
                p.unlink()
                done += 1
            except (ApiError, OSError):
                notes.append("could not delete: " + rel)
            continue
        in_head = head and git(["cat-file", "-e", "HEAD:" + rel], root, 20)["ok"]
        r = git(["checkout", "HEAD", "--", rel], root) if in_head else git(["rm", "-f", "--", rel], root)
        if r["ok"]:
            done += 1
        else:
            notes.append(rel + ": " + r["output"].strip()[:150])
    msg = "Discarded changes in %d file(s)." % done
    if notes:
        msg += "\n" + "\n".join(notes)
    h.send_json(200, {"ok": done > 0 or not notes, "output": msg})


def r_log(h, q):
    root = repo_path(q.get("repo"))
    try:
        n = min(100, max(1, int(q.get("n", "30"))))
    except ValueError:
        n = 30
    if not has_head(root):
        h.send_json(200, {"commits": []})
        return
    out = git_text(["log", "-n", str(n), "--format=%H%x1f%an%x1f%ar%x1f%s%x1e"], root)
    commits = []
    for rec in out.split("\x1e"):
        f = rec.strip("\n").split("\x1f")
        if len(f) == 4:
            commits.append({"hash": f[0], "short": f[0][:7], "author": f[1], "when": f[2], "subject": f[3]})
    h.send_json(200, {"commits": commits})


def r_show(h, q):
    root = repo_path(q.get("repo"))
    hs = q.get("hash", "")
    if not HASH_RE.match(hs):
        raise ApiError(400, "bad commit id")
    h.send_json(200, {"text": git_text(["show", "--stat", "--patch", "--no-color", hs], root)})


def r_revert(h, q):
    d = h.read_json()
    root = repo_path(d.get("repo"))
    hs = str(d.get("hash", ""))
    if not HASH_RE.match(hs):
        raise ApiError(400, "bad commit id")
    r = git(["revert", "--no-edit", hs], root, 120)
    if not r["ok"]:
        git(["revert", "--abort"], root, 20)
        r["output"] += "\nNothing was reverted. Commit or discard your changes first, or resolve conflicts in a terminal."
    h.send_json(200, r)


def r_switch(h, q):
    d = h.read_json()
    root = repo_path(d.get("repo"))
    name = str(d.get("name", "")).strip()
    if not name or name.startswith("-") or not git(["check-ref-format", "--branch", name], root, 20)["ok"]:
        raise ApiError(400, "bad branch name")
    if git(["show-ref", "--verify", "--quiet", "refs/heads/" + name], root, 20)["ok"]:
        res = git(["switch", name], root)
    elif git(["show-ref", "--verify", "--quiet", "refs/remotes/" + name], root, 20)["ok"]:
        res = git(["switch", "--track", name], root)
    else:
        raise ApiError(404, "branch not found")
    h.send_json(200, res)


# ---------- AI development automation (server side tools) ----------
# These gather facts the model cannot see by itself: project layout, check and
# test results, static findings, dependency versions and relevant file text.
# The model call itself stays in the browser (same /api/chat proxy as the chat).
import ast
import concurrent.futures
import shutil
import time
import urllib.request

SKIP_DIRS = {"node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next", "vendor", ".tox"}
TEXT_SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".tar", ".woff",
                 ".woff2", ".ttf", ".otf", ".mp3", ".mp4", ".webm", ".so", ".pyc", ".class", ".jar", ".apk", ".lock"}
LANGS = {".py": "Python", ".js": "JavaScript", ".mjs": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
         ".jsx": "JavaScript", ".html": "HTML", ".css": "CSS", ".json": "JSON", ".sh": "Shell", ".md": "Markdown",
         ".go": "Go", ".rs": "Rust", ".java": "Java", ".c": "C", ".cpp": "C++", ".rb": "Ruby", ".php": "PHP",
         ".yml": "YAML", ".yaml": "YAML", ".toml": "TOML", ".sql": "SQL"}
MANIFESTS = {"package.json", "pyproject.toml", "requirements.txt", "setup.py", "setup.cfg", "Pipfile", "Cargo.toml",
             "go.mod", "pom.xml", "build.gradle", "Gemfile", "composer.json", "Makefile", "Dockerfile", "start.sh"}
STOP = set("the and for with that this from into make add fix new use using create when where what should would "
           "could have has are was not but can will all any file files code function feature bug".split())


def repo_files(root, limit=3000):
    """Text-like files git knows about (tracked + untracked, not ignored)."""
    out = git_bytes(["ls-files", "-z", "--cached", "--others", "--exclude-standard"], root)
    names = []
    for t in out.split(b"\0"):
        if not t:
            continue
        try:
            n = t.decode("utf-8")
        except UnicodeDecodeError:
            continue
        parts = n.split("/")
        if any(p in SKIP_DIRS for p in parts[:-1]) or Path(n).suffix.lower() in TEXT_SKIP_EXT:
            continue
        if n.endswith((".min.js", ".min.css", "package-lock.json")):
            continue
        fp = root / n
        try:
            if fp.is_symlink() or not fp.is_file():
                continue
        except OSError:
            continue
        names.append(n)
        if len(names) >= limit:
            break
    return names


def read_text(root, rel, cap=MAX_FILE):
    """Decoded text of a repo file, or None when it is binary, huge or unsafe."""
    try:
        p = safe_join(root, rel)
        if p.is_symlink() or not p.is_file() or p.stat().st_size > cap:
            return None
        data = p.read_bytes()
    except (ApiError, OSError):
        return None
    if b"\0" in data[:4096]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def clamp_budget(v, default=24000):
    try:
        return max(2000, min(120000, int(v)))
    except (TypeError, ValueError):
        return default


# 1. Project analysis -------------------------------------------------------

def r_analyze(h, q):
    root = repo_path(q.get("repo"))
    budget = clamp_budget(q.get("budget"), 12000)
    names = repo_files(root)
    langs, loc = {}, {}
    for n in names:
        lang = LANGS.get(Path(n).suffix.lower())
        if not lang:
            continue
        t = read_text(root, n, 1_000_000)
        if t is None:
            continue
        langs[lang] = langs.get(lang, 0) + 1
        loc[lang] = loc.get(lang, 0) + t.count("\n") + 1
    manifests = [n for n in names if Path(n).name in MANIFESTS and n.count("/") <= 1]
    entry = [n for n in names if Path(n).name in ("main.py", "app.py", "server.py", "index.js", "app.js", "index.html",
                                                  "main.js", "cli.py", "__main__.py", "manage.py") and n.count("/") <= 2]
    tests = [n for n in names if re.search(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*\.py$|_test\.py$|\.(test|spec)\.[jt]sx?$", n)]
    readme = next((n for n in names if n.lower() in ("readme.md", "readme.txt", "readme")), "")
    parts = []
    used = 0

    def add(path, cap):
        nonlocal used
        t = read_text(root, path)
        if t is None or used >= budget:
            return
        t = t[:min(cap, budget - used)]
        used += len(t)
        parts.append({"path": path, "head": t})

    if readme:
        add(readme, 2500)
    for n in manifests[:6]:
        add(n, 1500)
    for n in entry[:4]:
        add(n, 2500)
    h.send_json(200, {
        "repo": root.name,
        "files": len(names),
        "languages": sorted(({"name": k, "files": langs[k], "loc": loc[k]} for k in langs),
                            key=lambda x: -x["loc"]),
        "manifests": manifests, "entry": entry, "tests": tests[:40], "has_tests": bool(tests),
        "tree": "\n".join(names[:250]), "more": max(0, len(names) - 250),
        "key_files": parts,
    })


# 2. Bug finding: syntax check + tests ---------------------------------------

def syntax_problems(root, names):
    found = []
    node = shutil.which("node")
    js_checked = 0
    for n in names:
        ext = Path(n).suffix.lower()
        if ext == ".py":
            t = read_text(root, n, 1_000_000)
            if t is None:
                continue
            try:
                compile(t, n, "exec", dont_inherit=True)  # compile only, never runs the code
            except SyntaxError as e:
                found.append({"path": n, "line": e.lineno or 0, "msg": "SyntaxError: %s" % e.msg})
            except (ValueError, RecursionError, MemoryError) as e:
                found.append({"path": n, "line": 0, "msg": "cannot compile: %s" % e})
        elif ext == ".json":
            t = read_text(root, n, 1_000_000)
            if t is None:
                continue
            try:
                json.loads(t)
            except ValueError as e:
                found.append({"path": n, "line": getattr(e, "lineno", 0), "msg": "Invalid JSON: %s" % e})
        elif ext in (".js", ".mjs") and node and js_checked < 60:
            js_checked += 1
            try:
                p = subprocess.run([node, "--check", str(root / n)], capture_output=True, text=True,
                                   errors="replace", timeout=20)
            except (subprocess.TimeoutExpired, OSError):
                continue
            if p.returncode != 0:
                err = p.stderr.strip().splitlines()
                m = re.search(r":(\d+)\s*$", err[0]) if err else None
                msg = next((ln for ln in err if "Error" in ln), err[-1] if err else "syntax error")
                found.append({"path": n, "line": int(m.group(1)) if m else 0, "msg": msg[:200]})
    return found


def pick_test_command(root, names):
    """Return (command, extra_env) for the project's tests, or None."""
    py_tests = [n for n in names if re.search(r"(^|/)tests?/.*\.py$|(^|/)test_[^/]*\.py$|_test\.py$", n)]
    pj = root / "package.json"
    if pj.is_file():
        try:
            scripts = json.loads(pj.read_text(encoding="utf-8")).get("scripts", {})
        except (OSError, ValueError, AttributeError):
            scripts = {}
        if isinstance(scripts, dict) and scripts.get("test") and shutil.which("npm") and (root / "node_modules").is_dir():
            return ["npm", "test", "--silent"], {}
    if py_tests:
        py = shutil.which("python3") or sys.executable
        path = os.pathsep.join(str(x) for x in (root, root / "src") if x.exists())
        env = {"PYTHONPATH": path}
        probe = subprocess.run([py, "-c", "import pytest"], capture_output=True, timeout=20)
        if probe.returncode == 0:
            return [py, "-m", "pytest", "-x", "-q", "--no-header", "-p", "no:cacheprovider"], env
        start = "tests" if (root / "tests").is_dir() else ("test" if (root / "test").is_dir() else ".")
        pat = "test_*.py" if any(Path(n).name.startswith("test_") for n in py_tests) else "*_test.py"
        return [py, "-m", "unittest", "discover", "-s", start, "-p", pat, "-v"], env
    return None


def r_check(h, q):
    """POST: syntax check every source file and run the project's tests (runs repo code)."""
    d = h.read_json()
    root = repo_path(d.get("repo"))
    names = repo_files(root)
    syntax = syntax_problems(root, names)
    res = {"syntax": syntax, "files_checked": len(names), "tests": {"ran": False}}
    if d.get("run_tests", True):
        picked = pick_test_command(root, names)
        if picked:
            cmd, extra = picked
            env = dict(os.environ, CI="1", PYTHONDONTWRITEBYTECODE="1", LC_ALL="C", **extra)
            t0 = time.time()
            try:
                p = subprocess.run(cmd, cwd=str(root), env=env, capture_output=True, text=True,
                                   errors="replace", timeout=180, stdin=subprocess.DEVNULL)
                out = p.stdout + p.stderr
                ok = p.returncode == 0
            except subprocess.TimeoutExpired:
                out, ok = "Tests timed out after 180 s.", False
            except OSError as e:
                out, ok = "Could not run tests: %s" % e, False
            if len(out) > MAX_OUT:
                out = out[:MAX_OUT // 2] + "\n[... truncated ...]\n" + out[-MAX_OUT // 2:]
            res["tests"] = {"ran": True, "cmd": " ".join(cmd[1:] if cmd[0] != "npm" else cmd), "ok": ok,
                            "seconds": round(time.time() - t0, 1), "output": out}
    h.send_json(200, res)


# 3. Feature development / tests / docs: choose the files the model needs ----

def rank_files(root, names, query):
    words = {w for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", (query or "").lower()) if w not in STOP}
    scored = []
    for n in names:
        s = 0
        low = n.lower()
        for w in words:
            if w in low:
                s += 5
        t = read_text(root, n, 120_000) if words else None
        if t:
            tl = t.lower()
            for w in words:
                s += min(tl.count(w), 6)
        if Path(n).name in ("README.md", "package.json", "requirements.txt", "pyproject.toml"):
            s += 1
        if s:
            scored.append((s, n))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [n for _, n in scored]


def r_bundle(h, q):
    """POST: return file text for the task, chosen by explicit paths, changes, or a keyword query."""
    d = h.read_json()
    root = repo_path(d.get("repo"))
    budget = clamp_budget(d.get("budget"))
    names = repo_files(root)
    known = set(names)
    chosen = []
    paths = d.get("paths")
    if isinstance(paths, list) and paths:
        prefix = str(root) + "/"
        rel = []
        for x in paths[:200]:
            if isinstance(x, str):
                x = x[len(prefix):] if x.startswith(prefix) else (x[2:] if x.startswith("./") else x)
                if x and not x.startswith("/") and ".." not in x.split("/") and "\x00" not in x:
                    rel.append(x)
        for p in rel:
            if p in known and p not in chosen:
                chosen.append(p)
    scope = d.get("scope", "query")
    if scope == "changes":
        chg = parse_status(root)
        chosen += [p for p in sorted(chg) if p in known and p not in chosen]
    if scope in ("query", "changes") and len(chosen) < 12:
        chosen += [p for p in rank_files(root, names, str(d.get("query", ""))[:2000]) if p not in chosen][:12 - len(chosen)]
    if scope == "all" or (not chosen and scope != "paths"):
        pri = sorted(names, key=lambda n: (n.count("/"), n))
        chosen += [p for p in pri if p not in chosen]
    out, used, omitted = [], 0, 0
    for p in chosen:
        t = read_text(root, p)
        if t is None:
            continue
        if used >= budget:
            omitted += 1
            continue
        room = budget - used
        trunc = len(t) > room
        if trunc:
            t = t[:room]
        used += len(t)
        out.append({"path": p, "content": t, "truncated": trunc})
    h.send_json(200, {"files": out, "omitted": omitted, "chars": used})


def r_apply(h, q):
    """POST: write several files at once (all paths are validated before anything is written)."""
    d = h.read_json()
    root = repo_path(d.get("repo"))
    files = d.get("files")
    if not isinstance(files, list) or not files or len(files) > 40:
        raise ApiError(400, "1 to 40 files required")
    plan = []
    for f in files:
        if not isinstance(f, dict) or not isinstance(f.get("path"), str) or not isinstance(f.get("content"), str):
            raise ApiError(400, "each file needs path and content")
        if len(f["content"]) > 1_000_000:
            raise ApiError(400, "file too large: " + f["path"])
        rel = f["path"].strip()
        clean_paths([rel])
        p = safe_join(root, rel)
        if p.is_dir():
            raise ApiError(400, "path is a directory: " + rel)
        plan.append((rel, p, f["content"]))
    lines = []
    for rel, p, content in plan:
        existed = p.exists()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        lines.append(("updated " if existed else "created ") + rel)
    h.send_json(200, {"ok": True, "output": "\n".join(lines), "count": len(lines)})


# 4. Code review: static scan -------------------------------------------------

SCAN_RULES = [
    # (id, severity, languages or None, regex, message)
    ("secret-key", "high", None, r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----", "private key committed"),
    ("secret-aws", "high", None, r"\bAKIA[0-9A-Z]{16}\b", "AWS access key id"),
    ("secret-gh", "high", None, r"\bgh[pousr]_[A-Za-z0-9]{30,}\b", "GitHub token"),
    ("secret-assign", "high", None,
     r"(?i)\b(?:api[_-]?key|secret|passwd|password|token|auth)\w*\s*[:=]\s*[\"'][^\"'\s]{8,}[\"']",
     "hard-coded credential"),
    ("py-eval", "high", {".py"}, r"(?<![\w.])eval\s*\(", "eval() runs arbitrary code"),
    ("py-exec", "high", {".py"}, r"(?<![\w.])exec\s*\(", "exec() runs arbitrary code"),
    ("py-shell", "high", {".py"}, r"shell\s*=\s*True", "subprocess with shell=True allows command injection"),
    ("py-ossystem", "medium", {".py"}, r"\bos\.(?:system|popen)\s*\(", "os.system/popen builds shell commands"),
    ("py-pickle", "high", {".py"}, r"\bpickle\.loads?\s*\(", "pickle can execute code when loading untrusted data"),
    ("py-yaml", "medium", {".py"}, r"\byaml\.load\s*\((?![^)]*Loader)", "yaml.load without a safe Loader"),
    ("py-verify", "high", {".py"}, r"verify\s*=\s*False", "TLS certificate verification disabled"),
    ("py-weakhash", "medium", {".py"}, r"\bhashlib\.(?:md5|sha1)\s*\(", "weak hash (fine for checksums, not security)"),
    ("py-mktemp", "medium", {".py"}, r"\btempfile\.mktemp\s*\(", "mktemp is race-prone, use mkstemp"),
    ("py-bind", "medium", {".py"}, r"[\"']0\.0\.0\.0[\"']", "binds to all network interfaces"),
    ("py-sqlfmt", "high", {".py"}, r"\.execute\s*\(\s*(?:f[\"']|[\"'][^\"']*[\"']\s*(?:%|\.format|\+))",
     "SQL built from strings, use parameters"),
    ("js-eval", "high", {".js", ".mjs", ".ts", ".jsx", ".tsx"}, r"(?<![\w.])eval\s*\(|new\s+Function\s*\(", "eval/new Function runs strings as code"),
    ("js-innerhtml", "medium", {".js", ".mjs", ".ts", ".jsx", ".tsx", ".html"}, r"\.innerHTML\s*[+]?=|document\.write\s*\(|dangerouslySetInnerHTML",
     "HTML injection risk (XSS) if the text is not trusted"),
    ("js-exec", "high", {".js", ".mjs", ".ts"}, r"child_process|\bexecSync\s*\(", "spawns shell commands"),
    ("js-localstorage-secret", "medium", {".js", ".mjs", ".ts", ".jsx", ".tsx"},
     r"localStorage\.setItem\([^)]*(?i:token|password|secret)", "secret stored in localStorage"),
    ("sh-curlpipe", "high", {".sh"}, r"curl[^|\n]*\|\s*(?:sudo\s+)?(?:ba)?sh", "pipes a download straight into a shell"),
    ("sh-unquoted-rm", "medium", {".sh"}, r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f?\s+\$\w+", "rm -r on an unquoted variable"),
    ("todo", "low", None, r"\b(?:TODO|FIXME|HACK|XXX)\b", "unfinished work marker"),
]
SCAN_COMPILED = [(i, s, l, re.compile(rx), m) for i, s, l, rx, m in SCAN_RULES]
SEV_ORDER = {"high": 0, "medium": 1, "low": 2}


def py_quality(path, text):
    """Quality findings from the AST: long functions, bare except, mutable defaults, many arguments."""
    res = []
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return res
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            n = (node.end_lineno or node.lineno) - node.lineno + 1
            if n > 80:
                res.append((node.lineno, "medium", "long-function", "%s() is %d lines, consider splitting it" % (node.name, n)))
            a = node.args
            if len(a.args) + len(a.kwonlyargs) > 7:
                res.append((node.lineno, "low", "many-args", "%s() takes %d arguments" % (node.name, len(a.args) + len(a.kwonlyargs))))
            for dflt in list(a.defaults) + [x for x in a.kw_defaults if x is not None]:
                if isinstance(dflt, (ast.List, ast.Dict, ast.Set)):
                    res.append((dflt.lineno, "medium", "mutable-default", "mutable default argument in %s()" % node.name))
        elif isinstance(node, ast.ExceptHandler):
            if node.type is None:
                res.append((node.lineno, "medium", "bare-except", "bare except hides real errors and catches KeyboardInterrupt"))
            elif (isinstance(node.type, ast.Name) and node.type.id == "Exception" and len(node.body) == 1
                  and isinstance(node.body[0], ast.Pass)):
                res.append((node.lineno, "medium", "swallowed-error", "exception caught and ignored with pass"))
    return res


def scan_text(path, text):
    ext = Path(path).suffix.lower()
    found = []
    for i, line in enumerate(text.split("\n"), 1):
        if len(line) > 2000 or "batacode: ignore" in line or "nosec" in line:
            continue
        for rid, sev, langs, rx, msg in SCAN_COMPILED:
            if langs is not None and ext not in langs:
                continue
            if rx.search(line):
                found.append({"path": path, "line": i, "severity": sev, "rule": rid, "message": msg,
                              "text": line.strip()[:160]})
    if ext == ".py":
        for line, sev, rid, msg in py_quality(path, text):
            found.append({"path": path, "line": line, "severity": sev, "rule": rid, "message": msg, "text": ""})
    if text.count("\n") > 1000 and ext in LANGS:
        found.append({"path": path, "line": 1, "severity": "low", "rule": "big-file",
                      "message": "file has %d lines, consider splitting it" % (text.count("\n") + 1), "text": ""})
    return found


def r_review_scan(h, q):
    root = repo_path(q.get("repo"))
    names = repo_files(root)
    scope = q.get("scope", "changes")
    if scope == "changes":
        chg = parse_status(root)
        names = [n for n in names if n in chg]
    findings = []
    for n in names:
        t = read_text(root, n, 500_000)
        if t is not None:
            findings += scan_text(n, t)
    findings.sort(key=lambda f: (SEV_ORDER[f["severity"]], f["path"], f["line"]))
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in SEV_ORDER}
    h.send_json(200, {"scope": scope, "files": len(names), "counts": counts,
                      "findings": findings[:200], "capped": len(findings) > 200})


# 5. Dependency analysis -----------------------------------------------------

PY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
NPM_NAME_RE = re.compile(r"^(?:@[a-z0-9~][a-z0-9._~-]*/)?[a-z0-9~][a-z0-9._~-]*$")
VER_RE = re.compile(r"\d+(?:\.\d+)*")
_latest_cache = {}


def vtuple(s):
    m = VER_RE.search(s or "")
    return tuple(int(x) for x in m.group(0).split(".")[:4]) if m else ()


def parse_requirement(line):
    line = line.split("#", 1)[0].split(";", 1)[0].strip()
    if not line or line.startswith(("-", "git+", "http", ".", "/")):
        return None
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(.*)$", line)
    if not m:
        return None
    return m.group(1), m.group(2).strip()


def spec_kind(spec):
    if not spec:
        return "unpinned"
    if spec.startswith("==") and "*" not in spec:
        return "pinned"
    return "range"


def collect_deps(root):
    deps = []  # dicts: ecosystem, name, spec, source, dev
    for rel in repo_files(root):
        base = Path(rel).name
        if rel.count("/") > 2:
            continue
        text = read_text(root, rel, 500_000)
        if text is None:
            continue
        if re.match(r"^requirements[\w.-]*\.txt$", base):
            for ln in text.splitlines():
                r = parse_requirement(ln)
                if r:
                    deps.append({"eco": "pypi", "name": r[0], "spec": r[1], "source": rel,
                                 "dev": "dev" in base or "test" in base})
        elif base == "pyproject.toml":
            try:
                import tomllib
                data = tomllib.loads(text)
            except Exception:  # missing module (Python < 3.11) or bad file
                continue
            proj = data.get("project", {})
            for ln in proj.get("dependencies", []) or []:
                r = parse_requirement(str(ln))
                if r:
                    deps.append({"eco": "pypi", "name": r[0], "spec": r[1], "source": rel, "dev": False})
            for grp in (proj.get("optional-dependencies", {}) or {}).values():
                for ln in grp or []:
                    r = parse_requirement(str(ln))
                    if r:
                        deps.append({"eco": "pypi", "name": r[0], "spec": r[1], "source": rel, "dev": True})
            for name, spec in ((data.get("tool", {}).get("poetry", {}) or {}).get("dependencies", {}) or {}).items():
                if name.lower() != "python":
                    s = spec if isinstance(spec, str) else (spec.get("version", "") if isinstance(spec, dict) else "")
                    if s[:1] in ("^", "~"):
                        s = ">=" + s[1:]
                    deps.append({"eco": "pypi", "name": name, "spec": s, "source": rel, "dev": False})
        elif base == "package.json":
            try:
                data = json.loads(text)
            except ValueError:
                continue
            for key, dev in (("dependencies", False), ("devDependencies", True), ("peerDependencies", False)):
                block = data.get(key, {})
                if isinstance(block, dict):
                    for name, spec in block.items():
                        deps.append({"eco": "npm", "name": name, "spec": str(spec), "source": rel, "dev": dev})
    seen, uniq = set(), []
    for dep in deps:
        k = (dep["eco"], dep["name"].lower(), dep["spec"], dep["source"], dep["dev"])
        if k not in seen:
            seen.add(k)
            uniq.append(dep)
    return uniq[:120]


def fetch_json(url, first_key=None):
    """GET a small JSON document. With first_key, only that top-level object is parsed
    (PyPI puts "info" first and the rest of the document can be several MB)."""
    req = urllib.request.Request(url, headers={"User-Agent": "batacode-deps/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:  # nosec - only pypi.org / registry.npmjs.org
        if not first_key:
            return json.loads(r.read(2_000_000))
        buf, limit = b"", 4_000_000
        dec = json.JSONDecoder()
        while len(buf) < limit:
            chunk = r.read(131072)
            if not chunk:
                break
            buf += chunk
            text = buf.decode("utf-8", "ignore")
            i = text.find('"%s":' % first_key)
            if i >= 0:
                try:
                    return dec.raw_decode(text[i + len(first_key) + 3:].lstrip())[0]
                except ValueError:
                    continue  # object not complete yet, read more
        raise ValueError("unexpected response")


def latest_info(eco, name):
    key = (eco, name.lower())
    hit = _latest_cache.get(key)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    info = {"latest": "", "requires": "", "error": ""}
    try:
        if eco == "pypi":
            if not PY_NAME_RE.match(name):
                raise ValueError("bad name")
            j = fetch_json("https://pypi.org/pypi/%s/json" % name, "info")
            info["latest"] = j.get("version") or ""
            info["requires"] = j.get("requires_python") or ""
        else:
            if not NPM_NAME_RE.match(name):
                raise ValueError("bad name")
            j = fetch_json("https://registry.npmjs.org/%s/latest" % name.replace("/", "%2f"))
            info["latest"] = j.get("version") or ""
            eng = j.get("engines")
            info["requires"] = ("node " + eng["node"]) if isinstance(eng, dict) and eng.get("node") else ""
    except Exception as e:  # network or registry problem: report, do not fail the whole scan
        info["error"] = (type(e).__name__ + ": " + str(e))[:80]
    if not info["error"]:
        _latest_cache[key] = (time.time(), info)
    return info


def spec_allows(spec, ver):
    """Does a version spec (PEP 440 or npm style) accept ver (a tuple)? Unknown syntax counts as yes."""
    spec = (spec or "").strip()
    if not spec or spec in ("*", "latest"):
        return True
    ok = True
    for part in re.split(r"[,\s]+(?=[<>=!~^])|,", spec):
        part = part.strip()
        m = re.match(r"^(>=|<=|==|!=|~=|>|<|\^|~)?\s*v?(\d+(?:\.\d+)*)(\.[*x])?", part)
        if not m:
            continue
        op, want = m.group(1) or "==", tuple(int(x) for x in m.group(2).split("."))
        wild = bool(m.group(3))
        have = ver[:len(want)]
        if op == "==" and wild:
            res = ver[:len(want)] == want
        elif op == "==":
            res = ver[:len(want)] == want
        elif op == "!=":
            res = have != want
        elif op == ">=":
            res = ver >= want
        elif op == ">":
            res = ver > want
        elif op == "<=":
            res = have <= want
        elif op == "<":
            res = ver < want
        elif op == "~=":      # compatible release: ~=2.2 -> 2.*, ~=2.2.1 -> 2.2.*
            keep = max(1, len(want) - 1)
            res = ver >= want and ver[:keep] == want[:keep]
        elif op == "^":       # npm caret: first non-zero part must match
            nz = next((i for i, x in enumerate(want) if x), len(want) - 1)
            res = ver >= want and ver[:nz + 1] == want[:nz + 1]
        else:                 # npm tilde: minor must match
            keep = 2 if len(want) >= 2 else 1
            res = ver >= want and ver[:keep] == want[:keep]
        ok = ok and res
    return ok


def python_allows(spec, ver):
    return spec_allows(spec, ver)


def r_deps(h, q):
    root = repo_path(q.get("repo"))
    deps = collect_deps(root)
    pyver = sys.version_info[:3]
    pairs = {(d["eco"], d["name"]) for d in deps}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        looked = dict(zip(pairs, ex.map(lambda k: latest_info(*k), pairs))) if pairs else {}
    rows, issues = [], []
    pins = {}
    for d in deps:
        info = looked.get((d["eco"], d["name"]), {"latest": "", "requires": "", "error": ""})
        declared = vtuple(d["spec"])
        latest = vtuple(info["latest"])
        kind = spec_kind(d["spec"]) if d["eco"] == "pypi" else ("pinned" if re.match(r"^\d", d["spec"]) else
                                                                 ("unpinned" if d["spec"] in ("", "*", "latest") else "range"))
        behind = ""
        allows = spec_allows(d["spec"], latest) if latest and d["spec"] and kind != "unpinned" else True
        if declared and latest and (kind == "pinned" or not allows):
            if latest[0] > declared[0]:
                behind = "major"
            elif latest[:2] > declared[:2]:
                behind = "minor"
            elif latest > declared:
                behind = "patch"
        problem = []
        if kind == "unpinned":
            problem.append("no version constraint")
        if behind == "major":
            problem.append("a new major version exists, check the changelog for breaking changes")
        elif not allows and kind != "pinned":
            problem.append("latest %s is outside the allowed range %s" % (info["latest"], d["spec"]))
        if d["eco"] == "pypi" and info["requires"] and not python_allows(info["requires"], pyver):
            problem.append("latest release needs Python %s, this device runs %d.%d" % (info["requires"], pyver[0], pyver[1]))
        if re.search(r"^(?:git\+|https?:|file:|github:)", d["spec"] or ""):
            problem.append("installed from a URL, not a registry version")
        rows.append({"eco": d["eco"], "name": d["name"], "spec": d["spec"], "source": d["source"], "dev": d["dev"],
                     "kind": kind, "latest": info["latest"], "behind": behind, "requires": info["requires"],
                     "problem": "; ".join(problem), "error": info["error"]})
        if d["eco"] == "pypi" and kind == "pinned":
            pins.setdefault(d["name"].lower().replace("_", "-"), set()).add(d["spec"])
    for name, specs in pins.items():
        if len(specs) > 1:
            issues.append("%s is pinned to different versions: %s" % (name, ", ".join(sorted(specs))))
    both = {d["name"] for d in deps if d["eco"] == "npm" and not d["dev"]} & {d["name"] for d in deps if d["eco"] == "npm" and d["dev"]}
    for name in sorted(both):
        issues.append("%s is listed as both a dependency and a devDependency" % name)
    h.send_json(200, {
        "python": "%d.%d.%d" % pyver, "count": len(rows), "deps": rows, "issues": issues,
        "offline": bool(rows) and all(r["error"] for r in rows),
        "outdated": sum(1 for r in rows if r["behind"]),
    })


# ---------- patch management ----------
PATCH_STAT_LIMIT = 200  # +/- counts are computed for the first files only


def patch_slug(name, fallback="changes"):
    """Safe lowercase file stem for the ZIP, from the change name."""
    s = re.sub(r"[^a-z0-9._-]+", "-", str(name or "").lower().strip())
    s = re.sub(r"\.zip$", "", s)
    s = re.sub(r"-{2,}", "-", s).strip(".-_")[:60].strip(".-_")
    return s or fallback


def clean_line(s, limit):
    """One line of text without control characters (newlines, tabs, NUL)."""
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(s or "")).strip()[:limit].strip()


def shpath(p):
    """Quote a path for a shell but keep a leading ~/ working."""
    p = p.strip()
    if p == "~" or p == "~/":
        return "~"
    if p.startswith("~/"):
        return "~/" + shlex.quote(p[2:])
    return shlex.quote(p)


def patch_status(root):
    """Changed files: [{path, code, old}] where code is M A D R C U or ? (untracked)."""
    toks = git_bytes(["status", "--porcelain", "-z", "-uall"], root).split(b"\0")
    res = []
    i = 0
    while i < len(toks):
        t = toks[i]
        i += 1
        if len(t) < 4:
            continue
        xy = t[:2].decode("ascii", "replace")
        old = ""
        if "R" in xy or "C" in xy:
            src = toks[i] if i < len(toks) else b""
            i += 1
            if "R" in xy:
                old = src.decode("utf-8", "replace")
        try:
            path = t[3:].decode("utf-8")
        except UnicodeDecodeError:
            continue
        if xy == "??":
            code = "?"
        elif "D" in xy and not os.path.lexists(str(root / path)):
            code = "D"
        else:
            code = xy[0] if xy[0] != " " else xy[1]
            if code == "D":
                code = "M"
        res.append({"path": path, "code": code, "old": old})
    return sorted(res, key=lambda c: c["path"].lower())


def patch_counts(root, c, head):
    """(lines added, lines removed, is_binary) for one changed file."""
    if c["code"] == "D":
        return 0, 0, False
    if c["code"] == "?" or not head:
        p = root / c["path"]
        try:
            if p.is_symlink() or not p.is_file():
                return 0, 0, False
            data = p.read_bytes()[:2_000_000]
        except OSError:
            return 0, 0, False
        if b"\0" in data[:8000]:
            return 0, 0, True
        return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0), 0, False
    out = git_text(["diff", "HEAD", "--numstat", "--no-color", "--", c["path"]], root, ok=(0, 1))
    for line in out.splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        if parts[0] == "-":
            return 0, 0, True
        if parts[0].isdigit() and parts[1].isdigit():
            return int(parts[0]), int(parts[1]), False
    return 0, 0, False


def patch_commands(slug, branch, files, message, target, downloads):
    """The exact shell commands that install the ZIP and commit it."""
    adds = [c["path"] for c in files if c["code"] != "D"]
    removes = []
    for c in files:
        if c["code"] == "D":
            removes.append(c["path"])
        elif c["old"] and c["old"] != c["path"]:
            removes.append(c["old"])
    zip_path = downloads.rstrip("/") + "/" + slug + ".zip"
    cmds = ["cd " + shpath(target)]
    if adds:
        cmds.append("unzip -o " + shpath(zip_path))
    if removes:
        cmds.append("git rm -f -q --ignore-unmatch -- " + " ".join(shlex.quote(p) for p in removes))
    if adds:
        cmds.append("git add -- " + " ".join(shlex.quote(p) for p in adds))
    cmds.append("git status --short")
    cmds.append("git commit -m " + shlex.quote(message))
    cmds.append("git push -u origin " + shlex.quote(branch) if branch else "git push")
    return cmds


def r_patch_plan(h, q):
    """POST {repo, name, paths?, message?, request?, target?, downloads?}: review list and commands."""
    d = h.read_json()
    repo = d.get("repo")
    root = repo_path(repo)
    allc = patch_status(root)
    if d.get("paths") is not None:
        want = set(clean_paths(d.get("paths")))
        chosen = [c for c in allc if c["path"] in want]
        missing = sorted(want - {c["path"] for c in chosen})
        if missing:
            raise ApiError(400, "not a changed file: " + missing[0])
    else:
        chosen = allc
    head = has_head(root)
    files = []
    for n, c in enumerate(chosen):
        added = removed = 0
        binary = False
        if n < PATCH_STAT_LIMIT:
            added, removed, binary = patch_counts(root, c, head)
        files.append({"path": c["path"], "code": c["code"], "old": c["old"],
                      "added": added, "removed": removed, "binary": binary})
    slug = patch_slug(d.get("name"))
    branch = git_text(["symbolic-ref", "--short", "-q", "HEAD"], root, ok=(0, 1)).strip()
    message = (clean_line(d.get("message"), 72) or clean_line(d.get("request"), 72)
               or re.sub(r"[-_]+", " ", slug))
    target = clean_line(d.get("target"), 300) or "~/batacode-workspace/" + repo
    downloads = clean_line(d.get("downloads"), 300) or "~/storage/downloads"
    zip_count = sum(1 for c in files if c["code"] != "D")
    removes = [c["path"] for c in files if c["code"] == "D"] + [
        c["old"] for c in files if c["old"] and c["old"] != c["path"]]
    h.send_json(200, {
        "name": slug, "zip": slug + ".zip", "branch": branch, "message": message,
        "target": target, "downloads": downloads,
        "files": files, "zip_files": zip_count, "removed": removes,
        "commands": patch_commands(slug, branch, files, message, target, downloads) if files else [],
    })


def r_patch_zip(h, q):
    """POST {repo, name, paths}: a ZIP with only the ticked changed files (deleted ones cannot be zipped)."""
    d = h.read_json()
    root = repo_path(d.get("repo"))
    want = set(clean_paths(d.get("paths")))
    chosen = [c for c in patch_status(root) if c["path"] in want]
    missing = sorted(want - {c["path"] for c in chosen})
    if missing:
        raise ApiError(400, "not a changed file: " + missing[0])
    buf = io.BytesIO()
    total = n = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for c in chosen:
            if c["code"] == "D":
                continue
            p = root / c["path"]
            if p.is_symlink() or not p.is_file():
                continue
            total += p.stat().st_size
            if total > MAX_ZIP_TOTAL:
                raise ApiError(413, "too large to zip (max 100 MB)")
            z.write(p, c["path"])
            n += 1
    if n == 0:
        raise ApiError(404, "nothing to zip (only deletions selected)")
    data = buf.getvalue()
    h.send_response(200)
    h.send_header("Content-Type", "application/zip")
    h.send_header("Content-Disposition", 'attachment; filename="%s.zip"' % patch_slug(d.get("name")))
    h.send_header("Content-Length", str(len(data)))
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    h.wfile.write(data)



# ---------- terminal and server management ----------
SSH_HOST = os.environ.get("PWA_SSH_HOST", "ubuntu@41.75.113.54")
SSH_KEY = os.environ.get("PWA_SSH_KEY", str(Path.home() / ".ssh" / "server_ssh_key"))
TUNNEL_PORT = OLLAMA.port or 11434
TUNNEL_LOG = WORKSPACE / ".batacode-tunnel.log"
EXEC_MAX_SECONDS = 300
EXEC_MAX_BYTES = 1_000_000          # stop a command that prints more than this
EXEC_SLOTS = threading.Semaphore(2)  # at most two commands at once
_tunnel_proc = None                  # the ssh we started ourselves (kept so it can be reaped)

# Commands that only read and run with no approval. They run without a shell, so ; | > $() do nothing.
SAFE_COMMANDS = {
    "pwd", "ls", "df", "du", "free", "uptime", "ps", "date", "uname", "whoami", "id", "hostname",
    "wc", "head", "tail", "cat", "grep", "which", "echo", "tree", "nproc", "stat", "file",
}
SAFE_GIT = {"status", "log", "diff", "branch", "show", "remote", "stash", "blame", "shortlog", "describe"}
SHELL_META = re.compile(r"[;&|<>`$\n\\()*?{}]")
# Refused even when approved: they cannot be undone and are almost never what you want.
BLOCKED = [
    (re.compile(r"\bmkfs(\.\w+)?\b"), "formatting a disk"),
    (re.compile(r"\bdd\b[^;&|]*\bof=/dev/"), "writing straight to a device"),
    (re.compile(r">\s*/dev/(sd|mmcblk|nvme|block)"), "writing straight to a device"),
    (re.compile(r":\s*\(\s*\)\s*\{"), "a fork bomb"),
    (re.compile(r"(^|[;&|]\s*)(sudo\s+)?(shutdown|reboot|poweroff|halt)\b"), "shutting the machine down"),
    (re.compile(r"\bchmod\s+-R\s+\d+\s+/(\s|$)"), "changing permissions on /"),
]
WIPE_TARGETS = {"/", "/*", "~", "~/", "~/*", "$HOME", "$HOME/", "$HOME/*", "${HOME}", "${HOME}/", "${HOME}/*", ".."}


def wipes_everything(cmd):
    """True if any part of the command line is a recursive rm of /, the home folder or the parent folder."""
    for seg in re.split(r"[;&|\n]+", cmd):
        try:
            argv = shlex.split(seg)
        except ValueError:
            continue
        for i, a in enumerate(argv[:4]):
            if os.path.basename(a) != "rm":
                continue
            rest = argv[i + 1:]
            recursive = any(x in ("--recursive",) or (x.startswith("-") and not x.startswith("--") and ("r" in x or "R" in x))
                            for x in rest)
            if recursive and any(x in WIPE_TARGETS for x in rest):
                return True
    return False


def classify_command(cmd):
    """('blocked', why) | ('approve', why) | ('safe', '') for one command line."""
    cmd = cmd.strip()
    if not cmd:
        return "approve", "empty"
    for rx, why in BLOCKED:
        if rx.search(cmd):
            return "blocked", why
    if wipes_everything(cmd):
        return "blocked", "deleting /, your home folder or the parent folder"
    if SHELL_META.search(cmd):
        return "approve", "uses shell features"
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return "approve", "unbalanced quotes"
    if not argv:
        return "approve", "empty"
    if any(a.startswith("~") for a in argv):
        return "approve", "uses ~ (needs a shell)"
    head = argv[0]
    if head == "git":
        rest = [a for a in argv[1:] if not a.startswith("-")]
        if rest and rest[0] in SAFE_GIT and not any(a.startswith(("--ext-diff", "--output", "-c", "--exec")) for a in argv[1:]):
            if rest[0] == "branch" and any(a in argv for a in ("-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--move")):
                return "approve", "changes branches"
            if rest[0] in ("remote", "stash") and len(rest) > 1 and rest[1] not in ("-v", "show", "list", "get-url"):
                return "approve", "changes git state"
            return "safe", ""
        return "approve", "git command that may change things"
    if head in SAFE_COMMANDS:
        return "safe", ""
    if head in ("python3", "python", "node", "pip", "pip3", "npm") and argv[1:] in (["--version"], ["-V"], ["list"], ["-v"]):
        return "safe", ""
    return "approve", "not on the read-only list"


def exec_env():
    return dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_PAGER="cat", PAGER="cat", TERM="dumb",
                PYTHONDONTWRITEBYTECODE="1")


def kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass


def run_command(cmd, cwd, use_shell, timeout):
    """Run one command; returns {code, output, seconds, timed_out, truncated}. Output is capped."""
    shell = shutil.which("bash") or shutil.which("sh") or "/bin/sh"
    args = [shell, "-c", cmd] if use_shell else shlex.split(cmd)
    t0 = time.time()
    try:
        proc = subprocess.Popen(args, cwd=str(cwd), env=exec_env(), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    except FileNotFoundError:
        return {"code": 127, "output": "command not found: %s" % args[0], "seconds": 0.0,
                "timed_out": False, "truncated": False}
    except OSError as e:
        return {"code": 126, "output": "could not run: %s" % e, "seconds": 0.0, "timed_out": False, "truncated": False}
    buf = bytearray()
    state = {"cut": False}

    def pump():
        while True:
            chunk = proc.stdout.read1(65536) if hasattr(proc.stdout, "read1") else proc.stdout.read(65536)
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > EXEC_MAX_BYTES:
                state["cut"] = True
                kill_group(proc)
                break

    th = threading.Thread(target=pump, daemon=True)
    th.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_group(proc)
        proc.wait()
    th.join(2)
    try:
        proc.stdout.close()
    except OSError:
        pass
    text = bytes(buf).decode("utf-8", "replace")
    truncated = state["cut"] or len(text) > MAX_OUT
    if len(text) > MAX_OUT:
        text = "[earlier output cut]\n" + text[-MAX_OUT:]
    return {"code": proc.returncode, "output": text, "seconds": round(time.time() - t0, 1),
            "timed_out": timed_out, "truncated": truncated}


def r_exec(h, q):
    """POST {cmd, cwd?, approved?, timeout?}. Read-only commands run at once, anything else needs approved=true."""
    d = h.read_json()
    cmd = d.get("cmd")
    if not isinstance(cmd, str) or not cmd.strip() or len(cmd) > 4000 or "\x00" in cmd:
        raise ApiError(400, "cmd required (max 4000 characters)")
    cmd = cmd.strip()
    cwd_name = d.get("cwd") or ""
    cwd = repo_path(cwd_name) if cwd_name else WORKSPACE
    kind, why = classify_command(cmd)
    if kind == "blocked":
        raise ApiError(403, "blocked, this looks like %s" % why)
    if kind == "approve" and d.get("approved") is not True:
        h.send_json(409, {"needs_approval": True, "reason": why, "cmd": cmd})
        return
    try:
        timeout = max(1, min(EXEC_MAX_SECONDS, int(d.get("timeout") or 60)))
    except (TypeError, ValueError):
        timeout = 60
    if not EXEC_SLOTS.acquire(blocking=False):
        raise ApiError(429, "two commands are already running, wait for one to finish")
    try:
        res = run_command(cmd, cwd, kind == "approve", timeout)
    finally:
        EXEC_SLOTS.release()
    res.update({"ok": res["code"] == 0, "cmd": cmd, "cwd": cwd_name or ".", "approved": kind == "approve"})
    h.send_json(200, res)


# --- this device ---

def read_meminfo():
    info = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _, v = line.partition(":")
            m = re.match(r"\s*(\d+)", v)
            if m:
                info[k] = int(m.group(1)) * 1024
    except OSError:
        pass
    return info


def r_sysinfo(h, q):
    """GET: CPU load, memory, disk, uptime of the device running this app."""
    mem = read_meminfo()
    total = mem.get("MemTotal")
    avail = mem.get("MemAvailable", mem.get("MemFree"))
    try:
        load = [round(x, 2) for x in os.getloadavg()]
    except (OSError, AttributeError):
        load = None
    try:
        up = float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        up = None
    try:
        du = shutil.disk_usage(str(WORKSPACE))
        disk = {"total": du.total, "used": du.used, "free": du.free}
    except OSError:
        disk = None
    h.send_json(200, {
        "cpus": os.cpu_count(), "load": load, "uptime": up,
        "mem_total": total, "mem_available": avail,
        "swap_total": mem.get("SwapTotal"), "swap_free": mem.get("SwapFree"),
        "disk": disk, "workspace": str(WORKSPACE), "python": sys.version.split()[0],
        "pid": os.getpid(), "time": time.strftime("%Y-%m-%d %H:%M:%S"),
    })


# --- Ollama ---

def ollama_get(path, timeout=3):
    conn = http.client.HTTPConnection(OLLAMA.hostname, OLLAMA.port or 11434, timeout=timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read(2_000_000)
        if resp.status != 200:
            raise OSError("HTTP %d" % resp.status)
        return json.loads(body)
    finally:
        conn.close()


def r_ollama_status(h, q):
    """GET: is Ollama reachable, how fast, which models exist and which are loaded in memory."""
    res = {"url": OLLAMA.geturl(), "up": False, "latency_ms": None, "version": "", "models": [], "running": [], "error": ""}
    t0 = time.time()
    try:
        tags = ollama_get("/api/tags")
        res["up"] = True
        res["latency_ms"] = int((time.time() - t0) * 1000)
        res["models"] = [{"name": m.get("name", ""), "size": m.get("size", 0)} for m in tags.get("models", [])]
    except (OSError, ValueError, http.client.HTTPException) as e:
        res["error"] = "%s: %s" % (type(e).__name__, e) if str(e) else type(e).__name__
        h.send_json(200, res)
        return
    for key, path in (("version", "/api/version"), ("running", "/api/ps")):
        try:
            j = ollama_get(path)
            if key == "version":
                res["version"] = str(j.get("version", ""))
            else:
                res["running"] = [{"name": m.get("name", ""), "size": m.get("size", 0),
                                   "size_vram": m.get("size_vram", 0), "expires_at": m.get("expires_at", "")}
                                  for m in j.get("models", [])]
        except (OSError, ValueError, http.client.HTTPException):
            pass  # older Ollama versions have no /api/ps
    h.send_json(200, res)


# --- the server behind the SSH tunnel ---

REMOTE_STATS = (
    "uptime; echo; free -m; echo; df -h / 2>/dev/null; echo; "
    "(nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu "
    "--format=csv,noheader 2>/dev/null || echo 'no GPU info (nvidia-smi not found)'); echo; "
    "ps -eo pid,pcpu,pmem,comm --sort=-pcpu | head -6"
)


def ssh_base():
    return ["ssh", "-i", SSH_KEY, "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3"]


def run_ssh(remote_cmd, timeout=25):
    """Run a fixed command on the server over SSH. Returns (ok, text)."""
    if not Path(SSH_KEY).is_file():
        return False, "SSH key not found: %s (set PWA_SSH_KEY)" % SSH_KEY
    try:
        p = subprocess.run(ssh_base() + [SSH_HOST, remote_cmd], capture_output=True, text=True,
                           errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return False, "SSH timed out after %d s" % timeout
    except FileNotFoundError:
        return False, "ssh is not installed (pkg install openssh)"
    return p.returncode == 0, (p.stdout + p.stderr)[:MAX_OUT]


def r_server_info(h, q):
    """GET: CPU, memory, disk, GPU and top processes on the server (a fixed read-only command over SSH)."""
    ok, text = run_ssh(REMOTE_STATS)
    h.send_json(200, {"ok": ok, "host": SSH_HOST, "output": text})


# --- SSH tunnel to Ollama ---

def tunnel_pids(port, proc_dir="/proc"):
    """PIDs of ssh processes that forward local `port` (found from their command lines)."""
    pids = []
    try:
        entries = os.listdir(proc_dir)
    except OSError:
        return pids
    for name in entries:
        if not name.isdigit():
            continue
        try:
            raw = Path(proc_dir, name, "cmdline").read_bytes()
        except OSError:
            continue
        argv = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
        if not argv or os.path.basename(argv[0]) != "ssh":
            continue
        for i, a in enumerate(argv):
            spec = argv[i + 1] if a == "-L" and i + 1 < len(argv) else (a[2:] if a.startswith("-L") and len(a) > 2 else "")
            if spec.startswith("%d:" % port) or spec.startswith("localhost:%d:" % port) or spec.startswith("127.0.0.1:%d:" % port):
                pids.append(int(name))
                break
    return sorted(pids)


def port_listening(port, host="127.0.0.1"):
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except OSError:
        return False


def tunnel_status():
    global _tunnel_proc
    if _tunnel_proc is not None and _tunnel_proc.poll() is not None:
        _tunnel_proc = None  # reaped
    local = OLLAMA.hostname in ("localhost", "127.0.0.1", "::1")
    log = ""
    try:
        log = "\n".join(TUNNEL_LOG.read_text(errors="replace").strip().splitlines()[-6:])
    except OSError:
        pass
    return {
        "port": TUNNEL_PORT, "local": local, "listening": port_listening(TUNNEL_PORT) if local else None,
        "pids": tunnel_pids(TUNNEL_PORT), "host": SSH_HOST, "key_found": Path(SSH_KEY).is_file(),
        "started_here": _tunnel_proc is not None, "log": log,
    }


def tunnel_start():
    global _tunnel_proc
    st = tunnel_status()
    if not st["local"]:
        raise ApiError(400, "OLLAMA_URL is not local, so no tunnel is needed")
    if st["listening"]:
        return "Port %d is already in use (tunnel looks up). Use Restart to replace it." % TUNNEL_PORT
    if not st["key_found"]:
        raise ApiError(400, "SSH key not found: %s (set PWA_SSH_KEY)" % SSH_KEY)
    if shutil.which("ssh") is None:
        raise ApiError(400, "ssh is not installed (pkg install openssh)")
    args = ssh_base() + ["-N", "-o", "ExitOnForwardFailure=yes", "-L", "%d:localhost:%d" % (TUNNEL_PORT, 11434), SSH_HOST]
    try:
        TUNNEL_LOG.parent.mkdir(parents=True, exist_ok=True)
        logf = open(TUNNEL_LOG, "w")
    except OSError:
        logf = subprocess.DEVNULL
    try:
        _tunnel_proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=logf, stderr=logf,
                                        start_new_session=True)
    finally:
        if logf is not subprocess.DEVNULL:
            logf.close()
    for _ in range(40):  # wait up to ~10 s for the forward to come up
        time.sleep(0.25)
        if _tunnel_proc.poll() is not None:
            break
        if port_listening(TUNNEL_PORT):
            return "Tunnel started on port %d." % TUNNEL_PORT
    if _tunnel_proc.poll() is not None:
        _tunnel_proc = None
        err = ""
        try:
            err = TUNNEL_LOG.read_text(errors="replace").strip()[-400:]
        except OSError:
            pass
        raise ApiError(502, "ssh exited. " + (err or "No error text. Check the key and the server address."))
    return "ssh is running but port %d is not open yet. Check the status again in a few seconds." % TUNNEL_PORT


def tunnel_stop():
    global _tunnel_proc
    pids = tunnel_pids(TUNNEL_PORT)
    if _tunnel_proc is not None and _tunnel_proc.pid not in pids and _tunnel_proc.poll() is None:
        pids.append(_tunnel_proc.pid)
    if not pids:
        return "No tunnel process found."
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
    for _ in range(20):
        time.sleep(0.2)
        if not tunnel_pids(TUNNEL_PORT):
            break
    if _tunnel_proc is not None:
        try:
            _tunnel_proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        _tunnel_proc = None
    return "Stopped %d tunnel process%s." % (len(pids), "" if len(pids) == 1 else "es")


def r_tunnel_get(h, q):
    h.send_json(200, tunnel_status())


def r_tunnel_post(h, q):
    d = h.read_json()
    act = d.get("action")
    if act not in ("start", "stop", "restart"):
        raise ApiError(400, "action must be start, stop or restart")
    if act == "stop":
        msg = tunnel_stop()
    elif act == "start":
        msg = tunnel_start()
    else:
        stopped = tunnel_stop()
        msg = stopped + " " + tunnel_start()
    h.send_json(200, {"message": msg, "status": tunnel_status()})



# 6. Development intelligence: architecture graph, API endpoints, technical debt ----

CODE_EXT = {".py", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".html", ".htm"}


def _module_name(path):
    p = Path(path)
    return str(p.with_suffix("")).replace("/", ".")


def build_arch(root, names):
    """File-level import graph (Python, JS/TS, HTML script tags) resolved inside the repo."""
    files = [n for n in names if Path(n).suffix.lower() in CODE_EXT]
    fset = set(files)
    pymods = {}
    for n in files:
        if n.endswith(".py"):
            m = _module_name(n)
            pymods[m] = n
            if m.endswith(".__init__"):
                pymods[m[:-9]] = n
    edges = set()

    def js_resolve(src, spec):
        base = Path(src).parent / spec
        parts = []
        for x in base.parts:
            if x == "..":
                if parts:
                    parts.pop()
            elif x != ".":
                parts.append(x)
        b = "/".join(parts)
        for cand in (b, b + ".js", b + ".mjs", b + ".ts", b + ".tsx", b + ".jsx", b + "/index.js", b + "/index.ts"):
            if cand in fset:
                return cand
        return None

    for n in files:
        t = read_text(root, n, 400_000)
        if t is None:
            continue
        ext = Path(n).suffix.lower()
        if ext == ".py":
            try:
                tree = ast.parse(t)
            except (SyntaxError, ValueError, RecursionError):
                continue
            here = _module_name(n).split(".")
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        pkg = here[:-1]
                        pkg = pkg[:max(0, len(pkg) - (node.level - 1))]
                        base = ".".join(pkg + ([node.module] if node.module else []))
                    else:
                        base = node.module or ""
                    subs = [base + "." + a.name for a in node.names if base]
                    mods = subs if any(x in pymods for x in subs) else [base] + subs
                for m in mods:
                    tgt = pymods.get(m)
                    if not tgt and "." in m:
                        tgt = pymods.get(m.rsplit(".", 1)[0]) if m.rsplit(".", 1)[0] in pymods else None
                    if not tgt:  # top-level script next to the importer
                        cand = str(Path(n).parent / (m.split(".")[0] + ".py")).lstrip("./")
                        tgt = cand if cand in fset else None
                    if tgt and tgt != n:
                        edges.add((n, tgt))
        elif ext in (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"):
            for m in re.finditer(r"""(?:\bfrom\s+|\bimport\s*\(?\s*|\brequire\s*\(\s*)['"](\.{1,2}/[^'"]+)['"]""", t):
                tgt = js_resolve(n, m.group(1))
                if tgt and tgt != n:
                    edges.add((n, tgt))
        else:
            for m in re.finditer(r"""<script[^>]+src=["']([^"':]+)["']""", t, re.I):
                tgt = js_resolve(n, m.group(1)) or js_resolve(n, "./" + m.group(1))
                if tgt and tgt != n:
                    edges.add((n, tgt))
    return files, sorted(edges)


def arch_mermaid(files, edges, limit=40):
    deg = {}
    for a, b in edges:
        deg[a] = deg.get(a, 0) + 1
        deg[b] = deg.get(b, 0) + 1
    keep = set(sorted(files, key=lambda f: (-deg.get(f, 0), f))[:limit])
    keep = {f for f in keep if deg.get(f)} or keep
    ids = {f: "n%d" % i for i, f in enumerate(sorted(keep))}
    lines = ["flowchart LR"]
    for f, i in ids.items():
        lines.append('  %s["%s"]' % (i, f.replace('"', "")))
    for a, b in edges:
        if a in ids and b in ids:
            lines.append("  %s --> %s" % (ids[a], ids[b]))
    return "\n".join(lines)


def r_arch(h, q):
    root = repo_path(q.get("repo"))
    names = repo_files(root)
    files, edges = build_arch(root, names)
    indeg, outdeg = {}, {}
    for a, b in edges:
        outdeg[a] = outdeg.get(a, 0) + 1
        indeg[b] = indeg.get(b, 0) + 1
    # Cycles (simple DFS) are a real architecture smell worth reporting.
    adj = {}
    for a, b in edges:
        adj.setdefault(a, []).append(b)
    cycles, state = [], {}

    def dfs(n, stack):
        state[n] = 1
        stack.append(n)
        for m in adj.get(n, []):
            if state.get(m) == 1 and len(cycles) < 10:
                cycles.append(stack[stack.index(m):] + [m])
            elif m not in state:
                dfs(m, stack)
        stack.pop()
        state[n] = 2

    old = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old, 5000))
    try:
        for n in sorted(adj):
            if n not in state:
                dfs(n, [])
    finally:
        sys.setrecursionlimit(old)
    folders = {}
    for f in files:
        d = str(Path(f).parent) if "/" in f else "."
        folders[d] = folders.get(d, 0) + 1
    h.send_json(200, {
        "repo": root.name, "files": len(files), "edges": [{"from": a, "to": b} for a, b in edges[:600]],
        "most_imported": sorted(({"path": k, "count": v} for k, v in indeg.items()), key=lambda x: -x["count"])[:10],
        "most_dependent": sorted(({"path": k, "count": v} for k, v in outdeg.items()), key=lambda x: -x["count"])[:10],
        "isolated": [f for f in files if f not in indeg and f not in outdeg][:30],
        "cycles": cycles, "folders": folders, "mermaid": arch_mermaid(files, edges),
    })


ROUTE_RULES = [
    # (framework, regex with groups: method?, path)
    ("flask/fastapi", re.compile(r"""@\w+\.(get|post|put|delete|patch|head|options)\(\s*['"]([^'"]+)['"]""")),
    ("flask", re.compile(r"""@\w+\.route\(\s*['"]([^'"]+)['"](?:[^)]*methods\s*=\s*\[([^\]]*)\])?""")),
    ("express", re.compile(r"""\b(?:app|router|server|api)\.(get|post|put|delete|patch|all|use)\(\s*['"`](/[^'"`]*)['"`]""")),
    ("django", re.compile(r"""\b(?:re_)?path\(\s*r?['"]([^'"]*)['"]\s*,\s*([\w.]+)""")),
    ("route table", re.compile(r"""\(\s*['"](GET|POST|PUT|DELETE|PATCH)['"]\s*,\s*['"](/[^'"]*)['"]\s*\)\s*:\s*([\w.]+)""")),
]
CALL_RE = re.compile(r"""(?:fetch|api|axios\.\w+|\$\.\w+|requests\.\w+|http\.\w+)\(\s*[`'"]([^`'"?$]*/[^`'"?$]*)""")


def find_endpoints(root, names):
    eps, calls = [], []
    for n in names:
        if Path(n).suffix.lower() not in CODE_EXT:
            continue
        t = read_text(root, n, 600_000)
        if t is None:
            continue
        for i, line in enumerate(t.split("\n"), 1):
            for fw, rx in ROUTE_RULES:
                for m in rx.finditer(line):
                    g = m.groups()
                    if fw == "flask/fastapi":
                        method, path, handler = g[0].upper(), g[1], ""
                    elif fw == "flask":
                        method = (re.sub(r"[\s'\"]", "", g[1] or "GET")).upper().replace(",", "|")
                        path, handler = g[0], ""
                    elif fw == "express":
                        method, path, handler = g[0].upper(), g[1], ""
                    elif fw == "django":
                        method, path, handler = "ANY", "/" + g[0].lstrip("/"), g[1]
                    else:
                        method, path, handler = g[0], g[1], g[2]
                    eps.append({"method": method, "path": path, "handler": handler, "file": n, "line": i, "framework": fw})
            for m in CALL_RE.finditer(line):
                if m.group(1).startswith("/") or m.group(1).startswith("http"):
                    calls.append({"path": m.group(1), "file": n, "line": i})
    return eps, calls


def r_endpoints(h, q):
    root = repo_path(q.get("repo"))
    names = repo_files(root)
    eps, calls = find_endpoints(root, names)
    seen, uniq = set(), []
    for e in eps:
        k = (e["method"], e["path"], e["file"], e["line"])
        if k not in seen:
            seen.add(k)
            uniq.append(e)
    called = {c["path"].split("http://", 1)[-1] for c in calls}
    for e in uniq:
        e["called_by_repo"] = any(c == e["path"] or c.endswith(e["path"]) for c in called)
    # Handlers declared in the same file: add a short summary of auth / validation hints.
    for e in uniq:
        t = read_text(root, e["file"], 600_000) or ""
        lines = t.split("\n")
        win = "\n".join(lines[e["line"] - 1:e["line"] + 25]).lower()
        e["hints"] = [k for k, w in (("auth", ("auth", "token", "login_required", "jwt", "permission")),
                                     ("validation", ("validate", "schema", "pydantic", "isinstance", "400")),
                                     ("writes", ("write", "delete", "insert", "commit", "subprocess", "rmtree")))
                      if any(x in win for x in w)]
    called_paths = {c["path"] for c in calls}
    h.send_json(200, {"repo": root.name, "count": len(uniq), "endpoints": uniq[:300],
                      "calls": len(calls), "capped": len(uniq) > 300,
                      "unused": [e["path"] for e in uniq if not e["called_by_repo"]][:60],
                      "unknown_calls": sorted(p for p in called_paths if p.startswith("/") and
                                              not any(e["path"] == p for e in uniq))[:40]})


DEBT_MARKERS = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b[:\s]*(.{0,100})")


def _py_functions(text):
    out = []
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return out
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno)
            branches = sum(isinstance(x, (ast.If, ast.For, ast.While, ast.Try, ast.BoolOp, ast.ExceptHandler,
                                          ast.IfExp, ast.comprehension)) for x in ast.walk(node))
            out.append({"name": node.name, "line": node.lineno, "length": end - node.lineno + 1,
                        "complexity": branches + 1, "args": len(node.args.args)})
    return out


def _js_functions(text):
    """Rough: brace-balanced bodies of function declarations / arrow assignments."""
    out = []
    lines = text.split("\n")
    rx = re.compile(r"^\s*(?:export\s+)?(?:async\s+)?(?:function\s*\*?\s*(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:function|\([^)]*\)\s*=>|\w+\s*=>))")
    for i, ln in enumerate(lines):
        m = rx.match(ln)
        if not m or "{" not in ln:
            continue
        depth, j = 0, i
        while j < len(lines) and j - i < 2000:
            depth += lines[j].count("{") - lines[j].count("}")
            if depth <= 0 and j >= i:
                break
            j += 1
        body = "\n".join(lines[i:j + 1])
        br = len(re.findall(r"\b(if|for|while|case|catch)\b|&&|\|\||\?", body))
        out.append({"name": m.group(1) or m.group(2), "line": i + 1, "length": j - i + 1, "complexity": br + 1, "args": 0})
    return out


def find_debt(root, names, test_names):
    markers, long_files, funcs, dup_map = [], [], [], {}
    sizes = {}
    for n in names:
        ext = Path(n).suffix.lower()
        if ext not in CODE_EXT and ext not in (".css", ".sh", ".md"):
            continue
        t = read_text(root, n, 600_000)
        if t is None:
            continue
        lines = t.split("\n")
        sizes[n] = len(lines)
        if ext in CODE_EXT and len(lines) > 600:
            long_files.append({"path": n, "lines": len(lines)})
        for i, ln in enumerate(lines, 1):
            m = DEBT_MARKERS.search(ln)
            if m and ext != ".md" and len(markers) < 120:
                markers.append({"path": n, "line": i, "kind": m.group(1), "text": m.group(2).strip()})
        if ext == ".py":
            fl = _py_functions(t)
        elif ext in (".js", ".mjs", ".jsx", ".ts", ".tsx"):
            fl = _js_functions(t)
        else:
            fl = []
        for f in fl:
            if f["length"] > 60 or f["complexity"] > 15 or f["args"] > 6:
                f["path"] = n
                funcs.append(f)
        if ext in CODE_EXT and n not in test_names:
            norm = [re.sub(r"\s+", " ", x).strip() for x in lines]
            for i in range(len(norm) - 5):
                blk = norm[i:i + 6]
                if sum(len(x) for x in blk) < 150 or any(len(x) < 3 for x in blk[:2]):
                    continue
                dup_map.setdefault(hash("\n".join(blk)), []).append((n, i + 1))
    dups = []
    for locs in dup_map.values():
        if len({p for p, _ in locs}) > 1 or len(locs) > 1:
            dups.append(locs[:4])
    dups.sort(key=lambda l: -len(l))
    funcs.sort(key=lambda f: -(f["length"] + f["complexity"] * 3))
    long_files.sort(key=lambda f: -f["lines"])
    return markers, long_files, funcs, dups[:15], sizes


def git_churn(root, days=180):
    try:
        out = git_bytes(["log", "--since=%d.days" % days, "--name-only", "--pretty=format:", "-n", "400"], root, 30)
    except ApiError:
        return {}
    c = {}
    for ln in out.decode("utf-8", "replace").split("\n"):
        if ln.strip():
            c[ln.strip()] = c.get(ln.strip(), 0) + 1
    return c


def r_debt(h, q):
    root = repo_path(q.get("repo"))
    names = repo_files(root)
    is_test = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*\.py$|_test\.py$|\.(test|spec)\.[jt]sx?$")
    tests = {n for n in names if is_test.search(n)}
    markers, long_files, funcs, dups, sizes = find_debt(root, names, tests)
    test_text = ""
    for n in sorted(tests)[:60]:
        test_text += (read_text(root, n, 200_000) or "").lower() + "\n"
    untested = []
    for n in names:
        if Path(n).suffix.lower() in (".py", ".js", ".mjs", ".ts") and n not in tests and sizes.get(n, 0) > 40:
            stem = Path(n).stem.lower()
            if stem not in test_text:
                untested.append({"path": n, "lines": sizes[n]})
    untested.sort(key=lambda x: -x["lines"])
    churn = git_churn(root)
    hot = sorted(({"path": p, "commits": c, "lines": sizes[p], "score": c * sizes[p]}
                  for p, c in churn.items() if p in sizes and sizes[p] > 80), key=lambda x: -x["score"])[:8]
    scan = []
    for n in names:
        t = read_text(root, n, 500_000)
        if t is not None:
            scan += scan_text(n, t)
    counts = {s: sum(1 for f in scan if f["severity"] == s) for s in SEV_ORDER}
    h.send_json(200, {
        "repo": root.name, "files": len(names), "has_tests": bool(tests),
        "markers": markers, "marker_count": len(markers),
        "long_files": long_files[:10], "complex_functions": funcs[:15],
        "duplicates": [[{"path": p, "line": l} for p, l in d] for d in dups],
        "untested": untested[:15], "hotspots": hot, "scan_counts": counts,
        "scan_top": sorted(scan, key=lambda f: (SEV_ORDER[f["severity"]], f["path"], f["line"]))[:15],
    })


ROUTES = {
    ("GET", "/api/memory"): r_memory_get,
    ("POST", "/api/memory"): r_memory_post,
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
    ("GET", "/api/context"): r_context,
    ("POST", "/api/create"): r_create,
    ("POST", "/api/delete"): r_delete,
    ("GET", "/api/search"): r_search,
    ("GET", "/api/tree"): r_tree,
    ("GET", "/api/gitinfo"): r_gitinfo,
    ("GET", "/api/filediff"): r_filediff,
    ("POST", "/api/commit_files"): r_commit_files,
    ("POST", "/api/discard"): r_discard,
    ("GET", "/api/log"): r_log,
    ("GET", "/api/show"): r_show,
    ("POST", "/api/revert"): r_revert,
    ("POST", "/api/switch"): r_switch,
    ("POST", "/api/upload"): r_upload,
    ("GET", "/api/analyze"): r_analyze,
    ("POST", "/api/check"): r_check,
    ("POST", "/api/bundle"): r_bundle,
    ("POST", "/api/apply"): r_apply,
    ("GET", "/api/review_scan"): r_review_scan,
    ("GET", "/api/deps"): r_deps,
    ("GET", "/api/arch"): r_arch,
    ("GET", "/api/endpoints"): r_endpoints,
    ("GET", "/api/debt"): r_debt,
    ("POST", "/api/patch_plan"): r_patch_plan,
    ("POST", "/api/patch_zip"): r_patch_zip,
    ("POST", "/api/exec"): r_exec,
    ("GET", "/api/sysinfo"): r_sysinfo,
    ("GET", "/api/ollama_status"): r_ollama_status,
    ("GET", "/api/server_info"): r_server_info,
    ("GET", "/api/tunnel"): r_tunnel_get,
    ("POST", "/api/tunnel"): r_tunnel_post,
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
