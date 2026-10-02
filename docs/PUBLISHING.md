# Preparing a personal source repository

The source repository and the private installation are different things. Only
reviewed code, synthetic tests, documentation, and third-party notices belong in
GitHub. Do not upload current users, conversations, usage reports, account login,
pairing files, private connector installers, TLS keys, or local backups.

No project license has been selected. Preparing a personal repository does not
change that choice. Preserve the bundled notices in
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).

## Review and export

Run these checks from the source checkout:

```bash
python3 tools/check_publication.py
python3 tools/check_publication.py --history
python3 -m unittest discover -s tests -p test_publication_tools.py -v
git diff --check
```

The history check reviews reachable revisions, including deleted files and
commit messages. It separately flags author identity metadata for review. It
never prints matched credential values. An automated scan does not replace a
human review of screenshots, text fixtures, or release archives.

If old local history contains personal information, do not push that history.
Make a clean export into a new directory:

```bash
python3 tools/check_publication.py --export ../codex-lan-personal-source
cd ../codex-lan-personal-source
git init -b main
git config user.name 'Your chosen author name'
git config user.email 'YOUR_GITHUB_NOREPLY_ADDRESS'
git add .
git diff --cached --stat
git commit -m 'Initial reviewed source'
python3 tools/check_publication.py --history --allow-identity-metadata
```

Choose a GitHub no-reply address in your GitHub account settings if you do not
want to publish your personal email. `--allow-identity-metadata` acknowledges
that author metadata is intentional; it does not bypass content checks.

The export includes reviewed working-tree changes and non-ignored new source
files. It refuses flagged files and an existing destination, excludes the
original `.git`, and leaves the original checkout and its history untouched.
Connect the new repository to GitHub only after reviewing its staged contents.
No helper automatically uploads or publishes anything.

## Private local reports and migration

To keep a small aggregate installation snapshot locally:

```bash
python3 tools/private_snapshot.py --html
```

The report contains aggregate counts and reported token totals, not names,
device or room IDs, conversation contents, credentials, or database paths.
Its directory is private (`0700`) and files are private (`0600`). Reports live
under the local application-data directory, outside this checkout. A screenshot
of that report must stay private too.

Private history migration is a separate opt-in operation using
`backup_lan_history.py`. Do not add its archive or any restored database to Git,
release assets, public issue attachments, or the clean source export.

## Installation-specific provider networking

New installations use direct provider access by default. If your host requires
a SOCKS5 route, configure `CODEX_LAN_PROXY_URL` in its private environment or a
local systemd drop-in, for example `socks5h://proxy.example:1080`. Never commit a
real proxy address or embedded proxy credentials. Existing recorded route
preferences continue to work when their proxy is explicitly configured; an
unconfigured proxy is never selected as a fallback.

Before migrating an existing installation that relied on a source-code proxy
default, preserve that endpoint in private configuration. Do not restart active
prompts merely to prepare the repository.
