# Access a laptop host through an SSH relay

The laptop runs the application, stores its state, and owns model sessions. A
separate SSH server carries encrypted HTTPS traffic between clients and the
laptop. The relay does not need the application, its database, or TLS private
keys. Clients still need application approval and their own Connector pairing.

For a Windows browser with a WSL 2 Connector, follow the dedicated
[Windows and WSL guide](WINDOWS-WSL.md). Windows and WSL use separate tunnel
launchers; no global certificate-policy or WSL network changes are needed.

The reserved browser address is `https://127.0.0.2:8767`. Its separate loopback IP
keeps browser cookies apart from local administration at `127.0.0.1:8765`.
Use this exact HTTPS address on clients; the certificate must cover `127.0.0.2`.

## Host and relay prerequisites

The host administrator must first install a release supporting the reserved
loopback HTTPS address. On a running host, use the [guarded backend
updater](LAN-RELEASES.md), which waits for queued and running prompts to finish.
Only after activation succeeds, configure `127.0.0.2`, port `8767`, in Host setup.
Do not write the new HTTPS configuration while the previous backend is active.
The setup action generates the laptop's certificate and starts HTTPS; keep its
CA key and server key private on the laptop.

The laptop's persistent SSH connection uses this remote forwarding destination:

```text
-R 127.0.0.1:18767:127.0.0.2:8767
```

The relay administrator supplies authorized SSH accounts and verifies these
restrictions. This guide and the client script do not create accounts or keys.

| Account | Forwarding allowed | Exact endpoint |
| --- | --- | --- |
| Laptop publisher | `AllowTcpForwarding remote` | `PermitListen 127.0.0.1:18767` |
| Client | `AllowTcpForwarding local` | `PermitOpen 127.0.0.1:18767` |

Use dedicated restricted accounts and separate client keys, rather than sharing
root access. Disable shell/subsystem sessions with `MaxSessions 0` and disable
agent, X11, Unix-socket, and device forwarding. Keep `GatewayPorts no`; the relay
port must bind only to loopback. Do not open public application ports or add
router forwarding. The public SSH port is the entry point. These restrictions
are described in the [OpenSSH server manual](https://man.openbsd.org/sshd_config).

Keep the laptop awake, connected to the Internet and power, and signed in.
Without user lingering, availability depends on its login session; enabling
user services alone does not promise operation before login or after logout.
Screen locking is fine. Check the laptop's lid policy separately: a sleep
inhibitor does not override every lid or firmware setting.

## Connect a client

Obtain the relay login, its SSH host-key fingerprint, and the laptop's public CA
certificate and SHA-256 fingerprint from the administrator through a trusted
channel. The SSH fingerprint and the TLS CA fingerprint identify different keys;
verify both. The script uses your existing SSH identity and normal SSH settings.
It does not bundle credentials, insert authorized keys, or require sudo.

From the source checkout, run:

```bash
bash setup.sh relay YOUR_USER@YOUR_RELAY
```

Replace the placeholder with the account and relay address supplied to you. An
SSH host alias from your own SSH configuration also works after `@`. When SSH
asks about an unknown host key, compare its fingerprint before accepting it.
A changed key must be investigated with the administrator.

The underlying `setup/connector/connect_ssh_relay.sh` remains directly usable
in a separately prepared client bundle. Neither launcher provisions relay
accounts or the laptop publisher tunnel.

Keep the terminal open and manually open `https://127.0.0.2:8767`. Verify the CA
certificate's SHA-256 fingerprint, then import that public CA certificate into
the browser's trust settings. For a PEM certificate, its fingerprint is:

```bash
openssl x509 -in /path/to/ca.pem -noout -fingerprint -sha256
```

Do not bypass certificate errors or import an unverified certificate. Never
copy `ca-key.pem`, `server-key.pem`, an administrator token, or another person's
pairing file to a client.

After successful authentication the tunnel stays silent; it does not open a
remote shell. `Permission denied` means authentication failed, not a silent
working tunnel. The launcher permits only one password prompt per attempt.
For an authorized key, append `--identity /path/to/private-key` to the command;
never distribute that private key with the bundle.

Enroll or sign in, then have the host administrator approve your device's
verification code. Download the Connector installer and your own one-use pairing
file from the app, and follow the installer instructions as your desktop user.
Connector access to local files and commands runs under that client's user;
review its access prompt. Keep the SSH tunnel running for browser and Connector
access. Closing it or pressing Ctrl+C disconnects both.

## Connection checks

If SSH reports that `127.0.0.2:8767` is already in use, check for an existing
tunnel or local installation. Do not change the browser host or port casually;
the configured origin and certificate must stay consistent.

An open SSH connection proves SSH connectivity, not laptop availability. If the
page cannot load, check that the laptop is awake, its HTTPS listener is ready,
and its publisher tunnel is connected. Certificate verification must succeed
before signing in. Application approval is separate from SSH authorization.
