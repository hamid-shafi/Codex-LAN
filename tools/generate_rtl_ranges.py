#!/usr/bin/env python3
"""Generate the browser's assigned strong-RTL intervals from a local Unicode UCD file.

Download DerivedBidiClass.txt for the intended Unicode version separately, then
run this script with its path and review the emitted block before updating app.js.
No network request or project-file modification is performed by this tool.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re


STRONG_RTL_LINE = re.compile(r"^([0-9A-F]+)(?:\.\.([0-9A-F]+))?\s*;\s*(R|AL)\b")


def strong_rtl_ranges(source: str) -> list[tuple[int, int]]:
    """Use explicit assigned R/AL entries, not defaults for unassigned characters."""
    ranges = []
    for line in source.splitlines():
        match = STRONG_RTL_LINE.match(line)
        if not match:
            continue
        start, end = int(match[1], 16), int(match[2] or match[1], 16)
        if not 0 <= start <= end <= 0x10FFFF:
            raise ValueError("Invalid Unicode interval")
        ranges.append((start, end))
    if not ranges:
        raise ValueError("No explicit strong-RTL entries in the UCD file")
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def render_browser_ranges(ranges: list[tuple[int, int]]) -> str:
    lines = ["// BEGIN GENERATED STRONG RTL RANGES", "const strongRtlRanges = ["]
    for offset in range(0, len(ranges), 6):
        lines.append("  " + ", ".join(
            f"[0x{start:x}, 0x{end:x}]" for start, end in ranges[offset:offset + 6]
        ) + ",")
    lines += ["];", "// END GENERATED STRONG RTL RANGES"]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ucd_file", type=Path, help="Local DerivedBidiClass.txt")
    args = parser.parse_args()
    print(render_browser_ranges(strong_rtl_ranges(args.ucd_file.read_text(encoding="utf-8"))))


if __name__ == "__main__":
    main()
