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

    def get_entity_stopwords(self) -> list[str]:
        """Vocabulary too generic in this industry's documents to identify
        a document in a query (e.g. "营业收入" in annual reports). The
        planner's entity-coverage check filters these out of extracted
        query entities so they cannot force-merge unrelated documents into
        the doc_filter. Core keeps its own domain-neutral question/meta
        words; these pack words are additive. Default: []."""
        return []

    def get_spec_extraction_config(self) -> dict[str, Any]:
        """Industry vocabulary for the rule-based spec-fact extractor:
          {spec_headers: [..], compute_units: [..], compute_attribute: str,
           frequency_terms: [..]}
        Core keeps only the extraction MECHANISMS; all word lists live in
        the pack. Missing/empty lists disable the corresponding pattern."""
        return {}

    def get_evidence_anchor_patterns(self) -> list[str]:
        """Regex patterns (via re.findall) that extract domain-specific
        evidence-anchor keywords from an answer for self-check context
        sampling (e.g. a legal pack might anchor on article numbers).
        Core always applies its generic anchors (numbers, model tokens,
        long phrases); these patterns are additive. Default: none."""
        return []

    def get_section_boost_rules(self) -> dict[str, Any]:
        """Section boost rules (query-intent -> boost/penalty), default {}."""
        return {}

    def get_retrieval_rules(self) -> dict[str, Any]:
        """Complete retrieval rules (query_expansion, low_value_sections,
        spec_sections, etc.), default {}."""
        return {}

    def get_query_expansion_keywords(self) -> list[str]:
        """Query expansion keywords (decomposed_retrieve sub-query
        enhancement), default []."""
        return []

    def get_low_value_sections(self) -> list[dict[str, Any]]:
        """Low-value section penalty rules, default []."""
        return []

    def get_spec_sections(self) -> list[dict[str, Any]]:
        """Spec-related section boost rules, default []."""
        return []

    def get_specificity_vocabulary(self) -> dict[str, list[str]]:
        """Domain vocabulary (units/terms regex fragments) for the
        confidence heuristic's specificity signal, default {}."""
        return {}

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

    @property
    def taxonomy(self) -> dict[str, Any]:
        """Document-classification taxonomy (optional; empty when not declared).

        Industry packages may ship a shared/taxonomy.yaml with a three-level
        document classification tree. It feeds the classifier prompt when the
        package is present. Defaults to empty so Python plugins that do not
        declare a taxonomy are unaffected.
        """
        return {}


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

    def get_entity_stopwords(self) -> list[str]:
        # Query-side entity stopwords from rules.yaml `entity_stopwords`
        # (default empty): vocabulary too generic in this industry's
        # documents to identify a document (e.g. "营业收入" in annual
        # reports). Merged into the planner's entity filter.
        return self.config.rules.get("entity_stopwords", []) or []

    def get_spec_extraction_config(self) -> dict[str, Any]:
        # Extractor vocabulary from rules.yaml `spec_extraction` (default
        # empty -> core runs structural patterns only).
        return self.config.rules.get("spec_extraction", {}) or {}

    def get_evidence_anchor_patterns(self) -> list[str]:
        # Domain-specific evidence-anchor regexes from rules.yaml
        # `evidence_anchor_patterns` (default empty).
        return self.config.rules.get("evidence_anchor_patterns", []) or []

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

    def get_specificity_vocabulary(self) -> dict[str, list[str]]:
        """Specificity vocabulary from rules.yaml `specificity_vocabulary`
        (default empty -> confidence heuristic keeps structural signals only)."""
        return self.config.rules.get("specificity_vocabulary", {}) or {}


class YAMLIndustryPlugin(IndustryPlugin):
    """Default industry package implementation based on YAML config"""

    def __init__(self, manifest: IndustryManifest,
                 ingestion_config: IngestionConfig,
                 retrieval_config: RetrievalConfig,
                 shared_config: SharedConfig):
        self._manifest = manifest
        self._ingestion = YAMLIngestionPlugin(ingestion_config, shared_config)
        self._retrieval = YAMLRetrievalPlugin(retrieval_config, shared_config)
        self._shared_config = shared_config

    @property
    def manifest(self) -> IndustryManifest:
        return self._manifest

    @property
    def taxonomy(self) -> dict[str, Any]:
        """Classification taxonomy loaded from shared/taxonomy.yaml (if any)."""
        return self._shared_config.taxonomy or {}

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
# Pack Composition: generic base layer + selected industry overlay
# =============================================================================

def _union_list(base: list, overlay: list) -> list:
    """Union two lists, base items first, preserving order."""
    out = list(base or [])
    for item in (overlay or []):
        if item not in out:
            out.append(item)
    return out


def _merge_hook_dicts(base: dict, overlay: dict) -> dict:
    """Merge hook config dicts: dicts recurse, lists union, scalars overlay-wins."""
    merged = dict(base or {})
    for key, value in (overlay or {}).items():
        if key not in merged:
            merged[key] = value
        elif isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _merge_hook_dicts(merged[key], value)
        elif isinstance(merged[key], list) and isinstance(value, list):
            merged[key] = _union_list(merged[key], value)
        else:
            merged[key] = value
    return merged


class ComposedRetrievalPlugin(RetrievalPlugin):
    """Retrieval hooks of (generic base + industry overlay) composed.

    Vocabulary/config hooks merge (lists union, overlay wins scalar/dict
    conflicts); behavioral hooks chain or delegate to the overlay. Core
    holds no vocabulary — both layers are pack data.
    """

    def __init__(self, base: RetrievalPlugin, overlay: RetrievalPlugin):
        self._base = base
        self._overlay = overlay

    # --- behavioral hooks: overlay decides / chains ---
    def rewrite_query(self, query: str, chat_history: list[dict] = None) -> str:
        return self._overlay.rewrite_query(query, chat_history)

    def format_citation(self, page_num: int, doc_title: str = "") -> str:
        return self._overlay.format_citation(page_num, doc_title)

    def post_process_answer(self, answer: str, context: str) -> str:
        return self._overlay.post_process_answer(
            self._base.post_process_answer(answer, context), context)

    def enhance_context(self, query: str, context: str,
                        sources: list[dict[str, Any]] = None) -> str:
        context = self._base.enhance_context(query, context, sources)
        return self._overlay.enhance_context(query, context, sources)

    def supplement_results(self, query: str, results: list[Any],
                           metadata_db: Any = None) -> list[Any]:
        return _union_list(
            self._base.supplement_results(query, results, metadata_db),
            self._overlay.supplement_results(query, results, metadata_db))

    # --- vocabulary/config hooks: merge ---
    def get_retrieval_prompts(self) -> dict[str, str]:
        return _merge_hook_dicts(self._base.get_retrieval_prompts(),
                                 self._overlay.get_retrieval_prompts())

    def get_answer_constraints(self) -> dict[str, Any]:
        return _merge_hook_dicts(self._base.get_answer_constraints(),
                                 self._overlay.get_answer_constraints())

    def disambiguate_terms(self, query: str) -> dict[str, str]:
        return _merge_hook_dicts(self._base.disambiguate_terms(query),
                                 self._overlay.disambiguate_terms(query))

    def get_synonyms(self, term: str) -> list[str]:
        return _union_list(self._base.get_synonyms(term),
                           self._overlay.get_synonyms(term))

    def get_spec_query_terms(self) -> dict[str, list[str]]:
        return _merge_hook_dicts(self._base.get_spec_query_terms(),
                                 self._overlay.get_spec_query_terms())

    def get_entity_patterns(self) -> list[str]:
        # Overlay patterns first: industry claims are more specific.
        return _union_list(self._overlay.get_entity_patterns(),
                           self._base.get_entity_patterns())

    def get_entity_stopwords(self) -> list[str]:
        return _union_list(self._base.get_entity_stopwords(),
                           self._overlay.get_entity_stopwords())

    def get_spec_extraction_config(self) -> dict[str, Any]:
        return _merge_hook_dicts(self._base.get_spec_extraction_config(),
                                 self._overlay.get_spec_extraction_config())

    def get_evidence_anchor_patterns(self) -> list[str]:
        return _union_list(self._base.get_evidence_anchor_patterns(),
                           self._overlay.get_evidence_anchor_patterns())

    def get_section_boost_rules(self) -> dict[str, Any]:
        return _merge_hook_dicts(self._base.get_section_boost_rules(),
                                 self._overlay.get_section_boost_rules())

    def get_retrieval_rules(self) -> dict[str, Any]:
        return _merge_hook_dicts(self._base.get_retrieval_rules(),
                                 self._overlay.get_retrieval_rules())

    def get_query_expansion_keywords(self) -> list[str]:
        return _union_list(self._base.get_query_expansion_keywords(),
                           self._overlay.get_query_expansion_keywords())

    def get_low_value_sections(self) -> list[dict[str, Any]]:
        return _union_list(self._base.get_low_value_sections(),
                           self._overlay.get_low_value_sections())

    def get_spec_sections(self) -> list[dict[str, Any]]:
        return _union_list(self._base.get_spec_sections(),
                           self._overlay.get_spec_sections())

    def get_specificity_vocabulary(self) -> dict[str, list[str]]:
        return _merge_hook_dicts(self._base.get_specificity_vocabulary(),
                                 self._overlay.get_specificity_vocabulary())


class ComposedIngestionPlugin(IngestionPlugin):
    """Ingestion hooks of (generic base + industry overlay) composed."""

    def __init__(self, base: IngestionPlugin, overlay: IngestionPlugin):
        self._base = base
        self._overlay = overlay

    def classify_document(self, content_sample: str, filename: str) -> dict[str, Any]:
        return self._overlay.classify_document(content_sample, filename) \
            or self._base.classify_document(content_sample, filename)

    def extract_entities(self, page_text: str, page_type: str) -> list[dict[str, Any]]:
        return _union_list(self._base.extract_entities(page_text, page_type),
                           self._overlay.extract_entities(page_text, page_type))

    def get_ingestion_prompts(self) -> dict[str, str]:
        return _merge_hook_dicts(self._base.get_ingestion_prompts(),
                                 self._overlay.get_ingestion_prompts())

    def get_parsing_rules(self) -> dict[str, Any]:
        return _merge_hook_dicts(self._base.get_parsing_rules(),
                                 self._overlay.get_parsing_rules())

    def enhance_summary(self, raw_summary: str, doc_metadata: dict) -> str:
        return self._overlay.enhance_summary(
            self._base.enhance_summary(raw_summary, doc_metadata), doc_metadata)

    def get_skill_config(self) -> dict[str, Any] | None:
        return self._overlay.get_skill_config() or self._base.get_skill_config()

    def detect_document_subtype(self, parsed_doc: Any) -> str | None:
        return self._overlay.detect_document_subtype(parsed_doc) \
            or self._base.detect_document_subtype(parsed_doc)

    def process_page(self, page: Any, raw_text: str, *args, **kwargs):
        self._base.process_page(page, raw_text, *args, **kwargs)
        return self._overlay.process_page(page, raw_text, *args, **kwargs)

    def on_document_complete(self, doc_id: str, metadata_db: Any) -> None:
        self._base.on_document_complete(doc_id, metadata_db)
        self._overlay.on_document_complete(doc_id, metadata_db)


class ComposedIndustryPlugin(IndustryPlugin):
    """An explicitly-selected industry pack layered over the generic base pack.

    Identity (manifest) is the overlay's; hooks are composed. Intentionally
    exposes no `name` attribute: the self-check gate keys off `name` and must
    stay disabled until its own fix lands.
    """

    def __init__(self, base: IndustryPlugin, overlay: IndustryPlugin):
        self._base = base
        self._overlay = overlay
        self._retrieval = ComposedRetrievalPlugin(base.retrieval, overlay.retrieval)
        self._ingestion = ComposedIngestionPlugin(base.ingestion, overlay.ingestion)

    @property
    def manifest(self) -> IndustryManifest:
        return self._overlay.manifest

    @property
    def taxonomy(self) -> dict[str, Any]:
        return self._overlay.taxonomy

    @property
    def ingestion(self) -> IngestionPlugin:
        return self._ingestion

    @property
    def retrieval(self) -> RetrievalPlugin:
        return self._retrieval

    def match_category(self, category: str) -> float:
        return self._overlay.match_category(category)


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

    def _plugin_match_keys(self, plugin: "IndustryPlugin") -> list[str]:
        """All strings a pack can be routed by: manifest.category_mapping
        PLUS its taxonomy names. The LLM classifier reads taxonomy.yaml and
        may emit its language (e.g. 数据手册) while category_mapping is
        English (Datasheets) — matching on mapping keys alone silently
        drops the pack for auto-ingested documents (spec-fact extraction
        then runs vocabulary-less). description is excluded on purpose."""
        keys: list[str] = [c for c in (plugin.manifest.category_mapping or []) if c]

        tax = getattr(plugin, "taxonomy", None) or {}

        def _collect(node: Any) -> None:
            if isinstance(node, str):
                if node and node not in keys:
                    keys.append(node)
            elif isinstance(node, dict):
                for k, v in node.items():
                    if k == "description":
                        continue
                    _collect(v)
            elif isinstance(node, list):
                for v in node:
                    _collect(v)

        _collect({"level1": tax.get("level1"), "level2": tax.get("level2")})
        return keys

    def get_plugin_by_category(self, category: str) -> IndustryPlugin | None:
        if not category:
            return self.get_generic()
        # Pass 1: exact, Pass 2: fuzzy substring — over the full key set
        # (category_mapping + taxonomy names, any language).
        for plugin in self._plugins.values():
            if category in self._plugin_match_keys(plugin):
                return plugin
        for plugin in self._plugins.values():
            for key in self._plugin_match_keys(plugin):
                if key in category or category in key:
                    return plugin
        return self.get_generic()

    def resolve_plugin_for_categories(self, categories: list) -> IndustryPlugin | None:
        """Resolve an industry plugin from classified document categories
        (tried in order — pass most specific first). Matching runs over each
        pack's full key set (category_mapping + taxonomy names, any
        language); exact hit wins over fuzzy substring. WITHOUT the generic
        fallback: ingestion vocabulary (spec-fact extraction) must only come
        from a pack that genuinely claims the document's category, never
        from a generic fallback that would inject industry vocabulary into
        unrelated documents."""
        for category in categories:
            if not category:
                continue
            for plugin in self._plugins.values():
                if category in self._plugin_match_keys(plugin):
                    return plugin
            for plugin in self._plugins.values():
                for key in self._plugin_match_keys(plugin):
                    if key in category or category in key:
                        return plugin
        return None

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
        """Get generic fallback plugin.

        Returns None when no genuine generic plugin is registered. Falling
        back to an arbitrary domain-specific pack would silently inject
        industry vocabulary/rules into unrelated queries.
        """
        for pid, plugin in self._plugins.items():
            if pid == "generic" or "通用" in plugin.manifest.name:
                return plugin
        return None

    def compose_with_base(self, plugin: IndustryPlugin | None) -> IndustryPlugin | None:
        """Layer the selected pack over the always-on generic base pack.

        - plugin None -> the generic base alone (universal knowledge still
          applies; returns None only when no generic pack is registered)
        - plugin is the generic base (or no base registered) -> plugin as-is
        - otherwise -> ComposedIndustryPlugin(base=generic, overlay=plugin)
        """
        base = self.get_generic()
        if plugin is None:
            return base
        if base is None or base is plugin or plugin.manifest.id == base.manifest.id:
            return plugin
        return ComposedIndustryPlugin(base, plugin)

    def detect_plugin_for_text(self, text: str) -> IndustryPlugin | None:
        """Detect which industry pack claims a piece of text (query or
        retrieved context) via the pack-declared entity patterns.

        Category routing is modal (dominant document category of the tenant),
        so it cannot scope industry behavior per query. Content detection is
        grounded: a pack applies only when its own entity patterns match the
        actual query/evidence text. Packs that declare no entity patterns
        never claim text and stay out of unrelated domains.
        """
        if not text:
            return None
        import re
        for plugin in self._plugins.values():
            try:
                retrieval = getattr(plugin, "retrieval", None)
                patterns = None
                if retrieval is not None and hasattr(retrieval, "get_entity_patterns"):
                    patterns = retrieval.get_entity_patterns()
                for pat in patterns or []:
                    if re.search(pat, text, re.IGNORECASE):
                        return plugin
            except Exception as e:
                logger.warning(
                    f"[PLUGIN_REGISTRY] detect_plugin_for_text failed for "
                    f"'{plugin.manifest.id}': {e}"
                )
        return None

    def list_plugins(self) -> dict[str, dict[str, Any]]:
        return {
            pid: {
                "name": p.manifest.name,
                "version": p.manifest.version,
                "description": p.manifest.description,
                "categories": p.manifest.category_mapping,
                "taxonomy": p.taxonomy,
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
