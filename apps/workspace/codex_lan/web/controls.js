/* CODEX: Progressive model and thinking controls keep each room's native selects authoritative. */
(() => {
  "use strict";
  if (window.CodexControls) return;
  const selector = "#model-select, #effort-select, select[data-control='model'], select[data-control='effort']";
  const controls = new WeakMap();
  const motion = matchMedia("(prefers-reduced-motion: reduce)");
  const finePointer = matchMedia("(hover: hover) and (pointer: fine)");
  const forcedColors = matchMedia("(forced-colors: active)");
  const effects = new Set();
  const replyStates = new WeakMap();
  let announceTimer = 0;
  let sequence = 0;
  let opened = null;

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  // CODEX: Interaction effects are finite, replaceable and stopped when the page is hidden or motion is reduced.
  function animate(element, frames, duration, name, delay = 0) {
    if (motion.matches || forcedColors.matches || document.hidden || !element?.isConnected || !element.animate) return null;
    for (const previous of element.getAnimations()) if (previous.id === name) previous.cancel();
    const effect = element.animate(frames, { duration, delay, easing: "cubic-bezier(.22, .8, .25, 1)" });
    effect.id = name;
    effects.add(effect);
    effect.finished.then(() => effects.delete(effect), () => effects.delete(effect));
    return effect;
  }

  function stopEffects() {
    for (const effect of effects) effect.cancel();
    effects.clear();
  }

  function visible(element) {
    if (motion.matches || forcedColors.matches || document.hidden || !element?.isConnected) return false;
    const rect = element.getBoundingClientRect();
    const clip = element.closest("#timeline-scroll")?.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0 && rect.bottom > Math.max(0, clip?.top || 0)
      && rect.top < Math.min(innerHeight, clip?.bottom ?? innerHeight) && rect.right > 0 && rect.left < innerWidth;
  }

  function announce(message) {
    let status = document.getElementById("interaction-feedback-status");
    if (!status) {
      status = node("span", "sr-only");
      status.id = "interaction-feedback-status";
      status.setAttribute("role", "status");
      status.setAttribute("aria-live", "polite");
      status.setAttribute("aria-atomic", "true");
      document.body.append(status);
    }
    clearTimeout(announceTimer);
    status.textContent = "";
    announceTimer = setTimeout(() => { if (status.isConnected) status.textContent = message; }, 20);
  }

  function copyFeedback(button) {
    announce("Copied to clipboard");
    if (!visible(button)) return;
    button.querySelector(".copy-success-effect")?.remove();
    button.classList.add("copy-effect-host");
    const shadow = getComputedStyle(button).boxShadow;
    const buttonEffect = animate(button, [
      { boxShadow: shadow, transform: "scale(1)" },
      { boxShadow: "0 0 0 2px color-mix(in srgb, var(--accent) 18%, transparent), 0 0 16px color-mix(in srgb, var(--accent) 18%, transparent)", transform: "scale(1.025)", offset: .28 },
      { boxShadow: shadow, transform: "scale(1)" },
    ], 760, "codex-copy-confirm");
    const mark = node("span", "copy-success-effect");
    mark.setAttribute("aria-hidden", "true");
    button.append(mark);
    const markEffect = animate(mark, [
      { opacity: 0, transform: "translate(-4px, 4px) scale(.35) rotate(-18deg)" },
      { opacity: 1, transform: "translate(0, -3px) scale(1.08) rotate(4deg)", offset: .35 },
      { opacity: 0, transform: "translate(5px, -10px) scale(.65) rotate(14deg)" },
    ], 720, "codex-copy-spark");
    Promise.allSettled([buttonEffect?.finished, markEffect?.finished]).then(() => {
      mark.remove();
      if (!button.querySelector(".copy-success-effect")) button.classList.remove("copy-effect-host");
    });
  }

  function attachmentFeedback(card, phase, name) {
    if (phase === "ready") announce(`${name} is ready`);
    else if (phase === "failed") announce(`Upload failed for ${name}`);
    if (!visible(card)) return;
    if (phase === "arrive") {
      animate(card, [
        { opacity: 0, transform: "translateY(7px) scale(.97)" },
        { opacity: 1, transform: "translateY(0) scale(1)" },
      ], 420, "codex-attachment-arrive");
      return;
    }
    if (phase === "failed") {
      const shadow = getComputedStyle(card).boxShadow;
      animate(card, [
        { transform: "translateX(0)", boxShadow: shadow },
        { transform: "translateX(-3px)", boxShadow: "0 0 0 1px color-mix(in srgb, var(--danger) 35%, transparent)", offset: .24 },
        { transform: "translateX(2px)", offset: .48 },
        { transform: "translateX(0)", boxShadow: shadow },
      ], 520, "codex-attachment-failed");
      return;
    }
    if (phase !== "ready") return;
    const shadow = getComputedStyle(card).boxShadow;
    const cardEffect = animate(card, [
      { boxShadow: shadow },
      { boxShadow: "0 0 0 1px color-mix(in srgb, var(--accent) 42%, transparent), 0 0 18px color-mix(in srgb, var(--accent) 14%, transparent)", offset: .3 },
      { boxShadow: shadow },
    ], 900, "codex-attachment-ready");
    const mark = node("span", "attachment-ready-effect", "✓");
    mark.setAttribute("aria-hidden", "true");
    card.append(mark);
    const markEffect = animate(mark, [
      { opacity: 0, transform: "scale(.35) rotate(-22deg)" },
      { opacity: 1, transform: "scale(1.08) rotate(3deg)", offset: .35 },
      { opacity: 0, transform: "translateY(-5px) scale(.82) rotate(8deg)" },
    ], 1100, "codex-attachment-check");
    Promise.allSettled([cardEffect?.finished, markEffect?.finished]).then(() => mark.remove());
  }

  function unreadFeedback(dot) {
    if (dot.hidden || !dot.closest(".room-row.own") || !visible(dot)) return;
    const shadow = getComputedStyle(dot).boxShadow;
    animate(dot, [
      { opacity: .35, transform: "scale(.45)", boxShadow: shadow },
      { opacity: 1, transform: "scale(1.45)", boxShadow: "0 0 0 6px color-mix(in srgb, var(--accent) 12%, transparent), 0 0 14px color-mix(in srgb, var(--accent) 42%, transparent)", offset: .3 },
      { opacity: 1, transform: "scale(1)", boxShadow: shadow },
    ], 920, "codex-unread-arrive");
  }

  function controlFeedback(select) {
    if (select.disabled) return;
    const wrapper = select.closest(".smart-control");
    const surface = wrapper?.querySelector(".model-picker-trigger") || wrapper;
    if (!visible(surface)) return;
    const intense = ["max", "ultra", "xhigh"].includes(select.value);
    const base = getComputedStyle(surface);
    animate(surface, [
      { boxShadow: base.boxShadow, borderColor: base.borderTopColor },
      { boxShadow: `0 0 0 2px color-mix(in srgb, var(--accent) 12%, transparent), 0 0 ${intense ? 22 : 14}px color-mix(in srgb, var(--accent) 22%, transparent)`, borderColor: "var(--accent)", offset: .18 },
      { boxShadow: base.boxShadow, borderColor: base.borderTopColor },
    ], intense ? 1100 : 800, "codex-control-glow");
    const mark = wrapper.querySelector(".model-picker-mark");
    if (mark) animate(mark, [
      { transform: "rotate(-55deg) scale(.7)" },
      { transform: "rotate(15deg) scale(1.3)", offset: .4 },
      { transform: "rotate(0) scale(1)" },
    ], 650, "codex-model-spark");
    else wrapper.querySelectorAll(".thinking-mark.is-filled").forEach((bar, index) => animate(bar, [
      { transform: "translateY(0) scaleY(1)" },
      { transform: "translateY(-2px) scaleY(1.65)", offset: .35 },
      { transform: "translateY(0) scaleY(1)" },
    ], 500, "codex-thinking-wave", index * 40));
  }

  function enterRoom() {
    if (motion.matches || forcedColors.matches || document.hidden) return;
    const selection = window.getSelection();
    if (selection && !selection.isCollapsed) return;
    for (const effect of effects) if (effect.id === "codex-room-enter") effect.cancel();
    const title = document.getElementById("room-title");
    if (visible(title)) animate(title, [{ opacity: .3, transform: "translateY(5px)" }, { opacity: 1, transform: "translateY(0)" }], 380, "codex-room-enter");
    const messages = [...document.querySelectorAll("#timeline > .message:not(.message-send-arrive):not(.message-send-staging)")].filter(visible).slice(-6);
    messages.forEach((message, index) => animate(message, [
      { opacity: .35, transform: "translateY(10px)" }, { opacity: 1, transform: "translateY(0)" },
    ], 440, "codex-room-enter", index * 45));
    const symbol = document.querySelector("#empty-state .empty-symbol");
    if (visible(symbol)) animate(symbol, [
      { opacity: .4, transform: "rotate(-24deg) scale(.8)" },
      { opacity: 1, transform: "rotate(5deg) scale(1.06)", offset: .55 },
      { opacity: 1, transform: "rotate(0) scale(1)" },
    ], 850, "codex-room-enter");
  }

  function revealReply(article, presentation) {
    if (!presentation.assistant) return;
    const next = { active: ["queued", "running"].includes(presentation.status), hasText: !!presentation.text.trim() };
    const previous = replyStates.get(article);
    replyStates.set(article, next);
    const arriving = !previous?.hasText && next.hasText;
    const completed = presentation.status === "completed" && !presentation.failed && next.hasText;
    if (!previous?.active || (!arriving && !completed) || !visible(article)) return;
    const selection = window.getSelection();
    if (selection && !selection.isCollapsed && (article.contains(selection.anchorNode) || article.contains(selection.focusNode))) return;
    const content = article.querySelector(".message-content");
    if (arriving) animate(content, [{ opacity: .25, transform: "translateY(5px)" }, { opacity: 1, transform: "translateY(0)" }], 420, "codex-reply-enter");
    if (!completed) return;
    const shadow = getComputedStyle(article).boxShadow;
    animate(article, [{ boxShadow: shadow }, { boxShadow: "0 0 0 1px color-mix(in srgb, var(--accent) 45%, transparent), 0 0 22px color-mix(in srgb, var(--accent) 10%, transparent)", offset: .2 }, { boxShadow: shadow }], 1500, "codex-reply-ready");
    const avatar = article.querySelector(".message-avatar");
    if (!visible(avatar)) return;
    const spark = node("span", "reply-ready-effect");
    spark.setAttribute("aria-hidden", "true");
    const orbit = node("span", "reply-ready-orbit");
    const star = node("span", "reply-ready-star", "✦");
    spark.append(orbit, star); avatar.append(spark);
    const ringEffect = animate(orbit, [{ opacity: 0, transform: "scale(.8)" }, { opacity: .8, transform: "scale(1.2)", offset: .2 }, { opacity: 0, transform: "scale(1.8)" }], 1400, "codex-reply-orbit");
    const starEffect = animate(star, [{ opacity: 0, transform: "translate(-5px, 4px) rotate(-25deg) scale(.4)" }, { opacity: 1, transform: "translate(0, 0) rotate(10deg) scale(1.1)", offset: .3 }, { opacity: 0, transform: "translate(6px, -8px) rotate(40deg) scale(.4)" }], 1400, "codex-reply-spark");
    Promise.allSettled([ringEffect?.finished, starEffect?.finished]).then(() => spark.remove());
  }

  function enabledOptions(select) {
    return [...select.options].filter(option => !option.disabled && !option.hidden
      && !(option.parentElement instanceof HTMLOptGroupElement && option.parentElement.disabled));
  }

  function commit(select, value) {
    if (select.disabled || select.value === value) return;
    select.value = value;
    select.dispatchEvent(new Event("input", { bubbles: true }));
    select.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function enableWheelSelection(select, target, sync, reverse = false) {
    let lastStepAt = 0;
    target.addEventListener("wheel", event => {
      if (select.disabled || event.ctrlKey || event.metaKey || opened?.select === select) return;
      const delta = Math.abs(event.deltaY) >= Math.abs(event.deltaX) ? event.deltaY : event.deltaX;
      if (Math.abs(delta) < 1) return;
      event.preventDefault();
      const now = performance.now();
      if (now - lastStepAt < 90) return;
      lastStepAt = now;
      const options = enabledOptions(select);
      if (options.length < 2) return;
      const current = Math.max(0, options.findIndex(option => option.value === select.value));
      const wheelDirection = delta > 0 ? 1 : -1;
      const next = Math.max(0, Math.min(options.length - 1, current + (reverse ? -wheelDirection : wheelDirection)));
      if (next === current) return;
      commit(select, options[next].value);
      sync();
    }, { passive: false });
  }

  function track(select, sync) {
    let queued = false;
    const schedule = () => {
      if (queued) return;
      queued = true;
      queueMicrotask(() => { queued = false; sync(); });
    };
    const observer = new MutationObserver(schedule);
    observer.observe(select, { childList: true, subtree: true, attributes: true, characterData: true });
    for (const property of ["value", "selectedIndex"]) {
      const descriptor = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, property);
      if (!descriptor || Object.getOwnPropertyDescriptor(select, property)) continue;
      Object.defineProperty(select, property, {
        configurable: true,
        get() { return descriptor.get.call(this); },
        set(value) { descriptor.set.call(this, value); schedule(); },
      });
    }
    select.addEventListener("input", schedule);
    select.addEventListener("change", () => { schedule(); queueMicrotask(() => controlFeedback(select)); });
    select.addEventListener("focus", () => controls.get(select)?.focus());
    return schedule;
  }

  function wrap(select, kind) {
    const wrapper = node("div", `smart-control smart-${kind}`);
    select.before(wrapper);
    wrapper.append(select);
    select.classList.add("smart-native-select");
    select.tabIndex = -1;
    select.setAttribute("aria-hidden", "true");
    wrapper.dataset.controlFor = select.id || `room-control-${++sequence}`;
    return wrapper;
  }

  function closePicker(restoreFocus = false) {
    if (!opened) return;
    const current = opened;
    opened = null;
    current.button.setAttribute("aria-expanded", "false");
    if (typeof current.menu.hidePopover === "function") {
      try { current.menu.hidePopover(); } catch {}
    }
    current.menu.remove();
    if (restoreFocus && current.button.isConnected && !current.button.disabled) current.button.focus({ preventScroll: true });
  }

  function positionPicker() {
    if (!opened) return;
    const { menu, button } = opened;
    const anchor = button.getBoundingClientRect();
    if (!button.isConnected || !anchor.width || !anchor.height) { closePicker(); return; }
    const viewport = window.visualViewport;
    const viewportTop = viewport?.offsetTop || 0;
    const viewportLeft = viewport?.offsetLeft || 0;
    const viewportHeight = viewport?.height || innerHeight;
    const viewportWidth = viewport?.width || innerWidth;
    const width = Math.min(326, viewportWidth - 24);
    const above = anchor.top - viewportTop - 16;
    const below = viewportTop + viewportHeight - anchor.bottom - 16;
    const useAbove = above >= Math.min(300, below);
    const height = Math.max(100, Math.min(388, useAbove ? above : below));
    menu.style.width = `${width}px`;
    menu.style.maxHeight = `${height}px`;
    menu.style.left = `${Math.max(viewportLeft + 12, Math.min(anchor.left, viewportLeft + viewportWidth - width - 12))}px`;
    menu.style.top = `${useAbove ? Math.max(viewportTop + 12, anchor.top - menu.offsetHeight - 9) : anchor.bottom + 9}px`;
    menu.dataset.placement = useAbove ? "above" : "below";
  }

  function enhanceModel(select) {
    const wrapper = wrap(select, "model");
    const button = node("button", "model-picker-trigger");
    button.type = "button";
    button.setAttribute("aria-haspopup", "listbox");
    button.setAttribute("aria-expanded", "false");
    const mark = node("span", "model-picker-mark", "✦");
    mark.setAttribute("aria-hidden", "true");
    const value = node("span", "model-picker-value");
    const chevron = node("span", "model-picker-chevron", "⌄");
    chevron.setAttribute("aria-hidden", "true");
    button.append(mark, value, chevron);
    wrapper.append(button);

    function sync() {
      const current = select.selectedOptions[0];
      value.textContent = current?.textContent || "Choose model";
      button.disabled = select.disabled || !enabledOptions(select).length;
      button.setAttribute("aria-label", `${select.getAttribute("aria-label") || "Model"}: ${value.textContent}`);
      button.title = `${value.textContent}${select.title ? ` · ${select.title}` : ""} · Scroll to change`;
      if (opened?.select === select) {
        if (button.disabled) closePicker();
        else opened.render();
      }
    }

    function open() {
      if (button.disabled) return;
      if (opened?.select === select) { closePicker(true); return; }
      closePicker();
      const menu = node("div", "model-picker-menu");
      menu.id = `model-picker-menu-${++sequence}`;
      menu.setAttribute("popover", "manual");
      const heading = node("div", "model-picker-heading");
      heading.append(node("strong", "", "Choose a model"), node("span", "", "For this room"));
      const search = node("input", "model-picker-search");
      search.type = "search";
      search.placeholder = "Find a model…";
      search.setAttribute("aria-label", "Find a model");
      search.autocomplete = "off";
      search.spellcheck = false;
      const list = node("div", "model-picker-list");
      list.setAttribute("role", "listbox");
      list.setAttribute("aria-label", select.getAttribute("aria-label") || "Model");
      const hint = node("p", "model-picker-hint", "↑ ↓ to move · Enter to choose");
      menu.append(heading, search, list, hint);
      button.setAttribute("aria-controls", menu.id);
      button.setAttribute("aria-expanded", "true");
      let signature = "";

      function render() {
        const options = [...select.options].filter(option => !option.hidden);
        const query = search.value.trim().toLocaleLowerCase();
        const nextSignature = JSON.stringify([select.value, query, options.map(option => [option.value, option.textContent, option.title, option.disabled, option.parentElement?.label])]);
        if (signature === nextSignature) return;
        signature = nextSignature;
        const focusedValue = document.activeElement?.dataset.modelValue;
        list.replaceChildren();
        search.hidden = options.length < 7;
        let group = "";
        for (const option of options) {
          if (query && !`${option.textContent} ${option.value}`.toLocaleLowerCase().includes(query)) continue;
          const label = option.parentElement?.label || "";
          if (label && label !== group) {
            const heading = node("div", "model-picker-group", label);
            heading.setAttribute("role", "presentation");
            list.append(heading);
          }
          group = label;
          const item = node("button", "model-picker-option");
          item.type = "button";
          item.setAttribute("role", "option");
          item.setAttribute("aria-selected", String(option.value === select.value));
          item.disabled = option.disabled || !!option.parentElement?.disabled;
          item.dataset.modelValue = option.value;
          const copy = node("span", "model-picker-option-copy");
          copy.append(node("strong", "", option.textContent));
          const detail = option.title || (option.value !== option.textContent ? option.value : "");
          if (detail) copy.append(node("small", "", detail));
          const check = node("span", "model-picker-check", option.value === select.value ? "✓" : "");
          check.setAttribute("aria-hidden", "true");
          item.append(copy, check);
          item.addEventListener("click", () => { commit(select, option.value); closePicker(true); });
          list.append(item);
        }
        if (!list.children.length) list.append(node("p", "model-picker-empty", "No matching models"));
        if (focusedValue) [...list.querySelectorAll("button")].find(item => item.dataset.modelValue === focusedValue)?.focus({ preventScroll: true });
      }

      opened = { menu, button, select, render };
      render();
      document.body.append(menu);
      if (typeof menu.showPopover === "function") menu.showPopover();
      else menu.classList.add("model-picker-fallback");
      positionPicker();
      const selected = list.querySelector('[aria-selected="true"]:not(:disabled)') || list.querySelector("button:not(:disabled)");
      selected?.focus({ preventScroll: true });
      selected?.scrollIntoView({ block: "nearest" });
      search.addEventListener("input", () => { render(); positionPicker(); });
      menu.addEventListener("keydown", event => {
        if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); closePicker(true); return; }
        if (event.key === "Tab") { closePicker(true); return; }
        if (event.altKey || event.ctrlKey || event.metaKey) return;
        const items = [...list.querySelectorAll("button:not(:disabled)")];
        const index = items.indexOf(document.activeElement);
        if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key) && (event.target !== search || event.key.startsWith("Arrow"))) {
          event.preventDefault();
          const target = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1 : event.key === "ArrowDown" ? (index + 1) % items.length : (index - 1 + items.length) % items.length;
          items[target]?.focus({ preventScroll: true });
          items[target]?.scrollIntoView({ block: "nearest" });
        } else if (event.key.length === 1 && event.target !== search && !search.hidden) {
          event.preventDefault();
          search.value = event.key;
          search.focus();
          render();
        }
      });
    }

    button.addEventListener("click", open);
    button.addEventListener("keydown", event => {
      if (!event.altKey && !event.ctrlKey && !event.metaKey && ["ArrowDown", "ArrowUp"].includes(event.key)) {
        event.preventDefault();
        open();
      }
    });
    enableWheelSelection(select, button, sync);
    controls.set(select, { sync, focus: () => button.focus({ preventScroll: true }) });
    track(select, sync);
    sync();
  }

  function enhanceEffort(select) {
    const wrapper = wrap(select, "effort");
    const minus = node("button", "thinking-step", "−");
    const plus = node("button", "thinking-step", "+");
    minus.type = plus.type = "button";
    minus.setAttribute("aria-label", "Less thinking");
    plus.setAttribute("aria-label", "More thinking");
    const center = node("div", "thinking-center");
    const label = node("span", "thinking-value");
    const marks = node("span", "thinking-marks");
    marks.setAttribute("aria-hidden", "true");
    const slider = node("input", "thinking-slider");
    slider.type = "range";
    slider.min = "0";
    slider.step = "1";
    slider.setAttribute("aria-label", select.getAttribute("aria-label") || "Reasoning effort");
    center.append(label, marks, slider);
    wrapper.append(minus, center, plus);
    let options = [];
    let dragging = false;
    let activePointer = null;

    function preview(index) {
      const option = options[index];
      label.textContent = option?.textContent || "Default";
      slider.setAttribute("aria-valuetext", label.textContent);
      slider.title = `${option?.title || `${label.textContent} thinking${select.title ? ` · ${select.title}` : ""}`} · Scroll to change`;
      [...marks.children].forEach((mark, position) => mark.classList.toggle("is-filled", position <= index));
      minus.disabled = select.disabled || options.length <= 1 || index <= 0;
      plus.disabled = select.disabled || options.length <= 1 || index >= options.length - 1;
    }

    function sync() {
      const nextOptions = enabledOptions(select);
      const optionsChanged = JSON.stringify(options.map(option => [option.value, option.textContent])) !== JSON.stringify(nextOptions.map(option => [option.value, option.textContent]));
      options = nextOptions;
      if (optionsChanged || select.disabled) { dragging = false; activePointer = null; }
      slider.max = String(Math.max(1, options.length - 1));
      slider.disabled = select.disabled || options.length <= 1;
      wrapper.classList.toggle("is-disabled", slider.disabled);
      if (marks.children.length !== options.length) marks.replaceChildren(...options.map(() => node("i", "thinking-mark")));
      if (!dragging) slider.value = String(Math.max(0, options.findIndex(option => option.value === select.value)));
      preview(Number(slider.value));
    }

    function step(direction) {
      const index = options.findIndex(option => option.value === select.value);
      const option = options[Math.max(0, Math.min(options.length - 1, index + direction))];
      if (option) commit(select, option.value);
      sync();
    }

    minus.addEventListener("click", () => step(-1));
    plus.addEventListener("click", () => step(1));
    slider.addEventListener("pointerdown", event => {
      dragging = true;
      activePointer = event.pointerId;
      slider.setPointerCapture(event.pointerId);
    });
    slider.addEventListener("input", () => preview(Number(slider.value)));
    slider.addEventListener("change", () => {
      dragging = false;
      activePointer = null;
      const option = options[Number(slider.value)];
      if (option) commit(select, option.value);
      sync();
    });
    slider.addEventListener("pointercancel", () => { dragging = false; activePointer = null; sync(); });
    slider.addEventListener("blur", () => { dragging = false; activePointer = null; sync(); });
    slider.addEventListener("pointerup", event => {
      if (event.pointerId !== activePointer) return;
      activePointer = null;
      requestAnimationFrame(() => {
        if (!dragging) return;
        dragging = false;
        const option = options[Number(slider.value)];
        if (option) commit(select, option.value);
        sync();
      });
    });
    enableWheelSelection(select, wrapper, sync, true);
    controls.set(select, { sync, focus: () => slider.focus({ preventScroll: true }) });
    track(select, sync);
    sync();
  }

  function enhance(root = document) {
    const candidates = root.matches?.(selector) ? [root] : [...root.querySelectorAll?.(selector) || []];
    for (const select of candidates) {
      if (controls.has(select)) continue;
      if (select.id === "effort-select" || select.dataset.control === "effort") enhanceEffort(select);
      else enhanceModel(select);
    }
    enhanceMist(root);
  }

  function enhanceMist(root) {
    const mistSelector = ".sidebar-brand, #composer, [data-pointer-mist]";
    const surfaces = root.matches?.(mistSelector) ? [root] : [...root.querySelectorAll?.(mistSelector) || []];
    for (const surface of surfaces) {
      if (surface.classList.contains("pointer-mist-surface")) continue;
      surface.classList.add("pointer-mist-surface");
      const glow = node("span", "pointer-mist");
      glow.setAttribute("aria-hidden", "true");
      surface.prepend(glow);
      let frame = 0;
      let idleTimer = 0;
      let clientX = 0;
      let clientY = 0;
      let trail = null;
      const clear = () => {
        cancelAnimationFrame(frame); clearTimeout(idleTimer); frame = 0; idleTimer = 0;
        trail = null; surface.classList.remove("pointer-mist-active");
      };
      surface.addEventListener("pointermove", event => {
        if (motion.matches || forcedColors.matches || document.hidden || !finePointer.matches || event.pointerType === "touch" || event.buttons) { clear(); return; }
        clientX = event.clientX;
        clientY = event.clientY;
        if (frame) return;
        frame = requestAnimationFrame(() => {
          frame = 0;
          if (motion.matches || forcedColors.matches || document.hidden || !finePointer.matches || !surface.isConnected) { clear(); return; }
          // Read geometry once for the latest event, even on high polling-rate pointers.
          const rect = surface.getBoundingClientRect();
          const x = clientX - rect.left;
          const y = clientY - rect.top;
          glow.style.setProperty("--pointer-x", `${x}px`);
          glow.style.setProperty("--pointer-y", `${y}px`);
          trail = trail ? { x: trail.x + (x - trail.x) * .35, y: trail.y + (y - trail.y) * .35 } : { x, y };
          glow.style.setProperty("--trail-x", `${trail.x}px`);
          glow.style.setProperty("--trail-y", `${trail.y}px`);
          surface.classList.add("pointer-mist-active");
          clearTimeout(idleTimer); idleTimer = setTimeout(clear, 120);
        });
      }, { passive: true });
      surface.addEventListener("pointerleave", clear);
      surface.addEventListener("pointerdown", clear);
      surface.addEventListener("pointercancel", clear);
      window.addEventListener("blur", clear);
    }
  }

  document.addEventListener("pointerdown", event => {
    if (opened && !opened.menu.contains(event.target) && !opened.button.contains(event.target)) closePicker();
  }, true);
  document.addEventListener("keydown", event => {
    if (opened && event.key === "Escape") { event.preventDefault(); closePicker(true); }
  });
  window.addEventListener("resize", positionPicker, { passive: true });
  window.visualViewport?.addEventListener("resize", positionPicker, { passive: true });
  document.addEventListener("scroll", event => { if (opened && !opened.menu.contains(event.target)) positionPicker(); }, { capture: true, passive: true });
  document.addEventListener("visibilitychange", () => { if (document.hidden) { closePicker(); stopEffects(); } });
  motion.addEventListener("change", () => { if (motion.matches) stopEffects(); });
  forcedColors.addEventListener("change", () => { if (forcedColors.matches) stopEffects(); });
  window.CodexControls = {
    enhance,
    sync: select => controls.get(select)?.sync(),
    close: closePicker,
    enterRoom,
    revealReply,
    copyFeedback,
    attachmentFeedback,
    unreadFeedback,
  };
  enhance();
  new MutationObserver(records => {
    for (const record of records) for (const added of record.addedNodes) if (added.nodeType === Node.ELEMENT_NODE) enhance(added);
    if (opened && !opened.button.isConnected) closePicker();
  }).observe(document.body, { childList: true, subtree: true });
})();
