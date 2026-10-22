# Get connected

Choose your role. For an offline, visual version, open [index.html](index.html)
from an extracted checkout.

## Host a workspace

On a Linux desktop, as your normal user:

```bash
bash setup.sh
```

Choose **Host**. The menu checks prerequisites and offers optional Python/OS
and official Codex helpers with confirmation. Sign in to Codex **on the host**.
Finish in **Administration → Host setup** to configure HTTPS and Connector
downloads. Keep the host awake when teammates need it. Do not reinstall a
running host during a response. [Host details](../operations/SETUP.md).

## Join a team

1. Get the host's HTTPS address and CA fingerprint from your administrator.
   Verify the fingerprint through a trusted channel; never bypass TLS warnings.
2. In your browser, select **Connect a new computer → Request access**. Give
   the verification code to the administrator and wait for approval in the
   same browser/profile. A Windows browser can do this before WSL or a Connector is installed.
3. Download **Connector → Download one-step setup** and run the private installer
   as your normal Linux user (WSL2 Ubuntu on Windows). A pairing download can
   expire; request a new one if needed.

You do **not** need the host's Codex login or your own Codex installation.
The Connector is for local file and tool access; full-access turns run under
your OS user. Do not share the private installer.
[Windows / WSL details](../operations/WINDOWS-WSL.md) ·
[Dependency limits](DEPENDENCIES.md).

## Windows

Extract the ZIP, then double-click [`setup/windows/start.cmd`](../../setup/windows/start.cmd).
It opens the local guide; it does not change PowerShell policy. The browser runs
on Windows, but the Connector currently needs WSL2 Ubuntu on x86_64 with CPython
3.10–3.14 and systemd user services. It is not a native Windows or ARM64
Connector. [Windows setup](../operations/WINDOWS-WSL.md).

## Connect through a server

The server is only a restricted SSH bridge; the awake laptop still hosts the
app, conversations, and Codex sign-in. After the administrator prepares the
relay, Linux clients can run:

```bash
bash setup.sh relay YOUR_USER@YOUR_RELAY
```

Then open `https://127.0.0.2:8767` and keep the SSH terminal open.
A silent tunnel can be healthy; `Permission denied` is not.
SSH authorization and app approval are separate steps. Check both Windows and
WSL connectivity when using a Windows Connector.
[Relay setup and troubleshooting](../operations/SSH-RELAY.md).

## Private data and downloads

Source ZIPs are not conversation backups. Chats, credentials, pairing files,
and history stay in local runtime storage.
[Move private history](../operations/MIGRATION.md). First installation may
download packages; model inference is online. No Python or WSL operating-system
image is bundled. [Exact dependency boundaries](DEPENDENCIES.md).
