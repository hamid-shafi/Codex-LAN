# Set up and maintain a workspace

| Area | Purpose |
| --- | --- |
| [Host](host/) | First-run installation, HTTPS, and the browser setup screen |
| [Connector](connector/) | Teammates' native clients and pairing installers |
| [Release tools](release-tools/) | Build and publish verified Connector releases |
| [Maintenance](maintenance/) | Guarded updates, backups, and targeted repairs |

For a new host, run `python3 project.py host --check` from the checkout, then
follow the [setup guide](../docs/operations/SETUP.md). The root `setup.sh` remains
the simple first-run entry point.

Do not reinstall a running host during prompts. Use the browser's Host setup
screen or the [guarded update workflow](../docs/operations/LAN-RELEASES.md).
Runtime databases, credentials, pairing files, and backups never belong here.
