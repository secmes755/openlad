"""The answer self-check gate must be a decision, not an accident.

The gate read ``getattr(industry_pack, "name", "generic") != "generic"``, but no
plugin class has ever had a ``name`` attribute — identity lives on ``manifest.id``
— so the default always won and the check could never run. The composed plugin's
docstring in fact recorded the disabled state as intentional ("must stay disabled
until its own fix lands"), which is why the gate now reads ``manifest.id`` plus an
explicit ``self_check_enabled`` config that stays off by default.
"""
from types import SimpleNamespace

import pytest

from core.config import CONTEXT_CONFIG
from core.retrieval.synthesizer import AnswerSynthesizer


def _synth() -> AnswerSynthesizer:
    return AnswerSynthesizer.__new__(AnswerSynthesizer)


def _fake_pack(pack_id, extra: dict | None = None):
    pack = SimpleNamespace(manifest=SimpleNamespace(id=pack_id, name=f"{pack_id} pack"))
    for key, value in (extra or {}).items():
        setattr(pack, key, value)
    return pack


def test_off_by_default(monkeypatch):
    monkeypatch.setitem(CONTEXT_CONFIG, "self_check_enabled", False)
    assert _synth()._should_self_check(_fake_pack("sample_semiconductor")) is False


def test_enabled_for_an_industry_pack(monkeypatch):
    monkeypatch.setitem(CONTEXT_CONFIG, "self_check_enabled", True)
    assert _synth()._should_self_check(_fake_pack("sample_semiconductor")) is True


def test_the_generic_base_is_never_checked(monkeypatch):
    monkeypatch.setitem(CONTEXT_CONFIG, "self_check_enabled", True)
    assert _synth()._should_self_check(_fake_pack("generic")) is False


def test_no_pack_means_no_check(monkeypatch):
    monkeypatch.setitem(CONTEXT_CONFIG, "self_check_enabled", True)
    assert _synth()._should_self_check(None) is False


def test_a_name_attribute_is_no_longer_consulted(monkeypatch):
    """The old gate's only input was an attribute that never existed."""
    monkeypatch.setitem(CONTEXT_CONFIG, "self_check_enabled", True)
    no_manifest = SimpleNamespace(name="Semiconductor Industry Sample Pack")
    assert _synth()._should_self_check(no_manifest) is False
    assert _synth()._should_self_check(SimpleNamespace()) is False


def test_a_real_composed_pack_is_recognised(monkeypatch):
    """Uses the actual objects the query path passes in."""
    monkeypatch.setitem(CONTEXT_CONFIG, "self_check_enabled", True)
    from core.plugins import get_plugin_registry

    registry = get_plugin_registry()
    industry = registry.compose_with_base(registry.get_plugin("sample_semiconductor"))

    # This is exactly what made the old gate dead: the object has no `name`.
    assert getattr(industry, "name", None) is None
    assert industry.manifest.id == "sample_semiconductor"
    assert _synth()._should_self_check(industry) is True

    generic = registry.compose_with_base(registry.get_plugin("generic"))
    assert generic.manifest.id == "generic"
    assert _synth()._should_self_check(generic) is False
