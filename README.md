# Codex LAN

### Your Codex. Your team. Their own computers.

Share a Linux host's Codex with approved coworkers. Everyone gets their own rooms
and can work on files on their own computer through a Connector. The host keeps
the Codex sign-in and conversation data; an optional SSH relay carries only the
connection.

## Start here

| Your role | First step |
| --- | --- |
| **Host** on Linux | Run `bash setup.sh`, choose **Host**, then finish in **Administration → Host setup**. |
| **Join a team** | Open your host's HTTPS address → **Connect a new computer** → **Request access**. |
| **Use Windows** | Extract the ZIP and double-click [`setup/windows/start.cmd`](setup/windows/start.cmd). |
| **Connect through a server** | Ask the host for a restricted SSH login and follow the [relay guide](docs/operations/SSH-RELAY.md). |

[Short setup guide](docs/getting-started/README.md) ·
[Offline start page](docs/getting-started/index.html) ·
[Dependencies and offline limits](docs/getting-started/DEPENDENCIES.md)

## Why teams use it

- Share the assistant **without sharing its credentials**. Approve devices,
  set usage limits, and revoke access.
- Keep independent rooms, models, thinking levels, workspaces, read-only turns,
  files, drafts, and **Send update** during a response.
- Explore selected text in a resizable Side thread or persistent More details.
- Revisit tool Activity, public Progress logs, available Prepared context,
  and per-response usage reports.
- Use multilingual LTR/RTL content, Markdown/math, notifications, and
  reduced-motion-aware themes and effects.

[Product overview and features](docs/product/OVERVIEW.md) ·
[The story with Hamid](docs/product/STORY.md)

## Source map

`apps/` is the product; `setup/` installs and connects it; `docs/` explains
it; `development/` holds tests and maintainer tools. `setup.sh` is the Linux
entry point. [Repository map](docs/architecture/REPOSITORY.md).

Codex LAN is for **authorized, trusted coworkers**, not public hosting or
isolation of untrusted tenants. Full-access turns can run commands as the
relevant OS user. Usage percentages are estimates, not official billing.
Private chats, credentials, pairing data, and generated installers stay outside
Git. The project is independent of OpenAI; no project-wide license has been
granted.

[Security](.github/SECURITY.md) ·
[Contributing](.github/CONTRIBUTING.md) ·
[Third-party notices](docs/legal/THIRD_PARTY_NOTICES.md)
