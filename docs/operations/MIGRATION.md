# Moving the host to another Linux computer

Source code and private runtime state are separate. GitHub carries only reviewed
source; chat history, user/device identities, pairing files, credentials, databases,
TLS keys, logs, and generated client installers stay off GitHub.

## Recommended: fresh host on the laptop

1. Clone or copy the reviewed source onto the laptop. Do not copy the whole old
   home directory, `.git` from an unaudited checkout, or `~/.codex/auth.json`.
2. Install official Codex and sign in on the laptop with your own account.
3. Follow [SETUP.md](SETUP.md): run `bash setup.sh`, then use Host setup to configure
   this laptop's actual LAN address and prepare its matching Connector.
4. Keep the laptop awake while serving clients, and reserve its address in DHCP.
5. People enroll and pair to the new host from their own browser on each client.
   An existing Connector tied to the old server origin must be deliberately
   removed/reconfigured locally before it can be paired to the new host.
6. After confirming the laptop works, stop the old host's services locally when
   its active prompts have finished. Keep the old machine's private state until
   you have decided whether its history is still needed.

This starts a new workspace without importing old chats or secrets. Both hosts
can stay available during the test, but they do not share live conversation state
and should not use the same copied credentials.

## Create a concrete private history backup

The included helper creates a SQLite-consistent history snapshot and copies its
referenced uploads, device names, usage records, and saved room settings. It uses
SQLite's backup API rather than copying a live database/WAL blindly. It does not
change or restart the source service.

```bash
python3 project.py backup \
  --output "$HOME/.local/share/codex-lan-private-backups/private-history-before-laptop.tar.gz"
```

Choose a new filename each time. The output directory is private (`0700`) and the
archive is `0600`; saving it inside the source checkout is refused. Password
verifiers, browser sessions, pairing records, and device credentials are removed
from the snapshot; device authorization is disabled. No `admin.token`, OpenAI
login, TLS key, log, or generated private installer is copied. Chat text and
attachments can still contain sensitive content you wrote, so transfer the
archive privately and never put it in GitHub.

`manifest.json` identifies the format and counts; `history.sqlite3` and `uploads/`
hold the preserved data. Attachment paths are relative, and native execution
resumption is disabled. This is an offline preservation artifact, not an automatic
restore of the old server. Keep the original host until a checked import is
available and has been verified. Prefer creating the backup after active prompts
finish so a message and any newly arriving attachment are captured together.

## Selected rooms with native conversation memory

For an independent Linux host that must continue selected administrator-owned
rooms, use the selective transfer tool. Unlike the history-only backup above,
this includes native Codex history, compacted memory, descendant agent sessions,
and referenced attachments. It does not issue model requests or restart the
source host.

The first format deliberately supports only the reviewed **Codex CLI 0.159.3**
paginated SQLite schemas. Both versions and schemas are checked; an unknown
format is refused rather than guessed. Export only root rooms without companions
from a single administrator. Obtain their IDs from the room URLs.

```bash
python3 setup/maintenance/transfer_lan_history.py export \
  --room ROOM_UUID --room ANOTHER_ROOM_UUID \
  --output "$HOME/.local/share/codex-lan-private-transfer/selected-history.zip"
```

The output directory must be private (`0700`) and outside any Git checkout.
The ZIP is `0600`, **not encrypted**. Login files, device credentials, pairing,
TLS keys, global usage calibration, and unrelated conversations are excluded.
Messages, native history, and attachments can themselves contain pasted secrets:
keep the ZIP private, including when sending it to yourself. Native history can
be several gigabytes before compression; do not assume it fits an email attachment.

On the laptop, install the reviewed application with `bash setup.sh` and sign in
to Codex locally. Do not create rooms or start prompts. Extract the trusted ZIP
outside Git, enter its directory, and run:

```bash
bash restore-history.sh
```

The importer requires an empty installation with its own approved administrator
and the verified Codex version. It verifies checksums before stopping destination
services, takes an exclusive server lock, backs up existing empty stores, imports
native memory and LAN history, and restores the previous service state. Failed
imports roll back; backups remain under the destination's
`~/.local/share/codex-lan/transfer-backups/`. It refuses nonempty histories and
the original source administrator. Do not use it as a merge or synchronization
tool. A crash or power loss during import requires reviewing the retained backup
before retrying; it is not an unattended disaster-recovery service.

Room ownership is reassigned to the new local administrator. Historical per-answer
usage remains visible, while provider/account calibration is fresh. Active runs
are interrupted in the exported copy and pending follow-ups are not replayed.
Review the saved activity before continuing. Messages arriving after the snapshot
stay on the original host; the two installations are independent.

Project working trees, external services, and Git/Gerrit credentials are not
copied. Clone projects separately and select the correct workspace before asking
the agent to change files. Historical text and rollout bytes are preserved, so
old paths can still appear in messages; filesystem metadata is remapped without
rewriting the memory or breaking native byte offsets. Keep the old host until you
have verified both the displayed history and continuation on the laptop.

## If old identities must also be preserved

A raw runtime directory is not a portable backup. `network.json` contains the old
address and certificate paths; sessions and workspaces may contain old absolute
paths; a live SQLite database may have an active WAL. Copying these blindly can
lose the latest messages or point clients and tools at the wrong computer.

Neither helper transfers usable credentials or preserves other users' identities.
Moving an entire deployment with existing enrollments therefore needs a separate
reviewed import plan. Keep OpenAI login separate and sign in again on the new host
rather than distributing an authentication cache.

The setup screen supports a fresh installation, not an automatic full identity
import across changed origins. Preserve the old host until paths, TLS identity,
client credentials, and room ownership have been reconciled. Do not delete old
data or claim a migration succeeded merely because the new health endpoint responds.

## What goes where

| Item | Location/policy |
| --- | --- |
| Reviewed source | GitHub or the new source checkout |
| Host application, private Python environment | Installed again under the new user's home |
| ChatGPT/Codex login | Sign in locally on the new host |
| Chats, uploads, usage, device identities | Private backup/import only; never GitHub |
| HTTPS address and private keys | Configure for the new host; never publish keys |
| Connector installer | Generate from the trusted source snapshot |
| Client pairing | Each user downloads their own approved, expiring file |
| Screen lock, power, firewall, lingering | New machine's Host setup and OS settings |
