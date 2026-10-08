# Linux host setup

The browser-based LAN app and the optional GTK desktop app are separate installs.
These instructions install the LAN host. Use a normal desktop account, not root.

## Requirements

- Linux with a working systemd user session and CPython 3.10 or newer.
- Python's `venv` and `ensurepip` modules. On Debian/Ubuntu, the package is
  `python3-venv`; install it through your software manager if setup reports it missing.
- OpenSSL for LAN HTTPS. A browser is needed for local sign-in and administration.
- An official standalone Codex installation, signed in on this host.
- Internet access for the initial Python dependencies and downloadable Connector
  build. The resulting Connector installers contain offline client dependencies.

Install Codex using the [official Linux instructions](https://learn.chatgpt.com/docs/codex/cli).
Then sign in with ChatGPT on this computer:

```bash
codex -c 'cli_auth_credentials_store="file"' login
```

This application currently reads the host's file-backed Codex login. If your usual
Codex login uses a keyring, the command above selects file storage for this login.
The credential remains private under `~/.codex`; setup never copies it into this
repository or a Connector package. For a headless machine, see
[official device sign-in](https://learn.chatgpt.com/docs/auth).

## Install from a clean checkout

Clone or extract the source into any directory, then run from that directory:

```bash
python3 project.py host --check
bash setup.sh
```

`setup.sh` creates a private Python environment, installs requirements from PyPI,
installs user services, starts the new host, and opens the local admin browser.
All executable and home paths are detected for the current Linux account.
It does not require a prebuilt `.lan-wheels` directory or a file in `/tmp`.

If you want to install without starting anything:

```bash
python3 project.py host
```

An existing running installation is preserved: the installer refuses to replace
it mid-conversation. Use the application's setup controls for configuration and
the safe updater for code releases.

## Finish in Administration → Host setup

1. Check host readiness and the current Codex login.
2. Choose the private IPv4 address actually assigned to this computer. HTTPS uses
   port 8767 by default; local-only administration remains on `127.0.0.1:8765`.
3. Prepare the downloadable Connector. This builds a credential-free package
   with the host's release version and an offline dependency bundle.
4. Enable keep-awake if this computer serves other users. The screen can still
   turn off or lock; the service inhibits automatic system suspend while active.
5. Share the displayed HTTPS address. Each person enrolls using their own browser
   and downloads their own one-use pairing file after administrator approval.

The setup screen can manage only this user's allowed services and local app
configuration. Operating-system settings that require authentication, such as
firewall rules or serving after logout, stay in the local OS settings. Passwords
are never requested in the browser or a chat.

For an initial command-line install, the equivalents are:

```bash
python3 project.py host --start --open --address YOUR_PRIVATE_IPV4 --keep-awake
```

Replace `YOUR_PRIVATE_IPV4` with your actual private address; it is not a literal
argument. Do not bind the service to a public address or expose it through router
port forwarding. Reserve the host's address in the router/DHCP settings so clients
keep a stable origin and certificate identity.

## Client connection

Open the host's HTTPS URL from a client on the same LAN. Verify the local CA
fingerprint with the administrator, then trust only that public CA certificate.
Never share `server-key.pem` or `ca-key.pem`.

Each client has its own approved browser identity and Connector. The Connector
runs local file and command tools under that client's Linux user; the host owns
model sessions. Approval allows powerful local access, so approve only intended
devices. Read-only controls can restrict individual turns.

The offline Connector currently targets Linux x86_64 and standard CPython
3.10–3.14. ARM64 and free-threaded Python are not included in the bundled client
installer; setup reports that limitation rather than downloading a wrong binary.

## Offline and advanced commands

Install the host from a previously prepared wheelhouse:

```bash
python3 project.py host --wheels /path/to/wheels
```

Prepare Connector downloads from the source checkout:

```bash
python3 project.py connector
```

The installed trusted source snapshot also retains its flat helper paths:

```bash
~/.local/share/codex-lan/venv/bin/python \
  ~/.local/share/codex-lan/setup-source/prepare_lan_connector.py
```

Add `--offline` to reuse dependencies cached by an earlier successful preparation.
Offline verification still requires a complete wheel set for every supported
client interpreter. Generated wheels, zipapps, archives, installers, and private
runtime data are local build/state artifacts and must not be pushed to GitHub.

## Laptop operation and troubleshooting

Keep the laptop connected to power while serving other users. Keep-awake does
not override every firmware policy or laptop-lid rule; configure “do nothing when
the lid closes” in your desktop Power settings if you need closed-lid operation.
Screen blanking and screen locking are safe to keep enabled.

User services normally follow the login session. If the host must serve after
logout, enable user lingering through the OS's authenticated administration
workflow (`loginctl enable-linger YOUR_LINUX_USERNAME`). This is optional and is
not executed automatically by the app.

If clients cannot reach the host, check the selected address, same-LAN membership,
HTTPS port, and desktop firewall. A disabled display alone should not disconnect
the service. The setup screen reports the service and certificate state; bounded
diagnostic logs are available with:

```bash
systemctl --user status codex-lan-server.service codex-lan-health-monitor.service
journalctl --user -u codex-lan-server.service -n 80 --no-pager
```

Do not repeatedly restart a service during active prompts. The health monitor
handles recovery conservatively, and the updater waits for active prompts before
activating a code release.
