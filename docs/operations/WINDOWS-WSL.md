# Windows browser + WSL Connector

Use a native Windows SSH tunnel for your browser, and a Linux tunnel inside
WSL 2 for the Connector. Both use `https://127.0.0.2:8767` and the same laptop CA.
The laptop remains the application host; the SSH server only relays traffic.

The Connector runs in **WSL 2 Ubuntu on x86-64**, not directly in Windows,
PowerShell, or Git Bash. It can work with Windows files exposed under `/mnt/c`,
subject to your access mode and filesystem permissions. ARM64 is not bundled.

## 1. Get the connection bundle and SSH login

Extract the complete connection ZIP, including its public CA certificate and
fingerprints. Use the SSH username already assigned to you. An existing `root`
SSH login is accepted; the launcher does not create or change server accounts.
Dedicated restricted accounts are preferable to shared root access.

Verify the relay SSH fingerprint and laptop CA fingerprint with the administrator
through a trusted channel. SSH access and application approval are separate.

If SSH says `Permission denied (publickey,password)`, stop: no authenticated
tunnel exists. Changing a certificate, browser, or WSL setting will not fix SSH
credentials. Ask the administrator to verify your login or authorize your public
SSH key. Do not repeatedly try passwords, send private keys, or share pairing
files. Windows and WSL have separate SSH configuration and key stores.

## 2. Open the Windows tunnel

In a normal PowerShell window, change to the extracted folder, then run:

```powershell
.\connect_ssh_relay.ps1 YOUR_USER@YOUR_RELAY
```

If using an authorized key:

```powershell
.\connect_ssh_relay.ps1 YOUR_USER@YOUR_RELAY -IdentityFile "$env:USERPROFILE\.ssh\codex-lan-relay"
```

Keep this window open. After successful authentication, the tunnel is normally
silent: no remote shell prompt appears. The initial instructions alone do not
prove that authentication succeeded. In another PowerShell window, run:

```powershell
.\connect_ssh_relay.ps1 -Check
```

This probes the real app using the bundled CA, without bypassing TLS verification.
It distinguishes a listening port from a valid application connection. `Ctrl+C`
closes the tunnel. Do not change the HTTPS host or port.

If Windows policy blocks PowerShell scripts, do not disable it globally. Review
the files with your administrator; the equivalent tunnel command can be run
directly in PowerShell without running a script:

```powershell
ssh -NT -o StrictHostKeyChecking=ask -o ExitOnForwardFailure=yes -o NumberOfPasswordPrompts=1 -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o ForwardAgent=no -o ForwardX11=no -L 127.0.0.2:8767:127.0.0.1:18767 YOUR_USER@YOUR_RELAY
```

If `ssh` is missing, ask your Windows administrator to enable the OpenSSH Client
optional feature. No server components or public firewall rules are required.

## 3. Trust the CA for this Windows user

Run this once, after independently verifying the fingerprint:

```powershell
.\trust_relay_ca.ps1
```

It verifies the full SHA-256 certificate fingerprint, displays it, and requires
you to type `TRUST`. It imports only the public CA into **Current User → Trusted
Root Certification Authorities**, never Local Machine. It does not elevate,
disable certificate checks, or change Windows policy. To remove this trust later,
use `certmgr.msc` and remove only the certificate with that exact fingerprint.

If scripts are blocked, double-click `Codex-LAN-CA.crt`, inspect the certificate,
then use **Install Certificate → Current User → Place all certificates in the
following store → Trusted Root Certification Authorities**. Compare its SHA-256
fingerprint through the administrator before accepting. If Current User also
returns `Access denied`, your organization may restrict certificate trust; ask
IT to approve it rather than trying to bypass the restriction.

Open `https://127.0.0.2:8767` in Windows Chrome or Edge, request access, and have
the host administrator approve your displayed verification code. Browser CA
trust does not grant application access.

## 4. Prepare the WSL Connector

Use Ubuntu under WSL 2 as your ordinary Linux user, **not root or sudo**. The
remote SSH username can still be `root`; that is separate from your local user.
The Connector requires standard CPython 3.10–3.14 and a working systemd user
session. Check it without printing environment variables:

```bash
systemctl --user show-environment >/dev/null
```

If this fails, follow Microsoft's systemd setup instructions below. Preserve
existing `/etc/wsl.conf` settings if you need to enable `[boot] systemd=true`.
Save all work before restarting WSL; `wsl --shutdown` stops every WSL distro and
its running commands. The Connector does not change these settings for you.

Open the Linux tunnel from the extracted folder, for example:

```bash
cd '/mnt/c/Users/WINDOWS_USER/Downloads/Codex-LAN-Connect'
bash connect_ssh_relay.sh YOUR_USER@YOUR_RELAY
```

Or add `--identity /home/user/.ssh/codex-lan-relay` (replace `user` with your Linux username) for a key already
authorized by the SSH administrator. Keep the WSL tunnel terminal open. NAT and
mirrored WSL networking behave differently; a Windows listener is not proof of
a working WSL listener. We do not change `.wslconfig`, enable wildcard listeners,
or expose the Connector to the LAN. If binding reports an existing listener,
verify that listener using the correct CA before reusing it; do not kill an
unrelated process or change the pairing origin.

In another WSL terminal, verify the exact endpoint with the supplied public CA:

```bash
curl --noproxy '*' --cacert './Codex-LAN-CA.crt' --fail --show-error --max-time 8 https://127.0.0.2:8767/api/health
```

After approval, download the **private Connector installer** from the Windows
browser. Immediately run that download inside WSL (its one-use pairing expires):

```bash
bash '/mnt/c/Users/WINDOWS_USER/Downloads/Install-Codex-Connector-VERSION-private.sh'
```

Replace placeholders with the actual download path/version. If using separate
installer and pairing downloads instead:

```bash
bash '/mnt/c/Users/WINDOWS_USER/Downloads/Install-Codex-Connector-VERSION.sh' --pair-file '/mnt/c/Users/WINDOWS_USER/Downloads/codex-pairing-VERSION.json'
```

Only use the pairing from **your approved browser session**. The installer asks
for consent before enabling local file/command access. Pairing contains the
pinned laptop CA, so the Connector needs no Windows administrator approval or
global Linux certificate import. Keep its token and private installer private.

Check the installed Connector inside WSL:

```bash
systemctl --user status codex-lan-direct.service
```

Keep both tunnel windows open while working. Systemd services alone do not keep
WSL running. Sleeping/shutting down either computer or terminating WSL interrupts
access; saved server conversations remain on the host laptop.

## Official references

- [WSL networking and localhost behavior](https://learn.microsoft.com/en-us/windows/wsl/networking)
- [Systemd requirements and WSL lifecycle](https://learn.microsoft.com/en-us/windows/wsl/systemd)
- [Current User versus Local Machine certificate stores](https://learn.microsoft.com/en-us/powershell/module/pki/import-certificate)
- [OpenSSH on Windows](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_install_firstuse)
