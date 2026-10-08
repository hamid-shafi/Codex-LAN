# Repository layout

The top level follows the products and workflows people use. Technical modules
live inside those areas, so opening the repository starts with the workspace,
the optional desktop app, installation, documentation, and development.

```text
apps/
  workspace/
    codex_lan/          Host service, workers, and browser interface
    requirements.txt   Workspace Python dependencies
  desktop/             Optional GTK app, assets, and desktop installers
setup/
  host/                New-host installation and HTTPS setup
  connector/           Connector installation and launch helpers
  release-tools/       Bundle building, publication, and guarded activation
  maintenance/         Private history backup and repair helpers
docs/
  product/             Product overview and development story
  architecture/        Component boundaries and historical designs
  operations/          Setup, migration, releases, and source publication
development/
  tests/               Synthetic unit, integration, and browser checks
  tools/               Publication checks and developer utilities
project.py             Source-checkout command runner
setup.sh               Shortcut for the host setup workflow
```

`README.md` introduces the product and installation. `CONTRIBUTING.md`,
`SECURITY.md`, and `THIRD_PARTY_NOTICES.md` stay at the root for contributors,
security review, and distribution notices. Existing filenames inside the folders
retain their familiar names; the folders supply their product and workflow context.

## Commands from the checkout

Run `project.py` with the Python environment containing the workspace dependencies.
It supplies the import paths for the workspace, desktop app, setup helpers, and
development tools. These commands work from the checkout root:

```bash
python3 project.py host --check
bash setup.sh
python3 project.py test
python3 project.py test test_lan_server -q
python3 project.py check
python3 project.py check --history
python3 project.py desktop --check
```

`test` forwards unittest arguments; without arguments it discovers
`development/tests`. `check` forwards publication-check arguments. The other
commands select host installation (`host`), Connector preparation (`connector`),
Connector release publication (`release`), guarded backend activation (`backend`),
browser-file activation (`web`), HTTPS
configuration (`https`), private history backup (`backup`), and the optional GTK
application (`desktop`). Review the relevant
[operations guide](../operations/LAN-RELEASES.md) before activating a release.

For workspace tests, create a local virtual environment and install
`apps/workspace/requirements.txt`. Browser checks also need Playwright and Chromium;
GTK checks need the distribution's GTK/PyGObject packages. Developer utilities
remain directly runnable under `development/tools/` when needed.

## Source folders and installed payloads

The repository layout describes editable source. Builders and installers select
the files needed by each product and preserve the established flat installed
payload: the `codex_lan` package and its setup helpers remain at their expected
installed paths. A Connector archive or trusted setup snapshot therefore need
not reproduce `apps/`, `setup/`, or `development/`. This keeps existing installed
service commands and client update contracts compatible with organized source.

The optional desktop app has its own installation and legacy history. Reorganizing
its source does not migrate desktop conversations into the shared workspace.

## Private state

Installed databases, conversations, uploads, usage records, credentials, pairing
files, TLS keys, local configuration, generated bundles, logs, and backups stay
outside Git. Workspace runtime state is under `~/.local/share/codex-lan/`; the
desktop app's legacy history is under `~/.local/share/hamsohbat-codex/`.
Source folders do not contain an installation snapshot.

Keep the development and reviewed GitHub-ready checkouts synchronized in source
and documentation, while retaining their separate histories. Record project
commits in both repositories and stage only the task's changes. Source
synchronization does not restart an application or authorize a GitHub push.
Follow [the publication checklist](../operations/PUBLISHING.md) in the reviewed
checkout before an explicitly requested publication.
