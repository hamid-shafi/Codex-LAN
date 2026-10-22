# Codex LAN: direct browser access and local execution

The browser connects to `https://SERVER-LAN-ADDRESS:8767`, using the address shown by host setup. The host browser and SSH tunnels can still use `http://127.0.0.1:8765`. These are two listeners on the same server, with the same rooms, approvals and history.

## First connection on each Ubuntu computer

1. Obtain `Codex-LAN-CA.crt` from the administrator through a trusted channel. Verify its SHA-256 fingerprint with the administrator before importing it as a trusted certificate authority in the browser's certificate settings. Do not disable certificate verification or ignore an unexpected certificate change.
2. Open the HTTPS address in Chrome. Enter this computer's name and request approval. Compare the displayed verification code with the administrator.
3. After approval, download the connector installer and a pairing file from **this computer's own browser session**. Do not copy the administrator's pairing file: it identifies the administrator's computer.
4. Run the installer as the normal desktop user, without sudo:

   ```bash
   bash Install-Codex-Connector.sh --pair-file codex-pairing.json
   ```

5. Review the local-access permission dialog. Approval permits reading/editing files and executing commands as this Ubuntu user. The connector does not grant root access or require the client's SSH password.

Pairing files expire after ten minutes and are single-use. Download a fresh file if pairing expired, its response was lost, or the server restarted before it was claimed. Long-lived device credentials remain in private local storage, not in the downloadable installer.

The offline bundle targets Ubuntu/Linux x86-64 with standard CPython 3.10–3.14. The installer selects a compatible installed interpreter and uses matching bundled wheels, without sudo or changing system Python. Other architectures and free-threaded Python builds require a matching package.

## Server-owned Codex sessions (0.3.0)

```text
Client browser ────── HTTPS/LAN ────── Host: rooms, approval, usage
Client Connector ─── outbound WSS ─── Host: one Codex process per client
    │                                    │
    └─ native exec-server                 └─ OpenAI via host Internet/proxy
       files and commands on the client
```

For direct HTTPS clients, the Connector runs only `codex exec-server`, not a
local model session. Provider sign-in and model connections stay on the host.
The host binds every model turn to that client's native execution environment;
missing or disconnected execution never falls back to host-file execution.
Only an approved bearer-authenticated connection can attach the executor.
The client neither needs nor inherits an OpenAI SOCKS proxy; pairing and update
downloads also bypass ambient proxies and retain the pinned server certificate.

This is not a system-wide VPN. A user-requested command such as `git clone` can
still need its own network access on the computer executing that command.
The host administrator's existing loopback worker continues running on the host.
The old SSH-only installer is legacy; use the direct HTTPS Connector for this
architecture.

### Lock/wake connection reliability (0.3.1)

Connector 0.3.1 allows a longer, symmetric WebSocket heartbeat window so a
brief desktop lock or wake stall does not look like a network failure. A real
socket loss still interrupts the active response exactly once; the app does not
silently replay it because tools may already have changed files or run commands.
Update both the backend and each Connector for this behavior.

Upgrade the backend before offering the 0.3.1 Connector. Old direct HTTPS model
workers are refused with an update-required response. Run the fresh installer
or choose **Check/install update** in the Connector; Chrome reload alone does
not replace the native process. Existing approval, room history and usage stay
intact. When a remote computer first connects with the new executor, its earlier
conversations become read-only: messages and images remain available, but sending,
steering and side-thread continuation are blocked. Create a new room to use
server-managed execution. New server-owned rooms remain usable after reconnects
and server restarts. The administrator's existing host-worker rooms are unaffected.

Native history migration is deferred. Older Codex conversations may use a
paginated SQLite history with absolute rollout paths; copying the visible JSONL
file is not enough to resume them safely. This release does not rewrite native
history databases, erase old messages or silently continue with missing context.

## Daily use

To update an already paired computer, download **Download update** from its
Connector panel and run that installer as the same desktop user. Existing
pairing is preserved; a fresh pairing file is only needed for initial setup or
explicit identity repair. Updating the browser alone does not update Connector.

Release metadata keeps `archive_url` at `/api/client/download/archive` because
installed native updaters require that endpoint. They validate the advertised
size, SHA-256, and payload version before activation. `versioned_archive_url`
is available for callers that support version-pinned downloads; versioned
installer and pairing links remain unchanged.

- Chrome can be closed without cancelling active work.
- With the app open, replies in your other rooms trigger an alert. Desktop notifications require browser permission; otherwise an in-app alert offers **Open room**. A visible room stays quiet, including when it is open in another tab. Reloading does not replay old alerts. Desktop permission is offered when sending a prompt and can also be enabled from **Reply notifications** in the quick switcher (`Ctrl+K`).
- **Send update** adds text or images to the active turn without starting a second turn. The model and thinking level remain unchanged for that turn.
- **Codex Connector** in the application menu opens chat, pauses/resumes local access, and checks for updates.
- Pause waits for active work to finish and blocks new work. Resume is explicit. The maintenance gate survives connector and server restarts.
- Locking the screen does not stop the services. Suspend, power loss, network loss and logging out are different; uninterrupted execution through those events is not promised.

## Per-response safety limits

Administrator responses are exempt from the app's per-response time and
model-usage safety stops. Provider limits still apply, and usage is still recorded.

For other users, open their device profile in **Administration**, change
**Limit each response**, and choose **Save limits**. This is enabled by default:
it stops a response after 45 minutes or unusually large recorded model usage.
Only administrators can change it. The setting applies to active and future
responses; enabling it can immediately stop a response already over its limit.
Disabling it does not resume a response that was already interrupted.

This switch does not change weekly shares, lifetime token budgets, usage history,
or execution permissions. It does not bypass provider-side limits.

## Weekly shares and response speed

The administrator's Usage page separates OpenAI's reported account allowance
from each computer's **estimated** consumption. Set **Weekly share of account
(%)** to `3` to allocate three percent of the weekly allowance. Blank means no
weekly cap; zero blocks new work. Existing lifetime token limits are separate.
Changing a share changes its denominator, not the recorded consumption: estimated
consumption of 1.5 percent of the account is 50 percent of a 3-percent share, or
30 percent after changing that share to 5 percent.

Every approved client can open **My usage** in the sidebar to see only its own
share, remaining allowance, reset time, and a plain-language calculation guide.
That view never requests administrator usage or discloses other clients' usage.
It leads with the percentage of the assigned share consumed, not the percentage
of the whole account. Smaller suitable models, Standard speed, and lower thinking
levels for simple tasks can reduce consumption; no fixed per-message cost is
promised.

Accounting stores integer millionths of a weighted usage unit, rather than
rounding consumption to whole account percentages. Weights use the model,
uncached input, cached input, output (including reasoning), and Standard/Fast
speed. Published credit rates are weighting references, **not** OpenAI's exact
included-subscription quota formula or charges. Rates are versioned in
`apps/workspace/codex_lan/usage_estimator.py`; consult the
[official pricing](https://learn.chatgpt.com/docs/pricing) when updating them.

The server reads the account's real weekly window and reset timestamp. It samples
read-only account limits at startup and every minute while work or weekly caps
exist. Calibration needs at least two percentage points of fresh account change
alongside known model-weighted activity. Until then the dashboard says Learning;
positive weekly caps are not enforced. Stale readings also suspend positive-cap
enforcement. A zero share remains blocked. Unknown legacy/model usage is marked
unavailable, never guessed from today's room settings. Consumption outside this
app, delayed or rounded reports, and unmetered tools can affect the estimate.
New work is checked at admission; active work is not killed at the limit and may
overshoot it. This is approximate allocation, not exact provider billing control.
Cache reclassifications crossing a weekly reset are marked partial and treated
conservatively rather than crediting old-week discounts against new-week work.

**Standard/Fast** is a per-room selector separate from reasoning effort. New turns
explicitly request Standard (`default`) or Fast; follow-up steering cannot change
the running turn's configuration. A switch to an unsupported model returns to
Standard. Fast's higher weight is shown beside the option; actual availability
still depends on the account. See the [official Fast documentation](https://learn.chatgpt.com/docs/agent-configuration/speed).

## Publishing changes

Work from the project checkout and run the relevant tests first.

Browser-only changes:

```bash
.venv-lan/bin/python development/project.py web
```

Users only need to reload Chrome. No connector reinstall is required.

Backend or protocol changes:

```bash
.venv-lan/bin/python development/project.py backend
```

This queues `codex-lan-backend-update.service` as a separate transient user service. It waits until all queued/running prompts finish, takes a database backup, and then activates the backend and host worker without being killed with the worker it is replacing. Do not manually restart these services during a prompt. Follow an activation with `journalctl --user -fu codex-lan-backend-update.service`. The updater retains old files and checks the loopback and configured HTTPS endpoints. An incompatible client protocol change also requires publishing a connector release.

Connector changes:

1. Increment `apps/workspace/codex_lan/__init__.py`'s version; never replace a published version with different executable code.
2. Verify the client tests and the offline package contents.
3. Publish:

   ```bash
   .venv-lan/bin/python development/project.py release
   ```

4. Clients choose **Check/install update** from **Codex Connector**. The update is downloaded over verified HTTPS, checked against the release checksum, prepared separately, and activated only after that computer's prompts finish. The previous release is retained for rollback. Chrome reload alone cannot update a locally installed connector.

Major-version changes and downgrades are not installed automatically. Device identity, worker state and chat history are not replaced by connector updates.

## Operational notes

- The listener binds only the configured private IP, not every interface. Keep the server IP stable, for example with a DHCP reservation. Address/certificate changes must be coordinated with clients.
- Do not port-forward the LAN service onto the public Internet. This deployment is intended for trusted teammates on the private network.
- Only explicitly approved devices receive worker access. Each non-admin device can run prompts only in its own rooms; the administrator can inspect usage and revoke access.
- In 0.3.0 direct mode, provider credentials stay in the host-side Codex process, not the client executor. Older worker releases received an access token in memory; updating cannot retroactively invalidate any token previously copied. This app is intended for trusted teammates, not hostile multi-tenant OS isolation.
- Relevant services: `codex-lan-server.service`, `codex-lan-host-worker.service`, and on direct clients `codex-lan-direct.service` (all user services).
- The health monitor protects active responses while new events are being saved. After four failed health checks, if the active-work count and event cursor stay unchanged for one minute, it restarts the server; interrupted responses remain in history and are not automatically resubmitted. A successful health check or fresh event progress resets this grace period. If health and database state both remain unreadable, recovery is deferred for at most three minutes after the failure threshold. An independent native timer logs Python thread stacks after a 30-second event-loop stall, including when the loop cannot handle signals; it never terminates the process itself.
- Server TLS material is under `~/.local/share/codex-lan/server/tls`. Share only the public CA certificate, never `ca-key.pem` or `server-key.pem`.
