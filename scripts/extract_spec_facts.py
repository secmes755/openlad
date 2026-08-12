#!/usr/bin/env python3
"""Offline spec-fact extraction: scan all doc_pages in a tenant DB and populate
spec_facts via the rule extractor. Idempotent (clears before re-extracting).
Usage: python scripts/extract_spec_facts.py [tenant_id]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db.tenant_db import get_tenant_metadata_db
from core.ingestion.spec_facts_extractor import extract_spec_facts_from_text, infer_doc_entity


def _load_entity_patterns() -> list[str]:
    """Entity patterns come from industry packs (core is industry-agnostic).
    Offline tool: collect patterns from all registered plugins."""
    patterns: list[str] = []
    try:
        from core.plugins import get_plugin_registry
        registry = get_plugin_registry()
        for name in (registry.list_plugins() or []):
            try:
                plugin = registry.get_plugin(name)
                if plugin is not None:
                    patterns.extend(plugin.retrieval.get_entity_patterns() or [])
            except Exception:
                continue
    except Exception as e:
        print(f"  (plugin registry unavailable, entity fallback active: {e})")
    return patterns


def _load_extraction_config() -> dict:
    """Merge extractor vocabulary from all registered industry packs.
    Word lists are unioned; the first non-empty compute_attribute wins."""
    merged: dict = {}
    try:
        from core.plugins import get_plugin_registry
        registry = get_plugin_registry()
        for name in (registry.list_plugins() or []):
            try:
                plugin = registry.get_plugin(name)
                if plugin is None:
                    continue
                cfg = plugin.retrieval.get_spec_extraction_config() or {}
                for key in ("spec_headers", "compute_units", "frequency_terms"):
                    merged.setdefault(key, [])
                    merged[key].extend(cfg.get(key) or [])
                if not merged.get("compute_attribute") and cfg.get("compute_attribute"):
                    merged["compute_attribute"] = cfg["compute_attribute"]
            except Exception:
                continue
        for key in ("spec_headers", "compute_units", "frequency_terms"):
            if key in merged:
                seen, uniq = set(), []
                for w in merged[key]:
                    if str(w).strip().lower() not in seen:
                        seen.add(str(w).strip().lower())
                        uniq.append(w)
                merged[key] = uniq
    except Exception as e:
        print(f"  (plugin registry unavailable, extraction vocab empty: {e})")
    return merged


def main(tenant_id: str = "test"):
    db = get_tenant_metadata_db(tenant_id)
    db.clear_spec_facts()
    entity_patterns = _load_entity_patterns()
    extraction = _load_extraction_config()

    with db.get_connection() as conn:
        docs = [dict(r) for r in conn.execute(
            "SELECT id, title, filename FROM documents").fetchall()]

    total_facts = 0
    for doc in docs:
        doc_id = doc["id"]
        entity = infer_doc_entity(doc.get("title", ""), doc.get("filename", ""),
                                  entity_patterns=entity_patterns)
        pages = db.get_document_pages(doc_id)
        doc_facts = 0
        for p in pages:
            raw = p.get("raw_text") or ""
            facts = extract_spec_facts_from_text(raw, p.get("page_num"), entity, doc_id,
                                                 extraction=extraction)
            for f in facts:
                db.insert_spec_fact(
                    doc_id=f["doc_id"], entity=f["entity"], attribute=f["attribute"],
                    value=f["value"], unit=f.get("unit", ""), page_num=f["page_num"],
                    source_text=f["source_text"], extractor=f["extractor"],
                    verified=f["verified"])
            doc_facts += len(facts)
        total_facts += doc_facts
        if doc_facts:
            print(f"  {doc_id[:8]} {doc.get('title','')[:28]:30s} entity={entity:10s} facts={doc_facts}")

    print(f"\nTotal spec facts: {total_facts}")

    # Verify the 4 target assertions from failing test cases.
    print("\n=== Target assertion verification ===")
    targets = [
        ("pd1 ball size", ["ball", "size", "0.35"]),
        ("pd1 ball pitch", ["ball", "pitch", "0.65"]),
        ("pd4 UART count", ["uart", "10"]),
        ("pd5 GPU", ["gpu", "mali"]),
        ("pd10 H.264", ["h.264", "3840"]),
    ]
    for name, kws in targets:
        hits = db.search_spec_facts(kws, limit=5)
        print(f"\n{name} ({kws}): {len(hits)} hits")
        for h in hits[:3]:
            print(f"   doc={h['doc_id'][:8]} p{h['page_num']} [{h['attribute']}] = [{h['value']}]")
            print(f"      src: {h['source_text'][:90]}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "test")
