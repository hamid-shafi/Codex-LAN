# Security and publication

This repository contains application source and synthetic tests. Account login,
device credentials, pairing files, TLS private keys, conversations, uploads,
SQLite databases, private installers, and diagnostic reports belong to each
installation and must never be added to Git or GitHub. Runtime state is stored
outside the checkout in the user's local application-data directory.

Before making a source repository public, run:

```bash
python3 tools/check_publication.py
python3 tools/check_publication.py --history
```

The first command scans the current tracked source and non-ignored new files.
The second also checks every reachable Git revision, including deleted files,
and reports whether commit identity metadata needs a privacy review. Reports
contain categories, paths, and line numbers, never matched credential values.
Synthetic secrets are allowed only by an exact value hash tied to a particular
test file. A new credential in that same test file still fails the check.

The scanner is a safeguard, not proof that an arbitrary file contains no
private information. Review screenshots, sample content, generated bundles,
issue attachments, and release archives separately. Automated GitHub secret
scanning complements this local check.

If local history contains personal paths, machine addresses, author email, or
private files, keep that original checkout private and export reviewed source:

```bash
python3 tools/check_publication.py --export ../codex-lan-public
```

The export creates a new directory without `.git`, ignored runtime files, or
local reports. It does not rewrite or delete the original history. Initialize
fresh history inside that directory after choosing the intended public author
identity. Never push the original history because a later deletion does not
remove earlier copies of a secret. If an actual credential was exposed, revoke
or rotate it before publication; deleting the file is not sufficient.

## Runtime trust

Only expose this service to a trusted local network. Clients should use HTTPS,
verify the server certificate fingerprint, and receive explicit administrator
approval. Pairing files and private installers identify one device and must not
be shared between users. Provider sign-in belongs on the host, not in browser
JavaScript or a public sample configuration.

Full-access turns can read files and execute commands as the authorized Linux
user. Read-only is the appropriate setting for inspection. This application is
not an isolation boundary for mutually untrusted users or for a public Internet
deployment. Do not publish an account shared with unknown parties.

## Reporting a vulnerability

Use the repository maintainer's private security-reporting channel when one is
configured on GitHub. Until then, contact the maintainer privately. Do not put
credentials, private conversation excerpts, or exploit details into a public
issue. Include affected versions and a minimal synthetic reproduction.

## Licensing

No project-wide license has been selected yet. Public visibility alone does not
grant redistribution rights. Choose a project license before advertising this
as reusable open-source software. The bundled KaTeX JavaScript is MIT-licensed
(`codex_lan/web/KaTeX-LICENSE.txt`); the unmodified Vazir font uses SIL OFL 1.1
(`codex_lan/web/Vazir-OFL.txt`, provenance in `Vazir-FONT.md`). Keep these notices
in distributions. Codex executables and Python dependencies are build inputs,
not tracked source; review their own terms when distributing connector bundles.
