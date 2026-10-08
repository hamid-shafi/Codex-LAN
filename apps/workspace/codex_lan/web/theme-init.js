"use strict";

(() => {
  const themes = new Set(["light", "dark", "midnight", "chatgpt", "torte", "cosmic", "aurora", "ember", "velvet"]);
  const spaceThemes = new Set(["cosmic", "aurora", "ember", "velvet"]);
  let saved = "";
  try { saved = localStorage.getItem("codex-lan-theme") || ""; } catch {}
  if (saved === "cosmic-live") {
    saved = "cosmic";
    try { localStorage.setItem("codex-lan-theme", saved); } catch {}
  }
  const system = window.matchMedia?.("(prefers-color-scheme: dark)")?.matches ? "dark" : "light";
  const current = themes.has(saved) ? saved : system;
  document.documentElement.dataset.theme = current;
  if (spaceThemes.has(current)) document.documentElement.dataset.themeFamily = "space";
  else delete document.documentElement.dataset.themeFamily;
})();
