"use strict";

// CODEX: Host controls use authenticated, allowlisted API actions and never execute browser-supplied commands.
window.CodexHostSetup = (() => {
  const byId = (id) => document.getElementById(id);
  const actionNames = new Set(["enable_startup", "keep_awake_on", "keep_awake_off", "prepare_connector", "configure_https"]);
  const actionLabels = {
    enable_startup: "Enable startup", keep_awake_on: "Keep host awake",
    keep_awake_off: "Allow automatic sleep", prepare_connector: "Prepare connector download",
    configure_https: "Set up HTTPS",
  };
  const relayActions = new Set(["configure_relay", "relay_connect", "relay_disconnect", "probe_relay", "bootstrap_relay"]);
  let options = null, snapshot = null, refreshing = null, changing = false;

  function node(tag, className = "", text = "") {
    const value = document.createElement(tag);
    value.className = className;
    value.textContent = String(text ?? "");
    return value;
  }

  function active() { return options?.isActive() === true; }
  function working() { return changing || ["queued", "pending", "running"].includes(snapshot?.job?.status); }
  function isEnabled(value) { return value === true || ["active", "enabled", "enabled-runtime"].includes(value); }

  function origin(value) {
    try {
      const url = new URL(value);
      if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash) return "";
      return url.origin;
    } catch { return ""; }
  }

  function badge(text, state = "unknown") {
    const value = node("span", "host-setup-badge", text);
    value.dataset.state = state;
    return value;
  }

  function card(title, status, state) {
    const value = node("section", "host-setup-card");
    const heading = node("div", "host-setup-card-heading");
    heading.append(node("h3", "", title), badge(status, state));
    const rows = node("dl", "host-setup-rows");
    value.append(heading, rows);
    value.addRow = (name, detail) => {
      const row = node("div", "host-setup-row");
      row.append(node("dt", "", name), node("dd", "", detail));
      rows.append(row);
    };
    value.addNote = (text) => value.append(node("p", "host-setup-card-note", text));
    return value;
  }

  function address(container, value, label) {
    const url = origin(value);
    if (!url) return;
    const row = node("div", "host-setup-address");
    const code = node("code", "", url);
    code.dir = "ltr";
    const copy = node("button", "button", "Copy address");
    copy.type = "button";
    copy.setAttribute("aria-label", `Copy ${label}`);
    copy.addEventListener("click", () => options.copyText(url, copy));
    row.append(code, copy);
    container.append(row);
  }

  function notice(text) { return node("p", "host-setup-notice", text); }

  function relayPermission(data, action) {
    return data.actions?.some((item) => item.id === action && item.enabled === true) === true;
  }

  function relayMigrationRequired() {
    return snapshot?.relay?.configured === true && snapshot.relay.managed === false;
  }

  function relayMigrationApproved() {
    if (!relayMigrationRequired()) return true;
    if (byId("host-setup-relay-replace-existing").checked) return true;
    relayStatus("Confirm replacement of the existing relay service before changing its configuration.", true);
    return false;
  }

  function relayStatus(message, error = false) {
    const label = byId("host-setup-relay-status");
    label.textContent = message;
    label.classList.toggle("error", error);
  }

  function relayDetail(name, value) {
    const row = node("div", "host-setup-relay-detail");
    row.append(node("span", "", name), node("span", "", value));
    return row;
  }

  function renderRelay(data) {
    const relay = data.relay;
    const section = byId("host-setup-relay");
    section.hidden = !relay;
    if (!relay) return;

    const supported = relay.supported !== false;
    const manageable = supported && relay.manageable !== false;
    const busy = working();
    const badgeNode = byId("host-setup-relay-badge");
    const label = !supported ? "Unavailable" : relay.active ? "Tunnel running" : relay.enabled ? "Reconnecting" : relay.configured ? "Disconnected" : "Not configured";
    badgeNode.textContent = label;
    badgeNode.dataset.state = relay.active ? "ready" : relay.enabled ? "unknown" : relay.configured ? "attention" : "unknown";

    const details = byId("host-setup-relay-details");
    details.replaceChildren();
    if (relay.configured) {
      if (relay.user && relay.server) details.append(relayDetail("SSH destination", `${relay.user}@${relay.server}`));
      details.append(relayDetail("Management", relay.managed === false ? "Existing service · replacement requires consent" : "Host admin"));
      details.append(relayDetail("Automatic reconnect", relay.enabled ? "On" : "Off"));
      details.append(relayDetail("Tunnel process", relay.active ? "Running" : "Not running"));
      if (relay.remote_port) details.append(relayDetail("Relay port", String(relay.remote_port)));
      const local = origin(relay.local_origin);
      if (local) details.append(relayDetail("Laptop HTTPS address", local));
      if (relay.host_key_fingerprint) details.append(relayDetail("Pinned SSH host key", relay.host_key_fingerprint));
    }
    const user = byId("host-setup-relay-user"), server = byId("host-setup-relay-server");
    const migration = byId("host-setup-relay-migration");
    migration.hidden = !relayMigrationRequired();
    if (migration.hidden) byId("host-setup-relay-replace-existing").checked = false;
    const migrationReady = migration.hidden || byId("host-setup-relay-replace-existing").checked;
    user.value = "codex-lan-relay";
    if (!server.dataset.edited) server.value = relay.server || "";
    server.disabled = !supported || busy;
    byId("host-setup-relay-save").disabled = !manageable || busy || !migrationReady || !relayPermission(data, "configure_relay");
    byId("host-setup-relay-connect").disabled = !manageable || busy || !relay.configured || relay.active || !relayPermission(data, "relay_connect");
    byId("host-setup-relay-disconnect").disabled = !manageable || busy || (!relay.enabled && !relay.active) || !relayPermission(data, "relay_disconnect");
    const key = typeof relay.public_key === "string" ? relay.public_key.trim() : "";
    byId("host-setup-relay-key").hidden = !key;
    byId("host-setup-relay-public-key").textContent = key;
    byId("host-setup-relay-copy-key").disabled = !key;

    const bootstrap = byId("host-setup-relay-bootstrap");
    bootstrap.hidden = !supported;
    const canBootstrap = manageable && migrationReady && relayPermission(data, "bootstrap_relay") && !busy;
    for (const id of ["host-setup-relay-fingerprint", "host-setup-relay-password", "host-setup-relay-confirm"]) byId(id).disabled = !canBootstrap;
    byId("host-setup-relay-probe").disabled = !relayPermission(data, "probe_relay") || busy || !server.value.trim();
    byId("host-setup-relay-bootstrap-submit").disabled = !canBootstrap;
    const prerequisite = byId("host-setup-relay-prerequisite");
    const hasHttps = data.network?.https_transport === "ssh_tunnel" && data.network?.https_enabled;
    prerequisite.textContent = !hasHttps ? "Set up HTTPS on 127.0.0.2:8767 first." : !relay.bootstrap_available ? "One-time password setup needs sshpass installed on this laptop." : !manageable ? "Review the existing relay service manually before changing it here." : "";
    prerequisite.hidden = !prerequisite.textContent;
    const job = data.job || {};
    if (job.action === "probe_relay") {
      const observed = job.result?.host_key_fingerprint;
      byId("host-setup-relay-observed").textContent = typeof observed === "string" && observed.startsWith("SHA256:")
        ? `Server presented ${observed} · unverified` : job.status === "failed" ? "Host-key check failed" : "Checking server host key…";
    }
    if (!supported) relayStatus("SSH relay management needs Linux user services on this host.", true);
    else if (!manageable) relayStatus("This relay configuration cannot be managed in the app. Existing access is unchanged.", true);
    else if (relay.error) relayStatus("The relay reported a connection problem. Check the server address, SSH authorization, and host key.", true);
    else if (!busy) relayStatus(relay.active ? "The SSH tunnel process is running. Check the HTTPS address from a client to confirm end-to-end access." : relay.enabled ? "Automatic reconnect is enabled; the tunnel is not running yet." : relay.configured ? "Relay disconnected; automatic reconnect is off." : "Enter a relay server for a pre-authorized key, or use first-time setup below.");
  }

  function render(data) {
    snapshot = data;
    const host = data.host || {}, network = data.network || {}, connector = data.connector || {}, awake = data.keep_awake || {}, codex = data.codex || {};
    byId("host-setup-name").textContent = host.hostname ? `${host.hostname} · ${host.platform || "Linux"}` : "Your Linux host";
    byId("host-setup-updated").textContent = `Checked ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
    const notices = byId("host-setup-notices");
    const notesOpen = notices.querySelector(".host-setup-notes")?.open === true;
    notices.replaceChildren();
    const notes = (Array.isArray(data.notices) ? data.notices : []).filter((text) => typeof text === "string" && text);
    if (notes.length) {
      const details = node("details", "host-setup-notes");
      details.open = notesOpen;
      details.append(node("summary", "", "Connection & availability notes"));
      for (const text of notes) details.append(node("p", "", text));
      notices.append(details);
    }
    const runs = Math.max(0, Number(data.active_runs) || 0);
    if (runs) notices.append(notice(`${runs} ${runs === 1 ? "response is" : "responses are"} running. These host settings do not restart the chat server.`));
    if (data.supported === false) notices.append(notice("Host controls are available on Linux with user services. You can still inspect the connection details here."));

    const connection = card("Connection", network.https_enabled ? "HTTPS ready" : "Local access", network.https_enabled ? "ready" : "unknown");
    connection.addRow("Backend listener", network.listen && network.port ? `${network.listen}:${network.port}` : "Unavailable");
    connection.addRow("HTTPS listener", network.https_enabled ? `Port ${network.port_https || 8767}` : "Not configured");
    const shared = origin(network.public_origin);
    if (shared) {
      address(connection, shared, "shared host address");
      if (network.https_transport === "ssh_tunnel") {
        connection.addRow("Access", "SSH tunnel");
        connection.addNote("Each computer forwards this loopback HTTPS address through its SSH tunnel. Keep the tunnel connected for the browser and Connector. Each person requests access, then you approve their device.");
      } else connection.addNote("Share this address with computers on the same local network. Each person requests access, then you approve their device.");
    } else connection.addNote("Configure HTTPS below to get an address other computers can open.");
    const local = origin(network.local_origin);
    if (local && local !== shared) address(connection, local, "local host address");
    if (network.https_enabled && network.ca_fingerprint) {
      connection.addRow("Certificate fingerprint", network.ca_fingerprint);
      const ca = node("a", "button", "Download host certificate");
      ca.href = "/api/admin/host-setup/ca";
      ca.download = "codex-lan-host-ca.crt";
      connection.append(ca);
    }

    const services = Array.isArray(data.services) ? data.services : [];
    const serviceCard = card("Host services", services.length && services.every((service) => isEnabled(service.active)) ? "Running" : "Check status", services.length && services.every((service) => isEnabled(service.active)) ? "ready" : "attention");
    for (const service of services) {
      const state = service.available === false ? "Not installed" : isEnabled(service.active) ? "Running" : typeof service.active === "string" ? service.active : "Stopped";
      serviceCard.addRow(service.label || service.id || "Service", `${state} · ${isEnabled(service.enabled) ? "Starts automatically" : "Startup disabled"}`);
    }
    if (!services.length) serviceCard.addNote("Service status is unavailable. Use the Linux host setup to install the host services.");

    const power = card("Power & availability", awake.active ? "Kept awake" : "Sleep allowed", awake.active ? "ready" : "unknown");
    power.addRow("Automatic sleep", awake.active ? "Blocked while host services run" : "Controlled by desktop settings");
    power.addRow("Keep-awake control", awake.supported ? "Available" : "Unavailable");
    power.addNote("The display may turn off while the host stays online. Closing a laptop lid can still suspend it; check your desktop’s power settings before using it as a host.");

    const download = card("Connector download", connector.available ? "Ready" : "Not prepared", connector.available ? "ready" : "attention");
    download.addRow("Published version", connector.version || "None");
    download.addNote("Each client installs its own connector to use tools and files on that computer. Pairing files are created for approved devices from their Connector setup.");
    if (connector.available) {
      const link = node("a", "button", "Download installer");
      link.href = "/api/client/download/installer";
      link.download = "";
      download.append(link);
    }
    const installation = card("Codex installation & sign-in", codex.available === false ? "Installation needed" : codex.signed_in === true ? "Sign-in configured" : codex.signed_in === false ? "Sign-in needed" : "Not checked", codex.available && codex.signed_in === true ? "ready" : "attention");
    installation.classList.add("host-setup-prerequisite");
    installation.addRow("Codex on this host", codex.available === true ? "Installed" : codex.available === false ? "Not found" : "Not checked");
    installation.addRow("Host sign-in", codex.signed_in === true ? "Configured locally" : codex.signed_in === false ? "Not configured" : "Not checked");
    installation.addNote("Complete Codex installation and sign-in on this host using the repository setup. Account credentials stay on the host and are not displayed here.");
    byId("host-setup-cards").replaceChildren(connection, serviceCard, power, download, installation);
    renderActions(data);
    renderRelay(data);
    if (data.job?.message) setStatus(data.job.message, data.job.status === "failed");
  }

  function renderActions(data) {
    const list = byId("host-setup-actions");
    list.replaceChildren();
    const actions = (Array.isArray(data.actions) ? data.actions : []).filter((action) => actionNames.has(action.id));
    for (const action of actions) {
      if (action.id === "configure_https") continue;
      const wrapper = node("div", "host-setup-action");
      const button = node("button", "button", action.label || actionLabels[action.id]);
      button.type = "button";
      button.dataset.action = action.id;
      button.disabled = action.enabled === false || data.supported === false || working();
      button.addEventListener("click", () => run(action.id));
      wrapper.append(button);
      if (action.description) wrapper.append(node("p", "", action.description));
      if (action.enabled === false && action.reason) wrapper.append(node("p", "", action.reason));
      list.append(wrapper);
    }
    if (!list.childElementCount) list.append(node("p", "usage-explanation", "No host actions are available for this installation."));
    const configure = actions.find((action) => action.id === "configure_https");
    const form = byId("host-setup-https-form");
    form.hidden = !configure;
    byId("host-setup-https-help").textContent = data.network?.https_enabled
      ? "HTTPS is configured. Existing address, port and certificate identity are preserved. Use the configured address and port to recheck; changing them is not supported here."
      : "Choose a local-network address or SSH tunnel access. For SSH, each computer must forward the same loopback address and HTTPS port. Trust the downloaded host certificate on each client.";
    const available = data.network?.https_addresses ?? data.network?.lan_addresses;
    const addresses = Array.isArray(available) ? available.filter((value) => typeof value === "string" && value) : [];
    const select = byId("host-setup-address");
    const chosen = select.value;
    if (JSON.stringify([...select.options].map((item) => item.value)) !== JSON.stringify(addresses)) {
      select.replaceChildren(...addresses.map((value) => {
        const option = node("option", "", value === "127.0.0.2" ? `${value} · SSH tunnel` : value);
        option.value = value;
        return option;
      }));
      if (addresses.includes(chosen)) select.value = chosen;
      else {
        try {
          const sharedHost = new URL(data.network?.public_origin).hostname.replace(/^\[|\]$/g, "");
          if (addresses.includes(sharedHost)) select.value = sharedHost;
        } catch {}
      }
    }
    const disabled = !configure || configure.enabled === false || !addresses.length || data.supported === false || working();
    byId("host-setup-https-submit").disabled = disabled;
    select.disabled = disabled;
    byId("host-setup-port").disabled = disabled;
    if (!byId("host-setup-port").dataset.edited && data.network?.port_https) byId("host-setup-port").value = data.network.port_https;
  }

  function setStatus(message, error = false) {
    byId("host-setup-status").textContent = message;
    byId("host-setup-status").classList.toggle("error", error);
  }

  async function refresh() {
    if (!active()) return;
    if (refreshing) return refreshing;
    byId("admin-refresh").disabled = true;
    refreshing = (async () => {
      try {
        const data = await options.api("/api/admin/host-setup");
        if (!active()) return;
        render(data);
        if (!data.job?.message && !changing) setStatus("Host details are up to date.");
      } catch (error) {
        if (active()) setStatus(`Could not check the host: ${error.message}`, true);
      } finally {
        byId("admin-refresh").disabled = false;
        refreshing = null;
      }
    })();
    return refreshing;
  }

  async function run(action, extra = {}) {
    if (!active() || !actionNames.has(action) || working()) return;
    const permission = snapshot?.actions?.find((value) => value.id === action);
    if (!permission || permission.enabled === false || snapshot.supported === false) return;
    changing = true;
    renderActions(snapshot);
    setStatus(`${actionLabels[action]}…`);
    try {
      const result = await options.api("/api/admin/host-setup", { method: "POST", body: JSON.stringify({ action, ...extra }) });
      if (active()) {
        await refresh();
        setStatus(result.message || "Host setup request accepted.", result.status === "failed");
      }
    } catch (error) { if (active()) setStatus(error.message, true); }
    finally { changing = false; if (active() && snapshot) renderActions(snapshot); }
  }

  async function runRelay(action, body, { sensitive = false } = {}) {
    if (!active() || !relayActions.has(action) || working() || !relayPermission(snapshot || {}, action)) return;
    changing = true;
    if (snapshot) { renderActions(snapshot); renderRelay(snapshot); }
    const verb = action === "probe_relay" ? "Checking the server host key…" : action === "bootstrap_relay" ? "Setting up the SSH relay…" : action === "relay_disconnect" ? "Disconnecting the relay…" : action === "relay_connect" ? "Connecting the relay…" : "Saving relay settings…";
    relayStatus(verb);
    let finalMessage = "", finalError = false;
    try {
      const result = await options.api("/api/admin/host-setup", { method: "POST", body });
      if (active()) {
        await refresh();
        finalMessage = sensitive ? "First-time setup started. Check the status here before retrying." : result.message || "Relay request accepted.";
      }
    } catch (error) {
      if (active()) {
        finalMessage = sensitive ? "First-time setup was not confirmed. Refresh Host setup and check the relay status before retrying; it may still be running." : error.message;
        finalError = true;
      }
    } finally {
      changing = false;
      byId("host-setup-relay-password").value = "";
      if (active() && snapshot) { renderActions(snapshot); renderRelay(snapshot); }
      if (active() && finalMessage) relayStatus(finalMessage, finalError);
    }
  }

  function clearRelayTransient() {
    byId("host-setup-relay-password").value = "";
    byId("host-setup-relay-confirm").checked = false;
  }

  function init(value) {
    if (options) return;
    options = value;
    byId("host-setup-port").addEventListener("input", (event) => { event.currentTarget.dataset.edited = "true"; });
    byId("host-setup-https-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const address = byId("host-setup-address").value;
      const port = Number(byId("host-setup-port").value);
      const addresses = snapshot?.network?.https_addresses ?? snapshot?.network?.lan_addresses;
      if (!addresses?.includes(address) || !Number.isInteger(port) || port < 1024 || port > 65535 || port === 8765) {
        setStatus("Choose a local host address and a port between 1024 and 65535, other than backend port 8765.", true);
        return;
      }
      run("configure_https", { address, port });
    });
    const relayUser = byId("host-setup-relay-user"), relayServer = byId("host-setup-relay-server");
    for (const field of [relayUser, relayServer]) field.addEventListener("input", () => {
      field.dataset.edited = "true";
      if (field === relayServer) {
        byId("host-setup-relay-observed").textContent = "Not checked for this server";
        byId("host-setup-relay-fingerprint").value = "";
        byId("host-setup-relay-confirm").checked = false;
      }
      if (snapshot) renderRelay(snapshot);
    });
    byId("host-setup-relay-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const user = relayUser.value.trim(), server = relayServer.value.trim();
      if (!user || !server || !relayMigrationApproved()) return;
      const replace_existing = relayMigrationRequired() ? true : undefined;
      byId("host-setup-relay-replace-existing").checked = false;
      runRelay("configure_relay", JSON.stringify({ action: "configure_relay", user, server, ...(replace_existing && { replace_existing }) }));
    });
    byId("host-setup-relay-connect").addEventListener("click", () => runRelay("relay_connect", '{"action":"relay_connect"}'));
    byId("host-setup-relay-disconnect").addEventListener("click", () => {
      if (window.confirm("Disconnect this SSH relay and stop automatic reconnect? If this browser reaches the host through that relay, this page may lose its connection.")) runRelay("relay_disconnect", '{"action":"relay_disconnect"}');
    });
    byId("host-setup-relay-copy-key").addEventListener("click", (event) => {
      const key = snapshot?.relay?.public_key;
      if (typeof key === "string" && key) options.copyText(key, event.currentTarget);
    });
    byId("host-setup-relay-probe").addEventListener("click", () => {
      const server = relayServer.value.trim();
      if (!server) { relayStatus("Enter the relay server above first.", true); return; }
      byId("host-setup-relay-observed").textContent = "Checking server host key…";
      runRelay("probe_relay", JSON.stringify({ action: "probe_relay", server }));
    });
    byId("host-setup-relay-bootstrap-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const adminUser = byId("host-setup-relay-admin-user").value.trim();
      const server = relayServer.value.trim();
      const fingerprint = byId("host-setup-relay-fingerprint").value.trim();
      const passwordInput = byId("host-setup-relay-password");
      if (!server || !adminUser || !passwordInput.value || !byId("host-setup-relay-confirm").checked) {
        relayStatus("Enter the server, administrator user, verified fingerprint, and one-time password.", true);
        clearRelayTransient();
        return;
      }
      if (!/^SHA256:[A-Za-z0-9+/]{43}$/.test(fingerprint)) {
        relayStatus("Enter only the independently verified SHA256 SSH host-key fingerprint.", true);
        clearRelayTransient();
        return;
      }
      if (!relayMigrationApproved()) { clearRelayTransient(); return; }
      const replace_existing = relayMigrationRequired() ? true : undefined;
      const body = JSON.stringify({ action: "bootstrap_relay", admin_user: adminUser, server, password: passwordInput.value, host_key_fingerprint: fingerprint, ...(replace_existing && { replace_existing }) });
      passwordInput.value = "";
      byId("host-setup-relay-confirm").checked = false;
      byId("host-setup-relay-replace-existing").checked = false;
      runRelay("bootstrap_relay", body, { sensitive: true });
    });
    byId("host-setup-relay-replace-existing").addEventListener("change", () => { if (snapshot) renderRelay(snapshot); });
    byId("host-setup-relay-password").value = "";
    byId("host-setup-relay-bootstrap").addEventListener("toggle", (event) => { if (!event.currentTarget.open) clearRelayTransient(); });
    new MutationObserver(() => { if (byId("host-setup").hidden) clearRelayTransient(); }).observe(byId("host-setup"), { attributes: true, attributeFilter: ["hidden"] });
    document.addEventListener("visibilitychange", () => { if (document.hidden) clearRelayTransient(); });
    window.addEventListener("pagehide", clearRelayTransient);
  }

  return { init, refresh };
})();
