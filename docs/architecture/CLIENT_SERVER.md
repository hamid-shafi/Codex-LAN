# Codex LAN

This document describes the legacy SSH/local-model-worker deployment. For the
current HTTPS Connector with server-owned Codex processes and client-side native
tool execution, follow [LAN-RELEASES.md](../operations/LAN-RELEASES.md).

A browser client with a central history/administration service and a local Codex
worker on each Ubuntu computer. This is installed alongside the GTK desktop app;
it does not replace its broker, stop its prompts, or modify its original database.

## Connection layout

```text
Client browser ── localhost:8766 ── SSH tunnel ── Server 127.0.0.1:8765
Client worker ─── localhost:8766 ──┘                │ history / approval / usage
    │                                              │ shared model sign-in
    └── local Codex app-server ← approved connection┘
            └── commands and file edits on THAT client
```

The server's own browser and worker use port 8765 directly. No client SSH
password is needed by the administrator. The client establishes the outbound SSH
tunnel itself, entering the **server's** SSH password locally if necessary.
Each device has a separate app credential and must be approved before it can
read rooms or receive model access.

## Server setup

Use Python 3.10+ and install `apps/workspace/requirements.txt` in a virtual environment.

```bash
PYTHONPATH=apps/workspace python -m codex_lan.server --init
PYTHONPATH=apps/workspace python -m codex_lan.server
PYTHONPATH=apps/workspace python -m codex_lan.worker --server http://127.0.0.1:8765 \
  --token-file "$HOME/.local/share/codex-lan/server/admin.token" \
  --state-dir "$HOME/.local/share/codex-lan/host-worker" \
  --cwd "$HOME" --open
```

The server binds **only to loopback**. Do not expose its HTTP port to the LAN or
put provider credentials in browser JavaScript. The initial administrator token
is written privately to `admin.token`; it is not printed in logs. The worker uses
the official experimental externally managed ChatGPT-token flow. Only access
tokens are sent to approved workers; refresh tokens remain on the server.

## Install an Ubuntu client

The simplest option is the single **`Install-Codex-LAN.sh`** file on the server's
Desktop. Copy it to the Ubuntu client (USB, shared folder, or SCP), then run:

```bash
bash Install-Codex-LAN.sh
```

The installer includes the complete offline package; manual extraction or a
separate Python installation command is not needed. It checks the embedded
package before extraction and removes only its own temporary files afterward.
Run as the normal desktop user, not with `sudo`. It supports `--server USER@HOST`,
`--port PORT`, `--no-launch`, `--help`, and `--verify` (check only; no installation).

For example, these commands run **on the client**, not on the server:

```bash
scp user@server-host:~/Desktop/Install-Codex-LAN.sh .
bash Install-Codex-LAN.sh
```

The original archive remains available as an alternative:

```bash
scp user@server-host:~/Desktop/Codex-LAN-Client.tar.gz .
tar -xzf Codex-LAN-Client.tar.gz
python3 Codex-LAN-Client/setup_lan_client.py --server user@server-host
```

The archive contains the client, Codex CLI and Python 3.10 wheels for the server's
Ubuntu x86-64 architecture. Other Python versions need a matching bundle. It contains no chats, device credentials or OpenAI
credentials. Installation is user-local and needs no `sudo`. On the first SSH
connection, verify the server host-key fingerprint before accepting it.

The browser displays **Pending approval** and a verification code. In the host
browser open **Administration**, check the device/code, then approve it. The
browser and worker proceed automatically. Future app launches reuse that
device identity. Use the **Codex LAN** launcher to reconnect a lost SSH tunnel.

The background worker remains running when the browser closes. System locking
does not deliberately stop it, but suspend, shutdown, logout policies, network
loss or terminating the worker can interrupt runs. Interrupted runs are recorded
as such; prompts are never automatically resubmitted after uncertain failure.

```bash
systemctl --user status codex-lan-client.service
systemctl --user stop codex-lan-client.service
journalctl --user -u codex-lan-client.service -n 30
```

After reboot, start Codex LAN to establish the SSH tunnel again. SSH password
authentication may be requested again; one-time **app approval** is separate
from SSH authentication. Existing SSH keys can avoid repeated SSH passwords.

## Administration and boundaries

- Clients see only their own rooms, history, uploads and events. The admin can
  inspect all rooms and revoke devices or stop selected device jobs.
- Admin inspection does not silently run commands on another computer.
- Multiple clients and multiple rooms can run independently, subject to each
  device's concurrency limit. The default is one active run per device; the admin
  can raise it to ten. A single room has at most one active run.
- Model lists and valid thinking levels come from each worker. Invalid
  model/effort combinations are rejected server-side.
- Token accounting records observed per-run cumulative usage, not the sum of
  repeated snapshots. Room deletion does not erase the usage ledger. Daily and
  monthly values use UTC. This is not an exact currency bill or subscription
  quota percentage. Unreported usage remains unknown.
- Token budgets gate new turns. Reported usage can arrive late and one turn can
  exceed the remaining budget. Provider limits are still shared by all clients.
- Revoke disconnects the app's worker and prevents further app access. An
  approved machine's owner can inspect its memory and copy an access token;
  revocation cannot invalidate such a copied upstream token retroactively.
- Sharing a normal unrestricted SSH account allows its users to access that
  account's files outside this app. App room isolation is not an OS security
  boundary against people who also have the server account password.

## Desktop history import and rollback

`python -m codex_lan.migrate --help` documents snapshot import. It opens the old
SQLite database read-only, creates a backup in the new data directory, imports
rooms/side threads/images into the admin account with deterministic ID mappings,
and can copy matching thread rollouts into the host worker's isolated home.
It does not copy authentication files. Repeat imports skip existing records.
Historical token counters are deliberately not treated as new measured usage.
This is a snapshot, not continuous two-way synchronization with the desktop app.

Stop only the new LAN service before importing; its lock prevents concurrent
server/import writers. To return to the original desktop workflow, stop the LAN
worker/server and use the existing Codex launcher. Its data is untouched.

## Current scope

Browser chat, native selection, Markdown/code, Persian direction handling,
image uploads, replies/side rooms, models/effort, per-device approval, usage and
revocation are the initial LAN implementation. It is not full feature parity
with every desktop feature. Unexpected interactive tool questions currently fail
explicitly rather than hanging. Local workers execute with the same full-access
behavior requested for the desktop app; choose the working directory carefully.

## Failed responses and large imported rooms

Version 0.1.1 resumes existing Codex threads with `excludeTurns: true`: stored
context is retained, while the browser continues paging history from the central
database. This avoids replaying an entire large imported conversation through a
single worker response.

The header distinguishes a reachable server from a ready local worker. A failed
response appears once in the assistant card. **Edit & retry** restores its prompt
and images to the composer for review; it never automatically submits a turn.
Check any partial work before sending again. Room controls in the sidebar require
confirmation before deletion, and active rooms cannot be deleted.

Protocol reference: [Codex app-server authentication](https://learn.chatgpt.com/docs/app-server#3c-log-in-with-externally-managed-chatgpt-tokens-chatgptauthtokens).

## Verified delivery (2026-09-08)

- Full desktop plus LAN regression suite: 291 passing tests, including 5 Chromium
  browser tests for failures, retry drafts, navigation, selection and room controls.
- Server and host worker installed as separate user services. Existing GTK app,
  persistent broker and model process were left running unchanged.
- Real local execution smoke returned the exact temporary worker directory and
  recorded usage; no other real model prompt was needed for LAN verification.
- Imported snapshot: 24 rooms/side threads, 835 messages, 29 images, 21 rollouts.
- Ubuntu x86-64/Python 3.10 client archive on the server Desktop:
  `Codex-LAN-Client.tar.gz` (about 130 MB).
- The self-contained `Install-Codex-LAN.sh` embeds that same package and is the
  preferred client installation entrypoint. No client password is needed.
- Version 0.1.1 also passed a no-inference reproduction with the 93 MB imported
  debug rollout: metadata-only resume succeeds where full-history resume exceeded
  the worker's 8 MiB frame limit. Production browser rendering and worker health
  were checked after a LAN-only service update; native desktop processes remained
  running. Previous code and installers were retained in a private rollback copy.
- Archive SHA256: `d454ca75e5b7a77761d0381c90c501bdc1af0cd1d66ba8e09f0917442eb9fea4`.
- Remote .57 installation and end-to-end SSH/client execution are not yet
  verified; they begin when its user runs the installer and the administrator
  approves the matching device code. Local two-client concurrency was tested.
