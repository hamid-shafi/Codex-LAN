# Contributing

Use a local Python virtual environment and synthetic test data. Never commit a
copy of an installation, local account sign-in, a pairing file, a private
connector installer, conversations, uploaded files, or a runtime database.
The installation and network setup are documented in [README.md](README.md).

Before submitting changes:

```bash
python3 project.py check
git diff --check
```

Run tests relevant to the changed feature. Server tests use `aiohttp`; browser
tests also need Playwright and Chromium. Keep tests offline where practical,
using temporary directories and fake account/executor fixtures. Desktop GTK
tests require the distribution's GTK/PyGObject packages. Do not use a real
provider login or live user conversations as fixtures.

The [repository guide](docs/architecture/REPOSITORY.md) explains the product-first
source layout. Use `python3 project.py test` for full unittest discovery, or
`python3 project.py test test_lan_server -q` for a specific test module. The runner
sets the source import paths for both apps, setup commands, and development tools.

Backend updates must wait for active prompts to finish. Use the documented
deferred deployment tool instead of restarting a service that owns live work.
Use a new release version when changing a published connector. Never replace a
versioned installer with different executable contents.

Before initial GitHub publication, follow [SECURITY.md](SECURITY.md) and review
all reachable history, not only the latest files. There is currently no
project-wide license; contributions and redistribution terms should be agreed
with the maintainer before accepting external contributions.
