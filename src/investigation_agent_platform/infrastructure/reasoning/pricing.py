# src/investigation_agent_platform/infrastructure/reasoning/pricing.py
"""Versioned LLM pricing table (F-029).

Per-token rates are deployment data, not code constants: the table carries a
``PRICING_VERSION`` that is recorded on every ``LLMCallMetadata`` so cost
telemetry is always attributable to the rates that produced it. Operators may
override rates via ``IAP_PRICING_JSON`` (``{"model": [prompt_per_1k, completion_per_1k]}``)
without a code change. Estimates are labeled as estimates — never used as
hard spend limits.
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

PRICING_VERSION = "2026-09-01"

_BASE_RATES: dict[str, tuple[float, float]] = {
    "gpt-4o": (0.005, 0.015),
    "gpt-4o-mini": (0.00015, 0.0006),
    "gpt-4-turbo": (0.01, 0.03),
    "gpt-3.5-turbo": (0.0005, 0.0015),
    "claude-3-5-sonnet": (0.003, 0.015),
    "claude-3-opus": (0.015, 0.075),
    "claude-3-haiku": (0.00025, 0.00125),
    "claude-sonnet-4": (0.003, 0.015),
}

_FALLBACK_RATE = (0.005, 0.015)


def get_rates() -> tuple[str, dict[str, tuple[float, float]]]:
    """Return ``(pricing_version, rates)`` honoring ``IAP_PRICING_JSON`` overrides."""
    rates = dict(_BASE_RATES)
    raw = os.environ.get("IAP_PRICING_JSON", "")
    if raw:
        try:
            overrides = json.loads(raw)
            for model, pair in overrides.items():
                rates[str(model).lower()] = (float(pair[0]), float(pair[1]))
        except Exception as exc:
            logger.warning("Ignoring invalid IAP_PRICING_JSON: %s", exc)
    version = os.environ.get("IAP_PRICING_VERSION", PRICING_VERSION)
    return version, rates


def estimate_cost(model_name: str, prompt_tokens: int, completion_tokens: int) -> tuple[float, str]:
    """Return ``(estimated_cost_usd, pricing_version)`` for a call."""
    version, rates = get_rates()
    prompt_rate, completion_rate = rates.get(model_name.lower(), _FALLBACK_RATE)
    return (prompt_tokens / 1000.0) * prompt_rate + (
        completion_tokens / 1000.0
    ) * completion_rate, version
