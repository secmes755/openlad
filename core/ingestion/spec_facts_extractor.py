"""Rule-based spec-fact extractor.

Extracts (entity, attribute, value) assertions from authoritative page text
(raw_text). NEVER reads VLM-generated descriptions — the extractor strips the
"Page Visual Analysis (VLM)" block before matching, so VLM hallucinations
(e.g. miscounted 560 solder balls) cannot enter the spec_facts table.

Design principles:
- Precision over recall (stage 1): only match unambiguous spec patterns.
- Self-verifying: every extracted value must appear verbatim in the stripped
  source line (guaranteed by construction), so no LLM-style hallucination.
- Datasheet-oriented patterns: key-value specs, Support sentences,
  resolution/fps declarations, table-like attribute rows.
"""
import re

# --- VLM block stripping -----------------------------------------------------
_VLM_BLOCK_RE = re.compile(r'---\s*#{0,3}\s*Page Visual Analysis \(VLM\).*', re.S | re.I)


def strip_vlm_blocks(text: str) -> str:
    """Remove AI-generated VLM description blocks from page text."""
    if not text:
        return ""
    return _VLM_BLOCK_RE.sub("", text)


# --- Number words ------------------------------------------------------------
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "single": 1, "dual": 2, "quad": 4,
}


def _num_word_to_int(word: str) -> int | None:
    w = word.lower().strip()
    if w in _NUMBER_WORDS:
        return _NUMBER_WORDS[w]
    try:
        return int(w)
    except ValueError:
        return None


# --- Patterns ----------------------------------------------------------------

# Pattern 1: key-value spec lines, e.g.
#   "ball size: 0.35mm"  "ball pitch: 0.65mm"  "body: 19mm x 19mm"
#   "GPU: Mali-G52 1-Core-2EE"  "Process: 22nm"
_KV_RE = re.compile(
    r'\b([A-Za-z][A-Za-z0-9 _/()&.\-]{1,35}?)\s*:\s*'
    r'((?:\d[\w.]*|[A-Z][\w\-]{2,})[\w.\s×x/@%+\-–,()]{0,45}?)'
    r'(?=$|[;\n]|\s{2,})'
)

# Pattern 2: Support sentences, e.g.
#   "Support ten UART interfaces"  "Supports up to 4 display interfaces"
#   "Support 2 channels"  "Support 8K bits Size"
_SUPPORT_RE = re.compile(
    r'\bSupports?\s+(?:up to\s+)?'
    r'(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|single|dual|quad|\d+)\s+'
    r'([\w][\w\s/\-]{0,28}?)'
    r'\s+(interfaces?|channels?|ports?|lanes|bits|cores?|displays?|cameras?|screens?)\b',
    re.I,
)

# Pattern 3: resolution / fps declarations, e.g.
#   "H.264 BP/MP/HP, up to 3840x2160@25fps"  "Support 4K@30fps"
_RESOLUTION_RE = re.compile(
    r'\b(H\.26[45]|HEVC|VP\d|AV\d|MPEG-?\d|JPEG)[\w\s/(),.\-]{0,30}?'
    r'(?:up to|max(?:imum)?)\s+'
    r'(\d{3,5}\s*[x×]\s*\d{3,5}(?:\s*@\s*\d+\s*fps)?|[48]K(?:\s*@\s*\d+\s*fps)?)',
    re.I,
)

# Pattern 4: NPU/compute power, e.g. "NPU: 1 TOPS" "up to 3 TOPS"
_TOPS_RE = re.compile(r'\b(\d+(?:\.\d+)?)\s*(TOPS?)\b', re.I)

# Known spec headers that may appear alone on one line with the value on the
# next line, e.g. "GPU\n Mali-G52 1-Core-2EE" (Features list layout).
_SPEC_HEADERS = {
    "gpu", "cpu", "npu", "mcu", "mpu", "isp", "vpu", "dpu", "dsp", "vop",
    "rga", "jpeg", "vdpu", "vepu", "package", "process", "memory", "ddr",
    "display", "camera", "video encoder", "video decoder", "encoder", "decoder",
}

# Attribute names that are too generic to be useful alone (skip unless value
# is distinctive). Keeps noise out of the table.
_SKIP_ATTRS = {
    "note", "notes", "copyright", "rev", "revision", "date", "version",
    "page", "figure", "fig", "table", "chapter", "section", "http", "https",
    "www", "tel", "fax", "email", "mail", "address", "zip",
}


def _clean(s: str) -> str:
    return re.sub(r'\s+', ' ', (s or '')).strip(' \t\r\n:;,.')


def extract_spec_facts_from_text(raw_text: str, page_num: int, entity: str,
                                  doc_id: str) -> list[dict]:
    """Extract spec facts from one page's raw text (VLM blocks stripped first).

    Returns list of dicts: {entity, attribute, value, unit, page_num,
    source_text, extractor, verified}.
    """
    text = strip_vlm_blocks(raw_text)
    if not text.strip():
        return []
    facts: list[dict] = []
    seen = set()

    def add(attr: str, value: str, source: str, unit: str = "",
            verify_against: str = ""):
        attr = _clean(attr)
        value = _clean(value)
        source = _clean(source)
        if not attr or not value or not source:
            return
        if attr.lower() in _SKIP_ATTRS:
            return
        if len(value) > 60 or len(attr) > 45:
            return
        # Self-verification: the value (or its original surface form, for
        # number-word normalization like ten->10) must appear in the source.
        probe = (verify_against or value).lower()
        if probe not in source.lower():
            return
        key = (attr.lower(), value.lower())
        if key in seen:
            return
        seen.add(key)
        facts.append({
            "doc_id": doc_id,
            "entity": entity,
            "attribute": attr,
            "value": value,
            "unit": unit,
            "page_num": page_num,
            "source_text": source[:300],
            "extractor": "rule",
            "verified": 1,
        })

    lines = text.splitlines()
    for i, line in enumerate(lines):
        line = line.strip()
        if len(line) < 2:
            continue

        # Pattern 0: two-line header+value. Two triggers:
        #  a) prev line is a known spec header word (GPU / CPU / ...)
        #  b) prev line is a short header ending with ':' (e.g.
        #     "3D Graphics Engine:") and current line looks like a model value
        #     (e.g. " Mali-G52 1-Core-2EE") — covers headers not in the list.
        if i > 0:
            prev = lines[i - 1].strip()
            prev_key = prev.lower().rstrip(':').strip()
            header_hit = prev_key in _SPEC_HEADERS and 0 < len(prev) < 20
            colon_header = (prev.endswith(':') and 2 < len(prev) < 32
                            and re.search(r'[A-Za-z]', prev))
            if (header_hit or colon_header) and line:
                if (len(line) < 60 and not line.endswith(':')
                        and re.search(r'[A-Za-z0-9]', line)
                        and line.lower() not in _SPEC_HEADERS):
                    add(prev.rstrip(':').strip(), line, f"{prev} {line}")

        if len(line) < 6:
            continue

        # Pattern 3 (resolution) first: most specific.
        for m in _RESOLUTION_RE.finditer(line):
            codec = _clean(m.group(1))
            res = _clean(m.group(2))
            add(f"{codec} max resolution", res, line)

        # Pattern 2 (Support sentences). verify_against keeps the original
        # number word so "Support ten UART" -> value "10" still verifies.
        for m in _SUPPORT_RE.finditer(line):
            num = _num_word_to_int(m.group(1))
            feature = _clean(m.group(2))
            unit_word = _clean(m.group(3))
            if num is not None and feature:
                add(f"{feature} {unit_word} count", str(num), line,
                    verify_against=m.group(1))

        # Pattern 4 (TOPS).
        for m in _TOPS_RE.finditer(line):
            add("compute power", f"{m.group(1)} {m.group(2).upper()}", line, unit=m.group(2).upper())

        # Pattern 1 (key-value). Skip if the line was already fully explained
        # by a more specific pattern to reduce duplicates.
        for m in _KV_RE.finditer(line):
            add(m.group(1), m.group(2), line)

    return facts


# --- Doc entity inference ------------------------------------------------------
_CHIP_MODEL_RE = re.compile(r'\b(RK\d{4}[A-Z]?|SSU\d{4}|T\d{3}|RV\d{4}|PX\d+|RK\d{3}[A-Z]?)\b')


def infer_doc_entity(title: str, filename: str = "") -> str:
    """Infer the primary entity (chip model) from doc title/filename."""
    for src in (title or "", filename or ""):
        m = _CHIP_MODEL_RE.search(src)
        if m:
            return m.group(1)
    return _clean(title or filename)[:40] or "unknown"
