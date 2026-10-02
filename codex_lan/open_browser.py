"""CODEX: Open a local/tunneled browser session without putting secrets in logs."""

import argparse
import urllib.parse
import webbrowser
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="http://127.0.0.1:8765")
    parser.add_argument("--token-file", type=Path, required=True)
    args = parser.parse_args()
    parsed = urllib.parse.urlsplit(args.server)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        parser.error("Use the local SSH tunnel URL")
    token = args.token_file.read_text().strip()
    webbrowser.open(args.server.rstrip("/") + "/#login=" + urllib.parse.quote(token, safe=""))


if __name__ == "__main__":
    main()
