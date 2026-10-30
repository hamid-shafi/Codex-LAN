# Linux host setup

Run as a normal Linux desktop user, **not root**:

```bash
bash setup.sh
```

Choose **Host**. The menu checks prerequisites and offers optional Python/OS
package and official Codex installation helpers; each installation asks first.
The LAN app is separate from the optional GTK desktop app.
[Quick start](../getting-started/README.md) ·
[Dependency boundaries](../getting-started/DEPENDENCIES.md).

## Requirements

Linux with systemd user services, CPython 3.10+ with `venv`/`ensurepip`,
OpenSSL, Internet for first-time downloads, and the official Codex CLI signed
in on this host. If a file-backed Codex login is needed, run:

```bash
codex -c 'cli_auth_credentials_store="file"' login
```

Credentials stay under the host user's `~/.codex`; they are never put in the
repository or a Connector bundle. Use the
[official Codex sign-in guide](https://learn.chatgpt.com/docs/auth) if needed.

## Install and finish in the browser

From an extracted or cloned checkout:

```bash
python3 development/project.py host --check
bash setup.sh host
```

The installer creates a private Python environment and user services, starts
**new** installations, and opens local administration. It refuses to overwrite
a running installation during a conversation. To prepare files without
starting the service, run `python3 development/project.py host`.

In **Administration → Host setup**, check readiness, choose this computer's
private LAN IPv4 address, prepare HTTPS/Connector downloads, and optionally
enable keep-awake. HTTPS defaults to port 8767; local administration remains
at `127.0.0.1:8765`. Share only the resulting HTTPS address. Each coworker
requests approval and downloads their own private Connector pairing.

Keep the address stable using your router/DHCP configuration. Do not expose the
app through public port forwarding. The browser cannot change firewall or
login-session settings that require OS administrator rights.

Equivalent first-install command:

```bash
python3 development/project.py host --start --open --address YOUR_PRIVATE_IPV4 --keep-awake
```

Replace the placeholder with the actual private address. For later code
releases, use the [guarded updater](LAN-RELEASES.md), not a fresh installer.

## Team connections

Clients verify the host CA fingerprint through a trusted channel and enroll
with their own browser identities. Never distribute the CA **private** key,
server private key, or host Codex credentials. Connector tools run under the
client's Linux user; approve only intended devices and file access.

The offline Connector bundle targets Linux x86_64 and standard CPython
3.10–3.14. Windows users can enroll in a native browser and run the Connector
in [WSL2 Ubuntu](WINDOWS-WSL.md). Remote teams can use an
[SSH relay](SSH-RELAY.md); the awake laptop still owns all conversations.

## Offline options and recovery

A prepared wheelhouse can be used for host dependencies:

```bash
python3 development/project.py host --wheels /path/to/wheels
python3 development/project.py connector
```

The Connector preparation command accepts `--offline` only when all required
client dependencies are cached. Installed releases retain their flat helper
paths; source ZIPs are not backups or credential bundles.

If serving after logout, ask the OS administrator about
`loginctl enable-linger YOUR_LINUX_USERNAME`. Keep-awake does not override
every lid or firmware sleep rule; screen blanking and locking are safe.
For connection problems, check the LAN address, HTTPS port, firewall, and:

```bash
systemctl --user status codex-lan-server.service codex-lan-host-worker.service
journalctl --user -u codex-lan-server.service -n 80 --no-pager
```

Do not repeatedly restart the service during active prompts.
