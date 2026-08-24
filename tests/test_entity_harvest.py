"""Unit tests for section entity harvest (deterministic identifier inventory).

Regression guard for the misplaced-function-body bug (harvest_section_entities
used to return None because its implementation was pasted into harvest_acronyms
after an unconditional return).
"""
import importlib.util
import os

import pytest

_MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "core", "ingestion", "entity_harvest.py")


@pytest.fixture(scope="module")
def eh():
    spec = importlib.util.spec_from_file_location("entity_harvest", _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestHarvestSectionEntities:
    def test_returns_non_none_for_identifier_rich_text(self, eh):
        text = ("The UART0-UART9 interfaces support 1.8V. "
                "GPIO0-GPIO4 provide config. SPI0-SPI3 for flash.")
        out = eh.harvest_section_entities(text)
        assert isinstance(out, str)
        assert "UART" in out
        assert "GPIO" in out
        assert "SPI" in out

    def test_empty_text_returns_empty_string(self, eh):
        assert eh.harvest_section_entities("") == ""
        assert eh.harvest_section_entities(None) == ""

    def test_short_text_without_identifiers_returns_empty(self, eh):
        assert eh.harvest_section_entities("abc") == ""
        assert eh.harvest_section_entities("这是一段没有标识符的中文文本。") == ""

    def test_instances_are_compressed_to_ranges(self, eh):
        text = ("Signal pin mapping table for the main connector: "
                "GPIO0, GPIO1, GPIO2, GPIO3, GPIO4 are all available "
                "on the expansion header for peripheral configuration.")
        out = eh.harvest_section_entities(text)
        # 0-4 contiguous -> range form; family name appears once
        assert "GPIO" in out
        assert "0-4" in out

    def test_junk_single_letter_families_dropped(self, eh):
        text = ("Register layout overview: A0 B1 C2 X3 are scratch bytes, "
                "and also the real I2C0 I2C1 signals are on the sensor bus "
                "which carries all the low speed traffic.")
        out = eh.harvest_section_entities(text)
        assert "I2C" in out
        for junk in ("A0", "B1", "C2", "X3"):
            assert junk not in out

    def test_large_instance_numbers_ignored(self, eh):
        # 2025 is a year, not an instance; family must be >=2 chars
        text = ("Firmware version 2025 rev 1.0 was released with the new "
                "bootloader, and the UART0 port is present on the debug "
                "header for serial console access during bring-up.")
        out = eh.harvest_section_entities(text)
        assert "UART" in out
        assert "2025" not in out

    def test_output_capped(self, eh):
        text = " ".join(f"CH{i} value {j}" for i in range(60) for j in range(3))
        out = eh.harvest_section_entities(text)
        assert len(out) <= 1500


class TestHarvestAcronyms:
    def test_short_text_returns_empty(self, eh):
        assert eh.harvest_acronyms(None, "short") == ""

    def test_returns_none_string_as_empty(self, eh):
        class FakeClient:
            def generate(self, prompt, temperature=0.1, max_tokens=256):
                return "NONE"
        assert eh.harvest_acronyms(FakeClient(), "long enough text " * 20) == ""

    def test_returns_pairs(self, eh):
        class FakeClient:
            def generate(self, prompt, temperature=0.1, max_tokens=256):
                return "NPU=Neural Process Unit, IPU=Intelligence Processing Unit"
        out = eh.harvest_acronyms(FakeClient(), "long enough text " * 20)
        assert "NPU=Neural Process Unit" in out

    def test_exception_returns_empty(self, eh):
        class BoomClient:
            def generate(self, prompt, temperature=0.1, max_tokens=256):
                raise RuntimeError("boom")
        assert eh.harvest_acronyms(BoomClient(), "long enough text " * 20) == ""
