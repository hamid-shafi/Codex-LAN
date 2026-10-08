/* CODEX: Offline, bounded TeX rendering into native MathML; quoted source and code remain unchanged. */
(() => {
  "use strict";
  const limit = 12000;
  const cache = new Map();
  const delimiters = [["\\[", "\\]", true], ["\\(", "\\)", false], ["$$", "$$", true], ["$", "$", false]];
  const pattern = String.raw`(?<!\\)\\\[[\s\S]{1,12000}?\\\]|(?<!\\)\\\([\s\S]{1,12000}?\\\)|(?<![\\$])\$\$[\s\S]{1,12000}?\$\$(?!\$)|(?<![\w\\$])\$(?![\s$])[^$\n]{1,12000}?(?<![\s\\])\$(?![\w$])`;

  function parse(raw) {
    for (const [open, close, display] of delimiters) {
      if (raw.startsWith(open) && raw.endsWith(close) && raw.length > open.length + close.length) {
        const tex = raw.slice(open.length, -close.length);
        if (tex.length <= limit) return { tex, display };
      }
    }
    return null;
  }

  function render(raw) {
    const parsed = parse(raw);
    if (!parsed || !window.katex) return document.createTextNode(raw);
    if (cache.has(raw)) return cache.get(raw).cloneNode(true);
    let node;
    try {
      const template = document.createElement("template");
      template.innerHTML = window.katex.renderToString(parsed.tex, {
        output: "mathml", displayMode: parsed.display, throwOnError: true,
        trust: false, strict: "ignore", maxExpand: 1000, maxSize: 10, macros: {},
      });
      const math = template.content.querySelector("math");
      if (!math) throw new Error("No MathML result");
      for (const styled of math.querySelectorAll("[style]")) styled.removeAttribute("style");
      math.removeAttribute("style");
      math.setAttribute("dir", "ltr");
      node = document.createElement("span");
      node.className = `math-render ${parsed.display ? "math-display" : "math-inline"}`;
      node.dir = "ltr";
      node.append(math);
    } catch {
      node = document.createTextNode(raw);
    }
    if (cache.size >= 128) cache.delete(cache.keys().next().value);
    cache.set(raw, node.cloneNode(true));
    return node;
  }

  function displayBlock(lines, start) {
    const first = lines[start].trimStart();
    const open = first.startsWith("\\[") ? "\\[" : first.startsWith("$$") ? "$$" : null;
    if (!open) return null;
    const close = open === "\\[" ? "\\]" : "$$";
    let raw = first;
    for (let end = start; raw.length <= limit + 4; end += 1) {
      const at = raw.indexOf(close, open.length);
      if (at >= 0) {
        const tail = raw.slice(at + close.length);
        if (tail.trim()) return null;
        return { raw: raw.slice(0, at + close.length), next: end + 1 };
      }
      if (end + 1 >= lines.length || /^\s{0,3}(`{3,}|~{3,})/.test(lines[end + 1])) break;
      raw += "\n" + lines[end + 1];
    }
    return null;
  }

  window.CodexMath = Object.freeze({ pattern, render, displayBlock });
})();
