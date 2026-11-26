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
document.querySelectorAll("nav button").forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll("nav button").forEach((x) => x.classList.toggle("on", x === b));
    document.querySelectorAll(".tab").forEach((s) => s.classList.toggle("on", s.id === b.dataset.tab));
  };
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
    const { repos } = await api("/api/repos");
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
  loadRepos();
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

// ---------- chat ----------
const SYSTEM = "You are a concise coding assistant. When you give code for a file, put it in a fenced code block.";
let history = store.get("hist", []);
let ctrl = null;

function renderText(el, text) {
  el.textContent = "";
  text.split("```").forEach((part, i) => {
    if (i % 2 === 0) {
      if (part) { const s = document.createElement("span"); s.textContent = part; el.append(s); }
      return;
    }
    const nl = part.indexOf("\n");
    let code = nl >= 0 ? part.slice(nl + 1) : "";
    code = code.replace(/\n$/, "");
    const wrap = document.createElement("div");
    wrap.className = "code";
    const pre = document.createElement("pre");
    pre.textContent = code;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "Save to repo";
    btn.onclick = () => saveBlock(code);
    wrap.append(pre, btn);
    el.append(wrap);
  });
}

async function saveBlock(code) {
  if (!currentRepo) { toast("Pick a repo first"); return; }
  const path = prompt("Save to path in " + currentRepo + " (example: src/app.py)");
  if (!path) return;
  try {
    await api("/api/file", { repo: currentRepo, path: path.trim(), content: code + "\n" });
    toast("Saved " + path.trim());
  } catch (e) { toast(e.message); }
}

function addMsg(role, text) {
  const d = document.createElement("div");
  d.className = "msg " + role;
  if (role === "assistant") renderText(d, text); else d.textContent = text;
  $("#msgs").append(d);
  $("#msgs").scrollTop = $("#msgs").scrollHeight;
  return d;
}

function drawHistory() {
  $("#msgs").textContent = "";
  history.forEach((m) => addMsg(m.role, m.shown || m.content));
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
    const messages = [{ role: "system", content: SYSTEM }].concat(history.map((m) => ({ role: m.role, content: m.content })));
    const r = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-PWA": "1" },
      body: JSON.stringify({ model, messages, stream: true }),
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
