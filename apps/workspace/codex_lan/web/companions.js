// CODEX: Selection companions are server-owned rooms, not disposable browser popups.
(() => {
  const path = (parent) => `/api/rooms/${encodeURIComponent(parent)}/companions`;
  const button = (label, className = "button") => {
    const node = element("button", className, label); node.type = "button"; return node;
  };
  const draftKey = (device, id) => `codex-companion-draft:${device}:${id}`;
  const saved = (key) => { try { return localStorage.getItem(key); } catch { return null; } };
  const save = (key, value) => { try { localStorage.setItem(key, value); } catch {} };
  const forget = (key) => { try { localStorage.removeItem(key); } catch {} };

  if (embeddedRoomId) {
    const params = new URL(location.href).searchParams;
    const parentId = params.get("parent_room"), companionId = params.get("companion");
    let loaded = false, loading = false, editing = false, pending = false, writing = false, restoring = false, revision = 0, timer;
    const endpoint = `${path(parentId)}/${encodeURIComponent(companionId)}`;
    const localKey = () => draftKey(state.me?.id, companionId);
    async function flushDraft() {
      if (!loaded || !pending || writing || !state.me) return;
      writing = true;
      const draft = roomState(embeddedRoomId).draft;
      try {
        const result = await api(endpoint, { method: "PATCH", body: JSON.stringify({ draft, draft_revision: revision }) });
        revision = result.companion.draft_revision ?? revision + 1;
        if (roomState(embeddedRoomId).draft === draft) { pending = false; forget(localKey()); }
      } catch (error) {
        if (error.status === 404) { pending = false; forget(localKey()); }
        else if (error.status === 409) {
          const result = await api(path(parentId)).catch(() => null);
          const current = result?.companions.find((item) => item.id === companionId);
          if (current) revision = current.draft_revision ?? revision;
        }
      } finally { writing = false; }
    }
    async function initializeDraft() {
      if (loaded || loading || !parentId || !companionId || state.selected !== embeddedRoomId || !state.me) return;
      loading = true;
      try {
        const result = await api(path(parentId));
        const companion = result.companions.find((item) => item.id === companionId);
        if (!companion) return;
        const local = saved(localKey());
        revision = companion.draft_revision ?? 0;
        if (!editing) {
          restoring = true;
          setComposerDraft(roomState(embeddedRoomId), local ?? companion.draft ?? "");
          updateComposer();
          restoring = false;
        }
        loaded = true;
        pending = editing || local !== null;
        flushDraft();
      } catch {} finally { loading = false; }
    }
    window.addEventListener("codex-companion-draft", () => {
      if (restoring || state.selected !== embeddedRoomId) return;
      editing = true; pending = true;
      save(localKey(), roomState(embeddedRoomId).draft);
      clearTimeout(timer); timer = setTimeout(flushDraft, 350);
    });
    window.addEventListener("storage", (event) => {
      if (event.key === "codex-lan-theme" && themes.includes(event.newValue)) setTheme(event.newValue, false);
    });
    window.addEventListener("message", (event) => {
      if (event.source !== window.parent || event.origin !== location.origin) return;
      if (event.data?.type === "codex-companion-room-settings") {
        openRoomDialog("edit");
        return;
      }
      if (event.data?.type !== "codex-companion-visible") return;
      const paused = event.data.visible !== true;
      if (paused === embeddedPaused) return;
      embeddedPaused = paused;
      if (paused) for (const key of ["eventTimer", "roomTimer", "sessionTimer"]) clearTimeout(state[key]);
      else { checkSession(); refreshRooms().catch(() => {}); scheduleRooms(); scheduleEvents(0); }
    });
    window.addEventListener("pagehide", () => {
      if (pending && state.me) save(localKey(), roomState(embeddedRoomId).draft);
    });
    setInterval(() => { initializeDraft(); flushDraft(); }, 2000);
    window.addEventListener("codex-room-render", initializeDraft);
    return;
  }

  const workbench = element("div", "chat-workbench");
  $("chat-view").before(workbench); workbench.append($("chat-view"));
  const dock = element("aside", "companion-dock"); dock.hidden = true;
  dock.setAttribute("aria-label", "Side threads"); dock.id = "companion-side-dock";
  const divider = element("div", "companion-divider"); divider.hidden = true; divider.tabIndex = 0;
  divider.setAttribute("role", "separator"); divider.setAttribute("aria-label", "Resize main room and side thread");
  divider.setAttribute("aria-orientation", "vertical"); divider.setAttribute("aria-controls", "chat-view companion-side-dock");
  workbench.append(divider, dock);
  const dockBar = element("div", "companion-dock-bar");
  const tabs = element("div", "companion-tabs"); tabs.setAttribute("role", "tablist");
  const sideMenu = document.createElement("details"); sideMenu.className = "companion-menu";
  const sideMenuTrigger = document.createElement("summary"); sideMenuTrigger.className = "icon-button companion-menu-trigger";
  sideMenuTrigger.textContent = "•••"; sideMenuTrigger.setAttribute("aria-label", "Side thread options"); sideMenuTrigger.title = "Side thread options";
  const sideMenuCard = element("div", "companion-menu-card");
  const sideMenuSource = element("p", "companion-menu-source");
  const sideSettings = button("Room settings", "button subtle companion-menu-action");
  const sideDelete = button("Delete side thread", "button danger companion-menu-action");
  sideMenuCard.append(sideMenuSource, sideSettings, sideDelete); sideMenu.append(sideMenuTrigger, sideMenuCard);
  dockBar.append(tabs, sideMenu);
  const bodies = element("div", "companion-bodies"); dock.append(dockBar, bodies);
  const detailsWindow = element("section", "companion-details-window"); detailsWindow.hidden = true;
  detailsWindow.setAttribute("role", "dialog"); detailsWindow.setAttribute("aria-modal", "false");
  detailsWindow.setAttribute("aria-label", "More details");
  const detailsHeader = element("div", "companion-details-header");
  const detailsGrip = element("div", "companion-details-grip"); detailsGrip.tabIndex = 0;
  detailsGrip.setAttribute("role", "button"); detailsGrip.setAttribute("aria-label", "Move More details window");
  detailsGrip.title = "Drag to move · Double-click to reset";
  detailsGrip.append(element("span", "companion-details-symbol", "✦"), element("strong", "", "More details"));
  const detailTabs = element("div", "companion-tabs companion-detail-tabs"); detailTabs.setAttribute("role", "tablist");
  const detailsClose = button("×", "icon-button companion-close companion-details-close");
  detailsClose.setAttribute("aria-label", "Close More details"); detailsClose.title = "Delete this explanation";
  detailsHeader.append(detailsGrip, detailTabs, detailsClose);
  const detailBodies = element("div", "companion-bodies");
  const detailsResize = element("div", "companion-details-resize"); detailsResize.tabIndex = 0;
  detailsResize.setAttribute("role", "separator"); detailsResize.setAttribute("aria-label", "Resize More details window");
  detailsWindow.append(detailsHeader, detailBodies, detailsResize); workbench.append(detailsWindow);
  const toolbar = element("div", "selection-actions"); toolbar.id = "selection-actions";
  toolbar.setAttribute("role", "toolbar"); toolbar.setAttribute("aria-label", "Selected text actions"); toolbar.hidden = true;
  const sideButton = button("Side thread", "selection-action"); sideButton.dataset.kind = "side_thread";
  const detailButton = button("More details", "selection-action"); detailButton.dataset.kind = "more_details";
  toolbar.append(sideButton, detailButton); document.body.append(toolbar);
  const notice = element("p", "companion-connection"); notice.setAttribute("role", "status"); notice.hidden = true;
  dockBar.after(notice);
  const rooms = new Map(), panels = new Map(), mutations = new Map(), createRequests = new Map();
  const touch = (parent) => mutations.set(parent, (mutations.get(parent) || 0) + 1);
  let selectedText = null, lastRoom = "", device = "", refreshing = false, lastRefresh = 0, capability = null, capabilityAt = 0;
  const activeKey = (parent, kind = "side_thread") => `codex-companion-active:${device}:${parent}:${kind}`;
  function currentCompanions() { return rooms.get(state.selected) || []; }
  function showNotice(text = "") { notice.textContent = text; notice.hidden = !text; }

  let selectionTimer = 0;
  const selectionPointers = new Set();
  function hideSelectionActions() {
    clearTimeout(selectionTimer); selectionTimer = 0;
    toolbar.hidden = true; selectedText = null;
  }
  function scheduleSelectionActions() {
    if (toolbar.contains(document.activeElement)) return;
    hideSelectionActions();
    if (selectionPointers.size || document.hidden) return;
    const parent = state.selected;
    selectionTimer = setTimeout(() => {
      selectionTimer = 0;
      if (!selectionPointers.size && !document.hidden && state.selected === parent) captureSelection();
    }, 160);
  }
  function captureSelection() {
    if (document.activeElement && toolbar.contains(document.activeElement)) return;
    const selection = window.getSelection();
    const text = selection?.toString();
    const anchor = selection?.anchorNode?.nodeType === Node.ELEMENT_NODE ? selection.anchorNode : selection?.anchorNode?.parentElement;
    const focus = selection?.focusNode?.nodeType === Node.ELEMENT_NODE ? selection.focusNode : selection?.focusNode?.parentElement;
    const content = anchor?.closest("#timeline .message-content");
    const article = content?.closest(".message");
    if (!text?.trim() || text.length > 50000 || !$("timeline").contains(anchor) || !$("timeline").contains(focus) || !writableRoom(selectedRoom()) || state.view !== "chat" || document.querySelector("dialog[open]")) {
      hideSelectionActions(); return;
    }
    selectedText = { parent: state.selected, source: article?._message?.id || article?.dataset.key || "", text };
    const rect = selection.getRangeAt(0).getBoundingClientRect();
    toolbar.hidden = false;
    const width = toolbar.offsetWidth;
    toolbar.style.left = `${Math.max(8, Math.min(innerWidth - width - 8, rect.left + rect.width / 2 - width / 2))}px`;
    toolbar.style.top = `${Math.min(innerHeight - toolbar.offsetHeight - 8, Math.max(8, rect.top > 55 ? rect.top - toolbar.offsetHeight - 9 : rect.bottom + 9))}px`;
  }
  toolbar.addEventListener("pointerdown", (event) => event.preventDefault());
  document.addEventListener("pointerdown", (event) => {
    if (toolbar.contains(event.target)) return;
    selectionPointers.add(event.pointerId); hideSelectionActions();
  }, true);
  document.addEventListener("pointerup", (event) => {
    if (selectionPointers.delete(event.pointerId)) scheduleSelectionActions();
  }, true);
  document.addEventListener("pointercancel", (event) => {
    selectionPointers.delete(event.pointerId); hideSelectionActions();
  }, true);
  document.addEventListener("selectionchange", scheduleSelectionActions);
  $("timeline-scroll").addEventListener("scroll", hideSelectionActions, { passive: true });
  window.addEventListener("resize", hideSelectionActions);
  window.addEventListener("blur", () => { selectionPointers.clear(); hideSelectionActions(); });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { selectionPointers.clear(); hideSelectionActions(); return; }
    lastRefresh = 0; synchronize();
  });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") hideSelectionActions(); });
  for (const item of [sideButton, detailButton]) item.addEventListener("click", () => createCompanion(item.dataset.kind));

  async function supported() {
    if (capability === true || Date.now() - capabilityAt < 10000) return capability;
    capabilityAt = Date.now();
    try { capability = (await api("/api/health")).room_companions === true; } catch { capability = null; }
    return capability;
  }

  async function createCompanion(kind) {
    const selection = selectedText;
    if (!selection || selection.parent !== state.selected) return;
    sideButton.disabled = detailButton.disabled = true;
    // CODEX: An uncertain create retry reuses the same identity and cannot create duplicate panels.
    const fingerprint = JSON.stringify([device, selection.parent, kind, selection.source, selection.text]);
    if (!createRequests.has(fingerprint)) createRequests.set(fingerprint, requestId());
    touch(selection.parent);
    try {
      if (!await supported()) throw new Error("Side panels are waiting for the server update. Your selection is kept; try again shortly.");
      const result = await api(path(selection.parent), { method: "POST", body: JSON.stringify({
        kind, source_message_id: selection.source, selected_text: selection.text, request_id: createRequests.get(fingerprint),
      }) });
      const companion = result.companion;
      touch(selection.parent); createRequests.delete(fingerprint);
      const list = rooms.get(selection.parent) || [];
      if (!list.some((item) => item.id === companion.id)) list.push(companion);
      rooms.set(selection.parent, list); mergeRoom(companion.room);
      save(activeKey(selection.parent, kind), companion.id);
      hideSelectionActions(); window.getSelection()?.removeAllRanges();
      renderPanels();
      if (state.selected === selection.parent) document.getElementById(`companion-tab-${companion.id}`)?.focus({ preventScroll: true });
      if (kind === "more_details") refreshDetails(companion);
    } catch (error) { toast(error.message); }
    finally { sideButton.disabled = detailButton.disabled = false; }
  }

  function panelFor(companion) {
    let panel = panels.get(companion.id);
    if (panel) { panel.companion = companion; return panel; }
    const node = element("section", "companion-panel"); node.id = `companion-panel-${companion.id}`;
    node.setAttribute("role", "tabpanel"); node.setAttribute("aria-labelledby", `companion-tab-${companion.id}`);
    const close = button("×", "icon-button companion-close");
    close.setAttribute("aria-label", `Close ${companion.kind === "side_thread" ? "Side thread" : "More details"}`);
    close.title = companion.kind === "side_thread" ? "Delete this side thread and its history" : "Delete this explanation";
    close.addEventListener("click", () => deleteCompanion(panel));
    if (companion.kind !== "side_thread") {
      const quote = element("details", "companion-source");
      const summary = element("summary", "", "Selected passage · room context included");
      const excerpt = element("blockquote", "", companion.selected_text); excerpt.dir = direction(companion.selected_text);
      quote.append(summary, excerpt); node.append(quote);
    }
    const status = element("p", "companion-status"); status.setAttribute("role", "status"); status.hidden = true; node.append(status);
    panel = { companion, node, close, status, busy: false, started: false, refreshAt: 0, bodyText: null };
    if (companion.kind !== "side_thread") {
      node.append(element("p", "companion-model", "Terra · Low · Standard · Explanation only"));
      const body = element("div", "companion-explanation message-content"); body.tabIndex = 0;
      body.setAttribute("aria-label", "Explanation"); node.append(body); panel.body = body;
      const retry = button("Retry explanation", "button companion-retry"); retry.hidden = true;
      retry.addEventListener("click", () => { panel.started = false; refreshDetails(panel.companion, true); });
      node.append(retry); panel.retry = retry;
    }
    panels.set(companion.id, panel); (companion.kind === "side_thread" ? bodies : detailBodies).append(node); return panel;
  }

  function setPanelVisible(panel, visible) {
    panel.node.hidden = !visible;
    if (visible && panel.companion.kind === "side_thread" && !panel.frame) {
      const companion = panel.companion;
      const frame = document.createElement("iframe"); frame.className = "companion-frame"; frame.title = "Independent side thread";
      const url = new URL(location.pathname, location.origin);
      url.search = new URLSearchParams({ room: companion.room_id, view: "chat", embedded: "1", parent_room: companion.parent_room_id, companion: companion.id });
      frame.src = url.href; panel.node.append(frame); panel.frame = frame;
      frame.addEventListener("load", () => frame.contentWindow?.postMessage({ type: "codex-companion-visible", visible: !panel.node.hidden }, location.origin));
    }
    if (panel.visible !== visible) panel.frame?.contentWindow?.postMessage({ type: "codex-companion-visible", visible }, location.origin);
    panel.visible = visible;
  }

  function renderPanels() {
    const list = currentCompanions();
    const visible = state.view === "chat" && !!state.me && list.length > 0 && !$("workspace").hidden;
    const sideList = list.filter((item) => item.kind === "side_thread");
    const detailList = list.filter((item) => item.kind === "more_details");
    dock.hidden = divider.hidden = !visible || !sideList.length;
    detailsWindow.hidden = !visible || !detailList.length;
    workbench.hidden = state.view !== "chat";
    workbench.classList.toggle("has-companion", !dock.hidden);
    for (const panel of panels.values()) if (!visible || panel.companion.parent_room_id !== state.selected) setPanelVisible(panel, false);
    const activeSide = renderPanelGroup(sideList, tabs, "side_thread", visible);
    renderPanelGroup(detailList, detailTabs, "more_details", visible);
    updateSideMenu(activeSide);
    updateSplitSize(Number(saved(splitKey())) || 50, false);
    if (!detailsWindow.hidden) requestAnimationFrame(() => applyDetailsGeometry());
  }
  function renderPanelGroup(list, tabs, kind, visible) {
    let active = saved(activeKey(state.selected, kind));
    if (!list.some((item) => item.id === active)) active = list[list.length - 1]?.id;
    for (const panel of panels.values()) if (panel.companion.kind === kind && (!visible || panel.companion.id !== active)) setPanelVisible(panel, false);
    if (!visible) return null;
    const old = new Map([...tabs.children].map((tab) => [tab.dataset.companion, tab]));
    const desired = [];
    for (const companion of list) {
      const panel = panelFor(companion);
      let tab = old.get(companion.id);
      if (!tab) {
        tab = button("", "companion-tab"); tab.dataset.companion = companion.id;
        tab.id = `companion-tab-${companion.id}`; tab.setAttribute("role", "tab"); tab.setAttribute("aria-controls", panel.node.id);
        tab.addEventListener("click", () => { save(activeKey(companion.parent_room_id, kind), companion.id); renderPanels(); });
      }
      const index = list.findIndex((item) => item.id === companion.id) + 1;
      tab.textContent = companion.kind === "side_thread" ? `↗${list.length > 1 ? ` ${index}` : ""}` : String(index);
      tab.setAttribute("aria-label", companion.kind === "side_thread" ? `Side thread ${index}` : `More details ${index}`);
      tab.setAttribute("aria-selected", String(companion.id === active)); tab.tabIndex = companion.id === active ? 0 : -1;
      setPanelVisible(panel, companion.id === active); desired.push(tab);
    }
    for (const tab of old.values()) if (!desired.includes(tab)) tab.remove();
    for (let index = 0; index < desired.length; index++) if (tabs.children[index] !== desired[index]) tabs.insertBefore(desired[index], tabs.children[index] || null);
    return list.find((item) => item.id === active) || null;
  }
  for (const tabList of [tabs, detailTabs]) tabList.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const items = [...tabList.children], index = items.indexOf(document.activeElement);
    if (index < 0) return;
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + items.length) % items.length;
    items[next].click(); items[next].focus();
  });

  function activeCompanion(kind) {
    const list = currentCompanions().filter((item) => item.kind === kind);
    const active = saved(activeKey(state.selected, kind));
    return list.find((item) => item.id === active) || list[list.length - 1] || null;
  }
  function updateSideMenu(companion) {
    sideMenu.hidden = !companion;
    if (!companion) { sideMenu.open = false; return; }
    const excerpt = companion.selected_text.trim().replace(/\s+/g, " ");
    sideMenuSource.textContent = excerpt ? `Selected: ${excerpt}` : "Selected room context";
    sideMenuSource.title = companion.selected_text;
    sideMenuSource.dir = direction(companion.selected_text);
  }
  sideSettings.addEventListener("click", () => {
    const companion = activeCompanion("side_thread"), panel = companion && panels.get(companion.id);
    sideMenu.open = false;
    panel?.frame?.contentWindow?.postMessage({ type: "codex-companion-room-settings" }, location.origin);
  });
  sideDelete.addEventListener("click", () => {
    const companion = activeCompanion("side_thread"), panel = companion && panels.get(companion.id);
    sideMenu.open = false;
    if (panel) deleteCompanion(panel);
  });
  detailsClose.addEventListener("click", () => {
    const companion = activeCompanion("more_details"), panel = companion && panels.get(companion.id);
    if (panel) deleteCompanion(panel);
  });
  document.addEventListener("pointerdown", (event) => {
    if (sideMenu.open && !sideMenu.contains(event.target)) sideMenu.open = false;
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && sideMenu.open) { sideMenu.open = false; sideMenuTrigger.focus(); }
  });

  const detailsGeometryKey = () => `codex-companion-details-geometry:${device}:${state.selected}`;
  let detailsGeometry = null, detailsGeometryRoom = "", detailsInteraction = null;
  function defaultDetailsGeometry() {
    const width = workbench.clientWidth, height = workbench.clientHeight;
    const panelWidth = Math.min(540, Math.max(280, width - 36));
    const panelHeight = Math.min(650, Math.max(230, height - 36));
    return { x: Math.max(8, width - panelWidth - 18), y: Math.min(18, Math.max(8, height - panelHeight - 8)), width: panelWidth, height: panelHeight };
  }
  function clampDetailsGeometry(value) {
    const outerWidth = Math.max(1, workbench.clientWidth), outerHeight = Math.max(1, workbench.clientHeight);
    const margin = outerWidth < 500 ? 8 : 12;
    const maxWidth = Math.max(1, outerWidth - margin * 2), maxHeight = Math.max(1, outerHeight - margin * 2);
    const minWidth = Math.min(320, maxWidth), minHeight = Math.min(240, maxHeight);
    const width = Math.max(minWidth, Math.min(maxWidth, Number(value?.width) || 540));
    const height = Math.max(minHeight, Math.min(maxHeight, Number(value?.height) || 650));
    const x = Math.max(margin, Math.min(outerWidth - width - margin, Number(value?.x) || 0));
    const y = Math.max(margin, Math.min(outerHeight - height - margin, Number(value?.y) || 0));
    return { x, y, width, height };
  }
  function setDetailsGeometry(value, persist = false) {
    detailsGeometry = clampDetailsGeometry(value);
    detailsWindow.style.left = `${detailsGeometry.x}px`;
    detailsWindow.style.top = `${detailsGeometry.y}px`;
    detailsWindow.style.width = `${detailsGeometry.width}px`;
    detailsWindow.style.height = `${detailsGeometry.height}px`;
    if (persist) save(detailsGeometryKey(), JSON.stringify(detailsGeometry));
  }
  function applyDetailsGeometry(reset = false) {
    if (detailsWindow.hidden || !state.selected) return;
    const identity = `${device}:${state.selected}`;
    if (reset || detailsGeometryRoom !== identity) {
      detailsGeometryRoom = identity; detailsGeometry = null;
      if (!reset) {
        try { detailsGeometry = JSON.parse(saved(detailsGeometryKey()) || "null"); } catch { detailsGeometry = null; }
      }
    }
    setDetailsGeometry(detailsGeometry || defaultDetailsGeometry());
  }
  function startDetailsInteraction(event, mode) {
    if (event.button !== 0) return;
    applyDetailsGeometry();
    event.preventDefault();
    const target = event.currentTarget;
    detailsInteraction = { mode, target, pointer: event.pointerId, x: event.clientX, y: event.clientY, start: { ...detailsGeometry } };
    target.setPointerCapture(event.pointerId); detailsWindow.classList.add("is-adjusting");
  }
  function moveDetailsInteraction(event) {
    if (!detailsInteraction || detailsInteraction.pointer !== event.pointerId) return;
    const dx = event.clientX - detailsInteraction.x, dy = event.clientY - detailsInteraction.y;
    const start = detailsInteraction.start;
    setDetailsGeometry(detailsInteraction.mode === "move"
      ? { ...start, x: start.x + dx, y: start.y + dy }
      : { ...start, width: start.width + dx, height: start.height + dy });
  }
  function finishDetailsInteraction(event) {
    if (!detailsInteraction || (event.pointerId !== undefined && detailsInteraction.pointer !== event.pointerId)) return;
    const { target, pointer } = detailsInteraction;
    detailsInteraction = null; detailsWindow.classList.remove("is-adjusting");
    if (target.hasPointerCapture(pointer)) target.releasePointerCapture(pointer);
    if (detailsGeometry) save(detailsGeometryKey(), JSON.stringify(detailsGeometry));
  }
  for (const [target, mode] of [[detailsGrip, "move"], [detailsResize, "resize"]]) {
    target.addEventListener("pointerdown", (event) => startDetailsInteraction(event, mode));
    target.addEventListener("pointermove", moveDetailsInteraction);
    for (const type of ["pointerup", "pointercancel", "lostpointercapture"]) target.addEventListener(type, finishDetailsInteraction);
    target.addEventListener("keydown", (event) => {
      if (!detailsGeometry || !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
      event.preventDefault();
      const step = event.shiftKey ? 30 : 10;
      const dx = event.key === "ArrowLeft" ? -step : event.key === "ArrowRight" ? step : 0;
      const dy = event.key === "ArrowUp" ? -step : event.key === "ArrowDown" ? step : 0;
      setDetailsGeometry(mode === "move"
        ? { ...detailsGeometry, x: detailsGeometry.x + dx, y: detailsGeometry.y + dy }
        : { ...detailsGeometry, width: detailsGeometry.width + dx, height: detailsGeometry.height + dy }, true);
    });
  }
  detailsGrip.addEventListener("dblclick", () => {
    forget(detailsGeometryKey()); applyDetailsGeometry(true); save(detailsGeometryKey(), JSON.stringify(detailsGeometry));
  });
  window.addEventListener("blur", finishDetailsInteraction);

  const splitKey = () => `codex-companion-split:${device}:${state.selected}`;
  let resizing = null;
  function updateSplitSize(value, persist = true) {
    const width = workbench.clientWidth;
    const minimum = Math.min(45, 310 / Math.max(width, 1) * 100);
    const percent = Math.max(minimum, Math.min(100 - minimum, value));
    workbench.style.setProperty("--companion-main-size", `${percent}%`);
    divider.setAttribute("aria-valuemin", String(Math.round(minimum)));
    divider.setAttribute("aria-valuemax", String(Math.round(100 - minimum)));
    divider.setAttribute("aria-valuenow", String(Math.round(percent)));
    divider.setAttribute("aria-valuetext", `Main room ${Math.round(percent)}%, side thread ${Math.round(100 - percent)}%`);
    if (persist) save(splitKey(), String(percent));
  }
  function finishResize() {
    if (resizing !== null && divider.hasPointerCapture(resizing)) divider.releasePointerCapture(resizing);
    resizing = null; workbench.classList.remove("resizing-companion");
  }
  divider.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    event.preventDefault(); divider.focus(); resizing = event.pointerId;
    divider.setPointerCapture(event.pointerId); workbench.classList.add("resizing-companion");
  });
  divider.addEventListener("pointermove", (event) => {
    if (resizing !== event.pointerId) return;
    const box = workbench.getBoundingClientRect(); updateSplitSize((event.clientX - box.left) / box.width * 100);
  });
  for (const type of ["pointerup", "pointercancel", "lostpointercapture"]) divider.addEventListener(type, finishResize);
  window.addEventListener("blur", finishResize);
  divider.addEventListener("keydown", (event) => {
    const value = Number(divider.getAttribute("aria-valuenow"));
    const values = { ArrowLeft: value - 3, ArrowRight: value + 3, Home: 0, End: 100 };
    if (!(event.key in values)) return;
    event.preventDefault(); updateSplitSize(values[event.key]);
  });
  new ResizeObserver(() => {
    updateSplitSize(Number(saved(splitKey())) || 50, false);
    if (!detailsWindow.hidden) applyDetailsGeometry();
  }).observe(workbench);

  async function deleteCompanion(panel) {
    const companion = panel.companion;
    if (companion.kind === "side_thread" && !confirm("Delete this side thread? Its messages, draft and saved context will be removed, and any running response will be stopped. The main room is kept. Recorded usage is not reset.")) return;
    panel.close.disabled = true;
    if (companion.kind === "side_thread") sideDelete.disabled = true;
    else detailsClose.disabled = true;
    panel.status.hidden = false; panel.status.textContent = "Closing and deleting this panel…";
    touch(companion.parent_room_id);
    try {
      await api(`${path(companion.parent_room_id)}/${encodeURIComponent(companion.id)}`, { method: "DELETE" });
      touch(companion.parent_room_id);
      dropPanel(companion);
      rooms.set(companion.parent_room_id, (rooms.get(companion.parent_room_id) || []).filter((item) => item.id !== companion.id));
      renderPanels();
      if (state.selected === companion.parent_room_id) (tabs.querySelector('[aria-selected="true"]') || $("message-input")).focus({ preventScroll: true });
    } catch (error) {
      panel.status.hidden = false; panel.status.textContent = `Not deleted: ${error.message} Reconnect and close again.`;
    } finally {
      panel.close.disabled = false;
      if (companion.kind === "side_thread") sideDelete.disabled = false;
      else detailsClose.disabled = false;
    }
  }
  function dropPanel(companion) {
    const panel = panels.get(companion.id);
    panel?.node.remove(); panels.delete(companion.id);
    forget(draftKey(device, companion.id));
    if (saved(activeKey(companion.parent_room_id, companion.kind)) === companion.id) forget(activeKey(companion.parent_room_id, companion.kind));
    const cache = state.roomStates.get(companion.room_id);
    if (cache) releaseRoomState(cache);
    state.roomStates.delete(companion.room_id);
    state.rooms = state.rooms.filter((room) => room.id !== companion.room_id);
  }

  async function refreshDetails(companion, retry = false) {
    const panel = panelFor(companion);
    if (panel.busy || panel.close.disabled || (panel.done && !retry) || (!retry && Date.now() < (panel.retryAt || 0))) return;
    panel.busy = true; panel.refreshAt = Date.now();
    try {
      const result = await api(`/api/rooms/${encodeURIComponent(companion.room_id)}/messages`);
      if (!panels.has(companion.id)) return;
      const runs = result.runs || [];
      if (!runs.length && !panel.started) {
        panel.started = true;
        panel.status.hidden = false; panel.status.textContent = "Requesting an explanation…";
        await api(`/api/rooms/${encodeURIComponent(companion.room_id)}/runs`, { method: "POST", body: JSON.stringify({
          text: "Explain the selected passage using the source message and room context. Answer in the language of the selected passage. Do not perform actions or modify anything.",
          request_id: `companion:${companion.id}:explain`, tool_read_only: true, attachment_ids: [],
        }) });
        panel.retry.hidden = true; return;
      }
      const running = runs.find(activeRun);
      const messages = (result.messages || []).filter((item) => item.role === "assistant");
      const text = [messages.map((item) => item.content || "").join("\n\n"), running?.output_text || ""].filter(Boolean).join("\n\n");
      panel.pendingText = text; renderExplanation(panel);
      panel.failures = 0; panel.retryAt = 0;
      const failure = runs.find((run) => ["failed", "interrupted", "cancelled", "stopped"].includes(run.status));
      const waiting = running && runConnection(running);
      const status = waiting ? "Reconnecting to the model — this panel is saved." : running ? "Explaining the selected passage…" : failure ? failure.error || "Explanation stopped. Close this panel and select the passage again to start a new explanation." : text ? "Saved in this room until you close it." : "Waiting for an explanation…";
      panel.status.textContent = status; panel.status.hidden = false;
      panel.status.classList.toggle("working", !!running);
      panel.done = !!runs.length && !running;
      panel.retry.hidden = true;
    } catch (error) {
      panel.status.hidden = false; panel.status.classList.remove("working");
      panel.status.textContent = `${error.message} This panel and its saved text are kept.`;
      panel.retry.hidden = false;
      panel.started = false;
      panel.failures = (panel.failures || 0) + 1;
      panel.retryAt = Date.now() + Math.min(30000, 2000 * 2 ** Math.min(panel.failures, 4));
    } finally { panel.busy = false; }
  }

  function renderExplanation(panel) {
    if (typeof panel.pendingText === "string" && panel.bodyText !== panel.pendingText && !selectedWithin(panel.body)) {
      panel.body.replaceChildren(markdown(panel.pendingText)); panel.bodyText = panel.pendingText;
    }
  }

  async function refreshCompanions() {
    const parent = state.selected, identity = state.me?.id;
    const mutation = mutations.get(parent) || 0;
    if (refreshing || !parent || !identity || state.view !== "chat" || !writableRoom(selectedRoom())) return;
    refreshing = true; lastRefresh = Date.now();
    try {
      if (!await supported()) return;
      const result = await api(path(parent));
      if (identity !== state.me?.id || mutation !== (mutations.get(parent) || 0)) return;
      const list = result.companions || [];
      for (const previous of rooms.get(parent) || []) if (!list.some((item) => item.id === previous.id)) dropPanel(previous);
      rooms.set(parent, list);
      showNotice(); renderPanels();
    } catch (error) {
      if (state.selected === parent) showNotice("Reconnecting — your saved side panels stay open.");
    } finally { refreshing = false; }
  }

  function synchronize() {
    if (device !== idOf(state.me?.id)) {
      for (const panel of panels.values()) panel.node.remove();
      panels.clear(); rooms.clear(); mutations.clear(); createRequests.clear(); tabs.replaceChildren(); detailTabs.replaceChildren(); device = idOf(state.me?.id); lastRoom = ""; lastRefresh = 0;
    }
    if (lastRoom !== state.selected) { lastRoom = state.selected; lastRefresh = 0; hideSelectionActions(); }
    if (document.hidden) return;
    renderPanels();
    if (state.view !== "chat" || !state.selected || !state.me || $("workspace").hidden) return;
    if (Date.now() - lastRefresh > 5000) refreshCompanions();
    for (const companion of currentCompanions()) {
      const panel = panels.get(companion.id);
      if (panel?.body) renderExplanation(panel);
      if (companion.kind === "more_details" && panel && Date.now() - panel.refreshAt > 2000) refreshDetails(companion);
    }
  }
  window.addEventListener("codex-room-render", synchronize);
  new MutationObserver(synchronize).observe($("chat-view"), { attributes: true, attributeFilter: ["hidden"] });
  window.addEventListener("online", () => { lastRefresh = 0; synchronize(); });
  setInterval(synchronize, 1000);
  synchronize();
})();
