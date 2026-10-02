"use strict";

const $ = (id) => document.getElementById(id);
const embeddedRoomId = window.parent !== window && new URL(location.href).searchParams.get("embedded") === "1" ? new URL(location.href).searchParams.get("room") : "";
if (embeddedRoomId) document.documentElement.dataset.embedded = "true";
let embeddedPaused = false;
const state = {
  me: null, myQuota: null, workerOnline: false, workerConnection: null, workerCwd: "", connected: false, rooms: [], models: [],
  selected: null, roomStates: new Map(), view: "chat", selectionVersion: 0, focusRevision: 0,
  eventTimer: null, roomTimer: null, sessionTimer: null, adminTimer: null,
  toastTimer: null, toastReturnFocus: null, lastEnter: null, dialog: null, roomCreating: false, follow: true, authExpired: false, themeExplicit: false, sidebarCollapsed: false,
  selectionRefreshPending: false,
  adminSnapshot: null, adminDeviceId: "", adminPanel: "", routeApplying: false, routeReady: false,
  serverRequestId: 0, serverSuccessId: 0, serverFailureId: 0, serverFailureTimer: null, serverResumeTimer: null, serverResumeUntil: 0, serverBrowserOffline: false,
};
const terminalStatuses = new Set(["completed", "cancelled", "canceled", "failed", "error", "stopped", "interrupted", "rejected"]);
const themes = ["light", "dark", "midnight", "chatgpt", "torte", "cosmic", "aurora", "ember", "velvet"];
const themeLabels = { chatgpt: "ChatGPT Black", torte: "Vim Torte" };
const spaceThemes = new Set(["cosmic", "aurora", "ember", "velvet"]);
const formatNumber = new Intl.NumberFormat("en-US");
const persianPattern = /[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff\ufb50-\ufdff\ufe70-\ufeff]/u;
const browserLoginNameKey = "codex-lan-browser-login-name";
const easySignInOfferPrefix = "codex-lan-easy-signin-offer:";
const roomDraftPrefix = "codex-lan-draft:";
const outgoingMessageOrigins = new Map();
let sendFeedbackTimer = 0;
const replyAlerts = { device: "", latest: new Map(), open: new Map(), permissionRequested: false, tab: Math.random().toString(36).slice(2) };

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function direction(text) { return persianPattern.test(String(text || "")) ? "rtl" : "ltr"; }
function idOf(value) { return String(value ?? ""); }
function selectedRoom() { return state.rooms.find((room) => idOf(room.id) === state.selected); }
function isAdmin() { return state.me?.role === "admin"; }
function modelCode(model) { return model.model || model.id; }
function modelGroup(model) {
  const major = /^gpt-(\d+)/i.exec(modelCode(model))?.[1];
  return major ? `GPT-${major}` : "Other models";
}
function orderedModels(models) {
  // CODEX: Sort only the presentation; provider defaults and saved room selections remain authoritative.
  const families = { astra: 0, sol: 1, terra: 2, luna: 3 };
  const keyed = models.map((model, index) => {
    const match = /^gpt-(\d+)(?:\.(\d+))?(?:-([a-z]+))?/i.exec(modelCode(model));
    return { model, index, major: match ? Number(match[1]) : -1, minor: Number(match?.[2] || 0), family: families[match?.[3]?.toLowerCase()] ?? 4 };
  });
  keyed.sort((a, b) => b.major - a.major || (a.major >= 6 ? a.family - b.family || b.minor - a.minor : b.minor - a.minor || a.family - b.family) || a.index - b.index);
  return keyed.map(({ model }) => model);
}
function matchingModel(value) { return state.models.find((model) => model.id === value || model.model === value); }
function effortCode(effort) { return typeof effort === "string" ? effort : effort?.reasoningEffort || effort?.effort || ""; }
function effortLabel(value) {
  const code = String(value || "").toLowerCase();
  if (!code) return "Default";
  if (code === "max") return "Max";
  if (code === "ultra") return "Ultra";
  if (code === "xhigh") return "Extra high";
  return code[0].toUpperCase() + code.slice(1);
}
function ownRoom(room) { return !!room && idOf(room.owner_id ?? room.owner_device_id ?? room.device_id) === idOf(state.me?.id); }
function writableRoom(room) { return ownRoom(room) && room.read_only !== true; }
function sessionDevice(result) {
  const source = result?.device && typeof result.device === "object" ? result.device : result || {};
  const loginName = typeof result?.login_name === "string" ? result.login_name : typeof source.login_name === "string" ? source.login_name : "";
  const passwordLoginEnabled = Object.hasOwn(result || {}, "password_login_enabled")
    ? result.password_login_enabled === true : source.password_login_enabled === true;
  return { ...source, login_name: loginName, password_login_enabled: passwordLoginEnabled };
}
function rememberedBrowserLoginName() {
  try { return localStorage.getItem(browserLoginNameKey) || ""; } catch { return ""; }
}
function rememberBrowserLoginName(name, remember = true) {
  try {
    if (remember && name) localStorage.setItem(browserLoginNameKey, name);
    else localStorage.removeItem(browserLoginNameKey);
  } catch {}
}
function setPasswordVisibility(button, inputs, visible) {
  for (const input of inputs) input.type = visible ? "text" : "password";
  button.setAttribute("aria-pressed", String(visible));
  button.setAttribute("aria-label", visible ? "Hide password" : "Show password");
  button.textContent = visible ? "Hide" : "Show";
}
function togglePasswordVisibility(button, inputs) {
  setPasswordVisibility(button, inputs, button.getAttribute("aria-pressed") !== "true");
}
function markRoomReadOnly(roomId, reason) {
  mergeRoom({ id: roomId, read_only: true, read_only_reason: reason });
  renderRooms(); renderHeader(); updateComposer();
}
function rememberedRoom() {
  if (embeddedRoomId) return state.rooms.find((room) => idOf(room.id) === embeddedRoomId);
  let requested = "", saved = "";
  try { requested = new URL(location.href).searchParams.get("room") || ""; } catch {}
  try { if (state.me?.id) saved = localStorage.getItem(`codex-lan-room:${state.me.id}`) || ""; } catch {}
  const recent = state.rooms.filter((room) => !room.companion_kind).sort((a, b) => timestamp(b.updated_at) - timestamp(a.updated_at) || idOf(a.id).localeCompare(idOf(b.id)));
  // CODEX: A tab's URL wins over the device preference, but only for accessible rooms.
  return state.rooms.find((room) => idOf(room.id) === requested)
    || state.rooms.find((room) => idOf(room.id) === saved)
    || recent.find(ownRoom) || recent[0];
}

function rememberRoom(id) {
  if (embeddedRoomId) return;
  if (!state.me?.id) return;
  const key = idOf(id);
  if (key && !state.rooms.some((room) => idOf(room.id) === key)) return;
  try {
    const storageKey = `codex-lan-room:${state.me.id}`;
    if (key) localStorage.setItem(storageKey, key);
    else localStorage.removeItem(storageKey);
  } catch {}
}

function roomDraftKey(roomId, deviceId = state.me?.id) {
  const device = idOf(deviceId), room = idOf(roomId);
  return device && room && !embeddedRoomId ? `${roomDraftPrefix}${device}:${room}` : "";
}

function savedRoomDraft(roomId, deviceId = state.me?.id) {
  const key = roomDraftKey(roomId, deviceId);
  if (!key) return "";
  try { return localStorage.getItem(key) || ""; } catch { return ""; }
}

function saveRoomDraft(roomId, draft, deviceId = state.me?.id) {
  const key = roomDraftKey(roomId, deviceId);
  if (!key) return;
  try {
    if (draft) localStorage.setItem(key, draft);
    else localStorage.removeItem(key);
  } catch {}
}

function forgetRoomDraft(roomId, deviceId = state.me?.id) { saveRoomDraft(roomId, "", deviceId); }

const routePanels = new Set(["usage", "connector", "signin", "room-settings", "device", "host-setup"]);

function locationRoute() {
  const params = new URL(location.href).searchParams;
  const panel = params.get("panel") || "";
  return {
    view: params.get("view") === "admin" ? "admin" : "chat",
    room: params.get("room") || "",
    panel: routePanels.has(panel) ? panel : "",
    device: params.get("device") || "",
  };
}

function replaceAppRoute(route = {}, mode = "replace") {
  const url = new URL(location.href);
  const view = route.view === "admin" ? "admin" : "chat";
  const room = idOf(route.room ?? state.selected);
  const panel = routePanels.has(route.panel) ? route.panel : "";
  const device = panel === "device" ? idOf(route.device) : "";
  url.searchParams.set("view", view);
  if (room && state.rooms.some((item) => idOf(item.id) === room)) url.searchParams.set("room", room); else url.searchParams.delete("room");
  if (panel) url.searchParams.set("panel", panel); else url.searchParams.delete("panel");
  if (device) url.searchParams.set("device", device); else url.searchParams.delete("device");
  const target = url.pathname + url.search + url.hash;
  const current = location.pathname + location.search + location.hash;
  if (target === current) return;
  history[mode === "push" ? "pushState" : "replaceState"](history.state, "", target);
}

function visibleRoutePanel() {
  if ($("admin-device-dialog")?.open) return { panel: "device", device: state.adminDeviceId };
  if ($("my-usage-dialog")?.open) return { panel: "usage" };
  if ($("connector-dialog")?.open) return { panel: "connector" };
  if ($("browser-login-dialog")?.open) return { panel: "signin" };
  if ($("room-dialog")?.open) return { panel: "room-settings" };
  if (state.view === "admin" && state.adminPanel === "host-setup") return { panel: "host-setup" };
  return { panel: "" };
}

function persistVisibleRoute(mode = "replace") {
  if (state.routeApplying || !state.me) return;
  publishReplyView();
  const visible = visibleRoutePanel();
  replaceAppRoute({ view: state.view, room: state.selected, ...visible }, mode);
}

function closeRouteDialogs(except = "") {
  const dialogs = {
    device: $("admin-device-dialog"), usage: $("my-usage-dialog"), connector: $("connector-dialog"),
    signin: $("browser-login-dialog"), "room-settings": $("room-dialog"),
  };
  for (const [panel, dialog] of Object.entries(dialogs)) if (panel !== except && dialog?.open) dialog.close();
}

async function applyLocationRoute() {
  if (!state.me || !["approved", "active"].includes(state.me.status)) return;
  const requested = locationRoute();
  state.routeApplying = true;
  try {
    closeRouteDialogs(requested.panel);
    if (requested.view === "admin" && isAdmin()) {
      const panel = requested.panel === "host-setup" ? "host-setup" : "";
      if (state.view !== "admin" || state.adminPanel !== panel) await openAdmin({ route: false, panel });
    } else {
      const room = state.rooms.find((item) => idOf(item.id) === requested.room) || selectedRoom() || rememberedRoom();
      if (room && (state.view !== "chat" || state.selected !== idOf(room.id))) await selectRoom(room.id, { route: false });
    }
    if (requested.panel === "usage") await openMyUsage({ route: false, refresh: false });
    else if (requested.panel === "connector") await openConnector({ route: false });
    else if (requested.panel === "signin") openBrowserLogin({ route: false });
    else if (requested.panel === "room-settings" && state.view === "chat") openRoomDialog("edit", "", requested.room || state.selected, { route: false });
    else if (requested.panel === "device" && state.view === "admin" && isAdmin()) await openAdminDevice(requested.device, { route: false });
  } finally {
    state.routeApplying = false;
    state.routeReady = true;
    persistVisibleRoute("replace");
  }
}

function sidebarPreferences() {
  const device = idOf(state.me?.id);
  if (state.sidebarPreferences?.device === device) return state.sidebarPreferences;
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(`codex-lan-sidebar:${device}`) || "{}"); } catch {}
  const seen = Object.create(null);
  for (const [id, value] of Object.entries(saved?.seen || {}).slice(-5000)) {
    if (value && typeof value.id === "string") seen[id] = { id: value.id, at: numeric(value.at) };
  }
  state.sidebarPreferences = { device, pins: Array.isArray(saved?.pins) ? [...new Set(saved.pins.filter((id) => typeof id === "string"))].slice(0, 5000) : [], seen };
  return state.sidebarPreferences;
}

function saveSidebarPreferences() {
  const preferences = sidebarPreferences();
  if (!preferences.device) return;
  try { localStorage.setItem(`codex-lan-sidebar:${preferences.device}`, JSON.stringify({ pins: preferences.pins, seen: preferences.seen })); } catch {}
}

function latestRoomReply(room, cachedOnly = false) {
  let latest = !cachedOnly && room?.last_assistant_message_id
    ? { id: idOf(room.last_assistant_message_id), at: timestamp(room.last_assistant_message_at) } : { id: "", at: 0 };
  for (const message of state.roomStates.get(idOf(room?.id))?.messages || []) {
    if (message.role === "assistant" && timestamp(message.created_at) >= latest.at) latest = { id: idOf(message.id), at: timestamp(message.created_at) };
  }
  return latest;
}

function markVisibleRoomRead() {
  const room = selectedRoom();
  const cache = state.roomStates.get(state.selected);
  if (!ownRoom(room) || !viewingReplyRoom(state.selected) || !state.follow || !cache?.loaded || cache.loading) return false;
  const latest = latestRoomReply(room, true);
  const preferences = sidebarPreferences();
  const previous = preferences.seen[idOf(room.id)];
  if (latest.id && (!previous || latest.at > previous.at || (latest.at === previous.at && latest.id !== previous.id))) {
    preferences.seen[idOf(room.id)] = latest;
    return true;
  }
  return false;
}

function roomHasUnread(room) {
  // CODEX: Administrative visibility does not make another user's replies personal notifications.
  if (!ownRoom(room)) return false;
  const latest = latestRoomReply(room);
  const previous = sidebarPreferences().seen[idOf(room.id)];
  return !!latest.id && !!previous && latest.id !== previous.id && latest.at >= previous.at;
}

function viewingReplyRoom(roomId) {
  return state.view === "chat" && state.selected === idOf(roomId) && !document.hidden && document.hasFocus() && !visibleRoutePanel().panel;
}

function publishReplyView(clear = false) {
  if (embeddedRoomId || !state.me) return;
  const key = `codex-lan-reply-view:${idOf(state.me.id)}`;
  const viewing = !clear && viewingReplyRoom(state.selected);
  if (viewing) closeReplyNotifications(state.selected);
  try {
    if (viewing) {
      localStorage.setItem(key, JSON.stringify({ tab: replyAlerts.tab, room: state.selected, at: Date.now() }));
    } else if (JSON.parse(localStorage.getItem(key) || "null")?.tab === replyAlerts.tab) localStorage.removeItem(key);
  } catch {}
}

function closeReplyNotifications(roomId = null) {
  for (const [id, notification] of replyAlerts.open) {
    if (roomId !== null && id !== idOf(roomId)) continue;
    try { notification.close(); } catch {}
    replyAlerts.open.delete(id);
  }
}

async function enableReplyNotifications({ automatic = false } = {}) {
  if (embeddedRoomId || !("Notification" in window) || !window.isSecureContext) return;
  if (automatic && (replyAlerts.permissionRequested || Notification.permission !== "default")) return;
  replyAlerts.permissionRequested = true;
  try {
    const permission = Notification.permission === "default" ? await Notification.requestPermission() : Notification.permission;
    if (!automatic) toast(permission === "granted" ? "Notifications enabled for replies when you are away from the conversation."
      : permission === "denied" ? "Allow notifications in your browser's site settings. In-app reply alerts are still enabled."
        : "In-app reply alerts are enabled. Desktop notifications need browser permission.");
  } catch {}
}

function showReplyNotification(room, deviceId) {
  const roomId = idOf(room.id), title = room.title || "New chat";
  const openRoom = () => {
    if (idOf(state.me?.id) !== deviceId) return;
    window.focus();
    closeReplyNotifications(roomId);
    selectRoom(roomId).catch((error) => toast(error.message));
  };
  if ("Notification" in window && Notification.permission === "granted") {
    try {
      closeReplyNotifications(roomId);
      const notification = new Notification(`Reply ready · ${title}`, {
        body: "Your Codex response is ready. Click to open the room.",
        tag: `codex-reply:${deviceId}:${roomId}`,
        requireInteraction: true,
      });
      replyAlerts.open.set(roomId, notification);
      notification.onclick = openRoom;
      notification.onclose = () => { if (replyAlerts.open.get(roomId) === notification) replyAlerts.open.delete(roomId); };
      return;
    } catch {}
  }
  closeReplyNotifications(roomId);
  let stack = $("reply-notifications");
  if (!stack) {
    stack = element("div", "reply-notifications"); stack.id = "reply-notifications";
    stack.setAttribute("aria-label", "Reply notifications");
    document.body.append(stack);
  }
  const card = element("section", "reply-notification");
  card.setAttribute("role", "status");
  const message = element("strong", "", `Reply ready · ${title}`);
  const open = element("button", "button reply-notification-open", "Open room"); open.type = "button"; open.onclick = openRoom;
  const close = element("button", "icon-button reply-notification-close", "×"); close.type = "button";
  close.setAttribute("aria-label", `Dismiss reply notification for ${title}`);
  close.onclick = () => closeReplyNotifications(roomId);
  card.append(message, open, close); stack.append(card);
  replyAlerts.open.set(roomId, { close: () => card.remove() });
}

async function observeRoomReplies(rooms, initial = false) {
  if (embeddedRoomId || !state.me) return;
  const deviceId = idOf(state.me.id);
  if (replyAlerts.device !== deviceId) {
    closeReplyNotifications();
    replyAlerts.device = deviceId; replyAlerts.latest.clear(); initial = true;
  }
  publishReplyView();
  const jobs = [];
  for (const room of rooms) {
    if (!ownRoom(room) || room.companion_kind) continue;
    const roomId = idOf(room.id);
    const reply = { id: idOf(room.last_assistant_message_id), at: timestamp(room.last_assistant_message_at) };
    const previous = replyAlerts.latest.get(roomId);
    if (previous && (previous.id === reply.id || previous.at > reply.at)) continue;
    replyAlerts.latest.set(roomId, reply);
    if (!reply.id || initial) continue;
    const key = `codex-lan-reply-alert:${deviceId}:${roomId}`;
    const deliver = () => {
      if (idOf(state.me?.id) !== deviceId) return;
      let recorded;
      try { recorded = JSON.parse(localStorage.getItem(key) || "null"); } catch {}
      if (recorded && (recorded.id === reply.id || numeric(recorded.at) > reply.at)) return;
      // CODEX: Persist only reply IDs and timestamps; reloads and parallel tabs must not repeat an alert.
      try { localStorage.setItem(key, JSON.stringify(reply)); } catch {}
      const seen = sidebarPreferences().seen[roomId];
      let visible;
      try { visible = JSON.parse(localStorage.getItem(`codex-lan-reply-view:${deviceId}`) || "null"); } catch {}
      if (viewingReplyRoom(roomId) || (visible?.room === roomId && Date.now() - numeric(visible.at) < 20000)
        || seen?.id === reply.id || numeric(seen?.at) > reply.at) return;
      showReplyNotification(room, deviceId);
    };
    jobs.push(navigator.locks?.request ? navigator.locks.request(key, deliver).catch(() => {}) : Promise.resolve(deliver()));
  }
  await Promise.allSettled(jobs);
}

function toggleRoomPin(id) {
  const key = idOf(id);
  if (!state.rooms.some((room) => idOf(room.id) === key)) return;
  const preferences = sidebarPreferences();
  const pinned = preferences.pins.includes(key);
  preferences.pins = pinned ? preferences.pins.filter((value) => value !== key) : [...preferences.pins, key];
  saveSidebarPreferences(); renderRooms();
  toast(pinned ? "Room unpinned" : "Room pinned");
}

function roomIcon(pathData) {
  const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  for (const [key, value] of Object.entries({ viewBox: "0 0 24 24", width: "16", height: "16", fill: "none", stroke: "currentColor", "stroke-width": "1.7", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" })) icon.setAttribute(key, value);
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", pathData); icon.append(path); return icon;
}

function activeRun(run) { return run && !terminalStatuses.has(String(run.status || "running")); }
let roomStateCleanupPromise = Promise.resolve();
function roomState(id = state.selected) {
  const key = idOf(id);
  if (!state.roomStates.has(key)) {
    const deviceId = idOf(state.me?.id), draft = savedRoomDraft(key, deviceId);
    state.roomStates.set(key, { messages: [], runs: new Map(), cursor: 0, loaded: false, loading: false, loadVersion: 0, more: false, draft, draftCheck: draft, draftHasText: /\S/u.test(draft), composerRevision: 0, files: [], released: false, reply: null, retryNotice: "", sending: false, toolReadOnlySaving: false, modelSettingsSave: null, modelSettingsRevision: 0, modelSettingsConfirmed: null, refreshMessages: false, scrollTop: 0, follow: true, lastAccess: Date.now(), roomId: key, deviceId });
  }
  const cache = state.roomStates.get(key);
  cache.lastAccess = Date.now();
  return cache;
}
function activeRuns(cache = roomState()) { return [...cache.runs.values()].filter(activeRun); }
function releaseRoomState(cache) {
  if (!cache) return [];
  const mayDiscard = cache?.deviceId && cache.deviceId === idOf(state.me?.id) && !cache.sending;
  const files = cache.files || [], cleanups = [];
  cache.released = true;
  cache.files = [];
  for (const file of files) {
    if (mayDiscard) {
      file.discardRequested = true;
      cleanups.push(discardUploadedFile(file));
    }
    releasePreview(file);
  }
  return cleanups;
}
function clearRoomStates() {
  publishReplyView(true);
  closeReplyNotifications();
  replyAlerts.device = ""; replyAlerts.latest.clear();
  const cleanups = [];
  for (const cache of state.roomStates.values()) cleanups.push(...releaseRoomState(cache));
  state.roomStates.clear();
  const previous = roomStateCleanupPromise, current = Promise.allSettled(cleanups);
  roomStateCleanupPromise = Promise.allSettled([previous, current]).then(() => {});
  return roomStateCleanupPromise;
}
function pruneRoomStates(limit = 12) {
  const disposable = [...state.roomStates.entries()]
    .filter(([id, cache]) => id !== state.selected && !cache.sending && !cache.modelSettingsSave && !cache.draft?.trim() && !cache.files?.length && !cache.reply && !roomIsRunning(state.rooms.find((room) => idOf(room.id) === id)))
    .sort((left, right) => numeric(right[1].lastAccess) - numeric(left[1].lastAccess));
  for (const [id, cache] of disposable.slice(limit)) {
    releaseRoomState(cache);
    state.roomStates.delete(id);
  }
}
function waitingConnection(connection) { return connection && ["connecting", "reconnecting", "switching", "resuming"].includes(connection.state) ? connection : null; }
function runConnection(run) {
  if (!activeRun(run) || run.status === "stopping") return null;
  if (run.connection?.state === "connected") return null;
  return waitingConnection(run.connection) || (run.retrying === true ? { state: "reconnecting", message: "Waiting for the model service to reconnect." } : null);
}
function roomConnection(room) {
  const cache = state.roomStates.get(idOf(room?.id));
  if (idOf(room?.id) === state.selected && cache?.loaded && !cache.loading) return activeRuns(cache).map(runConnection).find(Boolean) || null;
  if (room && Object.hasOwn(room, "connection")) return waitingConnection(room.connection);
  return cache ? activeRuns(cache).map(runConnection).find(Boolean) || null : null;
}
function connectionLabel(connection, compact = false) {
  const saved = !connection.preferred_mode || connection.preferred_mode === connection.mode;
  const title = connection.state === "resuming" ? "Restoring response"
    : connection.state === "switching" || (connection.state === "reconnecting" && !saved) ? "Trying another network route"
    : connection.state === "reconnecting" ? "Retrying saved connection" : "Connecting";
  const mode = connection.mode === "proxy" ? (compact ? "Proxy" : "SOCKS5 proxy") : connection.mode === "direct" ? "Direct" : "";
  return [title, mode].filter(Boolean).join(" · ");
}
function numeric(value) { const n = Number(value); return Number.isFinite(n) && n >= 0 ? n : 0; }
function count(value) { return formatNumber.format(numeric(value)); }
function knownNumber(value) { return value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value)) && Number(value) >= 0 ? Number(value) : null; }
function formatDuration(value) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed <= 0) return "";
  const seconds = Math.max(1, Math.round(parsed));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m${seconds % 60 ? `${seconds % 60}s` : ""}`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h${minutes % 60 ? `${minutes % 60}m` : ""}`;
}
function timestamp(value) { return typeof value === "number" ? (value < 1e12 ? value * 1000 : value) : (timeValue(value)?.getTime() || 0); }
function timeValue(value) {
  if (value === null || value === undefined || value === "") return null;
  const parsed = typeof value === "number" ? new Date(value < 1e12 ? value * 1000 : value) : new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}
function timeLabel(value) { return timeValue(value)?.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" }) || ""; }
function dismissToast(timer) {
  if (timer !== undefined && timer !== state.toastTimer) return false;
  clearTimeout(state.toastTimer);
  state.toastTimer = null;
  const toastNode = $("toast");
  const close = $("toast-close");
  const returnFocus = state.toastReturnFocus;
  const restoreFocus = document.activeElement === close || document.activeElement === $("toast-action");
  toastNode.hidden = true;
  $("toast-action").hidden = true; $("toast-action").onclick = null;
  state.toastReturnFocus = null;
  if (restoreFocus && returnFocus?.isConnected && typeof returnFocus.focus === "function" && !returnFocus.disabled) returnFocus.focus({ preventScroll: true });
  return true;
}

function toast(message, action = null) {
  const toastNode = $("toast");
  clearTimeout(state.toastTimer);
  if (toastNode.hidden) {
    const active = document.activeElement;
    state.toastReturnFocus = active instanceof HTMLElement && active !== document.body && !toastNode.contains(active) ? active : null;
  }
  toastNode.hidden = false;
  $("toast-message").textContent = String(message);
  const actionButton = $("toast-action");
  actionButton.hidden = !action;
  actionButton.textContent = action?.label || "";
  actionButton.onclick = action ? () => { dismissToast(); action.run(); } : null;
  const timer = setTimeout(() => dismissToast(timer), action ? 12000 : 5200);
  state.toastTimer = timer;
}

const serverOfflineDelay = 450;
const serverResumeGrace = 2200;

function setServerConnected(connected) {
  if (state.connected === connected) return;
  state.connected = connected;
  updateConnection();
}

function clearServerFailureTimer() {
  clearTimeout(state.serverFailureTimer);
  state.serverFailureTimer = null;
}

function markServerReachable(requestId) {
  if (state.serverBrowserOffline && navigator.onLine === false) return;
  state.serverSuccessId = Math.max(state.serverSuccessId, requestId);
  if (requestId < state.serverFailureId) return;
  clearServerFailureTimer();
  setServerConnected(true);
}

function scheduleServerUnreachable(requestId) {
  state.serverFailureId = Math.max(state.serverFailureId, requestId);
  if (requestId < state.serverSuccessId) return;
  if (document.hidden) return;
  if (state.serverFailureTimer) return;
  const delay = Math.max(serverOfflineDelay, state.serverResumeUntil - Date.now());
  state.serverFailureTimer = setTimeout(() => {
    state.serverFailureTimer = null;
    if (document.hidden || state.serverFailureId < state.serverSuccessId) return;
    setServerConnected(false);
  }, delay);
}

function resumeServerConnection() {
  if (navigator.onLine !== false) state.serverBrowserOffline = false;
  state.serverResumeUntil = Date.now() + serverResumeGrace;
  clearServerFailureTimer();
  if (state.serverFailureId >= state.serverSuccessId && state.serverFailureId) scheduleServerUnreachable(state.serverFailureId);
  clearTimeout(state.serverResumeTimer);
  state.serverResumeTimer = setTimeout(() => {
    state.serverResumeTimer = null;
    if (!state.me || document.hidden) return;
    checkSession();
    scheduleEvents(0);
  }, 50);
}

async function api(path, options = {}) {
  const requestId = ++state.serverRequestId;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), options.timeout || 20000);
  const headers = new Headers(options.headers || {});
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  let response, data = {};
  try {
    response = await fetch(path, { ...options, headers, credentials: "same-origin", cache: "no-store", signal: controller.signal });
    // CODEX: Keep the deadline active until the full response is read; a partial acknowledgement is not a successful send.
    if (response.status !== 204) {
      try { data = await response.json(); }
      catch (error) {
        if (controller.signal.aborted || error instanceof TypeError) throw error;
        if (response.ok) throw new Error("The server returned an incomplete or invalid response. Please retry.");
      }
    }
  } catch (error) {
    // CODEX: A suspended tab can deliver an old timeout after a newer request already proved the server reachable.
    if (!response || controller.signal.aborted || error instanceof TypeError) {
      scheduleServerUnreachable(requestId);
      throw new Error(controller.signal.aborted || error.name === "AbortError" ? "The server did not respond. Your saved conversation is still available when it reconnects." : "Cannot reach the server. Reconnecting…");
    }
    markServerReachable(requestId);
    throw error;
  } finally { clearTimeout(timeout); }
  markServerReachable(requestId);
  if (!response.ok) {
    const detail = data.error?.message || data.error || data.detail || data.message;
    const error = new Error(typeof detail === "string" ? detail : `Request failed (${response.status}).`);
    error.status = response.status;
    error.readOnly = data.read_only === true;
    if (response.status === 401 && state.me) showLogin("Your browser session expired. Sign in again or use your recovery key.");
    throw error;
  }
  return data;
}

const connectorInfo = { clientVersion: "", reportedVersion: "", versionLive: false, linked: false, paused: false, activeRuns: 0, release: null, releaseReachable: null, downloading: false, privateFilename: "", publicOrigin: "", webVersion: "", executionMode: "", recordDevice: "", notifiedKey: "", dismissedKey: "", notification: null };

function connectorVersionParts(value) {
  if (!/^\d+(?:\.\d+)*$/.test(String(value || ""))) return null;
  return String(value).split(".").map(Number);
}

function connectorVersionIsCurrent(installed, available) {
  if (installed === available && !!installed) return true;
  const left = connectorVersionParts(installed), right = connectorVersionParts(available);
  if (!left || !right) return false;
  const length = Math.max(left.length, right.length);
  for (let index = 0; index < length; index += 1) {
    const difference = (left[index] || 0) - (right[index] || 0);
    if (difference) return difference > 0;
  }
  return true;
}

function loadConnectorRecord(deviceId) {
  const key = idOf(deviceId);
  if (connectorInfo.recordDevice === key) return;
  closeConnectorNotification();
  connectorInfo.recordDevice = key;
  connectorInfo.clientVersion = "";
  connectorInfo.reportedVersion = "";
  connectorInfo.versionLive = false;
  connectorInfo.linked = false;
  connectorInfo.release = null;
  connectorInfo.releaseReachable = null;
  connectorInfo.privateFilename = "";
  connectorInfo.notifiedKey = "";
  connectorInfo.dismissedKey = "";
  try { connectorInfo.dismissedKey = sessionStorage.getItem(`codex-lan-connector-dismissed:${key}`) || ""; } catch {}
  try {
    const record = JSON.parse(localStorage.getItem(`codex-lan-connector:${key}`) || "{}");
    if (typeof record.version === "string" && record.version.length <= 80) connectorInfo.clientVersion = record.version;
    if (connectorVersionParts(record.requiredVersion)) {
      const tag = encodeURIComponent(record.requiredVersion);
      connectorInfo.release = {
        available: true, version: record.requiredVersion,
        installer_url: `/api/client/download/${tag}/installer`,
        private_installer_url: `/api/client/installer/${tag}`,
        pairing_url: `/api/client/pairing/${tag}`,
      };
    }
  } catch {}
}

function saveConnectorRecord() {
  if (!connectorInfo.recordDevice) return;
  const record = {};
  if (connectorInfo.clientVersion) record.version = connectorInfo.clientVersion;
  if (connectorInfo.release?.available === true && connectorVersionParts(connectorInfo.release.version)) record.requiredVersion = connectorInfo.release.version;
  try { localStorage.setItem(`codex-lan-connector:${connectorInfo.recordDevice}`, JSON.stringify(record)); } catch {}
}

function acceptConnectorRelease(release) {
  if (release?.available === true && connectorVersionParts(release.version)) {
    connectorInfo.release = release;
    connectorInfo.releaseReachable = true;
    saveConnectorRecord();
    return true;
  }
  connectorInfo.releaseReachable = false;
  if (connectorInfo.release?.available !== true) connectorInfo.release = { available: false };
  return false;
}

function connectorReleasePath(kind) {
  const release = connectorInfo.release || {};
  const fallback = kind === "private" ? "/api/client/installer"
    : kind === "pairing" ? "/api/client/pairing" : "/api/client/download/installer";
  const candidate = kind === "private" ? release.private_installer_url
    : kind === "pairing" ? release.pairing_url : release.installer_url;
  try {
    const url = new URL(candidate || fallback, location.origin);
    if (url.origin !== location.origin || !url.pathname.startsWith("/api/client/")) return fallback;
    return `${url.pathname}${url.search}`;
  } catch { return fallback; }
}

function connectorReleaseFilename(kind = "installer") {
  const release = connectorInfo.release || {};
  const published = kind === "private" ? release.private_installer_filename
    : kind === "pairing" ? release.pairing_filename : release.installer_filename;
  if (typeof published === "string" && /^[A-Za-z0-9][A-Za-z0-9._-]{0,150}$/.test(published)) return published;
  const version = connectorVersionParts(release.version) ? release.version : "current";
  if (kind === "private") return `Install-Codex-Connector-${version}-private.sh`;
  if (kind === "pairing") return `codex-pairing-${version}.json`;
  return `Install-Codex-Connector-${version}.sh`;
}

function connectorRunCommand(kind = "installer") {
  return `bash ~/Downloads/${connectorReleaseFilename(kind)}`;
}

function freshPrivateInstallerFilename() {
  const published = connectorReleaseFilename("private");
  const suffix = String(Date.now());
  return published.endsWith(".sh") ? `${published.slice(0, -3)}-${suffix}.sh` : `${published}-${suffix}`;
}

function connectorUpdateState() {
  const release = connectorInfo.release;
  if (!state.me || isAdmin() || !["approved", "active"].includes(state.me.status) || release?.available !== true || typeof release.version !== "string" || !release.version) return { needed: false };
  const installed = connectorInfo.versionLive ? connectorInfo.reportedVersion : "";
  const centralized = connectorInfo.linked;
  const current = !!installed && connectorVersionIsCurrent(installed, release.version);
  if (installed && !current) {
    return { needed: true, mode: "update", installed, available: release.version, centralized,
      message: `Connector ${installed} is installed. Update to ${release.version}, then run the installer.` };
  }
  if (centralized) {
    if (current && state.workerOnline) return { needed: false, installed, available: release.version };
    return { needed: true, mode: "reconnect", installed: connectorInfo.reportedVersion || connectorInfo.clientVersion, available: release.version, centralized,
      message: `This computer was already linked to the server, but its Connector is offline. Reopen Codex Connector to verify it before sending new work.` };
  }
  return { needed: true, mode: "connect", installed, available: release.version, centralized,
    message: current
      ? `Connector ${installed} is running, but it is not linked to this device identity. Pair this computer to finish setup.`
      : "No Connector is linked to this device identity. Download it and pair this computer to enable local file and terminal access." };
}

function closeConnectorNotification() {
  try { connectorInfo.notification?.close(); } catch {}
  connectorInfo.notification = null;
}

function connectorNoticeKey(update) {
  return `${idOf(state.me?.id)}:${update.mode}:${update.installed || "unknown"}:${update.available}`;
}

function dismissConnectorNotice() {
  connectorInfo.dismissedKey = connectorNoticeKey(connectorUpdateState());
  try { sessionStorage.setItem(`codex-lan-connector-dismissed:${idOf(state.me?.id)}`, connectorInfo.dismissedKey); } catch {}
  closeConnectorNotification();
  $("connector-update-notice").hidden = true;
}

function renderConnectorUpdateNotice() {
  const update = connectorUpdateState();
  const notice = $("connector-update-notice");
  const notificationKey = connectorNoticeKey(update);
  notice.hidden = !update.needed || connectorInfo.dismissedKey === notificationKey;
  if (notice.hidden) { closeConnectorNotification(); return; }
  const title = update.mode === "update" ? "Codex Connector update required" : update.mode === "reconnect" ? "Reconnect Codex Connector" : "Connect Codex Connector";
  $("connector-update-title").textContent = title;
  $("connector-update-message").textContent = `${update.message} This page cannot start local commands for you.${connectorInfo.releaseReachable === false ? " The server download is temporarily unavailable; retry after it returns." : ""}`;
  $("connector-update-download").textContent = "Download update";
  $("connector-update-download").hidden = update.mode !== "update";
  $("connector-update-details").textContent = update.mode === "update" ? "Update instructions" : update.mode === "reconnect" ? "Repair connection" : "Set up connector";
  $("connector-update-details").classList.toggle("primary", update.mode !== "update");
  const installerUrl = connectorReleasePath("installer");
  const installerName = connectorReleaseFilename("installer");
  for (const id of ["connector-update-download", "connector-dialog-update-download"]) {
    const link = $(id);
    if (connectorInfo.releaseReachable === false) { link.removeAttribute("href"); link.setAttribute("aria-disabled", "true"); }
    else { link.href = installerUrl; link.download = installerName; link.removeAttribute("aria-disabled"); }
  }
  $("connector-update-command").textContent = connectorRunCommand();
  $("connector-dialog-update-command").textContent = connectorRunCommand();
  if (connectorInfo.notifiedKey === notificationKey) return;
  connectorInfo.notifiedKey = notificationKey;
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  try {
    closeConnectorNotification();
    connectorInfo.notification = new Notification(title, {
      body: update.mode === "update" ? `Download Connector ${update.available}, then run the installer from Downloads.`
        : update.mode === "reconnect" ? "Open Codex Connector so the server can verify it."
          : "Download the Connector and pair it with this device.",
      tag: `codex-connector-${update.available}`,
    });
    connectorInfo.notification.onclick = () => { window.focus(); openConnector(); };
  } catch {}
}

async function enrollComputer() {
  const name = $("enroll-name").value.trim();
  if (!name) { $("enroll-name").focus(); return; }
  $("enroll-submit").disabled = true;
  $("gate-status").classList.remove("error");
  $("gate-status").textContent = "Requesting approval for this computer…";
  try {
    await clearRoomStates();
    state.rooms = []; state.selected = null;
    // CODEX: Enrollment establishes an HttpOnly session; never copy its credential into browser state.
    await api("/api/enroll", { method: "POST", body: JSON.stringify({ name }) });
    state.me = null;
    $("enroll-name").value = "";
    await checkSession({ afterCurrent: true });
  } catch (error) {
    $("gate-status").textContent = error.message;
    $("gate-status").classList.add("error");
  } finally { $("enroll-submit").disabled = false; }
}

function renderConnectorVersion() {
  const release = connectorInfo.release;
  const installed = connectorInfo.versionLive ? connectorInfo.reportedVersion : "";
  const remembered = connectorInfo.clientVersion;
  const update = connectorUpdateState();
  const parts = [state.workerOnline ? "Connected to this computer" : connectorInfo.paused ? "Paused on this computer" : "Local connector is offline"];
  if (installed) parts.push(`Installed: ${installed}`);
  else if (connectorInfo.reportedVersion) parts.push(`Last confirmed by the server: ${connectorInfo.reportedVersion}`);
  else if (remembered) parts.push(`Last reported in this browser: ${remembered}`);
  else if (state.workerOnline) parts.push("Installed version not reported");
  if (release?.available && release.version) parts.push(!update.needed ? "Up to date" : `Current release: ${release.version}`);
  if (release?.available === false) parts.push("The server has not published an installer yet");
  if (connectorInfo.activeRuns) parts.push(`${connectorInfo.activeRuns} active ${connectorInfo.activeRuns === 1 ? "response" : "responses"}`);
  $("connector-version").textContent = parts.join(" · ");
  const execution = $("connector-execution");
  execution.hidden = !connectorInfo.centralizedExecution;
  execution.textContent = connectorInfo.executionMode === "server"
    ? "Codex runs on the host server. Only file and terminal tools run on this computer; no client OpenAI connection or proxy is needed."
    : connectorInfo.executionMode === "local" && isAdmin()
      ? "This is the host computer. Codex and its Internet connection run here."
      : "Pair Codex Connector with this device to use server-hosted Codex. Keep the host reachable on your local network; a client OpenAI proxy is not needed.";
  $("connector-already-ready").hidden = !state.workerOnline || update.needed;
  $("connector-update-help").hidden = update.mode !== "update";
  const setupMode = update.mode === "connect" || update.mode === "reconnect";
  $("connector-setup-steps").hidden = !setupMode;
  $("connector-pairing-card").hidden = release?.available !== true;
  $("connector-manual-pairing").hidden = isAdmin() || release?.available !== true;
  $("connector-private-title").textContent = update.mode === "reconnect" ? "Repair this computer’s link." : "Download your private setup.";
  $("connector-private-description").textContent = update.mode === "reconnect"
    ? "This fresh, one-time setup automatically replaces a deleted or stale Connector identity. It expires after 10 minutes."
    : "It contains a one-time pairing value for this approved computer. It expires after 10 minutes, so do not share it.";
  $("connector-private-installer").textContent = update.mode === "reconnect" ? "Download repair setup" : "Download one-step setup";
  $("connector-private-installer").disabled = connectorInfo.downloading || release?.available !== true || connectorInfo.releaseReachable === false;
  $("connector-server").textContent = `Server: ${connectorInfo.publicOrigin || location.origin}${connectorInfo.webVersion ? ` · Web version: ${connectorInfo.webVersion}` : ""}`;
  const installerUrl = connectorReleasePath("installer");
  const installerName = connectorReleaseFilename("installer");
  for (const link of [$("connector-installer"), $("connector-update-download"), $("connector-dialog-update-download")]) {
    if (release?.available === false || connectorInfo.releaseReachable === false) { link.removeAttribute("href"); link.setAttribute("aria-disabled", "true"); }
    else { link.href = installerUrl; link.download = installerName; link.removeAttribute("aria-disabled"); }
  }
  $("connector-update-command").textContent = connectorRunCommand();
  $("connector-dialog-update-command").textContent = connectorRunCommand();
  $("connector-private-command").textContent = connectorInfo.privateFilename
    ? `bash ~/Downloads/${connectorInfo.privateFilename}`
    : "Download the one-step setup to show its exact command.";
  $("connector-manual-command").textContent = `${connectorRunCommand()} --pair-file ~/Downloads/${connectorReleaseFilename("pairing")}`;
  for (const id of ["connector-pairing", "connector-pairing-primary"]) {
    $(id).textContent = `Download ${connectorReleaseFilename("pairing")}`;
    $(id).disabled = connectorInfo.downloading;
  }
}

async function openConnector({ route = true } = {}) {
  if (!["approved", "active"].includes(state.me?.status)) return;
  closeRouteDialogs("connector");
  $("connector-status").textContent = "";
  if (!$("connector-dialog").open) $("connector-dialog").showModal();
  if (route) persistVisibleRoute("push");
  renderConnectorVersion();
  const [release, health] = await Promise.allSettled([api("/api/client/release"), api("/api/health")]);
  if (release.status === "fulfilled") {
    const available = acceptConnectorRelease(release.value);
    if (!available && connectorInfo.release?.available === true) $("connector-status").textContent = "The published update is temporarily unavailable. This reminder stays visible until the current connector reconnects.";
  }
  else $("connector-status").textContent = release.reason.message;
  if (health.status === "fulfilled") {
    connectorInfo.publicOrigin = health.value.public_origin || "";
    connectorInfo.webVersion = health.value.version || "";
    connectorInfo.centralizedExecution = health.value.centralized_execution === true;
  }
  renderConnectorVersion(); renderConnectorUpdateNotice();
}

async function downloadPairing() {
  if (connectorInfo.downloading || !["approved", "active"].includes(state.me?.status)) return;
  connectorInfo.downloading = true;
  for (const id of ["connector-pairing", "connector-pairing-primary", "connector-private-installer"]) $(id).disabled = true;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 20000);
  try {
    const response = await fetch(connectorReleasePath("pairing"), { method: "POST", credentials: "same-origin", cache: "no-store", headers: { "Content-Type": "application/json" }, body: "{}", signal: controller.signal });
    if (!response.ok) {
      if ([401, 403].includes(response.status)) checkSession();
      throw new Error(`Could not create a pairing file (${response.status}). Check your device approval and try again.`);
    }
    // CODEX: Download the opaque one-time credential as a file, without parsing, logging, or displaying it.
    const objectUrl = URL.createObjectURL(await response.blob());
    const link = element("a"); link.href = objectUrl; link.download = connectorReleaseFilename("pairing");
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 1500);
    const message = "Pairing file downloaded. Keep it beside the installer; it expires in 10 minutes and can be used once.";
    $("connector-status").textContent = message; toast(message);
  } catch (error) {
    const message = error.name === "AbortError" ? "Pairing download timed out. Try again when the server is reachable." : error.message;
    $("connector-status").textContent = message; toast(message);
  } finally {
    clearTimeout(timeout); connectorInfo.downloading = false;
    for (const id of ["connector-pairing", "connector-pairing-primary", "connector-private-installer"]) $(id).disabled = false;
    renderConnectorVersion();
  }
}

async function downloadPrivateInstaller() {
  if (connectorInfo.downloading || !["approved", "active"].includes(state.me?.status)) return;
  connectorInfo.downloading = true;
  for (const id of ["connector-pairing", "connector-pairing-primary", "connector-private-installer"]) $(id).disabled = true;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 30000);
  try {
    const response = await fetch(connectorReleasePath("private"), {
      method: "POST", credentials: "same-origin", cache: "no-store",
      headers: { "Content-Type": "application/json" }, body: "{}", signal: controller.signal,
    });
    if (!response.ok) {
      if ([401, 403].includes(response.status)) checkSession();
      let detail = "";
      try { detail = (await response.json()).error || ""; } catch {}
      throw new Error(detail || `Could not create private setup (${response.status}). Try again when the server is reachable.`);
    }
    // CODEX: Keep the short-lived pairing value inside the downloaded file; never parse, display, log, or place it in a URL.
    const objectUrl = URL.createObjectURL(await response.blob());
    const privateName = freshPrivateInstallerFilename();
    connectorInfo.privateFilename = privateName;
    renderConnectorVersion();
    const link = element("a"); link.href = objectUrl; link.download = privateName;
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 1500);
    const message = `Private setup downloaded. Run bash ~/Downloads/${privateName} within 10 minutes. It also repairs stale or deleted links.`;
    $("connector-status").textContent = message; toast(message);
  } catch (error) {
    const message = error.name === "AbortError" ? "Private setup download timed out. Try again when the server is reachable." : error.message;
    $("connector-status").textContent = message; toast(message);
  } finally {
    clearTimeout(timeout); connectorInfo.downloading = false;
    for (const id of ["connector-pairing", "connector-pairing-primary", "connector-private-installer"]) $(id).disabled = false;
    renderConnectorVersion();
  }
}

function systemTheme() { return window.matchMedia?.("(prefers-color-scheme: dark)")?.matches ? "dark" : "light"; }

function setTheme(theme, persist = true) {
  const current = theme === "cosmic-live" ? "cosmic" : themes.includes(theme) ? theme : systemTheme();
  document.documentElement.dataset.theme = current;
  if (spaceThemes.has(current)) document.documentElement.dataset.themeFamily = "space";
  else delete document.documentElement.dataset.themeFamily;
  const selector = $("theme-select");
  if (selector) selector.value = current;
  const next = themes[(themes.indexOf(current) + 1) % themes.length];
  const label = themeLabels[next] || next[0].toUpperCase() + next.slice(1);
  $("theme-toggle").setAttribute("aria-label", `Switch to ${label} theme`);
  $("theme-toggle").title = `Switch to ${label} theme`;
  if (persist) {
    state.themeExplicit = true;
    try { localStorage.setItem("codex-lan-theme", current); } catch {}
  }
}

function updateConnection() {
  const offline = !state.connected || !state.workerOnline;
  const selectedCache = state.roomStates.get(state.selected);
  const activeRoomSnapshot = selectedCache?.loaded && !selectedCache.loading && activeRuns(selectedCache).length;
  const waiting = roomConnection(selectedRoom()) || (!activeRoomSnapshot ? waitingConnection(state.workerConnection) : null);
  const reconnecting = state.connected && !!waiting;
  const status = $("connection-status");
  status.classList.toggle("offline", offline);
  status.classList.toggle("server-offline", !state.connected);
  status.classList.toggle("worker-offline", state.connected && !state.workerOnline);
  status.classList.toggle("reconnecting", reconnecting);
  status.dataset.serverConnected = String(state.connected);
  status.dataset.workerReady = String(state.connected && state.workerOnline);
  $("connection-text").textContent = !state.connected ? "Reconnecting to server" : reconnecting ? connectionLabel(waiting, true) : !state.workerOnline ? "Server online · Worker offline" : "Worker ready";
  status.title = !state.connected
    ? "The browser cannot reach the server. Saved history will return when the connection recovers."
    : reconnecting ? `The model service is reconnecting. ${connectionLabel(waiting)}. Your response remains active.`
    : !state.workerOnline
      ? "The server is reachable, but your computer’s execution worker is offline. Saved chats are available."
      : "The server is reachable and your local worker is ready for new messages. This does not restart earlier failed responses.";
  status.setAttribute("aria-label", status.title);
  $("connection-banner").hidden = !state.me || (!offline && !reconnecting);
  $("connection-banner-text").textContent = !state.connected
    ? "Connection lost. Saved messages stay here; live responses will catch up when the server returns."
    : reconnecting ? `${connectionLabel(waiting)}. ${waiting.message || "Your response is waiting for the model service."}`
    : "The execution worker is offline. You can read saved chats; sending will be available when it reconnects.";
  $("connection-setup").hidden = !state.connected || state.workerOnline || !["approved", "active"].includes(state.me?.status);
  if (state.renderedServerConnection !== state.connected) {
    state.renderedServerConnection = state.connected;
    for (const panel of document.querySelectorAll(".run-activity")) if (panel._run) updateActivityPanel(panel, panel._run);
  }
  if (state.me && state.selected) updateComposer();
  if (state.me) renderMyUsage();
}

function cancelTimers() {
  for (const key of ["eventTimer", "roomTimer", "sessionTimer", "adminTimer", "serverFailureTimer", "serverResumeTimer"]) { clearTimeout(state[key]); state[key] = null; }
}

function showLogin(message = "", isError = !!message) {
  cancelTimers();
  clearRoomStates();
  state.me = null;
  state.myQuota = null;
  state.authExpired = true;
  state.routeReady = false;
  state.selectionVersion += 1;
  $("workspace").hidden = true;
  $("gate").hidden = false;
  $("password-login-form").hidden = false;
  $("gate-alternatives").hidden = false;
  $("login-form").hidden = false;
  $("enroll-form").hidden = false;
  $("enroll-details").hidden = false;
  $("advanced-login").hidden = false;
  closeRouteDialogs();
  $("switch-device").hidden = true;
  $("gate-title").textContent = "Connect to Codex";
  $("gate-description").textContent = "Sign in with your easy login, or use a recovery key or connect a new computer.";
  if (!$("password-login-name").value) $("password-login-name").value = rememberedBrowserLoginName();
  $("password-login-password").value = "";
  setPasswordVisibility($("password-login-show"), [$("password-login-password")], false);
  $("gate-status").textContent = message;
  $("gate-status").classList.toggle("error", isError);
  renderConnectorUpdateNotice();
  requestAnimationFrame(() => {
    if (!$("gate").hidden) ($("password-login-name").value ? $("password-login-password") : $("password-login-name")).focus({ preventScroll: true });
  });
}

function showDeviceStatus(device) {
  cancelTimers();
  $("workspace").hidden = true;
  $("gate").hidden = false;
  $("password-login-form").hidden = true;
  $("gate-alternatives").hidden = true;
  $("login-form").hidden = true;
  $("enroll-form").hidden = true;
  $("enroll-details").hidden = true;
  $("advanced-login").hidden = true;
  state.routeReady = false;
  closeRouteDialogs();
  state.myQuota = null;
  $("switch-device").hidden = false;
  const status = device.status || "pending";
  const name = device.name || "This device";
  $("gate-title").textContent = status === "pending" ? "Waiting for approval" : status === "rejected" ? "Enrollment not approved" : "Device access revoked";
  $("gate-description").textContent = status === "pending"
    ? `${name} is connected. Your server administrator needs to approve it before you can open rooms.`
    : status === "rejected"
      ? `${name} was not approved. Ask your server administrator to restore access, or use another invitation.`
      : `${name} no longer has access. Ask your server administrator to restore it, or use another invitation.`;
  $("gate-status").classList.remove("error");
  $("gate-status").textContent = status === "pending" && device.verification_code
    ? `Verification code: ${device.verification_code}. Your administrator can match this code before approving. This page updates automatically.`
    : "This page checks your access automatically.";
  renderConnectorUpdateNotice();
  state.sessionTimer = setTimeout(checkSession, 3000);
}

let sessionCheckPromise = null;

function startSessionCheck() {
  const request = runSessionCheck();
  sessionCheckPromise = request;
  const release = () => { if (sessionCheckPromise === request) sessionCheckPromise = null; };
  request.then(release, release);
  return request;
}

async function checkSession({ afterCurrent = false } = {}) {
  const current = sessionCheckPromise;
  if (!current) return startSessionCheck();
  if (!afterCurrent) return current;
  try { await current; } catch {}
  return sessionCheckPromise || startSessionCheck();
}

async function runSessionCheck() {
  clearTimeout(state.sessionTimer);
  state.sessionTimer = null;
  if (embeddedRoomId && embeddedPaused) return;
  try {
    const result = await api("/api/me");
    const device = sessionDevice(result);
    const first = !state.me || $("workspace").hidden;
    const changedIdentity = !!state.me && idOf(state.me.id) !== idOf(device.id);
    if (changedIdentity) {
      await clearRoomStates();
      state.rooms = []; state.selected = null; state.routeReady = false;
    }
    state.me = device;
    loadConnectorRecord(device.id);
    state.myQuota = result.quota || null;
    state.workerOnline = result.worker_online === true;
    state.workerConnection = result.connection || result.worker_connection || null;
    state.workerCwd = result.cwd || "";
    const reportedVersion = typeof result.client_version === "string" && result.client_version
      ? result.client_version : typeof result.connector_version === "string" ? result.connector_version : "";
    connectorInfo.reportedVersion = reportedVersion;
    connectorInfo.versionLive = Object.hasOwn(result, "client_version_live") ? result.client_version_live === true : !!reportedVersion;
    if (reportedVersion) { connectorInfo.clientVersion = reportedVersion; saveConnectorRecord(); }
    connectorInfo.executionMode = result.execution_mode || device.execution_mode || "";
    connectorInfo.linked = Object.hasOwn(result, "connector_linked")
      ? result.connector_linked === true : connectorInfo.executionMode === "server" || device.execution_mode === "server";
    if (result.connector_release && typeof result.connector_release === "object") acceptConnectorRelease(result.connector_release);
    connectorInfo.paused = result.worker_paused === true;
    connectorInfo.activeRuns = numeric(result.active_runs);
    state.authExpired = false;
    if (device.status !== "approved" && device.status !== "active") { showDeviceStatus(device); return; }
    $("gate").hidden = true;
    $("workspace").hidden = false;
    $("device-name").textContent = device.name || "This device";
    $("device-role").textContent = isAdmin() ? "Server administrator" : "Connected client";
    $("admin-open").hidden = !isAdmin();
    renderBrowserLoginSettings();
    renderEasySignInOffer();
    renderMyUsage();
    document.querySelector(".device-avatar").textContent = String(device.name || "D").slice(0, 1).toUpperCase();
    updateConnection();
    renderConnectorUpdateNotice();
    if ($("connector-dialog").open) renderConnectorVersion();
    if (first || changedIdentity) {
      await Promise.allSettled([refreshRooms(true), refreshModels()]);
      await applyLocationRoute();
      scheduleRooms();
    } else {
      if (!state.routeReady) await applyLocationRoute();
      if (!state.models.length && state.workerOnline) await refreshModels();
    }
    state.sessionTimer = setTimeout(checkSession, 10000);
  } catch (error) {
    if (error.status === 401 || error.status === 403) {
      if (state.me && error.status === 403) {
        const status = state.me.status === "pending" ? "rejected" : "revoked";
        showDeviceStatus({ ...state.me, status }); return;
      }
      showLogin(); return;
    }
    if (!state.me) {
      $("gate-status").textContent = error.message;
      $("gate-status").classList.add("error");
    }
    state.sessionTimer = setTimeout(checkSession, 3000);
  }
}

async function login(token) {
  $("login-submit").disabled = true;
  $("gate-status").classList.remove("error");
  $("gate-status").textContent = "Checking your recovery key…";
  try {
    await clearRoomStates();
    state.rooms = [];
    state.selected = null;
    const request = api("/api/session", { method: "POST", body: JSON.stringify({ token }) });
    $("login-token").value = "";
    await request;
    state.me = null;
    await checkSession({ afterCurrent: true });
  } catch (error) {
    $("gate-status").textContent = error.message;
    $("gate-status").classList.add("error");
  } finally {
    $("login-token").value = "";
    $("login-submit").disabled = false;
  }
}

async function loginWithPassword() {
  const loginName = $("password-login-name").value.trim();
  const password = $("password-login-password").value;
  const remember = $("password-login-remember").checked;
  if (!loginName) { $("password-login-name").focus(); return; }
  if (!password) { $("password-login-password").focus(); return; }
  $("password-login-submit").disabled = true;
  $("gate-status").classList.remove("error");
  $("gate-status").textContent = "Signing in…";
  try {
    await clearRoomStates();
    state.rooms = [];
    state.selected = null;
    const request = api("/api/session", { method: "POST", body: JSON.stringify({ login_name: loginName, password, remember }) });
    $("password-login-password").value = "";
    await request;
    rememberBrowserLoginName(loginName, remember);
    state.me = null;
    await checkSession({ afterCurrent: true });
  } catch (error) {
    $("gate-status").textContent = error.message;
    $("gate-status").classList.add("error");
    $("password-login-password").focus();
  } finally {
    $("password-login-password").value = "";
    $("password-login-submit").disabled = false;
  }
}

function renderBrowserLoginSettings() {
  const enabled = state.me?.password_login_enabled === true;
  const loginName = typeof state.me?.login_name === "string" ? state.me.login_name : "";
  $("browser-login-brief").textContent = enabled ? "On" : "Set up";
  $("browser-login-state").classList.toggle("enabled", enabled);
  $("browser-login-state-title").textContent = enabled ? "Easy sign-in is on" : "Not set up";
  $("browser-login-state-detail").textContent = enabled
    ? `${loginName || "This login"} can sign in from a browser with a password.`
    : "Use a login name and password instead of pasting a recovery key.";
  $("browser-login-disable").hidden = !enabled;
  $("browser-login-save").textContent = enabled ? "Change password" : "Set up easy sign-in";
}

function easySignInOfferKey() {
  return state.me?.id ? `${easySignInOfferPrefix}${idOf(state.me.id)}` : "";
}

function renderEasySignInOffer() {
  const key = easySignInOfferKey();
  let dismissed = false;
  try { dismissed = !!key && localStorage.getItem(key) === "dismissed"; } catch {}
  $("easy-signin-offer").hidden = !key || isAdmin() || state.me?.status !== "approved"
    || state.me?.password_login_enabled === true || dismissed;
}

function dismissEasySignInOffer() {
  const key = easySignInOfferKey();
  if (key) try { localStorage.setItem(key, "dismissed"); } catch {}
  renderEasySignInOffer();
}

function resetBrowserLoginDialog() {
  $("browser-login-password").value = "";
  $("browser-login-confirm").value = "";
  $("browser-login-status").textContent = "";
  $("browser-login-status").classList.remove("error");
  $("browser-login-disable-confirm").hidden = true;
  $("browser-login-disable").setAttribute("aria-expanded", "false");
  setPasswordVisibility($("browser-login-show"), [$("browser-login-password"), $("browser-login-confirm")], false);
  for (const id of ["browser-login-save", "browser-login-disable", "browser-login-disable-confirm-button", "browser-login-logout"]) $(id).disabled = false;
}

function openBrowserLogin({ route = true } = {}) {
  if (!state.me) return;
  closeRouteDialogs("signin");
  resetBrowserLoginDialog();
  renderBrowserLoginSettings();
  $("browser-login-name").value = state.me.login_name || rememberedBrowserLoginName() || state.me.name || "";
  closeSidebar();
  if (!$("browser-login-dialog").open) $("browser-login-dialog").showModal();
  if (route) persistVisibleRoute("push");
  (state.me.password_login_enabled ? $("browser-login-password") : $("browser-login-name")).focus({ preventScroll: true });
}

function closeBrowserLogin() {
  if ($("browser-login-dialog").open) $("browser-login-dialog").close();
  else resetBrowserLoginDialog();
}

async function saveBrowserLogin() {
  const loginName = $("browser-login-name").value.trim();
  const password = $("browser-login-password").value;
  const confirmation = $("browser-login-confirm").value;
  const status = $("browser-login-status");
  status.classList.remove("error");
  if (!loginName) { status.textContent = "Enter a login name."; status.classList.add("error"); $("browser-login-name").focus(); return; }
  if (!password) { status.textContent = "Enter a new password."; status.classList.add("error"); $("browser-login-password").focus(); return; }
  if (password !== confirmation) { status.textContent = "The passwords do not match."; status.classList.add("error"); $("browser-login-confirm").focus(); return; }
  $("browser-login-save").disabled = true;
  $("browser-login-disable").disabled = true;
  $("browser-login-logout").disabled = true;
  status.textContent = "Saving easy sign-in…";
  try {
    const request = api("/api/me/browser-login", { method: "PUT", body: JSON.stringify({ login_name: loginName, password, revoke_other_sessions: true }) });
    $("browser-login-password").value = "";
    $("browser-login-confirm").value = "";
    const result = await request;
    const returned = sessionDevice(result);
    state.me = { ...state.me, ...returned, login_name: returned.login_name || loginName, password_login_enabled: true };
    rememberBrowserLoginName(state.me.login_name);
    const offerKey = easySignInOfferKey();
    if (offerKey) try { localStorage.setItem(offerKey, "dismissed"); } catch {}
    renderBrowserLoginSettings();
    renderEasySignInOffer();
    closeBrowserLogin();
    toast("Easy sign-in saved. Other browser sessions were signed out.");
  } catch (error) {
    status.textContent = error.message;
    status.classList.add("error");
  } finally {
    $("browser-login-password").value = "";
    $("browser-login-confirm").value = "";
    $("browser-login-save").disabled = false;
    $("browser-login-disable").disabled = false;
    $("browser-login-logout").disabled = false;
  }
}

async function logoutBrowser() {
  const status = $("browser-login-status");
  for (const id of ["browser-login-save", "browser-login-disable", "browser-login-disable-confirm-button", "browser-login-logout"]) $(id).disabled = true;
  $("browser-login-password").value = "";
  $("browser-login-confirm").value = "";
  status.classList.remove("error");
  status.textContent = "Signing out…";
  try {
    await clearRoomStates();
    await api("/api/logout", { method: "POST" });
    closeBrowserLogin();
    showLogin("You are signed out. Sign in again whenever you are ready.", false);
  } catch (error) {
    status.textContent = error.message;
    status.classList.add("error");
  } finally {
    for (const id of ["browser-login-save", "browser-login-disable", "browser-login-disable-confirm-button", "browser-login-logout"]) $(id).disabled = false;
  }
}

async function disableBrowserLogin() {
  const status = $("browser-login-status");
  $("browser-login-disable-confirm-button").disabled = true;
  $("browser-login-save").disabled = true;
  $("browser-login-disable").disabled = true;
  $("browser-login-logout").disabled = true;
  status.classList.remove("error");
  status.textContent = "Disabling easy sign-in…";
  try {
    await clearRoomStates();
    await api("/api/me/browser-login", { method: "DELETE" });
    rememberBrowserLoginName("", false);
    closeBrowserLogin();
    $("password-login-name").value = "";
    showLogin("Easy sign-in is disabled. Use your recovery key to connect again or set up a new login later.", false);
    $("advanced-login").open = true;
    requestAnimationFrame(() => $("login-token").focus({ preventScroll: true }));
  } catch (error) {
    status.textContent = error.message;
    status.classList.add("error");
  } finally {
    $("browser-login-disable-confirm-button").disabled = false;
    $("browser-login-save").disabled = false;
    $("browser-login-disable").disabled = false;
    $("browser-login-logout").disabled = false;
  }
}

async function refreshModels() {
  try {
    const result = await api("/api/models");
    state.models = result.models || result.data || [];
    renderModelControls(); updateComposer();
  } catch (error) {
    if (state.workerOnline && error.status !== 401) $("composer-hint").textContent = error.message;
  }
}

function scheduleRooms() {
  clearTimeout(state.roomTimer);
  if (!state.me || $("workspace").hidden || embeddedPaused) return;
  state.roomTimer = setTimeout(async () => {
    try { await refreshRooms(); } catch {}
    scheduleRooms();
  }, 6500);
}

async function refreshRooms(initial = false) {
  if (embeddedPaused) return;
  const deviceId = state.me?.id;
  const result = await api("/api/rooms");
  if (state.me?.id !== deviceId) return;
  state.rooms = result.rooms || [];
  await observeRoomReplies(state.rooms, initial);
  if (state.me?.id !== deviceId) return;
  const accessible = new Set(state.rooms.map((room) => idOf(room.id)));
  for (const [id, cache] of state.roomStates) {
    if (accessible.has(id)) continue;
    releaseRoomState(cache);
    state.roomStates.delete(id);
  }
  pruneRoomStates();
  renderRooms();
  if (!selectedRoom()) {
    const requestedRoute = locationRoute();
    state.selected = null;
    state.selectionVersion += 1;
    clearTimeout(state.eventTimer);
    if (state.view === "admin") { renderHeader(); return; }
    const room = rememberedRoom();
    if (requestedRoute.room && !accessible.has(requestedRoute.room)) {
      replaceAppRoute({ ...requestedRoute, room: room?.id || "" });
    }
    if (room) await selectRoom(room.id, { route: false });
    else { rememberRoom(null); renderCurrent(); }
  } else { renderHeader(); updateComposer(); }
}

function renderRooms() {
  const preferences = sidebarPreferences();
  let changed = false;
  for (const room of state.rooms) {
    if (ownRoom(room) && !Object.hasOwn(preferences.seen, idOf(room.id))) {
      // CODEX: Existing history is the first-visit baseline, not hundreds of new notifications.
      preferences.seen[idOf(room.id)] = latestRoomReply(room); changed = true;
    }
  }
  if (markVisibleRoomRead()) changed = true;
  if (changed) saveSidebarPreferences();
  const pins = new Set(preferences.pins);
  const filter = $("room-search").value.trim().toLocaleLowerCase();
  const visible = state.rooms.filter((room) => !room.companion_kind && `${room.title || "New chat"} ${room.owner_name || room.device_name || ""}`.toLocaleLowerCase().includes(filter))
    .sort((a, b) => {
      const aOwn = ownRoom(a), bOwn = ownRoom(b);
      const aPinned = pins.has(idOf(a.id)), bPinned = pins.has(idOf(b.id));
      // CODEX: Ownership is the hard sidebar boundary; pins only reorder rooms inside their owner's section.
      return Number(bOwn) - Number(aOwn)
        || Number(bPinned) - Number(aPinned)
        || (aPinned ? timestamp(a.created_at) - timestamp(b.created_at)
          : Math.max(timestamp(b.last_message_at), timestamp(b.created_at)) - Math.max(timestamp(a.last_message_at), timestamp(a.created_at)))
        || (aPinned ? 0 : numeric(b.last_message_sequence) - numeric(a.last_message_sequence))
        || idOf(a.id).localeCompare(idOf(b.id));
    });
  $("room-count").textContent = count(state.rooms.filter((room) => !room.companion_kind).length);
  const list = $("room-list");
  const oldRows = new Map([...list.querySelectorAll(".room-row")].map((row) => [row.dataset.roomId, row]));
  const oldLabels = new Map([...list.querySelectorAll(".room-group-label")].map((label) => [label.dataset.roomGroup, label]));
  const desired = [];
  let currentGroup = "";
  let currentOwnerScope = "";
  for (const room of visible) {
    const key = idOf(room.id);
    const active = state.selected === idOf(room.id) && state.view === "chat";
    const running = roomIsRunning(room);
    const connection = running ? roomConnection(room) : null;
    const unread = roomHasUnread(room);
    const pinned = pins.has(key);
    const editable = writableRoom(room);
    const ownerScope = ownRoom(room) ? "own" : "other";
    const group = ownerScope === "own" ? `own-${pinned ? "pinned" : "recent"}` : "other-recent";
    if (group !== currentGroup) {
      const label = oldLabels.get(group) || element("div", "room-group-label");
      label.dataset.roomGroup = group;
      label.dataset.roomOwner = ownerScope;
      label.setAttribute("role", "heading");
      label.setAttribute("aria-level", "3");
      label.classList.toggle("room-owner-boundary", !!currentOwnerScope && ownerScope !== currentOwnerScope);
      label.textContent = ({
        "own-pinned": "Your pinned",
        "own-recent": "Your rooms",
        "other-recent": "Other rooms",
      })[group];
      desired.push(label);
      currentGroup = group;
      currentOwnerScope = ownerScope;
    }
    let row = oldRows.get(key);
    const retainedRow = !!row && row.dataset.editable === String(editable);
    const wasUnread = retainedRow && row.classList.contains("unread");
    if (!retainedRow) {
      row = element("div", "room-row");
      row.dataset.roomId = key; row.dataset.editable = String(editable);
      const button = element("button", "room-item"); button.type = "button";
      const heading = element("span", "room-item-heading");
      const indicator = element("span", "running-dot"); indicator.title = "Response in progress"; indicator.setAttribute("aria-hidden", "true");
      const unreadDot = element("span", "unread-dot"); unreadDot.title = "New response"; unreadDot.setAttribute("aria-hidden", "true");
      heading.append(element("span", "room-item-title"), indicator, unreadDot);
      button.append(heading, element("span", "room-item-meta"));
      button.addEventListener("click", () => { selectRoom(key).catch((error) => toast(error.message)); closeSidebar(); });
      row.append(button);
      const actions = element("div", "room-actions");
      const pin = element("button", "icon-button room-pin"); pin.type = "button";
      pin.append(roomIcon("M9 3h6l-1 6 4 4v2H6v-2l4-4-1-6ZM12 15v6"));
      pin.addEventListener("click", () => toggleRoomPin(key));
      if (editable) {
        const settings = element("button", "icon-button room-settings", "•••"); settings.type = "button";
        settings.addEventListener("click", () => openRoomDialog("edit", "", key));
        const remove = element("button", "icon-button room-delete"); remove.type = "button";
        remove.append(roomIcon("M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v5M14 11v5"));
        remove.addEventListener("click", () => deleteRoom(key, remove));
        actions.append(settings, remove);
      }
      actions.append(pin);
      row.append(actions);
    }
    row.className = `room-row ${ownerScope}${active ? " active" : ""}${pinned ? " pinned" : ""}${unread ? " unread" : ""}`;
    row.dataset.ownerScope = ownerScope;
    const button = row.querySelector(".room-item");
    button.classList.toggle("active", active);
    button.setAttribute("aria-current", active ? "page" : "false");
    const ownerLabel = ownerScope === "other" ? room.owner_name || room.device_name || `Device ${room.owner_id}` : "";
    button.setAttribute("aria-label", [room.title || "New chat", ownerLabel, room.read_only ? "Read-only" : "", pinned ? "Pinned" : "", connection ? connectionLabel(connection, true) : running ? "Response in progress" : "", unread ? "New response" : ""].filter(Boolean).join(" · "));
    const title = row.querySelector(".room-item-title");
    title.textContent = room.title || "New chat"; title.dir = direction(room.title);
    row.querySelector(".running-dot").hidden = !running;
    const unreadDot = row.querySelector(".unread-dot");
    unreadDot.hidden = !unread;
    if (retainedRow && !wasUnread && unread) queueMicrotask(() => window.CodexControls?.unreadFeedback(unreadDot));
    row.classList.toggle("reconnecting", !!connection);
    row.querySelector(".running-dot").title = connection ? connectionLabel(connection) : "Response in progress";
    const meta = row.querySelector(".room-item-meta");
    const labels = [];
    if (room.read_only) labels.push("Read-only");
    if (connection) labels.push(connectionLabel(connection, true));
    if (room.parent_room_id) labels.push("↳ Side thread");
    if (isAdmin() && ownerScope === "other") labels.push(ownerLabel);
    else if (room.model && !connection) labels.push(matchingModel(room.model)?.displayName || room.model);
    const metaText = labels.join(" · "); if (meta.textContent !== metaText) meta.textContent = metaText;
    meta.hidden = !metaText;
    const pin = row.querySelector(".room-pin");
    pin.setAttribute("aria-label", `${pinned ? "Unpin" : "Pin"} ${room.title || "New chat"}`);
    pin.setAttribute("aria-pressed", String(pinned)); pin.title = pinned ? "Unpin room" : "Pin room";
    if (editable) {
      const settings = row.querySelector(".room-settings");
      settings.setAttribute("aria-label", `Settings for ${room.title || "New chat"}`); settings.title = "Room settings";
      const remove = row.querySelector(".room-delete");
      remove.setAttribute("aria-label", `Delete ${room.title || "New chat"}`);
      remove.title = running ? "Stop the active response before deleting this room" : "Delete room";
      remove.disabled = running;
    }
    desired.push(row);
  }
  if (!visible.length) desired.push(element("p", "room-empty", filter ? "No matching room titles." : "Your rooms will appear here."));
  // CODEX: Keep unchanged row nodes mounted so polling cannot reset animation, focus, or pointer targets.
  let cursor = list.firstChild;
  for (const node of desired) {
    if (node === cursor) cursor = cursor.nextSibling;
    else list.insertBefore(node, cursor);
  }
  const keep = new Set(desired);
  for (const node of [...list.childNodes]) if (!keep.has(node)) node.remove();
}

// CODEX: The quick switcher keeps navigation local to the loaded workspace and never needs another server request.
let quickSwitcherItems = [];
let quickSwitcherIndex = 0;
let quickSwitcherReturnFocus = null;

function quickSwitcherActions() {
  const room = selectedRoom() || rememberedRoom();
  const actions = [
    { id: "new-room", label: "New room", detail: "Start with your current workspace", symbol: "＋", keywords: "new create start chat room", run: createRoomNow },
    room && writableRoom(room) ? { id: "composer", label: "Focus message", detail: room.title || "Current room", symbol: "✎", keywords: "message prompt compose focus", run: async () => {
      if (state.view !== "chat" || state.selected !== idOf(room.id)) await selectRoom(room.id);
      requestAnimationFrame(() => $("message-input").focus({ preventScroll: true }));
    } } : null,
    { id: "usage", label: "My usage", detail: "Open your weekly usage meter", symbol: "◔", keywords: "usage quota allowance tokens", run: openMyUsage },
    { id: "connector", label: "Local connector", detail: "Connection, setup and update status", symbol: "⇄", keywords: "connector worker connection update install", run: openConnector },
    "Notification" in window && window.isSecureContext ? { id: "notifications", label: "Reply notifications", detail: "Enable desktop alerts when you are outside the room", symbol: "♧", keywords: "notifications alerts replies permission", run: enableReplyNotifications } : null,
    isAdmin() ? { id: "admin", label: "Administration", detail: "Devices, limits and shared usage", symbol: "⌘", keywords: "admin devices limits users usage", run: openAdmin } : null,
    isAdmin() ? { id: "host-setup", label: "Host setup", detail: "Network, startup and host services", symbol: "⇄", keywords: "host setup server linux network https startup sleep", run: () => openAdmin({ panel: "host-setup" }) } : null,
  ];
  return actions.filter(Boolean);
}

function quickSwitcherRoomEntries(query) {
  const needle = query.trim().toLocaleLowerCase();
  const rooms = state.rooms.filter((room) => !room.companion_kind && [
    room.title || "New chat",
    room.owner_name || room.device_name || "",
    room.model || "",
  ].join(" ").toLocaleLowerCase().includes(needle));
  rooms.sort((left, right) => Number(idOf(right.id) === state.selected) - Number(idOf(left.id) === state.selected)
    || Number(ownRoom(right)) - Number(ownRoom(left))
    || timestamp(right.updated_at || right.created_at) - timestamp(left.updated_at || left.created_at)
    || idOf(left.id).localeCompare(idOf(right.id)));
  return rooms.slice(0, needle ? 20 : 8).map((room) => {
    const own = ownRoom(room);
    const model = matchingModel(room.model)?.displayName || matchingModel(room.model)?.name || room.model || "Model not recorded";
    const owner = room.owner_name || room.device_name || "Another device";
    const current = idOf(room.id) === state.selected && state.view === "chat";
    return {
      id: `room-${idOf(room.id)}`,
      label: room.title || "New chat",
      detail: `${own ? "Your room" : owner} · ${model}`,
      badge: current ? "Current" : roomIsRunning(room) ? "Running" : own ? "" : "Read only",
      symbol: room.parent_room_id ? "↳" : own ? "◇" : "◎",
      run: async () => {
        await selectRoom(room.id);
        closeSidebar();
        const target = writableRoom(room) ? $("message-input") : $("timeline-scroll");
        target.focus({ preventScroll: true });
      },
    };
  });
}

function quickSwitcherButton(entry, index) {
  const button = element("button", "quick-switcher-item");
  button.type = "button";
  button.id = `quick-switcher-option-${index}`;
  button.dataset.index = String(index);
  button.setAttribute("role", "option");
  button.setAttribute("aria-selected", String(index === quickSwitcherIndex));
  button.tabIndex = -1;
  const symbol = element("span", "quick-switcher-item-symbol", entry.symbol);
  symbol.setAttribute("aria-hidden", "true");
  const copy = element("span", "quick-switcher-item-copy");
  const title = element("strong", "", entry.label); title.dir = direction(entry.label);
  const detail = element("small", "", entry.detail); detail.dir = direction(entry.detail);
  copy.append(title, detail);
  button.append(symbol, copy);
  if (entry.badge) button.append(element("span", "quick-switcher-item-badge", entry.badge));
  button.classList.toggle("active", index === quickSwitcherIndex);
  button.addEventListener("mouseenter", () => setQuickSwitcherIndex(index));
  button.addEventListener("click", () => activateQuickSwitcher(index));
  return button;
}

function renderQuickSwitcher({ reset = false } = {}) {
  const query = $("quick-switcher-input").value.trim();
  const needle = query.toLocaleLowerCase();
  const roomEntries = quickSwitcherRoomEntries(query);
  const actionEntries = quickSwitcherActions().filter((entry) => !needle || `${entry.label} ${entry.detail} ${entry.keywords}`.toLocaleLowerCase().includes(needle));
  quickSwitcherItems = [...roomEntries, ...actionEntries];
  if (reset || quickSwitcherIndex >= quickSwitcherItems.length) quickSwitcherIndex = 0;
  const results = $("quick-switcher-results");
  results.replaceChildren();
  let index = 0;
  for (const [label, entries] of [["Rooms", roomEntries], ["Actions", actionEntries]]) {
    if (!entries.length) continue;
    const heading = element("div", "quick-switcher-group", label);
    heading.setAttribute("role", "presentation");
    results.append(heading);
    for (const entry of entries) results.append(quickSwitcherButton(entry, index++));
  }
  if (!quickSwitcherItems.length) results.append(element("p", "quick-switcher-empty", `No room or action matches “${query}”.`));
  const active = quickSwitcherItems.length ? `quick-switcher-option-${quickSwitcherIndex}` : "";
  if (active) $("quick-switcher-input").setAttribute("aria-activedescendant", active);
  else $("quick-switcher-input").removeAttribute("aria-activedescendant");
}

function setQuickSwitcherIndex(index) {
  if (!quickSwitcherItems.length) return;
  quickSwitcherIndex = (index + quickSwitcherItems.length) % quickSwitcherItems.length;
  for (const button of $("quick-switcher-results").querySelectorAll(".quick-switcher-item")) {
    const active = Number(button.dataset.index) === quickSwitcherIndex;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
    if (active) button.scrollIntoView({ block: "nearest" });
  }
  $("quick-switcher-input").setAttribute("aria-activedescendant", `quick-switcher-option-${quickSwitcherIndex}`);
}

function activateQuickSwitcher(index = quickSwitcherIndex) {
  const entry = quickSwitcherItems[index];
  if (!entry) return;
  quickSwitcherReturnFocus = null;
  $("quick-switcher").close();
  Promise.resolve().then(entry.run).catch((error) => toast(error.message));
}

function openQuickSwitcher() {
  if (embeddedRoomId || !state.me || $("workspace").hidden) return false;
  const dialog = $("quick-switcher");
  if (dialog.open) { dialog.close(); return true; }
  if (document.querySelector("dialog[open]")) return false;
  quickSwitcherReturnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  $("quick-switcher-input").value = "";
  quickSwitcherIndex = 0;
  renderQuickSwitcher({ reset: true });
  dialog.showModal();
  requestAnimationFrame(() => $("quick-switcher-input").focus({ preventScroll: true }));
  return true;
}

function handleQuickSwitcherKey(event) {
  if (event.defaultPrevented || event.isComposing || event.repeat) return;
  if ((event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey && event.key.toLocaleLowerCase() === "k") {
    if ($("quick-switcher").open || !document.querySelector("dialog[open]")) {
      event.preventDefault();
      openQuickSwitcher();
    }
    return;
  }
  if (!$("quick-switcher").open) return;
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    setQuickSwitcherIndex(quickSwitcherIndex + (event.key === "ArrowDown" ? 1 : -1));
    $("quick-switcher-input").focus({ preventScroll: true });
  } else if (event.key === "Enter") {
    event.preventDefault();
    activateQuickSwitcher();
  }
}

function roomIsRunning(room) {
  const cache = state.roomStates.get(idOf(room?.id));
  if (idOf(room?.id) === state.selected && cache?.loaded && !cache.loading) return activeRuns(cache).length > 0;
  if (room?.status === "idle") return false;
  if (room?.status === "running") return true;
  return !!(cache && activeRuns(cache).length || room?.active_run_count || room?.active_runs?.length || room?.is_running || ["queued", "running"].includes(room?.run_status));
}

function renderHeader() {
  $("speed-select").closest(".speed-field").hidden = state.view === "admin";
  if (state.view === "admin") {
    $("room-title").textContent = "Administration";
    $("room-title").dir = "ltr";
    $("room-subtitle").textContent = "Manage access to your shared workspace";
    $("room-menu").disabled = true;
    return;
  }
  const room = selectedRoom();
  $("room-title").textContent = room?.title || "Your workspace";
  $("room-title").dir = direction(room?.title);
  $("room-subtitle").textContent = room ? (room.read_only ? "Earlier room · History preserved · Read-only" : ownRoom(room) ? (room.parent_room_id ? "Your private side thread" : "Your room · History saved on the server") : `Read-only · ${room.owner_name || room.device_name || "Another device"}`) : "A place for your next idea.";
  $("room-menu").disabled = !writableRoom(room);
  $("workspace-path").textContent = room?.cwd || "";
  $("workspace-path").title = room?.cwd || "";
  document.title = room ? `${room.title || "New chat"} · Codex` : "Codex · Shared workspace";
}

async function selectRoom(id, { route = true } = {}) {
  if (embeddedRoomId && idOf(id) !== embeddedRoomId) return;
  state.lastEnter = null;
  state.selectionRefreshPending = false;
  const focusRevision = state.focusRevision;
  const key = idOf(id);
  if (!state.rooms.some((room) => idOf(room.id) === key)) return;
  rememberRoom(key);
  closeRouteDialogs();
  if (state.selected === key && state.view === "chat" && roomState(key).loaded) {
    if (route) persistVisibleRoute("push");
    return;
  }
  saveRoomViewport();
  clearTimeout(state.eventTimer);
  clearTimeout(state.adminTimer);
  state.selected = key;
  state.view = "chat";
  publishReplyView();
  if (route) persistVisibleRoute("push");
  const version = ++state.selectionVersion;
  const cache = roomState(key);
  // CODEX: History navigation can switch rooms without blurring the composer; restore its matching draft immediately.
  $("message-input").value = cache.draft;
  const loadVersion = cache.loadVersion = numeric(cache.loadVersion) + 1;
  const cached = cache.loaded;
  state.follow = cache.follow !== false;
  cache.loading = !cached;
  $("admin-view").hidden = true;
  $("chat-view").hidden = false;
  if (!cached) $("timeline").replaceChildren();
  renderCurrent();
  if (cached) { restoreRoomViewport(cache); window.CodexControls?.enterRoom(); scheduleEvents(0); }
  try {
    const result = await api(`/api/rooms/${encodeURIComponent(key)}/messages`);
    if (cache.loadVersion !== loadVersion) return;
    if (result.room) mergeRoom(result.room);
    if (cached) {
      for (const message of result.messages || []) mergeMessage(cache, message);
    } else cache.messages = result.messages || [];
    cache.more = result.has_more ?? result.hasMore ?? cache.messages.length >= 100;
    if (result.event_cursor !== undefined) { cache.cursor = numeric(result.event_cursor); cache.runs.clear(); applySnapshots(cache, result.runs || []); }
    cache.loaded = true;
    cache.loading = false;
    if (version !== state.selectionVersion) return;
    await fetchEvents(key, loadVersion);
    if (version !== state.selectionVersion) return;
    renderCurrent(!cached);
    restoreRoomViewport(cache);
    if (!cached) window.CodexControls?.enterRoom();
    scheduleEvents();
    focusComposerAfterAsync(key, focusRevision);
  } catch (error) {
    if (cache.loadVersion !== loadVersion) return;
    cache.loading = false;
    if (version === state.selectionVersion) { renderCurrent(); toast(error.message); scheduleEvents(); }
  }
}

function focusComposerAfterAsync(roomId, focusRevision) {
  if (state.focusRevision !== focusRevision || state.selected !== idOf(roomId) || state.view !== "chat" || document.hidden || document.querySelector("dialog[open]")) return;
  if (writableRoom(selectedRoom()) && !roomState(roomId).loading) $("message-input").focus({ preventScroll: true });
}

function mergeRoom(room) {
  const index = state.rooms.findIndex((item) => idOf(item.id) === idOf(room.id));
  if (index >= 0) state.rooms[index] = { ...state.rooms[index], ...room };
  else state.rooms.unshift(room);
}

function mergeMessage(cache, message) {
  if (!message || message.id === undefined || message.id === null) return;
  const index = cache.messages.findIndex((item) => idOf(item.id) === idOf(message.id));
  if (index < 0) cache.messages.push(message);
  else cache.messages[index] = { ...cache.messages[index], ...message };
  cache.messages.sort((a, b) => timestamp(a.created_at) - timestamp(b.created_at));
}

function playSendFeedback() {
  const button = $("send-button"), composer = $("composer");
  clearTimeout(sendFeedbackTimer);
  button.classList.remove("send-confirmed"); composer.classList.remove("send-confirmed");
  void composer.offsetWidth;
  button.classList.add("send-confirmed"); composer.classList.add("send-confirmed");
  sendFeedbackTimer = setTimeout(() => {
    button.classList.remove("send-confirmed"); composer.classList.remove("send-confirmed");
  }, 3200);
}

function mergeContextOverview(previous, incoming) {
  const before = previous && typeof previous === "object" && !Array.isArray(previous) ? previous : null;
  const next = incoming && typeof incoming === "object" && !Array.isArray(incoming) ? incoming : null;
  if (!next) return before;
  if (!before) return next;
  const merged = { ...before, ...next };
  for (const key of ["thread", "input", "instructions", "turn", "limits", "compaction", "usage", "token_usage"]) {
    if (before[key] && typeof before[key] === "object" && next[key] && typeof next[key] === "object") merged[key] = { ...before[key], ...next[key] };
  }
  if (!Array.isArray(next.content) && Array.isArray(before.content)) merged.content = before.content;
  return merged;
}

function applySnapshots(cache, snapshots, recoverMessages = false) {
  for (const snapshot of snapshots) {
    const key = idOf(snapshot.id ?? snapshot.run_id);
    if (!key) continue;
    const known = cache.runs.has(key);
    const previous = cache.runs.get(key) || { id: key, output_text: "", status: "running", last_event_id: 0 };
    const cursor = numeric(snapshot.last_event_id);
    if (cursor >= numeric(previous.last_event_id) || !activeRun(snapshot)) {
      const next = { ...previous, ...snapshot, id: key, output_text: snapshot.output_text ?? previous.output_text, last_event_id: Math.max(cursor, numeric(previous.last_event_id)), terminal_snapshot: !activeRun(snapshot) };
      next.context_overview = mergeContextOverview(previous.context_overview, snapshot.context_overview);
      cache.runs.set(key, next);
    }
    if (snapshot.message) mergeMessage(cache, snapshot.message);
    if (recoverMessages && activeRun(snapshot) && snapshot.user_message_id && !cache.messages.some((message) => idOf(message.id) === idOf(snapshot.user_message_id))) cache.refreshMessages = true;
    if (recoverMessages && known && activeRun(previous) && !activeRun(snapshot)) cache.refreshMessages = true;
  }
}

async function fetchEvents(roomId, loadVersion = null) {
  const cache = roomState(roomId);
  const previouslyActive = activeRuns(cache).length > 0;
  const result = await api(`/api/rooms/${encodeURIComponent(roomId)}/events?after=${encodeURIComponent(cache.cursor)}`);
  if (loadVersion !== null && cache.loadVersion !== loadVersion) return 0;
  applySnapshots(cache, result.runs || [], true);
  for (const event of result.events || []) {
    cache.cursor = Math.max(cache.cursor, numeric(event.id));
    const key = idOf(event.run_id ?? event.payload?.run_id ?? event.payload?.run?.id);
    const payload = event.payload || {};
    if (!key) continue;
    const run = cache.runs.get(key) || { id: key, output_text: "", status: "running", last_event_id: 0 };
    // CODEX: Follow-up messages are durable transcript rows, even when a newer snapshot covers their event.
    if (event.kind === "steered" && payload.user_message) mergeMessage(cache, payload.user_message);
    if (event.kind === "completed" && payload.message && (!cache.more || !cache.messages.length || timestamp(payload.message.created_at) >= timestamp(cache.messages[0].created_at))) mergeMessage(cache, payload.message);
    if (event.kind === "usage" && numeric(event.id) > numeric(run.usage_event_id)) { run.usage = payload; run.usage_event_id = numeric(event.id); }
    if (event.kind === "activity_summary" && Array.isArray(payload.progress_log)) {
      run.progress_log = payload.progress_log;
      run.progress_log_count = payload.progress_log_count;
      run.progress_log_truncated = payload.progress_log_truncated;
    }
    if (run.terminal_snapshot) continue;
    if (numeric(event.id) <= numeric(run.last_event_id)) continue;
    const previousContext = run.context_overview;
    const eventContext = event.kind === "started" ? payload.run?.context_overview ?? payload.context_overview : payload.context_overview;
    if (event.kind === "started") Object.assign(run, payload.run || {}, { status: payload.status || "running" });
    else if (event.kind === "delta") {
      run.output_text = (run.output_text || "") + String(payload.text || "");
      if (typeof payload.latest_status_line === "string") run.latest_status_line = payload.latest_status_line;
    }
    else if (event.kind === "activity") {
      if (payload.text) run.activity = payload.text;
      if (typeof payload.latest_status_line === "string") run.latest_status_line = payload.latest_status_line;
      if (payload.entry?.id) {
        const entries = Array.isArray(run.activities) ? [...run.activities] : [];
        const index = entries.findIndex((entry) => idOf(entry.id) === idOf(payload.entry.id));
        if (index < 0) entries.push(payload.entry); else entries[index] = payload.entry;
        run.activities = entries;
      }
    }
    else if (event.kind === "progress") {
      const entries = new Map((Array.isArray(run.progress_log) ? run.progress_log : []).map((entry) => [idOf(entry.id), entry]));
      for (const removed of Array.isArray(payload.progress_removed_ids) ? payload.progress_removed_ids : []) entries.delete(idOf(removed));
      if (payload.progress_entry?.id) entries.set(idOf(payload.progress_entry.id), payload.progress_entry);
      run.progress_log = Array.isArray(payload.progress_log) ? payload.progress_log : [...entries.values()];
      run.progress_log_count = payload.progress_log_count ?? run.progress_log_count;
      run.progress_log_truncated = payload.progress_log_truncated ?? run.progress_log_truncated;
    }
    else if (event.kind === "usage") run.usage = payload;
    else if (event.kind === "status") { Object.assign(run, payload); if (payload.text) run.activity = payload.text; }
    else if (event.kind === "connection") { run.connection = payload.connection || payload; run.retrying = !!waitingConnection(run.connection); }
    else if (event.kind === "completed") { run.status = payload.status || "completed"; if (payload.error) run.error = payload.error; if (!payload.message) cache.refreshMessages = true; }
    else if (event.kind === "error") { run.error = payload.message || "The response could not be completed."; if (payload.status) run.status = payload.status; }
    run.context_overview = mergeContextOverview(previousContext, eventContext ?? run.context_overview);
    run.last_event_id = numeric(event.id);
    cache.runs.set(key, run);
  }
  if (result.room) mergeRoom(result.room);
  if (cache.refreshMessages) {
    const messages = await api(`/api/rooms/${encodeURIComponent(roomId)}/messages`);
    if (loadVersion !== null && cache.loadVersion !== loadVersion) return 0;
    for (const message of messages.messages || []) mergeMessage(cache, message);
    cache.refreshMessages = false;
  }
  if (previouslyActive && !activeRuns(cache).length && [...cache.runs.values()].some((run) => run.error || ["failed", "interrupted"].includes(run.status))) checkSession();
  return (result.events || []).length;
}

function scheduleEvents(delay) {
  clearTimeout(state.eventTimer);
  if (!state.me || !state.selected || state.view !== "chat" || $("workspace").hidden || embeddedPaused) return;
  const key = state.selected;
  const selectionVersion = state.selectionVersion;
  const loadVersion = roomState(key).loadVersion;
  state.eventTimer = setTimeout(async () => {
    if (embeddedPaused || state.selected !== key || state.view !== "chat" || state.selectionVersion !== selectionVersion || roomState(key).loadVersion !== loadVersion) return;
    try {
      const cache = roomState(key);
      if (!cache.loaded) {
        const result = await api(`/api/rooms/${encodeURIComponent(key)}/messages`);
        if (state.selected !== key || state.view !== "chat" || state.selectionVersion !== selectionVersion || cache.loadVersion !== loadVersion) return;
        cache.messages = result.messages || [];
        cache.more = result.has_more ?? cache.messages.length >= 100;
        if (result.event_cursor !== undefined) { cache.cursor = numeric(result.event_cursor); cache.runs.clear(); applySnapshots(cache, result.runs || []); }
        cache.loaded = true;
      }
      const countEvents = await fetchEvents(key, loadVersion);
      if (state.selected === key && state.view === "chat" && state.selectionVersion === selectionVersion && cache.loadVersion === loadVersion) { renderCurrent(); scheduleEvents(countEvents >= 200 ? 60 : undefined); }
    } catch (error) {
      if (error.status === 403) checkSession();
      if (state.selected === key && state.view === "chat" && state.selectionVersion === selectionVersion && roomState(key).loadVersion === loadVersion) scheduleEvents(2500);
    }
  }, delay ?? (activeRuns(roomState(key)).length ? 650 : 2000));
}

function nearBottom() { const node = $("timeline-scroll"); return node.scrollHeight - node.scrollTop - node.clientHeight < 90; }
function saveRoomViewport(id = state.selected) {
  if (!id || state.view !== "chat") return;
  const cache = state.roomStates.get(idOf(id));
  if (!cache) return;
  const node = $("timeline-scroll");
  cache.scrollTop = node.scrollTop;
  cache.follow = state.follow;
}
function restoreRoomViewport(cache = roomState()) {
  if (cache.follow !== false) { scrollBottom(); return; }
  const node = $("timeline-scroll");
  const maximum = Math.max(0, node.scrollHeight - node.clientHeight);
  node.scrollTop = Math.min(numeric(cache.scrollTop), maximum);
  state.follow = false;
  $("jump-bottom").hidden = false;
}
function scrollBottom() {
  const node = $("timeline-scroll");
  node.scrollTop = node.scrollHeight;
  state.follow = true;
  const cache = state.roomStates.get(state.selected);
  if (cache) { cache.scrollTop = node.scrollTop; cache.follow = true; }
  $("jump-bottom").hidden = true;
}

function safeLink(raw, attachmentsOnly = false) {
  try {
    const url = new URL(raw, location.origin);
    if (attachmentsOnly) return url.origin === location.origin && url.pathname.startsWith("/api/attachments/") ? url.href : null;
    return ["http:", "https:", "mailto:"].includes(url.protocol) ? url.href : null;
  } catch { return null; }
}

function inlineMarkdown(parent, text) {
  const mathPattern = window.CodexMath?.pattern || "(?!)";
  const pattern = new RegExp(String.raw`(\x60[^\x60\n]+\x60|${mathPattern}|\*\*[^*]+\*\*|__[^_]+__|\[[^\]\n]+\]\([^\s)]+\)|https?:\/\/[^\s<>]+|\*[^*\n]+\*)`, "g");
  let offset = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > offset) parent.append(document.createTextNode(text.slice(offset, match.index)));
    const token = match[0];
    if (token.startsWith("`")) parent.append(element("code", "", token.slice(1, -1)));
    else if (/^(\\[[(]|\$)/.test(token)) parent.append(window.CodexMath.render(token));
    else if (token.startsWith("**") || token.startsWith("__")) { const strong = element("strong"); inlineMarkdown(strong, token.slice(2, -2)); parent.append(strong); }
    else if (token.startsWith("*")) parent.append(element("em", "", token.slice(1, -1)));
    else {
      const markdownLink = token.match(/^\[([^\]]+)\]\((.+)\)$/);
      const label = markdownLink ? markdownLink[1] : token;
      const target = safeLink(markdownLink ? markdownLink[2] : token);
      if (target) {
        const link = element("a", "", label);
        link.href = target; link.target = "_blank"; link.rel = "noopener noreferrer";
        parent.append(link);
      } else parent.append(document.createTextNode(label));
    }
    offset = match.index + token.length;
  }
  if (offset < text.length) parent.append(document.createTextNode(text.slice(offset)));
}

function tableCells(line) { return line.trim().replace(/^\|/, "").replace(/\|$/, "").split(/(?<!\\)\|/).map((part) => part.trim().replace(/\\\|/g, "|")); }
function tableDivider(line) { return !!line && line.includes("|") && tableCells(line).every((cell) => /^:?-{3,}:?$/.test(cell)); }
function blockStart(lines, i) { return /^\s{0,3}(#{1,6}\s|```|~~~|>\s?|[-+*]\s+|\d+[.)]\s+|([-*_])(?:\s*\2){2,}\s*$)/.test(lines[i]) || tableDivider(lines[i + 1]) || !!window.CodexMath?.displayBlock(lines, i); }

function markdown(text) {
  const fragment = document.createDocumentFragment();
  const lines = String(text || "").replace(/\r\n?/g, "\n").split("\n");
  let i = 0;
  while (i < lines.length) {
    if (!lines[i].trim()) { i += 1; continue; }
    const fence = lines[i].match(/^\s{0,3}(`{3,}|~{3,})(.*)$/);
    if (fence) {
      const language = fence[2].trim().split(/\s/)[0].slice(0, 40);
      const code = [];
      const closer = new RegExp(`^\\s{0,3}${fence[1][0]}{${fence[1].length},}\\s*$`);
      i += 1;
      while (i < lines.length && !closer.test(lines[i])) code.push(lines[i++]);
      if (i < lines.length) i += 1;
      const wrapper = element("div", "code-block");
      const toolbar = element("div", "code-toolbar");
      toolbar.append(element("span", "", language || "code"));
      const copy = element("button", "button subtle", "Copy code"); copy.type = "button";
      copy.addEventListener("click", () => copyText(code.join("\n"), copy));
      toolbar.append(copy);
      const pre = element("pre"); pre.append(element("code", "", code.join("\n")));
      wrapper.append(toolbar, pre); fragment.append(wrapper); continue;
    }
    const formula = window.CodexMath?.displayBlock(lines, i);
    if (formula) { fragment.append(window.CodexMath.render(formula.raw)); i = formula.next; continue; }
    if (tableDivider(lines[i + 1])) {
      const headers = tableCells(lines[i]); i += 2;
      const table = element("table", "markdown-table");
      const head = element("thead"); const row = element("tr");
      for (const value of headers) { const cell = element("th"); cell.scope = "col"; cell.dir = direction(value); inlineMarkdown(cell, value); row.append(cell); }
      head.append(row); table.append(head);
      const body = element("tbody");
      while (i < lines.length && lines[i].trim() && lines[i].includes("|")) {
        const values = tableCells(lines[i++]); const tr = element("tr");
        headers.forEach((_header, index) => { const cell = element("td"); cell.dir = direction(values[index]); inlineMarkdown(cell, values[index] || ""); tr.append(cell); });
        body.append(tr);
      }
      table.append(body); fragment.append(table); continue;
    }
    const heading = lines[i].match(/^\s{0,3}(#{1,6})\s+(.+)$/);
    if (heading) { const node = element(`h${heading[1].length}`); node.dir = direction(heading[2]); inlineMarkdown(node, heading[2]); fragment.append(node); i += 1; continue; }
    if (/^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/.test(lines[i])) { fragment.append(element("hr")); i += 1; continue; }
    if (/^\s{0,3}>/.test(lines[i])) {
      const quote = [];
      while (i < lines.length && /^\s{0,3}>/.test(lines[i])) quote.push(lines[i++].replace(/^\s{0,3}>\s?/, ""));
      const node = element("blockquote"); node.dir = direction(quote.join("\n")); node.append(markdown(quote.join("\n"))); fragment.append(node); continue;
    }
    const list = lines[i].match(/^\s{0,3}([-+*]|\d+[.)])\s+(.+)$/);
    if (list) {
      const ordered = /^\d/.test(list[1]); const node = element(ordered ? "ol" : "ul");
      if (ordered) node.start = parseInt(list[1], 10);
      const items = [];
      while (i < lines.length) {
        const item = lines[i].match(/^\s{0,3}([-+*]|\d+[.)])\s+(.+)$/);
        if (!item || /^\d/.test(item[1]) !== ordered) break;
        items.push(item[2]); i += 1;
      }
      node.dir = direction(items.join("\n"));
      for (const value of items) { const li = element("li"); li.dir = direction(value); inlineMarkdown(li, value); node.append(li); }
      fragment.append(node); continue;
    }
    const paragraph = [lines[i++]];
    while (i < lines.length && lines[i].trim() && !blockStart(lines, i)) paragraph.push(lines[i++]);
    const content = paragraph.join("\n"); const node = element("p"); node.dir = direction(content); inlineMarkdown(node, content); fragment.append(node);
  }
  return fragment;
}

async function copyText(text, button) {
  try {
    if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(text);
    else {
      const field = element("textarea"); field.value = text; field.style.position = "fixed"; field.style.opacity = "0";
      document.body.append(field); field.select();
      const copied = document.execCommand("copy"); field.remove();
      if (!copied) throw new Error("Copy is unavailable");
    }
    if (button) {
      const previous = button._copyRestoreText ?? button.textContent;
      button._copyRestoreText = previous;
      clearTimeout(button._copyRestoreTimer);
      button.textContent = "Copied";
      window.CodexControls?.copyFeedback(button);
      button._copyRestoreTimer = setTimeout(() => {
        if (button.isConnected) button.textContent = button._copyRestoreText;
        delete button._copyRestoreText;
        delete button._copyRestoreTimer;
      }, 1500);
    }
  } catch { toast("Select the text and use your browser’s Copy command."); }
}

function releasePreview(file) {
  if (file.preview?.startsWith("blob:")) URL.revokeObjectURL(file.preview);
}

function discardUploadedFile(file, keepalive = false) {
  if (!file) return Promise.resolve();
  if (!file.id) {
    if (!file.uploadPromise) return Promise.resolve();
    if (file.discardPromise) return file.discardPromise;
    const waiting = file.uploadPromise.then(
      () => {
        if (file.discardPromise === waiting) file.discardPromise = null;
        return discardUploadedFile(file, keepalive);
      },
      () => { if (file.discardPromise === waiting) file.discardPromise = null; },
    );
    file.discardPromise = waiting;
    return waiting;
  }
  if (file.discardPromise && !keepalive) return file.discardPromise;
  const encoded = encodeURIComponent(file.id);
  if (keepalive) {
    const path = `/api/attachments/${encoded}/discard`;
    try {
      if (navigator.sendBeacon?.(path, new Blob([], { type: "application/octet-stream" }))) return Promise.resolve();
    } catch {}
    fetch(path, { method: "POST", credentials: "same-origin", cache: "no-store", keepalive: true }).catch(() => {});
    return Promise.resolve();
  }
  file.discarding = true;
  const request = api(`/api/attachments/${encoded}`, { method: "DELETE" })
    .catch(() => {})
    .finally(() => {
      file.discarding = false;
      if (file.discardPromise === request) file.discardPromise = null;
    });
  file.discardPromise = request;
  return request;
}

async function prepareRetry(message, run, button) {
  const roomId = idOf(message.room_id || run?.room_id || state.selected);
  const room = state.rooms.find((item) => idOf(item.id) === roomId);
  if (!writableRoom(room) || !message.id) return;
  const cache = roomState(roomId);
  if (cache.sending) { toast("Wait for the current message to finish sending before replacing its draft."); return; }
  button.disabled = true;
  try {
    const matches = (item) => item.role === "user" && (run?.user_message_id ? idOf(item.id) === idOf(run.user_message_id) : idOf(item.run_id) === idOf(message.run_id));
    let original = cache.messages.find(matches);
    if (!original) {
      const result = await api(`/api/rooms/${encodeURIComponent(roomId)}/messages?before=${encodeURIComponent(message.id)}`);
      original = (result.messages || []).find(matches);
    }
    if (!original) { toast("The original message could not be loaded. Your current draft was kept."); return; }
    if (!writableRoom(state.rooms.find((item) => idOf(item.id) === roomId))) { toast("This room is no longer available for editing."); return; }
    if (cache.sending) { toast("A message is being sent. Your current draft was kept."); return; }
    if ((cache.draft.trim() || cache.files.length || cache.reply) && !window.confirm("Replace this room’s current draft with the earlier message? Nothing will be sent until you press Send.")) return;
    const attachments = original.attachments?.length ? original.attachments : (original.attachment_ids || []).map((id) => ({ id, name: "File", url: `/api/attachments/${encodeURIComponent(id)}` }));
    const restoredFiles = [];
    for (const attachment of attachments) {
      const url = safeLink(attachment.url || `/api/attachments/${encodeURIComponent(attachment.id)}`, true);
      if (!attachment.id || !url) throw new Error("An original attachment could not be restored. Your current draft was kept.");
      restoredFiles.push({ ...attachment, localId: requestId(), preview: imageAttachment(attachment) ? url : null, url, name: attachment.name || "File", status: "ready" });
    }
    for (const file of cache.files) releasePreview(file);
    cache.files = restoredFiles;
    if (!setComposerDraft(cache, String(original.content || ""))) touchComposer(cache);
    cache.reply = null;
    cache.pendingSend = null;
    cache.retryNotice = "Review before sending again. The earlier attempt may have changed files or run commands.";
    if (state.selected !== roomId || state.view !== "chat") {
      toast(`The earlier message was restored as a draft in “${room.title || "New chat"}”. Nothing was sent.`);
      return;
    }
    $("message-input").value = cache.draft;
    renderReply(); renderAttachments(); updateComposer();
    $("message-input").focus({ preventScroll: true });
    toast("The earlier message and files are back in the composer. Edit them and press Send when you’re ready.");
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; }
}

function selectedQuote(content, fallback) {
  const selection = window.getSelection();
  return selection && !selection.isCollapsed && content.contains(selection.anchorNode) && content.contains(selection.focusNode) ? selection.toString() : fallback;
}

function messageRun(message, cache) {
  if (message.role !== "assistant") return null;
  const existing = cache.runs.get(idOf(message.run_id)) || message.run || null;
  const messageContext = message.context_overview ?? message.run?.context_overview;
  const messageStatus = message.run_status || message.run?.status;
  const terminalMessage = !!messageStatus && !activeRun({ status: messageStatus });
  const contextOverview = terminalMessage
    ? mergeContextOverview(existing?.context_overview, messageContext)
    : mergeContextOverview(messageContext, existing?.context_overview);
  const messageProgress = message.progress_log ?? message.run?.progress_log;
  if (!existing && !message.run_status && !message.run_error && !message.activities?.length && !messageProgress?.length && !message.latest_status_line && !contextOverview) return null;
  // CODEX: Terminal messages carry the final log when lightweight run snapshots omit it.
  const activitySource = Array.isArray(message.activities) && !activeRun(existing) ? message : existing || message;
  const progressSource = Array.isArray(messageProgress) && (terminalMessage || !activeRun(existing)) ? message : existing || message;
  return { ...existing, id: existing?.id || message.run_id || message.id, status: message.run_status || existing?.status, error: message.run_error ?? existing?.error, activities: activitySource.activities ?? message.activities, activity_count: activitySource.activity_count ?? message.activity_count, activity_truncated: activitySource.activity_truncated ?? message.activity_truncated, latest_status_line: activitySource.latest_status_line ?? message.latest_status_line, context_overview: contextOverview,
    progress_log: progressSource === message ? messageProgress : progressSource.progress_log ?? messageProgress,
    progress_log_count: progressSource.progress_log_count ?? message.run?.progress_log_count ?? message.progress_log_count,
    progress_log_truncated: progressSource.progress_log_truncated ?? message.run?.progress_log_truncated ?? message.progress_log_truncated };
}

function activityPreferences(runId) {
  const key = `codex-lan-activity:${state.me?.id || ""}:${runId}`;
  let saved = {};
  try { saved = JSON.parse(sessionStorage.getItem(key) || "{}"); } catch {}
  return { key, open: saved?.open === true, tab: ["activity", "progress", "context"].includes(saved?.tab) ? saved.tab : "activity", level: ["summary", "steps", "output"].includes(saved?.level) ? saved.level : "steps", entries: saved?.entries && typeof saved.entries === "object" ? saved.entries : {} };
}

function saveActivityPreferences(preferences) {
  // CODEX: Persist only view preferences; logs, context metadata and credentials never enter browser storage.
  try { sessionStorage.setItem(preferences.key, JSON.stringify({ open: preferences.open, tab: preferences.tab, level: preferences.level, entries: preferences.entries })); } catch {}
}

function activityEntries(run) {
  const hiddenKinds = new Set(["reasoning", "analysis", "reasoning_summary"]);
  const entries = new Map();
  for (const entry of Array.isArray(run?.activities) ? run.activities : []) {
    if (entry && entry.id && !hiddenKinds.has(entry.kind)) entries.set(idOf(entry.id), entry);
  }
  return [...entries.values()];
}

function readableActivity(value) {
  if (typeof value !== "string") return "";
  const text = value.replace(/\*{2,}/g, "\n").split(/\n+/).map((line) => line.trim()).filter(Boolean).at(-1) || "";
  if (/^(working|thinking|analyzing|processing)(?:[.…\s]*)$/i.test(text) || text.startsWith("{")) return "";
  return text.length > 180 ? `${text.slice(0, 177)}…` : text;
}

function selectedWithin(node) {
  const selection = window.getSelection();
  return !!selection && !selection.isCollapsed && (node.contains(selection.anchorNode) || node.contains(selection.focusNode));
}

function updateActivityText(node, value) {
  const text = String(value ?? "");
  if (node.textContent === text) return;
  if (selectedWithin(node)) { state.selectionRefreshPending = true; return; }
  // CODEX: Appending output preserves existing text nodes and native selection during streaming.
  if (text.startsWith(node.textContent) && node.childNodes.length === 1 && node.firstChild.nodeType === Node.TEXT_NODE) node.firstChild.appendData(text.slice(node.textContent.length));
  else node.textContent = text;
}

function activityStatus(entry) {
  const value = String(entry.status || "running");
  return value === "canceled" ? "cancelled" : value;
}

function activityDuration(entry) {
  if (entry.duration_ms !== undefined && entry.duration_ms !== null) return formatDuration(numeric(entry.duration_ms) / 1000);
  const started = timestamp(entry.started_at);
  const finished = timestamp(entry.updated_at);
  return started ? formatDuration(Math.max(0, ((activityStatus(entry) === "running" ? Date.now() : finished) - started) / 1000)) : "";
}

function activityLog(entries) {
  return entries.map((entry) => {
    const status = activityStatus(entry);
    const exit = entry.exit_code !== undefined && entry.exit_code !== null ? ` · exit ${entry.exit_code}` : "";
    const duration = activityDuration(entry);
    const parts = [`[${status}${exit}${duration ? ` · ${duration}` : ""}] ${entry.title || entry.kind || "Action"}`];
    if (entry.detail) parts.push(String(entry.detail));
    if (entry.output) parts.push(String(entry.output));
    if (entry.output_truncated) parts.push("[Earlier output omitted by the server]");
    return parts.join("\n");
  }).join("\n\n");
}

function progressLogEntries(run) {
  const entries = new Map();
  for (const entry of Array.isArray(run?.progress_log) ? run.progress_log : []) {
    if (entry && entry.id && typeof entry.text === "string" && entry.text.trim()) entries.set(idOf(entry.id), entry);
  }
  return [...entries.values()];
}

function progressLogText(run) {
  const entries = progressLogEntries(run);
  const text = entries.map((entry) => {
    const date = timeValue(entry.started_at);
    return `${date ? `[${date.toLocaleString()}]\n` : ""}${entry.text}${entry.truncated ? "\n[Update shortened by the server]" : ""}`;
  }).join("\n\n");
  return text + (run?.progress_log_truncated ? "\n\n[Earlier updates or text omitted by the server]" : "");
}

function updateProgressLog(panel, run, entries) {
  const view = panel.querySelector(".activity-progress");
  view.querySelector(".activity-progress-copy").disabled = !entries.length;
  view.querySelector(".activity-progress-empty").hidden = !!entries.length;
  const note = view.querySelector(".activity-progress-truncation");
  note.hidden = !run.progress_log_truncated;
  updateActivityText(note, "This is a bounded log. Earlier updates or parts of very long updates were omitted by the server.");
  const list = view.querySelector(".activity-progress-list");
  const existing = new Map([...list.children].map((node) => [node.dataset.entryId, node]));
  const desired = [];
  for (const entry of entries) {
    const key = idOf(entry.id);
    let row = existing.get(key);
    if (!row) {
      row = element("section", "activity-progress-entry"); row.dataset.entryId = key;
      const heading = element("div", "activity-progress-entry-heading");
      heading.append(element("span", "", "Public update"), element("time", "activity-progress-time"));
      const text = element("div", "activity-progress-text"); text.tabIndex = 0; text.setAttribute("aria-label", "Public progress message");
      row.append(heading, text, element("p", "activity-progress-entry-truncation", "This long update was shortened by the server."));
    }
    const date = timeValue(entry.started_at);
    const time = row.querySelector("time");
    updateActivityText(time, date ? timeLabel(entry.started_at) : ""); time.hidden = !date;
    if (date) { time.dateTime = date.toISOString(); time.title = date.toLocaleString(); }
    const text = row.querySelector(".activity-progress-text");
    updateActivityText(text, entry.text); text.dir = direction(text.textContent);
    row.querySelector(".activity-progress-entry-truncation").hidden = !entry.truncated;
    desired.push(row);
  }
  const keys = new Set(desired.map((row) => row.dataset.entryId));
  for (const row of [...list.children]) if (!keys.has(row.dataset.entryId)) {
    if (selectedWithin(row)) state.selectionRefreshPending = true;
    else row.remove();
  }
  let position = list.firstChild;
  for (const row of desired) { if (row === position) position = position.nextSibling; else list.insertBefore(row, position); }
}

let activityPanelSequence = 0;

function boundedContextText(value, limit = 120) {
  if (typeof value !== "string") return "";
  return value.replace(/[\u0000-\u001f\u007f]+/g, " ").replace(/\s+/g, " ").trim().slice(0, limit);
}

function boundedContextContentText(value, limit = 32768) {
  if (typeof value !== "string") return "";
  return value.replace(/\r\n?/g, "\n").replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\u202a-\u202e\u2066-\u2069]/g, "").slice(0, limit);
}

function contextBasename(value) {
  const text = boundedContextText(value, 400);
  return boundedContextText(text.split(/[\\/]/).filter(Boolean).at(-1) || "", 100);
}

function contextNumber(value) {
  const number = knownNumber(value);
  return number === null ? null : Math.min(Math.floor(number), 1_000_000_000_000_000);
}

function contextContentKind(value) {
  if (value === "turn_input") return "Turn input";
  if (value === "visible_room_history") return "Room history reference";
  if (value === "instruction") return "Instruction";
  return "Local context";
}

function contextContentSource(value) {
  const source = boundedContextText(value, 120);
  if (source === "message") return "Current message";
  if (source === "selected_parent_context") return "Selected parent context";
  if (source === "companion") return "More details request";
  if (source === "local_message_store") return "Local room history";
  if (source === "app_server_instruction_source") return "Codex instruction source";
  return /[\\/]/.test(source) ? contextBasename(source) : source;
}

function normalizedContextContent(value) {
  if (!Array.isArray(value?.content)) return [];
  const result = [];
  let remaining = 131072;
  for (const item of value.content.slice(0, 24)) {
    if (!item || typeof item !== "object" || Array.isArray(item) || remaining <= 0) continue;
    const original = typeof item.text === "string" ? item.text : "";
    const limit = Math.min(32768, remaining);
    const text = boundedContextContentText(original, limit);
    if (!text) continue;
    const kind = boundedContextText(item.kind, 48).toLowerCase().replace(/[^a-z0-9_-]/g, "_") || "context";
    const rawLabel = boundedContextText(item.label, 120);
    result.push({
      kind,
      label: (/[\\/]/.test(rawLabel) ? contextBasename(rawLabel) : rawLabel) || contextContentKind(kind),
      source: contextContentSource(item.source),
      text,
      truncated: item.truncated === true || original.length > text.length,
    });
    remaining -= text.length;
  }
  return result;
}

function normalizedContextOverview(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const thread = value.thread && typeof value.thread === "object" ? value.thread : {};
  const input = value.input && typeof value.input === "object" ? value.input : {};
  const instructions = value.instructions && typeof value.instructions === "object" ? value.instructions : {};
  const turn = value.turn && typeof value.turn === "object" ? value.turn : {};
  const limits = value.limits && typeof value.limits === "object" ? value.limits : {};
  const compaction = value.compaction && typeof value.compaction === "object" ? value.compaction : {};
  const usageSource = value.usage && typeof value.usage === "object" ? value.usage : value.token_usage && typeof value.token_usage === "object" ? value.token_usage : {};
  const files = Array.isArray(instructions.files) ? instructions.files.map(contextBasename).filter(Boolean).slice(0, 12) : [];
  const mode = thread.mode === "resumed" || thread.mode === "resume" ? "resumed" : "new";
  const inputSource = ["message", "companion", "selected_parent_context"].includes(input.source) ? input.source : "message";
  const access = turn.access === "read_only" || turn.access === "read-only" ? "read_only" : "full_access";
  return {
    version: contextNumber(value.version),
    capturedAt: contextNumber(value.captured_at),
    mode,
    historyManagedBy: thread.history_managed_by === "codex" ? "codex" : "",
    inputSource,
    inputCharacters: contextNumber(input.characters),
    inputLines: contextNumber(input.lines),
    inputImages: contextNumber(input.images),
    inputFiles: contextNumber(input.files),
    inputTruncated: input.truncated === true,
    instructionCount: contextNumber(instructions.count),
    instructionFiles: files,
    instructionsTruncated: instructions.truncated === true || (contextNumber(instructions.count) ?? 0) > files.length,
    model: boundedContextText(turn.model, 100),
    effort: boundedContextText(turn.effort, 40),
    speed: boundedContextText(turn.speed, 40),
    access,
    workspace: contextBasename(turn.workspace),
    approvalPolicy: boundedContextText(turn.approval_policy, 40),
    serviceTier: boundedContextText(turn.service_tier, 40),
    newContextTokens: contextNumber(limits.new_context_tokens),
    limitScope: boundedContextText(limits.scope, 60),
    toolOutputTokens: contextNumber(limits.tool_output_tokens),
    compactionEvents: contextNumber(compaction.events),
    compactionContents: compaction.contents === "opaque" ? "opaque" : "",
    inputTokens: contextNumber(usageSource.input_tokens),
    cachedInputTokens: contextNumber(usageSource.cached_input_tokens),
    outputTokens: contextNumber(usageSource.output_tokens),
    totalTokens: contextNumber(usageSource.total_tokens),
    contextWindow: contextNumber(usageSource.model_context_window),
    content: normalizedContextContent(value),
  };
}

function contextInputSource(value) {
  if (value === "companion") return "Companion request";
  if (value === "selected_parent_context") return "Selected parent context";
  return "Message";
}

function contextFactList(title, values) {
  const section = element("section", "activity-context-section");
  section.append(element("h4", "activity-context-section-title", title));
  const facts = element("dl", "activity-context-facts");
  for (const [label, value] of values) {
    if (value === "" || value === null || value === undefined) continue;
    const row = element("div", "activity-context-fact");
    const detail = element("dd", "", value); detail.dir = direction(value);
    row.append(element("dt", "", label), detail); facts.append(row);
  }
  section.append(facts);
  return section;
}

function contextUsagePanel(context) {
  return contextFactList("Reported token usage", [
    ["Input", context.inputTokens === null ? "Not reported" : count(context.inputTokens)],
    ["Cached input", context.cachedInputTokens === null ? "Not reported" : count(context.cachedInputTokens)],
    ["Output", context.outputTokens === null ? "Not reported" : count(context.outputTokens)],
    ["Total", context.totalTokens === null ? "Not reported" : count(context.totalTokens)],
    ["Model context window", context.contextWindow === null ? "Not reported" : count(context.contextWindow)],
  ]);
}

function contextOverviewText(value) {
  const context = normalizedContextOverview(value);
  if (!context?.content.length) return "";
  return context.content.map((block) => {
    const provenance = [contextContentKind(block.kind), block.source].filter(Boolean).join(" · ");
    return `${block.label}${provenance ? `\nSource: ${provenance}` : ""}${block.truncated ? "\n[truncated]" : ""}\n\n${block.text}`;
  }).join("\n\n──────────\n\n");
}

function renderContextMetadata(context) {
  const details = element("details", "activity-context-metadata");
  details.append(element("summary", "activity-context-metadata-summary", "Turn details"));
  const grid = element("div", "activity-context-metadata-grid");
  if ([context.inputTokens, context.cachedInputTokens, context.outputTokens, context.totalTokens, context.contextWindow].some((item) => item !== null)) grid.append(contextUsagePanel(context));
  const memory = context.mode === "resumed" ? "Resumed thread" : "New thread";
  const memoryDetail = context.mode === "resumed" && context.historyManagedBy === "codex" ? "Conversation memory managed by Codex" : context.mode === "new" ? "No earlier thread memory reused" : "History source not reported";
  const sizePrefix = context.inputTruncated ? "At least " : "";
  grid.append(contextFactList("Request & memory", [
    ["Input source", contextInputSource(context.inputSource)],
    ["Input size", `${sizePrefix}${count(context.inputCharacters ?? 0)} characters · ${sizePrefix.toLowerCase()}${count(context.inputLines ?? 0)} lines`],
    ["Images", `${sizePrefix}${count(context.inputImages ?? 0)}`],
    ...(context.inputFiles ? [["Files", count(context.inputFiles)]] : []),
    ["Thread", memory],
    ["Saved memory", memoryDetail],
  ]));
  const instructionSection = contextFactList("Instructions", [
    ["Sources", count(context.instructionCount ?? context.instructionFiles.length)],
    ["Coverage", context.instructionsTruncated ? "Some source names omitted" : "All reported source names shown"],
  ]);
  if (context.instructionFiles.length) {
    const files = element("div", "activity-context-files");
    for (const file of context.instructionFiles) files.append(element("code", "activity-context-file", file));
    instructionSection.append(files);
  }
  grid.append(instructionSection);
  grid.append(contextFactList("Turn setup", [
    ["Model", context.model || "Not reported"],
    ["Thinking", context.effort ? effortLabel(context.effort) : "Default"],
    ["Speed", context.speed || "Standard"],
    ["Access", context.access === "read_only" ? "Read only" : "Full access"],
    ["Workspace", context.workspace || "Not reported"],
    ["Approval policy", context.approvalPolicy || "Not reported"],
    ["Service tier", context.serviceTier || "Not reported"],
  ]));
  const scope = context.limitScope === "body_after_prefix" ? "New content after retained prefix" : context.limitScope || "Not reported";
  grid.append(contextFactList("Limits & compaction", [
    ["New-context budget", context.newContextTokens === null ? "Not reported" : `${count(context.newContextTokens)} tokens`],
    ["Budget scope", scope],
    ["Tool-output budget", context.toolOutputTokens === null ? "Not reported" : `${count(context.toolOutputTokens)} tokens`],
    ["Compaction events", count(context.compactionEvents ?? 0)],
    ["Compacted contents", context.compactionContents === "opaque" ? "Opaque · unavailable" : "None reported"],
  ]));
  details.append(grid);
  return details;
}

function renderContextOverview(panel, value) {
  const contextPanel = panel.querySelector(".activity-context");
  const body = contextPanel.querySelector(".activity-context-body");
  const copy = contextPanel.querySelector(".activity-context-copy");
  const context = normalizedContextOverview(value);
  copy.disabled = !context?.content.length;
  const signature = JSON.stringify(context);
  if (panel._contextSignature === signature) return;
  if (selectedWithin(body)) { state.selectionRefreshPending = true; return; }
  panel._contextSignature = signature;
  body.replaceChildren();
  const captured = context?.capturedAt ? timeValue(context.capturedAt) : null;
  updateActivityText(contextPanel.querySelector(".activity-context-captured"), captured ? `Captured ${captured.toLocaleString()}` : "");
  if (!context) {
    body.append(element("p", "activity-context-empty", "Prepared context details are not available for this response."));
    return;
  }
  if (!context.content.length) {
    body.append(element("p", "activity-context-content-empty", "The content itself was not captured for this response. Turn details are still available below."));
  } else {
    const content = element("div", "activity-context-content");
    for (const block of context.content) {
      const section = element("section", `activity-context-block activity-context-block-${block.kind}`);
      const header = element("div", "activity-context-block-header");
      const title = element("div", "activity-context-block-title");
      title.append(element("h4", "", block.label), element("span", "activity-context-kind", contextContentKind(block.kind)));
      const provenance = element("p", "activity-context-source", `Source: ${block.source || "Locally observed"}`);
      header.append(title, provenance);
      if (block.truncated) header.append(element("span", "activity-context-truncated", "Truncated"));
      const text = element("pre", "activity-context-text", block.text);
      text.tabIndex = 0;
      text.dir = direction(block.text);
      text.setAttribute("aria-label", `${block.label} content`);
      section.append(header, text);
      content.append(section);
    }
    body.append(content);
  }
  body.append(renderContextMetadata(context));
}

function createActivityPanel(panel, runId) {
  const preferences = activityPreferences(runId);
  panel._preferences = preferences;
  panel._runId = runId;
  const current = element("div", "activity-current");
  current.append(element("span", "activity-dot"), element("span", "activity-current-label"));
  const connection = element("div", "run-connection"); connection.hidden = true;
  const connectionHeading = element("div", "run-connection-heading"); connectionHeading.setAttribute("role", "status");
  const connectionSpinner = element("span", "run-connection-spinner"); connectionSpinner.setAttribute("aria-hidden", "true");
  connectionHeading.append(connectionSpinner, element("strong", "run-connection-title"));
  connection.append(connectionHeading, element("p", "run-connection-detail"), element("p", "run-connection-policy"));
  const details = element("details", "activity-details");
  const summary = element("summary", "activity-disclosure");
  summary.append(element("span", "activity-disclosure-label", "Activity"), element("span", "activity-counts"));
  const panelId = `activity-panel-${++activityPanelSequence}`;
  const tabs = element("div", "activity-tabs"); tabs.setAttribute("role", "tablist"); tabs.setAttribute("aria-label", "Response details");
  const activityView = element("div", "activity-view"); activityView.id = `${panelId}-activity-panel`; activityView.setAttribute("role", "tabpanel");
  const progressView = element("div", "activity-progress"); progressView.id = `${panelId}-progress-panel`; progressView.setAttribute("role", "tabpanel");
  const contextView = element("div", "activity-context"); contextView.id = `${panelId}-context-panel`; contextView.setAttribute("role", "tabpanel");
  const tabNames = ["activity", "progress", "context"];
  for (const [tab, label, controlled] of [["activity", "Activity", activityView], ["progress", "Progress log", progressView], ["context", "Prepared context", contextView]]) {
    const button = element("button", "activity-tab", label); button.type = "button"; button.dataset.tab = tab;
    button.id = `${panelId}-${tab}-tab`; button.setAttribute("role", "tab"); button.setAttribute("aria-controls", controlled.id);
    controlled.setAttribute("aria-labelledby", button.id);
    button.addEventListener("click", () => {
      preferences.tab = tab; saveActivityPreferences(preferences); updateActivityPanel(panel, panel._run);
    });
    button.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const index = tabNames.indexOf(tab);
      const target = event.key === "Home" ? tabNames[0] : event.key === "End" ? tabNames.at(-1)
        : tabNames[(index + (event.key === "ArrowLeft" ? -1 : 1) + tabNames.length) % tabNames.length];
      preferences.tab = target; saveActivityPreferences(preferences); updateActivityPanel(panel, panel._run);
      tabs.querySelector(`.activity-tab[data-tab="${target}"]`).focus();
    });
    tabs.append(button);
  }
  const toolbar = element("div", "activity-toolbar");
  const levels = element("div", "activity-levels"); levels.setAttribute("role", "group"); levels.setAttribute("aria-label", "Log detail level");
  for (const level of ["summary", "steps", "output"]) {
    const button = element("button", "activity-level", level[0].toUpperCase() + level.slice(1)); button.type = "button"; button.dataset.level = level;
    button.addEventListener("click", () => {
      preferences.level = level;
      if (level !== "summary") for (const entry of panel._entries || []) preferences.entries[idOf(entry.id)] = level === "output";
      saveActivityPreferences(preferences); updateActivityPanel(panel, panel._run);
    });
    levels.append(button);
  }
  const copy = element("button", "button subtle activity-copy", "Copy log"); copy.type = "button";
  copy.addEventListener("click", () => copyText(activityLog(panel._entries || []), copy));
  toolbar.append(levels, copy);
  const overview = element("p", "activity-overview");
  const latest = element("div", "activity-latest");
  latest.append(element("span", "activity-latest-label"), element("p", "activity-latest-line"));
  const historyNote = element("p", "activity-history-note"); historyNote.hidden = true;
  const list = element("div", "activity-steps");
  activityView.append(toolbar, overview, latest, historyNote, list);
  const progressHeader = element("div", "activity-progress-header");
  progressHeader.append(element("strong", "", "Updates from this response"));
  const progressCopy = element("button", "button subtle activity-progress-copy", "Copy log"); progressCopy.type = "button";
  progressCopy.setAttribute("aria-label", "Copy progress log");
  progressCopy.addEventListener("click", () => copyText(progressLogText(panel._run), progressCopy));
  progressHeader.append(progressCopy);
  const progressDescription = element("p", "activity-progress-description", "Public messages shown while Codex was working. Tool output is kept in Activity.");
  const progressEmpty = element("p", "activity-progress-empty", "No public progress messages were recorded for this response.");
  const progressNote = element("p", "activity-progress-truncation"); progressNote.hidden = true;
  const progressList = element("div", "activity-progress-list"); progressList.tabIndex = 0; progressList.setAttribute("aria-label", "Saved public progress messages");
  progressView.append(progressHeader, progressDescription, progressNote, progressEmpty, progressList);
  const contextHeader = element("div", "activity-context-header");
  const contextHeading = element("div", "activity-context-heading");
  contextHeading.append(element("strong", "", "Content prepared locally for this turn"), element("span", "activity-context-captured"));
  const contextCopy = element("button", "button subtle activity-context-copy", "Copy content"); contextCopy.type = "button";
  contextCopy.setAttribute("aria-label", "Copy prepared content");
  contextCopy.addEventListener("click", () => copyText(contextOverviewText(panel._run?.context_overview), contextCopy));
  contextHeader.append(contextHeading, contextCopy);
  const disclaimer = element("p", "activity-context-disclaimer", "This shows bounded content observed locally. The provider’s exact serialized prompt, hidden prompts, hidden reasoning, and opaque compacted memory are unavailable.");
  contextView.append(contextHeader, disclaimer, element("div", "activity-context-body"));
  details.append(summary, tabs, activityView, progressView, contextView); details.open = preferences.open;
  details.addEventListener("toggle", () => {
    preferences.open = details.open; saveActivityPreferences(preferences);
    if (details.open) state.follow = false;
  });
  panel.replaceChildren(connection, current, details);
}

function createActivityEntry(panel, entry) {
  const row = element("details", "activity-step"); row.dataset.entryId = idOf(entry.id);
  const summary = element("summary", "activity-step-summary");
  summary.append(element("span", "activity-step-icon"), element("span", "activity-step-title"), element("span", "activity-step-status"), element("span", "activity-step-duration"));
  const body = element("div", "activity-step-body");
  const command = element("pre", "activity-command");
  const outputLabel = element("div", "activity-output-label", "Output");
  const output = element("pre", "activity-output"); output.tabIndex = 0; output.setAttribute("aria-label", "Command output");
  const truncation = element("p", "activity-truncation", "Earlier output omitted by the server.");
  body.append(command, outputLabel, output, truncation); row.append(summary, body);
  row.addEventListener("toggle", () => {
    panel._preferences.entries[row.dataset.entryId] = row.open; saveActivityPreferences(panel._preferences);
    if (row.open) state.follow = false;
  });
  return row;
}

function updateActivityPanel(panel, run) {
  if (!run) { panel.hidden = true; return; }
  const entries = activityEntries(run);
  const progressEntries = progressLogEntries(run);
  const hasContext = !!normalizedContextOverview(run.context_overview);
  const publicStatus = typeof run.latest_status_line === "string" ? run.latest_status_line.trim() : "";
  const running = activeRun(run);
  const stopped = ["cancelled", "canceled", "stopped"].includes(run.status);
  panel.hidden = !entries.length && !progressEntries.length && !publicStatus && !running && !stopped && !hasContext;
  if (panel.hidden) return;
  const runId = idOf(run.id || run.run_id);
  if (panel._runId !== runId) createActivityPanel(panel, runId);
  panel._entries = entries; panel._run = run;
  const preferences = panel._preferences;
  const inProgress = entries.filter((entry) => activityStatus(entry) === "running");
  const failed = entries.filter((entry) => activityStatus(entry) === "failed");
  const complete = entries.filter((entry) => activityStatus(entry) === "completed");
  const interrupted = entries.filter((entry) => ["cancelled", "interrupted"].includes(activityStatus(entry)));
  const last = inProgress.at(-1);
  const connection = running ? runConnection(run) : null;
  const browserOffline = running && !state.connected;
  const fallback = run.status === "queued" ? "Queued — waiting for your worker" : run.status === "stopping" ? "Stopping response…" : run.output_text ? "Preparing the response" : "Waiting for the next update";
  const label = browserOffline ? "Reconnecting to server — live status unavailable" : connection ? connectionLabel(connection) : running ? (readableActivity(last?.title) || readableActivity(run.activity) || fallback) : stopped ? "Response stopped" : ["failed", "interrupted", "error"].includes(run.status) ? "Activity ended before completion" : "Activity complete";
  updateActivityText(panel.querySelector(".activity-current-label"), label);
  panel.querySelector(".activity-current-label").dir = direction(panel.querySelector(".activity-current-label").textContent);
  panel.querySelector(".activity-dot").hidden = !running;
  panel.dataset.status = connection || browserOffline ? "reconnecting" : running ? "running" : failed.length || run.status === "failed" ? "failed" : "completed";
  panel.querySelector(".activity-current").hidden = !!connection || browserOffline || (!running && (!!entries.length || !!progressEntries.length || hasContext));
  const connectionCard = panel.querySelector(".run-connection"); connectionCard.hidden = !connection && !browserOffline;
  if (!connectionCard.hidden) {
    updateActivityText(connectionCard.querySelector(".run-connection-title"), label);
    const details = [];
    if (browserOffline) details.push("The browser is retrying its connection. The server may still be processing your response.");
    else {
      if (connection.message) details.push(String(connection.message));
      const delay = numeric(connection.retry_in_seconds);
      if (delay) {
        const retryAt = timestamp(connection.retry_at) || (timestamp(connection.updated_at) ? timestamp(connection.updated_at) + delay * 1000 : 0);
        details.push(retryAt ? `Next attempt in ${Math.max(0, Math.ceil((retryAt - Date.now()) / 1000))}s.` : `Retry delay: ${Math.ceil(delay)}s.`);
      }
      const switches = Math.floor(numeric(connection.switches));
      if (switches) details.push(`Network route changed ${switches} ${switches === 1 ? "time" : "times"}.`);
    }
    updateActivityText(connectionCard.querySelector(".run-connection-detail"), details.join(" "));
    updateActivityText(connectionCard.querySelector(".run-connection-policy"), browserOffline ? "Live updates will catch up when connected." : "The response stays open while connecting. You can stop it below.");
  }
  const counts = [];
  if (complete.length) counts.push(`${complete.length} done`);
  if (inProgress.length) counts.push(`${inProgress.length} ${connection || browserOffline ? "last reported active" : "running"}`);
  if (failed.length) counts.push(`${failed.length} failed`);
  if (interrupted.length) counts.push(`${interrupted.length} stopped`);
  updateActivityText(panel.querySelector(".activity-counts"), counts.join(" · "));
  const total = Math.max(entries.length, numeric(run.activity_count));
  const progressCount = Math.max(progressEntries.length, numeric(run.progress_log_count));
  const disclosureLabel = total ? `Activity · ${total} ${total === 1 ? "step" : "steps"}` : "Activity";
  updateActivityText(panel.querySelector(".activity-disclosure-label"), `${disclosureLabel}${progressCount ? ` · ${progressCount} ${progressCount === 1 ? "update" : "updates"}` : ""}`);
  const historyNote = panel.querySelector(".activity-history-note"); historyNote.hidden = !run.activity_truncated;
  updateActivityText(historyNote, `Showing the latest ${entries.length} steps. Counts below refer to these retained steps; earlier details were omitted by the server.`);
  panel.querySelector(".activity-details").hidden = !entries.length && !progressEntries.length && !publicStatus && !hasContext;
  panel.querySelector(".activity-details").dataset.level = preferences.level;
  const selectedTab = ["activity", "progress", "context"].includes(preferences.tab) ? preferences.tab : "activity";
  for (const button of panel.querySelectorAll(".activity-tab")) {
    const selected = button.dataset.tab === selectedTab;
    button.setAttribute("aria-selected", String(selected)); button.tabIndex = selected ? 0 : -1;
  }
  panel.querySelector(".activity-view").hidden = selectedTab !== "activity";
  panel.querySelector(".activity-progress").hidden = selectedTab !== "progress";
  panel.querySelector(".activity-context").hidden = selectedTab !== "context";
  if (selectedTab === "progress") updateProgressLog(panel, run, progressEntries);
  renderContextOverview(panel, run.context_overview);
  for (const button of panel.querySelectorAll(".activity-level")) button.setAttribute("aria-pressed", String(button.dataset.level === preferences.level));
  const overview = panel.querySelector(".activity-overview"); overview.hidden = preferences.level !== "summary";
  updateActivityText(overview, `${label}. ${counts.join(" · ") || "No tool actions reported yet"}. Choose Steps to inspect commands, or Output to expand their results.`);
  const latestEntry = [...entries].sort((a, b) => timestamp(a.updated_at || a.started_at) - timestamp(b.updated_at || b.started_at)).at(-1);
  const lastLine = (value) => typeof value === "string" ? value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).at(-1) || "" : "";
  const latestLine = lastLine(run.latest_status_line) || lastLine(latestEntry?.output) || lastLine(latestEntry?.detail) || readableActivity(latestEntry?.title) || readableActivity(run.activity);
  const latest = panel.querySelector(".activity-latest"); latest.hidden = preferences.level !== "summary" || !latestLine;
  updateActivityText(latest.querySelector(".activity-latest-label"), connection || browserOffline || !running ? "Last activity" : "Latest update");
  updateActivityText(latest.querySelector(".activity-latest-line"), latestLine);
  latest.querySelector(".activity-latest-line").dir = direction(latest.querySelector(".activity-latest-line").textContent);
  const list = panel.querySelector(".activity-steps"); list.hidden = preferences.level === "summary";
  const existing = new Map([...list.children].map((node) => [node.dataset.entryId, node]));
  const desired = [];
  for (const entry of entries) {
    const key = idOf(entry.id), row = existing.get(key) || createActivityEntry(panel, entry);
    const status = activityStatus(entry);
    row.dataset.status = status;
    const title = String(entry.title || entry.kind || "Action");
    updateActivityText(row.querySelector(".activity-step-title"), title); row.querySelector(".activity-step-title").dir = direction(row.querySelector(".activity-step-title").textContent);
    updateActivityText(row.querySelector(".activity-step-icon"), status === "running" ? "›" : status === "completed" ? "✓" : status === "failed" ? "!" : "–");
    const exit = entry.exit_code !== undefined && entry.exit_code !== null ? ` · exit ${entry.exit_code}` : "";
    updateActivityText(row.querySelector(".activity-step-status"), `${status}${exit}`);
    updateActivityText(row.querySelector(".activity-step-duration"), activityDuration(entry));
    const command = row.querySelector(".activity-command");
    updateActivityText(command, entry.detail || ""); command.hidden = !entry.detail;
    const output = row.querySelector(".activity-output"); updateActivityText(output, entry.output || ""); output.hidden = !entry.output;
    row.querySelector(".activity-output-label").hidden = !entry.output;
    row.querySelector(".activity-truncation").hidden = !entry.output_truncated;
    const open = Object.hasOwn(preferences.entries, key) ? preferences.entries[key] === true : preferences.level === "output";
    if (row.open !== open) row.open = open;
    desired.push(row);
  }
  const keys = new Set(desired.map((row) => row.dataset.entryId));
  for (const row of [...list.children]) if (!keys.has(row.dataset.entryId)) row.remove();
  let position = list.firstChild;
  for (const row of desired) { if (row === position) position = position.nextSibling; else list.insertBefore(row, position); }
}

function answerPresentation(message, run) {
  const assistant = message.role === "assistant";
  const status = run?.status || message.run_status || "";
  const rawError = message.run_error ?? run?.error;
  const error = typeof rawError === "string" ? rawError : rawError?.message || "";
  const failed = assistant && terminalStatuses.has(status) && (!!error || ["failed", "error", "interrupted", "rejected"].includes(status));
  const original = String(message.content || "");
  const textIsError = failed && error && [error.trim(), `Error: ${error.trim()}`].includes(original.trim());
  const text = textIsError ? "" : original;
  return { assistant, status, failed, error, text, emptyFailure: failed && !text.trim() };
}

function messageNode(message, key) {
  const article = element("article", `message ${message.role || "assistant"}`);
  article.dataset.key = key;
  const header = element("div", "message-header");
  header.append(element("span", "message-avatar", message.role === "user" ? "You".slice(0, 1) : "C"));
  header.append(element("strong", "", message.role === "user" ? (ownRoom(selectedRoom()) ? "You" : selectedRoom()?.owner_name || "Client") : message.role === "system" ? "System" : "Codex"));
  const time = element("time", "", timeLabel(message.created_at));
  const date = timeValue(message.created_at); if (date) { time.dateTime = date.toISOString(); time.title = date.toLocaleString(); }
  header.append(time);
  const body = element("div", "message-body");
  const quote = element("div", "message-quote"); quote.hidden = true;
  const attachments = element("div", "message-attachments"); attachments.hidden = true;
  const content = element("div", "message-content");
  const activity = element("div", "run-activity"); activity.hidden = true;
  body.append(quote, attachments, content, activity);
  if (message.role === "assistant") {
    const failure = element("div", "run-failure"); failure.hidden = true;
    const title = element("strong", "failure-title");
    const detail = element("p", "run-error");
    const note = element("p", "retry-note", "Review before sending again. The earlier attempt may have changed files or run commands.");
    const retry = element("button", "button retry-button", "Edit & retry"); retry.type = "button";
    retry.addEventListener("click", () => prepareRetry(article._message, article._run, retry));
    failure.append(title, detail, note, retry); body.append(failure);
  }
  article.append(header, body, messageFooter(article, content, message, key));
  return article;
}

function messageFooter(article, content, message, key) {
  const footer = element("div", "message-footer");
  if (!key.startsWith("run:")) {
    const copy = element("button", "button subtle message-action", "Copy"); copy.type = "button";
    copy.addEventListener("click", () => copyText(article._message.content || "", copy)); footer.append(copy);
    if (ownRoom(selectedRoom())) {
      const reply = element("button", "button subtle message-action", "Reply"); reply.type = "button";
      reply.dataset.requiresWrite = "true";
      let captured = null;
      reply.addEventListener("pointerdown", () => { captured = selectedQuote(content, article._message.content || ""); });
      reply.addEventListener("click", () => { if (!writableRoom(selectedRoom())) return; const cache = roomState(); cache.reply = { id: article._message.id, quote: captured ?? selectedQuote(content, article._message.content || "") }; touchComposer(cache); captured = null; renderReply(); updateComposer(); $("message-input").focus(); });
      footer.append(reply);
    }
  }
  footer.append(element("span", "message-metrics"));
  if (message.role === "assistant") {
    const usage = element("details", "message-usage");
    usage.append(element("summary", "message-usage-label"), element("div", "message-usage-body"));
    footer.append(usage);
  }
  return footer;
}

function responseUsage(message, run) {
  const usage = (activeRun(run) ? run?.usage : message.usage) || message.usage || run?.usage || {};
  const number = (value) => typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
  const input = number(usage.input_tokens ?? usage.inputTokens);
  const cached = number(usage.cached_tokens ?? usage.cachedInputTokens);
  const output = number(usage.output_tokens ?? usage.outputTokens);
  const total = number(usage.total_tokens ?? usage.totalTokens ?? message.token_usage ?? run?.token_usage);
  const credits = number(message.estimated_usage_credits_micros);
  const quota = state.myQuota;
  const capacity = number(quota?.calibration?.effective_capacity_credits_micros ?? quota?.calibration?.capacity_credits_micros);
  const start = timestamp(run?.started_at ?? run?.created_at);
  const end = timestamp(run?.finished_at ?? run?.completed_at);
  const windowStart = timestamp(quota?.window?.starts_at);
  const windowEnd = timestamp(quota?.window?.ends_at);
  // CODEX: Never apply this week's calibration to past or reset-spanning responses, or call it an official charge.
  const sameWindow = start > 0 && end >= start && start >= windowStart && end < windowEnd && Date.now() < windowEnd;
  const percent = credits !== null && capacity > 0 && windowStart > 0 && sameWindow &&
    quota.calibration.status === "estimated" && quota.calibration.refresh_status === "fresh" &&
    run?.model !== "gpt-5.3-codex-spark" ? credits / capacity * 100 : null;
  return { input, cached, output, total, credits, percent, active: activeRun(run) };
}

function updateResponseUsage(article, message, run) {
  const disclosure = article.querySelector(".message-usage");
  if (!disclosure) return;
  const usage = responseUsage(message, run);
  const signature = JSON.stringify(usage);
  if (disclosure.dataset.signature === signature) return;
  disclosure.dataset.signature = signature;
  const reported = [usage.input, usage.cached, usage.output, usage.total].some((value) => value !== null);
  const percentage = usage.percent === null ? "" : usage.percent > 0 && usage.percent < .0001
    ? "<0.0001" : usage.percent.toLocaleString("en-US", { maximumFractionDigits: 4 });
  const label = percentage ? `Usage · ≈${percentage}% equivalent` : usage.credits !== null
    ? `Usage · ${estimatedUnits(usage.credits)} units` : reported ? "Usage · tokens" : "Usage · awaiting report";
  disclosure.querySelector("summary").textContent = label;
  const body = disclosure.querySelector(".message-usage-body");
  const rows = element("dl", "message-usage-grid");
  const row = (name, value) => rows.append(element("dt", "", name), element("dd", "", value));
  for (const [name, value] of [["Input tokens", usage.input], ["Cached input (included above)", usage.cached],
    ["Output tokens", usage.output], ["Total tokens", usage.total]]) row(name, value === null ? "Not reported" : count(value));
  if (usage.credits !== null) row("Estimated usage units", estimatedUnits(usage.credits));
  if (percentage) row("Approximate account equivalent", `≈${percentage}%`);
  const explanation = !reported ? "Usage has not been reported for this response. Unknown usage is not zero."
    : "Only this response's reported usage, including its model requests and tool steps. Cached input is already part of input; it is not added twice.";
  body.replaceChildren(element("strong", "", "This response"), rows, element("p", "", explanation));
  if (usage.active) body.append(element("p", "", "Still running · counts update as usage is reported."));
  if (usage.credits !== null) body.append(element("p", "", percentage
    ? "Account equivalent uses this response's estimated units and the current weekly calibration. It is not an official per-response charge or the settled share shown in Administration; it can change as calibration improves."
    : "Units are an estimate, not an official charge. A comparable current-week account percentage is not available for this response."));
}

function updateMessageNode(article, message, run = null) {
  article._message = message;
  article._run = message.role === "assistant" ? run : null;
  run = article._run;
  const presentation = answerPresentation(message, run);
  article.classList.toggle("failed", presentation.failed);
  article.classList.toggle("empty-answer", presentation.emptyFailure);
  const content = article.querySelector(".message-content");
  const text = presentation.text;
  const selection = window.getSelection();
  const beingSelected = selection && !selection.isCollapsed && (content.contains(selection.anchorNode) || content.contains(selection.focusNode));
  if (article._rendered !== text) {
    if (beingSelected) state.selectionRefreshPending = true;
    else { content.replaceChildren(markdown(text)); article._rendered = text; }
  }
  content.hidden = !text.trim();
  const quote = article.querySelector(".message-quote");
  quote.hidden = !message.reply_quote;
  if (message.reply_quote) { quote.textContent = message.reply_quote; quote.dir = direction(message.reply_quote); }
  const attachments = article.querySelector(".message-attachments");
  const files = message.attachments || [];
  const signature = JSON.stringify(files);
  if (article._files !== signature) {
    attachments.replaceChildren();
    for (const file of files) {
      const url = safeLink(file.url || `/api/attachments/${encodeURIComponent(file.id)}`, true);
      if (!url) continue;
      const link = element("a", "message-attachment"); link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer";
      if (imageAttachment(file)) {
        const image = element("img"); image.src = url; image.alt = file.name || "Attached image"; image.loading = "lazy";
        const renderedRoom = state.selected;
        image.addEventListener("load", () => {
          if (state.view === "chat" && state.selected === renderedRoom && image.isConnected && state.follow && !selectedWithin($("timeline"))) scrollBottom();
        });
        link.append(image, element("span", "", file.name || "Image"));
      } else {
        link.classList.add("message-file"); link.download = file.name || "attachment";
        const info = element("span", "message-file-info");
        info.append(element("strong", "", file.name || "File"), element("small", "", `${fileSize(file.size)} · Download`));
        link.append(fileBadge(file), info);
      }
      attachments.append(link);
    }
    attachments.hidden = !attachments.childNodes.length;
    article._files = signature;
  }
  const metrics = [];
  if (presentation.assistant) {
    if (!presentation.emptyFailure) {
      const startedAt = timestamp(run?.started_at ?? run?.created_at);
      const finishedAt = timestamp(run?.finished_at ?? run?.completed_at);
      const elapsed = message.duration_seconds ?? run?.duration_seconds ?? (startedAt ? ((finishedAt || (activeRun(run) ? Date.now() : 0)) - startedAt) / 1000 : null);
      const duration = formatDuration(elapsed);
      if (duration) metrics.push(`${activeRun(run) && (runConnection(run) || !state.connected) ? "Elapsed " : ""}${duration}`);
    }
    const tokens = message.token_usage ?? run?.token_usage ?? run?.usage?.total_tokens ?? run?.usage?.totalTokens;
    if (tokens !== undefined && tokens !== null && (!presentation.emptyFailure || numeric(tokens) > 0)) metrics.push(`${count(tokens)} tokens`);
  }
  article.querySelector(".message-metrics").textContent = metrics.join(" · ");
  updateResponseUsage(article, message, run);
  for (const action of article.querySelectorAll(".message-action")) action.hidden = !text.trim();
  const footer = article.querySelector(".message-footer");
  footer.hidden = !metrics.length && !footer.querySelector(".message-usage") && ![...footer.querySelectorAll("button")].some((button) => !button.hidden);
  const failure = article.querySelector(".run-failure");
  if (failure) {
    failure.hidden = !presentation.failed;
    if (presentation.failed) {
      const title = presentation.status === "interrupted" ? "Response interrupted" : "Response failed";
      const detail = presentation.error || "This attempt ended before the response could be completed.";
      const titleNode = failure.querySelector(".failure-title");
      const detailNode = failure.querySelector(".run-error");
      if (titleNode.textContent !== title) titleNode.textContent = title;
      if (detailNode.textContent !== detail) detailNode.textContent = detail;
      detailNode.dir = direction(detail);
      const editable = writableRoom(selectedRoom());
      failure.querySelector(".retry-button").hidden = !editable;
      failure.querySelector(".retry-note").hidden = !editable;
    }
  }
  updateActivityPanel(article.querySelector(".run-activity"), presentation.assistant ? run : null);
  const uncertainUpdates = presentation.assistant ? (run?.steering_receipts || []).filter((receipt) => receipt.status === "uncertain") : [];
  let steeringNotice = article.querySelector(".steering-notice");
  if (uncertainUpdates.length && !steeringNotice) {
    steeringNotice = element("p", "steering-notice");
    article.querySelector(".message-body").append(steeringNotice);
  }
  if (steeringNotice) {
    steeringNotice.hidden = !uncertainUpdates.length;
    const label = `Delivery of ${uncertainUpdates.length} ${uncertainUpdates.length === 1 ? "update" : "updates"} is unconfirmed. It may already be applied; avoid sending it again as a new message.`;
    if (steeringNotice.textContent !== label) steeringNotice.textContent = label;
  }
  window.CodexControls?.revealReply(article, presentation);
}

function renderTimeline(force = false) {
  const timeline = $("timeline");
  const cache = roomState();
  const follow = state.follow && nearBottom() && !selectedWithin(timeline);
  if (force) timeline.replaceChildren();
  const existing = new Map([...timeline.children].map((node) => [node.dataset.key, node]));
  const desired = [], arrivals = [];
  for (const message of cache.messages) {
    const key = `message:${message.id}`;
    let node = existing.get(key);
    if (!node && message.role === "assistant" && message.run_id) {
      node = existing.get(`run:${message.run_id}`);
      if (node) {
        // CODEX: Completing a response must not replace a selected live log or its open details.
        node.dataset.key = key;
        node.querySelector(".message-footer").replaceWith(messageFooter(node, node.querySelector(".message-content"), message, key));
      }
    }
    if (!node) node = messageNode(message, key);
    const arrivalKey = `${state.selected}:${idOf(message.id)}`;
    const origin = message.role === "user" ? outgoingMessageOrigins.get(arrivalKey) : null;
    if (origin) { node.classList.add("message-send-staging"); arrivals.push({ node, origin, key: arrivalKey }); }
    updateMessageNode(node, message, messageRun(message, cache)); desired.push(node);
  }
  for (const run of cache.runs.values()) {
    if (!activeRun(run)) continue;
    const key = `run:${run.id}`;
    const message = { role: "assistant", content: run.output_text || "", created_at: run.created_at, token_usage: run.token_usage };
    const node = existing.get(key) || messageNode(message, key);
    updateMessageNode(node, message, run); desired.push(node);
  }
  const keys = new Set(desired.map((node) => node.dataset.key));
  for (const node of [...timeline.children]) { if (!keys.has(node.dataset.key)) node.remove(); }
  let position = timeline.firstChild;
  for (const node of desired) { if (node === position) position = position.nextSibling; else timeline.insertBefore(node, position); }
  const empty = !cache.messages.length && !activeRuns(cache).length && !cache.loading;
  $("empty-state").hidden = !empty;
  $("empty-state").querySelector("h2").textContent = state.selected ? "What are we working on?" : "Your next idea starts here";
  $("empty-state").querySelector("p").textContent = state.selected ? (writableRoom(selectedRoom()) ? "Send a message to start this room’s conversation." : "This room has no messages yet.") : "Create a room to start a conversation. Your chats stay in the shared workspace.";
  $("empty-new-room").hidden = !!state.selected;
  $("room-loading").hidden = !cache.loading;
  $("timeline-scroll").setAttribute("aria-busy", String(cache.loading));
  $("history-toolbar").hidden = !cache.more || !cache.messages.length;
  if (!selectedWithin(timeline)) state.selectionRefreshPending = false;
  if (follow || force) scrollBottom();
  if (arrivals.length) requestAnimationFrame(() => {
    for (const arrival of arrivals) {
      outgoingMessageOrigins.delete(arrival.key);
      if (!arrival.node.isConnected) continue;
      const box = arrival.node.getBoundingClientRect();
      const dx = Math.max(-220, Math.min(220, arrival.origin.x - (box.left + box.width / 2)));
      const dy = Math.max(-280, Math.min(280, arrival.origin.y - (box.top + box.height / 2)));
      arrival.node.style.setProperty("--message-send-x", `${dx}px`);
      arrival.node.style.setProperty("--message-send-y", `${dy}px`);
      arrival.node.classList.remove("message-send-staging");
      arrival.node.classList.add("message-send-arrive");
      setTimeout(() => {
        arrival.node.classList.remove("message-send-arrive");
        arrival.node.style.removeProperty("--message-send-x");
        arrival.node.style.removeProperty("--message-send-y");
      }, 3150);
    }
  });
}

function flushDeferredSelectionRender() {
  if (!state.selectionRefreshPending || !state.selected || state.view !== "chat") return;
  if (selectedWithin($("timeline"))) return;
  state.selectionRefreshPending = false;
  renderTimeline();
}

function renderCurrent(force = false) {
  window.dispatchEvent(new Event("codex-room-render"));
  renderHeader(); renderRooms();
  updateConnection();
  if (state.view !== "chat") return;
  if (!state.selected) {
    $("timeline").replaceChildren(); $("empty-state").hidden = false; $("empty-new-room").hidden = false;
    $("empty-state").querySelector("p").textContent = "Create a room to start a conversation. Your chats stay in the shared workspace.";
    $("history-toolbar").hidden = true; $("room-loading").hidden = true;
    $("timeline-scroll").setAttribute("aria-busy", "false");
  } else { renderTimeline(force); }
  renderModelControls(); renderReply(); renderAttachments(); updateComposer();
}

function renderModelControls() {
  const room = selectedRoom();
  if (!room) return;
  const models = orderedModels(state.models);
  const running = activeRuns(roomState(room.id)).sort((left, right) => timestamp(left.created_at) - timestamp(right.created_at) || idOf(left.id).localeCompare(idOf(right.id)));
  const snapshot = running.at(-1) || null;
  const recordedModel = snapshot?.model || room.model;
  const verified = state.models.filter((model) => model.availabilityVerified !== false);
  const preferred = matchingModel(recordedModel) || verified.find((model) => model.isDefault) || verified[0];
  const selected = recordedModel ? (matchingModel(recordedModel) ? modelCode(matchingModel(recordedModel)) : recordedModel) : preferred ? modelCode(preferred) : "";
  const modelSelect = $("model-select");
  const signature = JSON.stringify(models.map((model) => [model.id, model.model, model.displayName, model.name, model.description, model.availabilityVerified]));
  if (modelSelect.dataset.signature !== signature || ![...modelSelect.options].some((option) => option.value === selected)) {
    modelSelect.replaceChildren();
    let group;
    for (const model of models) {
      const label = modelGroup(model);
      if (group?.label !== label) { group = document.createElement("optgroup"); group.label = label; modelSelect.append(group); }
      const option = new Option(model.displayName || model.name || model.id, modelCode(model));
      option.title = model.availabilityVerified === false
        ? "Access not confirmed by this Codex installation. The provider may reject this model." : model.description || "";
      group.append(option);
    }
    if (!models.length) modelSelect.append(new Option("Models unavailable", ""));
    else if (!matchingModel(selected)) modelSelect.append(new Option(`${selected} (unavailable)`, selected));
    modelSelect.dataset.signature = signature;
  }
  modelSelect.value = selected;
  const metadata = matchingModel(selected);
  const supported = metadata?.supportedReasoningEfforts || [];
  const hasSnapshotEffort = !!snapshot && Object.hasOwn(snapshot, "effort");
  const recordedEffort = String(hasSnapshotEffort ? snapshot.effort || "" : room.effort || "");
  const supportedValues = supported.map(effortCode);
  const preserveRecordedEffort = hasSnapshotEffort || !!room.effort;
  const effort = preserveRecordedEffort ? recordedEffort : metadata?.defaultReasoningEffort || effortCode(supported[0]);
  const effortSelect = $("effort-select");
  const effortSignature = JSON.stringify([supported, selected, preserveRecordedEffort ? recordedEffort : null]);
  if (effortSelect.dataset.signature !== effortSignature) {
    effortSelect.replaceChildren();
    for (const item of supported) {
      const value = effortCode(item);
      const option = new Option(effortLabel(value), value || ""); option.title = typeof item === "object" ? item.description || "" : ""; effortSelect.append(option);
    }
    if (preserveRecordedEffort && !supportedValues.includes(recordedEffort)) {
      const option = new Option(effortLabel(recordedEffort), recordedEffort);
      option.title = snapshot ? "Used by the response in progress" : "Saved for this room";
      effortSelect.append(option);
    }
    if (!effortSelect.options.length) effortSelect.append(new Option("Default", ""));
    effortSelect.dataset.signature = effortSignature;
  }
  effortSelect.value = effort;
  const recordedSpeed = snapshot?.speed || room.speed;
  const snapshotFast = !!snapshot && recordedSpeed === "fast";
  $("speed-select").value = (metadata?.supportsFast === true || snapshotFast) && recordedSpeed === "fast" ? "fast" : "standard";
  $("speed-select").querySelector('option[value="fast"]').disabled = metadata?.supportsFast !== true && !snapshotFast;
  $("tool-read-only").checked = room.tool_read_only === true;
}

let enterPreference = { deviceId: null, double: true };

function syncEnterPreference() {
  const deviceId = idOf(state.me?.id);
  if (enterPreference.deviceId !== deviceId) {
    let saved = null;
    try { if (deviceId) saved = localStorage.getItem(`codex-lan-double-enter:${deviceId}`); } catch {}
    enterPreference = { deviceId, double: saved !== "false" };
    state.lastEnter = null;
  }
  $("double-enter-send").checked = enterPreference.double;
  return enterPreference.double;
}

function setDoubleEnterMode(enabled) {
  syncEnterPreference();
  enterPreference.double = !!enabled;
  state.lastEnter = null;
  try {
    if (enterPreference.deviceId) localStorage.setItem(`codex-lan-double-enter:${enterPreference.deviceId}`, String(enterPreference.double));
  } catch {}
  updateComposer();
}

function composerMessagePayload(cache) {
  const payload = { text: cache.draft.trim(), attachment_ids: cache.files.map((file) => file.id) };
  if (cache.reply) { payload.reply_to_id = cache.reply.id; payload.reply_quote = cache.reply.quote; }
  return payload;
}

function setComposerDraft(cache, value) {
  const draft = String(value ?? "");
  if (cache.draft === draft && typeof cache.draftHasText === "boolean") return false;
  cache.draft = draft;
  cache.draftCheck = draft;
  cache.draftHasText = /\S/u.test(draft);
  cache.composerRevision = numeric(cache.composerRevision) + 1;
  saveRoomDraft(cache.roomId, draft, cache.deviceId);
  if (embeddedRoomId) window.dispatchEvent(new Event("codex-companion-draft"));
  return true;
}

function touchComposer(cache) { cache.composerRevision = numeric(cache.composerRevision) + 1; }
function composerHasText(cache) {
  if (!cache) return false;
  if (cache.draftCheck !== cache.draft || typeof cache.draftHasText !== "boolean") {
    cache.draftCheck = cache.draft;
    cache.draftHasText = /\S/u.test(String(cache.draft || ""));
  }
  return cache.draftHasText;
}

function composerIntentPreview(cache) {
  const runs = activeRuns(cache);
  const toolReadOnly = $("tool-read-only").checked;
  const pendingSend = cache.pendingSend;
  const pendingRevision = pendingSend?.composerRevision === cache.composerRevision;
  let pending = null;
  let retryingOriginal = false;
  if (pendingRevision) {
    const content = composerMessagePayload(cache);
    if (pendingSend.mode === "steer") {
      const fingerprint = JSON.stringify({ ...content, tool_read_only: toolReadOnly });
      if (pendingSend.fingerprint === fingerprint) pending = pendingSend;
    } else if (pendingSend.mode === "new") {
      const originalFingerprint = JSON.stringify({ ...content, model: $("model-select").value, effort: $("effort-select").value || null, speed: $("speed-select").value || "standard", tool_read_only: toolReadOnly });
      retryingOriginal = pendingSend.fingerprint === originalFingerprint;
    }
  }
  const runId = retryingOriginal ? null : pending?.runId || runs[0]?.id;
  const targetRun = runs.find((run) => idOf(run.id) === idOf(runId));
  const modeChanged = pending?.modeChanged === true || (typeof targetRun?.tool_read_only === "boolean" && targetRun.tool_read_only !== toolReadOnly);
  return { pending, runId, steering: !!runId, runs, modeChanged };
}

function composerSendIntent(cache) {
  const content = composerMessagePayload(cache);
  const toolReadOnly = $("tool-read-only").checked;
  const fingerprint = JSON.stringify({ ...content, tool_read_only: toolReadOnly });
  const pending = cache.pendingSend?.mode === "steer" && cache.pendingSend.fingerprint === fingerprint ? cache.pendingSend : null;
  const originalFingerprint = JSON.stringify({ ...content, model: $("model-select").value, effort: $("effort-select").value || null, speed: $("speed-select").value || "standard", tool_read_only: toolReadOnly });
  const retryingOriginal = cache.pendingSend?.mode === "new" && cache.pendingSend.fingerprint === originalFingerprint;
  const runs = activeRuns(cache);
  // CODEX: A lost initial-submit response is still an idempotent submit retry, not a new follow-up.
  const runId = retryingOriginal ? null : pending?.runId || runs[0]?.id;
  const targetRun = runs.find((run) => idOf(run.id) === idOf(runId));
  const modeChanged = pending?.modeChanged === true || (typeof targetRun?.tool_read_only === "boolean" && targetRun.tool_read_only !== toolReadOnly);
  return { content, fingerprint, pending, runId, steering: !!runId, runs, toolReadOnly, modeChanged };
}

function ownQuotaBlockReason() {
  if (isAdmin()) return "";
  const report = state.myQuota?.device;
  const share = report && Object.hasOwn(report, "allocated_percent") ? knownNumber(report.allocated_percent) : knownNumber(state.me?.weekly_share_percent);
  if (share === null) return "";
  if (!report || idOf(report.device_id) !== idOf(state.me?.id)) return "calibration_unavailable";
  return report.strict_blocked === true ? report.block_reason || "calibration_unavailable" : "";
}

function quotaBlockMessage(reason) {
  return {
    disabled: "New responses are blocked because your weekly share is set to 0%. Existing responses can finish.",
    exhausted: "Your weekly share is used up. New responses are blocked until the allowance resets or your administrator changes it.",
    unknown_usage: "Some recorded work has incomplete usage data. Incomplete usage by itself does not block new responses.",
    calibration_unavailable: "The weekly estimate is not currently reliable. An unavailable estimate by itself does not block new responses.",
  }[reason] || "New responses are blocked by your weekly usage limit.";
}

function composerSubmissionState(room, cache, doubleEnter) {
  const intent = composerIntentPreview(cache);
  const running = intent.runs;
  const modelMetadata = matchingModel($("model-select").value);
  return {
    room, cache, doubleEnter, intent, running, modelMetadata,
    editable: writableRoom(room) && state.view === "chat",
    unavailable: !state.connected || !state.workerOnline || cache.loading,
    loading: cache.loading,
    quotaBlock: ownQuotaBlockReason(),
    validModel: !!modelMetadata,
    badFiles: cache.files.some((file) => file.status !== "ready"),
    toolReadOnlySaving: cache.toolReadOnlySaving === true,
    stopping: running.some((run) => run.status === "stopping"),
  };
}

function renderComposerSubmission(view) {
  const { cache, doubleEnter, editable, intent, running, unavailable, loading, quotaBlock, validModel, badFiles, toolReadOnlySaving, stopping } = view;
  $("send-button").disabled = !editable || unavailable || !!quotaBlock || (!intent.steering && !validModel) || cache.sending || toolReadOnlySaving || stopping || running.length > 1 || badFiles || (!composerHasText(cache) && !cache.files.length && !intent.modeChanged);
  $("send-button").dataset.sending = String(cache.sending);
  $("send-button").setAttribute("aria-busy", String(cache.sending));
  const sendLabel = intent.pending?.uncertain ? "Check update" : intent.steering ? "Send update" : "Send message";
  const accessMode = $("tool-read-only").checked ? "Read only" : "Full access";
  $("send-button").dataset.label = intent.steering ? sendLabel : "Send";
  $("send-button").dataset.mode = intent.steering ? "steer" : "new";
  $("send-button").setAttribute("aria-label", sendLabel);
  $("send-button").title = intent.steering ? `${sendLabel} to apply ${accessMode} to this response without starting a new prompt` : "Send message";
  $("stop-button").hidden = !running.length;
  $("stop-button").disabled = !editable || !state.connected || running.every((run) => run.status === "stopping");
  $("composer-hint").textContent = loading ? "Loading recent messages…" : toolReadOnlySaving ? "Saving room access mode…" : quotaBlock ? quotaBlockMessage(quotaBlock) : badFiles ? "Wait for files to upload, or remove failed attachments" : cache.retryNotice || (stopping ? "Stopping this response · Your draft is kept" : intent.steering ? `Send update to apply ${accessMode} to this response · ${doubleEnter ? "Enter twice" : "Enter"} to send` : !validModel ? "Waiting for the server’s model list" : `${doubleEnter ? "Enter twice" : "Enter"} to send · Shift+Enter for a new line`);
}

function renderDraftMeter(cache) {
  const meter = $("draft-meter");
  const text = String(cache?.draft || "");
  const characters = text.length;
  meter.hidden = characters < 24;
  if (meter.hidden) { meter.textContent = ""; return; }
  const nonAscii = (text.match(/[^\u0000-\u007f]/g) || []).length;
  const estimatedTokens = Math.max(1, Math.ceil((characters - nonAscii) / 4 + nonAscii / 2));
  meter.textContent = `${formatNumber.format(characters)} chars · ≈${formatNumber.format(estimatedTokens)} tokens`;
}

function updateComposerDraftState() {
  const room = selectedRoom();
  if (!room) return;
  const cache = roomState();
  renderDraftMeter(cache);
  renderComposerSubmission(composerSubmissionState(room, cache, syncEnterPreference()));
  scheduleComposerResize();
}

function updateComposer() {
  const doubleEnter = syncEnterPreference();
  const room = selectedRoom();
  const editable = writableRoom(room) && state.view === "chat";
  const archived = !!room?.read_only && state.view === "chat";
  $("chat-view").classList.toggle("read-only", !editable);
  $("read-only-notice").hidden = !archived;
  $("read-only-reason").textContent = room?.read_only_reason || "This room’s history is preserved. Start a new room to use server-managed responses.";
  const cache = room ? roomState() : null;
  renderDraftMeter(cache);
  $("read-only-copy-draft").hidden = !archived || !composerHasText(cache);
  for (const action of $("timeline").querySelectorAll("[data-requires-write]")) action.disabled = !editable;
  $("composer-area").hidden = !editable;
  $("inspector-notice").hidden = !room || ownRoom(room) || archived || state.view !== "chat";
  if (!room) {
    $("model-select").disabled = true;
    $("effort-select").disabled = true;
    $("speed-select").disabled = true;
    $("tool-read-only").setAttribute("disabled", "");
    return;
  }
  if (!ownRoom(room)) {
    const model = matchingModel(room.model);
    const modelName = model?.displayName || model?.name || room.model || "Model not recorded";
    const effort = room.effort ? ` · Thinking: ${effortLabel(room.effort)}` : "";
    $("inspector-notice").textContent = `This room belongs to another device. You can inspect its history. Model: ${modelName}${effort}.`;
  }
  const submission = composerSubmissionState(room, cache, doubleEnter);
  if (document.activeElement !== $("message-input") && $("message-input").value !== cache.draft) $("message-input").value = cache.draft;
  $("message-input").disabled = !editable || cache.sending;
  $("attach-button").disabled = !editable || cache.sending || submission.unavailable;
  for (const action of $("attachment-list").querySelectorAll("button[data-local-id]")) action.disabled = !editable || cache.sending;
  renderComposerSubmission(submission);
  $("model-select").disabled = !editable || !!submission.running.length || cache.sending || cache.loading || cache.toolReadOnlySaving || !state.models.length;
  $("effort-select").disabled = $("model-select").disabled || $("effort-select").options.length <= 1;
  const modelMetadata = submission.modelMetadata;
  let availabilityNotice = $("model-availability-notice");
  if (!availabilityNotice) {
    availabilityNotice = element("span", "model-availability-notice");
    availabilityNotice.id = "model-availability-notice";
    availabilityNotice.setAttribute("role", "status");
    $("speed-notice").after(availabilityNotice);
  }
  availabilityNotice.hidden = modelMetadata?.availabilityVerified !== false;
  availabilityNotice.textContent = availabilityNotice.hidden ? ""
    : "Model access not confirmed by this Codex installation · the provider may reject the request.";
  if ($("model-select").title !== availabilityNotice.textContent) $("model-select").title = availabilityNotice.textContent;
  $("speed-select").disabled = $("model-select").disabled || modelMetadata?.supportsFast !== true;
  $("tool-read-only").disabled = !editable || cache.sending || cache.loading || cache.toolReadOnlySaving;
  const accessMode = $("tool-read-only").checked ? "Read only" : "Changes allowed";
  $("tool-read-only-detail").textContent = submission.running.length ? "Send update to apply" : ($("tool-read-only").checked ? "No changes" : "Changes allowed");
  $("tool-read-only").closest("label").title = submission.running.length
    ? `${accessMode} is selected. Send an update to apply it to the response in progress.`
    : ($("tool-read-only").checked ? "Inspect only. Codex cannot modify files or the computer for the next response." : "Codex may modify files or the computer for the next response.");
  const fast = $("speed-select").value === "fast";
  const multiplier = knownNumber(modelMetadata?.fastUsageMultiplier);
  $("speed-select").title = modelMetadata?.supportsFast === true ? "Fast uses more of the shared allowance. Availability depends on the account." : "Fast is not supported by this model";
  $("speed-notice").hidden = !fast;
  $("speed-notice").textContent = fast ? `Fast: ${multiplier ? `${multiplier}× estimated usage weight` : "higher estimated usage weight"} · Availability depends on your account` : "";
  scheduleComposerResize();
}

let composerResizeFrame = null;
function scheduleComposerResize() {
  if (composerResizeFrame !== null) return;
  composerResizeFrame = requestAnimationFrame(() => { composerResizeFrame = null; resizeComposer(); });
}
function resizeComposer() { const input = $("message-input"); const compactEmbedded = document.documentElement.dataset.embedded === "true" && innerWidth <= 480; input.style.height = "auto"; input.style.height = `${Math.min(Math.max(input.scrollHeight, compactEmbedded ? 60 : 68), compactEmbedded ? 160 : 220)}px`; input.dir = direction(input.value); }
function renderReply() { const reply = state.selected ? roomState().reply : null; $("reply-preview").hidden = !reply; $("reply-text").textContent = reply?.quote || ""; $("reply-text").dir = direction(reply?.quote); }

function imageAttachment(file) { return ["image/png", "image/jpeg", "image/gif", "image/webp"].includes(file.mime || file.type); }
function fileBadge(file) {
  const badge = element("span", "attachment-file-icon", (String(file.name || "").split(".").pop() || "FILE").slice(0, 6).toUpperCase());
  badge.setAttribute("aria-hidden", "true");
  return badge;
}
function fileSize(size) { return size >= 1024 * 1024 ? `${(size / (1024 * 1024)).toFixed(1)} MiB` : size >= 1024 ? `${Math.ceil(size / 1024)} KiB` : `${size || 0} B`; }

function renderAttachments() {
  const list = $("attachment-list");
  list.replaceChildren();
  if (!state.selected) return;
  for (const file of roomState().files) {
    const card = element("div", `attachment-card${file.status === "failed" ? " failed" : ""}`);
    const image = imageAttachment(file) && file.preview ? element("img") : fileBadge(file);
    if (image.tagName === "IMG") { image.src = file.preview; image.alt = ""; }
    const info = element("div"); info.append(element("strong", "", file.name), element("small", "", file.status === "ready" ? imageAttachment(file) ? "Ready" : `${fileSize(file.size)} · Ready` : file.status === "failed" ? file.error || "Upload failed" : "Uploading…"));
    const remove = element("button", "icon-button", "×"); remove.type = "button"; remove.dataset.localId = file.localId; remove.setAttribute("aria-label", `Remove ${file.name}`);
    const roomId = state.selected;
    remove.disabled = roomState(roomId).sending;
    remove.addEventListener("click", () => {
      const cache = roomState(roomId);
      if (cache.sending) return;
      const removedIndex = cache.files.findIndex((item) => item.localId === file.localId);
      file.discardRequested = true;
      cache.files = cache.files.filter((item) => item.localId !== file.localId);
      const nextFile = cache.files[Math.min(Math.max(removedIndex, 0), cache.files.length - 1)];
      touchComposer(cache); discardUploadedFile(file); releasePreview(file); renderAttachments(); updateComposer();
      const nextRemove = nextFile ? [...list.querySelectorAll("button")].find((button) => button.dataset.localId === nextFile.localId) : null;
      (nextRemove || $("attach-button")).focus({ preventScroll: true });
    });
    card.append(image, info, remove); list.append(card);
    if (file.effectPhase) {
      const phase = file.effectPhase;
      delete file.effectPhase;
      window.CodexControls?.attachmentFeedback(card, phase, file.name);
    }
  }
}

function requestId() {
  if (globalThis.crypto?.randomUUID) return crypto.randomUUID();
  if (!globalThis.crypto?.getRandomValues) throw new Error("This browser cannot create secure request IDs. Use a current browser.");
  const bytes = crypto.getRandomValues(new Uint8Array(16)); bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
  const hex = [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

async function addFiles(files) {
  if (!writableRoom(selectedRoom())) return;
  const roomId = state.selected;
  const cache = roomState(roomId);
  for (const source of files) {
    if (cache.released) break;
    if (source.size > 25 * 1024 * 1024) { toast("Each file must be no larger than 25 MiB."); continue; }
    if (cache.files.length >= 10) { toast("You can attach up to 10 files to one message."); break; }
    const file = { localId: requestId(), name: source.name || "attachment", mime: source.type, size: source.size, preview: imageAttachment(source) ? URL.createObjectURL(source) : null, status: "uploading", effectPhase: "arrive" };
    cache.files.push(file); touchComposer(cache); renderAttachments(); updateComposer();
    const form = new FormData(); form.append("file", source, file.name);
    try {
      file.uploadPromise = api("/api/attachments", { method: "POST", body: form, timeout: 60000 });
      const result = await file.uploadPromise;
      const uploaded = result.attachment || result;
      if (!uploaded.id) throw new Error("The server did not return an attachment ID.");
      Object.assign(file, uploaded, { status: "ready", effectPhase: "ready" });
      if (imageAttachment(file) && !file.preview) file.preview = URL.createObjectURL(source);
    } catch (error) { file.status = "failed"; file.error = error.message; file.effectPhase = "failed"; }
    finally { file.uploadPromise = null; }
    if (file.discardRequested || !cache.files.includes(file)) {
      discardUploadedFile(file);
      releasePreview(file);
      continue;
    }
    touchComposer(cache);
    if (state.selected === roomId) { renderAttachments(); updateComposer(); }
  }
}

async function sendMessage() {
  if ($("send-button").disabled || !state.selected || state.view !== "chat" || !writableRoom(selectedRoom())) return;
  const quotaBlock = ownQuotaBlockReason();
  if (quotaBlock) { toast(quotaBlockMessage(quotaBlock)); updateComposer(); return; }
  enableReplyNotifications({ automatic: true });
  const roomId = state.selected;
  const focusRevision = state.focusRevision;
  const cache = roomState(roomId);
  const intent = composerSendIntent(cache);
  const model = $("model-select").value;
  const metadata = matchingModel(model);
  const supported = metadata?.supportedReasoningEfforts || [];
  const selectedEffort = $("effort-select").value;
  const effort = supported.some((item) => effortCode(item) === selectedEffort)
    ? selectedEffort : metadata?.defaultReasoningEffort || effortCode(supported[0]) || null;
  const speed = metadata?.supportsFast === true && $("speed-select").value === "fast" ? "fast" : "standard";
  const payload = intent.steering ? { ...intent.content, run_id: intent.runId, tool_read_only: intent.toolReadOnly } : { ...intent.content, model, effort, speed, tool_read_only: intent.toolReadOnly };
  const fingerprint = intent.steering ? intent.fingerprint : JSON.stringify(payload);
  const mode = intent.steering ? "steer" : "new";
  if (!cache.pendingSend || cache.pendingSend.fingerprint !== fingerprint || cache.pendingSend.mode !== mode) cache.pendingSend = { fingerprint, id: requestId(), mode, runId: intent.runId || null, composerRevision: cache.composerRevision, modeChanged: intent.modeChanged === true };
  cache.pendingSend.composerRevision = cache.composerRevision;
  payload.request_id = cache.pendingSend.id;
  const sendBox = $("send-button").getBoundingClientRect();
  const sendOrigin = { x: sendBox.left + sendBox.width / 2, y: sendBox.top + sendBox.height / 2 };
  if (!intent.steering) Object.assign(selectedRoom(), { model, effort, speed });
  if (state.selected === roomId) playSendFeedback();
  cache.sending = true; updateComposer();
  try {
    const result = await api(`/api/rooms/${encodeURIComponent(roomId)}/${intent.steering ? "steer" : "runs"}`, { method: "POST", body: JSON.stringify(payload), timeout: 45000 });
    if (result.room) mergeRoom(result.room);
    if (intent.steering && result.steering?.status === "uncertain") {
      cache.pendingSend.uncertain = true;
      cache.retryNotice = "Update delivery is unconfirmed. Your draft is kept; Check update will not resend it.";
      toast(result.steering.error || cache.retryNotice);
      if (state.selected === roomId) scheduleEvents(100);
      return;
    }
    if (intent.steering && !result.user_message) throw new Error("The server did not confirm the update.");
    if (result.user_message) {
      mergeMessage(cache, result.user_message);
      if (!intent.steering && state.selected === roomId) outgoingMessageOrigins.set(`${roomId}:${idOf(result.user_message.id)}`, sendOrigin);
    }
    // CODEX: Merge both ordinary starts and access-mode handoffs without erasing streamed output.
    if (result.run) {
      const key = idOf(result.run.id), previous = cache.runs.get(key);
      if (!previous || activeRun(previous)) cache.runs.set(key, { status: "queued", ...previous, ...result.run, id: key, output_text: previous?.output_text || result.run.output_text || "" });
    }
    if (result.previous_run_id && idOf(result.previous_run_id) !== idOf(result.run?.id)) {
      const previous = cache.runs.get(idOf(result.previous_run_id));
      if (activeRun(previous)) previous.status = "interrupted";
    }
    setComposerDraft(cache, ""); forgetRoomDraft(roomId, cache.deviceId); cache.reply = null; cache.pendingSend = null; cache.retryNotice = "";
    for (const file of cache.files) releasePreview(file);
    cache.files = [];
    if (state.selected === roomId) {
      $("message-input").value = ""; state.follow = true; renderCurrent(); scrollBottom(); scheduleEvents(100);
    }
    refreshRooms().catch(() => {});
  } catch (error) {
    if (error.readOnly) {
      markRoomReadOnly(roomId, error.message);
      toast("This earlier room is now read-only. Your draft is kept; start a new room to continue.");
      return;
    }
    if (intent.steering) cache.retryNotice = `${error.message} Your draft still targets the same response; it was not sent as a new prompt.`;
    toast(`${error.message} Your draft is kept; retrying the same message will reuse its request ID.`);
    if (state.selected === roomId) scheduleEvents(300);
  } finally { cache.sending = false; if (state.selected === roomId) { updateComposer(); focusComposerAfterAsync(roomId, focusRevision); } }
}

async function stopResponses() {
  if (!state.selected || state.view !== "chat" || !writableRoom(selectedRoom())) return;
  const roomId = state.selected;
  const runs = activeRuns(roomState(roomId));
  $("stop-button").disabled = true;
  try {
    for (const run of runs) {
      await api(`/api/runs/${encodeURIComponent(run.id)}/stop`, { method: "POST", body: "{}" });
      run.status = "stopping";
    }
    if (state.selected === roomId) { renderCurrent(); scheduleEvents(100); }
  } catch (error) { toast(error.message); updateComposer(); }
}

async function saveModelSettings(modelChanged) {
  const room = selectedRoom(); if (!writableRoom(room) || state.view !== "chat" || $("model-select").disabled) return;
  const roomId = idOf(room.id), cache = roomState(roomId);
  const model = $("model-select").value;
  const metadata = matchingModel(model);
  if (!metadata) { renderModelControls(); updateComposer(); return; }
  const supported = metadata.supportedReasoningEfforts || [];
  const fallbackEffort = metadata.defaultReasoningEffort || effortCode(supported[0]) || null;
  const selectedEffort = $("effort-select").value || null;
  const effort = modelChanged ? fallbackEffort : supported.some((item) => effortCode(item) === selectedEffort) ? selectedEffort : fallbackEffort;
  const speed = metadata.supportsFast === true && $("speed-select").value === "fast" ? "fast" : "standard";
  const old = { model: room.model, effort: room.effort, speed: room.speed };
  if (!cache.modelSettingsSave) cache.modelSettingsConfirmed = old;
  const revision = numeric(cache.modelSettingsRevision) + 1;
  cache.modelSettingsRevision = revision;
  room.model = model; room.effort = effort; room.speed = speed;
  renderModelControls(); updateComposer(); renderRooms();
  const previousSave = cache.modelSettingsSave || Promise.resolve();
  const save = previousSave.catch(() => {}).then(async () => {
    const current = state.rooms.find((item) => idOf(item.id) === roomId);
    if (!writableRoom(current)) {
      if (cache.modelSettingsRevision === revision && current && cache.modelSettingsConfirmed) Object.assign(current, cache.modelSettingsConfirmed);
      if (cache.modelSettingsRevision === revision && state.selected === roomId) { renderModelControls(); updateComposer(); renderRooms(); }
      return;
    }
    try {
      const result = await api(`/api/rooms/${encodeURIComponent(roomId)}`, { method: "PATCH", body: JSON.stringify({ model, effort, speed }) });
      const updated = result.room || result;
      cache.modelSettingsConfirmed = {
        model: updated.model ?? model,
        effort: Object.hasOwn(updated, "effort") ? updated.effort : effort,
        speed: updated.speed ?? speed,
      };
      if (cache.modelSettingsRevision !== revision) return;
      if (result.room || result.id) mergeRoom(updated);
    } catch (error) {
      const latest = state.rooms.find((item) => idOf(item.id) === roomId);
      if (cache.modelSettingsRevision === revision && latest) Object.assign(latest, cache.modelSettingsConfirmed || old);
      if (error.readOnly) markRoomReadOnly(roomId, error.message);
      toast(error.message);
    } finally {
      if (cache.modelSettingsRevision === revision && state.selected === roomId) { renderModelControls(); updateComposer(); renderRooms(); }
    }
  }).finally(() => { if (cache.modelSettingsSave === save) cache.modelSettingsSave = null; });
  cache.modelSettingsSave = save;
  return save;
}

async function saveToolReadOnly() {
  const room = selectedRoom();
  if (!writableRoom(room) || state.view !== "chat" || $("tool-read-only").disabled) return;
  const roomId = idOf(room.id), cache = roomState(roomId);
  const responseInProgress = activeRuns(cache).length > 0;
  const previous = room.tool_read_only === true;
  const next = $("tool-read-only").checked;
  room.tool_read_only = next;
  cache.toolReadOnlySaving = true;
  updateComposer();
  try {
    const result = await api(`/api/rooms/${encodeURIComponent(roomId)}`, { method: "PATCH", body: JSON.stringify({ tool_read_only: next }) });
    if (result.room || result.id) mergeRoom(result.room || result);
    const message = next ? "Read only is on · Codex can inspect, but cannot change files or the computer" : "Read only is off · Changes are allowed";
    toast(message + (responseInProgress ? " · Send update to apply this mode to the response in progress" : ""));
  } catch (error) {
    room.tool_read_only = previous;
    if (error.readOnly) markRoomReadOnly(roomId, error.message);
    toast(error.message);
  } finally {
    cache.toolReadOnlySaving = false;
    if (state.selected === roomId) { renderModelControls(); updateComposer(); }
  }
}

function selectedRoomCreationSettings(payload) {
  const verifiedModels = state.models.filter((item) => item.availabilityVerified !== false);
  const fallbackModel = verifiedModels.find((item) => item.isDefault) || verifiedModels[0];
  const selectedModel = matchingModel($("model-select").value);
  const modelMetadata = selectedModel || fallbackModel;
  const model = modelMetadata ? modelCode(modelMetadata) : "";
  if (model) payload.model = model;
  const supported = modelMetadata?.supportedReasoningEfforts || [];
  const selectedEffort = $("effort-select").value;
  const effort = supported.some((item) => effortCode(item) === selectedEffort)
    ? selectedEffort : modelMetadata?.defaultReasoningEffort || effortCode(supported[0]);
  if (effort) payload.effort = effort;
  payload.speed = modelMetadata?.supportsFast === true && $("speed-select").value === "fast" ? "fast" : "standard";
  return payload;
}

function setRoomCreationBusy(busy) {
  state.roomCreating = busy;
  for (const id of ["new-room", "empty-new-room", "read-only-new-room"]) {
    const button = $(id);
    button.disabled = busy;
    button.setAttribute("aria-busy", String(busy));
  }
}

async function createRoomNow() {
  if (embeddedRoomId) return;
  if (state.roomCreating) return;
  setRoomCreationBusy(true);
  const payload = selectedRoomCreationSettings({});
  const current = selectedRoom();
  const workspace = state.workerCwd || (ownRoom(current) ? current.cwd || "" : "");
  if (workspace) payload.cwd = workspace;
  try {
    const result = await api("/api/rooms", { method: "POST", body: JSON.stringify(payload) });
    const room = result.room || result;
    mergeRoom(room);
    await selectRoom(room.id);
    closeSidebar();
  } catch (error) {
    toast(error.message);
  } finally {
    setRoomCreationBusy(false);
  }
}

function openRoomDialog(mode = "edit", quote = "", roomId = state.selected, { route = true } = {}) {
  if (mode === "new") { void createRoomNow(); return; }
  const room = state.rooms.find((item) => idOf(item.id) === idOf(roomId));
  if (!writableRoom(room)) return;
  closeRouteDialogs("room-settings");
  const workspaceLocked = mode === "edit" && roomIsRunning(room);
  state.dialog = { mode, roomId: room?.id, quote };
  $("dialog-title").textContent = mode === "edit" ? "Room settings" : "Create a side thread";
  $("room-name-input").value = mode === "edit" ? room.title || "" : "Side thread";
  $("room-cwd-input").value = room?.cwd || state.workerCwd;
  $("room-cwd-input").disabled = workspaceLocked;
  $("room-cwd-hint").textContent = workspaceLocked
    ? "Stop the active response before changing this workspace."
    : mode === "edit"
      ? "An absolute folder on this computer. The next response uses it; this room’s history stays together."
      : "An absolute folder on this computer. The side thread starts there.";
  $("room-delete").hidden = !!embeddedRoomId || mode !== "edit";
  $("room-delete").disabled = mode === "edit" && roomIsRunning(room);
  $("room-save").textContent = mode === "edit" ? "Save changes" : "Create room";
  $("room-save").disabled = false;
  $("dialog-error").textContent = "";
  $("dialog-quote").hidden = !quote;
  $("dialog-quote").textContent = quote;
  $("dialog-quote").dir = direction(quote);
  if (!$("room-dialog").open) $("room-dialog").showModal();
  if (route) persistVisibleRoute("push");
  $("room-name-input").focus();
}

async function submitRoom(event) {
  event.preventDefault();
  const dialog = state.dialog; if (!dialog) return;
  const sourceRoom = state.rooms.find((room) => idOf(room.id) === idOf(dialog.roomId));
  if (!writableRoom(sourceRoom)) {
    $("dialog-error").textContent = "This earlier room is read-only. Create a new room to continue.";
    return;
  }
  $("room-save").disabled = true;
  const payload = { title: $("room-name-input").value.trim() || "New chat" };
  if (!$("room-cwd-input").disabled && $("room-cwd-input").value.trim()) payload.cwd = $("room-cwd-input").value.trim();
  if (dialog.mode === "side") {
    selectedRoomCreationSettings(payload);
    payload.parent_room_id = dialog.roomId;
    payload.reply_quote = dialog.quote;
    payload.tool_read_only = sourceRoom?.tool_read_only === true;
  }
  try {
    const result = await api(dialog.mode === "edit" ? `/api/rooms/${encodeURIComponent(dialog.roomId)}` : "/api/rooms", { method: dialog.mode === "edit" ? "PATCH" : "POST", body: JSON.stringify(payload) });
    const room = result.room || result; mergeRoom(room);
    $("room-dialog").close();
    if (dialog.mode === "edit") { renderCurrent(); }
    else { await selectRoom(room.id); closeSidebar(); }
  } catch (error) { if (error.readOnly && dialog.roomId) markRoomReadOnly(dialog.roomId, error.message); $("dialog-error").textContent = error.message; $("dialog-error").classList.add("error"); }
  finally { $("room-save").disabled = false; }
}

async function deleteRoom(roomId = state.dialog?.roomId, trigger = $("room-delete")) {
  const id = idOf(roomId);
  const room = state.rooms.find((item) => idOf(item.id) === id);
  if (!writableRoom(room)) return;
  if (roomIsRunning(room)) { toast("Stop this room’s active response before deleting it."); return; }
  if (!window.confirm(`Delete “${room.title || "New chat"}” and its saved messages? This cannot be undone.`)) return;
  trigger.disabled = true;
  try {
    await api(`/api/rooms/${encodeURIComponent(id)}`, { method: "DELETE" });
    if ($("room-dialog").open && idOf(state.dialog?.roomId) === id) $("room-dialog").close();
    const cache = state.roomStates.get(id);
    releaseRoomState(cache);
    forgetRoomDraft(id, cache?.deviceId || state.me?.id);
    state.roomStates.delete(id);
    state.rooms = state.rooms.filter((item) => idOf(item.id) !== id);
    const deletedSelected = state.selected === id;
    if (deletedSelected) { state.selected = null; state.selectionVersion += 1; clearTimeout(state.eventTimer); }
    await refreshRooms(deletedSelected); renderCurrent();
    toast("Room and saved messages deleted. This cannot be undone.");
  } catch (error) {
    if ($("room-dialog").open && idOf(state.dialog?.roomId) === id) $("dialog-error").textContent = error.message;
    else toast(error.message);
  }
  finally { trigger.disabled = false; }
}

async function loadOlder() {
  const id = state.selected; if (!id) return;
  const selectionVersion = state.selectionVersion;
  const cache = roomState(id); const oldest = cache.messages[0]; if (!oldest) return;
  $("load-older").disabled = true;
  const scroll = $("timeline-scroll"); const previousHeight = scroll.scrollHeight; const previousTop = scroll.scrollTop;
  try {
    const result = await api(`/api/rooms/${encodeURIComponent(id)}/messages?before=${encodeURIComponent(oldest.id)}`);
    const existingIds = new Set(cache.messages.map((message) => idOf(message.id)));
    cache.messages = [...(result.messages || []).filter((message) => !existingIds.has(idOf(message.id))), ...cache.messages];
    cache.more = result.has_more ?? result.hasMore ?? (result.messages || []).length >= 100;
    if (state.selected === id && state.view === "chat" && state.selectionVersion === selectionVersion) {
      state.follow = false;
      cache.follow = false;
      renderTimeline();
      scroll.scrollTop = previousTop + scroll.scrollHeight - previousHeight;
      cache.scrollTop = scroll.scrollTop;
    }
  } catch (error) { toast(error.message); }
  finally { $("load-older").disabled = false; }
}

async function openAdmin({ route = true, panel = "" } = {}) {
  if (embeddedRoomId) return;
  if (!isAdmin()) return;
  closeRouteDialogs();
  saveRoomViewport();
  clearTimeout(state.eventTimer);
  state.view = "admin"; state.selectionVersion += 1;
  state.adminPanel = panel === "host-setup" ? "host-setup" : "";
  const setup = state.adminPanel === "host-setup";
  $("admin-content").hidden = setup;
  $("host-setup").hidden = !setup;
  $("admin-title").textContent = setup ? "Host setup" : "Devices & usage";
  $("admin-description").textContent = setup ? "Keep this host available and connect computers on your local network." : "Approve devices, manage their limits, and see their activity.";
  $("admin-devices-open").setAttribute("aria-current", setup ? "false" : "page");
  $("host-setup-open").setAttribute("aria-current", setup ? "page" : "false");
  publishReplyView();
  $("chat-view").hidden = true; $("admin-view").hidden = false;
  renderHeader(); renderRooms(); closeSidebar();
  if (route) persistVisibleRoute("push");
  await refreshAdmin();
}

async function refreshAdmin() {
  clearTimeout(state.adminTimer);
  if (!isAdmin() || state.view !== "admin") return;
  if (state.adminPanel === "host-setup") {
    await window.CodexHostSetup?.refresh();
    if (state.view === "admin" && state.adminPanel === "host-setup") state.adminTimer = setTimeout(refreshAdmin, 8000);
    return;
  }
  $("admin-refresh").disabled = true;
  try {
    const [devicesResult, usageResult] = await Promise.all([api("/api/admin/devices"), api("/api/admin/usage")]);
    if (state.view !== "admin") return;
    const devices = devicesResult.devices || [];
    const usage = Array.isArray(usageResult.usage) ? usageResult.usage : [];
    state.adminSnapshot = { devices, usage, envelope: usageResult };
    renderAdmin(devices, usage, usageResult);
    $("admin-updated").textContent = `Updated ${timeLabel(Date.now())}`;
    $("admin-status").textContent = "Usage is recorded on the server. Model token reports may arrive after a response completes.";
    $("admin-status").classList.remove("error");
  } catch (error) { $("admin-status").textContent = error.message; $("admin-status").classList.add("error"); }
  finally {
    $("admin-refresh").disabled = false;
    if (state.view === "admin") state.adminTimer = setTimeout(() => {
      const editing = $("admin-devices").contains(document.activeElement) || $("admin-device-dialog").contains(document.activeElement);
      if (editing) state.adminTimer = setTimeout(refreshAdmin, 8000); else refreshAdmin();
    }, 8000);
  }
}

function usageTotal(row) { return numeric(row.total_tokens ?? row.token_usage ?? row.tokens ?? (numeric(row.input_tokens) + numeric(row.output_tokens))); }
function quotaReset(value) {
  const date = timeValue(value);
  if (!date) return "Reset time unavailable";
  const remaining = Math.ceil((date.getTime() - Date.now()) / 1000);
  const days = Math.floor(remaining / 86400);
  const countdown = remaining <= 0 ? "time passed; awaiting a new report" : days ? `in ${days}d ${Math.floor((remaining % 86400) / 3600)}h` : `in ${formatDuration(remaining)}`;
  return `Resets ${date.toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: "UTC", timeZoneName: "short" })} · ${countdown}`;
}

function quotaMeter(percent, label, options = {}) {
  const meter = element("div", "quota-meter"); meter.setAttribute("role", "progressbar"); meter.setAttribute("aria-label", label);
  const semanticMax = knownNumber(options.max) ?? 100;
  const semanticValue = knownNumber(options.value);
  meter.setAttribute("aria-valuemin", "0"); meter.setAttribute("aria-valuemax", String(semanticMax));
  const fill = element("span"); meter.append(fill);
  if (percent === null) { fill.style.width = "0%"; meter.classList.add("unavailable"); meter.setAttribute("aria-valuetext", "Unavailable"); }
  else {
    const capped = Math.min(100, Math.max(0, percent)); fill.style.width = `${capped}%`;
    const ariaValue = semanticValue === null ? capped : Math.min(semanticMax, Math.max(0, semanticValue));
    meter.setAttribute("aria-valuenow", String(ariaValue)); meter.setAttribute("aria-valuetext", options.valueText || `${percent}% used`);
    meter.classList.toggle("exhausted", percent >= 100);
  }
  return meter;
}

function renderProviderUsage(provider = {}) {
  const section = $("provider-usage"), status = $("provider-usage-status"), windows = $("provider-windows");
  section.classList.toggle("quota-stale", provider.stale === true);
  status.textContent = provider.available !== true ? "Official quota unavailable. Device estimates below are not a replacement for the account’s live remaining allowance."
    : provider.stale ? "Stale report — these are the last known account limits, not live remaining quota."
      : `Account-wide report${provider.fetched_at ? ` · Checked ${timeLabel(provider.fetched_at)}` : ""}. Shared by every connected device.`;
  const fragment = document.createDocumentFragment();
  for (const window of provider.available === true && Array.isArray(provider.windows) ? provider.windows : []) {
    const percent = knownNumber(window.used_percent), minutes = knownNumber(window.window_minutes);
    const duration = minutes ? minutes % 1440 === 0 ? `${minutes / 1440}-day window` : minutes % 60 === 0 ? `${minutes / 60}-hour window` : `${minutes}-minute window` : window.kind || "Usage window";
    const title = [window.limit_name || window.limit_id || "Shared account", duration].join(" · ");
    const card = element("article", "provider-window"); card.append(element("h4", "", title));
    const caption = percent === null ? "Usage unavailable" : `${Number(Math.max(0, 100 - percent).toFixed(1))}% remaining · ${Number(percent.toFixed(1))}% used`;
    card.append(element("strong", "quota-caption", caption), quotaMeter(percent, title), element("p", "quota-reset", quotaReset(window.resets_at)));
    if (window.limit_id === "codex_bengalfox") card.append(element("p", "usage-explanation", "Separate Codex-Spark allowance · not counted in regular per-device Codex shares."));
    fragment.append(card);
  }
  if (provider.available === true && !fragment.childNodes.length) fragment.append(element("p", "usage-explanation", "No percentage windows were included in the account report."));
  windows.replaceChildren(fragment);
}

function estimatedUnits(micros) {
  const value = knownNumber(micros);
  return value === null ? "Unavailable" : (value / 1000000).toLocaleString("en-US", { maximumFractionDigits: 6 });
}

function renderAllocatedQuota(pool) {
  const host = $("allocated-quota");
  if (!pool || typeof pool !== "object") { host.hidden = true; host.replaceChildren(); return; }
  host.hidden = false;
  const allocated = knownNumber(pool.allocated_percent) ?? 0;
  const used = knownNumber(pool.account_used_percent);
  const filled = knownNumber(pool.used_percent);
  const devices = numeric(pool.limited_devices);
  const lowerBound = pool.lower_bound === true || numeric(pool.unknown_runs) > 0;
  if (pool.status === "not_configured") {
    host.replaceChildren(element("strong", "quota-caption", "No active limited shares"),
      element("p", "usage-explanation", "Unlimited users are excluded. Assign a weekly percentage to at least one approved user to track this pool."));
    return;
  }
  if (pool.status === "zero_allocation") {
    host.replaceChildren(element("strong", "quota-caption", "Allocated pool: 0%"), quotaMeter(null, "Combined allocated weekly pool"),
      element("p", "usage-explanation", "All active limited users currently have a 0% share, so there is no allocated pool to measure."));
    return;
  }
  const usedLabel = used === null ? "Usage unavailable" : `${lowerBound ? "At least " : ""}${Number(used.toFixed(4))}% of the account used`;
  const filledLabel = filled === null ? "Allocated-pool usage unavailable" : `${lowerBound ? "At least " : ""}${Number(filled.toFixed(2))}% of allocated pool used`;
  const valueText = used === null || filled === null ? "Unavailable" : `${lowerBound ? "At least " : ""}${Number(used.toFixed(4))} of ${Number(allocated.toFixed(4))} account-percentage points used; ${Number(filled.toFixed(2))}% of the allocated pool`;
  const meter = quotaMeter(filled, "Combined allocated weekly pool", { value: used, max: allocated, valueText });
  const scale = element("div", "quota-meter-scale");
  scale.append(element("span", "", "0% of pool"), element("strong", "", `${usedLabel} · ${filled === null ? "fill unavailable" : `${Number(filled.toFixed(2))}% filled`}`), element("span", "", `${Number(allocated.toFixed(4))}% allocated`));
  const countLabel = devices === 1 ? "1 approved limited user" : `${count(devices)} approved limited users`;
  host.replaceChildren(element("strong", "quota-caption", filledLabel), meter, scale,
    element("p", "usage-explanation", `${Number(allocated.toFixed(4))}% of the account is allocated across ${countLabel}. Unlimited, inactive, 0%-blocked, and administrator analysis targets are excluded.${lowerBound ? " Incomplete responses make this a lower bound." : ""}`));
}

function deviceQuotaView(device, row, quota) {
  const share = row && Object.hasOwn(row, "allocated_percent") ? knownNumber(row.allocated_percent) : knownNumber(device.weekly_share_percent);
  const unknown = numeric(row?.unknown_runs);
  const blockingUnknown = row && Object.hasOwn(row, "blocking_unknown_runs") ? numeric(row.blocking_unknown_runs) : unknown;
  const acknowledged = numeric(row?.acknowledged_unreported_runs);
  const acknowledgeable = numeric(row?.acknowledgeable_unreported_runs);
  const calibrated = quota?.calibration?.status === "estimated";
  const reliable = calibrated && unknown === 0;
  const knownPercent = calibrated ? knownNumber(row?.used_percent) : null;
  const percent = reliable ? knownPercent : null;
  const partialPercent = calibrated && unknown > 0 ? knownPercent : null;
  const accountPercent = calibrated ? knownNumber(row?.account_used_percent) : null;
  const analysisOnly = row?.analysis_only === true || (device.role === "admin" && share !== null && row?.enforcement_enabled !== true);
  const hasStrictStatus = typeof row?.strict_blocked === "boolean";
  const blocked = analysisOnly ? false : hasStrictStatus ? row.strict_blocked : share === 0;
  const reason = analysisOnly ? "analysis_only" : hasStrictStatus ? row.block_reason || (blocked ? "calibration_unavailable" : "available") : share === 0 ? "disabled" : share === null ? "not_configured" : "available";
  return { share, percent, partialPercent, accountPercent, analysisOnly, blocked, reason, unknown, blockingUnknown, acknowledged, acknowledgeable, active: !analysisOnly && (row?.limit_active === true || share === 0), reliable };
}

function createAdminDevice(device) {
  const card = element("article", "admin-device"); card.dataset.deviceId = idOf(device.id);
  const head = element("div", "admin-device-header");
  const quotaBody = element("div", "device-quota");
  const actions = element("div", "admin-device-actions");
  const concurrency = limitField(`concurrency-${device.id}`, "Concurrent responses", "", "1", false);
  const share = limitField(`share-${device.id}`, "Weekly share of account (%)", "", "No weekly limit", true);
  share.input.max = "100"; share.input.step = "any"; share.wrapper.classList.add("quota-share-field");
  const budget = limitField(`budget-${device.id}`, "Token budget", "", "Unlimited", true);
  const details = element("details", "device-usage-details");
  const metrics = element("div", "admin-device-metrics");
  details.append(element("summary", "", "Usage details & advanced limits"), metrics, budget.wrapper, element("p", "usage-explanation", "Token budget is a separate lifetime limit. Leaving the weekly share blank does not remove it."));
  card._fields = { concurrency, share, budget }; card._dirty = new Set();
  for (const [key, field] of Object.entries(card._fields)) field.input.addEventListener("input", () => { card._dirty.add(key); });
  const save = element("button", "button", "Save limits"); save.type = "button";
  save.addEventListener("click", async () => {
    const max = Number(concurrency.input.value);
    const amount = budget.input.value.trim() ? Number(budget.input.value) : null;
    const allocation = share.input.value.trim() ? Number(share.input.value) : null;
    const ownAdmin = card._device.role === "admin" && idOf(card._device.id) === idOf(state.me?.id);
    if (!Number.isInteger(max) || max < 1 || max > 10 || (amount !== null && (!Number.isSafeInteger(amount) || amount < 0)) || (allocation !== null && (!Number.isFinite(allocation) || allocation < 0 || allocation > 100 || (ownAdmin && allocation === 0))) || share.input.validity.badInput || budget.input.validity.badInput) {
      toast(ownAdmin ? "Use 1–10 concurrent responses, a personal target above 0 and up to 100%, and a non-negative whole-number lifetime token budget." : "Use 1–10 concurrent responses, a weekly share from 0 to 100%, and a non-negative whole-number lifetime token budget."); return;
    }
    if (!ownAdmin && allocation !== null && allocation > 0 && !card._quotaReliable && !window.confirm("The weekly estimate is learning, stale, or incomplete. Saving this share starts tracking it now; new responses are blocked only at 0% or when a fresh calibrated measurement reaches the share. Save this limit?")) return;
    const values = Object.fromEntries(Object.entries(card._fields).map(([key, field]) => [key, field.input.value]));
    save.disabled = true;
    try {
      await api(`/api/admin/devices/${encodeURIComponent(card._device.id)}`, { method: "PATCH", body: JSON.stringify({ max_concurrent: max, token_budget: amount, weekly_share_percent: allocation }) });
      // CODEX: Keep edits made while the save was in flight; successful saves never erase newer drafts.
      for (const [key, value] of Object.entries(values)) if (card._fields[key].input.value === value) card._dirty.delete(key);
      await refreshAdmin();
      if (ownAdmin) await checkSession();
    } catch (error) { toast(error.message); }
    finally { save.disabled = false; }
  });
  const approval = element("button", "button primary"); approval.type = "button";
  approval.addEventListener("click", () => adminAction(approval, `/api/admin/devices/${encodeURIComponent(card._device.id)}`, "PATCH", { status: "approved" }));
  const reject = element("button", "button danger", "Reject"); reject.type = "button";
  reject.addEventListener("click", () => adminAction(reject, `/api/admin/devices/${encodeURIComponent(card._device.id)}`, "PATCH", { status: "rejected" }));
  const revoke = element("button", "button danger", "Revoke"); revoke.type = "button";
  revoke.addEventListener("click", () => { if (window.confirm(`Revoke access for ${card._device.name || "this device"}?`)) adminAction(revoke, `/api/admin/devices/${encodeURIComponent(card._device.id)}`, "PATCH", { status: "revoked" }); });
  const stop = element("button", "button danger", "Stop responses"); stop.type = "button";
  stop.addEventListener("click", () => adminAction(stop, `/api/admin/devices/${encodeURIComponent(card._device.id)}/stop`, "POST", {}));
  const deleteDevice = element("button", "button danger", "Delete device"); deleteDevice.type = "button";
  deleteDevice.addEventListener("click", () => deleteAdminDevice(card, deleteDevice));
  const acknowledgeUnknown = element("button", "button quota-acknowledge-button"); acknowledgeUnknown.type = "button"; acknowledgeUnknown.hidden = true;
  const acknowledgeHelp = element("p", "quota-save-note quota-acknowledge-note"); acknowledgeHelp.hidden = true;
  acknowledgeUnknown.addEventListener("click", async () => {
    const view = card._quotaView;
    if (!view || view.acknowledgeable < 1) return;
    const available = view.acknowledgeable;
    const responseLabel = `${count(available)} incomplete ${available === 1 ? "response" : "responses"}`;
    const shareLabel = view.share === null ? "No weekly share is configured" : `The ${view.share}% weekly share stays active`;
    if (!window.confirm(`Mark ${responseLabel} as reviewed for ${card._device.name || "this device"}? ${shareLabel}. This is recordkeeping only: it does not change access, erase usage, or count missing usage as zero. Incomplete usage never blocks new work by itself.`)) return;
    acknowledgeUnknown.disabled = true; acknowledgeUnknown.setAttribute("aria-busy", "true");
    try {
      const result = await api(`/api/admin/devices/${encodeURIComponent(card._device.id)}/quota/acknowledge-unknown`, { method: "POST", body: "{}" });
      await refreshAdmin();
      const acknowledged = numeric(result?.acknowledged_runs);
      toast(acknowledged ? `${count(acknowledged)} incomplete ${acknowledged === 1 ? "response was" : "responses were"} marked as reviewed. Access was unchanged; the weekly share remains active.` : "No eligible completed response needed review.");
    } catch (error) { toast(error.message); }
    finally { acknowledgeUnknown.disabled = false; acknowledgeUnknown.removeAttribute("aria-busy"); }
  });
  const note = element("p", "quota-save-note", "Blank = no weekly limit · 0% = block new responses · Changing a share keeps usage history. Running responses are not interrupted.");
  actions.append(concurrency.wrapper, share.wrapper, save, element("span", "flex-spacer"), approval, reject, revoke, stop, deleteDevice, acknowledgeUnknown, acknowledgeHelp, note);
  card.append(head, quotaBody, actions, details);
  card._nodes = { head, quotaBody, metrics, approval, reject, revoke, deleteDevice, acknowledgeUnknown, acknowledgeHelp, note };
  return card;
}

async function deleteAdminDevice(card, button) {
  const device = card._device;
  if (!device || device.role === "admin" || idOf(device.id) === idOf(state.me?.id)) return;
  const name = device.name || `Device ${device.id}`;
  const confirmed = window.confirm(`Permanently delete “${name}”? This removes this user/device, its access credentials, rooms, messages, uploaded files, usage history, and settings. This cannot be undone. Active responses must be stopped first.`);
  if (!confirmed) return;
  const originalText = button.textContent;
  button.disabled = true; button.setAttribute("aria-busy", "true"); button.textContent = "Deleting…";
  try {
    await api(`/api/admin/devices/${encodeURIComponent(device.id)}`, { method: "DELETE" });
    if (state.adminDeviceId === idOf(device.id)) {
      state.adminDeviceId = "";
      if ($("admin-device-dialog").open) $("admin-device-dialog").close();
    }
    const refreshed = await Promise.allSettled([refreshRooms(), refreshAdmin()]);
    const refreshFailed = refreshed.some((result) => result.status === "rejected");
    toast(refreshFailed ? `${name} was permanently deleted. Refresh the page to update every list.` : `${name} and its owned data were permanently deleted.`);
  } catch (error) {
    toast(error.status === 409 ? `${error.message} Stop this device’s active responses, then try again.` : `Could not delete ${name}: ${error.message}`);
  } finally {
    button.disabled = false; button.removeAttribute("aria-busy"); button.textContent = originalText;
  }
}

function renderDeviceQuota(quotaBody, device, estimate, quota) {
  const view = deviceQuotaView(device, estimate, quota);
  const blockedCaption = { disabled: "Blocked · 0% weekly share", exhausted: "Blocked · Weekly share used", unknown_usage: "Blocked · Usage incomplete", calibration_unavailable: "Blocked · Usage check unavailable" }[view.reason] || "Blocked · Weekly limit";
  const targetPercent = view.percent !== null ? view.percent : view.partialPercent;
  const deviceAccountPoints = view.accountPercent !== null ? view.accountPercent : view.share > 0 && view.percent !== null ? view.percent * view.share / 100 : null;
  const officialPercent = device.role === "admin" ? knownNumber(quota?.calibration?.provider_used_percent) : null;
  const attributionText = deviceAccountPoints === null ? "" : view.unknown ? `At least ${Number(deviceAccountPoints.toFixed(4))} account-percentage points are attributable to this device from recorded work` : `Estimated attribution to this device: ${Number(deviceAccountPoints.toFixed(4))} account-percentage points`;
  const cappedUsageCaption = view.share > 0 && targetPercent !== null && deviceAccountPoints !== null
    ? `${view.unknown ? "At least " : ""}${Number(deviceAccountPoints.toFixed(4))}% of account allowance used out of ${Number(view.share.toFixed(4))}% · ${Number(targetPercent.toFixed(2))}% of assigned share`
    : null;
  const caption = view.blocked ? blockedCaption
    : view.analysisOnly ? cappedUsageCaption || (targetPercent === null ? "Personal analysis target" : `${view.unknown ? "At least " : ""}${Number(targetPercent.toFixed(2))}% of personal target used`)
      : officialPercent !== null ? `Official shared-account meter: ${Number(officialPercent.toFixed(1))}% used`
      : view.share === null ? deviceAccountPoints === null ? "No weekly share limit" : `Estimated device attribution: ${Number(deviceAccountPoints.toFixed(4))} account-percentage points`
        : targetPercent === null ? "Usage fraction unavailable"
          : view.acknowledged ? `${cappedUsageCaption || `At least ${Number(targetPercent.toFixed(2))}% of share used`} · ${count(view.acknowledged)} incomplete ${view.acknowledged === 1 ? "response" : "responses"} acknowledged`
            : cappedUsageCaption || (view.percent === null ? `At least ${Number(targetPercent.toFixed(2))}% of share used` : `${Number(view.percent.toFixed(2))}% of share used · ${Number(Math.max(0, 100 - view.percent).toFixed(2))}% remaining`);
  const shareText = view.share === null ? deviceAccountPoints === null ? "No personal weekly cap" : `${attributionText} · No personal weekly cap`
    : deviceAccountPoints !== null ? `${attributionText} · ${view.share}% ${view.analysisOnly ? "personal target" : "allocated"}`
      : `${view.analysisOnly ? "Personal target" : "Allocated"} ${view.share}% of the account’s weekly allowance`;
  const recordedUnits = view.unknown && !numeric(estimate?.spent_credits_micros) ? "Unreported" : estimatedUnits(estimate?.spent_credits_micros);
  const partial = view.unknown && numeric(estimate?.spent_credits_micros) > 0;
  const unitText = partial ? `Known usage recorded so far: ${recordedUnits} estimated units` : `Estimated usage units: ${recordedUnits}${knownNumber(estimate?.allocated_credits_micros) !== null ? ` / ${estimatedUnits(estimate.allocated_credits_micros)} ${view.analysisOnly ? "target" : "allocated"}` : ""}`;
  const warning = view.analysisOnly ? "Personal analysis target only. It never blocks host administrator responses." : view.blocked ? quotaBlockMessage(view.reason) : view.acknowledged ? `Some incomplete responses were reviewed for reporting. This did not change access. ${view.share === null ? "Reported usage remains a lower bound" : `The ${view.share}% weekly share remains active against reported usage, which is a lower bound`}; missing usage was not erased or counted as zero.` : view.active ? "Estimated share limit active for new responses." : "Usage is tracked without a personal weekly cap.";
  const meterPercent = view.share === null && officialPercent !== null ? officialPercent : view.share === null && deviceAccountPoints !== null ? deviceAccountPoints : view.analysisOnly ? targetPercent : !view.blocked && view.acknowledged ? targetPercent : view.percent;
  const meterLabel = view.share === null && officialPercent !== null ? "Official shared-account usage" : view.share === null && deviceAccountPoints !== null ? "Estimated device attribution" : view.analysisOnly ? "Estimated personal analysis target consumed" : "Estimated weekly share consumed";
  const units = element("p", "usage-explanation", unitText); units.title = "Model, token, cache and Fast-weighted estimate; not OpenAI credits or charges.";
  const accountAxis = view.share > 0 && meterPercent !== null && deviceAccountPoints !== null;
  const meter = quotaMeter(meterPercent, meterLabel, accountAxis ? {
    value: deviceAccountPoints,
    max: view.share,
    valueText: `${view.unknown ? "At least " : ""}${Number(deviceAccountPoints.toFixed(4))} of ${Number(view.share.toFixed(4))} account-percentage points used; ${Number(meterPercent.toFixed(2))}% of the assigned share`,
  } : {});
  if (view.acknowledged && meterPercent !== null) meter.setAttribute("aria-valuetext", accountAxis
    ? `At least ${Number(deviceAccountPoints.toFixed(4))} of ${Number(view.share.toFixed(4))} account-percentage points used; ${Number(meterPercent.toFixed(2))}% of the assigned share; ${count(view.acknowledged)} incomplete ${view.acknowledged === 1 ? "response is" : "responses are"} acknowledged and actual usage may be higher`
    : `At least ${Number(meterPercent.toFixed(2))}% used from reported usage; ${count(view.acknowledged)} incomplete ${view.acknowledged === 1 ? "response is" : "responses are"} acknowledged and actual usage may be higher`);
  const meterScale = accountAxis ? element("div", "quota-meter-scale") : null;
  if (meterScale) meterScale.append(
    element("span", "", "0% account"),
    element("strong", "", `${view.unknown ? "At least " : ""}${Number(deviceAccountPoints.toFixed(4))}% used · ${Number(meterPercent.toFixed(2))}% of this allocation`),
    element("span", "", `${Number(view.share.toFixed(4))}% cap`),
  );
  quotaBody.replaceChildren(element("strong", "quota-caption", caption), element("p", "usage-explanation", shareText), meter, ...(meterScale ? [meterScale] : []), units, element("p", `usage-explanation${view.blocked || view.acknowledged ? " quota-warning" : ""}`, warning));
  const snapshotPercent = knownNumber(estimate?.snapshot_account_used_percent);
  const pendingPercent = knownNumber(estimate?.pending_account_used_percent);
  if (snapshotPercent !== null || pendingPercent > 0) {
    const settled = snapshotPercent === null ? "Unavailable" : `${Number(snapshotPercent.toFixed(4))}%`;
    const pending = pendingPercent > 0 ? ` · +${Number(pendingPercent.toFixed(4))}% recorded after that snapshot` : "";
    quotaBody.append(element("p", "usage-explanation", `Estimated for this device through the last official snapshot: ${settled} account-percentage points${pending}. Work recorded after the snapshot is pending reconciliation.`));
  }
  if (view.reliable && knownNumber(estimate?.remaining_credits_micros) !== null) quotaBody.append(element("p", "quota-reset", `${view.analysisOnly ? "Estimated units until target" : "Estimated units remaining"}: ${estimatedUnits(estimate.remaining_credits_micros)}`));
  if (view.unknown) {
    const legacy = numeric(estimate?.legacy_unknown_runs), unreported = numeric(estimate?.unreported_runs), other = numeric(estimate?.other_unknown_runs);
    if (legacy + unreported + other === view.unknown) {
      const reasons = [];
      if (legacy) reasons.push(`${count(legacy)} older ${legacy === 1 ? "response predates" : "responses predate"} model and speed tracking`);
      if (unreported) reasons.push(`${count(unreported)} ${unreported === 1 ? "response has" : "responses have"} not reported usage`);
      if (other) reasons.push(`${count(other)} ${other === 1 ? "response cannot" : "responses cannot"} be priced with the available data`);
      const acknowledgement = view.acknowledged ? ` ${count(view.acknowledged)} completed ${view.acknowledged === 1 ? "response was" : "responses were"} reviewed by an administrator; this does not change access or make missing usage zero.` : "";
      quotaBody.append(element("p", "usage-explanation quota-warning", `${reasons.join("; ")}. ${partial ? "The amount above includes reported work only. " : ""}Unknown usage is not zero, but it does not block new responses by itself.${acknowledgement}`));
    } else {
      const acknowledgement = view.acknowledged ? ` ${count(view.acknowledged)} completed ${view.acknowledged === 1 ? "response was" : "responses were"} reviewed by an administrator; actual usage may be higher than the reported lower bound.` : " The fraction is unavailable";
      quotaBody.append(element("p", "usage-explanation quota-warning", `${count(view.unknown)} response(s) have unknown usage.${acknowledgement}. Unknown usage is not zero, but it does not block new responses by itself.`));
    }
  }
}

function renderMyUsage() {
  if (!state.me || !["approved", "active"].includes(state.me.status)) return;
  const quota = state.myQuota, own = quota?.device;
  const valid = own && idOf(own.device_id) === idOf(state.me.id);
  const calibration = !state.connected ? "stale" : quota?.calibration?.status || "unknown";
  const displayQuota = { ...quota, calibration: { ...quota?.calibration, status: calibration } };
  const displayOwn = valid ? { ...own } : null;
  const view = deviceQuotaView(state.me, displayOwn, displayQuota);
  const knownTarget = view.percent !== null ? view.percent : view.partialPercent;
  const officialPercent = isAdmin() ? knownNumber(quota?.calibration?.provider_used_percent) : null;
  $("my-usage-brief").textContent = !valid ? "Unavailable" : view.blocked ? "Blocked" : officialPercent !== null ? `${Number(officialPercent.toFixed(1))}% official` : view.analysisOnly && knownTarget !== null ? `${view.unknown ? "≥" : ""}${Number(knownTarget.toFixed(1))}% target` : view.acknowledged && knownTarget !== null ? `≥${Number(knownTarget.toFixed(1))}% of share` : view.percent !== null ? `${Number(view.percent.toFixed(1))}% of share` : view.share === null && view.accountPercent !== null ? `${view.unknown ? "≥" : "≈"}${Number(view.accountPercent.toFixed(2))}% account` : view.share === null ? "No cap" : "Unavailable";
  const personalControls = $("my-usage-admin-controls"), personalInput = $("my-usage-share-input");
  personalControls.hidden = !isAdmin(); personalInput.disabled = !isAdmin();
  if (!$("my-usage-dialog").open) return;
  const signature = JSON.stringify([state.me.id, valid, displayOwn, calibration, quota?.window?.ends_at, quota?.calibration?.carried_forward, quota?.calibration?.learning_reason, quota?.calibration?.provider_used_percent, quota?.calibration?.provisional_higher_capacity, state.connected, Math.floor(Date.now() / 60000)]);
  if ($("my-usage-dialog")._quotaSignature === signature) return;
  $("my-usage-dialog")._quotaSignature = signature;
  const learningReason = {
    awaiting_weekly_change: "waiting for enough change in the account’s weekly meter",
    awaiting_usage_reports: "waiting for usage reports from responses",
    unpriced_usage: "some recorded work does not yet have a supported estimate",
  }[quota?.calibration?.learning_reason] || "matching recorded work to account usage";
  const status = !valid ? "Usage report unavailable. Your administrator can check your allowance; do not assume it is unused."
    : !state.connected ? "Offline — the last report may be outdated. Your current usage fraction is unavailable."
      : officialPercent !== null ? `Official account meter: ${Number(officialPercent.toFixed(1))}% used. Per-device attribution below is approximate${quota.calibration?.provisional_higher_capacity ? "; a higher capacity measured in the current week is already applied so the older calibration cannot overstate it" : ""}.`
        : calibration === "estimated" ? `Estimated from your recorded work, not an exact OpenAI debit.${quota.calibration?.carried_forward ? " A previously calibrated capacity remains in use while the provider’s current window is tracked." : ""}`
        : calibration === "stale" ? view.analysisOnly ? "Stale report — your personal target remains analysis-only and never blocks host responses." : "Stale report — enforcement waits for a fresh calibrated measurement; staleness alone does not block new responses."
          : view.analysisOnly ? `Learning weekly capacity — ${learningReason}. Your personal target never blocks host responses; recorded usage is kept.` : `Learning weekly capacity — ${learningReason}. Recorded usage is kept; unavailable calibration does not block new responses by itself.`;
  $("my-usage-status").textContent = status;
  $("my-usage-status").classList.toggle("quota-warning", !valid || calibration !== "estimated");
  if (valid) renderDeviceQuota($("my-usage-meter"), state.me, displayOwn, displayQuota);
  else $("my-usage-meter").replaceChildren(element("strong", "quota-caption", "Usage fraction unavailable"), quotaMeter(null, "Estimated weekly share consumed"));
  $("my-usage-reset").textContent = quota?.window?.ends_at ? quotaReset(quota.window.ends_at) : "Weekly reset unavailable until a provider report arrives.";
  if (isAdmin() && personalInput.dataset.dirty !== "true" && document.activeElement !== personalInput) personalInput.value = view.share ?? "";
}

async function saveMyUsageTarget() {
  if (!isAdmin()) return;
  const input = $("my-usage-share-input"), button = $("my-usage-share-save");
  const target = input.value.trim() ? Number(input.value) : null;
  if ((target !== null && (!Number.isFinite(target) || target <= 0 || target > 100)) || input.validity.badInput) { toast("Use a personal target above 0 and up to 100%, or leave it blank."); return; }
  button.disabled = true; button.textContent = "Saving…";
  try {
    const device = await api(`/api/admin/devices/${encodeURIComponent(state.me.id)}`, { method: "PATCH", body: JSON.stringify({ weekly_share_percent: target }) });
    state.me = { ...state.me, ...device }; input.dataset.dirty = "false";
    await checkSession();
    if (state.view === "admin") await refreshAdmin();
    toast(target === null ? "Personal target cleared; your estimated device attribution remains visible." : "Personal analysis target saved. Host responses remain unrestricted.");
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; button.textContent = "Save my target"; }
}

async function openMyUsage({ route = true, refresh = true } = {}) {
  if (!state.me || !["approved", "active"].includes(state.me.status)) return;
  closeRouteDialogs("usage");
  if (!$("my-usage-dialog").open) { closeSidebar(); $("my-usage-dialog").showModal(); }
  if (route) persistVisibleRoute("push");
  renderMyUsage();
  if (!refresh) return;
  $("my-usage-refresh").disabled = true;
  try { await checkSession(); renderMyUsage(); }
  finally { $("my-usage-refresh").disabled = false; }
}

async function refreshMyUsage() {
  if (!state.me || !["approved", "active"].includes(state.me.status)) return;
  const button = $("my-usage-refresh"); button.disabled = true; button.textContent = "Refreshing…";
  const deviceId = idOf(state.me.id);
  try {
    const report = await api("/api/usage/me", { method: "POST", body: "{}", timeout: 35000 });
    if (idOf(state.me?.id) !== deviceId) return;
    state.myQuota = report;
    renderMyUsage();
    if (state.selected) updateComposer();
  } catch (error) {
    if (error.status === 403) await checkSession();
    else toast(error.message);
  } finally { button.disabled = false; button.textContent = "Refresh usage"; }
}

function updateAdminDevice(card, device, rows, estimate, quota) {
  card._device = device;
  const { head, quotaBody, metrics, approval, reject, revoke, deleteDevice, acknowledgeUnknown, acknowledgeHelp, note } = card._nodes;
  const view = deviceQuotaView(device, estimate, quota); card._quotaReliable = view.reliable; card._quotaView = view;
  const name = element("div", "admin-device-name");
  const label = element("strong", "", device.name || `Device ${device.id}`); label.dir = direction(device.name); name.append(label, element("span", `badge ${device.status}`, device.status || "pending"));
  if (device.role === "admin") name.append(element("span", "badge", "Admin"));
  if (device.verification_code) name.append(element("span", "badge", device.verification_code));
  name.append(element("span", "badge", device.worker_online ? "Worker online" : "Worker offline"));
  if (idOf(device.id) === idOf(state.me.id)) name.append(element("span", "badge", "This device"));
  head.replaceChildren(name, element("span", "muted", device.last_seen_at ? `Seen ${timeValue(device.last_seen_at)?.toLocaleString() || ""}` : ""));
  renderDeviceQuota(quotaBody, device, estimate, quota);
  const tokens = rows.reduce((sum, row) => sum + usageTotal(row), 0);
  const runs = rows.reduce((sum, row) => sum + numeric(row.run_count ?? row.runs ?? row.total_runs ?? row.requests), 0);
  const running = numeric(device.active_runs ?? device.active_run_count ?? device.running ?? rows.reduce((sum, row) => sum + numeric(row.active_runs), 0));
  const today = rows.reduce((sum, row) => sum + numeric(row.today_tokens), 0);
  const month = rows.reduce((sum, row) => sum + numeric(row.month_tokens), 0);
  const unknown = rows.reduce((sum, row) => sum + numeric(row.unknown_usage_runs), 0);
  metrics.replaceChildren();
  for (const [value, caption] of [[tokens, "total tokens"], [today, "today"], [month, "this month"], [runs, "runs"], [running, "active"], [unknown, "runs with unreported usage"]]) { const item = element("span"); item.append(element("strong", "", count(value)), document.createTextNode(` ${caption}`)); metrics.append(item); }
  for (const [key, value] of [["concurrency", device.max_concurrent ?? 1], ["share", view.share], ["budget", device.token_budget]]) {
    const input = card._fields[key].input;
    if (!card._dirty.has(key) && document.activeElement !== input) input.value = value ?? "";
  }
  const ownAdmin = device.role === "admin" && idOf(device.id) === idOf(state.me.id);
  card._fields.share.wrapper.querySelector("label").textContent = ownAdmin ? "Personal analysis target (% of account)" : "Weekly share of account (%)";
  card._fields.share.input.placeholder = ownAdmin ? "No target" : "No weekly limit";
  note.textContent = ownAdmin ? "Analysis target only · It never blocks host prompts · Blank keeps estimated device attribution visible." : "Blank = no weekly limit · 0% = block new responses · Changing a share keeps usage history. Running responses are not interrupted.";
  approval.hidden = device.status === "approved"; approval.textContent = device.status === "pending" ? "Approve" : "Restore access";
  reject.hidden = device.status !== "pending";
  revoke.hidden = device.status !== "approved" || idOf(device.id) === idOf(state.me.id);
  const deletionProtected = device.role === "admin" || idOf(device.id) === idOf(state.me.id);
  deleteDevice.hidden = deletionProtected;
  deleteDevice.disabled = deletionProtected || deleteDevice.getAttribute("aria-busy") === "true";
  deleteDevice.setAttribute("aria-label", `Delete ${device.name || `device ${device.id}`} and its data permanently`);
  const canAcknowledge = device.status === "approved" && device.role !== "admin" && view.acknowledgeable > 0;
  acknowledgeUnknown.hidden = !canAcknowledge;
  acknowledgeHelp.hidden = !canAcknowledge;
  if (canAcknowledge) {
    acknowledgeUnknown.textContent = `Mark ${count(view.acknowledgeable)} incomplete ${view.acknowledgeable === 1 ? "response" : "responses"} reviewed`;
    acknowledgeUnknown.setAttribute("aria-label", `${acknowledgeUnknown.textContent} for ${device.name || "this device"}`);
    acknowledgeHelp.textContent = `${view.share === null ? "No weekly share is configured." : `The ${view.share}% weekly share stays active.`} Incomplete usage remains a lower bound and never blocks new work by itself. Reviewing it does not erase usage or count it as zero.`;
  }
}

function adminDeviceSnapshot(deviceId) {
  const key = idOf(deviceId), snapshot = state.adminSnapshot;
  if (!snapshot) return null;
  const device = snapshot.devices.find((item) => idOf(item.id) === key);
  if (!device) return null;
  const rows = snapshot.usage.filter((row) => idOf(row.device_id ?? row.owner_id ?? row.id) === key);
  const quota = snapshot.envelope?.quota || {};
  const estimate = (Array.isArray(quota.devices) ? quota.devices : []).find((row) => idOf(row.device_id) === key);
  return { device, rows, estimate, quota };
}

function createAdminDeviceSummary(device) {
  const card = element("article", "admin-device-summary"); card.dataset.deviceId = idOf(device.id);
  const identity = element("div", "admin-device-summary-identity");
  const title = element("div", "admin-device-summary-title");
  const meta = element("p", "admin-device-summary-meta");
  identity.append(title, meta);
  const usage = element("div", "admin-device-summary-usage");
  const menu = element("button", "icon-button admin-device-menu", "•••"); menu.type = "button";
  menu.addEventListener("click", () => openAdminDevice(card.dataset.deviceId).catch((error) => toast(error.message)));
  card.append(identity, usage, menu);
  card._nodes = { title, meta, usage, menu };
  return card;
}

function updateAdminDeviceSummary(card, device, rows, estimate, quota) {
  const { title, meta, usage, menu } = card._nodes;
  const label = element("strong", "", device.name || `Device ${device.id}`); label.dir = direction(device.name);
  title.replaceChildren(label, element("span", `badge ${device.status}`, device.status || "pending"));
  if (device.role === "admin") title.append(element("span", "badge", "Admin"));
  if (device.verification_code) title.append(element("span", "badge", device.verification_code));
  if (idOf(device.id) === idOf(state.me?.id)) title.append(element("span", "badge", "This device"));
  const runs = rows.reduce((sum, row) => sum + numeric(row.run_count ?? row.runs ?? row.total_runs ?? row.requests), 0);
  const active = numeric(device.active_runs ?? device.active_run_count ?? device.running ?? rows.reduce((sum, row) => sum + numeric(row.active_runs), 0));
  const seen = device.last_seen_at ? `Seen ${timeValue(device.last_seen_at)?.toLocaleString() || ""}` : "Never connected";
  meta.textContent = `${device.worker_online ? "Worker online" : "Worker offline"} · ${count(runs)} runs${active ? ` · ${count(active)} active` : ""} · ${seen}`;
  const view = deviceQuotaView(device, estimate, quota);
  const target = view.percent !== null ? view.percent : view.partialPercent;
  const caption = view.share === null ? "No weekly limit"
    : target === null ? `${Number(view.share.toFixed(4))}% weekly share · usage pending`
      : `${view.unknown ? "At least " : ""}${Number(target.toFixed(2))}% of ${Number(view.share.toFixed(4))}% share used`;
  usage.replaceChildren(element("strong", "", caption));
  if (view.share !== null) usage.append(quotaMeter(target, `Usage for ${device.name || "device"}`));
  menu.setAttribute("aria-label", `Open details for ${device.name || `device ${device.id}`}`);
  menu.title = "Device details";
}

async function openAdminDevice(deviceId, { route = true } = {}) {
  if (!isAdmin()) return;
  const key = idOf(deviceId);
  if (!key) return;
  if (state.view !== "admin" || state.adminPanel) await openAdmin({ route: false });
  if (!state.adminSnapshot) await refreshAdmin();
  const snapshot = adminDeviceSnapshot(key);
  if (!snapshot) {
    state.adminDeviceId = "";
    if (route) persistVisibleRoute("replace");
    toast("That device profile is no longer available.");
    return;
  }
  closeRouteDialogs("device");
  state.adminDeviceId = key;
  $("admin-device-title").textContent = snapshot.device.name || `Device ${snapshot.device.id}`;
  $("admin-device-subtitle").textContent = `Device ID ${snapshot.device.id} · Detailed access, limits and usage`;
  const detail = $("admin-device-detail");
  let card = detail.firstElementChild;
  if (!card || card.dataset.deviceId !== key) {
    card = createAdminDevice(snapshot.device);
    detail.replaceChildren(card);
  }
  updateAdminDevice(card, snapshot.device, snapshot.rows, snapshot.estimate, snapshot.quota);
  if (!$("admin-device-dialog").open) $("admin-device-dialog").showModal();
  if (route) persistVisibleRoute("push");
}

function renderAdmin(devices, usage, envelope = {}) {
  renderProviderUsage(envelope.provider_usage || {});
  const quota = envelope.quota || {}, calibration = quota.calibration?.status || "unknown";
  const calibrated = calibration === "estimated";
  $("quota-calibration").textContent = calibrated ? quota.calibration?.provisional_higher_capacity ? "Calibrated estimate · A higher capacity observed in the current week is already used for attribution while confirmation completes. Settled device attribution cannot exceed the official account meter; account activity not attributable to a device remains unassigned." : quota.calibration?.carried_forward ? "Calibrated estimate · A previously measured capacity is being enforced while the provider’s current window is tracked. Settled device attribution cannot exceed the official account meter; account activity not attributable to a device remains unassigned." : "Calibrated estimate · Settled intervals are checkpointed. Device attribution cannot exceed the official account meter, and unattributed account activity remains unassigned." : calibration === "stale" ? "Stale calibration · Limits are not enforced from stale data. New responses remain available unless the share is 0%; enforcement resumes only after a fresh calibrated measurement reaches the allocation." : "Learning the account’s weekly capacity · Device percentages are unavailable. New responses remain available unless the share is 0%; enforcement begins only after a fresh calibrated measurement reaches the allocation.";
  $("quota-calibration").classList.toggle("quota-warning", !calibrated);
  renderAllocatedQuota(quota.allocated_pool);
  $("quota-week-reset").textContent = quota.window?.ends_at ? quotaReset(quota.window.ends_at) : "Weekly reset unavailable until the provider reports its weekly window.";
  const totalRuns = usage.reduce((sum, row) => sum + numeric(row.run_count ?? row.runs ?? row.total_runs ?? row.requests), 0);
  const estimates = Array.isArray(quota.devices) ? quota.devices : [];
  const totalMicros = estimates.length ? estimates.reduce((sum, row) => sum + numeric(row.spent_credits_micros), 0) : null;
  const summary = $("usage-summary"); summary.replaceChildren();
  for (const [label, value] of [["Approved devices", count(devices.filter((device) => device.status === "approved").length)], ["Estimated usage units", estimatedUnits(totalMicros)], ["Recorded runs", count(totalRuns)]]) {
    const card = element("div", "summary-card"); card.append(element("span", "", label), element("strong", "", value)); summary.append(card);
  }
  const list = $("admin-devices"), focused = list.contains(document.activeElement) ? document.activeElement : null;
  const existing = new Map([...list.children].filter((node) => node.dataset.deviceId).map((node) => [node.dataset.deviceId, node]));
  const ids = new Set(devices.map((device) => idOf(device.id)));
  for (const child of [...list.children]) if (!ids.has(child.dataset.deviceId)) child.remove();
  devices.forEach((device, index) => {
    const key = idOf(device.id), card = existing.get(key) || createAdminDeviceSummary(device);
    const rows = usage.filter((row) => idOf(row.device_id ?? row.owner_id ?? row.id) === key);
    updateAdminDeviceSummary(card, device, rows, estimates.find((row) => idOf(row.device_id) === key), quota);
    // CODEX: Keyed summary rows avoid movement and focus loss while the administration page polls.
    if (list.children[index] !== card) list.insertBefore(card, list.children[index] || null);
  });
  if (!devices.length) list.append(element("p", "muted", "No devices are registered yet."));
  if ($("admin-device-dialog").open && state.adminDeviceId) {
    const snapshot = adminDeviceSnapshot(state.adminDeviceId);
    if (snapshot) {
      let card = $("admin-device-detail").firstElementChild;
      if (!card || card.dataset.deviceId !== state.adminDeviceId) {
        card = createAdminDevice(snapshot.device);
        $("admin-device-detail").replaceChildren(card);
      }
      updateAdminDevice(card, snapshot.device, snapshot.rows, snapshot.estimate, snapshot.quota);
    } else {
      state.adminDeviceId = "";
      $("admin-device-dialog").close();
    }
  }
  if (focused?.isConnected && document.activeElement !== focused) focused.focus({ preventScroll: true });
}

function limitField(id, label, value, placeholder, allowZero) {
  const wrapper = element("div", "limit-field"); const caption = element("label", "", label); caption.htmlFor = id;
  const input = element("input"); input.id = id; input.type = "number"; input.min = allowZero ? "0" : "1"; if (!allowZero) input.max = "10"; input.step = "1"; input.value = value ?? ""; input.placeholder = placeholder;
  wrapper.append(caption, input); return { wrapper, input };
}
async function adminAction(button, path, method, body) {
  button.disabled = true;
  try { await api(path, { method, body: JSON.stringify(body) }); await refreshAdmin(); }
  catch (error) { toast(error.message); }
  finally { button.disabled = false; }
}

function mobileSidebar() { return window.matchMedia("(max-width: 720px)").matches; }

function updateSidebarAccessibility() {
  const mobile = mobileSidebar(), expanded = mobile ? $("workspace").classList.contains("sidebar-open") : !state.sidebarCollapsed;
  const sidebar = $("sidebar"), toggle = $("sidebar-toggle"), main = document.querySelector(".main");
  const label = expanded ? "Hide rooms" : "Show rooms";
  main.inert = mobile && expanded;
  if (!expanded && sidebar.contains(document.activeElement)) toggle.focus({ preventScroll: true });
  sidebar.inert = !expanded;
  sidebar.setAttribute("aria-hidden", String(!expanded));
  toggle.setAttribute("aria-expanded", String(expanded)); toggle.setAttribute("aria-label", label); toggle.title = label;
  $("sidebar-scrim").hidden = !mobile || !expanded;
}

function closeSidebar() {
  $("workspace").classList.remove("sidebar-open");
  updateSidebarAccessibility();
}

function preserveSidebarReadingPosition() {
  if (state.view !== "chat") return;
  const scroll = $("timeline-scroll"), top = scroll.getBoundingClientRect().top;
  const anchor = [...$("timeline").querySelectorAll(".message-content > *, .run-activity, .message-header")].find((node) => node.getBoundingClientRect().bottom > top + 8);
  const offset = anchor ? anchor.getBoundingClientRect().top - top : 0;
  const follow = state.follow, roomId = state.selected;
  const until = performance.now() + (window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 260);
  const version = (state.sidebarLayoutVersion || 0) + 1; state.sidebarLayoutVersion = version;
  state.sidebarTransition = true;
  const restore = () => {
    if (version !== state.sidebarLayoutVersion) return;
    if (state.selected !== roomId || state.view !== "chat") { state.sidebarTransition = false; return; }
    if (follow) scroll.scrollTop = scroll.scrollHeight;
    else if (anchor?.isConnected) scroll.scrollTop += anchor.getBoundingClientRect().top - scroll.getBoundingClientRect().top - offset;
    if (performance.now() < until) requestAnimationFrame(restore);
    else { state.sidebarTransition = false; state.follow = follow; $("jump-bottom").hidden = follow || !state.selected; }
  };
  requestAnimationFrame(restore);
}

function toggleSidebar() {
  let openedMobile = false;
  if (mobileSidebar()) { $("workspace").classList.toggle("sidebar-open"); openedMobile = $("workspace").classList.contains("sidebar-open"); }
  else {
    preserveSidebarReadingPosition();
    state.sidebarCollapsed = !state.sidebarCollapsed;
    $("workspace").classList.toggle("sidebar-collapsed", state.sidebarCollapsed);
    try { localStorage.setItem("codex-lan-sidebar-collapsed", String(state.sidebarCollapsed)); } catch {}
  }
  updateSidebarAccessibility();
  if (openedMobile) $("new-room").focus({ preventScroll: true });
}

function cycleSetting(event) {
  if (event.defaultPrevented || event.isComposing || event.repeat || !["ArrowUp", "ArrowDown"].includes(event.key)) return;
  if (state.view !== "chat" || document.querySelector("dialog[open]")) return;
  const target = event.target instanceof Element ? event.target : null;
  if (target?.closest("input, textarea, select, [contenteditable='true']") && target !== $("message-input")) return;
  const modelShortcut = event.altKey && event.ctrlKey && !event.metaKey && !event.shiftKey;
  const effortShortcut = event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey;
  if (!modelShortcut && !effortShortcut) return;
  const select = $(modelShortcut ? "model-select" : "effort-select");
  if (select.disabled) return;
  const options = [...select.options].filter((option) => !option.disabled && option.value);
  if (options.length < 2) return;
  const current = Math.max(0, options.findIndex((option) => option.value === select.value));
  const arrowDirection = event.key === "ArrowDown" ? 1 : -1;
  const direction = effortShortcut ? -arrowDirection : arrowDirection;
  const index = modelShortcut
    ? (current + direction + options.length) % options.length
    : Math.max(0, Math.min(options.length - 1, current + direction));
  event.preventDefault();
  if (options[index].value === select.value) return;
  select.value = options[index].value;
  select.dispatchEvent(new Event("change", { bubbles: true }));
}

function bindEvents() {
  window.CodexHostSetup?.init({ api, copyText, isActive: () => isAdmin() && state.view === "admin" && state.adminPanel === "host-setup" });
  document.addEventListener("focusin", () => { state.focusRevision += 1; }, true);
  $("toast-close").addEventListener("click", () => dismissToast());
  $("password-login-form").addEventListener("submit", (event) => { event.preventDefault(); loginWithPassword(); });
  $("password-login-show").addEventListener("click", () => togglePasswordVisibility($("password-login-show"), [$("password-login-password")]));
  $("enroll-form").addEventListener("submit", (event) => { event.preventDefault(); enrollComputer(); });
  $("connector-open").addEventListener("click", openConnector);
  $("connector-update-details").addEventListener("click", openConnector);
  $("connector-update-dismiss").addEventListener("click", dismissConnectorNotice);
  for (const id of ["connector-update-download", "connector-dialog-update-download"]) $(id).addEventListener("click", (event) => {
    if (!event.currentTarget.hasAttribute("href")) { event.preventDefault(); toast("The update download is temporarily unavailable. Try again when the server returns."); return; }
    toast(`Download started. When it finishes, run ${connectorRunCommand()}`);
  });
  $("connector-close").addEventListener("click", () => $("connector-dialog").close());
  $("my-usage-open").addEventListener("click", openMyUsage);
  $("my-usage-close").addEventListener("click", () => $("my-usage-dialog").close());
  $("my-usage-refresh").addEventListener("click", refreshMyUsage);
  $("my-usage-share-input").addEventListener("input", (event) => { event.currentTarget.dataset.dirty = "true"; });
  $("my-usage-share-save").addEventListener("click", saveMyUsageTarget);
  $("browser-login-open").addEventListener("click", openBrowserLogin);
  $("easy-signin-setup").addEventListener("click", openBrowserLogin);
  $("easy-signin-dismiss").addEventListener("click", dismissEasySignInOffer);
  $("browser-login-form").addEventListener("submit", (event) => { event.preventDefault(); saveBrowserLogin(); });
  $("browser-login-show").addEventListener("click", () => togglePasswordVisibility($("browser-login-show"), [$("browser-login-password"), $("browser-login-confirm")]));
  for (const id of ["browser-login-close", "browser-login-cancel"]) $(id).addEventListener("click", closeBrowserLogin);
  $("browser-login-dialog").addEventListener("cancel", resetBrowserLoginDialog);
  $("browser-login-dialog").addEventListener("close", resetBrowserLoginDialog);
  $("browser-login-disable").addEventListener("click", () => {
    $("browser-login-disable-confirm").hidden = false;
    $("browser-login-disable").setAttribute("aria-expanded", "true");
    $("browser-login-disable-confirm-button").focus({ preventScroll: true });
  });
  $("browser-login-disable-cancel").addEventListener("click", () => {
    $("browser-login-disable-confirm").hidden = true;
    $("browser-login-disable").setAttribute("aria-expanded", "false");
    $("browser-login-disable").focus({ preventScroll: true });
  });
  $("browser-login-disable-confirm-button").addEventListener("click", disableBrowserLogin);
  $("browser-login-logout").addEventListener("click", logoutBrowser);
  $("connector-private-installer").addEventListener("click", downloadPrivateInstaller);
  $("connector-pairing").addEventListener("click", downloadPairing);
  $("connector-pairing-primary").addEventListener("click", downloadPairing);
  $("banner-connector-setup").addEventListener("click", openConnector);
  $("connector-reload").addEventListener("click", () => location.reload());
  $("login-form").addEventListener("submit", (event) => { event.preventDefault(); const token = $("login-token").value.trim(); if (token) login(token); });
  $("switch-device").addEventListener("click", () => showLogin());
  $("theme-toggle").addEventListener("click", () => setTheme(themes[(themes.indexOf(document.documentElement.dataset.theme) + 1) % themes.length]));
  $("theme-select")?.addEventListener("change", (event) => setTheme(event.currentTarget.value));
  $("new-room").addEventListener("click", createRoomNow);
  $("empty-new-room").addEventListener("click", createRoomNow);
  $("read-only-new-room").addEventListener("click", createRoomNow);
  $("read-only-copy-draft").addEventListener("click", (event) => copyText(roomState().draft, event.currentTarget));
  $("room-menu").addEventListener("click", () => openRoomDialog("edit"));
  $("room-search").addEventListener("input", renderRooms);
  $("quick-switcher-open").addEventListener("click", openQuickSwitcher);
  $("quick-switcher-close").addEventListener("click", () => $("quick-switcher").close());
  $("quick-switcher-input").addEventListener("input", () => renderQuickSwitcher({ reset: true }));
  $("quick-switcher").addEventListener("click", (event) => { if (event.target === $("quick-switcher")) $("quick-switcher").close(); });
  $("quick-switcher").addEventListener("close", () => {
    $("quick-switcher-input").value = "";
    quickSwitcherItems = [];
    const target = quickSwitcherReturnFocus;
    quickSwitcherReturnFocus = null;
    if (target?.isConnected) target.focus({ preventScroll: true });
  });
  $("room-form").addEventListener("submit", submitRoom);
  $("room-delete").addEventListener("click", () => deleteRoom());
  for (const id of ["dialog-close", "dialog-cancel"]) $(id).addEventListener("click", () => $("room-dialog").close());
  $("send-button").addEventListener("click", sendMessage);
  $("stop-button").addEventListener("click", stopResponses);
  $("load-older").addEventListener("click", loadOlder);
  $("reply-clear").addEventListener("click", () => { const cache = roomState(); cache.reply = null; touchComposer(cache); renderReply(); updateComposer(); $("message-input").focus({ preventScroll: true }); });
  $("model-select").addEventListener("change", () => saveModelSettings(true));
  $("effort-select").addEventListener("change", () => saveModelSettings(false));
  $("speed-select").addEventListener("change", () => saveModelSettings(false));
  $("tool-read-only").addEventListener("change", saveToolReadOnly);
  $("model-select").title = "Switch model · Ctrl+Alt+↑/↓";
  $("effort-select").title = "Switch thinking effort · Alt+↑/↓";
  $("attach-button").addEventListener("click", () => $("attachment-input").click());
  $("attachment-input").addEventListener("change", (event) => { addFiles([...event.target.files]).catch((error) => toast(error.message)); event.target.value = ""; });
  const input = $("message-input");
  input.addEventListener("input", () => { if (state.selected) setComposerDraft(roomState(), input.value); updateComposerDraftState(); });
  input.addEventListener("pointerdown", () => { state.lastEnter = null; });
  input.addEventListener("compositionstart", () => { state.lastEnter = null; });
  $("double-enter-send").addEventListener("change", (event) => setDoubleEnterMode(event.currentTarget.checked));
  input.addEventListener("keydown", (event) => {
    if (event.isComposing || event.keyCode === 229) { state.lastEnter = null; return; }
    if (event.key !== "Enter" || event.shiftKey) { state.lastEnter = null; return; }
    if (event.repeat) { event.preventDefault(); state.lastEnter = null; return; }
    if (event.ctrlKey || event.metaKey) { event.preventDefault(); state.lastEnter = null; sendMessage(); return; }
    if (event.altKey) { state.lastEnter = null; return; }
    if (!syncEnterPreference()) { event.preventDefault(); state.lastEnter = null; sendMessage(); return; }
    const previous = state.lastEnter;
    const now = performance.now();
    if (previous && previous.roomId === state.selected && now - previous.time < 650 && input.value === previous.expected && input.selectionStart === previous.position + 1 && input.selectionEnd === input.selectionStart && !$("send-button").disabled) {
      event.preventDefault();
      input.value = input.value.slice(0, previous.position) + input.value.slice(previous.position + 1);
      setComposerDraft(roomState(), input.value); state.lastEnter = null; sendMessage();
    } else { state.lastEnter = { time: now, roomId: state.selected, position: input.selectionStart, expected: `${input.value.slice(0, input.selectionStart)}\n${input.value.slice(input.selectionEnd)}` }; }
  });
  input.addEventListener("paste", (event) => {
    const files = [...(event.clipboardData?.items || [])].filter((item) => item.kind === "file").map((item) => item.getAsFile()).filter(Boolean);
    if (files.length) { event.preventDefault(); addFiles(files).catch((error) => toast(error.message)); }
  });
  const composer = $("composer");
  composer.addEventListener("dragover", (event) => { if (event.dataTransfer.types.includes("Files")) { event.preventDefault(); composer.classList.add("dragover"); } });
  composer.addEventListener("dragleave", (event) => { if (!composer.contains(event.relatedTarget)) composer.classList.remove("dragover"); });
  composer.addEventListener("drop", (event) => { event.preventDefault(); composer.classList.remove("dragover"); addFiles([...event.dataTransfer.files]).catch((error) => toast(error.message)); });
  window.addEventListener("dragover", (event) => { if (event.dataTransfer.types.includes("Files")) event.preventDefault(); });
  window.addEventListener("drop", (event) => { if (event.dataTransfer.types.includes("Files")) event.preventDefault(); });
  $("timeline-scroll").addEventListener("scroll", (event) => {
    if (state.sidebarTransition) return;
    state.follow = nearBottom();
    const cache = state.roomStates.get(state.selected);
    if (cache && state.view === "chat") { cache.scrollTop = event.currentTarget.scrollTop; cache.follow = state.follow; }
    $("jump-bottom").hidden = state.follow || !state.selected;
    if (markVisibleRoomRead()) { saveSidebarPreferences(); renderRooms(); }
  }, { passive: true });
  $("jump-bottom").addEventListener("click", scrollBottom);
  document.addEventListener("selectionchange", flushDeferredSelectionRender);
  $("admin-open").addEventListener("click", openAdmin);
  $("admin-devices-open").addEventListener("click", () => openAdmin());
  $("host-setup-open").addEventListener("click", () => openAdmin({ panel: "host-setup" }));
  $("admin-refresh").addEventListener("click", refreshAdmin);
  $("admin-device-close").addEventListener("click", () => $("admin-device-dialog").close());
  $("admin-device-dialog").addEventListener("close", () => { state.adminDeviceId = ""; persistVisibleRoute("replace"); });
  for (const id of ["connector-dialog", "my-usage-dialog", "room-dialog"]) $(id).addEventListener("close", () => persistVisibleRoute("replace"));
  $("browser-login-dialog").addEventListener("close", () => persistVisibleRoute("replace"));
  $("sidebar-toggle").addEventListener("pointerdown", (event) => { if (document.activeElement === $("message-input")) event.preventDefault(); });
  $("sidebar-toggle").addEventListener("click", toggleSidebar);
  $("sidebar-scrim").addEventListener("click", () => { closeSidebar(); $("sidebar-toggle").focus({ preventScroll: true }); });
  window.matchMedia("(max-width: 720px)").addEventListener("change", closeSidebar);
  document.addEventListener("keydown", (event) => { if (event.key === "Escape" && mobileSidebar() && $("workspace").classList.contains("sidebar-open")) { closeSidebar(); $("sidebar-toggle").focus({ preventScroll: true }); } });
  document.addEventListener("keydown", handleQuickSwitcherKey);
  document.addEventListener("keydown", cycleSetting);
  window.addEventListener("online", resumeServerConnection);
  window.addEventListener("offline", () => { state.serverBrowserOffline = true; scheduleServerUnreachable(++state.serverRequestId); });
  window.addEventListener("focus", resumeServerConnection);
  window.addEventListener("focus", () => publishReplyView());
  window.addEventListener("blur", () => publishReplyView(true));
  window.addEventListener("storage", (event) => {
    if (!state.me || event.key !== `codex-lan-reply-view:${idOf(state.me.id)}` || !event.newValue) return;
    try {
      const view = JSON.parse(event.newValue);
      if (view?.tab !== replyAlerts.tab && typeof view?.room === "string" && Math.abs(Date.now() - numeric(view.at)) < 20000) closeReplyNotifications(view.room);
    } catch {}
  });
  window.addEventListener("pageshow", resumeServerConnection);
  window.addEventListener("pagehide", (event) => {
    publishReplyView(true);
    if (event.persisted) return;
    const seen = new Set();
    for (const cache of state.roomStates.values()) {
      if (cache.sending || cache.deviceId !== idOf(state.me?.id)) continue;
      for (const file of cache.files || []) {
        if (!file.id || seen.has(file.id)) continue;
        seen.add(file.id); discardUploadedFile(file, true);
      }
    }
  });
  window.addEventListener("popstate", () => applyLocationRoute().catch((error) => toast(error.message)));
  document.addEventListener("visibilitychange", () => { publishReplyView(); if (!document.hidden) resumeServerConnection(); });
}

async function boot() {
  const fragment = new URLSearchParams(location.hash.slice(1));
  const token = fragment.get("login");
  if (location.hash) history.replaceState(null, "", location.pathname + location.search);
  let savedTheme = "";
  try { savedTheme = localStorage.getItem("codex-lan-theme") || ""; } catch {}
  const retiredTheme = savedTheme === "cosmic-live";
  if (retiredTheme) savedTheme = "cosmic";
  state.themeExplicit = themes.includes(savedTheme);
  setTheme(state.themeExplicit ? savedTheme : systemTheme(), retiredTheme); bindEvents();
  try { state.sidebarCollapsed = localStorage.getItem("codex-lan-sidebar-collapsed") === "true"; } catch {}
  $("workspace").classList.toggle("sidebar-collapsed", state.sidebarCollapsed); updateSidebarAccessibility();
  window.matchMedia?.("(prefers-color-scheme: dark)")?.addEventListener?.("change", (event) => { if (!state.themeExplicit) setTheme(event.matches ? "dark" : "light", false); });
  if (token) await login(token);
  else await checkSession();
}

boot();
