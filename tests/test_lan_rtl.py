"""Browser RTL data follows Unicode bidi classes, not Persian-specific blocks."""

from pathlib import Path
import re
import unittest
import unicodedata

from tools.generate_rtl_ranges import render_browser_ranges, strong_rtl_ranges


WEB = Path(__file__).resolve().parents[1] / "codex_lan" / "web"


class UnicodeRtlRangeTests(unittest.TestCase):
    def test_generator_merges_only_explicit_strong_classes(self):
        source = """# @missing: 0590..05FF; Right_To_Left
05D2..05D3 ; R # letters
05D0..05D1 ; R # letters
05D4 ; R # letter
0627 ; AL # letter
0660..0669 ; AN # numbers
06F0..06F9 ; EN # numbers
0301 ; NSM # mark
060C ; CS # punctuation
0020 ; WS # space
"""
        self.assertEqual(strong_rtl_ranges(source), [(0x5D0, 0x5D4), (0x627, 0x627)])
        self.assertIn("[0x5d0, 0x5d4]", render_browser_ranges(strong_rtl_ranges(source)))

    def test_generator_rejects_empty_or_invalid_data(self):
        for source in ("# @missing: 0590..05FF; Right_To_Left", "0660..0669 ; AN", "110000 ; R", "05FF..0590 ; R"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                strong_rtl_ranges(source)

    def test_generated_intervals_match_all_known_assigned_unicode_characters(self):
        source = (WEB / "app.js").read_text(encoding="utf-8")
        block = source.split("// BEGIN GENERATED STRONG RTL RANGES", 1)[1].split("// END GENERATED STRONG RTL RANGES", 1)[0]
        ranges = [(int(start, 16), int(end, 16)) for start, end in re.findall(r"\[0x([0-9a-f]+), 0x([0-9a-f]+)\]", block)]
        self.assertTrue(ranges)
        self.assertEqual(ranges, sorted(ranges))
        self.assertTrue(all(previous[1] + 1 < current[0] for previous, current in zip(ranges, ranges[1:])))
        interval = 0
        for point in range(0x110000):
            while interval < len(ranges) and ranges[interval][1] < point:
                interval += 1
            # The OS may expose an older UCD than the pinned browser data.
            if unicodedata.category(chr(point)) == "Cn":
                continue
            covered = interval < len(ranges) and ranges[interval][0] <= point
            expected = unicodedata.bidirectional(chr(point)) in {"R", "AL"}
            if covered != expected:
                self.fail(f"U+{point:04X} has Bidi_Class={unicodedata.bidirectional(chr(point))}, covered={covered}")


if __name__ == "__main__":
    unittest.main()
