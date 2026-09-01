"""Pack-gating tests for spec-fact extraction P2/P3 and planner stopwords.

The "Support <num> <feature> <object>" (P2) and codec-resolution (P3)
patterns used to carry hardcoded datasheet vocabulary in core
(interfaces/channels/.../controllers; H.264, HEVC, VP-family...). That vocabulary
is now injected by industry packs via extraction.support_objects /
extraction.resolution_codecs; core keeps only the sentence mechanisms.
These tests pin: (a) gated patterns fire with pack vocabulary, (b) they are
inert without it, (c) verbatim-form semantics (no auto-pluralization —
"bits" plural-only excludes bit-width declarations), (d) the planner's
entity stopwords merge core meta-words with pack-injected words.
"""

from core.ingestion.spec_facts_extractor import (
    _build_resolution_re,
    _build_support_re,
    extract_spec_facts_from_text,
)

_SUPPORT_OBJECTS = ["interface", "interfaces", "channel", "channels",
                    "ports", "lanes", "bits", "core", "cores", "display",
                    "displays", "camera", "cameras", "screen", "screens",
                    "controller", "controllers"]
_CODECS = ["H.264", "H.265", "HEVC", "VP8", "VP9", "AV1",
           "MPEG2", "MPEG-2", "MPEG4", "MPEG-4", "JPEG"]


def _extract(text, **overrides):
    extraction = {"spec_headers": ["uart", "pcie", "i2c"],
                  "compute_units": ["TOPS"],
                  "compute_attribute": "compute power",
                  "frequency_terms": ["frequency", "clock"]}
    extraction.update(overrides)
    return extract_spec_facts_from_text(text, 1, "CHIPX", "doc1",
                                        extraction=extraction)


class TestSupportGating:
    def test_fires_with_pack_vocabulary(self):
        facts = _extract("Support ten UART interfaces",
                         support_objects=_SUPPORT_OBJECTS)
        # Verbatim object form is kept in the attribute ("interfaces").
        assert any(f["attribute"] == "UART interfaces count"
                   and f["value"] == "10" for f in facts), facts

    def test_inert_without_vocabulary(self):
        # Only spec_headers etc. — P2 must be disabled.
        assert _extract("Support ten UART interfaces") == []

    def test_empty_list_disables(self):
        assert _extract("Support ten UART interfaces",
                        support_objects=[]) == []

    def test_bits_plural_only_excludes_width_declaration(self):
        # "16 to 31 bit" is a bit-width declaration, not a count. The pack
        # lists only plural "bits" so this must NOT produce a fact.
        facts = _extract(
            "Support 16 to 31 bit audio data left or right justified",
            support_objects=_SUPPORT_OBJECTS)
        assert not any("bit" in f["attribute"].lower() for f in facts), facts

    def test_plural_bits_still_matches(self):
        # P2 shape is num + feature + object; feature must be non-empty.
        facts = _extract("Support two 128 bits keys",
                         support_objects=_SUPPORT_OBJECTS)
        assert any(f["attribute"].endswith("bits count")
                   and f["value"] == "2" for f in facts), facts

    def test_only_support_objects_still_extracts(self):
        # Master switch: support_objects alone (no spec_headers/compute/freq)
        # must be enough to activate extraction.
        facts = extract_spec_facts_from_text(
            "Support ten UART interfaces", 1, "CHIPX", "doc1",
            extraction={"support_objects": _SUPPORT_OBJECTS})
        assert any(f["attribute"] == "UART interfaces count"
                   for f in facts), facts


class TestResolutionGating:
    def test_fires_with_pack_vocabulary(self):
        facts = _extract("H.264 BP/MP/HP, up to 3840x2160@25fps",
                         resolution_codecs=_CODECS)
        assert any(f["attribute"] == "H.264 max resolution"
                   and "3840x2160" in f["value"] for f in facts), facts

    def test_inert_without_vocabulary(self):
        assert _extract("H.264 BP/MP/HP, up to 3840x2160@25fps") == []

    def test_verbatim_matching_no_prefix_steal(self):
        # H.264 and H.265 are distinct literals; longest-first ordering must
        # keep them apart.
        facts = _extract("H.265 main profile, up to 7680x4320@30fps",
                         resolution_codecs=_CODECS)
        assert any(f["attribute"] == "H.265 max resolution" for f in facts), facts

    def test_unlisted_codec_not_matched(self):
        # MJPEG is not in the pack list; the embedded "JPEG" substring must
        # not match (word-boundary).
        facts = _extract("MJPEG, up to 1920x1080@60fps",
                         resolution_codecs=_CODECS)
        assert not any("resolution" in f["attribute"] for f in facts), facts


class TestBuilderFunctions:
    def test_support_builder_none_on_empty(self):
        assert _build_support_re([]) is None
        assert _build_support_re(None) is None

    def test_resolution_builder_none_on_empty(self):
        assert _build_resolution_re([]) is None
        assert _build_resolution_re(None) is None


class TestPlannerStopwords:
    def test_core_metawords_always_present(self):
        from core.retrieval.planner import QueryPlanner
        sw = QueryPlanner._CN_ENTITY_STOPWORDS
        for w in ("多少", "什么", "如何", "是什么", "情况", "主要"):
            assert w in sw

    def test_core_no_longer_carries_financial_words(self):
        # Domain vocabulary must live in packs, not core.
        from core.retrieval.planner import QueryPlanner
        sw = QueryPlanner._CN_ENTITY_STOPWORDS
        for w in ("营业收入", "净利润", "公司", "年报", "每股收益"):
            assert w not in sw

    def test_pack_stopwords_merge(self, monkeypatch):
        from core.retrieval.planner import QueryPlanner
        QueryPlanner._STOPWORDS_CACHE = None

        class _FakeRetrieval:
            def get_entity_stopwords(self):
                return ["营业收入", "公司"]

        class _FakePlugin:
            retrieval = _FakeRetrieval()

        class _FakeRegistry:
            def list_plugins(self):
                return ["fake_pack"]

            def get_plugin(self, pack_id):
                return _FakePlugin()

        monkeypatch.setattr("core.plugins.get_plugin_registry",
                            lambda: _FakeRegistry())
        try:
            sw = QueryPlanner._all_entity_stopwords()
            assert "营业收入" in sw      # pack-injected
            assert "公司" in sw          # pack-injected
            assert "多少" in sw          # core meta-word survives
            assert "心肌梗死" not in sw  # domain entity NOT filtered
        finally:
            QueryPlanner._STOPWORDS_CACHE = None

    def test_pack_stopwords_failure_falls_back_to_core(self, monkeypatch):
        from core.retrieval.planner import QueryPlanner
        QueryPlanner._STOPWORDS_CACHE = None

        def _boom():
            raise RuntimeError("registry unavailable")

        monkeypatch.setattr("core.plugins.get_plugin_registry", _boom)
        try:
            sw = QueryPlanner._all_entity_stopwords()
            assert sw == QueryPlanner._CN_ENTITY_STOPWORDS
        finally:
            QueryPlanner._STOPWORDS_CACHE = None
