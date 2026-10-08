"""CODEX: Prepare private-LAN HTTPS from an explicit address without restarting services."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

for _source_parent in Path(__file__).resolve().parents:
    if (_source_parent / "project_layout.py").is_file():
        sys.path.insert(0, str(_source_parent))
        break
from project_layout import configure_imports

configure_imports(__file__)

from codex_lan.https_setup import ca_fingerprint, prepare_network
from codex_lan.network import load_network


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare LAN HTTPS; services are not started or restarted")
    parser.add_argument("--data-dir", type=Path, default=Path.home() / ".local/share/codex-lan/server")
    parser.add_argument("--address", help="Private IPv4 address of this host on the shared LAN")
    parser.add_argument("--port", type=int, help="HTTPS port (defaults to 8767 for a new setup)")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        existing = load_network(args.data_dir)
        address = args.address or (existing or {}).get("lan_address")
        if not address:
            parser.error("Choose this host's private IPv4 address with --address; no network was changed")
        port = args.port if args.port is not None else (existing or {}).get("port", 8767)
        config = prepare_network(args.data_dir, address, port)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print("HTTPS configuration prepared:", config["public_origin"])
    print("Public CA certificate:", config["ca"])
    print("CA SHA256 fingerprint:", ca_fingerprint(config["ca"]))
    print("Verify and trust this CA on each client before connecting. Services were not restarted.")


if __name__ == "__main__":
    main()
