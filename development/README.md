# Develop and verify

`tests/` holds synthetic unit, integration, and browser checks. `tools/` holds
publication safeguards and developer utilities, not installation data.

Run workflows from the checkout with the same Python environment used for the
workspace:

```sh
python3 project.py test test_project_layout test_publication_tools -q
python3 project.py test test_lan_server test_lan_store -q
python3 project.py check
```

`python3 project.py test` discovers the full suite. Browser checks need their
browser dependencies; desktop checks additionally need GTK and a display or
headless display. See [contributing](../.github/CONTRIBUTING.md) and the
[repository guide](../docs/architecture/REPOSITORY.md).

The runner sets explicit import paths for the organized sources and child
processes. It works from any working directory and does not modify installed
services or Python environments.
