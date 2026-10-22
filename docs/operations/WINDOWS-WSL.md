# Windows browser + WSL Connector

Extract the source ZIP and double-click `setup/windows/start.cmd` for an
offline guide and read-only checks—no Python or PowerShell-policy change.
The Windows browser can request app access **before** WSL is installed.
For a same-LAN connection, use the host HTTPS address; no SSH tunnels are needed.

For a remote relay, run one tunnel in Windows for the browser and another in
WSL2 Ubuntu for the Connector. Both use `https://127.0.0.2:8767`. The
Connector is **not a native Windows app**: it needs WSL2 Ubuntu on x86_64,
standard CPython 3.10–3.14, and working systemd user services. ARM64 is not
bundled. Windows files can be reached through `/mnt/c` subject to permissions.

## 1. Verify your connection bundle

Get the public laptop CA, SSH host-key fingerprint, CA fingerprint, and your
assigned SSH login from the administrator. Verify both fingerprints through
a trusted channel. A source ZIP does not contain private team credentials.
A dedicated, restricted SSH account is safer than a shared root login.

`Permission denied (publickey,password)` means SSH authentication failed,
not that a tunnel exists. Ask the administrator to check your login or public
key; do not keep trying passwords or send private keys. Windows and WSL have
separate SSH key/configuration stores.

## 2. Windows browser tunnel and CA

In PowerShell, from the extracted **connection bundle**:

```powershell
.\connect_ssh_relay.ps1 YOUR_USER@YOUR_RELAY
```

If you have an authorized key, add
`-IdentityFile "$env:USERPROFILE\.ssh\codex-lan-relay"`.
Keep that window open. In a second PowerShell window, run
`.\connect_ssh_relay.ps1 -Check` to verify the app through the bundled CA.
A silent tunnel after authentication is normal; `Ctrl+C` closes it.

If scripts are blocked, **do not disable policy globally**. After reviewing
the script, this direct OpenSSH command supplies the same tunnel:

```powershell
ssh -NT -o StrictHostKeyChecking=ask -o ExitOnForwardFailure=yes -o NumberOfPasswordPrompts=1 -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o ForwardAgent=no -o ForwardX11=no -L 127.0.0.2:8767:127.0.0.1:18767 YOUR_USER@YOUR_RELAY
```

If `ssh` is missing, ask IT to enable Windows OpenSSH Client. To trust the
**verified public** laptop CA for this user only, run
`.\trust_relay_ca.ps1`. It checks the SHA-256 fingerprint, requires typed
confirmation, and imports to **Current User → Trusted Root Certification
Authorities**. Alternatively inspect `Codex-LAN-CA.crt` and import it
manually into that same store. If access is denied, ask IT; do not bypass it.
Open `https://127.0.0.2:8767` in Chrome/Edge and request app approval.

## 3. WSL Connector tunnel

As an ordinary WSL Ubuntu user, **not root**, verify systemd:

```bash
systemctl --user show-environment >/dev/null
```

If it fails, ask your Windows administrator to enable WSL systemd without
overwriting other `/etc/wsl.conf` settings. Save work before
`wsl --shutdown`: it stops all running WSL distributions.

From the extracted connection bundle in WSL, run and keep open:

```bash
cd '/mnt/c/Users/WINDOWS_USER/Downloads/Codex-LAN-Connect'
bash connect_ssh_relay.sh YOUR_USER@YOUR_RELAY
```

Add `--identity /path/to/private-key` for an already authorized
WSL key. Windows tunnel health does **not** prove WSL tunnel health.
In another WSL terminal, verify the pinned endpoint:

```bash
curl --noproxy '*' --cacert './Codex-LAN-CA.crt' --fail --show-error --max-time 8 https://127.0.0.2:8767/api/health
```

After app approval, download **Connector → Download one-step setup** in the
Windows browser and run your private, short-lived installer inside WSL:

```bash
bash '/mnt/c/Users/WINDOWS_USER/Downloads/Install-Codex-Connector-VERSION-private.sh'
```

Replace the placeholders with the actual paths. If you received separate
downloads, run the public installer with `--pair-file` pointing to your own
`codex-pairing-VERSION.json`. Never share pairing data. The installer asks
before enabling local tool access and pins the laptop CA. Check the service
with `systemctl --user status codex-lan-direct.service`.

Keep **both** tunnels and WSL running while working. Neither systemd nor a
Windows tunnel alone keeps WSL alive. Do not widen listeners or change
`.wslconfig` just to make an unverified tunnel appear connected.

[Microsoft WSL networking](https://learn.microsoft.com/en-us/windows/wsl/networking) ·
[WSL systemd](https://learn.microsoft.com/en-us/windows/wsl/systemd) ·
[Windows certificate stores](https://learn.microsoft.com/en-us/powershell/module/pki/import-certificate)
