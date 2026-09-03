"""
OpenLAD System Configuration
Core configuration management, fully standalone, uses OPENLAD_ prefix for environment variables
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
_env_data_dir = os.environ.get("OPENLAD_DATA_DIR")
DATA_DIR = Path(_env_data_dir) if _env_data_dir else BASE_DIR / "data"

# Global system data
SYSTEM_DB_PATH = DATA_DIR / "system.db"
TENANTS_DIR = DATA_DIR / "tenants"

# Common subdirectories
LOG_DIR = BASE_DIR / "logs"
UPLOAD_DIR = DATA_DIR / "uploads"

# Industry pack directory
INDUSTRIES_DIR = BASE_DIR / "industries"

# Frontend static assets
STATIC_DIR = BASE_DIR / "frontend" / "web" / "dist"
IMAGES_DIR = DATA_DIR / "images"


def ensure_dirs():
    for d in [LOG_DIR, DATA_DIR, TENANTS_DIR, UPLOAD_DIR, STATIC_DIR, IMAGES_DIR]:
        d.mkdir(exist_ok=True, parents=True)


# =============================================================================
# Model Service Endpoints
# =============================================================================
LLM_BASE_URL = os.environ.get("OPENLAD_LLM_URL", "http://localhost:8080/v1")
LLM_MODEL_NAME = os.environ.get("OPENLAD_LLM_MODEL", "")  # User must configure, no default
EMBEDDING_API_BASE = os.environ.get("OPENLAD_EMB_URL", "http://localhost:8081/v1")
EMBEDDING_MODEL_NAME = os.environ.get("OPENLAD_EMB_MODEL", "")  # User must configure, no default
CHART_VLM_BASE_URL = os.environ.get("OPENLAD_CHART_VLM_URL", LLM_BASE_URL)
CHART_VLM_MODEL_NAME = os.environ.get("OPENLAD_CHART_VLM_MODEL", LLM_MODEL_NAME)

# =============================================================================
# API Service Configuration
# =============================================================================
# Supports both naming conventions: OPENLAD_API_HOST/PORT (canonical) and OPENLAD_HOST/PORT (start.sh legacy)
API_HOST = os.environ.get("OPENLAD_API_HOST") or os.environ.get("OPENLAD_HOST", "0.0.0.0")
API_PORT = int(os.environ.get("OPENLAD_API_PORT") or os.environ.get("OPENLAD_PORT", "11296"))

# CORS Configuration: Default to localhost only for security. Set OPENLAD_CORS_ORIGINS env var for production.
_cors_raw = os.environ.get("OPENLAD_CORS_ORIGINS", "http://localhost:11296,http://127.0.0.1:11296")
if _cors_raw == "*":
    CORS_ORIGINS = ["*"]
else:
    CORS_ORIGINS = [o.strip() for o in _cors_raw.split(",") if o.strip()]

# =============================================================================
# Query Concurrency Policy
# =============================================================================
# External mode: concurrency controlled by env vars, no hardware probing
# mode: auto | serial (force serial) | parallel (allow concurrent)
QUERY_CONCURRENCY_MODE = os.environ.get("OPENLAD_QUERY_CONCURRENCY_MODE", "auto")
# max_concurrent: 0=use default policy, >0=force explicit
QUERY_MAX_CONCURRENT = int(os.environ.get("OPENLAD_QUERY_MAX_CONCURRENT", "0"))
# LLM concurrent slots (for OpenAI API concurrency control)
LLM_MAX_CONCURRENT = int(os.environ.get("OPENLAD_LLM_NP", "1"))

# =============================================================================
# Ingestion Concurrency Configuration
# =============================================================================
# These control how many pages / chart regions are processed concurrently during
# document ingestion.  Set them to match your LLM server's --parallel value
# (the number of simultaneous requests the LLM can handle).
#
# Default 1 is safe for all hardware.  Increase if your LLM has more slots.
# e.g. llama-server --parallel 2 → set both to 2
INGEST_MAX_WORKERS = int(os.environ.get("OPENLAD_INGEST_MAX_WORKERS", "1"))
CHART_VLM_MAX_WORKERS = int(os.environ.get("OPENLAD_CHART_VLM_MAX_WORKERS", "1"))
# Rebuild labeled ruled-grid diagrams (ball maps, register maps) as clean
# tables during ingestion (deterministic, model-free). 1=on, 0=off.
GRID_RECONSTRUCTION_ENABLED = int(os.environ.get("OPENLAD_GRID_RECONSTRUCTION_ENABLED", "1"))
# Harvest per-section identifier inventory (e.g. UART0-UART9) into the
# structure index so chapter selection can match instance-level queries.
SECTION_ENTITY_HARVEST_ENABLED = int(os.environ.get("OPENLAD_SECTION_ENTITY_HARVEST_ENABLED", "1"))

# =============================================================================
# Context Window Configuration
# =============================================================================
CONTEXT_CONFIG = {
    "llm_max_tokens": 65536,  # Match actual llama-server --ctx-size deployment
    "deep_explore_chars": 60000,
    "standard_chars": 60000,
    "search_only_chars": 70000,
    "decomposed_sub_chars": 30000,
    "compare_docs_chars": 50000,
    "token_to_char_ratio": 0.7,
    "phase1_max_chars": 60000,
    "phase1_coarse_topk": 100,
    "phase1_coarse_max_chars": 40000,
    "phase1_fine_max_chars": 35000,
    "phase2_max_chars": 60000,
    # Maximum context chars for exhaustive scan queries (e.g. "which crimes carry the death penalty?")
    # Must be large enough to hold the full document to avoid truncating the latter half and missing content
    "exhaustive_max_chars": 80000,
    "avg_page_chars": 1200,
    "min_pages_per_doc": 5,
    "max_pages_per_doc": 80,
    "filtered_steps_quota_ratio": 0.8,
    # ── Synthesis context budget (derived from llm_max_tokens) ──
    # Total safe prompt space (llm_max_tokens × token_to_char_ratio)
    "synthesis_safe_chars": 45875,  # 65536 × 0.7
    # Context available after reserving ~15% for system prompt + query + output.
    # FIX: 39000 + real prompt overhead (~7.2K) totalled ~46.2K and exceeded
    # synthesis_safe_chars, so every query hit the model-client tail truncation,
    # silently dropping the LAST part of the context (often the totals pages).
    # 35500 keeps total prompt under the safe limit without truncation.
    "synthesis_context_budget": 35500,  # 65536 × 0.7 × 0.85 - overhead margin
    # Map-Reduce trigger: context exceeding this goes through chunked extraction
    # DISABLED: Map-Reduce causes 3-4x latency increase with minimal quality gain.
    # Context is truncated instead, which preserves the most relevant leading content.
    "map_reduce_threshold": 999999,  # Effectively disabled
    # Map phase: per-chunk size (duplicate of the one below; kept disabled
    # together with Map-Reduce — the effective value is defined in the
    # context-extraction section)
    # Max Map chunks (caps LLM calls = caps latency)
    "map_reduce_max_chunks": 5,
    # Reduce phase: input limit per Reduce call
    "map_reduce_reduce_limit": 15000,  # 65536 × 0.7 × 0.33
    # ── Retrieval executor hardcoded values (moved from executor.py) ──
    # Structured extraction: sampling size for uniform sampling (start/middle/end)
    "extraction_sample_size": 12000,
    # Structured extraction: LLM max_tokens for data extraction
    "extraction_max_tokens": 4096,
    # Chapter retrieval: max chapters LLM can select
    "chapter_select_max": 20,
    # Chapter retrieval: LLM max_tokens for chapter selection
    "chapter_select_max_tokens": 1024,
    # Chapter retrieval: pin pages containing selective exact query-term hits
    # above the character-budget cut, so blind tail truncation can never drop
    # the only page that literally mentions the queried parameter.
    # Set False to roll back to pure document-order truncation.
    "chapter_exact_match_pinning": True,
    # Single-step query quota cap (for 128K context models)
    "single_step_quota_max": 80000,
    # Multi-step query total quota cap
    "multi_step_quota_max": 80000,
    # Max results per document for single-document queries
    "max_results_per_doc": 15,
    # Max results multiplier for multi-document queries (per_doc = max_results_per_doc × num_docs)
    "max_results_multi_doc_multiplier": 1,
    # Decomposed sub-query merge context cap (per sub-query)
    "decomposed_sub_merge_cap": 30000,
    # Extraction sampling: size of each fragment (start/middle/end/tail)
    "extraction_fragment_size": 3000,
    # Extraction fallback truncation limit
    "extraction_fallback_limit": 3000,
    # Minimum step quota (for multi-step queries)
    "min_step_quota": 5000,
    # Minimum single-step quota (for single-step queries with large quota)
    "min_single_step_quota": 20000,
    # Chapter retrieve fallback quota (when max_context_quota not provided)
    "chapter_fallback_quota": 32000,
    # Document list limit for doc_filter resolution
    "doc_filter_list_limit": 10000,
    # ── Synthesizer hardcoded values (moved from synthesizer.py) ──
    # Standard synthesis: LLM max_tokens
    "synthesis_max_tokens": 4096,
    # Context extraction: max_chars for _extract_relevant_context
    "context_extract_max_chars": 10000,
    # Context extraction: keyword sample size
    "context_extract_keyword_sample": 3000,
    # Context extraction: deduplication window
    "context_extract_dedup_window": 1000,
    # Context extraction: start/end fragment size
    "context_extract_fragment_size": 3000,
    # Self-check context sampling: max evidence-anchor keywords per answer
    "context_extract_max_keywords": 20,
    # Map-Reduce: chunk size for splitting context
    "map_reduce_chunk_size": 8000,
    # Direct generation: max_tokens for simple queries
    "direct_generate_max_tokens": 4096,
    # ── Retriever / Merger hardcoded values (moved from retriever.py) ──
    # Merger: default max_context_chars
    "merger_default_max_chars": 32000,
    # Merger: minimum chars per document
    "merger_min_chars_per_doc": 5000,
    # Merger: minimum score threshold for results
    "merger_min_score_threshold": 0.15,
    # Merger: low-value page score penalty
    "merger_low_value_penalty": 3.0,
    # Merger: content length cap by score tier (top, high, medium, low, floor)
    "merger_content_cap_top": 16000,
    "merger_content_cap_high": 10000,
    "merger_content_cap_medium": 6000,
    "merger_content_cap_low": 3000,
    "merger_content_cap_floor": 3000,
    # FIX: A single high-score page (e.g. a 48K-char pin list) must not be allowed
    # to monopolize the per-document quota and starve every other relevant page.
    # Single-page content cap = max(floor, per_doc_quota * fraction). Applied on
    # top of the score-tier cap above (min of the two).
    "merger_single_page_cap_fraction": 0.25,
    # Merger: max source content returned to client
    "merger_max_source_content": 8000,
    # ── Retriever: structure-index chapter filter widening (generic) ──
    # Top-K chapters selected by IDF-weighted keyword scoring, unioned into the
    # chapter page filter. Only ever widens the filter, never narrows it.
    "structure_chapter_weighted_topk": 5,
    # Retriever: rare-token page rescue (generic). Exact identifiers (pin names,
    # register names, part numbers, codes) that are rare in the structure index
    # rescue the pages containing them verbatim from chapter-filter exclusion.
    "rare_token_rescue_enabled": True,
    # A query keyword qualifies as "rare" when it matches at most this many
    # structure-index sections (0 = no section mentions it at all).
    "rare_token_max_structure_df": 2,
    # Max rare keywords to rescue per document (bounds extra page scans).
    "rare_token_max_tokens": 5,
    # A rare keyword matching more pages than this is non-discriminating and skipped.
    # Excerpt-only delivery makes rescued pages cheap, so this can be generous.
    "rare_token_max_pages": 16,
    # Hard cap on total rescued pages per retrieval, ranked by token selectivity.
    # Prevents rescued pages from flooding the merger's context budget.
    "rare_token_max_rescued_pages": 12,
    # Score assigned to pages appended/boosted by exact rare-token matches. Keeps
    # them at the top content-cap tier and ahead of merger budget pressure.
    "rare_token_rescue_score": 45.0,
    # ── Hybrid vector recall v2 (conservative) ──
    # Historical constraint: early experiments showed raw vector recall was LESS
    # accurate than FTS for exact-lookup, so vector was demoted to a fallback-only
    # path. This block must NOT replace or delete any FTS result. It only:
    #   A. boosts pages already recalled by FTS/structure when the vector signal
    #      independently confirms them (rescues low-frequency feature sentences
    #      like "Support ten UART interfaces" from ranking loss), and
    #   B. appends strongly-matching pages (strict threshold, capped per doc) that
    #      FTS missed, with a low base score so they never displace FTS leaders.
    "hybrid_vector_enabled": True,        # master switch; False = pure FTS (current behavior)
    "hybrid_vector_min_score": 0.45,      # similarity threshold (stricter than fallback 0.3)
    "hybrid_vector_per_doc": 4,           # max gap-filled pages per doc
    "hybrid_vector_boost_scale": 25.0,    # confirmed-page boost = vec_score * scale
    "hybrid_vector_supplement_base": 5.0, # base score for gap-filled pages (tail, low)
    # Exact-match excerpt: chars of context window around each keyword hit, and
    # max windows per rescued page. The excerpt is prepended to the page content
    # so critical rows survive truncation and stay salient to the LLM.
    "exact_match_window_chars": 1500,
    "exact_match_max_windows": 4,
    # ── Low-value section indicators (generic document metadata pages) ──
    # These are cross-language generic patterns for cover/TOC/copyright pages
    # Industry-specific low-value sections should be defined in industry package rules.yaml
    "low_value_section_indicators": [
        "copyright", "revision history", "table of content",
        "figure index", "table index", "warranty disclaimer",
        "acknowledgment", "preface", "foreword",
        "page visual analysis",
    ],
    # FIX: VLM-generated page descriptions are AI hallucination risk (e.g. a VLM
    # miscounted FCBGA636L as 560 solder balls on a package drawing). Penalize
    # these pages heavily so authoritative text pages (Features) outrank them.
    # 0 disables the penalty.
    "vlm_page_penalty": 15.0,
    # ── Spec facts bypass: assertion-level (entity, attribute, value) index ──
    # Built from authoritative page text (never VLM descriptions). Spec queries
    # ("X 的 Y 是多少") look up the index first; hits are prepended to the
    # context as authoritative evidence, bypassing page/chapter-granularity
    # retrieval weaknesses (the abstraction layer the vector-hybrid / VLM-
    # penalty / chapter-scope patches were compensating for).
    "spec_facts_enabled": True,
    "spec_facts_max_inject": 12,          # max facts prepended to context
    "spec_facts_min_hits": 2,             # min keyword hits for a fact to qualify
    "spec_facts_entity_restriction": True,  # scope injected facts to query-named entities
    # Selectivity guard for keyword matching: a keyword whose hits span more
    # distinct attributes than the threshold has no discriminating power —
    # generic verbs (e.g. "support") appear in the source line of nearly
    # every "Support X" fact and would otherwise qualify all of them,
    # flooding the injected block with irrelevant authoritative-looking rows.
    # Measured against the document's own fact table; entity-vocabulary
    # tokens are exempt (entity restriction handles them). No wordlists.
    "spec_facts_selectivity_guard": os.environ.get(
        "OPENLAD_SPEC_FACTS_SELECTIVITY_GUARD", "1") == "1",
    "spec_facts_selectivity_max_attrs": int(os.environ.get(
        "OPENLAD_SPEC_FACTS_SELECTIVITY_MAX_ATTRS", "3")),
    # Presentation of injected facts. "source_first": the verbatim source
    # sentence leads (the flattened value stays internal for matching only) —
    # an enumeration like "PCIe3.1(8Gbps), PCIe2.1" invites literal-list
    # readings by small models ("3.0 is not in the list -> unsupported").
    # "value_first": legacy attribute:value rendering. One-switch rollback.
    # Env-overridable for ops rollback without a code edit.
    "spec_facts_presentation": os.environ.get(
        "OPENLAD_SPEC_FACTS_PRESENTATION", "source_first"),
    # Evidence appendix: after the LLM answer, mechanically append the verbatim
    # source sentences behind the injected facts (never model-written). The
    # reader gets an auditable original even when the narrative layer slips.
    "spec_facts_evidence_appendix": os.environ.get(
        "OPENLAD_SPEC_FACTS_APPENDIX", "1") == "1",
    "spec_facts_evidence_max": 5,             # max excerpt lines appended
    "rewrite_collapse_guard": True,         # planner rewrite dropping a query entity -> frame synthesis with the original query
    # Chinese query-term -> English keyword expansion for spec-fact lookup.
    # This is a QUERY-UNDERSTANDING layer (synonym expansion), extensible via
    # config; it never hardcodes any answer.
    # LAYERING RULE: core keeps ONLY domain-neutral terms (quantity/version
    # measure words). Industry vocabulary (gpu/uart/h.264/算力/主频/…) lives in
    # industry packs (retrieval/rules.yaml -> spec_query_terms) and is merged in
    # by the engine at query time via RetrievalPlugin.get_spec_query_terms().
    "spec_query_terms": {
        "数量": ["count", "number"],
        "多少个": ["count", "number"],
        "版本": ["version", "protocol", "support"],
    },
    # ── Page type detection keywords (for layout analyzer) ──
    # Generic document structure keywords used to detect cover/TOC pages
    # These are cross-language and not specific to any industry
    "page_type_detection": {
        "cover_indicators": ["cover", "目录"],
        "toc_indicators": ["contents", "目录", "table of contents", "章节"],
    },
}

# Planner configuration
PLANNER_CONFIG = {
    "coarse_topk": 100,
    "doc_filter_cap": 30,
    "title_display_max": 50,
    "filename_display_max": 40,
    "display_string_max": 80,
    "estimated_chars_default": 30000,
    "estimated_chars_prompt": 50000,
    "pronoun_query_length_threshold": 15,
    "short_query_length_threshold": 10,
}

# Router configuration
ROUTER_CONFIG = {
    "deep_explore_multiplier": 15,
    "deep_explore_max_results": 500,
}

# Agentic retriever configuration
AGENTIC_CONFIG = {
    "catalog_chapter_limit": 40,
    "catalog_priority_chapters_max": 20,
    "catalog_other_chapters_max": 10,
    "catalog_total_chapters_max": 30,
    "expand_keywords_max": 5,
    "expand_chapters_max": 20,
    "fts_keyword_word_limit": 3,
    "fts_min_keyword_length": 2,
    "fts_max_keywords": 10,
    "fts_threshold": 5.0,
    "fts_min_results": 2,
    "vector_score_scale": 10.0,
    "verify_page_content_limit": 1500,
    "verify_max_pages": 2,
    "extract_page_content_limit": 2000,
    "query_page_content_limit": 2000,
    "candidate_pages_max": 10,
    "answer_min_length": 100,
    "catalog_json_max": 4000,
    "summary_max_tokens": 4000,
    # Key terms for catalog chapter prioritization (generic English terms only; no language-specific hardcoding)
    "catalog_key_terms": ['feature', 'overview', 'specification', 'introduction'],
    # Keywords for comparison detection (language-agnostic)
    "compare_keywords": ['vs', 'versus', 'compare', 'comparison', 'diff', 'difference'],
}

# =============================================================================
# Embedding Configuration
# All chunk/embedding size limits derive from the embedding model's context window.
# =============================================================================
_emb_ctx = int(os.environ.get("OPENLAD_EMB_CTX_SIZE", "8192"))
_emb_char_ratio = float(os.environ.get("OPENLAD_EMB_TOKEN_CHAR_RATIO", "1.5"))
_emb_safety = float(os.environ.get("OPENLAD_EMB_SAFETY_RATIO", "0.7"))
# llama-server physical batch: max tokens a single input may contain.
# Must match the deployed llama-server --batch-size — a single input larger
# than this is rejected outright ("input (N tokens) is too large to process")
# and the chunk is silently skipped. Default 2048 matches llama.cpp's own
# default --batch-size, so an undeclared deployment behaves exactly as before.
_emb_max_input_tokens = int(os.environ.get("OPENLAD_EMB_MAX_INPUT_TOKENS", "2048"))

EMBEDDING_CONFIG = {
    # Embedding model context window (must match llama-server -c value)
    "ctx_size": _emb_ctx,
    # Conservative token→char ratio (1.5 for mixed CN/EN, 4.0 for pure EN)
    "token_to_char_ratio": _emb_char_ratio,
    # Safety margin: only use this fraction of the context window
    "safety_ratio": _emb_safety,
    # Hard cap: any single chunk exceeding this will be split (derived).
    # Bound by both the context window and the physical batch: whichever is
    # tighter wins, so chunks stay embeddable on small-batch deployments.
    "max_chunk_chars": int(min(
        _emb_ctx * _emb_char_ratio * _emb_safety,
        _emb_max_input_tokens * _emb_char_ratio * _emb_safety,
    )),
    # Target chunk size for retrieval granularity (smaller = more precise)
    "chunk_size": int(os.environ.get("OPENLAD_EMB_CHUNK_SIZE", "1600")),
    # Per-document chunk count safety cap
    "max_chunks_per_doc": int(os.environ.get("OPENLAD_EMB_MAX_CHUNKS_PER_DOC", "5000")),
    # Batch size for embedding API calls (more = faster but more memory on server)
    "batch_size": int(os.environ.get("OPENLAD_EMB_BATCH_SIZE", "8")),
    # Single-embed / batch truncation limit, likewise bounded by the
    # physical batch (chars = tokens × ratio × safety).
    "max_embed_chars": int(min(
        _emb_ctx * _emb_char_ratio * _emb_safety * 0.8,
        _emb_max_input_tokens * _emb_char_ratio * _emb_safety,
    )),
    # Declared physical batch (tokens per single input) — for logs/asserts
    "max_input_tokens": _emb_max_input_tokens,
}

# =============================================================================
# OCR Configuration
# =============================================================================
OCR_CONFIG = {
    "engine": os.environ.get("OPENLAD_OCR_ENGINE", "auto"),
    "language": "zh_en",
    "min_confidence": 0.6,
    "enable_deskew": True,
    "enable_dewarp": False,
    "enable_denoise": True,
    "fallback_engine": "vlm",
}

# =============================================================================
# Layout Analysis Configuration
# =============================================================================
LAYOUT_CONFIG = {
    "model": "pp_doclayout_v3",
    "detect_columns": True,
    "restore_reading_order": True,
    "element_types": [
        "text", "title", "section-header", "caption", "footnote",
        "page-header", "page-footer", "picture", "table", "formula", "list-item"
    ],
}

# =============================================================================
# Formula Recognition Configuration
# =============================================================================
FORMULA_CONFIG = {
    "model": "pix2tex",
    "output_format": "latex",
    "keep_image": True,
    "min_confidence": 0.7,
}

# =============================================================================
# Chart Analysis Configuration
# =============================================================================
CHART_CONFIG = {
    "enabled": True,
    "min_region_area": 15000,
    "max_regions_per_page": 4,
    "min_region_wh": 120,
    "text_dilate_size": 25,
    "margin_ignore": 15,
    "vlm_max_tokens": 1024,
    "vlm_temperature": 0.2,
    "max_image_size": 1024,
    "append_to_raw_text": True,
    # Page classification / image description guards
    "vlm_blank_image_threshold": 0.005,   # ratio of non-white pixels; below this is blank
    "vlm_min_text_len_for_candidate": 1000,  # pages with more text are not VLM candidates
    "vlm_image_description_enabled": True,
    "vlm_image_description_max_tokens": 1024,
    "vlm_image_description_temperature": 0.2,
    # Cost guard for deep VLM analysis of CHART pages.
    # 0 = unlimited (analyze every classified CHART page). Set >0 to cap cost on large schematics.
    "vlm_max_chart_pages_per_doc": int(os.environ.get("OPENLAD_VLM_MAX_CHART_PAGES", "0")),
    # Dedicated OCR endpoint transcription (used when model_config ocr_url is
    # set): visual candidate pages are transcribed directly instead of the
    # VLM classify -> CHART/IMAGE split.
    # Max tokens for OCR page transcription. OvisOCR2 (0.8B) official
    # inference uses max_tokens=16384; short budgets force the model into
    # over-generation once the real content is exhausted, so keep parity
    # with the reference config (llama-server ctx must be >= this value).
    "ocr_transcription_max_tokens": 16384,
    "ocr_transcription_temperature": 0.0,
}

# =============================================================================
# Text Quality Configuration
# =============================================================================
TEXT_QUALITY_CONFIG = {
    "garbled_threshold": 0.05,
    "min_dictionary_hit_rate": 0.3,
    "enable_ocr_fallback": True,
}

# =============================================================================
# Rate Limiting & Agent Configuration
# =============================================================================
RATE_LIMIT_CONFIG = {
    "query_per_minute": 30,
    "upload_per_minute": 10,
    "max_query_length": 2000,
    "agent_max_steps": 8,
    "agent_max_replan": 3,
    "agent_step_timeout_seconds": 120,
}

# Login rate limit (dual-track: per-username + per-IP). Throttle only, no lockout.
# Lenient defaults for internal-network deployment; overridable via env vars.
LOGIN_RATE_LIMIT = {
    "username_per_minute": int(os.environ.get("OPENLAD_LOGIN_USER_PER_MIN", "5")),
    "ip_per_minute": int(os.environ.get("OPENLAD_LOGIN_IP_PER_MIN", "20")),
}

# API Key lifecycle. Default TTL for newly created users (days).
# 0 / negative = never expires. Preset options surfaced in the admin UI.
API_KEY_CONFIG = {
    "default_ttl_days": int(os.environ.get("OPENLAD_API_KEY_TTL_DAYS", "90")),
    "ttl_presets_days": [90, 180, 365],  # 3 months / half year / 1 year
}

# =============================================================================
# Multi-Tenant Configuration
# =============================================================================
TENANT_CONFIG = {
    "default_storage_quota_mb": int(os.environ.get("OPENLAD_DEFAULT_QUOTA_MB", "10240")),
    "max_tenants": int(os.environ.get("OPENLAD_MAX_TENANTS", "100")),
    "enable_api_key_auth": True,
    "enable_password_auth": True,
}

# =============================================================================
# Plugin Configuration
# =============================================================================
PLUGIN_CONFIG = {
    "industries_scan_dirs": [
        str(INDUSTRIES_DIR),
        # Extra closed-source industry pack dirs (private packs live outside
        # this repo; never committed here). Colon-separated, e.g.
        # OPENLAD_INDUSTRIES_DIRS=/srv/packs:/srv/more
        *[d for d in os.environ.get("OPENLAD_INDUSTRIES_DIRS", "").split(os.pathsep) if d],
    ],
    "enable_hot_reload": os.environ.get("OPENLAD_HOT_RELOAD", "false").lower() == "true",
    "hot_reload_interval_seconds": 30,
}

ensure_dirs()


class Settings:
    """Unified configuration access point"""
    BASE_DIR = BASE_DIR
    DATA_DIR = DATA_DIR
    SYSTEM_DB_PATH = SYSTEM_DB_PATH
    TENANTS_DIR = TENANTS_DIR
    LOG_DIR = LOG_DIR
    UPLOAD_DIR = UPLOAD_DIR
    INDUSTRIES_DIR = INDUSTRIES_DIR
    STATIC_DIR = STATIC_DIR
    IMAGES_DIR = IMAGES_DIR

    LLM_BASE_URL = LLM_BASE_URL
    LLM_MODEL_NAME = LLM_MODEL_NAME
    EMBEDDING_API_BASE = EMBEDDING_API_BASE
    EMBEDDING_MODEL_NAME = EMBEDDING_MODEL_NAME
    CHART_VLM_BASE_URL = CHART_VLM_BASE_URL
    CHART_VLM_MODEL_NAME = CHART_VLM_MODEL_NAME

    API_HOST = API_HOST
    API_PORT = API_PORT
    CORS_ORIGINS = CORS_ORIGINS

    OCR_CONFIG = OCR_CONFIG
    LAYOUT_CONFIG = LAYOUT_CONFIG
    FORMULA_CONFIG = FORMULA_CONFIG
    CHART_CONFIG = CHART_CONFIG
    TEXT_QUALITY_CONFIG = TEXT_QUALITY_CONFIG
    CONTEXT_CONFIG = CONTEXT_CONFIG
    EMBEDDING_CONFIG = EMBEDDING_CONFIG
    RATE_LIMIT_CONFIG = RATE_LIMIT_CONFIG
    LOGIN_RATE_LIMIT = LOGIN_RATE_LIMIT
    API_KEY_CONFIG = API_KEY_CONFIG
    TENANT_CONFIG = TENANT_CONFIG
    PLUGIN_CONFIG = PLUGIN_CONFIG
    PLANNER_CONFIG = PLANNER_CONFIG
    ROUTER_CONFIG = ROUTER_CONFIG
    AGENTIC_CONFIG = AGENTIC_CONFIG

    # Query concurrency policy config
    LLM_MAX_CONCURRENT = LLM_MAX_CONCURRENT
    QUERY_CONCURRENCY_MODE = QUERY_CONCURRENCY_MODE
    QUERY_MAX_CONCURRENT = QUERY_MAX_CONCURRENT

    @staticmethod
    def ensure_dirs():
        ensure_dirs()

    @staticmethod
    def get_tenant_data_dir(tenant_id: str) -> Path:
        return DATA_DIR / "tenants" / tenant_id

    @staticmethod
    def get_tenant_db_path(tenant_id: str) -> Path:
        return DATA_DIR / "tenants" / tenant_id / "metadata.db"

    @staticmethod
    def get_tenant_vec_db_path(tenant_id: str) -> Path:
        return DATA_DIR / "tenants" / tenant_id / "vectors.vec.db"

    @staticmethod
    def get_tenant_documents_dir(tenant_id: str) -> Path:
        return DATA_DIR / "tenants" / tenant_id / "documents"

    @staticmethod
    def get_tenant_images_dir(tenant_id: str) -> Path:
        return DATA_DIR / "tenants" / tenant_id / "images"


settings = Settings()
