"""Append-only, request-local evidence accounting and citation validation."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

CITE_RE = re.compile(
    r"[\[\(]d:([0-9a-fA-F]{4,64}),\s*((?:p:\d+(?:-\d+)?\s*,?\s*)+)[\]\)]")
PAGE_RE = re.compile(r"p:(\d+)(?:-(\d+))?")
INSUFFICIENT_RE = re.compile(
    r"未找到|没有找到|无法确认|未提及|未明确|无法回答|无法比较|"
    r"缺少[^。！？\n]{0,30}(?:文档|资料|证据|信息)|"
    r"没有[^。！？\n]{0,30}(?:相关|关于)[^。！？\n]{0,30}(?:文档|资料|信息)|"
    r"not found|no relevant|cannot determine|unable to find|not mentioned|"
    r"insufficient evidence|no [\w -]{0,40}documentation",
    re.IGNORECASE)
ABSOLUTE_RE = re.compile(
    r"不支持|无法支持|不存在|does not support|cannot support|does not exist",
    re.IGNORECASE)


def has_absolute_claim(text: str) -> bool:
    return bool(ABSOLUTE_RE.search(text or ""))


def citation_coverage(text: str) -> dict:
    """Diagnostic citation presence per statement, not semantic claim support."""
    statements = []
    for line in (text or "").splitlines():
        if not line.strip() or re.fullmatch(r"[\s|:\-]+", line):
            continue
        for statement in re.split(
                r"(?<=[。！？；])\s*|(?<=[.!?])\s+(?=[A-Z])", line):
            statement = statement.strip()
            if len(statement) >= 12:
                statements.append(statement)
    uncited = [statement[:240] for statement in statements
               if not extract_citations(statement)]
    cited_count = len(statements) - len(uncited)
    return {
        "statements": len(statements),
        "cited_statements": cited_count,
        "coverage": round(cited_count / len(statements), 4) if statements else None,
        "uncited_examples": uncited[:8],
        "semantic_support_verified": False,
    }


def has_insufficient_evidence_statement(text: str) -> bool:
    # The leading conclusion must be insufficiency. A positive answer cannot
    # gain a citation exemption from a caveat buried later in the response.
    return bool(INSUFFICIENT_RE.search((text or "").strip()[:300]))


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
    citations, seen = [], set()
    for match in CITE_RE.finditer(text or ""):
        doc_id = match.group(1).lower()
        for start, end in PAGE_RE.findall(match.group(2)):
            first, last = int(start), int(end) if end else int(start)
            if last < first or last - first > 50:
                continue
            for page_num in range(first, last + 1):
                ref = (doc_id, page_num)
                if ref not in seen:
                    citations.append(ref)
                    seen.add(ref)
    return citations
