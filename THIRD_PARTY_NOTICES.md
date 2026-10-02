# Third-party distribution notes

No project-wide license has been selected for this repository. This inventory
does not grant rights to the application itself or change the terms of bundled
software. Keep the following upstream notices when copying source or making a
distribution.

| Component | Tracked distribution | Required notice |
| --- | --- | --- |
| KaTeX | Browser math rendering JavaScript, version 0.18.7 | [KaTeX MIT notice](codex_lan/web/KaTeX-LICENSE.txt) |
| Vazir | Unmodified Persian font, version 30.1.0 | [SIL OFL 1.1](codex_lan/web/Vazir-OFL.txt), [font provenance](codex_lan/web/Vazir-FONT.md) |

The application also depends on Python packages identified by its requirements
files. Installers may download PyPA's pip zipapp and package wheels; connector
builds can include a Codex executable. These generated inputs are not tracked
source and are excluded from the reviewed source export. A distributor must
review and retain the licenses and notices of the actual versions they bundle,
including their transitive dependencies. A source-only export is not approval
to redistribute generated executables or installer archives.

See [SECURITY.md](SECURITY.md) and [the publication checklist](docs/PUBLISHING.md)
for privacy and packaging safeguards.
