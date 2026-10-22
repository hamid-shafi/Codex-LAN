# Get your team connected

Choose one role. You do not need to learn the repository layout first.
For an offline visual version, open [index.html](index.html) from an extracted
checkout. It makes no network requests and changes no settings.

## Host a workspace

Use a normal Linux desktop account, not root. From the extracted/cloned project:

```bash
bash setup.sh
```

Choose the host path. The menu also offers prerequisite checks, an optional
Python/OS package helper, and official Codex installation/sign-in help. You can
run the helpers directly:

```bash
bash setup.sh prerequisites --check
bash setup.sh prerequisites
bash setup.sh codex
bash setup.sh codex --login
bash setup.sh host
```

The OS helper currently installs packages on Debian/Ubuntu with your explicit
terminal approval; other distributions get guidance. The Codex helper is for the
**host**, not teammates. Sign in on the host using OpenAI's login flow—never send
login files, passwords, or API keys to a teammate or commit them.

The host installer creates a private Python environment and user services, then
opens the local administrator page. Finish in **Administration → Host setup**:
choose your LAN address, prepare HTTPS/Connector downloads, and optionally keep
the laptop awake. Existing running installations are not overwritten; use the
guarded updater for those. [Full host guide](../operations/SETUP.md).

## Join a team

1. Get the **HTTPS address** and **public CA fingerprint/certificate** from your
   administrator. Verify the fingerprint through a trusted channel before trusting
   the certificate. Do not bypass browser certificate errors.
2. Open that address → **Connect a new computer** → enter a computer name →
   **Request access**. Send the displayed verification code to the administrator.
3. Stay in the same browser/profile while they approve your device. A Windows
   browser can do this before WSL or a Connector is installed.
4. After approval, open **Connector → Download one-step setup**. Run your private
   installer as your normal Linux user (inside WSL2 on Windows). Pairing expires;
   download a fresh installer if necessary. Do not share it with anyone else.
5. Keep the Connector running and start a room. Only approve local tool access
   you intend to grant; full-access turns run commands under your OS user.

**You do not need the host's OpenAI login or your own Codex installation.** The
Connector package carries its runtime dependencies, but Linux/Python and the OS
service prerequisites must exist first. [Dependency guide](DEPENDENCIES.md).

## Windows

Extract the repository ZIP first, then double-click
[`setup/windows/start.cmd`](../../setup/windows/start.cmd). It opens the local
guide and provides checks; it does not require Python or change PowerShell policy.

The browser is native Windows. The Connector currently needs **WSL2 Ubuntu,
x86_64, CPython 3.10–3.14, and working systemd user services**. It does not run as
a native Windows app or in Git Bash. Windows ARM64 is not in the Connector bundle.
Use [the Windows/WSL guide](../operations/WINDOWS-WSL.md) for installation,
certificate trust, and troubleshooting. WSL access restrictions require your
Windows administrator; a ZIP cannot bypass them.

## Connect through a server

The relay server is a bridge, not a second app installation:

```text
Coworker's browser + Connector
          │ HTTPS inside SSH
          ▼
Restricted SSH relay (loopback-only forwarded port)
          │ laptop's persistent SSH tunnel
          ▼
Your awake laptop: app + conversations + Codex login
```

The administrator configures the relay accounts and laptop publisher tunnel
first. Then clients use the provided restricted SSH login and verified SSH/TLS
fingerprints. Linux users can launch the existing client tunnel with:

```bash
bash setup.sh relay YOUR_USER@YOUR_RELAY
```

Replace the placeholder with the administrator's details. Open the exact address
`https://127.0.0.2:8767`. Keep the SSH terminal open; a successful tunnel can be
silent. `Permission denied` means authentication failed, not that it is ready.
SSH authorization and app approval are separate steps. For Windows browser and
WSL Connector, verify connectivity in both environments.

[Relay administration and connection guide](../operations/SSH-RELAY.md).
Do not expose app ports publicly or distribute a shared root password.

## Know what stays private

History, uploads, usage records, sign-ins, keys, pairing files, and generated
installers stay in local runtime storage—not GitHub. A source ZIP is **not** a
backup of your conversations. [Private laptop migration](../operations/MIGRATION.md).

First setup can download software; model requests require provider connectivity.
The helpers are included, not a complete Python/WSL operating-system image.
