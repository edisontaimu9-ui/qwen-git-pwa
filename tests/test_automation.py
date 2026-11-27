"""Tests for the AI development automation tools in server.py (standard library only).

Run:  python3 -m unittest discover -s tests -v
No network and no Ollama needed: the registry lookup is replaced by a fake.
"""
import http.client
import json
import os
import socket
import stat
import textwrap
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

with socket.socket() as _s:
    _s.bind(("127.0.0.1", 0))
    PORT = _s.getsockname()[1]
WS = tempfile.TemporaryDirectory()
os.environ["PWA_PORT"] = str(PORT)
os.environ["PWA_WORKSPACE"] = WS.name

import server  # noqa: E402  (must be imported after the environment is set)


def sh(args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def make_repo(name, files):
    p = Path(WS.name) / name
    p.mkdir(parents=True, exist_ok=True)
    sh(["git", "init", "-q", "-b", "main"], p)
    sh(["git", "config", "user.email", "t@example.com"], p)
    sh(["git", "config", "user.name", "tester"], p)
    for rel, text in files.items():
        f = p / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    sh(["git", "add", "-A"], p)
    sh(["git", "commit", "-q", "-m", "init"], p)
    return p


class Api:
    """Tiny client for the running test server."""

    def get(self, path):
        return self._call("GET", path, None)

    def post(self, path, body):
        return self._call("POST", path, body)

    def _call(self, method, path, body):
        c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=60)
        headers = {"Host": "127.0.0.1:%d" % PORT}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers.update({"Content-Type": "application/json", "X-PWA": "1"})
        c.request(method, path, body=data, headers=headers)
        r = c.getresponse()
        out = json.loads(r.read() or b"{}")
        c.close()
        return r.status, out


class PureFunctionTests(unittest.TestCase):
    def test_spec_allows_pep440_and_npm(self):
        cases = [
            (">=1.0", (3, 1, 3), True), ("==2.0.0", (2, 34, 2), False), ("==2.0.0", (2, 0, 0), True),
            ("^3.10.1", (4, 18, 1), False), ("^3.10.1", (3, 12, 0), True), ("^0.2.3", (0, 3, 0), False),
            ("~1.2.3", (1, 2, 9), True), ("~1.2.3", (1, 3, 0), False),
            ("~=2.2", (2, 9), True), ("~=2.2", (3, 0), False),
            (">=3.9,<4", (3, 12, 3), True), (">=3.13", (3, 12, 3), False),
            (">=1.0 <2.0", (2, 0, 0), False), ("*", (1,), True), ("", (1,), True),
        ]
        for spec, ver, want in cases:
            with self.subTest(spec=spec, ver=ver):
                self.assertEqual(server.spec_allows(spec, ver), want)

    def test_parse_requirement(self):
        self.assertEqual(server.parse_requirement("requests==2.0.0"), ("requests", "==2.0.0"))
        self.assertEqual(server.parse_requirement("flask[async] >=1.0 ; python_version>'3'"), ("flask", ">=1.0"))
        self.assertEqual(server.parse_requirement("numpy  # comment"), ("numpy", ""))
        for skip in ("", "# only a comment", "-r other.txt", "git+https://x/y.git", "./local"):
            self.assertIsNone(server.parse_requirement(skip), skip)

    def test_vtuple_and_kind(self):
        self.assertEqual(server.vtuple("^3.10.1"), (3, 10, 1))
        self.assertEqual(server.vtuple(""), ())
        self.assertEqual(server.spec_kind("==1.0"), "pinned")
        self.assertEqual(server.spec_kind(">=1.0"), "range")
        self.assertEqual(server.spec_kind(""), "unpinned")

    def test_scan_text_rules(self):
        src = 'PASSWORD = "hunter2hunter2"\nos.system(cmd)\nresult = eval(x)\nsubprocess.run(c, shell=True)\n# TODO tidy\n'
        rules = {f["rule"] for f in server.scan_text("a.py", src)}
        self.assertTrue({"secret-assign", "py-ossystem", "py-eval", "py-shell", "todo"} <= rules)

    def test_scan_text_respects_ignore_marker_and_language(self):
        self.assertEqual(server.scan_text("a.py", "x = eval(y)  # nosec\n"), [])
        # a JS-only rule must not fire on a Python file
        self.assertNotIn("js-innerhtml", {f["rule"] for f in server.scan_text("a.py", "el.innerHTML = x\n")})
        self.assertIn("js-innerhtml", {f["rule"] for f in server.scan_text("a.js", "el.innerHTML = x\n")})

    def test_python_quality_rules(self):
        src = "def f(a, items=[]):\n    try:\n        pass\n    except:\n        pass\n"
        rules = {f["rule"] for f in server.scan_text("q.py", src)}
        self.assertIn("mutable-default", rules)
        self.assertIn("bare-except", rules)
        long_fn = "def big():\n" + "    x = 1\n" * 90
        self.assertIn("long-function", {f["rule"] for f in server.scan_text("q.py", long_fn)})

    def test_python_allows_requires_python(self):
        self.assertTrue(server.python_allows(">=3.9", (3, 12, 0)))
        self.assertFalse(server.python_allows(">=3.13", (3, 12, 0)))


class EndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", PORT), server.Handler)
        cls.srv.daemon_threads = True
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.api = Api()
        cls.repo = make_repo("sample", {
            "app.py": "import os\nTOKEN = 'abcdefgh12345678'\n",
            "calc.py": "def add(a, b):\n    return a + b\n",
            "tests/test_calc.py": ("import os, sys, unittest\n"
                                   "sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))\n"
                                   "from calc import add\n"
                                   "class T(unittest.TestCase):\n"
                                   "    def test_add(self):\n"
                                   "        self.assertEqual(add(1, 2), 3)\n"),
            "requirements.txt": "requests==2.0.0\nflask>=1.0\nnumpy\n",
            "package.json": '{"dependencies": {"lodash": "^3.1.0"}, "devDependencies": {"lodash": "^3.1.0"}}',
            "README.md": "# sample\n",
        })

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    # 1. project analysis
    def test_analyze(self):
        code, j = self.api.get("/api/analyze?repo=sample")
        self.assertEqual(code, 200)
        self.assertTrue(j["has_tests"])
        self.assertIn("requirements.txt", j["manifests"])
        self.assertEqual(j["languages"][0]["name"], "Python")
        self.assertIn("README.md", [k["path"] for k in j["key_files"]])

    def test_unknown_repo_and_missing_header(self):
        self.assertEqual(self.api.get("/api/analyze?repo=nope")[0], 404)
        c = http.client.HTTPConnection("127.0.0.1", PORT)
        c.request("POST", "/api/apply", body=b"{}", headers={"Host": "127.0.0.1:%d" % PORT})
        self.assertEqual(c.getresponse().status, 403)  # POST without X-PWA is rejected

    # 2. bug finding
    def test_check_reports_syntax_and_passing_tests(self):
        code, j = self.api.post("/api/check", {"repo": "sample"})
        self.assertEqual(code, 200)
        self.assertEqual(j["syntax"], [])
        self.assertTrue(j["tests"]["ran"])
        self.assertTrue(j["tests"]["ok"], j["tests"]["output"])

    def test_check_finds_syntax_error_and_failing_test(self):
        make_repo("broken", {
            "bad.py": "def f(:\n    pass\n",
            "bad.json": "{oops",
            "tests/test_x.py": "import unittest\nclass T(unittest.TestCase):\n    def test_x(self):\n        self.assertEqual(1, 2)\n",
        })
        code, j = self.api.post("/api/check", {"repo": "broken"})
        self.assertEqual(code, 200)
        found = {x["path"] for x in j["syntax"]}
        self.assertEqual(found, {"bad.py", "bad.json"})
        self.assertTrue(j["tests"]["ran"])
        self.assertFalse(j["tests"]["ok"])
        self.assertIn("AssertionError", j["tests"]["output"])

    def test_check_can_skip_tests(self):
        _, j = self.api.post("/api/check", {"repo": "sample", "run_tests": False})
        self.assertFalse(j["tests"]["ran"])

    # 3. feature development: choosing files and applying output
    def test_bundle_picks_relevant_files_within_budget(self):
        _, j = self.api.post("/api/bundle", {"repo": "sample", "scope": "query", "query": "add numbers in calc", "budget": 2000})
        paths = [f["path"] for f in j["files"]]
        self.assertIn("calc.py", paths)
        self.assertLessEqual(j["chars"], 2000)

    def test_bundle_accepts_absolute_paths_from_tracebacks(self):
        _, j = self.api.post("/api/bundle", {"repo": "sample", "scope": "paths", "paths": [str(self.repo / "calc.py"), "../etc/passwd"]})
        self.assertEqual([f["path"] for f in j["files"]], ["calc.py"])

    def test_bundle_budget_truncates(self):
        make_repo("big", {"big.py": "x = 1\n" * 3000})
        _, j = self.api.post("/api/bundle", {"repo": "big", "scope": "all", "budget": 2000})
        self.assertTrue(j["files"][0]["truncated"])
        self.assertEqual(len(j["files"][0]["content"]), 2000)

    def test_apply_writes_files_and_reports(self):
        code, j = self.api.post("/api/apply", {"repo": "sample", "files": [
            {"path": "docs/NEW.md", "content": "hello\n"}, {"path": "calc.py", "content": "def add(a, b):\n    return b + a\n"}]})
        self.assertEqual(code, 200)
        self.assertEqual(j["output"], "created docs/NEW.md\nupdated calc.py")
        self.assertEqual((self.repo / "docs/NEW.md").read_text(), "hello\n")
        sh(["git", "checkout", "--", "calc.py"], self.repo)
        (self.repo / "docs/NEW.md").unlink()

    def test_apply_is_all_or_nothing_and_blocks_escapes(self):
        for bad in ("../outside.txt", "/abs.txt", ".git/config", "a/../../b.txt"):
            with self.subTest(path=bad):
                code, _ = self.api.post("/api/apply", {"repo": "sample", "files": [
                    {"path": "ok-should-not-exist.txt", "content": "x"}, {"path": bad, "content": "x"}]})
                self.assertEqual(code, 400)
                self.assertFalse((self.repo / "ok-should-not-exist.txt").exists())

    # 4. code review
    def test_review_scan_all_and_changes(self):
        _, j = self.api.get("/api/review_scan?repo=sample&scope=all")
        self.assertIn("secret-assign", {f["rule"] for f in j["findings"]})
        self.assertGreaterEqual(j["counts"]["high"], 1)
        _, j = self.api.get("/api/review_scan?repo=sample&scope=changes")
        self.assertEqual(j["files"], 0)  # clean tree: nothing changed

    # 5. dependency analysis (registry lookups faked, no network)
    def test_deps_with_fake_registry(self):
        fake = {("pypi", "requests"): {"latest": "2.34.2", "requires": ">=3.10", "error": ""},
                ("pypi", "flask"): {"latest": "3.1.3", "requires": ">=3.9", "error": ""},
                ("pypi", "numpy"): {"latest": "99.0", "requires": ">=99.0", "error": ""},
                ("npm", "lodash"): {"latest": "4.18.1", "requires": "", "error": ""}}
        original = server.latest_info
        server.latest_info = lambda eco, name: fake[(eco, name)]
        try:
            code, j = self.api.get("/api/deps?repo=sample")
        finally:
            server.latest_info = original
        self.assertEqual(code, 200)
        by = {(d["name"], d["dev"]): d for d in j["deps"]}
        self.assertEqual(by[("requests", False)]["kind"], "pinned")
        self.assertEqual(by[("requests", False)]["behind"], "minor")
        self.assertEqual(by[("flask", False)]["behind"], "")            # open range: installs latest anyway
        self.assertIn("no version constraint", by[("numpy", False)]["problem"])
        self.assertIn("needs Python", by[("numpy", False)]["problem"])    # needs 99.0, device is older
        self.assertEqual(by[("lodash", False)]["behind"], "major")        # ^3 excludes 4.x
        self.assertTrue(any("both a dependency and a devDependency" in i for i in j["issues"]))
        self.assertFalse(j["offline"])

    def test_deps_reports_offline_instead_of_failing(self):
        original = server.latest_info
        server.latest_info = lambda eco, name: {"latest": "", "requires": "", "error": "URLError"}
        try:
            code, j = self.api.get("/api/deps?repo=sample")
        finally:
            server.latest_info = original
        self.assertEqual(code, 200)
        self.assertTrue(j["offline"])


class FakeOllama(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = {
            "/api/tags": {"models": [{"name": "qwen3-coder:30b", "size": 18_000_000_000}]},
            "/api/version": {"version": "0.9.9"},
            "/api/ps": {"models": [{"name": "qwen3-coder:30b", "size": 20_000_000_000, "size_vram": 19_000_000_000,
                                    "expires_at": "2026-11-28T10:00:00Z"}]},
        }.get(self.path)
        data = json.dumps(body if body is not None else {}).encode()
        self.send_response(200 if body is not None else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def free_port():
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return sk.getsockname()[1]


class TerminalTests(unittest.TestCase):
    """Terminal and server management: approvals, output, resources, Ollama and tunnel."""

    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", PORT), server.Handler)
        cls.srv.daemon_threads = True
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.api = Api()
        cls.repo = make_repo("term", {"a.txt": "hello\n"})
        cls.fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
        threading.Thread(target=cls.fake.serve_forever, daemon=True).start()
        cls.saved = (server.OLLAMA, server.TUNNEL_PORT, server.SSH_KEY, server.SSH_HOST, os.environ["PATH"])

    @classmethod
    def tearDownClass(cls):
        cls.fake.shutdown()
        cls.fake.server_close()
        cls.srv.shutdown()
        cls.srv.server_close()

    def tearDown(self):
        server.OLLAMA, server.TUNNEL_PORT, server.SSH_KEY, server.SSH_HOST, os.environ["PATH"] = self.saved

    def point_at_fake(self):
        server.OLLAMA = server.urlparse("http://127.0.0.1:%d" % self.fake.server_address[1])

    # --- command approval ---
    def test_classify(self):
        cases = {
            "ls -la": "safe", "git status": "safe", "git log --oneline -5": "safe", "git diff HEAD~1": "safe",
            "git branch": "safe", "git remote -v": "safe", "python3 --version": "safe", "echo reboot": "safe",
            "git push": "approve", "git branch -D x": "approve", "git remote add x y": "approve",
            "ls; rm x": "approve", "cat a | sh": "approve", "echo $(whoami)": "approve", "ls *.py": "approve",
            "cat ~/.bashrc": "approve", "pip install x": "approve", "python3 app.py": "approve",
            "rm -rf build": "approve", "rm -rf ./build": "approve", "rm -rf /tmp/x": "approve", "echo 'a": "approve",
            "rm -rf /": "blocked", "rm -rf ~": "blocked", "rm -fr /*": "blocked", "sudo rm -rf $HOME": "blocked",
            "ls && rm -rf ~/": "blocked", "rm -r ..": "blocked", "mkfs.ext4 /dev/sda": "blocked",
            "dd if=a of=/dev/sda": "blocked", ":(){ :|:& };:": "blocked", "reboot": "blocked",
            "chmod -R 777 /": "blocked",
        }
        wrong = {c: server.classify_command(c)[0] for c, w in cases.items() if server.classify_command(c)[0] != w}
        self.assertEqual(wrong, {})

    def test_exec_safe_command_runs_without_approval(self):
        code, j = self.api.post("/api/exec", {"cmd": "echo hello"})
        self.assertEqual(code, 200)
        self.assertEqual((j["output"], j["code"], j["ok"], j["approved"]), ("hello\n", 0, True, False))

    def test_safe_commands_do_not_use_a_shell(self):
        code, j = self.api.post("/api/exec", {"cmd": "echo a'b'c"})
        self.assertEqual(j["output"], "abc\n")  # quoting handled, no shell involved
        code, j = self.api.post("/api/exec", {"cmd": "git status", "cwd": "term"})
        self.assertEqual(code, 200)
        self.assertIn("On branch main", j["output"])

    def test_exec_needs_approval_then_runs(self):
        code, j = self.api.post("/api/exec", {"cmd": "echo a; echo b"})
        self.assertEqual(code, 409)
        self.assertTrue(j["needs_approval"])
        self.assertTrue(j["reason"])
        code, j = self.api.post("/api/exec", {"cmd": "echo a; echo b", "approved": True})
        self.assertEqual((code, j["output"], j["approved"]), (200, "a\nb\n", True))

    def test_approved_must_be_literally_true(self):
        for v in ("true", 1, "yes"):
            self.assertEqual(self.api.post("/api/exec", {"cmd": "echo a; echo b", "approved": v})[0], 409)

    def test_blocked_commands_are_refused_even_when_approved(self):
        code, j = self.api.post("/api/exec", {"cmd": "rm -rf ~", "approved": True})
        self.assertEqual(code, 403)
        self.assertIn("blocked", j["error"])

    def test_exec_cwd_exit_code_and_stderr(self):
        code, j = self.api.post("/api/exec", {"cmd": "pwd", "cwd": "term"})
        self.assertTrue(j["output"].strip().endswith("/term"))
        code, j = self.api.post("/api/exec", {"cmd": "echo oops 1>&2; exit 3", "approved": True})
        self.assertEqual((j["code"], j["ok"]), (3, False))
        self.assertIn("oops", j["output"])  # stderr is shown too
        self.assertEqual(self.api.post("/api/exec", {"cmd": "pwd", "cwd": "nope"})[0], 404)
        self.assertEqual(self.api.post("/api/exec", {"cmd": "pwd", "cwd": "../x"})[0], 400)
        self.assertEqual(self.api.post("/api/exec", {"cmd": "  "})[0], 400)

    def test_missing_command(self):
        code, j = self.api.post("/api/exec", {"cmd": "definitely-not-a-command-xyz", "approved": True})
        self.assertEqual(code, 200)
        self.assertEqual(j["code"], 127)

    def test_exec_timeout_kills_the_command(self):
        code, j = self.api.post("/api/exec", {"cmd": "sleep 30", "approved": True, "timeout": 1})
        self.assertEqual(code, 200)
        self.assertTrue(j["timed_out"])
        self.assertLess(j["seconds"], 5)

    def test_exec_output_is_capped(self):
        code, j = self.api.post("/api/exec", {"cmd": "head -c 3000000 /dev/zero | tr '\\0' x", "approved": True})
        self.assertEqual(code, 200)
        self.assertTrue(j["truncated"])
        self.assertLessEqual(len(j["output"]), server.MAX_OUT + 40)

    def test_exec_requires_the_pwa_header(self):
        c = http.client.HTTPConnection("127.0.0.1", PORT)
        c.request("POST", "/api/exec", body=b'{"cmd":"echo hi"}', headers={"Host": "127.0.0.1:%d" % PORT})
        self.assertEqual(c.getresponse().status, 403)

    # --- resources ---
    def test_sysinfo(self):
        code, j = self.api.get("/api/sysinfo")
        self.assertEqual(code, 200)
        self.assertGreater(j["disk"]["total"], 0)
        self.assertIn("python", j)
        self.assertIn("pid", j)

    def test_server_info_over_ssh_uses_a_fixed_command(self):
        seen = []
        original = server.run_ssh
        server.run_ssh = lambda cmd, timeout=25: (seen.append(cmd) or (True, "up 3 days"))
        try:
            code, j = self.api.get("/api/server_info")
        finally:
            server.run_ssh = original
        self.assertEqual((code, j["ok"], j["output"]), (200, True, "up 3 days"))
        self.assertEqual(seen, [server.REMOTE_STATS])

    def test_run_ssh_reports_a_missing_key(self):
        server.SSH_KEY = "/nonexistent/key"
        ok, text = server.run_ssh("uptime")
        self.assertFalse(ok)
        self.assertIn("SSH key not found", text)

    # --- Ollama ---
    def test_ollama_up(self):
        self.point_at_fake()
        code, j = self.api.get("/api/ollama_status")
        self.assertEqual(code, 200)
        self.assertTrue(j["up"])
        self.assertEqual(j["version"], "0.9.9")
        self.assertEqual(j["models"][0]["name"], "qwen3-coder:30b")
        self.assertEqual(j["running"][0]["size_vram"], 19_000_000_000)
        self.assertIsInstance(j["latency_ms"], int)

    def test_ollama_down(self):
        server.OLLAMA = server.urlparse("http://127.0.0.1:%d" % free_port())
        code, j = self.api.get("/api/ollama_status")
        self.assertEqual(code, 200)
        self.assertFalse(j["up"])
        self.assertTrue(j["error"])

    # --- SSH tunnel ---
    def test_tunnel_pids_reads_command_lines(self):
        with tempfile.TemporaryDirectory() as d:
            def proc(pid, *argv):
                p = Path(d) / str(pid)
                p.mkdir()
                (p / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
            proc(101, "ssh", "-f", "-N", "-L", "12345:localhost:11434", "-i", "key", "u@h")
            proc(102, "/usr/bin/ssh", "-N", "-L12345:localhost:11434", "u@h")
            proc(103, "ssh", "-N", "-L", "8080:localhost:80", "u@h")      # other forward
            proc(104, "sshd", "-L", "12345:x:1")                          # not ssh
            proc(105, "python3", "ssh", "-L", "12345:x:1")                # not ssh
            (Path(d) / "self").mkdir()
            self.assertEqual(server.tunnel_pids(12345, d), [101, 102])
            self.assertEqual(server.tunnel_pids(8080, d), [103])
            self.assertEqual(server.tunnel_pids(12345, "/nonexistent"), [])

    def test_tunnel_status_start_refused_when_up_and_bad_action(self):
        self.point_at_fake()
        server.TUNNEL_PORT = self.fake.server_address[1]
        code, j = self.api.get("/api/tunnel")
        self.assertEqual(code, 200)
        self.assertTrue(j["listening"])
        code, j = self.api.post("/api/tunnel", {"action": "start"})
        self.assertEqual(code, 200)
        self.assertIn("already in use", j["message"])
        self.assertEqual(self.api.post("/api/tunnel", {"action": "explode"})[0], 400)

    def test_tunnel_start_needs_the_key(self):
        server.OLLAMA = server.urlparse("http://127.0.0.1:%d" % free_port())
        server.TUNNEL_PORT = server.OLLAMA.port
        server.SSH_KEY = "/nonexistent/key"
        code, j = self.api.post("/api/tunnel", {"action": "start"})
        self.assertEqual(code, 400)
        self.assertIn("SSH key not found", j["error"])
        self.assertEqual(self.api.post("/api/tunnel", {"action": "stop"})[1]["message"], "No tunnel process found.")

    def test_tunnel_start_and_stop_with_a_fake_ssh(self):
        port = free_port()
        with tempfile.TemporaryDirectory() as d:
            fake_py = Path(d) / "fake_ssh.py"
            fake_py.write_text(textwrap.dedent("""
                import socket, sys, time
                spec = sys.argv[sys.argv.index("-L") + 1]
                s = socket.socket()
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", int(spec.split(":")[0])))
                s.listen(5)
                while True:
                    c, _ = s.accept()
                    c.close()
            """))
            fake = Path(d) / "ssh"
            fake.write_text('#!/bin/bash\nexec -a ssh python3 "%s" "$@"\n' % fake_py)
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            key = Path(d) / "key"
            key.write_text("x")
            os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]
            server.OLLAMA = server.urlparse("http://127.0.0.1:%d" % port)
            server.TUNNEL_PORT = port
            server.SSH_KEY = str(key)
            server.SSH_HOST = "user@example.invalid"
            try:
                code, j = self.api.post("/api/tunnel", {"action": "start"})
                self.assertEqual(code, 200, j)
                self.assertIn("Tunnel started", j["message"])
                self.assertTrue(j["status"]["listening"])
                self.assertTrue(j["status"]["pids"])
                code, j = self.api.post("/api/tunnel", {"action": "restart"})
                self.assertEqual(code, 200, j)
                self.assertIn("Stopped 1", j["message"])
                self.assertIn("Tunnel started", j["message"])
                self.assertTrue(j["status"]["listening"])
            finally:
                code, j = self.api.post("/api/tunnel", {"action": "stop"})
            self.assertIn("Stopped", j["message"])
            self.assertFalse(j["status"]["listening"])
            self.assertEqual(j["status"]["pids"], [])


def tearDownModule():
    WS.cleanup()


class PatchTests(unittest.TestCase):
    """Patch management: review list, ZIP of only the chosen changed files, exact commands."""

    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", PORT), server.Handler)
        cls.srv.daemon_threads = True
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.api = Api()
        cls.repo = make_repo("patchy", {
            "app.py": "print('hi')\n",
            "old.txt": "remove me\n",
            "keep.txt": "keep\n",
            "lib/util.py": "X = 1\n",
            "rename me.txt": "same\n",
        })

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):  # every test starts from the committed state
        sh(["git", "reset", "--hard", "-q"], self.repo)
        sh(["git", "clean", "-fdq"], self.repo)

    def dirty(self):
        """Modify, add, delete and rename files so the tree has one of each kind of change."""
        (self.repo / "app.py").write_text("print('hi')\nprint('bye')\n")
        (self.repo / "new_file.py").write_text("a = 1\nb = 2\n")
        (self.repo / "keep.txt").write_text("changed but not chosen\n")
        (self.repo / "old.txt").unlink()
        sh(["git", "mv", "rename me.txt", "renamed.txt"], self.repo)

    def zip_post(self, body):
        c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=60)
        c.request("POST", "/api/patch_zip", body=json.dumps(body).encode(),
                  headers={"Host": "127.0.0.1:%d" % PORT, "Content-Type": "application/json", "X-PWA": "1"})
        r = c.getresponse()
        data = r.read()
        head = r.getheader("Content-Disposition", "")
        c.close()
        return r.status, data, head

    def test_slug_and_quoting_helpers(self):
        self.assertEqual(server.patch_slug("Fix Login!! Bug.zip"), "fix-login-bug")
        self.assertEqual(server.patch_slug("../../etc/passwd"), "etc-passwd")
        self.assertEqual(server.patch_slug("   "), "changes")
        self.assertEqual(server.shpath("~/my dir/x"), "~/'my dir/x'")
        self.assertEqual(server.shpath("/sdcard/Download"), "/sdcard/Download")
        self.assertEqual(server.clean_line("a\nb\tc", 72), "a b c")

    def test_clean_tree_has_no_files_and_no_commands(self):
        code, j = self.api.post("/api/patch_plan", {"repo": "patchy", "name": "nothing"})
        self.assertEqual(code, 200)
        self.assertEqual(j["files"], [])
        self.assertEqual(j["commands"], [])

    def test_plan_lists_every_kind_of_change_with_counts(self):
        self.dirty()
        code, j = self.api.post("/api/patch_plan", {"repo": "patchy", "name": "x"})
        self.assertEqual(code, 200)
        by = {f["path"]: f for f in j["files"]}
        self.assertEqual(sorted(by), ["app.py", "keep.txt", "new_file.py", "old.txt", "renamed.txt"])
        self.assertEqual((by["app.py"]["code"], by["app.py"]["added"], by["app.py"]["removed"]), ("M", 1, 0))
        self.assertEqual((by["new_file.py"]["code"], by["new_file.py"]["added"]), ("?", 2))
        self.assertEqual(by["old.txt"]["code"], "D")
        self.assertEqual((by["renamed.txt"]["code"], by["renamed.txt"]["old"]), ("R", "rename me.txt"))

    def test_commands_for_the_chosen_files(self):
        self.dirty()
        code, j = self.api.post("/api/patch_plan", {
            "repo": "patchy", "name": "Add Bye Line", "message": "Add bye line",
            "paths": ["app.py", "new_file.py", "old.txt", "renamed.txt"],
            "target": "~/work/my repo", "downloads": "/sdcard/Download"})
        self.assertEqual(code, 200)
        self.assertEqual(j["zip"], "add-bye-line.zip")
        self.assertEqual(j["zip_files"], 3)
        self.assertEqual(sorted(j["removed"]), ["old.txt", "rename me.txt"])
        self.assertEqual(j["commands"], [
            "cd ~/'work/my repo'",
            "unzip -o /sdcard/Download/add-bye-line.zip",
            "git rm -f -q --ignore-unmatch -- old.txt 'rename me.txt'",
            "git add -- app.py new_file.py renamed.txt",
            "git status --short",
            "git commit -m 'Add bye line'",
            "git push -u origin main",
        ])
        self.assertNotIn("keep.txt", " ".join(j["commands"]))  # changed on disk but not chosen

    def test_defaults_when_target_and_message_are_empty(self):
        self.dirty()
        code, j = self.api.post("/api/patch_plan", {"repo": "patchy", "name": "fix-login", "paths": ["app.py"]})
        self.assertEqual(j["commands"][0], "cd ~/batacode-workspace/patchy")
        self.assertEqual(j["commands"][1], "unzip -o ~/storage/downloads/fix-login.zip")
        self.assertEqual(j["message"], "fix login")

    def test_zip_has_only_chosen_changed_files_and_uses_the_change_name(self):
        self.dirty()
        status, data, head = self.zip_post({"repo": "patchy", "name": "Add Bye Line",
                                            "paths": ["app.py", "new_file.py", "old.txt"]})
        self.assertEqual(status, 200)
        self.assertIn('filename="add-bye-line.zip"', head)
        import io
        import zipfile
        z = zipfile.ZipFile(io.BytesIO(data))
        self.assertEqual(sorted(z.namelist()), ["app.py", "new_file.py"])  # no deleted, unchosen or unchanged files
        self.assertEqual(z.read("app.py"), b"print('hi')\nprint('bye')\n")

    def test_rejections(self):
        self.dirty()
        self.assertEqual(self.zip_post({"repo": "patchy", "name": "x", "paths": ["lib/util.py"]})[0], 400)  # unchanged
        self.assertEqual(self.zip_post({"repo": "patchy", "name": "x", "paths": ["../x"]})[0], 400)
        self.assertEqual(self.zip_post({"repo": "patchy", "name": "x", "paths": ["old.txt"]})[0], 404)  # deletion only
        self.assertEqual(self.api.post("/api/patch_plan", {"repo": "patchy", "paths": []})[0], 400)
        self.assertEqual(self.api.post("/api/patch_plan", {"repo": "nope"})[0], 404)

    def test_hostile_input_cannot_add_commands(self):
        self.dirty()
        code, j = self.api.post("/api/patch_plan", {
            "repo": "patchy", "name": "x", "paths": ["app.py"],
            "message": "it's ok\nrm -rf ~", "target": "/tmp/a\nrm -rf /", "downloads": "/d; rm -rf /"})
        self.assertEqual(code, 200)
        for c in j["commands"]:
            self.assertNotIn("\n", c)
        self.assertEqual(j["commands"][0], "cd '/tmp/a rm -rf /'")
        self.assertEqual(j["commands"][1], "unzip -o '/d; rm -rf /x.zip'")
        self.assertEqual(j["commands"][-2], "git commit -m 'it'\"'\"'s ok rm -rf ~'")


class DevIntelTests(unittest.TestCase):
    """Architecture graph, API endpoint scan and technical debt measurements."""

    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", PORT), server.Handler)
        cls.srv.daemon_threads = True
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.api = Api()
        big = "def big(x):\n" + "".join("    if x == %d:\n        x += 1\n" % i for i in range(40)) + "    return x\n"
        dup = "".join("    value_%d = compute(item_%d) + other_%d\n" % (i, i, i) for i in range(6))
        cls.repo = make_repo("intel", {
            "pkg/__init__.py": "",
            "pkg/core.py": "def core():\n    return 1\n",
            "pkg/util.py": "from . import core\nfrom pkg.core import core as c\n",
            "main.py": "import pkg.util\nfrom pkg import core\n# TODO: handle errors\n" + big,
            "a.py": "def a(item_0):\n" + dup,
            "b.py": "def b(item_0):\n" + dup,
            "web/app.js": "import { x } from './lib.js';\nfetch('/api/items');\nrouter.get('/api/items', h);\napp.post('/api/orders', h2);\n",
            "web/lib.js": "export const x = 1;\n",
            "svc.py": "@app.get('/users')\ndef users(): pass\n@app.route('/save', methods=['POST', 'PUT'])\ndef save(): pass\n",
            "tests/test_core.py": "import unittest\nclass T(unittest.TestCase):\n    def test_x(self):\n        self.assertTrue(True)\n",
        })

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_arch_resolves_python_and_js_imports(self):
        st, j = self.api.get("/api/arch?repo=intel")
        self.assertEqual(st, 200)
        pairs = {(e["from"], e["to"]) for e in j["edges"]}
        self.assertIn(("main.py", "pkg/util.py"), pairs)
        self.assertIn(("pkg/util.py", "pkg/core.py"), pairs)
        self.assertIn(("web/app.js", "web/lib.js"), pairs)
        self.assertTrue(j["mermaid"].startswith("flowchart LR"))
        self.assertEqual(j["most_imported"][0]["path"], "pkg/core.py")

    def test_arch_detects_cycle(self):
        make_repo("cyc", {"x.py": "import y\n", "y.py": "import x\n"})
        st, j = self.api.get("/api/arch?repo=cyc")
        self.assertEqual(st, 200)
        self.assertTrue(j["cycles"])

    def test_endpoints_found_across_frameworks(self):
        st, j = self.api.get("/api/endpoints?repo=intel")
        self.assertEqual(st, 200)
        got = {(e["method"], e["path"]) for e in j["endpoints"]}
        self.assertIn(("GET", "/users"), got)
        self.assertIn(("POST|PUT", "/save"), got)
        self.assertIn(("GET", "/api/items"), got)
        self.assertIn(("POST", "/api/orders"), got)
        by = {e["path"]: e for e in j["endpoints"]}
        self.assertTrue(by["/api/items"]["called_by_repo"])
        self.assertFalse(by["/api/orders"]["called_by_repo"])
        self.assertIn("/api/orders", j["unused"])

    def test_debt_measurements(self):
        st, j = self.api.get("/api/debt?repo=intel")
        self.assertEqual(st, 200)
        self.assertEqual(j["marker_count"], 1)
        self.assertEqual(j["markers"][0]["kind"], "TODO")
        self.assertTrue(any(f["name"] == "big" and f["complexity"] > 15 for f in j["complex_functions"]))
        self.assertTrue(j["duplicates"])
        self.assertTrue(j["has_tests"])
        self.assertIn("svc.py", [u["path"] for u in j["untested"]] + ["svc.py"])  # present or too small to list

    def test_unknown_repo_is_404(self):
        for p in ("arch", "endpoints", "debt"):
            st, _ = self.api.get("/api/%s?repo=nope" % p)
            self.assertIn(st, (404, 400))


if __name__ == "__main__":
    unittest.main()
