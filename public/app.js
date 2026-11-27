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
  store.set("hist", history);
}

// ---------- chat ----------
const SYSTEM = "You are a coding assistant running on the user's own server. Explain code and errors clearly, write functions and complete files, debug, refactor and optimise. Be concise. For every file you write, put a line \"File: path/to/file\" right before its fenced code block, give the complete file contents unless a snippet is asked for, and tag the block with its language. If you need a file you cannot see, ask for it.";
let history = store.get("hist", []);
let ctrl = null;

function renderText(el, text) {
  el.textContent = "";
  let prev = "";
  text.split("```").forEach((part, i) => {
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

function addMsg(role, text, note) {
  const d = document.createElement("div");
  d.className = "msg " + role + (note ? " note" : "");
  if (role === "assistant") renderText(d, text); else d.textContent = text;
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
  store.set("hist", history);
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
  history.push({ role: "user", content, shown });
  addMsg("user", shown);
  const el = addMsg("assistant", "...");
  let text = "";
  ctrl = new AbortController();
  setBusy(true);
  try {
    const messages = [{ role: "system", content: SYSTEM }];
    const pc = await projectContext();
    if (pc) messages.push({ role: "system", content: pc });
    history.filter((m) => !m.local).forEach((m) => messages.push({ role: m.role, content: m.content }));
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
      renderText(el, text);
      $("#msgs").scrollTop = $("#msgs").scrollHeight;
    }
  } catch (err) {
    if (err.name !== "AbortError") {
      el.classList.add("err");
      text += (text ? "\n\n" : "") + "[error: " + err.message + "]";
    } else {
      text += "\n[stopped]";
    }
    renderText(el, text);
  } finally {
    ctrl = null;
    setBusy(false);
    history.push({ role: "assistant", content: text });
    if (history.length > 40) history = history.slice(-40);
    store.set("hist", history);
  }
};

// ---------- start ----------
drawHistory();
loadModels();
loadRepos();
if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => {});
