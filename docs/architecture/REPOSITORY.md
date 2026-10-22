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
  bootstrap/           Guided Linux start and prerequisite helpers
  windows/             Native Windows getting-started launcher
  host/                New-host installation and HTTPS setup
  connector/           Connector installation and launch helpers
  relay/               SSH tunnel and CA-trust helpers
  release-tools/       Bundle building, publication, and guarded activation
  maintenance/         Private history backup and repair helpers
docs/
  getting-started/      Role-based guide and offline HTML start page
  product/             Product overview and development story
  architecture/        Source map and desktop component boundaries
  operations/          Setup, migration, releases, and source publication
  legal/               Third-party notices
development/
  project.py           Source-checkout command runner
  tests/               Synthetic unit, integration, and browser checks
  tools/               Publication checks and developer utilities
setup.sh               Guided Linux start (host, team, relay, prerequisites)
```

`README.md` is the short product and setup entry point. Contributor and security
policies live in `.github/`, where GitHub recognizes them; third-party notices
live in `docs/legal/`. `project_layout.py` remains at the root because installers
use it as a source/payload boundary, preserving existing release contracts.
Existing filenames inside the folders
retain their familiar names; the folders supply their product and workflow context.

## Commands from the checkout

Run `development/project.py` with the Python environment containing the workspace dependencies.
It supplies the import paths for the workspace, desktop app, setup helpers, and
development tools. These commands work from the checkout root:

```bash
python3 development/project.py host --check
bash setup.sh
python3 development/project.py test
python3 development/project.py test test_lan_server -q
python3 development/project.py check
python3 development/project.py check --history
python3 development/project.py desktop --check
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

Use a reviewed Git checkout as the source of truth; keep any working source copies
synchronized without copying runtime data. Stage only reviewed task changes.
Source synchronization and Git publication do not restart an installed service.
Follow [the publication checklist](../operations/PUBLISHING.md) before publication;
deployment remains a separate, guarded operation.
