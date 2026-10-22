# Set up and maintain a workspace

| Area | Purpose |
| --- | --- |
| [Start here](../docs/getting-started/README.md) | Choose host, teammate, or relay without learning the file layout |
| [Windows](windows/start.cmd) | Double-click entry point, local guide, and read-only prerequisite inventory |
| [Bootstrap](bootstrap/) | Python-independent Linux prerequisite and official Codex helpers |
| [Host](host/) | First-run installation, HTTPS, and the browser setup screen |
| [Connector](connector/) | Teammates' native clients and pairing installers |
| [Release tools](release-tools/) | Build and publish verified Connector releases |
| [Maintenance](maintenance/) | Guarded updates, backups, and targeted repairs |

From an extracted checkout, run `bash setup.sh` on Linux or double-click
`setup/windows/start.cmd` on Windows. The Linux menu separates hosting from joining
a team and includes optional, explicitly confirmed prerequisite/Codex helpers.
See the [setup guide](../docs/operations/SETUP.md) for advanced host options.

The helpers are included in source, not preinstalled Python/WSL images. First-time
package downloads need Internet access; [offline preparation](../docs/getting-started/DEPENDENCIES.md)
has explicit boundaries. Installation-specific certificates, keys, and pairings
must be obtained from your administrator, never from a GitHub source checkout.

Do not reinstall a running host during prompts. Use the browser's Host setup
screen or the [guarded update workflow](../docs/operations/LAN-RELEASES.md).
Runtime databases, credentials, pairing files, and backups never belong here.
