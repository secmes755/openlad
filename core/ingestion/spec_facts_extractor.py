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

Vocabulary boundary (industry-agnostic core): every word list lives in the
industry pack and arrives via the `extraction` dict
(RetrievalPlugin.get_spec_extraction_config -> rules.yaml `spec_extraction`):
  - spec_headers: two-line header words (GPU/CPU/...)
  - compute_units + compute_attribute: unit-based compute declarations
    (e.g. TOPS -> "compute power")
  - frequency_terms: measure words for frequency/clock rows
Core keeps only the matching MECHANISMS; a missing/empty list disables the
corresponding pattern (no pack = structural patterns only).
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
    r'\s+(interfaces?|channels?|ports?|lanes|bits|cores?|displays?|cameras?|screens?|controllers?)\b',
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

# Pattern 4: compute-power declarations, e.g. "NPU: 1 TOPS" "up to 3 TOPS".
# The unit list (e.g. ["TOPS"]) and attribute name come from the industry
# pack via `extraction`; core only supplies the number+unit mechanism.
# Strict plural units only: datasheet section headings like "2.2 Top
# Marking" (silkscreen) or video modes like "Output 1 Top frame mode" must
# NOT be read as compute power — real compute declarations carry the unit
# verbatim ("1 TOPS", "3 TOPS", "8 TOPS").
def _build_compute_re(units: list[str]) -> re.Pattern | None:
    if not units:
        return None
    return re.compile(
        rf'\b(\d+(?:\.\d+)?)\s*({"|".join(re.escape(u) for u in units)})\b',
        re.I)

# Pattern 6: frequency/clock declarations, e.g.
#   "Max CPU frequency NA NA 2 GHz"   (table text rows)
#   "Max NPU frequency 1.0 GHz"
#   "OSC input clock frequency NA 24 NA MHz"
# Attribute name is captured from the text (NOT enumerated — core keeps no
# chip vocabulary); the measure words (frequency/clock) come from the pack
# via `extraction`; the unit is a neutral SI Hz-family suffix. TBD/NA cells
# carry no digits, so they never match.
def _build_freq_re(terms: list[str]) -> re.Pattern | None:
    if not terms:
        return None
    alternation = "|".join(re.escape(t) for t in terms)
    return re.compile(
        rf'\b((?:Max\s+)?[A-Za-z][A-Za-z0-9 \-/]{{0,24}}?)\s+'
        rf'(?:{alternation})\s*(?:rate)?\s*[:|]?\s*'
        rf'(?:[^0-9\n]{{0,12}}?)\s*(\d+(?:\.\d+)?)\s*([A-Za-z]{{0,3}}Hz)\b',
        re.I)

# Pattern 5: versioned protocol support declarations, e.g.
#   "Support PCIe3.1(8Gbps) protocol and backward compatible with the PCIe2.1
#    and PCIe1.1 protocol"
#   "Supports USB3.0 standard"  "Support HDMI2.1 interface"
# Generic across protocol families (PCIe/USB/HDMI/SATA/DDR/MIPI/NVMe...):
# the lead token must be letters + version digits. All same-family version
# tokens in the sentence (incl. backward-compatible lists) are collected.
_PROTOCOL_SUPPORT_RE = re.compile(
    r'\bSupports?\s+(?:up to\s+)?'
    r'([A-Za-z][A-Za-z\-]*\s?\d+(?:\.\d+)?)(\([^)]{1,20}\))?'
    r'[\w\s()/%.]*?\b(protocol|standard|interface|revision|version)\b',
    re.I,
)
# A version token of a given family, e.g. "PCIe3.1" / "PCIe 3.1" / "USB3.0".
def _version_tokens(line: str, family: str) -> list[str]:
    fam = re.escape(family).replace('\\ ', r'\s?')
    out = []
    for m in re.finditer(rf'\b({fam}\s?\d+(?:\.\d+)?)', line, re.I):
        tok = re.sub(r'\s+', '', m.group(1))
        if tok.lower() not in (t.lower() for t in out):
            out.append(tok)
    return out

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
                                  doc_id: str,
                                  extraction: dict | None = None) -> list[dict]:
    """Extract spec facts from one page's raw text (VLM blocks stripped first).

    `extraction` carries the industry-pack vocabulary (see module docstring):
      {spec_headers: [..], compute_units: [..], compute_attribute: str,
       frequency_terms: [..]}
    Core keeps only the mechanisms; when a list is missing/empty the
    corresponding pattern is disabled (no pack = structural patterns only).

    Returns list of dicts: {entity, attribute, value, unit, page_num,
    source_text, extractor, verified}.
    """
    extraction = extraction or {}
    spec_headers = {h.strip().lower() for h in (extraction.get("spec_headers") or []) if h}
    compute_re = _build_compute_re(
        [u for u in (extraction.get("compute_units") or []) if u])
    compute_attribute = (extraction.get("compute_attribute") or "").strip()
    freq_re = _build_freq_re(
        [t for t in (extraction.get("frequency_terms") or []) if t])

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
        #  a) prev line is a pack-provided spec header word (GPU / CPU / ...)
        #  b) prev line is a short header ending with ':' (e.g.
        #     "3D Graphics Engine:") and current line looks like a model value
        #     (e.g. " Mali-G52 1-Core-2EE") — covers headers not in the list.
        if i > 0:
            prev = lines[i - 1].strip()
            prev_key = prev.lower().rstrip(':').strip()
            header_hit = prev_key in spec_headers and 0 < len(prev) < 20
            colon_header = (prev.endswith(':') and 2 < len(prev) < 32
                            and re.search(r'[A-Za-z]', prev))
            if (header_hit or colon_header) and line:
                if (len(line) < 60 and not line.endswith(':')
                        and re.search(r'[A-Za-z0-9]', line)
                        and line.lower() not in spec_headers):
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

        # Pattern 4 (compute power). Unit comes from the pack's
        # compute_units (e.g. "TOPS"); attribute name likewise. Disabled
        # without a pack vocabulary.
        if compute_re and compute_attribute:
            for m in compute_re.finditer(line):
                add(compute_attribute, f"{m.group(1)} {m.group(2).upper()}",
                    line, unit=m.group(2).upper())

        # Pattern 6 (frequency/clock). Measure words come from the pack's
        # frequency_terms; attribute name is captured from the text; the
        # unit is a neutral SI Hz-family suffix. NA/TBD cells never match
        # because no digits precede the unit. Disabled without a pack
        # vocabulary.
        if freq_re:
            for m in freq_re.finditer(line):
                attr = _clean(m.group(1))
                unit = m.group(3)  # keep surface case ("GHz"/"MHz")
                freq = f"{m.group(2)} {unit}"
                add(f"{attr} frequency", freq, line, unit=unit)

        # Pattern 5 (versioned protocol support). Value lists every same-family
        # version token in the sentence (e.g. "PCIe3.1(8Gbps), PCIe2.1, PCIe1.1").
        # Each component is verified verbatim against the source line; the lead
        # token doubles as the self-verification probe for the composite value.
        for m in _PROTOCOL_SUPPORT_RE.finditer(line):
            family = _clean(m.group(1))
            family_tok = re.sub(r'\s+', '', family)
            # family stem = letters only (group(1) is letters+version, e.g.
            # "PCIe3.1" -> stem "PCIe"); version tokens are collected by stem.
            stem_m = re.match(r'[A-Za-z][A-Za-z\-]*', family_tok)
            if not stem_m or len(stem_m.group(0)) < 2:
                # 1-char stems ("I" from I2S/I2C) are meaningless attributes.
                continue
            stem = stem_m.group(0)
            lead = family_tok + (m.group(2) or '')
            versions = _version_tokens(line, stem)
            if not versions:
                continue
            line_nospace = line.lower().replace(' ', '')
            if not all(re.sub(r'\s+', '', v).lower() in line_nospace
                       or v.lower() in line.lower() for v in versions):
                continue
            rest = [v for v in versions if v.lower() != family_tok.lower()]
            value = ', '.join([lead] + rest)
            add(f"{stem} protocol", value, line, verify_against=family_tok)

        # Pattern 1 (key-value). Skip if the line was already fully explained
        # by a more specific pattern to reduce duplicates.
        for m in _KV_RE.finditer(line):
            add(m.group(1), m.group(2), line)

    return facts


# --- Doc entity inference ------------------------------------------------------
# Core is industry-agnostic: entity patterns (e.g. chip-model regexes) are
# supplied by the active industry pack via RetrievalPlugin.get_entity_patterns()
# (see industries/*/retrieval/rules.yaml -> entity_patterns). With no patterns
# the entity falls back to a compact title/filename tag.
def infer_doc_entity(title: str, filename: str = "",
                     entity_patterns: list[str] | None = None) -> str:
    """Infer the primary document entity from title/filename via pack patterns."""
    for pat in (entity_patterns or []):
        try:
            rx = re.compile(pat)
        except re.error:
            continue
        m = rx.search(title or "") or rx.search(filename or "")
        if m:
            return m.group(1)
    return _clean(title or filename)[:40] or "unknown"
