# Codex LAN

**Start here:** [One-page overview](OVERVIEW.md) · [Development story](STORY.md).

**Share your Codex with your team at work.** Codex LAN turns one Linux host into
a shared coding-assistant workspace for approved coworkers on a trusted office
network. People connect from their own browsers, keep separate conversations and
access limits, and use native Connectors to work on their own computers. The host
owns the Codex login; teammates do not need its login credentials.

This is an independent interface around the official Codex app server—not an
OpenAI product or a public account-sharing service. Use it only with authorized,
trusted colleagues and within your organization's and provider's access rules.
No OpenAI API key belongs in this repository.

The browser supports multilingual LTR/RTL content, Markdown and math, file attachments,
independent model/thinking controls, read-only turns, streaming activity, persistent
side threads/details, saved drafts, themes, unread indicators, and notifications.
Administrators approve devices, review reported/estimated usage, and configure
the host through **Administration → Host setup**.

### Language and writing direction

The interface, developer comments, and public documentation are in English.
Message and draft direction uses Unicode bidirectional character classes, not
an assumption that every non-Latin character is Persian or right-to-left.
This covers Arabic, Persian, Urdu, Hebrew, Syriac, Thaana, NKo, Adlam, and other
RTL scripts recognized by the bundled browser data or Python's Unicode database
in the optional desktop client. Mixed-script drafting remains supported; code,
math, and password fields keep their left-to-right presentation.

Vazir is bundled for Arabic-script typography; other scripts use available
system/browser fallback fonts. Writing-direction support is not full interface
translation or a promise that every system has fonts for every script. Multilingual
test fixtures intentionally retain their original text.

`Ctrl+K` / `Cmd+K` uses the physical browser key position, so switching the keyboard
to Persian, Arabic, or Hebrew does not disable the Quick Switcher.

Each response's **Activity → Progress log** keeps the public updates shown while
Codex was working, even after the final answer arrives or the page is reloaded.
Tool steps and their output remain in the Activity tab. These bounded, redacted
logs are private room data stored on the host, not private reasoning or source
files included in a repository export; older runs may have no archived updates.

## Start on a new Linux host

You need Linux, Python 3.10+, Python's `venv` support, a working systemd user session,
OpenSSL for HTTPS, and the official Codex CLI. Run as your desktop user, not root.
Install and sign in to Codex locally before starting the shared host; never paste
a password or copy an authentication cache into chat.

```bash
codex -c 'cli_auth_credentials_store="file"' login
python3 install_lan_server.py --check
bash setup.sh
```

`setup.sh` installs a private Python environment and user services, then opens the
local administrator page. It downloads Python dependencies unless you supply an
offline wheel directory. The source checkout stays separate from runtime data.
It refuses to reinstall over a running server.

In **Host setup**, choose the actual private IPv4 address of the new computer,
prepare HTTPS and the downloadable Connector, and optionally prevent host sleep.
The screen shows prerequisites, service state, certificate fingerprint, download
status, and safe next actions. Privileged firewall/OS changes still require local
operating-system approval, not a password entered into this app.

See [the complete setup guide](docs/SETUP.md), [laptop migration and private backups](docs/MIGRATION.md),
and [the current architecture and Connector guide](LAN-RELEASES.md).

## Trust and access

Only loopback HTTP is enabled initially. LAN clients use the configured private
HTTPS address and verify the host's local CA. Keep this on a trusted LAN; it is not
a public-internet deployment recipe. Client pairing files are per-user, expiring,
private credentials and must not be committed.

Codex can run commands and modify files accessible to the host account when full
access is enabled. Read-only is a per-turn execution setting. Do not expose a
personal host to untrusted users, and never enter a `sudo` password in chat or the
setup UI. Locking the screen may remain enabled; suspending the host stops access.

Reported token usage and account-percentage estimates are different measurements.
Missing usage is not counted as zero, and estimates are not official billing data.

### Context and compaction

New and resumed turns use a bounded 96,000-token budget for **new content after
the retained prefix**, with a 12,000-token tool-output budget. This balances
context growth against repeated compaction pauses; Codex's own model-window
limit remains authoritative. Raising the previous 64,000-token budget can reduce
compaction frequency, but can also increase input tokens in individual model
requests. It does not promise lower usage or a fixed compaction duration.

Normal turns send the current message and attachments, not a second copy of the
visible room history or the Prepared context display. Recorded historical limits
remain unchanged. See [OpenAI's compaction guide](https://developers.openai.com/api/docs/guides/compaction)
for the underlying context-management mechanism.

## Private state stays off GitHub

Installed runtime state lives under `~/.local/share/codex-lan/`: databases, uploads,
tokens, pairing material, TLS keys, bundles, and logs. OpenAI authentication stays
in the local Codex credential store. Do not add any of these to the repository,
even in a private GitHub repository or its history.

Before publishing, follow [the publication checklist](docs/PUBLISHING.md) and run
the repository's publication check. Its source-only export excludes `.git` and
runtime state. Private history snapshots are saved separately with mode `0600`;
they are not source exports and should be transferred only by a private channel.

There is currently no project-wide license grant. Third-party assets retain their
own licenses; see [the third-party inventory](THIRD_PARTY_NOTICES.md).

## Development and verification

```bash
python3 -m venv .venv-lan
.venv-lan/bin/python -m pip install -r requirements-lan.txt
PYTHONPATH=.:tests .venv-lan/bin/python -m unittest discover -s tests -p 'test_lan_*.py'
```

Tests use temporary state and loopback listeners. A running server should be
updated through its guarded deployment path after active prompts finish, not by
blindly restarting it. See [release procedures](LAN-RELEASES.md).

The older GTK desktop client remains available as an independent optional app:
`python3 app.py --check`, `bash run.sh`, and `bash install.sh`. Its private legacy
history directory is `~/.local/share/hamsohbat-codex`; it is not included in a clean
LAN installation unless you explicitly request the existing desktop-history import.
