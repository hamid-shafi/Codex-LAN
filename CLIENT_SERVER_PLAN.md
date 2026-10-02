# Codex client/server implementation plan

Status: initial LAN implementation installed alongside the current desktop app.
The server and host worker run independently on loopback port 8765. Local
multi-device simulations, browser checks, and a real local execution smoke test
have passed. Remote-client installation remains a user-run enrollment step.

## Requirements and decisions

- The chosen host machine runs the central service at its configured LAN address.
- The host's own interface uses the same client API, with an administrator role.
- Each device must receive explicit administrator approval once before accessing
  any rooms, attachments, model operations, or prompt execution.
- Approved devices see only their own rooms. The administrator can inspect all
  rooms and control devices. This interprets the user's first option selection.
- Existing rooms migrate to the administrator, preserving their IDs, replies,
  branches, and attachments. The user left this migration choice open.
- Prefer a browser interface, matching the user's preference for option two.
  Keep the existing desktop installation available during implementation.
- SSH is an available transport. Device authorization remains an application
  responsibility; an IP address or a shared SSH password is not a device identity.

## Confirmed execution and authentication

- Commands and edits execute on the originating client's own computer, through
  a local worker. Browser-only access cannot execute on a client without it.
- All workers use the server's existing Codex login. The supported experimental
  `chatgptAuthTokens` app-server flow supplies access tokens in memory. Refresh
  credentials stay on the server; provider tokens are never browser credentials.
  An approved machine's owner can inspect its process memory, so application
  revocation cannot invalidate a provider access token already copied elsewhere.
- All clients run Ubuntu. Expect 4–5 additional machines, with a maximum of 10.
- The worker and browser connect through an SSH local-port tunnel to a loopback
  service. Closing the browser does not stop the worker. No new public HTTP port
  is opened, and the current desktop broker is not reused or disconnected.
- Confirm each client's identity and network reachability before installation.

## Central service

The service owns history, attachments, runs, event replay, and usage accounting.
The browser renders and submits application operations. It never receives direct
access to unrestricted app-server JSON-RPC or the central database.

Extract the room and side-thread orchestration currently embedded in GTK into
headless services. Persist incoming execution events before distributing them,
and persist completion without relying on a connected browser. A disconnected
browser can resume from its last event cursor without duplicating messages.
Browser closure leaves a running job active when its execution worker remains
available. A disconnected local worker requires a visible interrupted/waiting
state; it cannot be assumed to keep executing.

Each request is authorized against the authenticated device. Room, message,
branch, attachment, event, export, cancellation, and deletion operations all
resolve ownership on the server. Client-supplied IDs do not establish ownership.
Upload bytes as attachments and return opaque IDs; do not accept an arbitrary
client path as permission to read a server file.

## Enrollment and control

Enrollment lifecycle: pending -> approved -> revoked. Pending requests can also
be rejected. Revoked devices require deliberate administrator action to regain
access.

A new device submits a display name and enrollment proof, and receives a pending
request identifier. The administrator sees a matching short verification code
and approves that specific request. Approval binds a unique device credential
to its server-generated identity. Store credential hashes, not reusable plaintext
secrets. A pending credential can inspect only its own enrollment status.

The host administrator uses a separately bootstrapped administrator credential.
Clients cannot assign their own roles or approve themselves. Enforce revocation
on existing streams and subsequent requests, and give the administrator a
separate explicit control to interrupt that device's active jobs.

Shared unrestricted SSH access to the host account also grants access to files
outside the app. If room privacy must hold beyond normal UI use, deployment needs
dedicated restricted access and isolated execution workers. A separate directory
under the same unrestricted OS account is insufficient.

## Usage and scheduling

Keep an append-only usage ledger separate from message deletion. Attribute runs
and available usage breakdowns to device, room, branch, model, thread, turn, and
response. Deduplicate replayed events and preserve cumulative baselines. Include
failed and cancelled runs when usage is supplied; mark missing usage as unknown.

The administrator dashboard shows active jobs, observed daily/monthly usage,
model breakdowns, and device status. Controls include approval/rejection,
revocation, stopping runs, allowed models/efforts, concurrency limits, and
configurable budgets. Enforce scheduling and model permissions on the server.
Token budgets can gate new work and trigger cancellation after reported usage;
an in-flight request can overshoot. Observed token totals do not establish an
exact monetary charge or share of a subscription allowance.

## Delivery stages

1. Settle execution/authentication choices and verify one target client. Define
   the service boundary and make a migration backup before touching live data.
2. Implement the isolated server database, approval flow, owner authorization,
   and migration into a new data location. Verify with two distinct identities.
3. Move turn orchestration and durable event/usage processing into the service.
   Add scheduling, interruption, and the selected execution worker arrangement.
4. Build the browser chat UI: Markdown/RTL, native selection, replies, side
   threads, multiple images, streaming, reconnect, and supported model efforts.
5. Add the administrator dashboard and client bootstrap/SSH connection setup.
6. Test host-client plus remote-client concurrently, then deploy to the verified
   remote host. Verify rollback and recovery before replacing the daily workflow.

## Initial delivery notes

- Implemented the isolated SQLite store, per-device approval and revocation,
  private rooms/uploads/events, persistent runs, usage ledger, browser client,
  outbound SSH client launcher, and host/client systemd worker setup.
- Host import copied 24 rooms/side threads, 835 messages, 29 images, and 21 thread
  rollout files. No missing images were reported. The source database remains
  untouched and a read-only snapshot backup is retained in the new server data.
- Browser native selection/Markdown/RTL, themes, images, reply/side rooms,
  reconnect, and dynamic model/effort selection have automated Chrome coverage.
- Actual model authentication lists Astra including Ultra. A real turn ran pwd
  in a dedicated local-worker test directory, returned the expected path, and
  recorded its observed usage. The temporary test room was removed afterward;
  its usage ledger remains by design.
- The user does not need to disclose any client password. Client .57 is reachable
  by SSH, but installation will be initiated from that computer instead of
  logging into it remotely. Each real device still awaits explicit admin approval.
- This first delivery is not full desktop feature parity: interactive tool
  questions fail explicitly; budgets gate new turns; monetary billing and
  granular per-model policy controls are not yet implemented. See CLIENT_SERVER.md.

## Acceptance checks

- A pending device cannot read history or invoke a model; an approved device can.
- Device A cannot access device B through guessed IDs, search, event cursors,
  replies, branches, attachments, exports, or execution tools.
- Revocation invalidates open sessions, and admin cancellation targets the
  selected client's jobs without stopping other clients.
- Concurrent streams and reconnect replay remain isolated and do not duplicate
  submissions, messages, or usage.
- History and usage survive client closure; server/worker restarts recover
  durable state and report interrupted runs honestly.
- Deleting a room retains its usage accounting; historical unknown metrics stay
  unknown. Sub-agent and side-thread usage is attributed without double counting.
- The administrator's own browser works through the same ownership-aware API.
