"""The synthesis context budget has exactly one source of truth.

``synthesis_context_budget`` used to be read as
``cfg.get("synthesis_context_budget", 35500)`` in four places. The magic number
in each call site meant that removing or renaming the config key would silently
fall back to a stale value — the config would appear to work while changing
nothing. Consumers now index the key directly, so a missing key fails loudly at
the first query instead of degrading quietly.

These tests pin both halves: the key must exist, and no consumer may carry its
own copy of the number.
"""

import re

import pytest

from core.config import settings

CONSUMERS = [
    "core/retrieval/executor.py",
    "core/retrieval/engine.py",
]


def test_budget_key_exists():
    assert "synthesis_context_budget" in settings.CONTEXT_CONFIG


def test_budget_is_positive_and_below_the_safe_ceiling():
    budget = settings.CONTEXT_CONFIG["synthesis_context_budget"]
    safe = settings.CONTEXT_CONFIG["synthesis_safe_chars"]
    assert 0 < budget <= safe, (
        f"budget {budget} must sit at or below the safe prompt ceiling {safe}; "
        "above it every synthesis hits the model client's truncation guard"
    )


def test_llm_max_tokens_is_the_per_slot_context_not_the_total():
    """The value is compared against a single request's window.

    llama.cpp gives each parallel slot ``--ctx-size / --parallel`` tokens, so a
    config that equalled the *total* ctx would ask for twice the window each
    request actually has.
    """
    cfg = settings.CONTEXT_CONFIG
    ctx = cfg["llm_max_tokens"]
    safe = cfg["synthesis_safe_chars"]
    out_max = cfg.get("synthesis_max_tokens", 4096)
    ratio = cfg["token_to_char_ratio"]

    prompt_tokens = safe / ratio
    assert prompt_tokens + out_max <= ctx * 1.5, (
        f"safe prompt ({prompt_tokens:.0f} tok) + output ({out_max} tok) exceeds "
        f"llm_max_tokens ({ctx}); prompt and completion share one window"
    )


@pytest.mark.parametrize("path", CONSUMERS)
def test_consumers_do_not_hardcode_a_budget_fallback(path):
    src = open(path).read()
    offenders = re.findall(r'\.get\(\s*"synthesis_context_budget"\s*,\s*\d+', src)
    assert not offenders, (
        f"{path} still carries a numeric fallback for synthesis_context_budget: "
        f"{offenders}. A per-call-site default silently overrides config."
    )


@pytest.mark.parametrize("path", CONSUMERS)
def test_consumers_read_the_key_from_config(path):
    src = open(path).read()
    assert 'cfg["synthesis_context_budget"]' in src, (
        f"{path} should index the config key directly"
    )
