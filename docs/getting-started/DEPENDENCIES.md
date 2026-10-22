# Dependencies, without guesswork

The repository includes setup helpers and browser assets. It deliberately does
not carry operating-system installers, personal credentials, or generated binary
bundles in Git. An initial Internet connection is still needed for downloads and
Codex sign-in; model inference is not offline.

| Where | Required | How it is handled |
| --- | --- | --- |
| Linux host | Python 3.10+, `venv`/`ensurepip`, systemd user session, OpenSSL, official standalone Codex | `bash setup.sh prerequisites` checks OS tools and offers Debian/Ubuntu packages; `bash setup.sh codex` handles Codex separately |
| Host Python environment | Packages from `apps/workspace/requirements.txt` | Installer uses a private venv; optional complete wheelhouse avoids PyPI during install |
| Linux Connector | x86_64, standard CPython 3.10–3.14, systemd user session | Host-built installer contains Codex binaries, pip zipapp, and matching dependency wheels; **Python itself is not bundled** |
| Windows browser | Browser; OpenSSH Client when using relay | Native Windows; no Python or WSL needed to request access |
| Windows local tools | WSL2 Ubuntu x86_64 and the Linux Connector prerequisites | Guided checks and Microsoft's WSL setup; not a native Windows Connector |
| SSH relay | Administrator-provisioned SSH accounts and forwarding | No Codex or app database installed on the relay |

## Python and OS packages

The Bash helper works even when Python is absent:

```bash
bash setup.sh prerequisites --check
bash setup.sh prerequisites
```

The check changes nothing. The install path shows its package-manager command
and requires confirmation before requesting local OS permission. It does not
change the system's default Python, run the app as root, or install into system
Python with pip. Unsupported distributions receive a list of prerequisites.
For a Connector, do not use the host's Codex login/setup steps.

## Official Codex on the host

Use the included helper or [OpenAI's official CLI installation guide](https://learn.chatgpt.com/docs/codex/cli).
The helper downloads the official standalone installer over HTTPS only after
confirmation, then asks before executing it as your own user. Existing Codex is
not silently replaced. Review downloaded code before executing it if required by
your organization's policy. This is an upstream download, not a vendored binary
or a pinned, fully offline supply-chain guarantee.

Connector preparation needs the standalone `codex` executable and its adjacent
`codex-code-mode-host` companion; an npm shim alone is not a complete bundle.
After installation, sign in locally:

```bash
codex -c 'cli_auth_credentials_store="file"' login
```

The host integration currently reads file-backed authentication. Treat that
local file like a password. Do not move it into the checkout or share it.
The host reads `~/.codex/auth.json`; if `CODEX_HOME` points elsewhere, the
guided login stops and shows how to sign in with the default Codex home.
For headless sign-in, see [OpenAI's authentication guide](https://learn.chatgpt.com/docs/auth).
Native Windows Codex support does **not** imply this project's Connector supports
native Windows; the Connector remains a Linux/WSL2 component.

## Prepare for a restricted network

On a connected **matching OS, CPU architecture, and Python version**, prepare a
host wheelhouse outside the repository:

```bash
python3 -m pip download --only-binary=:all: -r apps/workspace/requirements.txt --dest /path/to/wheels
python3 development/project.py host --wheels /path/to/wheels
```

The second command installs without starting services. Add `--start --open` when
ready. Offline host installation still requires Python, venv, Codex, and OS
prerequisites to have been installed separately. Use your distribution's trusted
offline package workflow for those; do not download random Python executables.

The host's **Prepare Connector** action packages its own offline client runtime
dependencies. `python3 development/project.py connector --offline` reuses a complete cache
from a previous preparation. Neither path removes the need for networking to
reach the host, SSH relay, or model provider during normal use.

Keep generated wheels/installers and all private pairing material out of Git.
See [distribution notices](../legal/THIRD_PARTY_NOTICES.md) before redistributing
third-party binaries. A clean source checkout and an installation bundle are
different artifacts.
