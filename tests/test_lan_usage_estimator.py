"""CODEX: Offline tests for precise, conservative published-credit usage estimates."""

from decimal import Decimal, ROUND_CEILING, localcontext
import unittest

from codex_lan.usage_estimator import (
    MAX_TOKEN_COUNT, RATE_VERSION, SOURCE, canonical_model, estimate_usage, supports_fast,
)


class UsageEstimatorTests(unittest.TestCase):
    def test_published_standard_rates(self):
        rates = {
            "gpt-6-astra": (250, 25, 1250),
            "gpt-6.1-sol": (50, Decimal("2.5"), 250),
            "gpt-6-sol": (50, 5, 250),
            "gpt-6-luna": (Decimal("2.5"), Decimal("0.25"), Decimal("12.5")),
            "gpt-5.6-sol": (100, 10, 500),
            "gpt-5.6-terra": (50, 5, 300),
            "gpt-5.6-luna": (5, Decimal("0.5"), 30),
            "gpt-5.5": (125, Decimal("12.5"), 750),
            "gpt-5.4": (Decimal("62.5"), Decimal("6.25"), 375),
            "gpt-5.4-mini": (Decimal("18.75"), Decimal("1.875"), 113),
        }
        for model, (input_rate, cache_rate, output_rate) in rates.items():
            with self.subTest(model=model):
                self.assertEqual(estimate_usage(model, 1_000_000, 0, 0)["credits_micros"],
                                 int(input_rate * 1_000_000))
                self.assertEqual(estimate_usage(model, 1_000_000, 0, 1_000_000)["credits_micros"],
                                 int(cache_rate * 1_000_000))
                self.assertEqual(estimate_usage(model, 0, 1_000_000, 0)["credits_micros"],
                                 int(output_rate * 1_000_000))

    def test_cached_input_is_a_subset_not_extra_usage(self):
        result = estimate_usage("gpt-5.6-sol", 1000, 50, 750)
        self.assertEqual(result["credits_micros"], 57_500)
        self.assertEqual(Decimal(result["credits_micros_exact"]), Decimal(57_500))

    def test_no_additional_reasoning_effort_multiplier(self):
        result = estimate_usage("gpt-6-astra", 0, 1000, 0)
        self.assertEqual(result["credits_micros"], 1_250_000)
        self.assertNotIn("effort_multiplier", result)

    def test_fast_multipliers(self):
        for model, multiplier in (("gpt-6-astra", "2.5"), ("gpt-5.6-sol", "2.5"),
                                  ("gpt-6.1-sol", "2"), ("gpt-6-sol", "2"), ("gpt-6-luna", "2"),
                                  ("gpt-5.6-terra", "2.5"), ("gpt-5.6-luna", "2.5"),
                                  ("gpt-5.5", "2.5"), ("gpt-5.4", "2")):
            with self.subTest(model=model):
                standard = estimate_usage(model, 1000, 1000, 400)
                fast = estimate_usage(model, 1000, 1000, 400, speed="fast")
                self.assertEqual(fast["credits_micros"],
                                 int(Decimal(standard["credits_micros_exact"]) * Decimal(multiplier)))
                self.assertEqual(fast["multiplier"], multiplier)
                self.assertTrue(supports_fast(model))

    def test_unsupported_fast_is_not_standard_or_free(self):
        self.assertFalse(supports_fast("gpt-5.4-mini"))
        self.assertFalse(supports_fast("gpt-5.3-codex-spark"))
        result = estimate_usage("gpt-5.4-mini", 100, 100, 0, "fast")
        self.assertIsNone(result["credits_micros"])
        self.assertEqual(result["unknown_reason"], "fast_not_supported")

    def test_dated_aliases_are_exact_and_calendar_valid(self):
        for model in ("gpt-6-astra", "gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna",
                      "gpt-5.6-sol", "gpt-5.4-mini", "gpt-5.4"):
            with self.subTest(model=model):
                self.assertEqual(canonical_model(model + "-2026-09-09"), model)
                self.assertEqual(estimate_usage(model + "-2026-09-09", 100, 100, 50)["credits_micros"],
                                 estimate_usage(model, 100, 100, 50)["credits_micros"])
        self.assertEqual(canonical_model("  GPT-5.6-SOL  "), "gpt-5.6-sol")
        self.assertTrue(supports_fast("gpt-6-astra-2026-09-09"))
        self.assertFalse(supports_fast("gpt-5.4-mini-2026-09-09"))

    def test_unknown_models_do_not_inherit_prefix_rates(self):
        for model in (None, 7, [], "", "gpt-5.6", "gpt-6", "gpt-5.4-nano", "gpt-5.4-miniature",
                      "gpt-5.6-sol-latest", "gpt-5.6-sol-extra", "gpt-5.4-mini-2026-02-30",
                      "gpt-5.6-sol-20260909", "gpt-5.6-sol-2026-9-9", "gpt-5.3-codex-spark",
                      "gpt-5.5-codex", "gpt-5.4-mini-2026-09-09-extra", "x" * 101):
            with self.subTest(model=model):
                result = estimate_usage(model, 100, 100, 0)
                self.assertIsNone(result["credits_micros"])
                self.assertEqual(result["unknown_reason"], "unsupported_model")
                self.assertFalse(supports_fast(model))

    def test_unknown_speed_is_not_zero_usage(self):
        for speed in (None, False, [], "", "priority", "flex", "faster", 2.5):
            with self.subTest(speed=speed):
                result = estimate_usage("gpt-5.6-sol", 100, 100, 0, speed)
                self.assertIsNone(result["credits_micros"])
                self.assertEqual(result["unknown_reason"], "unsupported_speed")
        self.assertEqual(estimate_usage("gpt-5.6-sol", 1, 0, 0, " FAST ")["credits_micros"], 250)

    def test_invalid_counts_are_explicitly_unknown(self):
        for value in (-1, None, True, False, 1.0, "1", Decimal(1), [], float("nan"),
                      float("inf"), MAX_TOKEN_COUNT + 1):
            for column in range(3):
                with self.subTest(value=value, column=column):
                    counts = [100, 100, 0]
                    counts[column] = value
                    result = estimate_usage("gpt-5.6-sol", *counts)
                    self.assertIsNone(result["credits_micros"])
                    self.assertEqual(result["unknown_reason"], "invalid_token_counts")

    def test_cached_input_cannot_exceed_input(self):
        result = estimate_usage("gpt-5.6-sol", 100, 10, 101)
        self.assertIsNone(result["credits_micros"])
        self.assertEqual(result["unknown_reason"], "cached_tokens_exceed_input")

    def test_actual_zero_is_distinct_from_unknown(self):
        result = estimate_usage("gpt-5.6-sol", 0, 0, 0)
        self.assertEqual(result["credits_micros"], 0)
        self.assertIsNone(result["unknown_reason"])

    def test_large_integer_counts_remain_exact_above_float_precision(self):
        count = (1 << 60) + 17
        result = estimate_usage("gpt-5.6-sol", count, 37, count - 11)
        self.assertEqual(result["credits_micros"], 11 * 100 + (count - 11) * 10 + 37 * 500)
        maximum = estimate_usage("gpt-6-astra", MAX_TOKEN_COUNT, MAX_TOKEN_COUNT, 0, "fast")
        self.assertEqual(maximum["credits_micros"], MAX_TOKEN_COUNT * 3750)

    def test_half_even_once_preserves_fractional_microcredits_without_float_ceiling(self):
        one = estimate_usage("gpt-5.6-luna", 1, 0, 1)
        three = estimate_usage("gpt-5.6-luna", 3, 0, 3)
        self.assertEqual(one["credits_micros"], 0)
        self.assertEqual(one["credits_micros_exact"], "0.5")
        self.assertEqual(three["credits_micros"], 2)
        mini = estimate_usage("gpt-5.4-mini", 8, 0, 8)
        self.assertEqual(mini["credits_micros"], 15)
        self.assertEqual(Decimal(mini["credits_micros_exact"]), Decimal(15))

    def test_ambient_decimal_context_cannot_change_results(self):
        with localcontext() as context:
            context.prec = 3
            context.rounding = ROUND_CEILING
            result = estimate_usage("gpt-5.4-mini", 987654321, 123456789, 987654320)
            one = estimate_usage("gpt-5.6-luna", 1, 0, 1)
        expected = Decimal(1) * Decimal("18.75") + Decimal(987654320) * Decimal("1.875") + Decimal(123456789) * 113
        self.assertEqual(Decimal(result["credits_micros_exact"]), expected)
        self.assertEqual(one["credits_micros"], 0)

    def test_cache_reclassification_replaces_prior_snapshot_and_can_reduce_estimate(self):
        before = estimate_usage("gpt-5.6-sol", 1000, 100, 0)
        after = estimate_usage("gpt-5.6-sol", 1000, 100, 800)
        self.assertEqual(after["credits_micros"] - before["credits_micros"], -72_000)
        self.assertEqual(after["credits_micros"], 78_000)
        self.assertEqual(after, estimate_usage("gpt-5.6-sol", 1000, 100, 800))

    def test_cumulative_snapshot_rounding_has_no_per_event_drift(self):
        previous, ledger = 0, 0
        for count in range(1, 1001):
            current = estimate_usage("gpt-5.6-luna", count, 0, count)["credits_micros"]
            ledger += current - previous
            previous = current
        self.assertEqual(ledger, 500)

    def test_estimate_has_provenance_not_a_quota_or_dollar_claim(self):
        result = estimate_usage("gpt-5.6-sol", 1000, 1000, 500)
        self.assertEqual(result["source"], SOURCE)
        self.assertEqual(result["rate_version"], RATE_VERSION)
        self.assertTrue(result["estimate_only"])
        self.assertIn("https://learn.chatgpt.com/docs/pricing", result["source_urls"])
        self.assertNotIn("used_percent", result)
        self.assertNotIn("usd", result)

    def test_new_model_rate_versions_do_not_relabel_legacy_estimates(self):
        for model, standard, fast in (("gpt-6.1-sol", 26_875, 53_750),
                                      ("gpt-6-sol", 28_750, 57_500),
                                      ("gpt-6-luna", 1_438, 2_875)):
            with self.subTest(model=model):
                result = estimate_usage(model, 1000, 50, 750)
                self.assertEqual(result["credits_micros"], standard)
                self.assertEqual(result["rate_version"], "2026-09-30")
                self.assertEqual(estimate_usage(model, 1000, 50, 750, "fast")["credits_micros"], fast)
                dated = estimate_usage(model + "-2026-09-29", 1000, 50, 750)
                self.assertEqual(dated, result)
        legacy = estimate_usage("gpt-6-astra", 1000, 50, 750, "fast")
        self.assertEqual(legacy["rate_version"], RATE_VERSION)
        self.assertEqual(legacy["multiplier"], "2.5")


if __name__ == "__main__":
    unittest.main()
