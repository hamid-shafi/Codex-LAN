#!/usr/bin/env python3
"""Run product workflows from the source checkout, from any working directory."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

from project_layout import configure_imports, source_file


COMMANDS = {
    "check": "development/tools/check_publication.py",
    "host": "install_lan_server.py",
    "https": "setup_lan_https.py",
    "connector": "prepare_lan_connector.py",
    "release": "publish_lan_release.py",
    "backend": "deploy_lan_backend.py",
    "web": "deploy_lan_web.py",
    "backup": "backup_lan_history.py",
}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=(*COMMANDS, "test", "desktop"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER,
                        help="Arguments passed to the chosen workflow")
    args = parser.parse_args(argv)
    root = configure_imports(__file__)
    environment = os.environ.copy()
    # Child processes need the same explicit import roots; no installed Python
    # environment or running service is changed by source navigation.
    environment["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(
        [str(root), *(path for path in sys.path if path and Path(path).is_relative_to(root)),
         *filter(None, environment.get("PYTHONPATH", "").split(os.pathsep))]
    ))
    if args.command == "test":
        test_args = args.arguments
        option_values = {"-k"}
        has_target = False
        skip = False
        for token in test_args:
            if skip:
                skip = False
            elif token in option_values:
                skip = True
            elif not token.startswith("-"):
                has_target = True
        if not has_target:
            test_args = ["discover", "-s", "development/tests", *test_args]
        command = [sys.executable, "-m", "unittest", *test_args]
    elif args.command == "desktop":
        if args.arguments == ["--check"]:
            for path in sorted((root / "apps/desktop").glob("*.py")):
                compile(path.read_text(encoding="utf-8"), str(path), "exec")
            print("Desktop source syntax passed; no application or services were started.")
            return 0
        command = ["bash", str(source_file(root, "run.sh")), *args.arguments]
    else:
        command = [sys.executable, str(source_file(root, COMMANDS[args.command])), *args.arguments]
    return subprocess.call(command, cwd=root, env=environment)


if __name__ == "__main__":
    raise SystemExit(main())
