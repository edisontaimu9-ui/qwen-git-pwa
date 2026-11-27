"use strict";
const $ = (s) => document.querySelector(s);

const store = {
  get(k, d) {
    try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; }
  },
  set(k, v) {
    try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* ignore */ }
  },
};

async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-PWA": "1" },
    body: JSON.stringify(body),
  };
  const r = await fetch(path, opt);
  let j;
  try { j = await r.json(); } catch (e) { j = { error: "bad response" }; }
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

function toast(t) {
  const el = $("#toast");
  el.textContent = t;
  el.hidden = false;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => { el.hidden = true; }, 2500);
}

// ---------- tabs ----------
function showTab(id) {
  document.querySelectorAll("nav button").forEach((x) => x.classList.toggle("on", x.dataset.tab === id));
  document.querySelectorAll(".tab").forEach((s) => s.classList.toggle("on", s.id === id));
  if (id === "browse") renderBrowse();
  if (id === "git") gRefresh();
  if (id === "auto") autoRefresh();
  if (id === "patch") patchRefresh();
  if (id === "term") termRefresh();
}
document.querySelectorAll("nav button").forEach((b) => {
  b.onclick = () => showTab(b.dataset.tab);
});

// ---------- Ollama status and models ----------
async function loadModels() {
  const net = $("#net");
  try {
    const j = await api("/api/tags");
    const names = (j.models || []).map((m) => m.name);
    const sel = $("#model");
    const keep = store.get("model", "");
    sel.textContent = "";
    names.forEach((n) => sel.add(new Option(n, n)));
    const pick = names.includes(keep) ? keep : (names.find((n) => n.includes("qwen3-coder")) || names[0]);
    if (pick) sel.value = pick;
    net.textContent = "Ollama ok";
    net.className = "ok";
  } catch (e) {
    net.textContent = "Ollama down";
    net.className = "bad";
  }
}
$("#model").onchange = () => store.set("model", $("#model").value);
setInterval(loadModels, 30000);

// ---------- repos ----------
let currentRepo = store.get("repo", "");

async function loadRepos() {
  try {
    const { repos, workspace } = await api("/api/repos");
    $("#wsPath").textContent = "Workspace: " + (workspace || "") + (repos.length ? "" : " (no repos yet)");
    const sel = $("#repo");
    sel.textContent = "";
    sel.add(new Option("no repo", ""));
    repos.forEach((r) => sel.add(new Option(r.name + " (" + r.branch + ")", r.name)));
    if (!repos.some((r) => r.name === currentRepo)) currentRepo = "";
    sel.value = currentRepo;
    const list = $("#repoList");
    list.textContent = "";
    repos.forEach((r) => {
      const b = document.createElement("button");
      b.textContent = r.name;
      b.className = r.name === currentRepo ? "on" : "";
      b.onclick = () => selectRepo(r.name);
      list.append(b);
    });
    showPanel();
  } catch (e) {
    $("#out").textContent = e.message;
  }
}

function showPanel() {
  $("#repoPanel").hidden = !currentRepo;
  $("#repoTitle").textContent = currentRepo;
}

function selectRepo(name) {
  currentRepo = name;
  store.set("repo", name);
  $("#repo").value = name;
  attachments.length = 0;
  renderChips();
  $("#files").hidden = true;
  browsePath = "";
  closeEditor(true);
  $("#fTreeOut").hidden = true;
  loadRepos();
  if ($("#browse").classList.contains("on")) renderBrowse();
  gUnchecked.clear();
  pUnchecked.clear();
  $("#pView").textContent = "";
  $("#pOut").textContent = "";
  $("#gView").textContent = "";
  if ($("#git").classList.contains("on")) gRefresh();
  if ($("#patch").classList.contains("on")) patchRefresh();
  tFillCwd();
}
$("#repo").onchange = () => selectRepo($("#repo").value);

function show(r) { $("#out").textContent = (r.ok === false ? "FAILED\n" : "") + (r.output || "ok"); }

async function gitAct(act, extra) {
  if (!currentRepo) return;
  $("#out").textContent = "working...";
  try {
    const isGet = act === "status" || act === "diff";
    const r = isGet
      ? await api("/api/" + act + "?repo=" + encodeURIComponent(currentRepo))
      : await api("/api/" + act, Object.assign({ repo: currentRepo }, extra || {}));
    show(r);
    if (!isGet) loadRepos();
  } catch (e) {
    $("#out").textContent = e.message;
  }
}

document.querySelectorAll("#repoPanel [data-act]").forEach((b) => {
  b.onclick = () => gitAct(b.dataset.act);
});
$("#commitForm").onsubmit = (e) => {
  e.preventDefault();
  const m = $("#commitMsg").value.trim();
  if (m) { gitAct("commit", { message: m }); $("#commitMsg").value = ""; }
};
$("#branchForm").onsubmit = (e) => {
  e.preventDefault();
  const n = $("#branchName").value.trim();
  if (n) { gitAct("branch", { name: n }); $("#branchName").value = ""; }
};
$("#cloneForm").onsubmit = async (e) => {
  e.preventDefault();
  const url = $("#cloneUrl").value.trim();
  if (!url) return;
  $("#out").textContent = "cloning...";
  try {
    const r = await api("/api/clone", { url, name: $("#cloneName").value.trim() });
    show(r);
    if (r.ok) { $("#cloneUrl").value = ""; $("#cloneName").value = ""; }
    loadRepos();
  } catch (err) {
    $("#out").textContent = err.message;
  }
};

// ---------- new project ----------
$("#newForm").onsubmit = async (e) => {
  e.preventDefault();
  const name = $("#newName").value.trim();
  if (!name) return;
  $("#out").textContent = "creating...";
  try {
    const r = await api("/api/create", { name, template: $("#newTpl").value });
    show(r);
    $("#newName").value = "";
    selectRepo(r.name);
  } catch (err) {
    $("#out").textContent = err.message;
  }
};

// ---------- file browser and editor ----------
let browsePath = "";
let openPath = "";
let dirty = false;
const enc = encodeURIComponent;

async function renderBrowse() {
  const box = $("#fList");
  box.textContent = "";
  $("#fPath").textContent = currentRepo ? currentRepo + "/" + browsePath : "";
  if (!currentRepo) {
    const d = document.createElement("div");
    d.textContent = "Pick a repo in the Repos tab first.";
    box.append(d);
    return;
  }
  try {
    const { items } = await api("/api/files?repo=" + enc(currentRepo) + "&path=" + enc(browsePath));
    const add = (label, fn) => {
      const d = document.createElement("div");
      d.textContent = label;
      d.onclick = fn;
      box.append(d);
    };
    if (browsePath) add("..", () => { browsePath = browsePath.split("/").slice(0, -1).join("/"); renderBrowse(); });
    items.forEach((it) => {
      const full = browsePath ? browsePath + "/" + it.name : it.name;
      if (it.dir) add(it.name + "/", () => { browsePath = full; renderBrowse(); });
      else add(it.name, () => openFile(full));
    });
    if (!items.length) add("(empty folder)", () => {});
  } catch (e) {
    toast(e.message);
  }
}

async function openFile(path, line) {
  if (dirty && !window.confirm("Discard unsaved changes?")) return;
  try {
    const j = await api("/api/file?repo=" + enc(currentRepo) + "&path=" + enc(path));
    openPath = path;
    dirty = false;
    const ta = $("#edText");
    ta.value = j.content;
    if (window.EditorKit) window.EditorKit.setFile(path);
    $("#edName").textContent = currentRepo + "/" + path;
    $("#editor").hidden = false;
    $("#fList").hidden = true;
    $("#fTreeOut").hidden = true;
    ta.scrollTop = 0;
    if (line) {
      const lines = j.content.split("\n");
      let off = 0;
      for (let i = 0; i < line - 1 && i < lines.length; i++) off += lines[i].length + 1;
      const end = off + (lines[line - 1] || "").length;
      ta.focus();
      ta.setSelectionRange(off, end);
      const lh = parseFloat(getComputedStyle(ta).lineHeight) || 18;
      ta.scrollTop = Math.max(0, (line - 4) * lh);
    }
  } catch (e) {
    toast(e.message);
  }
}

function closeEditor(force) {
  if (!force && dirty && !window.confirm("Discard unsaved changes?")) return;
  dirty = false;
  openPath = "";
  $("#editor").hidden = true;
  $("#fList").hidden = false;
}

$("#edText").oninput = () => { dirty = true; };
$("#edClose").onclick = () => { closeEditor(false); };
$("#edSave").onclick = async () => {
  if (!openPath) return;
  try {
    await api("/api/file", { repo: currentRepo, path: openPath, content: $("#edText").value });
    dirty = false;
    toast("Saved " + openPath);
  } catch (e) { toast(e.message); }
};
$("#edDel").onclick = async () => {
  if (!openPath || !window.confirm("Delete " + openPath + "?")) return;
  try {
    await api("/api/delete", { repo: currentRepo, path: openPath });
    toast("Deleted " + openPath);
    closeEditor(true);
    renderBrowse();
  } catch (e) { toast(e.message); }
};
$("#fNew").onclick = async () => {
  if (!currentRepo) { toast("Pick a repo first"); return; }
  const p = (window.prompt("New file path in " + currentRepo + " (example: src/util.py)", browsePath ? browsePath + "/" : "") || "").trim();
  if (!p || p.endsWith("/")) return;
  try {
    try {
      await api("/api/file?repo=" + enc(currentRepo) + "&path=" + enc(p));
    } catch (x) {
      await api("/api/file", { repo: currentRepo, path: p, content: "" });
    }
    browsePath = p.split("/").slice(0, -1).join("/");
    await renderBrowse();
    openFile(p);
  } catch (e) { toast(e.message); }
};
$("#fTree").onclick = async () => {
  if (!currentRepo) { toast("Pick a repo first"); return; }
  const out = $("#fTreeOut");
  if (!out.hidden) { out.hidden = true; $("#fList").hidden = !$("#editor").hidden; return; }
  try {
    const j = await api("/api/tree?repo=" + enc(currentRepo));
    out.textContent = j.tree + "\n\n" + j.files + " files" + (j.truncated ? " (list cut at 1500 lines)" : "");
    out.hidden = false;
    $("#fList").hidden = true;
    $("#editor").hidden = true;
  } catch (e) { toast(e.message); }
};

// ---------- search across repos ----------
$("#searchForm").onsubmit = async (e) => {
  e.preventDefault();
  const q = $("#sq").value.trim();
  const box = $("#sOut");
  if (!q) return;
  const one = $("#sScope").value === "repo";
  if (one && !currentRepo) { toast("Pick a repo first"); return; }
  box.textContent = "";
  const note = (t) => { const d = document.createElement("div"); d.textContent = t; box.append(d); };
  note("searching...");
  try {
    const j = await api("/api/search?q=" + enc(q) + (one ? "&repo=" + enc(currentRepo) : "") +
      ($("#sRegex").checked ? "&regex=1" : ""));
    box.textContent = "";
    if (!j.results.length) { note("No matches."); return; }
    j.results.forEach((r) => {
      const d = document.createElement("div");
      d.className = "hit";
      const b = document.createElement("b");
      b.textContent = r.repo + "/" + r.path + ":" + r.line;
      const s = document.createElement("span");
      s.textContent = r.text;
      d.append(b, s);
      d.onclick = () => {
        if (r.repo !== currentRepo) selectRepo(r.repo);
        showTab("browse");
        browsePath = r.path.split("/").slice(0, -1).join("/");
        renderBrowse();
        openFile(r.path, r.line);
      };
      box.append(d);
    });
    if (j.capped) note("Showing the first 300 matches. Narrow the search.");
  } catch (err) {
    box.textContent = "";
    note(err.message);
  }
};

// ---------- git control centre ----------
const gUnchecked = new Set();
let gFiles = [];

function diffEl(text) {
  const pre = document.createElement("pre");
  text.split("\n").forEach((ln) => {
    const s = document.createElement("span");
    s.textContent = ln + "\n";
    if (/^(\+\+\+|---|diff |index )/.test(ln)) s.className = "dl-meta";
    else if (ln.startsWith("@@")) s.className = "dl-hunk";
    else if (ln[0] === "+") s.className = "dl-add";
    else if (ln[0] === "-") s.className = "dl-del";
    pre.append(s);
  });
  return pre;
}

function gShow(text, title, buttons) {
  const v = $("#gView");
  v.textContent = "";
  if (title || buttons) {
    const bar = document.createElement("div");
    bar.className = "row";
    if (title) {
      const b = document.createElement("b");
      b.className = "muted";
      b.textContent = title;
      bar.append(b);
    }
    (buttons || []).forEach(([label, fn]) => {
      const x = document.createElement("button");
      x.type = "button";
      x.textContent = label;
      x.onclick = fn;
      bar.append(x);
    });
    v.append(bar);
  }
  v.append(diffEl(text));
}

function gSelected() {
  return gFiles.filter((c) => !gUnchecked.has(c.path)).map((c) => c.path);
}

async function gRefresh() {
  const list = $("#gChanges");
  const sel = $("#gBranch");
  if (!currentRepo) {
    $("#gLine").textContent = "Pick a repo in the Repos tab first.";
    list.textContent = "";
    sel.textContent = "";
    gFiles = [];
    return;
  }
  try {
    const j = await api("/api/gitinfo?repo=" + enc(currentRepo));
    $("#gLine").textContent = currentRepo + "  " + j.line;
    sel.textContent = "";
    if (!j.branch) sel.add(new Option("(detached HEAD)", ""));
    const group = (label, names) => {
      if (!names.length) return;
      const g = document.createElement("optgroup");
      g.label = label;
      names.forEach((n) => g.append(new Option(n, n)));
      sel.append(g);
    };
    group("Local", j.local);
    group("Remote", j.remote);
    sel.value = j.branch || "";
    gFiles = j.changes;
    list.textContent = "";
    if (!gFiles.length) {
      const d = document.createElement("div");
      d.className = "muted";
      d.style.padding = "8px";
      d.textContent = "No changes. Working tree clean.";
      list.append(d);
    }
    gFiles.forEach((c) => {
      const row = document.createElement("div");
      row.className = "chg";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = !gUnchecked.has(c.path);
      cb.onchange = () => { if (cb.checked) gUnchecked.delete(c.path); else gUnchecked.add(c.path); };
      const bd = document.createElement("span");
      bd.className = "badge b" + (c.code === "?" ? "Q" : c.code);
      bd.textContent = c.code;
      const nm = document.createElement("span");
      nm.className = "nm";
      nm.textContent = c.path;
      nm.onclick = () => gFileDiff(c.path);
      row.append(cb, bd, nm);
      list.append(row);
    });
  } catch (e) {
    $("#gLine").textContent = e.message;
  }
}

async function gFileDiff(path) {
  try {
    const j = await api("/api/filediff?repo=" + enc(currentRepo) + "&path=" + enc(path));
    gShow(j.diff.trim() ? j.diff : "(no textual changes)", path);
  } catch (e) { gShow(e.message, path); }
}

async function gRun(act, extra, title) {
  if (!currentRepo) { toast("Pick a repo first"); return null; }
  gShow("working...", title);
  try {
    const r = await api("/api/" + act, Object.assign({ repo: currentRepo }, extra || {}));
    gShow((r.ok === false ? "FAILED\n" : "") + (r.output || "ok"), title);
    gRefresh();
    loadRepos();
    return r;
  } catch (e) {
    gShow(e.message, title);
    gRefresh();
    return null;
  }
}

async function gHistory() {
  if (!currentRepo) { toast("Pick a repo first"); return; }
  try {
    const j = await api("/api/log?repo=" + enc(currentRepo) + "&n=40");
    if (!j.commits.length) { gShow("No commits yet.", "History"); return; }
    const v = $("#gView");
    v.textContent = "";
    j.commits.forEach((c) => {
      const d = document.createElement("div");
      d.className = "hit";
      const b = document.createElement("b");
      b.textContent = c.short + "  " + c.author + "  " + c.when;
      const s = document.createElement("span");
      s.textContent = c.subject;
      d.append(b, s);
      d.onclick = () => gCommitView(c);
      v.append(d);
    });
  } catch (e) { gShow(e.message, "History"); }
}

async function gCommitView(c) {
  try {
    const j = await api("/api/show?repo=" + enc(currentRepo) + "&hash=" + c.hash);
    gShow(j.text, c.short + " " + c.subject, [
      ["Back", gHistory],
      ["Revert this commit", () => {
        if (window.confirm("Create a new commit that undoes " + c.short + "?")) gRun("revert", { hash: c.hash }, "Revert " + c.short);
      }],
    ]);
  } catch (e) { gShow(e.message, c.short); }
}

document.querySelectorAll("#git [data-g]").forEach((b) => {
  b.onclick = () => {
    const k = b.dataset.g;
    if (k === "pull") gRun("pull", {}, "Pull");
    else if (k === "pullr") gRun("pull", { rebase: true }, "Pull (rebase)");
    else gRun("push", {}, "Push");
  };
});
$("#gRefresh").onclick = () => gRefresh();
$("#gHist").onclick = () => gHistory();
$("#gFull").onclick = async () => {
  if (!currentRepo) { toast("Pick a repo first"); return; }
  try {
    const r = await api("/api/diff?repo=" + enc(currentRepo));
    gShow(r.ok === false ? "FAILED\n" + r.output : (r.output.trim() || "No changes to tracked files since the last commit."), "Full diff vs last commit");
  } catch (e) { gShow(e.message, "Full diff"); }
};
$("#gBranch").onchange = () => {
  if ($("#gBranch").value) gRun("switch", { name: $("#gBranch").value }, "Switch branch");
};
$("#gBranchForm").onsubmit = async (e) => {
  e.preventDefault();
  const n = $("#gNewBranch").value.trim();
  if (!n) return;
  const r = await gRun("branch", { name: n }, "New branch");
  if (r && r.ok) $("#gNewBranch").value = "";
};
function gSetAll(on) {
  gUnchecked.clear();
  if (!on) gFiles.forEach((c) => gUnchecked.add(c.path));
  document.querySelectorAll("#gChanges input").forEach((i) => { i.checked = on; });
}
$("#gAll").onclick = () => gSetAll(true);
$("#gNone").onclick = () => gSetAll(false);
$("#gCommit").onsubmit = async (e) => {
  e.preventDefault();
  const message = $("#gMsg").value.trim();
  const paths = gSelected();
  if (!message) { toast("Write a commit message"); return; }
  if (!paths.length) { toast("Select at least one file"); return; }
  const r = await gRun("commit_files", { paths, message }, "Commit");
  if (r && r.ok) $("#gMsg").value = "";
};
$("#gDiscard").onclick = () => {
  const paths = gSelected();
  if (!paths.length) { toast("Select at least one file"); return; }
  const names = paths.slice(0, 8).join("\n") + (paths.length > 8 ? "\n...and " + (paths.length - 8) + " more" : "");
  if (!window.confirm("Discard changes to " + paths.length + " file(s)? This cannot be undone.\n\n" + names)) return;
  gRun("discard", { paths }, "Discard");
};

// ---------- attachments ----------
const attachments = [];
let attachPath = "";

function renderChips() {
  const c = $("#chips");
  c.textContent = "";
  attachments.forEach((a, i) => {
    const s = document.createElement("span");
    s.className = "chip";
    s.textContent = a.path;
    const x = document.createElement("b");
    x.textContent = "x";
    x.onclick = () => { attachments.splice(i, 1); renderChips(); };
    s.append(x);
    c.append(s);
  });
}

async function renderFiles() {
  const box = $("#files");
  box.textContent = "";
  try {
    const { items } = await api("/api/files?repo=" + encodeURIComponent(currentRepo) + "&path=" + encodeURIComponent(attachPath));
    const add = (label, fn) => {
      const d = document.createElement("div");
      d.textContent = label;
      d.onclick = fn;
      box.append(d);
    };
    if (attachPath) add("..", () => { attachPath = attachPath.split("/").slice(0, -1).join("/"); renderFiles(); });
    items.forEach((it) => {
      const full = attachPath ? attachPath + "/" + it.name : it.name;
      if (it.dir) add(it.name + "/", () => { attachPath = full; renderFiles(); });
      else add(it.name, async () => {
        try {
          const j = await api("/api/file?repo=" + encodeURIComponent(currentRepo) + "&path=" + encodeURIComponent(full));
          if (!attachments.some((a) => a.path === full)) attachments.push({ path: full, content: j.content });
          renderChips();
          box.hidden = true;
        } catch (e) { toast(e.message); }
      });
    });
  } catch (e) {
    toast(e.message);
    box.hidden = true;
  }
}

$("#attachBtn").onclick = () => {
  if (!currentRepo) { toast("Pick a repo first"); return; }
  const box = $("#files");
  box.hidden = !box.hidden;
  if (!box.hidden) { attachPath = ""; renderFiles(); }
};

// ---------- assistant tools ----------
const TEMPLATES = {
  explain: "Explain what this code does, step by step:",
  error: "Explain this error, find the cause, and show the fix:",
  refactor: "Refactor the attached code for readability without changing behavior. Show the full updated file(s).",
  optimise: "Optimise the attached code for speed and memory. Show the full updated file(s) and explain each change briefly.",
  newfile: "Write a complete file that does this:",
};
document.querySelectorAll("#quick [data-q]").forEach((b) => {
  b.onclick = () => {
    const i = $("#input");
    i.value = TEMPLATES[b.dataset.q] + (i.value ? "\n" + i.value : "");
    i.focus();
  };
});

$("#diffBtn").onclick = async () => {
  if (!currentRepo) { toast("Pick a repo first"); return; }
  try {
    const r = await api("/api/diff?repo=" + encodeURIComponent(currentRepo));
    if (!r.output || !r.output.trim()) { toast("No changes to attach"); return; }
    if (!attachments.some((a) => a.path === "git diff")) attachments.push({ path: "git diff", content: r.output });
    renderChips();
  } catch (e) { toast(e.message); }
};

function syncProj() {
  const on = store.get("proj", false);
  $("#projBtn").textContent = "Project: " + (on ? "on" : "off");
  $("#projBtn").classList.toggle("on", on);
}
$("#projBtn").onclick = () => { store.set("proj", !store.get("proj", false)); syncProj(); };
syncProj();

$("#ctx").value = String(store.get("ctx", 8192));
$("#ctx").onchange = () => store.set("ctx", Number($("#ctx").value));

async function projectContext() {
  if (!store.get("proj", false) || !currentRepo) return "";
  try {
    const j = await api("/api/context?repo=" + encodeURIComponent(currentRepo));
    return "Project context for repo " + currentRepo + ".\nGit status:\n" + j.status.trim() +
      "\nFiles:\n" + j.tree + (j.more ? "\n... and " + j.more + " more files" : "");
  } catch (e) { return ""; }
}

// ---------- downloads ----------
const EXT = { python: "py", py: "py", javascript: "js", js: "js", typescript: "ts", ts: "ts", html: "html",
  css: "css", json: "json", bash: "sh", sh: "sh", shell: "sh", markdown: "md", md: "md", yaml: "yml",
  yml: "yml", java: "java", c: "c", cpp: "cpp", go: "go", rust: "rs", rs: "rs", ruby: "rb", php: "php", sql: "sql" };

function saveBlob(name, blob) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 10000);
}

function downloadBlock(code, lang, def) {
  const name = window.prompt("File name to download", def ? def.split("/").pop() : "file." + (EXT[lang] || "txt"));
  if (!name || !name.trim()) return;
  saveBlob(name.trim().split("/").pop(), new Blob([code + "\n"], { type: "text/plain" }));
}

async function downloadZip(changed) {
  if (!currentRepo) return;
  let name = currentRepo;
  if (changed) {
    const n = window.prompt("Name this change (example: fix-login)", "changes");
    if (!n || !n.trim()) return;
    name = n.trim();
  }
  const url = "/api/zip?repo=" + encodeURIComponent(currentRepo) + (changed ? "&changed=1" : "") +
    "&name=" + encodeURIComponent(name);
  $("#out").textContent = "zipping...";
  try {
    const r = await fetch(url);
    if (!r.ok) {
      let m = r.statusText;
      try { m = (await r.json()).error || m; } catch (x) { /* ignore */ }
      throw new Error(m);
    }
    const mt = (r.headers.get("Content-Disposition") || "").match(/filename="([^"]+)"/);
    const fname = mt ? mt[1] : name + ".zip";
    saveBlob(fname, await r.blob());
    $("#out").textContent = "Downloaded " + fname;
  } catch (e) {
    $("#out").textContent = e.message;
  }
}
$("#dlRepo").onclick = () => downloadZip(false);
$("#dlChanged").onclick = () => downloadZip(true);

// ---------- uploads ----------
function readB64(file) {
  return new Promise((res, rej) => {
    const fr = new FileReader();
    fr.onload = () => res(String(fr.result).split(",")[1] || "");
    fr.onerror = () => rej(new Error("read failed"));
    fr.readAsDataURL(file);
  });
}

// device files into the repo (zips can be extracted)
$("#upBtn").onclick = () => {
  if (!currentRepo) { toast("Pick a repo first"); return; }
  $("#upFile").click();
};
$("#upFile").onchange = async () => {
  const files = Array.from($("#upFile").files);
  $("#upFile").value = "";
  const dir = $("#upDir").value.trim();
  const lines = [];
  for (const f of files) {
    if (f.size > 15000000) { lines.push(f.name + ": too large (max 15 MB)"); continue; }
    const extract = /\.zip$/i.test(f.name) &&
      window.confirm("Extract " + f.name + " into the repo?\nCancel saves it as a zip file.");
    $("#out").textContent = "uploading " + f.name + "...";
    try {
      const r = await api("/api/upload", { repo: currentRepo, dir, name: f.name, data: await readB64(f), extract });
      lines.push(r.output);
    } catch (e) {
      lines.push(f.name + ": " + e.message);
    }
  }
  $("#out").textContent = lines.join("\n");
};

// device text files into the chat as context
$("#chatUp").onclick = () => $("#chatFile").click();
$("#chatFile").onchange = async () => {
  const files = Array.from($("#chatFile").files);
  $("#chatFile").value = "";
  for (const f of files) {
    if (f.size > 300000) { toast(f.name + " is too large (max 300 KB)"); continue; }
    const text = await f.text();
    if (text.includes("\u0000")) { toast(f.name + " is not a text file"); continue; }
    if (!attachments.some((a) => a.path === f.name)) attachments.push({ path: f.name, content: text });
  }
  renderChips();
};

// ---------- clone from chat ----------
async function chatClone(shown, url, name) {
  history.push({ role: "user", content: shown, shown, local: true });
  addMsg("user", shown);
  const el = addMsg("assistant", "cloning...", true);
  let text;
  try {
    const r = await api("/api/clone", { url, name });
    text = (r.ok ? "Cloned " + r.name + " and selected it.\n" : "Clone failed.\n") + (r.output || "").trim();
    if (r.ok) selectRepo(r.name);
  } catch (e) {
    text = "Clone failed: " + e.message;
  }
  renderText(el, text);
  history.push({ role: "assistant", content: text, local: true });
  saveHist();
}

// ---------- lightweight memory ----------
let memNotes = [];
async function loadMemory() {
  try { memNotes = (await api("/api/memory")).notes; } catch (e) { /* ignore */ }
}
async function memoryCmd(shown, cmd, arg) {
  history.push({ role: "user", content: shown, shown, local: true });
  addMsg("user", shown);
  let text;
  try {
    if (cmd === "remember" && arg) memNotes = (await api("/api/memory", { notes: memNotes.concat(arg) })).notes;
    else if (cmd === "forget" && /^\d+$/.test(arg)) memNotes = (await api("/api/memory", { notes: memNotes.filter((_, i) => i !== Number(arg) - 1) })).notes;
    else if (cmd === "forget" && arg.toLowerCase() === "all") memNotes = (await api("/api/memory", { notes: [] })).notes;
    else if (cmd !== "memory") { toast("Use: remember <text> | forget <n|all> | memory"); history.pop(); drawHistory(); return; }
    text = memNotes.length ? "Memory:\n" + memNotes.map((n, i) => (i + 1) + ". " + n).join("\n") : "Memory is empty.";
  } catch (e) {
    text = "Memory error: " + e.message;
  }
  addMsg("assistant", text, true);
  history.push({ role: "assistant", content: text, local: true });
  saveHist();
}
loadMemory();

// ---------- chat ----------
const SYSTEM = "You are a coding assistant running on the user's own server. Explain code and errors clearly, write functions and complete files, debug, refactor and optimise. Be concise. For every file you write, put a line \"File: path/to/file\" right before its fenced code block, give the complete file contents unless a snippet is asked for, and tag the block with its language. If you need a file you cannot see, ask for it.";
let history = store.get("hist", []);
let ctrl = null;

// Persist the chat. Big automation prompts are stored as their short summary, and the
// oldest messages are dropped if the browser storage quota is hit.
function saveHist() {
  let h = history.map((m) => (m.big ? { role: m.role, content: m.shown, shown: m.shown } : m));
  for (;;) {
    try { localStorage.setItem("hist", JSON.stringify(h)); return; } catch (e) {
      if (h.length <= 2) return;
      h = h.slice(2);
    }
  }
}

function cleanRel(p) {
  p = String(p || "").trim().replace(/^\.\//, "");
  return p && !p.startsWith("/") && !p.split("/").includes("..") ? p : "";
}

function renderText(el, text, done) {
  el.textContent = "";
  let prev = "";
  const blocks = [];
  const pieces = text.split("```");
  const unclosed = pieces.length % 2 === 0;
  pieces.forEach((part, i) => {
    if (i % 2 === 0) {
      prev = part;
      if (part) { const s = document.createElement("span"); s.textContent = part; el.append(s); }
      return;
    }
    const nl = part.indexOf("\n");
    const lang = nl >= 0 ? part.slice(0, nl).trim().toLowerCase() : "";
    const fm = prev.trimEnd().split("\n").pop().match(/(?:File|Path):\s*[`*]*([^\s`*]+)/i);
    const fpath = fm ? fm[1] : "";
    let code = nl >= 0 ? part.slice(nl + 1) : "";
    code = code.replace(/\n$/, "");
    if (fpath && cleanRel(fpath) && !(unclosed && i === pieces.length - 1)) blocks.push({ path: cleanRel(fpath), content: code + "\n" });
    const wrap = document.createElement("div");
    wrap.className = "code";
    const pre = document.createElement("pre");
    pre.textContent = code;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "Save to repo";
    btn.onclick = () => saveBlock(code, fpath);
    const dl = document.createElement("button");
    dl.type = "button";
    dl.textContent = "Download";
    dl.onclick = () => downloadBlock(code, lang, fpath);
    wrap.append(pre, btn, dl);
    el.append(wrap);
  });
  if (done && blocks.length) {
    // the last block for a path wins if the model repeated a file
    const byPath = new Map();
    blocks.forEach((b) => byPath.set(b.path, b));
    const files = Array.from(byPath.values());
    const bar = document.createElement("div");
    bar.className = "applybar";
    const mk = (label, check) => {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = label;
      b.onclick = () => applyFiles(files, check, b);
      return b;
    };
    const n = document.createElement("span");
    n.className = "muted";
    n.textContent = files.length + " file" + (files.length === 1 ? "" : "s") + ":";
    const rv = document.createElement("button");
    rv.type = "button";
    rv.textContent = "Review patch";
    rv.onclick = () => showTab("patch");
    bar.append(n, mk("Apply to repo", false), mk("Apply and check", true), rv);
    el.append(bar);
  }
}

async function applyFiles(files, check, btn) {
  if (!currentRepo) { toast("Pick a repo first"); return; }
  const names = files.slice(0, 10).map((f) => f.path).join("\n") + (files.length > 10 ? "\n...and " + (files.length - 10) + " more" : "");
  if (!window.confirm("Write " + files.length + " file(s) into " + currentRepo + "? Existing files with these paths are overwritten (git can undo this).\n\n" + names)) return;
  btn.disabled = true;
  try {
    const r = await api("/api/apply", { repo: currentRepo, files });
    noteMsg("Applied to " + currentRepo + ":\n" + r.output);
    if (check) await autoChecks(true);
    else toast("Applied " + r.count + " file(s). Open the Patch tab to review and zip.");
  } catch (e) {
    noteMsg("Apply failed: " + e.message);
  } finally {
    btn.disabled = false;
  }
}

// a local note in the chat (not sent to the model)
function noteMsg(text) {
  history.push({ role: "assistant", content: text, local: true });
  addMsg("assistant", text, true);
  saveHist();
}

async function saveBlock(code, def) {
  if (!currentRepo) { toast("Pick a repo first"); return; }
  const path = prompt("Save to path in " + currentRepo + " (example: src/app.py)", def || "");
  if (!path) return;
  try {
    await api("/api/file", { repo: currentRepo, path: path.trim(), content: code + "\n" });
    toast("Saved " + path.trim());
  } catch (e) { toast(e.message); }
}

function addMsg(role, text, note, streaming) {
  const d = document.createElement("div");
  d.className = "msg " + role + (note ? " note" : "");
  if (role === "assistant") renderText(d, text, !streaming); else d.textContent = text;
  $("#msgs").append(d);
  $("#msgs").scrollTop = $("#msgs").scrollHeight;
  return d;
}

function drawHistory() {
  $("#msgs").textContent = "";
  history.forEach((m) => addMsg(m.role, m.shown || m.content, m.local));
}

$("#newChat").onclick = () => {
  if (ctrl) ctrl.abort();
  history = [];
  saveHist();
  drawHistory();
};

function setBusy(b) {
  const s = $("#send");
  s.textContent = b ? "Stop" : "Send";
  s.type = b ? "button" : "submit";
}
$("#send").onclick = () => { if (ctrl) ctrl.abort(); };

$("#composer").onsubmit = async (e) => {
  e.preventDefault();
  if (ctrl) return;
  const prompt = $("#input").value.trim();
  const model = $("#model").value;
  if (!prompt) return;
  const mm = prompt.match(/^\/?(remember|forget|memory)\b\s*(.*)$/i);
  if (mm) { $("#input").value = ""; await memoryCmd(prompt, mm[1].toLowerCase(), mm[2].trim()); return; }
  const cm = prompt.match(/^\/?clone\s+(\S+)(?:\s+(\S+))?$/i);
  if (cm) { $("#input").value = ""; await chatClone(prompt, cm[1], cm[2] || ""); return; }
  if (!model) { toast("No model. Is the tunnel up?"); return; }
  let content = prompt;
  let shown = prompt;
  if (attachments.length) {
    const ctx = attachments.map((a) => "File: " + a.path + "\n```\n" + a.content + "\n```").join("\n\n");
    content = ctx + "\n\n" + prompt;
    shown = prompt + "\n[files: " + attachments.map((a) => a.path).join(", ") + "]";
  }
  $("#input").value = "";
  await runChat(content, shown, false);
};

// Send one prompt to the model and stream the answer into the chat.
// big = an automation prompt that carries file contents (kept in context only until the next big one).
async function runChat(content, shown, big) {
  const model = $("#model").value;
  if (!model) { toast("No model. Is the tunnel up?"); return; }
  if (ctrl) { toast("Wait for the current reply or press Stop"); return; }
  history.push({ role: "user", content, shown, big: !!big });
  addMsg("user", shown);
  const el = addMsg("assistant", "...", false, true);
  let text = "";
  ctrl = new AbortController();
  setBusy(true);
  try {
    const messages = [{ role: "system", content: SYSTEM }];
    const pc = await projectContext();
    if (memNotes.length) messages.push({ role: "system", content: "Things to remember about the user and their projects:\n- " + memNotes.join("\n- ") });
    if (pc) messages.push({ role: "system", content: pc });
    const live = history.filter((m) => !m.local);
    let lastBig = -1;
    live.forEach((m, i) => { if (m.big) lastBig = i; });
    live.forEach((m, i) => {
      // older automation prompts are replaced by their summary so they do not fill the context window
      const c = m.big && i !== lastBig ? m.shown + "\n[file contents omitted to save context]" : m.content;
      messages.push({ role: m.role, content: c });
    });
    const r = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-PWA": "1" },
      body: JSON.stringify({ model, messages, stream: true, options: { num_ctx: Number($("#ctx").value) || 8192 } }),
      signal: ctrl.signal,
    });
    if (!r.ok) {
      let m = r.statusText;
      try { m = (await r.json()).error || m; } catch (x) { /* ignore */ }
      throw new Error(m);
    }
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split("\n");
      buf = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        let j;
        try { j = JSON.parse(line); } catch (x) { continue; }
        if (j.error) throw new Error(j.error);
        if (j.message && j.message.content) text += j.message.content;
      }
      renderText(el, text, false);
      $("#msgs").scrollTop = $("#msgs").scrollHeight;
    }
  } catch (err) {
    if (err.name !== "AbortError") {
      el.classList.add("err");
      text += (text ? "\n\n" : "") + "[error: " + err.message + "]";
    } else {
      text += "\n[stopped]";
    }
  } finally {
    renderText(el, text, true);
    ctrl = null;
    setBusy(false);
    history.push({ role: "assistant", content: text });
    if (history.length > 40) history = history.slice(-40);
    saveHist();
  }
}

// ---------- AI development automation ----------
// Each action asks the server for facts (checks, scans, file text), builds a prompt that fits
// the context budget, and sends it through the normal chat so you can follow up and Apply files.
let autoBusy = false;
const AUTO_LABEL = {
  analyze: "Analyze project", bugs: "Find and fix bugs", feature: "Build a feature", review: "Review code",
  tests: "Generate tests", docs: "Write docs", deps: "Check dependencies",
  explain: "Explain repo", arch: "Architecture diagram", api: "Analyse API endpoints", debt: "Technical debt", plan: "Plan milestones",
};

function autoBudget() { return Math.round((Number($("#ctx").value) || 8192) * 1.6); }

function autoRefresh() {
  $("#aLine").textContent = currentRepo
    ? currentRepo + " | about " + autoBudget() + " characters of code per request (set by the ctx menu in Chat)"
    : "Pick a repo in the Repos tab first.";
}

function fence(c) { return c.includes("```") ? "~~~~" : "```"; }
function fmtFiles(files) {
  return files.map((f) => {
    const f3 = fence(f.content);
    return "File: " + f.path + (f.truncated ? " (cut: file is longer)" : "") + "\n" + f3 + "\n" + f.content + "\n" + f3;
  }).join("\n\n");
}

function checkSummary(j) {
  const lines = [];
  if (j.syntax.length) {
    lines.push("Syntax problems (" + j.syntax.length + "):");
    j.syntax.slice(0, 30).forEach((x) => lines.push("- " + x.path + (x.line ? ":" + x.line : "") + " " + x.msg));
  } else {
    lines.push("Syntax: no problems in " + j.files_checked + " files.");
  }
  const t = j.tests;
  lines.push(t.ran ? "Tests (" + t.cmd + "): " + (t.ok ? "PASSED" : "FAILED") + " in " + t.seconds + " s" : "Tests: none found or not run.");
  return lines.join("\n");
}

// Run syntax check and tests, and post the result as a note in the chat.
async function autoChecks(announce) {
  const j = await api("/api/check", { repo: currentRepo, run_tests: $("#aRun").checked });
  let text = "Checks on " + currentRepo + ":\n" + checkSummary(j);
  if (j.tests.ran && !j.tests.ok) text += "\n\n" + j.tests.output.slice(-1800);
  if (announce && (j.syntax.length || (j.tests.ran && !j.tests.ok))) text += "\n\nUse Auto > Find and fix bugs to send the failures to the model.";
  noteMsg(text);
  return j;
}

const AUTO = {
  async analyze(req, step) {
    step("Scanning project...");
    const j = await api("/api/analyze?repo=" + enc(currentRepo) + "&budget=" + Math.round(autoBudget() * 0.5));
    const langs = j.languages.map((l) => l.name + " " + l.files + " files, " + l.loc + " lines").join("; ") || "none detected";
    step(j.files + " files. " + langs);
    const content = [
      "Analyse the repository \"" + j.repo + "\" using only the facts below.",
      ...(req ? ["Focus: " + req] : []),
      "", "Facts:",
      "- " + j.files + " files. Languages: " + langs,
      "- Manifests: " + (j.manifests.join(", ") || "none"),
      "- Entry points: " + (j.entry.join(", ") || "none found"),
      "- Tests: " + (j.has_tests ? j.tests.slice(0, 15).join(", ") : "none found"),
      "", "Key files:", fmtFiles(j.key_files.map((k) => ({ path: k.path, content: k.head }))),
      "", "File list:", j.tree + (j.more ? "\n... and " + j.more + " more files" : ""),
      "", "Answer with these sections: Purpose; Architecture (main modules and how they connect); How to run, build and test; " +
        "Risks and weak spots (security, error handling, missing tests, missing docs); Top 5 next steps, each naming the file and the change. " +
        "Do not invent files that are not listed, and say what you could not see.",
    ].join("\n");
    return { content, shown: "Analyze project" + (req ? ": " + req : "") + "\n[" + j.files + " files, " + j.languages.length + " languages]" };
  },

  async bugs(req, step) {
    const run = $("#aRun").checked;
    step("Checking syntax" + (run ? " and running tests (up to 3 min)" : "") + "...");
    const j = await api("/api/check", { repo: currentRepo, run_tests: run });
    step(checkSummary(j));
    const t = j.tests;
    const failing = j.syntax.length > 0 || (t.ran && !t.ok);
    if (!failing && !req) { step("\nNothing failed. Describe a bug in the box above to investigate it."); return null; }
    let out = t.ran && !t.ok ? t.output : "";
    if (out.length > 4000) out = out.slice(0, 1500) + "\n[... cut ...]\n" + out.slice(-2500);
    const paths = j.syntax.map((x) => x.path);
    const re1 = /File "([^"]+)"/g;
    const re2 = /([\w./-]+\.(?:py|js|mjs|ts|jsx|tsx|go|rs|java|rb|php|sh)):\d+/g;
    let m;
    while ((m = re1.exec(out)) && paths.length < 30) paths.push(m[1]);
    while ((m = re2.exec(out)) && paths.length < 30) paths.push(m[1]);
    step("Reading the files involved...");
    const b = await api("/api/bundle", { repo: currentRepo, paths: Array.from(new Set(paths)), scope: "query",
      query: req + " " + out.slice(0, 1500), budget: Math.round(autoBudget() * 0.75) });
    const content = [
      "Find and fix the bugs in repo \"" + currentRepo + "\".",
      ...(req ? ["", "User report: " + req] : []),
      "", "Automated checks:", checkSummary(j),
      ...(out ? ["", "Test output:", "```", out, "```"] : []),
      "", "Relevant files:", fmtFiles(b.files),
      "", "For each problem: state the root cause in one or two sentences, then give the fix. Show every changed file in full, " +
        "each preceded by a line \"File: path\" and a fenced block, so it can be applied directly. Change only what is needed. " +
        "If the checks pass, look for likely bugs in the files shown (logic errors, unhandled edge cases) and cite line numbers. " +
        "If you need a file that is not shown, name it and stop.",
    ].join("\n");
    return { content, shown: "Find and fix bugs" + (req ? ": " + req : "") + "\n[" + j.syntax.length + " syntax problems, tests " +
      (t.ran ? (t.ok ? "passed" : "failed") : "not run") + ", " + b.files.length + " files read]" };
  },

  async feature(req, step) {
    step("Finding the relevant files...");
    const [ctx, b] = await Promise.all([
      api("/api/context?repo=" + enc(currentRepo)),
      api("/api/bundle", { repo: currentRepo, scope: "query", query: req, budget: Math.round(autoBudget() * 0.75) }),
    ]);
    step(b.files.length + " files read: " + b.files.map((f) => f.path).join(", "));
    const content = [
      "Implement this feature in repo \"" + currentRepo + "\":", req,
      "", "Project files:", ctx.tree.slice(0, 3000), "",
      "Existing code that is likely relevant:", fmtFiles(b.files),
      "", "First give a short plan (3 to 6 bullets: which files change or are created and why). Then give every new or changed file in full, " +
        "each preceded by a line \"File: path\" and a fenced block with the language. Match the existing style, use only libraries already in the project " +
        "unless you say otherwise, and do not rewrite unrelated code. End with how to try the feature. " +
        "If you need a file that is not shown, name it and stop.",
    ].join("\n");
    return { content, shown: "Build feature: " + req + "\n[" + b.files.length + " files read]" };
  },

  async review(req, step) {
    const scope = $("#aScope").value;
    step("Scanning " + (scope === "changes" ? "changed files" : "the whole project") + "...");
    const sc = await api("/api/review_scan?repo=" + enc(currentRepo) + "&scope=" + scope);
    if (!sc.files) { step("No changed files. Set the scope to whole project."); return null; }
    const c = sc.counts;
    step(sc.files + " files. Findings: " + c.high + " high, " + c.medium + " medium, " + c.low + " low");
    let diff = "";
    if (scope === "changes") {
      const d = await api("/api/diff?repo=" + enc(currentRepo));
      diff = (d.output || "").slice(0, Math.round(autoBudget() * 0.35));
    }
    const b = await api("/api/bundle", { repo: currentRepo, scope: scope === "changes" ? "changes" : (req ? "query" : "all"),
      query: req, budget: Math.round(autoBudget() * (diff ? 0.4 : 0.75)) });
    const found = sc.findings.slice(0, 40).map((f) => "- [" + f.severity + "] " + f.path + ":" + f.line + " " + f.message + (f.text ? " -> " + f.text : ""));
    const content = [
      "Review the code in repo \"" + currentRepo + "\" (" + (scope === "changes" ? "changed files" : "whole project") + ").",
      ...(req ? ["Focus: " + req] : []),
      "", "Automated pattern scan (" + c.high + " high, " + c.medium + " medium, " + c.low + " low" + (sc.capped ? ", list capped" : "") + "):",
      found.join("\n") || "(nothing flagged)",
      ...(diff ? ["", "Git diff against the last commit (tracked files):", "```diff", diff, "```"] : []),
      "", "Files:", fmtFiles(b.files),
      "", "Review like a senior engineer. The scan is a simple pattern matcher: confirm each real finding, dismiss false positives with a reason, " +
        "and add problems it cannot see (logic bugs, missing validation, error handling, races, performance, readability). " +
        "For each issue give: severity, file:line, what is wrong, how to fix it. End with the 3 most important fixes. " +
        "Write corrected files (\"File: path\" plus a fenced block, full contents) only for high severity issues.",
    ].join("\n");
    return { content, shown: "Review code (" + (scope === "changes" ? "changes" : "whole project") + ")" + (req ? ": " + req : "") +
      "\n[scan: " + c.high + " high, " + c.medium + " medium, " + c.low + " low; " + b.files.length + " files read]" };
  },

  async tests(req, step) {
    const scope = $("#aScope").value;
    step("Reading the project...");
    const a = await api("/api/analyze?repo=" + enc(currentRepo) + "&budget=2000");
    const b = await api("/api/bundle", { repo: currentRepo, scope: req ? "query" : (scope === "changes" ? "changes" : "all"),
      query: req, budget: Math.round(autoBudget() * 0.8) });
    const src = b.files.filter((f) => !a.tests.includes(f.path));
    if (!src.length) { step("No source files to test in this scope."); return null; }
    step(src.length + " source files, " + (a.has_tests ? a.tests.length + " existing test files" : "no tests yet"));
    const content = [
      "Write tests for repo \"" + currentRepo + "\"." + (req ? " Focus: " + req : ""),
      "Languages: " + (a.languages.map((l) => l.name).join(", ") || "unknown") + ". Existing tests: " + (a.has_tests ? a.tests.slice(0, 15).join(", ") : "none") + ".",
      "", "Source files:", fmtFiles(src),
      ...(b.files.length > src.length ? ["", "Existing tests (keep their style, do not duplicate):", fmtFiles(b.files.filter((f) => a.tests.includes(f.path)))] : []),
      "", "Use the test framework the project already uses. If there is none, use unittest for Python (standard library) and node:test for JavaScript. " +
        "Write (1) unit tests: normal, edge and error cases for the public functions; (2) integration tests that exercise several parts together " +
        "(for example the HTTP API against a temporary folder, or file and git flows). No network and no real model calls: use temporary folders and fakes. " +
        "Name files tests/test_<module>.py (or <name>.test.js). Do not change the source files; if something cannot be tested as written, say so after the code. " +
        "Give each test file in full, preceded by \"File: tests/...\" and a fenced block, then the command to run them.",
    ].join("\n");
    return { content, shown: "Generate tests" + (req ? ": " + req : "") + " (" + (scope === "changes" && !req ? "changes" : "whole project") + ")\n[" + src.length + " source files read]" };
  },

  async docs(req, step) {
    step("Reading the project...");
    const a = await api("/api/analyze?repo=" + enc(currentRepo) + "&budget=1500");
    const b = await api("/api/bundle", { repo: currentRepo, paths: ["README.md"], scope: req ? "query" : "all", query: req, budget: autoBudget() });
    step(b.files.length + " files read" + (b.omitted ? ", " + b.omitted + " skipped (context budget)" : ""));
    const content = [
      "Write documentation for repo \"" + currentRepo + "\"." + (req ? " Focus: " + req : ""),
      "Languages: " + (a.languages.map((l) => l.name).join(", ") || "unknown") + ". Files: " + a.files + ".",
      "", "Files:", fmtFiles(b.files),
      "", "Produce two files. (1) README.md: title, what it does, features, requirements, install, usage with commands, configuration and environment variables, " +
        "project structure, how to test, troubleshooting. (2) docs/TECHNICAL.md: architecture, module by module description, data flow, " +
        "API or endpoint reference if there is one, extension points, known limitations. " +
        "Base everything on the files shown and keep the parts of the existing README that are still correct. Where something is unknown write \"TODO: ...\" instead of guessing. " +
        "Write each file in full, preceded by \"File: path\" and a fenced block. Inside the markdown files use 4-space indented code blocks, never triple backticks, so the file fits inside the fence.",
    ].join("\n");
    return { content, shown: "Write docs" + (req ? ": " + req : "") + "\n[" + b.files.length + " files read]" };
  },

  async deps(req, step) {
    step("Reading manifests and asking PyPI and npm for the latest versions...");
    const j = await api("/api/deps?repo=" + enc(currentRepo));
    if (!j.count) { step("No requirements*.txt, pyproject.toml or package.json found."); return null; }
    step(j.count + " dependencies, " + j.outdated + " behind" + (j.offline ? " (registries unreachable)" : "") + ", Python " + j.python);
    j.issues.forEach((i) => step("! " + i));
    const rows = j.deps.slice(0, 80).map((d) => "- " + d.eco + " " + d.name + " " + (d.spec || "(no constraint)") + (d.dev ? " [dev]" : "") +
      " in " + d.source + " | latest " + (d.latest || "unknown") + (d.behind ? " (" + d.behind + " behind)" : "") +
      (d.requires ? " | needs " + d.requires : "") + (d.problem ? " | " + d.problem : "") + (d.error ? " | lookup failed" : ""));
    const b = await api("/api/bundle", { repo: currentRepo, paths: Array.from(new Set(j.deps.map((d) => d.source))), scope: "paths", budget: 12000 });
    const content = [
      "Analyse the dependencies of repo \"" + currentRepo + "\"." + (req ? " Focus: " + req : ""),
      "Latest versions come from PyPI and npm just now. Python on this device: " + j.python + ".",
      ...(j.offline ? ["The registries could not be reached, so the latest versions are unknown. Say so."] : []),
      "", "Dependencies:", rows.join("\n"),
      ...(j.issues.length ? ["", "Detected issues:", ...j.issues.map((i) => "- " + i)] : []),
      "", "Manifests:", fmtFiles(b.files),
      "", "Report: (1) safe to update now (patch and minor); (2) needs care (major): what usually breaks; (3) incompatible or risky: Python or Node version limits, " +
        "conflicting pins, duplicates, URL installs, unpinned packages with a suggested constraint; (4) packages to drop or replace if you know they are abandoned or deprecated " +
        "(say \"not sure\" otherwise). You have no vulnerability database here, so do not claim security advisories; recommend pip-audit or npm audit for that. " +
        "Then give the updated manifest(s) for the safe updates only, in full, preceded by \"File: path\" and a fenced block.",
    ].join("\n");
    return { content, shown: "Check dependencies" + (req ? ": " + req : "") + "\n[" + j.count + " dependencies, " + j.outdated + " behind, " + j.issues.length + " issues]" };
  },
};

// ----- development intelligence helpers -----
function topList(items, fmt, n) { return items.slice(0, n || 10).map(fmt).join("\n") || "none"; }

// Layered SVG drawing of the import graph (no libraries). Longest-path layers, left to right.
function drawDiagram(files, edges) {
  const box = $("#aDiagram");
  box.textContent = "";
  const deg = {};
  edges.forEach((e) => { deg[e.from] = (deg[e.from] || 0) + 1; deg[e.to] = (deg[e.to] || 0) + 1; });
  let nodes = Array.from(new Set(edges.flatMap((e) => [e.from, e.to])));
  if (!nodes.length) nodes = files.slice(0, 12);
  nodes = nodes.sort((a, b) => (deg[b] || 0) - (deg[a] || 0) || (a < b ? -1 : 1)).slice(0, 30);
  const keep = new Set(nodes);
  const es = edges.filter((e) => keep.has(e.from) && keep.has(e.to));
  const layer = {};
  nodes.forEach((n) => { layer[n] = 0; });
  for (let it = 0; it < nodes.length; it++) {
    let ch = false;
    es.forEach((e) => { if (layer[e.to] < layer[e.from] + 1 && layer[e.from] + 1 < nodes.length) { layer[e.to] = layer[e.from] + 1; ch = true; } });
    if (!ch) break;
  }
  const cols = {};
  nodes.forEach((n) => { (cols[layer[n]] = cols[layer[n]] || []).push(n); });
  const W = 150, H = 26, GX = 50, GY = 12;
  const pos = {};
  const nCols = Object.keys(cols).length;
  let maxRows = 0;
  Object.keys(cols).sort((a, b) => a - b).forEach((c, ci) => {
    cols[c].sort();
    maxRows = Math.max(maxRows, cols[c].length);
    cols[c].forEach((n, ri) => { pos[n] = { x: 8 + ci * (W + GX), y: 8 + ri * (H + GY) }; });
  });
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("width", 16 + nCols * (W + GX) - GX);
  svg.setAttribute("height", 16 + maxRows * (H + GY) - GY);
  const mk = (tag, attrs) => { const e = document.createElementNS(ns, tag); Object.keys(attrs).forEach((k) => e.setAttribute(k, attrs[k])); return e; };
  es.forEach((e) => {
    const a = pos[e.from], b = pos[e.to];
    const x1 = a.x + W, y1 = a.y + H / 2, x2 = b.x, y2 = b.y + H / 2, mx = (x1 + x2) / 2;
    svg.appendChild(mk("path", { d: "M" + x1 + " " + y1 + " C" + mx + " " + y1 + " " + mx + " " + y2 + " " + x2 + " " + y2 }));
  });
  nodes.forEach((n) => {
    const p = pos[n];
    svg.appendChild(mk("rect", { x: p.x, y: p.y, width: W, height: H, rx: 6 }));
    const t = mk("text", { x: p.x + 6, y: p.y + 17 });
    t.textContent = n.length > 22 ? "…" + n.slice(-21) : n;
    const ti = document.createElementNS(ns, "title");
    ti.textContent = n;
    t.appendChild(ti);
    svg.appendChild(t);
  });
  const cap = document.createElement("div");
  cap.className = "cap";
  cap.textContent = "Imports go left to right: a file points to the files it uses. " + nodes.length + " files, " + es.length + " links.";
  box.appendChild(cap);
  box.appendChild(svg);
  box.hidden = false;
}

AUTO.explain = async function (req, step) {
  step("Scanning project...");
  const j = await api("/api/analyze?repo=" + enc(currentRepo) + "&budget=" + Math.round(autoBudget() * 0.4));
  const g = await api("/api/arch?repo=" + enc(currentRepo));
  const langs = j.languages.map((l) => l.name + " " + l.files + " files, " + l.loc + " lines").join("; ") || "none detected";
  step(j.files + " files. Most imported: " + (g.most_imported.map((x) => x.path).slice(0, 3).join(", ") || "n/a"));
  const content = [
    "I am new to the repository \"" + j.repo + "\". Explain it to me like a patient senior colleague, using only the facts below." + (req ? " Focus: " + req : ""),
    "", "Facts:",
    "- " + j.files + " files. Languages: " + langs,
    "- Manifests: " + (j.manifests.join(", ") || "none") + ". Entry points: " + (j.entry.join(", ") || "none found"),
    "- Most imported files (the core): " + topList(g.most_imported, (x) => x.path + " (" + x.count + ")", 6),
    "- Files that depend on the most others: " + topList(g.most_dependent, (x) => x.path + " (" + x.count + ")", 6),
    "- Tests: " + (j.has_tests ? j.tests.slice(0, 10).join(", ") : "none found"),
    "", "Key files:", fmtFiles(j.key_files.map((k) => ({ path: k.path, content: k.head }))),
    "", "File list:", j.tree + (j.more ? "\n... and " + j.more + " more files" : ""),
    "", "Answer with: 1) What it is in two sentences. 2) A reading order: the 5 files to open first and why. 3) How a typical request or action flows " +
      "through the code, step by step, naming files. 4) Key concepts and terms, one line each. 5) How to run it and where to make common changes. " +
      "6) What is confusing or fragile for a newcomer. Do not invent files or behaviour you cannot see, and say what you could not check.",
  ].join("\n");
  return { content, shown: "Explain repo" + (req ? ": " + req : "") + "\n[" + j.files + " files]" };
};

AUTO.arch = async function (req, step) {
  step("Reading imports...");
  const g = await api("/api/arch?repo=" + enc(currentRepo));
  step(g.files + " code files, " + g.edges.length + " import links, " + g.cycles.length + " cycles.");
  drawDiagram(g.isolated, g.edges);
  step("Diagram drawn above. Asking the model to explain it and write a Mermaid version.");
  const b = await api("/api/bundle", { repo: currentRepo, paths: g.most_imported.map((x) => x.path), scope: "paths", budget: Math.round(autoBudget() * 0.5) });
  const content = [
    "Describe the architecture of repo \"" + currentRepo + "\" from the import graph and files below." + (req ? " Focus: " + req : ""),
    "", "Import links (from -> to):", g.edges.slice(0, 150).map((e) => e.from + " -> " + e.to).join("\n") || "none found",
    "", "Folders (file counts): " + Object.keys(g.folders).map((k) => k + " " + g.folders[k]).join(", "),
    "Most imported: " + topList(g.most_imported, (x) => x.path + " (" + x.count + ")", 8),
    "Isolated files (no imports in or out): " + (g.isolated.join(", ") || "none"),
    "Circular imports: " + (g.cycles.map((c) => c.join(" -> ")).join(" | ") || "none"),
    "", "Core files:", fmtFiles(b.files),
    "", "Auto-generated Mermaid graph (file level) you may refine:", "~~~~", g.mermaid, "~~~~",
    "", "Answer with: (1) layers or components and what each does; (2) a Mermaid diagram of the components, in a fenced block tagged mermaid, " +
      "with at most 15 nodes and labelled arrows (for example `flowchart TD`); (3) the main data flow in numbered steps; " +
      "(4) architecture problems: cycles, god files, unclear boundaries, with the file names. Mention which links come only from imports and may miss " +
      "runtime links such as HTTP calls, since you cannot see those from imports.",
  ].join("\n");
  return { content, shown: "Architecture diagram" + (req ? ": " + req : "") + "\n[" + g.files + " files, " + g.edges.length + " links]" };
};

AUTO.api = async function (req, step) {
  step("Finding routes and callers...");
  const j = await api("/api/endpoints?repo=" + enc(currentRepo));
  if (!j.count) { step("No routes found (looked for Flask, FastAPI, Express, Django and route tables)."); return null; }
  step(j.count + " endpoints, " + j.calls + " calls in client code, " + j.unused.length + " not called from this repo.");
  const rows = j.endpoints.slice(0, 120).map((e) => "- " + e.method + " " + e.path + " -> " + (e.handler || "(inline)") + " [" + e.file + ":" + e.line + "]" +
    (e.called_by_repo ? "" : " NOT CALLED IN REPO") + (e.hints.length ? " hints: " + e.hints.join(",") : ""));
  const files = Array.from(new Set(j.endpoints.map((e) => e.file))).slice(0, 8);
  const b = await api("/api/bundle", { repo: currentRepo, paths: files, scope: "paths", budget: Math.round(autoBudget() * 0.6) });
  const content = [
    "Analyse the HTTP API of repo \"" + currentRepo + "\"." + (req ? " Focus: " + req : ""),
    "", "Endpoints found by a static scan (" + j.count + (j.capped ? ", list capped" : "") + "):", rows.join("\n"),
    ...(j.unknown_calls.length ? ["", "Client calls to paths with no matching route: " + j.unknown_calls.join(", ")] : []),
    "", "Source:", fmtFiles(b.files),
    "", "Report: (1) a table of endpoints: method, path, purpose, input, output; (2) consistency problems in naming, methods, status codes and error shapes; " +
      "(3) security: missing authentication, missing input validation, path traversal, command execution, CORS, rate limits (only what the code shows); " +
      "(4) endpoints nothing calls and client calls with no endpoint; (5) missing endpoints or tests, and the 5 most valuable fixes with file names. " +
      "The scan is static, so say when dynamically registered routes could be missing.",
  ].join("\n");
  return { content, shown: "Analyse API endpoints" + (req ? ": " + req : "") + "\n[" + j.count + " endpoints]" };
};

AUTO.debt = async function (req, step) {
  step("Measuring technical debt...");
  const j = await api("/api/debt?repo=" + enc(currentRepo));
  const c = j.scan_counts;
  step(j.marker_count + " TODO/FIXME markers, " + j.complex_functions.length + " heavy functions, " + j.long_files.length + " long files, " +
    j.duplicates.length + " duplicate blocks, " + j.untested.length + " untested files, scan: " + c.high + " high / " + c.medium + " medium / " + c.low + " low.");
  const hot = j.hotspots.map((x) => x.path).concat(j.complex_functions.map((x) => x.path), j.long_files.map((x) => x.path));
  const b = await api("/api/bundle", { repo: currentRepo, paths: Array.from(new Set(hot)).slice(0, 6), scope: "paths", budget: Math.round(autoBudget() * 0.5) });
  const content = [
    "Assess the technical debt of repo \"" + currentRepo + "\" from these measurements." + (req ? " Focus: " + req : ""),
    "", "Markers (" + j.marker_count + "):", topList(j.markers, (m) => "- " + m.path + ":" + m.line + " " + m.kind + " " + m.text, 25),
    "", "Long files:", topList(j.long_files, (f) => "- " + f.path + " " + f.lines + " lines"),
    "", "Heavy functions (long, many branches or many arguments):", topList(j.complex_functions, (f) => "- " + f.path + ":" + f.line + " " + f.name + " " + f.length + " lines, complexity " + f.complexity + ", " + f.args + " args"),
    "", "Duplicated 6-line blocks:", topList(j.duplicates, (d) => "- " + d.map((x) => x.path + ":" + x.line).join(" = ")),
    "", "Source files whose name never appears in the tests: " + (j.has_tests ? topList(j.untested, (u) => u.path + " (" + u.lines + ")", 10) : "there are no tests at all"),
    "", "Churn hotspots (changed often and big, last 180 days):", topList(j.hotspots, (x) => "- " + x.path + " " + x.commits + " commits, " + x.lines + " lines"),
    "", "Pattern scan: " + c.high + " high, " + c.medium + " medium, " + c.low + " low.", topList(j.scan_top, (f) => "- [" + f.severity + "] " + f.path + ":" + f.line + " " + (f.message || f.msg || f.id || ""), 10),
    "", "Worst files:", fmtFiles(b.files),
    "", "Report: (1) a debt register: item, file, why it hurts, effort (S/M/L), risk if ignored, ranked by value for effort; (2) quick wins for one afternoon; " +
      "(3) the refactors worth planning, with a safe first step for each; (4) what to leave alone. Use only the measurements shown and say when a figure is a rough heuristic.",
  ].join("\n");
  return { content, shown: "Technical debt" + (req ? ": " + req : "") + "\n[" + j.marker_count + " markers, " + j.complex_functions.length + " heavy functions]" };
};

AUTO.plan = async function (req, step) {
  step("Collecting project facts...");
  const a = await api("/api/analyze?repo=" + enc(currentRepo) + "&budget=2500");
  const d = await api("/api/debt?repo=" + enc(currentRepo));
  const e = await api("/api/endpoints?repo=" + enc(currentRepo)).catch(() => ({ count: 0, endpoints: [] }));
  let log = "";
  try {
    const l = await api("/api/log?repo=" + enc(currentRepo));
    log = (l.commits || l.log || []).slice(0, 15).map((c) => "- " + (c.subject || c.msg || c.message || JSON.stringify(c))).join("\n");
  } catch (x) { log = ""; }
  step(a.files + " files, " + d.marker_count + " markers, " + e.count + " endpoints.");
  const content = [
    "Plan development milestones for repo \"" + currentRepo + "\"." + (req ? " Goal from the owner: " + req : " No goal was given, so propose sensible goals and state your assumptions."),
    "", "State of the project:",
    "- " + a.files + " files; languages: " + (a.languages.map((l) => l.name + " " + l.loc + " lines").join(", ") || "unknown"),
    "- Tests: " + (a.has_tests ? a.tests.length + " test files" : "none") + ". API endpoints: " + e.count,
    "- Debt: " + d.marker_count + " TODO/FIXME, " + d.complex_functions.length + " heavy functions, " + d.long_files.length + " long files, " + d.untested.length + " untested files, scan " + d.scan_counts.high + " high / " + d.scan_counts.medium + " medium",
    "- Unfinished markers:", topList(d.markers, (m) => "  " + m.path + ":" + m.line + " " + m.text, 15),
    "", "Recent commits (newest first):", log || "not available",
    "", "README / key files:", fmtFiles(a.key_files.slice(0, 3).map((k) => ({ path: k.path, content: k.head.slice(0, 1500) }))),
    "", "Write: (1) a one-paragraph assessment of where the project stands; (2) 3 to 5 milestones, each with a goal, the concrete tasks (name the files), " +
      "a rough size in days for one developer working part time, dependencies, how to know it is done, and the main risk; (3) the order and why; " +
      "(4) what to cut if time runs short; (5) the single next task to start today. Keep it realistic for a solo developer working from a phone.",
  ].join("\n");
  return { content, shown: "Plan milestones" + (req ? ": " + req : "") + "\n[" + a.files + " files, " + d.marker_count + " markers]" };
};

async function autoRun(kind, reqText) {
  if (autoBusy) return;
  if (!currentRepo) { toast("Pick a repo in the Repos tab first"); return; }
  if (ctrl) { toast("Wait for the current chat reply or press Stop"); return; }
  if (!$("#model").value) { toast("No model. Is the tunnel up?"); return; }
  const req = reqText !== undefined ? reqText : $("#aReq").value.trim();
  if (kind === "feature" && !req) { toast("Describe the feature first"); $("#aReq").focus(); return; }
  if (kind === "bugs" && $("#aRun").checked &&
      !window.confirm("Running tests executes this project's code on your phone. Continue?\nUntick 'run tests' to only check syntax.")) return;
  autoBusy = true;
  const btns = document.querySelectorAll("#aBtns button");
  btns.forEach((b) => { b.disabled = true; });
  const out = $("#aOut");
  $("#aDiagram").hidden = true;
  out.textContent = AUTO_LABEL[kind] + "...\n";
  const step = (t) => { out.textContent += t + "\n"; };
  try {
    const built = await AUTO[kind](req, step);
    if (built) {
      step("\nSending to the model. The reply appears in the Chat tab.");
      showTab("chat");
      await runChat(built.content, built.shown, true);
    }
  } catch (e) {
    step("Error: " + e.message);
  } finally {
    autoBusy = false;
    btns.forEach((b) => { b.disabled = false; });
  }
}
document.querySelectorAll("#aBtns [data-a]").forEach((b) => { b.onclick = () => autoRun(b.dataset.a); });

// ---------- patch management ----------
// Ask Qwen -> Apply to repo (Chat) -> review the changed files here -> ZIP with only the ticked
// files, named after the change -> exact unzip and git commands.
const pUnchecked = new Set();
let pFiles = [];

function pSlug(text) {
  const words = String(text || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim().split(" ").filter(Boolean);
  return words.slice(0, 5).join("-").slice(0, 40);
}
function pSelected() { return pFiles.filter((c) => !pUnchecked.has(c.path)).map((c) => c.path); }

function pLoadFields() {
  $("#pReq").value = store.get("pReq", "");
  $("#pName").value = store.get("pName", "");
  $("#pMsg").value = store.get("pMsg", "");
  $("#pTarget").value = store.get("pTarget:" + currentRepo, "");
  $("#pDl").value = store.get("pDl", "");
}
["pReq", "pName", "pMsg", "pDl"].forEach((id) => { $("#" + id).oninput = () => store.set(id, $("#" + id).value); });
$("#pTarget").oninput = () => store.set("pTarget:" + currentRepo, $("#pTarget").value);

function pBody(paths) {
  return {
    repo: currentRepo, paths,
    name: $("#pName").value.trim() || pSlug($("#pReq").value) || "changes",
    request: $("#pReq").value.trim(), message: $("#pMsg").value.trim(),
    target: $("#pTarget").value.trim(), downloads: $("#pDl").value.trim(),
  };
}

async function patchRefresh() {
  const list = $("#pChanges");
  pLoadFields();
  if (!currentRepo) {
    $("#pLine").textContent = "Pick a repo in the Repos tab first.";
    list.textContent = "";
    pFiles = [];
    return;
  }
  try {
    const j = await api("/api/patch_plan", pBody());
    $("#pLine").textContent = currentRepo + (j.branch ? "  on " + j.branch : "  (detached HEAD)") + "  " +
      j.files.length + " changed file" + (j.files.length === 1 ? "" : "s");
    pFiles = j.files;
    const keep = new Set(pFiles.map((c) => c.path));
    Array.from(pUnchecked).forEach((p) => { if (!keep.has(p)) pUnchecked.delete(p); });
    list.textContent = "";
    if (!pFiles.length) {
      const d = document.createElement("div");
      d.className = "muted";
      d.style.padding = "8px";
      d.textContent = "No changes yet. Ask Qwen above and press Apply to repo in the Chat tab.";
      list.append(d);
    }
    pFiles.forEach((c) => {
      const row = document.createElement("div");
      row.className = "chg";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = !pUnchecked.has(c.path);
      cb.onchange = () => { if (cb.checked) pUnchecked.delete(c.path); else pUnchecked.add(c.path); };
      const bd = document.createElement("span");
      bd.className = "badge b" + (c.code === "?" ? "Q" : c.code);
      bd.textContent = c.code;
      const nm = document.createElement("span");
      nm.className = "nm";
      nm.textContent = c.path + (c.old ? "  (was " + c.old + ")" : "");
      nm.onclick = () => patchDiff(c);
      const cnt = document.createElement("span");
      cnt.className = "cnt";
      if (c.binary) cnt.textContent = "binary";
      else if (c.code !== "D") {
        const a = document.createElement("span");
        a.className = "a";
        a.textContent = "+" + c.added;
        const d = document.createElement("span");
        d.className = "d";
        d.textContent = " -" + c.removed;
        cnt.append(a, d);
      }
      row.append(cb, bd, nm, cnt);
      list.append(row);
    });
  } catch (e) {
    $("#pLine").textContent = e.message;
  }
}

async function patchDiff(c) {
  const v = $("#pView");
  v.textContent = "";
  const bar = document.createElement("div");
  bar.className = "row";
  const b = document.createElement("b");
  b.className = "muted";
  b.textContent = c.path;
  bar.append(b);
  v.append(bar);
  try {
    const j = await api("/api/filediff?repo=" + enc(currentRepo) + "&path=" + enc(c.path));
    v.append(diffEl(j.diff.trim() ? j.diff : "(no textual changes)"));
  } catch (e) {
    v.append(diffEl(e.message));
  }
}

function copyText(t) {
  if (navigator.clipboard && window.isSecureContext !== false) {
    return navigator.clipboard.writeText(t).then(() => toast("Copied"), () => copyFallback(t));
  }
  copyFallback(t);
  return Promise.resolve();
}
function copyFallback(t) {
  const ta = document.createElement("textarea");
  ta.value = t;
  document.body.append(ta);
  ta.select();
  try { document.execCommand("copy"); toast("Copied"); } catch (e) { toast("Select the text and copy it"); }
  ta.remove();
}

function patchShowCommands(j, zipSaved) {
  const out = $("#pOut");
  out.textContent = "";
  const note = (t) => {
    const d = document.createElement("div");
    d.className = "note";
    d.textContent = t;
    out.append(d);
  };
  note(j.zip_files
    ? (zipSaved ? "ZIP downloaded: " : "ZIP name: ") + j.zip + " (" + j.zip_files + " file" + (j.zip_files === 1 ? "" : "s") + ", only the ticked changes)."
    : "No ZIP needed: only deletions are selected.");
  if (j.removed.length) note("Deleted or renamed away (removed by git rm): " + j.removed.join(", "));
  note("Run these in a terminal, one line at a time or all at once. They assume the ZIP is in " + j.downloads +
    " and your repo is in " + j.target + " (change both under \"Where the files are installed\"). " +
    "First time in Termux: termux-setup-storage and pkg install unzip git.");
  const text = j.commands.join("\n");
  const row = document.createElement("div");
  row.className = "row";
  const cp = document.createElement("button");
  cp.type = "button";
  cp.textContent = "Copy commands";
  cp.onclick = () => copyText(text);
  row.append(cp);
  const pre = document.createElement("pre");
  pre.textContent = text;
  out.append(row, pre);
  if (out.scrollIntoView) out.scrollIntoView({ block: "nearest" });
}

async function patchGenerate(withZip) {
  if (!currentRepo) { toast("Pick a repo first"); return; }
  const paths = pSelected();
  if (!paths.length) { toast("Tick at least one file"); return; }
  const out = $("#pOut");
  out.textContent = "working...";
  try {
    const j = await api("/api/patch_plan", pBody(paths));
    let saved = false;
    if (withZip && j.zip_files) {
      const r = await fetch("/api/patch_zip", {
        method: "POST", headers: { "Content-Type": "application/json", "X-PWA": "1" },
        body: JSON.stringify(pBody(paths)),
      });
      if (!r.ok) {
        let m = r.statusText;
        try { m = (await r.json()).error || m; } catch (x) { /* ignore */ }
        throw new Error(m);
      }
      saveBlob(j.zip, await r.blob());
      saved = true;
    }
    patchShowCommands(j, saved);
  } catch (e) {
    out.textContent = e.message;
  }
}

$("#pAsk").onclick = async () => {
  const req = $("#pReq").value.trim();
  if (!currentRepo) { toast("Pick a repo in the Repos tab first"); return; }
  if (!req) { toast("Describe the change first"); $("#pReq").focus(); return; }
  if (!$("#pName").value.trim()) { $("#pName").value = pSlug(req); store.set("pName", $("#pName").value); }
  await autoRun("feature", req);
};
$("#pRefresh").onclick = () => patchRefresh();
$("#pAll").onclick = () => { pUnchecked.clear(); patchRefresh(); };
$("#pNone").onclick = () => { pFiles.forEach((c) => pUnchecked.add(c.path)); patchRefresh(); };
$("#pZip").onclick = () => patchGenerate(true);
$("#pCmds").onclick = () => patchGenerate(false);

// ---------- terminal and server management ----------
// Read-only commands run at once. Anything else asks you first, and the server refuses it
// unless the request says it was approved. A few destructive commands are always refused.
const QUICK = ["git status", "git log --oneline -10", "ls -la", "df -h", "free -m", "uptime", "ps",
  "python3 -m unittest discover -s tests", "pip list", "python3 --version"];
let tHist = store.get("tHist", []);
let tBusy = false;

function fmtBytes(n) {
  if (n === null || n === undefined) return "?";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (n >= 10 || i === 0 ? Math.round(n) : n.toFixed(1)) + " " + u[i];
}
function fmtUp(sec) {
  if (sec === null || sec === undefined) return "?";
  const d = Math.floor(sec / 86400), hh = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  return d ? d + " d " + hh + " h" : hh ? hh + " h " + m + " min" : m + " min";
}

function tPrint(text, cls) {
  const out = $("#tOut");
  const sp = document.createElement("span");
  if (cls) sp.className = cls;
  sp.textContent = text;
  out.append(sp);
  while (out.childNodes.length > 400) out.removeChild(out.firstChild);
  out.scrollTop = out.scrollHeight;
}

function tStatusLine(el, state, label, note) {
  el.textContent = "";
  const dot = document.createElement("span");
  dot.className = "dot " + state;
  dot.textContent = "●";
  el.append(dot, document.createTextNode(label));
  if (note) {
    const sm = document.createElement("small");
    sm.textContent = note;
    el.append(sm);
  }
}

async function tFill() {
  const sel = $("#tQuick");
  sel.textContent = "";
  sel.add(new Option("Quick and recent...", ""));
  const g1 = document.createElement("optgroup");
  g1.label = "Quick";
  QUICK.forEach((c) => g1.append(new Option(c, c)));
  sel.append(g1);
  if (tHist.length) {
    const g2 = document.createElement("optgroup");
    g2.label = "Recent";
    tHist.forEach((c) => g2.append(new Option(c, c)));
    sel.append(g2);
  }
  sel.value = "";
}
$("#tQuick").onchange = () => {
  const v = $("#tQuick").value;
  if (v) { $("#tCmd").value = v; $("#tCmd").focus(); }
  $("#tQuick").value = "";
};

function tFillCwd() {
  const sel = $("#tCwd");
  const keep = sel.value;
  sel.textContent = "";
  sel.add(new Option("in: workspace", ""));
  if (currentRepo) sel.add(new Option("in: " + currentRepo, currentRepo));
  sel.value = Array.from(sel.options).some((o) => o.value === keep) && keep ? keep : (currentRepo || "");
}

function tRemember(cmd) {
  tHist = [cmd].concat(tHist.filter((c) => c !== cmd)).slice(0, 15);
  store.set("tHist", tHist);
  tFill();
}

async function tExec(cmd, approved, quiet) {
  const cwd = $("#tCwd").value;
  if (!quiet) tPrint("$ " + cmd + "   [" + (cwd || "workspace") + "]\n", "t-cmd");
  const r = await fetch("/api/exec", {
    method: "POST", headers: { "Content-Type": "application/json", "X-PWA": "1" },
    body: JSON.stringify({ cmd, cwd, approved: !!approved }),
  });
  let j;
  try { j = await r.json(); } catch (e) { j = { error: "bad response" }; }
  if (r.status === 409 && j.needs_approval) {
    const ok = window.confirm("Run this command?\n\n$ " + cmd + "\n\nin: " + (cwd || "workspace") +
      "\nWhy it needs approval: " + j.reason + "\n\nIt runs on this phone with your permissions.");
    if (!ok) { tPrint("(not run)\n\n", "t-note"); return; }
    return tExec(cmd, true, true);
  }
  if (!r.ok) { tPrint("error: " + (j.error || r.statusText) + "\n\n", "t-bad"); return; }
  const text = j.output || "";
  tPrint(text ? (text.endsWith("\n") ? text : text + "\n") : "(no output)\n");
  tPrint("exit " + j.code + (j.timed_out ? " (timed out)" : "") + (j.truncated ? " (output cut)" : "") + "  " + j.seconds + " s\n\n",
    j.ok ? "t-ok" : "t-bad");
}

$("#tForm").onsubmit = async (e) => {
  e.preventDefault();
  const cmd = $("#tCmd").value.trim();
  if (!cmd || tBusy) return;
  tBusy = true;
  $("#tCmd").value = "";
  tRemember(cmd);
  try { await tExec(cmd, false, false); } catch (err) { tPrint("error: " + err.message + "\n\n", "t-bad"); }
  tBusy = false;
};
$("#tClear").onclick = () => { $("#tOut").textContent = ""; };

$("#tTests").onclick = async () => {
  if (!currentRepo) { toast("Pick a repo in the Repos tab first"); return; }
  if (tBusy) return;
  if (!window.confirm("Run the tests of " + currentRepo + "? This executes the project's code on this phone.")) return;
  tBusy = true;
  tPrint("$ run tests   [" + currentRepo + "]\n", "t-cmd");
  try {
    const j = await api("/api/check", { repo: currentRepo, run_tests: true });
    tPrint(checkSummary(j) + "\n");
    if (j.tests.ran) {
      tPrint((j.tests.output || "(no output)").replace(/\n?$/, "\n"));
      tPrint((j.tests.ok ? "PASSED" : "FAILED") + "  " + j.tests.seconds + " s\n\n", j.tests.ok ? "t-ok" : "t-bad");
    } else {
      tPrint("\n");
    }
  } catch (err) { tPrint("error: " + err.message + "\n\n", "t-bad"); }
  tBusy = false;
};

async function termOllama() {
  const el = $("#tOllama");
  try {
    const j = await api("/api/ollama_status");
    if (!j.up) { tStatusLine(el, "bad", "Ollama down", j.error + "\nStart the tunnel below, or check that Ollama is running on the server."); return; }
    const loaded = j.running.length
      ? "loaded: " + j.running.map((m) => m.name + " (" + fmtBytes(m.size_vram || m.size) + (m.size_vram ? " VRAM" : "") + ")").join(", ")
      : "no model loaded in memory";
    tStatusLine(el, "ok", "Ollama up, " + j.latency_ms + " ms" + (j.version ? ", v" + j.version : "") + ", " + j.models.length + " model" + (j.models.length === 1 ? "" : "s"), loaded);
  } catch (e) { tStatusLine(el, "bad", "Ollama status failed", e.message); }
}

async function termTunnel() {
  const el = $("#tTunnel");
  try {
    const j = await api("/api/tunnel");
    if (!j.local) { tStatusLine(el, "warn", "No tunnel in use", "OLLAMA_URL points to another machine."); return; }
    if (j.listening && j.pids.length) {
      tStatusLine(el, "ok", "Tunnel up, port " + j.port + ", ssh pid " + j.pids.join(", "), "to " + j.host);
    } else if (j.listening) {
      tStatusLine(el, "warn", "Port " + j.port + " is open but no ssh tunnel process was found", "Something else may be forwarding it. Restart replaces only our own ssh.");
    } else if (j.pids.length) {
      tStatusLine(el, "warn", "ssh is running but port " + j.port + " is closed", "Try Restart.\n" + j.log);
    } else {
      tStatusLine(el, "bad", "Tunnel down", (j.key_found ? "Press Start tunnel (to " + j.host + ")." : "SSH key not found. Set PWA_SSH_KEY.") + (j.log ? "\n" + j.log : ""));
    }
  } catch (e) { tStatusLine(el, "bad", "Tunnel status failed", e.message); }
}

async function termPhone() {
  const el = $("#tPhone");
  try {
    const j = await api("/api/sysinfo");
    const parts = [];
    parts.push(j.load ? "load " + j.load.join(" ") + " (" + j.cpus + " cpus)" : j.cpus + " cpus");
    if (j.mem_total) parts.push("memory " + fmtBytes(j.mem_available) + " free of " + fmtBytes(j.mem_total));
    if (j.disk) parts.push("disk " + fmtBytes(j.disk.free) + " free of " + fmtBytes(j.disk.total));
    parts.push("up " + fmtUp(j.uptime));
    tStatusLine(el, "ok", "This phone", parts.join("\n") + "\nPython " + j.python + ", app pid " + j.pid);
  } catch (e) { tStatusLine(el, "warn", "Resources unavailable", e.message); }
}

function termStatus() { termOllama(); termTunnel(); termPhone(); }

function termRefresh() {
  tFill();
  tFillCwd();
  termStatus();
}
$("#tStatRefresh").onclick = termStatus;
setInterval(() => {
  if ($("#term").classList.contains("on") && $("#tStat").open && !document.hidden) { termOllama(); termTunnel(); }
}, 10000);

async function tunnelAct(action) {
  const names = { start: "Start the SSH tunnel to the server?", stop: "Stop the SSH tunnel? Chat will not reach Ollama until it is started again.",
    restart: "Restart the SSH tunnel?" };
  if (!window.confirm(names[action])) return;
  const btns = ["#tTunStart", "#tTunStop", "#tTunRestart"].map((x) => $(x));
  btns.forEach((b) => { b.disabled = true; });
  tPrint("tunnel " + action + "...\n", "t-note");
  try {
    const j = await api("/api/tunnel", { action });
    tPrint(j.message + "\n\n", "t-ok");
    toast(j.message);
  } catch (e) {
    tPrint("tunnel error: " + e.message + "\n\n", "t-bad");
    toast(e.message);
  }
  btns.forEach((b) => { b.disabled = false; });
  termTunnel();
  termOllama();
  loadModels();
}
$("#tTunStart").onclick = () => tunnelAct("start");
$("#tTunStop").onclick = () => tunnelAct("stop");
$("#tTunRestart").onclick = () => tunnelAct("restart");

$("#tSrvBtn").onclick = async () => {
  const pre = $("#tSrv");
  pre.hidden = false;
  pre.textContent = "asking the server over SSH...";
  $("#tSrvBtn").disabled = true;
  try {
    const j = await api("/api/server_info");
    pre.textContent = (j.ok ? "" : "FAILED\n") + j.host + "\n\n" + (j.output || "(no output)");
  } catch (e) { pre.textContent = e.message; }
  $("#tSrvBtn").disabled = false;
};

// ---------- start ----------
drawHistory();
loadModels();
loadRepos();
if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
