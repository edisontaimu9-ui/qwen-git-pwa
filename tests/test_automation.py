"""Tests for the AI development automation tools in server.py (standard library only).

Run:  python3 -m unittest discover -s tests -v
No network and no Ollama needed: the registry lookup is replaced by a fake.
"""
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
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
        WS.cleanup()

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


if __name__ == "__main__":
    unittest.main()
