"""Assertion-level spec-fact lookup, shared by ALL retrieval paths.

The spec_facts index is populated at ingest time (spec_facts_extractor) with
verbatim-verified (entity, attribute, value) assertions. Every retrieval path
— traditional, agentic, decomposed — must consult it: page-level retrieval is
asymmetric across entities ("same attribute, two products"), which is exactly
what this assertion layer exists to fix.

Industry vocabulary (spec_query_terms) is provided by industry packs via the
plugin interface; core keeps only domain-neutral terms.
"""

import logging
import re

from ..config import settings

logger = logging.getLogger(__name__)


# Model-token pattern shared by every retrieval-path entity check (engine
# coverage check, decomposer entity hints, rewrite-collapse guard, and the
# entity scoping below). Boundary lookarounds instead of \b so UUID-prefixed
# or underscore-joined filenames still match; requires 2+ digits so plain
# words ("GPU", "UART") are never mistaken for model tokens.
_MODEL_TOKEN_RE = re.compile(r'(?<![A-Za-z0-9])[A-Z]{1,}\d{2,}[A-Z]*(?![A-Za-z0-9])')


def extract_model_tokens(text: str) -> list[str]:
    """Model-like tokens (letters + 2+ digits, e.g. AB1234, T123) from text,
    deduplicated in order of appearance. Industry-agnostic shape only."""
    if not text:
        return []
    return list(dict.fromkeys(_MODEL_TOKEN_RE.findall(text)))


def entity_mentioned(entity: str, text: str) -> bool:
    """Boundary-aware containment: 'T1' must not match 'T123'."""
    if not entity or not text:
        return False
    return re.search(r'(?<![A-Za-z0-9])' + re.escape(entity) + r'(?![A-Za-z0-9])',
                     text, re.I) is not None


def _merged_spec_terms(plan: dict | None, industry_hint: str | None,
                       query_text: str = "") -> dict:
    """Core generic terms merged with the active industry pack's terms."""
    terms = dict(settings.CONTEXT_CONFIG.get("spec_query_terms") or {})
    try:
        from ..plugins import get_plugin_registry
        registry = get_plugin_registry()
        plugin = None
        if industry_hint and industry_hint != "auto":
            plugin = registry.get_plugin(industry_hint)
        if plugin is None:
            cat = (plan or {}).get("routed_category") or ""
            if cat and hasattr(registry, "get_plugin_by_category"):
                plugin = registry.get_plugin_by_category(cat)
        if plugin is None and query_text and hasattr(registry, "detect_plugin_for_text"):
            # Content-grounded fallback: the pack applies only when its own
            # entity patterns match the query (category routing is modal and
            # cannot scope packs per query).
            plugin = registry.detect_plugin_for_text(query_text)
        if plugin is not None:
            pack_terms = plugin.retrieval.get_spec_query_terms() or {}
            if pack_terms:
                terms.update(pack_terms)
    except Exception as e:
        logger.warning(f"[SPEC_FACTS] pack spec_query_terms unavailable (non-fatal): {e}")
    return terms


def build_spec_keywords(query_text: str, plan: dict | None = None,
                        industry_hint: str | None = None) -> list[str]:
    """Keyword set for spec-fact matching.

    Sources: English/model tokens from the raw query AND the planner's
    rewritten query (follow-ups like "哪个更强？" carry no entity tokens;
    the history-aware rewrite does), term synonym expansion (core + pack),
    and planner-harvested entities for scoping.
    """
    keywords: list[str] = []
    keyword_text = query_text or ""
    rw = (plan or {}).get("rewritten_query")
    if isinstance(rw, list):
        rw = "; ".join(str(q) for q in rw)
    if isinstance(rw, str) and rw:
        keyword_text += " " + rw

    # NOTE: continuation chars must be ASCII-only — \w in Unicode mode eats
    # CJK, so an unspaced query like "RK3562的CPU架构" would otherwise fuse
    # into one useless token instead of yielding RK3562 + CPU.
    for tok in re.findall(r'[A-Za-z][A-Za-z0-9.\-]{1,20}', keyword_text):
        keywords.append(tok)

    text_lower = keyword_text.lower()
    for zh, en_words in _merged_spec_terms(plan, industry_hint, keyword_text).items():
        if zh in keyword_text or zh.lower() in text_lower:
            keywords.extend(en_words)

    # Planner-harvested entities improve entity scoping.
    for ent in ((plan or {}).get("entities") or []):
        if isinstance(ent, str) and ent:
            keywords.append(ent)

    # Deduplicate, keep order.
    seen, uniq = set(), []
    for kw in keywords:
        k = kw.lower()
        if k and k not in seen:
            seen.add(k)
            uniq.append(kw)
    return uniq


def lookup_spec_facts(query_text: str, metadata_db, plan: dict | None = None,
                      industry_hint: str | None = None,
                      doc_id_filter: set | None = None) -> list[dict]:
    """Look up the assertion-level spec_facts index for this query.

    Only facts with >= spec_facts_min_hits keyword hits qualify. Returns []
    when the feature is off, the table is empty, or nothing matches — the
    caller then proceeds with the normal page-level context unchanged.
    """
    cfg = settings.CONTEXT_CONFIG
    if not cfg.get("spec_facts_enabled", False):
        return []
    if not hasattr(metadata_db, "search_spec_facts"):
        return []

    uniq = build_spec_keywords(query_text, plan, industry_hint)
    if not uniq:
        return []

    doc_filter = doc_id_filter
    if doc_filter is None:
        try:
            df = (plan or {}).get("doc_id_filter") or (plan or {}).get("doc_filter")
            if df:
                doc_filter = set(df)
        except Exception:
            doc_filter = None

    try:
        hits = metadata_db.search_spec_facts(
            uniq, doc_id_filter=doc_filter,
            limit=cfg.get("spec_facts_max_inject", 6) * 3)
    except Exception as e:
        logger.warning(f"[SPEC_FACTS] lookup failed (non-fatal): {e}")
        return []

    min_hits = cfg.get("spec_facts_min_hits", 2)
    qualified = []
    for h in hits:
        hay = f"{h.get('entity','')} {h.get('attribute','')} {h.get('value','')} {h.get('source_text','')}".lower()
        n = sum(1 for kw in uniq if kw.lower() in hay)
        if n >= min_hits:
            h["_hits"] = n
            qualified.append(h)

    # Entity scoping: when the query (or its rewrite) literally names entities
    # from the assertion index's own vocabulary, keep only their facts.
    # Synonym matching alone can pull in UNRELATED entities sharing the
    # attribute family (e.g. a "DDR4 support" query about chip A also matches
    # chip B's DDR4 row), which pollutes the injected block. The vocabulary is
    # the index's entity set (not the qualified hits) so a query about an
    # entity with no matching facts injects NOTHING instead of leaking other
    # entities' facts; protocol tokens (e.g. "PCIE30") never count as entities
    # because they are not in the entity vocabulary.
    if cfg.get("spec_facts_entity_restriction", True) and qualified:
        keyword_text = f"{query_text or ''} {((plan or {}).get('rewritten_query') if isinstance((plan or {}).get('rewritten_query'), str) else ' '.join(str(q) for q in ((plan or {}).get('rewritten_query') or [])))}"
        tokens = extract_model_tokens(keyword_text)
        if tokens:
            vocab = None
            if hasattr(metadata_db, "get_spec_fact_entities"):
                try:
                    vocab = {e.strip().lower() for e in metadata_db.get_spec_fact_entities() if e}
                except Exception:
                    vocab = None
            if vocab is None:
                vocab = {(h.get("entity") or "").strip().lower() for h in qualified}
            named = {t for t in tokens if t.lower() in vocab}
            if named:
                named_l = {n.lower() for n in named}
                qualified = [h for h in qualified
                             if (h.get("entity") or "").strip().lower() in named_l]

    qualified.sort(key=lambda x: -x["_hits"])
    return qualified[: cfg.get("spec_facts_max_inject", 6)]


def format_spec_facts(facts: list[dict]) -> str:
    """Render spec facts as an authoritative evidence block for the context.

    Facts are grouped by entity so the model cannot misattribute a value
    (e.g. RK3562's GPU frequency) to another entity: each group carries a
    clear [ENTITY] header and every fact keeps its verbatim source line.
    """
    if not facts:
        return ""
    lines = ["【权威规格事实 / Authoritative Spec Facts】(extracted from original page text, verbatim-verified)"]
    order: list[str] = []
    by_entity: dict[str, list[dict]] = {}
    for f in facts:
        ent = (f.get("entity") or "?").strip()
        if ent not in by_entity:
            by_entity[ent] = []
            order.append(ent)
        by_entity[ent].append(f)
    for ent in order:
        lines.append(f"- [{ent}]")
        for f in by_entity[ent]:
            lines.append(
                f"  - {f.get('attribute','')}: {f.get('value','')}"
                f"  (page {f.get('page_num','?')}; 原文: \"{f.get('source_text','')[:120]}\")")
    return "\n".join(lines)
