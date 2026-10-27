"""Append-only, request-local evidence accounting and citation validation."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

CITE_RE = re.compile(
    r"[\[\(]d:([0-9a-fA-F]{4,64}),\s*((?:p:\d+(?:-\d+)?\s*,?\s*)+)[\]\)]")
MALFORMED_CITE_RE = re.compile(r"[\[\(]\s*d\s*:[^\]\)]*[\]\)]", re.IGNORECASE)
PAGE_RE = re.compile(r"p:(\d+)(?:-(\d+))?")
PAGE_ONLY_RE = re.compile(
    r"(?<![\w:])pp?\.\s*(\d+)(?:\s*[-–—]\s*(\d+))?", re.IGNORECASE)
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
QUALIFIED_ABSOLUTE_RE = re.compile(
    r"无法确认(?:是|是否)[^。！？\n]{0,30}(?:不支持|不存在)|"
    r"(?:不作为|不等于|并不意味着|并非)[^。！？\n]{0,12}(?:不支持|不存在)|"
    r"cannot determine[^.!?\n]{0,40}(?:does not support|does not exist)|"
    r"not (?:a conclusion|evidence) of (?:does not support|does not exist)",
    re.IGNORECASE)


def has_absolute_claim(text: str) -> bool:
    """Detect unqualified absolute claims, not explicit uncertainty or meta-negation."""
    for statement in re.split(r"(?<=[。！？!?])|\n+", text or ""):
        if ABSOLUTE_RE.search(statement) and not QUALIFIED_ABSOLUTE_RE.search(statement):
            return True
    return False


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
               if not extract_citations(statement)
               and not extract_page_references(statement)]
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
        malformed = [match.group(0) for match in MALFORMED_CITE_RE.finditer(answer or "")
                     if not CITE_RE.fullmatch(match.group(0))]
        page_references = extract_page_references(answer)
        readable_docs = {ref[0] for ref in readable}
        ambiguous_pages = []
        if len(readable_docs) == 1:
            doc_id = next(iter(readable_docs))
            seen = set(cited)
            for page_num in page_references:
                ref = (doc_id, page_num)
                if ref not in seen:
                    cited.append(ref)
                    seen.add(ref)
        elif len(readable_docs) > 1:
            ambiguous_pages = page_references
        verifiable, unverifiable = [], []
        for short_id, page_num in cited:
            hit = next((ref for ref in readable
                        if ref[0].lower().startswith(short_id) and ref[1] == page_num), None)
            (verifiable if hit else unverifiable).append(hit or (short_id, page_num))
        return {
            "cited": cited,
            "verifiable": verifiable,
            "unverifiable": unverifiable,
            "malformed_citations": malformed,
            "ambiguous_page_references": ambiguous_pages,
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


def extract_page_references(text: str) -> list[int]:
    """Read natural p.9 / pp.10–12 references, excluding explicit d:/p: citations."""
    masked = CITE_RE.sub(lambda match: " " * len(match.group(0)), text or "")
    pages, seen = [], set()
    for match in PAGE_ONLY_RE.finditer(masked):
        first = int(match.group(1))
        last = int(match.group(2)) if match.group(2) else first
        if last < first or last - first > 50:
            continue
        for page_num in range(first, last + 1):
            if page_num not in seen:
                pages.append(page_num)
                seen.add(page_num)
    return pages


def readable_evidence_payload(store: EvidenceStore, max_chars: int) -> list[dict]:
    """Fairly allocate a bounded evidence budget without hiding truncation."""
    readable = store.readable_items()
    if not readable:
        return []
    lengths = [sum(len(segment["content"]) for segment in item.segments)
               for item in readable]
    budget = max(0, int(max_chars))
    base = budget // len(readable)
    allocations = [min(length, base) for length in lengths]
    remaining = budget - sum(allocations)
    while remaining:
        active = [index for index, length in enumerate(lengths)
                  if allocations[index] < length]
        if not active:
            break
        share = max(1, remaining // len(active))
        progressed = False
        for index in active:
            addition = min(lengths[index] - allocations[index], share, remaining)
            allocations[index] += addition
            remaining -= addition
            progressed = progressed or addition > 0
            if not remaining:
                break
        if not progressed:
            break

    payload = []
    for item, length, allocation in zip(readable, lengths, allocations, strict=True):
        parts, left, included = [], allocation, 0
        for segment in item.segments:
            content = segment["content"][:left]
            if content:
                parts.append(f"[offset {segment['offset']}]\n{content}")
                included += len(content)
                left -= len(content)
            if left <= 0:
                break
        payload.append({
            "doc_id": item.doc_id,
            "page_num": item.page_num,
            "doc_name": item.doc_name,
            "section_title": item.section_title,
            "content": "\n".join(parts),
            "content_chars": length,
            "included_chars": included,
            "truncated": included < length,
        })
    return payload
