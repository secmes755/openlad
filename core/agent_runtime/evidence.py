"""Append-only, request-local evidence accounting and citation validation."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

CITE_RE = re.compile(r"\[d:([0-9a-fA-F]{4,64}),\s*((?:p:\d+\s*,?\s*)+)\]")
PAGE_RE = re.compile(r"p:(\d+)")


@dataclass
class EvidenceItem:
    doc_id: str
    page_num: int
    section_title: str = ""
    doc_name: str = ""
    excerpt: str = ""
    content: str = ""
    segments: list[dict] = field(default_factory=list)
    channels: set[str] = field(default_factory=set)

    @property
    def ref(self) -> tuple[str, int]:
        return self.doc_id, self.page_num

    @property
    def readable(self) -> bool:
        return bool(self.content or self.segments)


class EvidenceStore:
    def __init__(self):
        self.items: dict[tuple[str, int], EvidenceItem] = {}
        self.events: list[dict] = []

    def add_candidates(self, records: list[dict], channel: str) -> list[dict]:
        refs = []
        for record in records:
            ref = self._ref(record)
            if not ref:
                continue
            item = self.items.setdefault(ref, EvidenceItem(*ref))
            item.channels.add(channel)
            self._update_metadata(item, record)
            if not item.excerpt:
                item.excerpt = str(
                    record.get("excerpt") or record.get("chunk_text") or "")[:1200]
            refs.append(self._ref_dict(ref))
        if refs:
            self.events.append({"event": "candidates", "channel": channel, "refs": refs})
        return refs

    def mark_read(self, record: dict) -> dict | None:
        ref = self._ref(record)
        content = str(record.get("content") or record.get("raw_text") or "")
        if not ref or not content:
            return None
        item = self.items.setdefault(ref, EvidenceItem(*ref))
        item.channels.add("read")
        self._update_metadata(item, record)
        segment = {"offset": int(record.get("offset") or 0), "content": content}
        if segment not in item.segments:
            item.segments.append(segment)
        if len(content) > len(item.content):
            item.content = content
        result = self._ref_dict(ref)
        self.events.append({"event": "read", "refs": [result]})
        return result

    def readable_items(self) -> list[EvidenceItem]:
        return [item for item in self.items.values() if item.readable]

    def audit_answer(self, answer: str) -> dict:
        readable = {item.ref for item in self.readable_items()}
        cited = extract_citations(answer)
        verifiable, unverifiable = [], []
        for short_id, page_num in cited:
            hit = next((ref for ref in readable
                        if ref[0].lower().startswith(short_id) and ref[1] == page_num), None)
            (verifiable if hit else unverifiable).append(hit or (short_id, page_num))
        return {
            "cited": cited,
            "verifiable": verifiable,
            "unverifiable": unverifiable,
            "readable_pages": len(readable),
        }

    def sources(self, audit: dict) -> list[dict]:
        sources = []
        for ref in audit["verifiable"]:
            item = self.items[ref]
            sources.append({
                **self._ref_dict(ref), "doc_name": item.doc_name,
                "section_title": item.section_title,
            })
        return sources

    def snapshot(self) -> dict:
        return {
            "candidates": len(self.items),
            "readable_pages": len(self.readable_items()),
            "items": [{
                **self._ref_dict(item.ref), "section_title": item.section_title,
                "doc_name": item.doc_name, "channels": sorted(item.channels),
                "content_chars": sum(len(segment["content"]) for segment in item.segments),
                "readable": item.readable,
            } for item in self.items.values()],
            "events": self.events,
        }

    @staticmethod
    def _ref(record: dict) -> tuple[str, int] | None:
        doc_id = str(record.get("doc_id") or "").lower()
        page_num = record.get("page_num")
        if not doc_id or page_num is None:
            return None
        try:
            return doc_id, int(page_num)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _ref_dict(ref: tuple[str, int]) -> dict:
        return {"doc_id": ref[0], "page_num": ref[1]}

    @staticmethod
    def _update_metadata(item: EvidenceItem, record: dict):
        item.section_title = item.section_title or str(record.get("section_title") or "")
        item.doc_name = item.doc_name or str(
            record.get("doc_name") or record.get("filename") or record.get("title") or "")


def extract_citations(text: str) -> list[tuple[str, int]]:
    citations = []
    for match in CITE_RE.finditer(text or ""):
        doc_id = match.group(1).lower()
        citations.extend(
            (doc_id, int(page)) for page in PAGE_RE.findall(match.group(2)))
    return citations
