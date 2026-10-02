#!/usr/bin/env python3
"""CODEX: Save aggregate installation diagnostics locally without exporting user content."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import sqlite3
import time


STATUSES = ("queued", "running", "completed", "failed", "cancelled", "interrupted")
DEVICE_STATUSES = ("pending", "approved", "revoked", "rejected")


def collect(database: Path) -> dict:
    deadline = time.monotonic() + 5
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=2000")
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        connection.execute("BEGIN")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}

        def count(table: str) -> int:
            return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] if table in tables else 0

        counts = {table: count(table) for table in ("devices", "rooms", "runs", "messages", "attachments")}
        usage = {"input_tokens": 0, "cached_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        if "usage_ledger" in tables:
            names = tuple(usage)
            values = connection.execute(
                "SELECT " + ",".join(f"coalesce(sum({name}),0)" for name in names) + " FROM usage_ledger"
            ).fetchone()
            usage = dict(zip(names, values))
        status_counts = {
            status: connection.execute("SELECT count(*) FROM runs WHERE status=?", (status,)).fetchone()[0]
            if "runs" in tables else 0 for status in STATUSES
        }
        device_counts = {
            status: connection.execute("SELECT count(*) FROM devices WHERE status=?", (status,)).fetchone()[0]
            if "devices" in tables else 0 for status in DEVICE_STATUSES
        }
        attachment_bytes = connection.execute(
            "SELECT coalesce(sum(size),0) FROM attachments"
        ).fetchone()[0] if "attachments" in tables else 0
        weighted_units = connection.execute(
            "SELECT coalesce(sum(credits_micros),0) FROM quota_usage"
        ).fetchone()[0] / 1000000 if "quota_usage" in tables else 0
        return {
            "schema_version": 1,
            "captured_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "scope": "Lifetime aggregate of currently retained records; not provider billing",
            "counts": counts, "run_statuses": status_counts, "device_statuses": device_counts,
            "reported_usage": usage, "weighted_usage_units": round(weighted_units, 6),
            "attachment_bytes": attachment_bytes,
            "privacy": "Local aggregate only: no names, IDs, messages, credentials, or filesystem paths",
        }
    finally:
        connection.close()


def markdown(report: dict) -> str:
    lines = [
        "# Private installation snapshot", "", report["captured_at_utc"], "",
        report["scope"], "", "| Retained records | Count |", "| --- | ---: |",
    ]
    lines.extend(f"| {name} | {value:,} |" for name, value in report["counts"].items())
    lines.extend(["", "| Run status | Count |", "| --- | ---: |"])
    lines.extend(f"| {name} | {value:,} |" for name, value in report["run_statuses"].items())
    lines.extend(["", "| Reported usage | Tokens |", "| --- | ---: |"])
    lines.extend(f"| {name} | {value:,} |" for name, value in report["reported_usage"].items())
    lines.extend([
        "", f"Weighted usage units: {report['weighted_usage_units']:,.6f}",
        f"Attachment bytes: {report['attachment_bytes']:,}", "", report["privacy"], "",
    ])
    return "\n".join(lines)


def html_report(report: dict) -> str:
    cards = "".join(
        f"<article><span>{html.escape(name.title())}</span><strong>{value:,}</strong></article>"
        for name, value in report["counts"].items()
    )
    usage_rows = "".join(
        f"<tr><td>{html.escape(name.replace('_', ' ').title())}</td><td>{value:,}</td></tr>"
        for name, value in report["reported_usage"].items()
    )
    statuses = "".join(
        f"<tr><td>{html.escape(name.title())}</td><td>{value:,}</td></tr>"
        for name, value in report["run_statuses"].items()
    )
    return (
        "<!doctype html><html lang='en'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Private installation snapshot</title><style>"
        "*{box-sizing:border-box}body{margin:0;background:#080d19;color:#e8efff;"
        "font:16px system-ui,sans-serif;padding:48px}main{max-width:1120px;margin:auto}"
        "h1{font-size:32px;margin:8px 0}p{color:#aebedb;line-height:1.5}"
        ".label{color:#67d5f7;text-transform:uppercase;letter-spacing:.14em;font-size:12px}"
        ".cards{display:grid;grid-template-columns:repeat(5,1fr);gap:16px;margin:30px 0}"
        "article,.panel{padding:22px;border:1px solid #334b6b;border-radius:16px;"
        "background:linear-gradient(135deg,#172039,#102b38)}article span{color:#aebedb}"
        "article strong{display:block;font-size:32px;margin-top:8px}.panels{display:grid;"
        "grid-template-columns:1fr 1fr;gap:20px}h2{font-size:18px}table{width:100%;"
        "border-collapse:collapse}td{padding:12px 0;border-top:1px solid #334b6b}"
        "td+td{text-align:right;color:#8de4ff}footer{margin-top:24px;font-size:13px;color:#aebedb}"
        "@media(max-width:700px){body{padding:20px}.cards{grid-template-columns:1fr 1fr}"
        ".panels{grid-template-columns:1fr}}</style><main>"
        "<span class='label'>Local report · private</span><h1>Installation snapshot</h1>"
        f"<p>{html.escape(report['captured_at_utc'])}<br>{html.escape(report['scope'])}</p>"
        f"<section class='cards'>{cards}</section><section class='panels'>"
        f"<div class='panel'><h2>Reported token usage</h2><table>{usage_rows}</table></div>"
        f"<div class='panel'><h2>Run outcomes</h2><table>{statuses}</table></div></section>"
        f"<footer>{html.escape(report['privacy'])}</footer></main></html>\n"
    )


def save(report: dict, directory: Path, include_html: bool = False) -> list[Path]:
    directory = directory.absolute()
    repository = Path(__file__).resolve().parents[1]
    resolved = directory.resolve()
    if resolved == repository or repository in resolved.parents:
        raise ValueError("Reports must be stored outside the project checkout")
    if directory.is_symlink():
        raise ValueError("Report directory must not be a symbolic link")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.stat().st_uid != os.getuid():
        raise ValueError("Report directory must belong to the current Linux user")
    directory.chmod(0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    formats = {"json": json.dumps(report, indent=2) + "\n", "md": markdown(report)}
    if include_html:
        formats["html"] = html_report(report)
    paths = []
    for suffix, content in formats.items():
        path = directory / f"installation-{stamp}.{suffix}"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
        paths.append(path)
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path.home() / ".local/share/codex-lan/server/server.sqlite3")
    parser.add_argument("--output", type=Path, default=Path.home() / ".local/share/codex-lan/private-reports")
    parser.add_argument("--html", action="store_true", help="Also save a static HTML page for a local screenshot")
    args = parser.parse_args()
    try:
        report = collect(args.database)
        for path in save(report, args.output, args.html):
            print(path)
        return 0
    except (OSError, ValueError, sqlite3.Error):
        parser.exit(1, "Could not create a private aggregate snapshot; no database content was disclosed.\n")


if __name__ == "__main__":
    raise SystemExit(main())
