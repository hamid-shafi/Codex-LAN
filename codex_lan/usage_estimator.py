"""CODEX: Published credit-weighted estimates, not OpenAI's included-quota formula.

Re-estimate cumulative token snapshots and replace prior estimates. Cache counts
can be revised without increasing input counts, so adding independently clamped
token deltas or rounding each streamed token would overcount usage. Output counts
already include reasoning tokens; reasoning effort gets no extra multiplier.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
import re


RATE_VERSION = "2026-09-09"
SOURCE = "published_credit_rate_estimate"
PRICING_URL = "https://learn.chatgpt.com/docs/pricing"
SPEED_URL = "https://learn.chatgpt.com/docs/agent-configuration/speed"
MAX_TOKEN_COUNT = (1 << 63) - 1

# CODEX: Credits per million input / cached input / output tokens; new models carry their own verification date.
_RATES = {
    "gpt-6-astra": ("250", "25", "1250"),
    "gpt-6.1-sol": ("50", "2.5", "250"),
    "gpt-6-sol": ("50", "5", "250"),
    "gpt-6-luna": ("2.5", "0.25", "12.5"),
    "gpt-5.6-sol": ("100", "10", "500"),
    "gpt-5.6-terra": ("50", "5", "300"),
    "gpt-5.6-luna": ("5", "0.5", "30"),
    "gpt-5.5": ("125", "12.5", "750"),
    "gpt-5.4": ("62.5", "6.25", "375"),
    "gpt-5.4-mini": ("18.75", "1.875", "113"),
}
_RATE_VERSIONS = {
    "gpt-6.1-sol": "2026-09-30",
    "gpt-6-sol": "2026-09-30",
    "gpt-6-luna": "2026-09-30",
}

# CODEX: New models use purchased-credit/Enterprise pay-as-you-go Fast 2x, not included-subscription 2.5x.
# CODEX: Existing model multipliers are preserved pending a separate versioned accounting migration.
_FAST_MULTIPLIERS = {
    "gpt-6-astra": "2.5",
    "gpt-6.1-sol": "2",
    "gpt-6-sol": "2",
    "gpt-6-luna": "2",
    "gpt-5.6-sol": "2.5",
    "gpt-5.6-terra": "2.5",
    "gpt-5.6-luna": "2.5",
    "gpt-5.5": "2.5",
    "gpt-5.4": "2",
}
_DATED_MODEL = re.compile(r"(?P<model>.+)-(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})\Z")


def canonical_model(model):
    """CODEX: Match exact priced aliases and dated snapshots, never model-name prefixes."""
    if not isinstance(model, str) or len(model) > 100:
        return None
    model = model.strip().lower()
    if model in _RATES:
        return model
    dated = _DATED_MODEL.fullmatch(model)
    if dated is None or dated["model"] not in _RATES:
        return None
    try:
        date.fromisoformat(dated["date"])
    except ValueError:
        return None
    return dated["model"]


def supports_fast(model):
    """CODEX: Report documented Fast pricing support; the provider still checks access."""
    return canonical_model(model) in _FAST_MULTIPLIERS


def estimate_usage(model, input_tokens, output_tokens, cached_tokens, speed="standard"):
    """CODEX: Return an integer microcredit estimate or explicit unknown, without quota conversion."""
    canonical = canonical_model(model)
    normalized_speed = speed.strip().lower() if isinstance(speed, str) else None
    result = {
        "credits_micros": None,
        "credits_micros_exact": None,
        "source": SOURCE,
        "rate_version": _RATE_VERSIONS.get(canonical, RATE_VERSION),
        "source_urls": [PRICING_URL, SPEED_URL],
        "model": canonical,
        "speed": normalized_speed if normalized_speed in ("standard", "fast") else None,
        "estimate_only": True,
        "rounding": "half_even_per_cumulative_snapshot",
        "unknown_reason": None,
    }
    if canonical is None:
        result["unknown_reason"] = "unsupported_model"
        return result
    if normalized_speed not in ("standard", "fast"):
        result["unknown_reason"] = "unsupported_speed"
        return result
    if normalized_speed == "fast" and not supports_fast(canonical):
        result["unknown_reason"] = "fast_not_supported"
        return result
    counts = (input_tokens, output_tokens, cached_tokens)
    if any(isinstance(value, bool) or not isinstance(value, int)
           or not 0 <= value <= MAX_TOKEN_COUNT for value in counts):
        result["unknown_reason"] = "invalid_token_counts"
        return result
    if cached_tokens > input_tokens:
        result["unknown_reason"] = "cached_tokens_exceed_input"
        return result

    with localcontext() as context:
        context.prec = 64
        input_rate, cached_rate, output_rate = (Decimal(rate) for rate in _RATES[canonical])
        multiplier = Decimal(_FAST_MULTIPLIERS[canonical] if normalized_speed == "fast" else "1")
        # CODEX: Per-million-token credit rates equal microcredits per token; count cached input only once.
        exact = ((input_tokens - cached_tokens) * input_rate
                 + cached_tokens * cached_rate + output_tokens * output_rate) * multiplier
        result.update(
            credits_micros=int(exact.to_integral_value(rounding=ROUND_HALF_EVEN)),
            credits_micros_exact=format(exact, "f"),
            multiplier=format(multiplier, "f"),
            rates_per_million={"input": str(input_rate), "cached_input": str(cached_rate),
                               "output": str(output_rate)},
        )
    return result
