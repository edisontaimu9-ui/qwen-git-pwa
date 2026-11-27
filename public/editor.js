// Code editor extras for the Files tab: syntax highlighting, line numbers,
// Tab indent and word completion. No dependencies. Works on top of #edText.
(function () {
  const $ = (s) => document.querySelector(s);
  const ta = $("#edText"), hi = $("#edHi"), gut = $("#edGutter"), sug = $("#edSug");
  if (!ta || !hi || !gut || !sug) return;

  const MAX_HL = 300000;
  const KW = {
    js: "async await break case catch class const continue default delete do else export extends false finally for from function if import in instanceof let new null of return static super switch this throw true try typeof undefined var void while with yield interface type enum implements public private protected readonly console document window require module JSON Math Promise Array Object String Number Map Set".split(" "),
    py: "and as assert async await break class continue def del elif else except False finally for from global if import in is lambda None nonlocal not or pass raise return True try while with yield self print len range dict list set tuple str int float open isinstance enumerate zip super".split(" "),
    css: "display flex grid position absolute relative fixed margin padding border background color font-size font-family width height max-width min-width overflow z-index opacity transition transform align-items justify-content gap cursor important none auto inherit".split(" "),
    html: "div span section header footer main nav button input textarea select option form label script style link meta title body head html class id href src type value placeholder".split(" "),
    sh: "if then else elif fi for while do done case esac function echo export cd ls cat grep sed awk git pkg pip python python3 node npm mkdir rm cp mv chmod curl source exit return".split(" "),
    json: ["true", "false", "null"],
    gen: [],
  };
  const EXT = {
    js: "js", mjs: "js", cjs: "js", jsx: "js", ts: "js", tsx: "js",
    py: "py", css: "css", scss: "css", html: "html", htm: "html", xml: "html", svg: "html", vue: "html",
    sh: "sh", bash: "sh", zsh: "sh", json: "json", webmanifest: "json",
  };
  const COMMENT = { js: "//", py: "#", sh: "#", gen: "" };
  let lang = "gen", dirtyWords = true, words = [];

  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const span = (c, s) => '<span class="t-' + c + '">' + esc(s) + "</span>";

  function rules(l) {
    const kw = new RegExp("^(?:" + KW[l].join("|") + ")$");
    const R = [];
    if (l === "html") {
      R.push(["comment", /<!--[\s\S]*?(?:-->|$)/y], ["tag", /<\/?[A-Za-z][\w:-]*|\/?>/y],
        ["string", /"[^"\n]*"|'[^'\n]*'/y], ["attr", /[A-Za-z_:][\w:.-]*(?==)/y]);
      return { R, kw };
    }
    if (l === "css") R.push(["comment", /\/\*[\s\S]*?(?:\*\/|$)/y]);
    else if (l === "py") R.push(["comment", /#[^\n]*/y], ["string", /"""[\s\S]*?(?:"""|$)|'''[\s\S]*?(?:'''|$)/y]);
    else if (l === "sh") R.push(["comment", /#[^\n]*/y]);
    else if (l === "js") R.push(["comment", /\/\/[^\n]*|\/\*[\s\S]*?(?:\*\/|$)/y]);
    R.push(["string", /"(?:\\.|[^"\\\n])*"?|'(?:\\.|[^'\\\n])*'?|`(?:\\[\s\S]|[^`\\])*`?/y]);
    if (l === "css") R.push(["attr", /[-\w]+(?=\s*:)/y], ["num", /#[0-9a-fA-F]{3,8}\b|\d+(?:\.\d+)?(?:px|em|rem|%|vh|vw|s|ms)?/y]);
    else R.push(["num", /\b(?:0x[0-9a-fA-F]+|\d+(?:\.\d+)?)\b/y]);
    if (l === "py" || l === "js") R.push(["fn", /[A-Za-z_$][\w$]*(?=\()/y]);
    R.push(["word", /[A-Za-z_$][\w$-]*/y]);
    return { R, kw };
  }
  const cache = {};

  function highlight(src, l) {
    if (l === "gen" || src.length > MAX_HL) return esc(src);
    const { R, kw } = cache[l] || (cache[l] = rules(l));
    let out = "", i = 0, plain = "";
    const flush = () => { if (plain) { out += esc(plain); plain = ""; } };
    while (i < src.length) {
      let hit = null;
      for (const [cls, re] of R) {
        re.lastIndex = i;
        const m = re.exec(src);
        if (m && m[0].length) { hit = [cls, m[0]]; break; }
      }
      if (!hit) { plain += src[i++]; continue; }
      let [cls, text] = hit;
      if (cls === "word") {
        if (kw.test(text)) cls = "kw"; else { plain += text; i += text.length; continue; }
      }
      flush(); out += span(cls, text); i += text.length;
    }
    flush();
    return out;
  }

  function render() {
    const v = ta.value;
    hi.innerHTML = highlight(v, lang) + "\n";
    const n = v.split("\n").length;
    let g = "";
    for (let i = 1; i <= n; i++) g += i + "\n";
    gut.textContent = g;
    gut.style.minWidth = (String(n).length + 1) + "ch";
    dirtyWords = true;
    sync();
  }
  function sync() {
    hi.scrollTop = ta.scrollTop; hi.scrollLeft = ta.scrollLeft;
    gut.scrollTop = ta.scrollTop;
  }

  // ----- completion -----
  function collect() {
    if (!dirtyWords) return words;
    const set = new Set(KW[lang] || []);
    const m = ta.value.match(/[A-Za-z_$][\w$]{2,}/g) || [];
    for (const w of m) { set.add(w); if (set.size > 4000) break; }
    words = Array.from(set);
    dirtyWords = false;
    return words;
  }
  function prefix() {
    const p = ta.selectionStart;
    if (p !== ta.selectionEnd) return null;
    const m = /[A-Za-z_$][\w$]*$/.exec(ta.value.slice(Math.max(0, p - 40), p));
    return m ? m[0] : null;
  }
  function suggest() {
    const pre = prefix();
    sug.textContent = "";
    if (!pre || pre.length < 2) { sug.hidden = true; return; }
    const lower = pre.toLowerCase();
    const list = collect().filter((w) => w !== pre && w.toLowerCase().startsWith(lower))
      .sort((a, b) => a.length - b.length || (a < b ? -1 : 1)).slice(0, 8);
    if (!list.length) { sug.hidden = true; return; }
    for (const w of list) {
      const b = document.createElement("button");
      b.type = "button"; b.textContent = w;
      b.onmousedown = (e) => e.preventDefault(); // keep keyboard open
      b.onclick = () => accept(w, pre);
      sug.appendChild(b);
    }
    sug.hidden = false;
  }
  function accept(word, pre) {
    const p = ta.selectionStart;
    ta.setRangeText(word, p - pre.length, p, "end");
    ta.dispatchEvent(new Event("input", { bubbles: true }));
    ta.focus();
  }

  // ----- keys: Tab indent, Enter keeps indent, comment toggle -----
  ta.addEventListener("keydown", (e) => {
    const s = ta.selectionStart, t = ta.selectionEnd, v = ta.value;
    if (e.key === "Tab") {
      e.preventDefault();
      if (s === t) { ta.setRangeText("  ", s, t, "end"); }
      else {
        const a = v.lastIndexOf("\n", s - 1) + 1;
        const block = v.slice(a, t);
        const out = e.shiftKey ? block.replace(/^( {1,2}|\t)/gm, "") : block.replace(/^/gm, "  ");
        ta.setRangeText(out, a, t, "select");
      }
      ta.dispatchEvent(new Event("input", { bubbles: true }));
    } else if (e.key === "Enter" && !e.ctrlKey && !e.metaKey && s === t) {
      const a = v.lastIndexOf("\n", s - 1) + 1;
      let ind = /^[ \t]*/.exec(v.slice(a, s))[0];
      if (/[{(\[:]\s*$/.test(v.slice(a, s)) && lang !== "gen") ind += "  ";
      e.preventDefault();
      ta.setRangeText("\n" + ind, s, t, "end");
      ta.dispatchEvent(new Event("input", { bubbles: true }));
    } else if (e.key === "/" && (e.ctrlKey || e.metaKey) && COMMENT[lang]) {
      e.preventDefault();
      const a = v.lastIndexOf("\n", s - 1) + 1;
      const c = COMMENT[lang] + " ";
      const lines = v.slice(a, t).split("\n");
      const all = lines.every((l) => !l.trim() || l.trimStart().startsWith(COMMENT[lang]));
      const out = lines.map((l) => !l.trim() ? l : all
        ? l.replace(COMMENT[lang] + " ", "").replace(COMMENT[lang], "")
        : l.replace(/^(\s*)/, "$1" + c)).join("\n");
      ta.setRangeText(out, a, t, "select");
      ta.dispatchEvent(new Event("input", { bubbles: true }));
    }
  });

  ta.addEventListener("input", () => { render(); suggest(); });
  ta.addEventListener("scroll", sync);
  ta.addEventListener("click", suggest);
  ta.addEventListener("blur", () => setTimeout(() => { sug.hidden = true; }, 150));

  window.EditorKit = {
    setFile(path) {
      const ext = (/\.([A-Za-z0-9]+)$/.exec(path || "") || [])[1];
      lang = EXT[(ext || "").toLowerCase()] || "gen";
      sug.hidden = true;
      render();
    },
    refresh: render,
    langOf: () => lang,
    _highlight: highlight,
  };
})();
