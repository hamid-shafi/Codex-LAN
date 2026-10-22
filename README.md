# Codex LAN

### Your Codex. Your team. Their own computers.

Turn a Linux laptop or workstation into a shared coding workspace for approved
coworkers. Keep the Codex sign-in and conversations on the host; teammates join
in a browser and use a Connector to work with files on their own computers.

**At the office or through an SSH relay:** the relay carries the connection,
not your database or your Codex credentials. The host must stay awake and online.

## Start with your role

| I want to… | Start here |
| --- | --- |
| **Host a workspace** on my Linux computer | Run `bash setup.sh` → Host · [Host guide](docs/operations/SETUP.md) |
| **Join my team's workspace** | Open the address from your administrator → **Connect a new computer** → **Request access** · [Join guide](docs/getting-started/README.md#join-a-team) |
| **Connect from Windows** | Extract the repository, then double-click [`setup/windows/start.cmd`](setup/windows/start.cmd) · [Windows / WSL](docs/operations/WINDOWS-WSL.md) |
| **Reach a laptop through a server** | Use the administrator's restricted SSH login · [Relay guide](docs/operations/SSH-RELAY.md) |

**Downloaded the source?** Open [`docs/getting-started/index.html`](docs/getting-started/index.html)
locally for a guided start page—no Python, web server, or Internet needed to read it.
On GitHub, use the [Getting started guide](docs/getting-started/README.md).

Missing Python or Codex? The setup menu includes prerequisite checks and optional
installation helpers. It explains changes before asking for confirmation.
First-time downloads still need Internet access; this is not a bundled operating
system or an offline model. [What is included?](docs/getting-started/DEPENDENCIES.md)

## Built for a working team

- **Controlled sharing:** approve devices, assign usage limits, revoke access;
  teammates never need the host's sign-in credentials.
- **Independent work:** rooms, model/thinking/speed settings, read-only turns,
  attachments, and **Send update** while a response is running.
- **Explore without losing your place:** resizable Side threads and floating
  More details, with saved drafts and history across reloads.
- **See what happened:** streaming Activity, preserved public Progress logs,
  available Prepared context, and per-response usage reports.
- **A considered interface:** themes, Markdown/math, multilingual LTR/RTL,
  keyboard navigation, unread indicators, and focus-aware notifications.

[Feature details](docs/product/FEATURES.md) · [The one-page overview](docs/product/OVERVIEW.md)
· [The story: built with Hamid](docs/product/STORY.md)

## A small map

| Folder | What's inside |
| --- | --- |
| [`setup/`](setup/README.md) | Install, connect, update, and back up |
| [`apps/`](apps/) | Shared workspace and optional desktop app |
| [`docs/`](docs/README.md) | Getting started, product, architecture, and operations |
| [`development/`](development/README.md) | Tests and maintainer tools |

`setup.sh` is the Linux entry point. `project.py` and `project_layout.py` support
developer commands and stable installed paths. [Repository guide](docs/architecture/REPOSITORY.md).

## Trust comes first

For authorized, trusted coworkers—not public hosting or isolation between
untrusted tenants. Full-access turns can execute commands as the relevant OS
user. Use only within your organization's and provider's access rules.
Usage percentages are estimates, not official per-user billing.

Databases, chats, uploads, pairing files, certificates, and credentials stay
outside Git. This is an independent project, not an OpenAI product. No
project-wide license has been granted.

[Security](.github/SECURITY.md) · [Contributing](.github/CONTRIBUTING.md)
· [Third-party notices](docs/legal/THIRD_PARTY_NOTICES.md)
