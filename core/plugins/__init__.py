"""
OpenLAD Plugin System Interface Definition
"""
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)


# =============================================================================
# Data Class Definitions
# =============================================================================

@dataclass
class IndustryManifest:
    """Industry Package Metadata"""
    id: str
    name: str
    version: str
    description: str
    author: str = ""
    category_mapping: list[str] = field(default_factory=list)
    entry_point: str | None = None  # Python entry module (optional)
    path: str = ""
    is_builtin: bool = False

    @classmethod
    def from_yaml(cls, path: Path) -> Optional["IndustryManifest"]:
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if not data:
                return None
            return cls(
                id=data.get("id", path.parent.name),
                name=data.get("name", path.parent.name),
                version=str(data.get("version", "1.0.0")),
                description=data.get("description", ""),
                author=data.get("author", ""),
                category_mapping=data.get("category_mapping", []),
                entry_point=data.get("entry_point"),
                path=str(path.parent),
                is_builtin=data.get("is_builtin", False),
            )
        except Exception as e:
            logger.error(f"[PLUGIN] Failed to load manifest {path}: {e}")
            return None


@dataclass
class IngestionConfig:
    """Ingestion Phase Configuration"""
    prompts: dict[str, str] = field(default_factory=dict)
    rules: dict[str, Any] = field(default_factory=dict)
    skill: dict[str, Any] = field(default_factory=dict)
    sop: str = ""


@dataclass
class RetrievalConfig:
    """Retrieval Phase Configuration"""
    prompts: dict[str, str] = field(default_factory=dict)
    rules: dict[str, Any] = field(default_factory=dict)
    templates: dict[str, str] = field(default_factory=dict)
    answer_constraints: dict[str, Any] = field(default_factory=dict)


@dataclass
class SharedConfig:
    """Shared Industry Knowledge"""
    glossary: dict[str, str] = field(default_factory=dict)
    taxonomy: dict[str, Any] = field(default_factory=dict)


# =============================================================================
# Abstract Base Classes
# =============================================================================

class IngestionPlugin(ABC):
    """Ingestion Phase Plugin Interface

    Industry packages can implement this interface to customize document ingestion:
    - Document classification strategy
    - Entity extraction rules
    - Layout parsing rules
    - Summary enhancement
    """

    @abstractmethod
    def classify_document(self, content_sample: str, filename: str) -> dict[str, Any]:
        """Document classification: returns {category_level1, category_level2, category_level3, confidence}"""
        pass

    @abstractmethod
    def extract_entities(self, page_text: str, page_type: str) -> list[dict[str, Any]]:
        """Entity extraction: returns [{type, value, position, confidence}]"""
        pass

    @abstractmethod
    def get_ingestion_prompts(self) -> dict[str, str]:
        """Get prompt templates for ingestion"""
        pass

    @abstractmethod
    def get_parsing_rules(self) -> dict[str, Any]:
        """Get parsing rules (regex, structural patterns, etc.)"""
        pass

    def enhance_summary(self, raw_summary: str, doc_metadata: dict) -> str:
        """Enhance document summary (optional, returns raw by default)"""
        return raw_summary

    def get_skill_config(self) -> dict[str, Any] | None:
        """Get Skill processing strategy config (optional)"""
        return None

    # === OpenLAD Industry Package Hook Interface (optional) ===
    def detect_document_subtype(self, parsed_doc: Any) -> str | None:
        """Detect document subtype (e.g., schematic / datasheet / layout).
        Returns None to skip and use the generic flow."""
        return None

    def process_page(self, page: Any, raw_text: str,
                     layout_result: Any = None,
                     model_client: Any = None,
                     page_image: Any = None) -> dict[str, Any] | None:
        """Industry package post-processing for a single page.
        Returns optional extra_data; core stores it in doc_pages.extra_data.

        Args:
            page_image: Optional PIL Image object. Provided so industry packages can
                        call vision models without re-rendering. Core does not
                        interpret the image content.

        Return format:
        {
            "subtype": "schematic",           # Document subtype identifier
            "data": { ... },                  # Structured data (stored in extra_data)
            "searchable_text": "...",         # Searchable text appended to raw_text
            "page_type_override": "..."      # Optional: override layout_analyzer page_type
        }
        """
        return None

    def on_document_complete(self, doc_id: str, metadata_db: Any) -> None:
        """Callback after document ingestion completes. Industry pack can do cross-page aggregation here."""
        pass


class RetrievalPlugin(ABC):
    """Retrieval Phase Plugin Interface

    Industry packages can implement this interface to customize query retrieval:
    - Query understanding enhancement
    - Retrieval strategy adjustment
    - Answer synthesis rules
    - Term disambiguation
    """

    @abstractmethod
    def rewrite_query(self, query: str, chat_history: list[dict] = None) -> str:
        """Query rewriting: transform user query into a form more suitable for retrieval"""
        pass

    @abstractmethod
    def get_retrieval_prompts(self) -> dict[str, str]:
        """Get prompt templates for retrieval (system_prompt, answer_rules, etc.)"""
        pass

    @abstractmethod
    def get_answer_constraints(self) -> dict[str, Any]:
        """Get answer constraints (format, depth, citation standards)"""
        pass

    def disambiguate_terms(self, query: str) -> dict[str, str]:
        """Term disambiguation: returns {original_term: canonical_term}"""
        return {}

    def get_synonyms(self, term: str) -> list[str]:
        """Get term synonyms"""
        return []

    def get_spec_query_terms(self) -> dict[str, list[str]]:
        """Industry-specific spec-fact query terms: {query_term: [synonyms]}.
        Merged with core's generic table during spec-fact assertion lookup,
        so industry vocabulary lives in the pack instead of core."""
        return {}

    def get_entity_patterns(self) -> list[str]:
        """Regex patterns (one capture group each) to extract the document
        entity (e.g. chip model) from title/filename at ingest time.
        Core falls back to the plain title/filename when empty."""
        return []

    def format_citation(self, page_num: int, doc_title: str = "") -> str:
        """Format citation (default [^page_num^])"""
        return f"[^{page_num}^]"

    def post_process_answer(self, answer: str, context: str) -> str:
        """Answer post-processing (optional, returns raw by default)"""
        return answer

    # === OpenLAD Industry Package Hook Interface (optional) ===
    def supplement_results(self, query: str, results: list[Any],
                           metadata_db: Any = None) -> list[Any]:
        """Supplement recall: when FTS/Vector recall is insufficient,
        the industry pack can supplement with specialized logic.
        Returns additional SearchResult list (core handles dedup and merging)."""
        return []

    def enhance_context(self, query: str, context: str,
                        sources: list[dict[str, Any]] = None) -> str:
        """Enhance context: before synthesis, industry pack can make final adjustments to context."""
        return context


class IndustryPlugin(ABC):
    """Industry Package Main Interface

    Each industry package must implement this interface, or provide a YAML config
    file for automatic parsing by the system.
    """

    @property
    @abstractmethod
    def manifest(self) -> IndustryManifest:
        """Industry package metadata"""
        pass

    @property
    @abstractmethod
    def ingestion(self) -> IngestionPlugin:
        """Ingestion plugin"""
        pass

    @property
    @abstractmethod
    def retrieval(self) -> RetrievalPlugin:
        """Retrieval plugin"""
        pass

    def match_category(self, category: str) -> float:
        """Match score: 0.0-1.0, how well category fits the industry package"""
        if not category:
            return 0.0
        for mapped in self.manifest.category_mapping:
            if mapped == category:
                return 1.0
            if mapped in category or category in mapped:
                return 0.8
        return 0.0


# =============================================================================
# YAML-driven Default Implementations
# =============================================================================

class YAMLIngestionPlugin(IngestionPlugin):
    """Default ingestion plugin implementation based on YAML config"""

    def __init__(self, config: IngestionConfig, shared: SharedConfig):
        self.config = config
        self.shared = shared

    def classify_document(self, content_sample: str, filename: str) -> dict[str, Any]:
        # Return empty by default, relies on generic classifier + industry prompts
        return {}

    def extract_entities(self, page_text: str, page_type: str) -> list[dict[str, Any]]:
        # Return empty by default, relies on generic entity extraction
        return []

    def get_ingestion_prompts(self) -> dict[str, str]:
        return self.config.prompts

    def get_parsing_rules(self) -> dict[str, Any]:
        return self.config.rules

    def enhance_summary(self, raw_summary: str, doc_metadata: dict) -> str:
        return raw_summary

    def get_skill_config(self) -> dict[str, Any] | None:
        return self.config.skill if self.config.skill else None


class YAMLRetrievalPlugin(RetrievalPlugin):
    """Default retrieval plugin implementation based on YAML config"""

    def __init__(self, config: RetrievalConfig, shared: SharedConfig):
        self.config = config
        self.shared = shared

    def rewrite_query(self, query: str, chat_history: list[dict] = None) -> str:
        # No rewriting by default
        return query

    def get_retrieval_prompts(self) -> dict[str, str]:
        return self.config.prompts

    def get_answer_constraints(self) -> dict[str, Any]:
        return self.config.answer_constraints

    def disambiguate_terms(self, query: str) -> dict[str, str]:
        result = {}
        for term, canonical in self.shared.glossary.items():
            if term in query:
                result[term] = canonical
        return result

    def get_synonyms(self, term: str) -> list[str]:
        # Look up synonyms from glossary (reverse mapping)
        return []

    def get_spec_query_terms(self) -> dict[str, list[str]]:
        # Industry spec-fact query terms from rules.yaml (default empty).
        return self.config.rules.get("spec_query_terms", {}) or {}

    def get_entity_patterns(self) -> list[str]:
        # Document-entity regex patterns from rules.yaml (default empty).
        return self.config.rules.get("entity_patterns", []) or []

    def format_citation(self, page_num: int, doc_title: str = "") -> str:
        return f"[^{page_num}^]"

    def post_process_answer(self, answer: str, context: str) -> str:
        return answer

    def get_section_boost_rules(self) -> dict[str, Any]:
        """Get section boost rules (if configured by industry package)"""
        return self.config.rules.get("section_boost_rules", {})

    def get_retrieval_rules(self) -> dict[str, Any]:
        """Get complete retrieval rules (including query_expansion, low_value_sections, spec_sections, etc.)"""
        return self.config.rules

    def get_query_expansion_keywords(self) -> list[str]:
        """Get query expansion keywords (for decomposed_retrieve sub-query enhancement)"""
        return self.config.rules.get("query_expansion", {}).get("keywords", [])

    def get_low_value_sections(self) -> list[dict[str, Any]]:
        """Get low-value section penalty rules (filter irrelevant pages during retrieval phase)"""
        return self.config.rules.get("low_value_sections", [])

    def get_spec_sections(self) -> list[dict[str, Any]]:
        """Get spec-related section boost rules (boost key pages during retrieval phase)"""
        return self.config.rules.get("spec_sections", [])


class YAMLIndustryPlugin(IndustryPlugin):
    """Default industry package implementation based on YAML config"""

    def __init__(self, manifest: IndustryManifest,
                 ingestion_config: IngestionConfig,
                 retrieval_config: RetrievalConfig,
                 shared_config: SharedConfig):
        self._manifest = manifest
        self._ingestion = YAMLIngestionPlugin(ingestion_config, shared_config)
        self._retrieval = YAMLRetrievalPlugin(retrieval_config, shared_config)

    @property
    def manifest(self) -> IndustryManifest:
        return self._manifest

    @property
    def ingestion(self) -> IngestionPlugin:
        return self._ingestion

    @property
    def retrieval(self) -> RetrievalPlugin:
        return self._retrieval

    def build_prompt(self, query: str, context: str, catalog: str = "",
                     history_section: str = "", schematic_context: str = None) -> str:
        """Build synthesis prompt from industry package config"""
        prompts = self._retrieval.config.prompts or {}
        constraints = self._retrieval.config.answer_constraints or {}

        system_prompt = prompts.get("system_prompt", "")
        answer_rules = prompts.get("answer_rules", [])
        context_filtering = prompts.get("context_filtering", "")
        output_structure = prompts.get("output_structure", "")

        format_constraints = constraints.get("format_constraints", [])
        depth_constraints = constraints.get("depth_constraints", [])
        prohibited_behaviors = constraints.get("prohibited_behaviors", [])

        prompt = f"""基于以下信息回答用户问题。{history_section}

知识库目录（列出所有可用文档，问"有哪些信息/文档"时据此回答全部）：
{catalog}

用户问题：{query}"""

        if schematic_context:
            prompt += f"""

原理图结构化数据（精确引脚-网络连接关系）：
{schematic_context}"""

        prompt += f"""

检索到的详细内容：
{context}"""

        if system_prompt:
            prompt += f"""

{system_prompt}"""

        if context_filtering:
            prompt += f"""

{context_filtering}"""

        rules_text = "\n".join(f"- {rule}" for rule in answer_rules)
        if rules_text:
            prompt += f"""

回答规则：
{rules_text}"""

        fmt_text = "\n".join(f"- {c}" for c in format_constraints)
        depth_text = "\n".join(f"- {c}" for c in depth_constraints)
        prohibit_text = "\n".join(f"- {c}" for c in prohibited_behaviors)

        if fmt_text:
            prompt += f"""

格式约束：
{fmt_text}"""
        if depth_text:
            prompt += f"""

深度约束：
{depth_text}"""
        if prohibit_text:
            prompt += f"""

禁止行为：
{prohibit_text}"""

        if output_structure:
            prompt += f"""

输出格式：
{output_structure}"""

        prompt += """

请用中文回答。"""
        return prompt


# =============================================================================
# Plugin Registry
# =============================================================================

class PluginRegistry:
    """Industry Package Registry

    Scans the industries/ directory and loads all industry packages.
    Supports hot reload (optional).
    """

    def __init__(self, scan_dirs: list[str] = None):
        self.scan_dirs = scan_dirs or []
        self._plugins: dict[str, IndustryPlugin] = {}
        self._category_map: dict[str, str] = {}  # category -> plugin_id
        self._load_all()

    def _load_all(self):
        for dir_path in self.scan_dirs:
            path = Path(dir_path)
            if not path.exists():
                logger.info(f"[PLUGIN_REGISTRY] Directory not found, skipping: {dir_path}")
                continue
            for subdir in path.iterdir():
                if not subdir.is_dir():
                    continue
                if subdir.name.startswith("_"):
                    continue  # Skip private directories like _proprietary
                manifest_path = subdir / "manifest.yaml"
                if manifest_path.exists():
                    self._load_package(subdir, manifest_path)

    def _load_package(self, package_dir: Path, manifest_path: Path):
        manifest = IndustryManifest.from_yaml(manifest_path)
        if not manifest:
            return

        manifest.path = str(package_dir)

        # Read sub-configs
        ingestion_config = self._load_ingestion_config(package_dir)
        retrieval_config = self._load_retrieval_config(package_dir)
        shared_config = self._load_shared_config(package_dir)

        # If Python entry point exists, try dynamic loading
        if manifest.entry_point:
            plugin = self._load_python_plugin(manifest, package_dir)
            if plugin:
                self._register(plugin)
                return

        # Default to YAML-driven
        plugin = YAMLIndustryPlugin(manifest, ingestion_config, retrieval_config, shared_config)
        self._register(plugin)

    def _load_ingestion_config(self, package_dir: Path) -> IngestionConfig:
        config = IngestionConfig()
        prompts_path = package_dir / "ingestion" / "prompts.yaml"
        rules_path = package_dir / "ingestion" / "rules.yaml"
        skill_path = package_dir / "ingestion" / "skill.yaml"

        if prompts_path.exists():
            with open(prompts_path, encoding="utf-8") as f:
                config.prompts = yaml.safe_load(f) or {}
        if rules_path.exists():
            with open(rules_path, encoding="utf-8") as f:
                config.rules = yaml.safe_load(f) or {}
        if skill_path.exists():
            with open(skill_path, encoding="utf-8") as f:
                config.skill = yaml.safe_load(f) or {}

        return config

    def _load_retrieval_config(self, package_dir: Path) -> RetrievalConfig:
        config = RetrievalConfig()
        prompts_path = package_dir / "retrieval" / "prompts.yaml"
        rules_path = package_dir / "retrieval" / "rules.yaml"
        constraints_path = package_dir / "retrieval" / "answer_constraints.yaml"
        extraction_rules_path = package_dir / "retrieval" / "extraction_rules.yaml"

        if prompts_path.exists():
            with open(prompts_path, encoding="utf-8") as f:
                config.prompts = yaml.safe_load(f) or {}
        if rules_path.exists():
            with open(rules_path, encoding="utf-8") as f:
                config.rules = yaml.safe_load(f) or {}
        if constraints_path.exists():
            with open(constraints_path, encoding="utf-8") as f:
                config.answer_constraints = yaml.safe_load(f) or {}
        # FIX: Load structured extraction rules for exhaustive scan queries
        if extraction_rules_path.exists():
            with open(extraction_rules_path, encoding="utf-8") as f:
                extraction_data = yaml.safe_load(f) or {}
                # Merge into rules for unified access by industry packages and synthesizer
                config.rules["exhaustive_extraction"] = extraction_data.get("exhaustive_extraction", {})

        return config

    def _load_shared_config(self, package_dir: Path) -> SharedConfig:
        config = SharedConfig()
        glossary_path = package_dir / "shared" / "glossary.yaml"
        taxonomy_path = package_dir / "shared" / "taxonomy.yaml"

        if glossary_path.exists():
            with open(glossary_path, encoding="utf-8") as f:
                config.glossary = yaml.safe_load(f) or {}
        if taxonomy_path.exists():
            with open(taxonomy_path, encoding="utf-8") as f:
                config.taxonomy = yaml.safe_load(f) or {}

        return config

    def _load_python_plugin(self, manifest: IndustryManifest, package_dir: Path) -> IndustryPlugin | None:
        """Attempt to dynamically load Python plugin class"""
        try:
            import importlib.util
            import sys
            entry = manifest.entry_point
            if ":" in entry:
                module_path, class_name = entry.split(":")
            else:
                module_path = entry
                class_name = "Plugin"

            file_path = package_dir / f"{module_path}.py"
            if not file_path.exists():
                return None

            # FIX: Add project root to sys.path so that absolute imports in plugins
            # (e.g., from core.plugins import ...) work correctly
            # package_dir = /project/OpenLAD/industries/sample_semiconductor
            # parent.parent = /project/OpenLAD, parent.parent.parent = /project
            project_root = package_dir.parent.parent.parent
            if str(project_root) not in sys.path:
                sys.path.insert(0, str(project_root))

            spec = importlib.util.spec_from_file_location(module_path, file_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            plugin_class = getattr(module, class_name, None)
            if plugin_class:
                # FIX: Plugin may inherit from core.plugins.IndustryPlugin or
                # core.plugins.IndustryPlugin — both from same file but different
                # module name; need to check both
                is_sub = issubclass(plugin_class, IndustryPlugin)
                if not is_sub:
                    try:
                        import core.plugins as _core_plugins
                        is_sub = issubclass(plugin_class, getattr(_core_plugins, "IndustryPlugin"))
                    except Exception:
                        pass
                if is_sub:
                    return plugin_class()
        except Exception as e:
            logger.warning(f"[PLUGIN_REGISTRY] Failed to load Python plugin {manifest.id}: {e}")
            import traceback
            logger.warning(traceback.format_exc())
        return None

    def _register(self, plugin: IndustryPlugin):
        self._plugins[plugin.manifest.id] = plugin
        for cat in plugin.manifest.category_mapping:
            self._category_map[cat] = plugin.manifest.id
        logger.info(f"[PLUGIN_REGISTRY] Registered industry package: {plugin.manifest.id} v{plugin.manifest.version} -> {plugin.manifest.category_mapping}")

    def get_plugin(self, plugin_id: str) -> IndustryPlugin | None:
        return self._plugins.get(plugin_id)

    def get_plugin_by_category(self, category: str) -> IndustryPlugin | None:
        if not category:
            return self.get_generic()
        if category in self._category_map:
            plugin_id = self._category_map[category]
            return self._plugins.get(plugin_id)
        # Fuzzy match
        for cat_key, plugin_id in self._category_map.items():
            if cat_key in category or category in cat_key:
                return self._plugins.get(plugin_id)
        return self.get_generic()

    def detect_plugin_for_document(self, parsed_doc: Any) -> IndustryPlugin | None:
        """Auto-detect an industry plugin by inspecting the parsed document.

        Iterates registered plugins and calls their ingestion.detect_document_subtype()
        hook. Returns the first plugin that claims the document, or None to fall back
        to the generic flow. This keeps core free of industry-specific rules.
        """
        for plugin in self._plugins.values():
            ingestion = getattr(plugin, "ingestion", None)
            if not ingestion or not hasattr(ingestion, "detect_document_subtype"):
                continue
            try:
                subtype = ingestion.detect_document_subtype(parsed_doc)
                if subtype:
                    logger.info(
                        f"[PLUGIN_REGISTRY] Auto-detected industry plugin "
                        f"'{plugin.manifest.id}' for document subtype '{subtype}'"
                    )
                    return plugin
            except Exception as e:
                logger.warning(
                    f"[PLUGIN_REGISTRY] detect_document_subtype failed for "
                    f"{plugin.manifest.id}: {e}"
                )
        return None

    def get_generic(self) -> IndustryPlugin | None:
        """Get generic fallback plugin"""
        for pid, plugin in self._plugins.items():
            if pid == "generic" or "通用" in plugin.manifest.name:
                return plugin
        # If no generic, return first or None
        return next(iter(self._plugins.values()), None)

    def list_plugins(self) -> dict[str, dict[str, Any]]:
        return {
            pid: {
                "name": p.manifest.name,
                "version": p.manifest.version,
                "description": p.manifest.description,
                "categories": p.manifest.category_mapping,
            }
            for pid, p in self._plugins.items()
        }

    def reload(self):
        self._plugins.clear()
        self._category_map.clear()
        self._load_all()


# Global registry singleton
_registry: PluginRegistry | None = None


def get_plugin_registry(scan_dirs: list[str] = None) -> PluginRegistry:
    global _registry
    if _registry is None:
        from ..config import settings
        dirs = scan_dirs or settings.PLUGIN_CONFIG.get("industries_scan_dirs", [])
        _registry = PluginRegistry(scan_dirs=dirs)
    return _registry
