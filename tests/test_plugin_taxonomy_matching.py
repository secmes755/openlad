"""Auto-detection chain: can a pack be resolved from the categories the
classifier actually produces?

The LLM classifier reads pack taxonomy.yaml (Chinese for the sample
semiconductor pack) and may emit Chinese category values (e.g. 数据手册),
while manifest.category_mapping is English (Datasheets). Bilingual matching
must work in BOTH languages or spec-fact extraction silently loses its
vocabulary pack for auto-ingested documents.
"""
from core.plugins import (
    IndustryManifest,
    IngestionConfig,
    PluginRegistry,
    RetrievalConfig,
    SharedConfig,
    YAMLIndustryPlugin,
)


def _registry_with_semiconductor_like_pack() -> PluginRegistry:
    manifest = IndustryManifest(
        id="sample_semiconductor",
        name="Semiconductor",
        version="1.0.0",
        description="",
        category_mapping=[
            "Technical & Product Documents",
            "Chip Specifications",
            "Technical Manuals",
            "Datasheets",
            "Circuit Schematics",
        ],
    )
    shared = SharedConfig(
        taxonomy={
            "level1": "技术与产品文档",
            "level2": [
                {"name": "数据手册", "level3": ["处理器芯片", "存储芯片"]},
                {"name": "技术手册", "level3": ["硬件设计指南"]},
                {"name": "原理图", "level3": ["系统原理图"]},
                {"name": "规格书", "level3": []},
            ],
        }
    )
    pack = YAMLIndustryPlugin(manifest, IngestionConfig(), RetrievalConfig(), shared)

    registry = PluginRegistry(scan_dirs=[])
    registry._register(pack)
    return registry


class TestResolveFromClassifierCategories:
    def test_english_mapping_still_matches(self):
        reg = _registry_with_semiconductor_like_pack()
        assert reg.resolve_plugin_for_categories(
            ["RK3588", "Datasheet", "Technical Documentation"]) is not None

    def test_chinese_taxonomy_names_match(self):
        """The auto path: LLM emits taxonomy.yaml's Chinese names."""
        reg = _registry_with_semiconductor_like_pack()
        assert reg.resolve_plugin_for_categories(
            ["处理器芯片", "数据手册", "技术与产品文档"]) is not None

    def test_chinese_level3_alone_matches(self):
        reg = _registry_with_semiconductor_like_pack()
        assert reg.resolve_plugin_for_categories(["存储芯片"]) is not None

    def test_unrelated_category_resolves_to_none(self):
        reg = _registry_with_semiconductor_like_pack()
        assert reg.resolve_plugin_for_categories(["年度报告", "财报"]) is None

    def test_query_time_category_lookup_matches_chinese(self):
        reg = _registry_with_semiconductor_like_pack()
        assert reg.get_plugin_by_category("数据手册") is not None
        assert reg.get_plugin_by_category("技术与产品文档") is not None
