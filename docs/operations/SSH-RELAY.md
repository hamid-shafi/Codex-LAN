# Connect through an SSH relay

The laptop hosts the app, Codex sign-in, and conversations. The SSH server
forwards encrypted HTTPS traffic only; it stores no application database or
TLS private keys. Each teammate still needs app approval and a private
Connector pairing. Windows browser/WSL users: follow the
[Windows guide](WINDOWS-WSL.md).

The client address is always `https://127.0.0.2:8767` so browser identity
stays separate from local administration.

## Host and relay administrator

First activate a release that supports the reserved loopback address using
the [guarded updater](LAN-RELEASES.md). **Then** configure
`127.0.0.2:8767` in Host setup; do not write this HTTPS setting while an
older backend is active. Keep the laptop CA/server private keys on the laptop.
Its persistent publisher SSH connection forwards:

```text
-R 127.0.0.1:18767:127.0.0.2:8767
```

Provision individual SSH identities and restrict forwarding:

| Account | Allowed direction | Endpoint |
| --- | --- | --- |
| Laptop publisher | `AllowTcpForwarding remote` | `PermitListen 127.0.0.1:18767` |
| Client | `AllowTcpForwarding local` | `PermitOpen 127.0.0.1:18767` |

Prefer dedicated accounts over shared root. Disable shell/subsystem sessions
(`MaxSessions 0`), agent/X11/Unix-socket/device forwarding, and public
forwarding (`GatewayPorts no`). See the
[OpenSSH server manual](https://man.openbsd.org/sshd_config).
Keep the laptop awake, online, and signed in; after-logout service needs
user lingering. Screen locking is fine, but lid/firmware sleep rules may
override the app's inhibitor.

## Linux teammate

Obtain the assigned SSH login, relay host-key fingerprint, and laptop public
CA certificate/fingerprint through a trusted channel. Verify **both** keys.
From the source checkout:

```bash
bash setup.sh relay YOUR_USER@YOUR_RELAY
```

Or use `setup/relay/connect_ssh_relay.sh` from a prepared client bundle.
An authorized private SSH identity can be passed with
`--identity /path/to/private-key`. The launcher does not create accounts
or copy keys. Keep the terminal open; a successful tunnel is normally silent,
while `Permission denied` means authentication failed.

Open `https://127.0.0.2:8767` and trust only the **verified public CA**.
You can inspect a PEM certificate with:

```bash
openssl x509 -in /path/to/ca.pem -noout -fingerprint -sha256
```

Never bypass TLS warnings or distribute CA/server private keys, admin tokens,
or anyone else's pairing file. Request app access, have the host approve your
verification code, then download your own private Connector installer.
SSH authorization and app approval are separate. Local file/command access
runs under your own OS user.

## If it does not connect

An open SSH session alone does not prove the laptop is online. Check the
laptop's HTTPS listener and publisher tunnel, then verify the CA before
signing in. If `127.0.0.2:8767` is already bound, inspect the existing
listener; do not kill an unrelated process or change the URL without
updating the certificate and app origin. `Ctrl+C` closes your tunnel.
