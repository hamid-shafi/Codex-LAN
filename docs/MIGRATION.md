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
python3 backup_lan_history.py \
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

## If old identities and native sessions must be preserved

A raw runtime directory is not a portable backup. `network.json` contains the old
address and certificate paths; sessions and workspaces may contain old absolute
paths; a live SQLite database may have an active WAL. Copying these blindly can
lose the latest messages or point clients and tools at the wrong computer.

The helper above deliberately does not transfer usable credentials or native
Codex rollout/session state. A complete private migration therefore needs a
separate reviewed import plan. Keep OpenAI login separate and sign in again on
the new host rather than distributing an authentication cache.

The current setup screen supports a fresh installation, not an automatic full
history/identity import across changed origins. Preserve the old host until a
checked import has reconciled paths, TLS identity, client credentials, and room
ownership. Do not delete old data or claim a migration succeeded merely because
the new health endpoint responds.

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
