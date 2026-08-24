"""Section entity harvest — build a compact identifier inventory per section.

Problem: structure-index keywords come only from the section title, and
summaries mention interface families at most ("covers UART, SPI"). Queries
about specific instances ("UART0", "I2C1_SDA") therefore cannot match the
right chapter at the structure level.

Approach (deterministic, model-free): harvest identifier-shaped tokens from
the section's full text, cluster them into families by their numeric instance
part, and compress instances into ranges. Families act as the chapter-level
vocabulary that instance queries can hit. Fragments from broken text mostly
vanish because clustering looks only at (family, instance-number), and junk
single-letter families are dropped.

Output example: "DDR3-DDR4, GPIO0-GPIO4, I2C0-I2C6, SPI0-SPI3, UART0-UART9"
"""
import logging
import re
from collections import defaultdict

logger = logging.getLogger(__name__)

# Identifier-shaped token: starts with a letter, may contain digits/underscores,
# and must contain at least one digit (the instance signal).
_TOKEN_RE = re.compile(r'[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*')
# Family/instance split: family = leading alphanumeric part (>=2 chars, may
# contain embedded digits like I2C/I2S), instance = the LAST bare digit run
# before an optional underscore suffix.
_FAMILY_RE = re.compile(r'^([A-Za-z][A-Za-z0-9]*?[A-Za-z])(\d+)(?:_.*)?$')

_MAX_INSTANCE = 9999        # ignore implausible instance numbers (years, values)
_MAX_FAMILIES = 40          # cap inventory size per section
_MAX_OUTPUT_CHARS = 1500


def _compress_ranges(nums: list[int]) -> str:
    """[0,1,2,4,5,9] -> '0-2, 4-5, 9'"""
    nums = sorted(set(nums))
    if not nums:
        return ''
    parts, start, prev = [], nums[0], nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        parts.append(f'{start}-{prev}' if prev > start else str(start))
        start = prev = n
    parts.append(f'{start}-{prev}' if prev > start else str(start))
    return ', '.join(parts)


def harvest_section_entities(text: str) -> str:
    """Harvest a compact family inventory from section text.

    Returns e.g. 'DDR3-DDR4, GPIO0-GPIO4, UART0-UART9' or '' when the section
    contains no identifier instances. Deterministic and model-free."""
    if not text or len(text) < 50:
        return ''
    try:
        families: dict[str, set] = defaultdict(set)
        for m in _TOKEN_RE.finditer(text):
            tok = m.group(0)
            if len(tok) > 64 or not any(c.isdigit() for c in tok):
                continue
            fm = _FAMILY_RE.match(tok)
            if not fm:
                continue
            fam, num = fm.group(1).upper(), int(fm.group(2))
            if len(fam) < 2 or num > _MAX_INSTANCE:
                continue
            families[fam].add(num)

        if not families:
            return ''

        # Prefer families with more evidence (more distinct instances or more
        # frequent tokens); cap the inventory to keep it compact.
        def _fam_count(fam: str) -> int:
            return sum(1 for m in _TOKEN_RE.finditer(text)
                       if (lambda f: f and f.group(1).upper() == fam)(_FAMILY_RE.match(m.group(0))))

        ranked = sorted(families.items(), key=lambda kv: -len(kv[1]))
        if len(ranked) > _MAX_FAMILIES:
            ranked = sorted(ranked, key=lambda kv: -_fam_count(kv[0]))[:_MAX_FAMILIES]

        parts = []
        for fam, nums in sorted(ranked):
            rng = _compress_ranges(sorted(nums))
            if rng:
                parts.append(f'{fam}{rng}')
        out = ', '.join(parts)
        return out[:_MAX_OUTPUT_CHARS]
    except Exception as e:
        logger.debug(f'[ENTITY] harvest failed: {e}')
        return ''


def harvest_acronyms(client, text: str, title: str = "") -> str:
    """Ask the LLM to extract acronym/alias pairs from section text.

    Returns comma-delimited pairs like 'NPU=Neural Process Unit, IPU=Intelligence
    Processing Unit' or '' when none found.

    This is model-driven and fully generic: the LLM reads the text for patterns
    like 'X (Y)' or 'Y stands for X' and emits any it finds. No industry-specific
    rules, no hardcoded mappings.
    """
    if not text or len(text) < 100:
        return ""
    snippet = text[:3000] + ("..." if len(text) > 3000 else "")
    title_hint = f' in section "{title}"' if title else ""
    prompt = (
        f"Extract all abbreviation-fullname pairs from this technical text{title_hint}.\n"
        f"Look for patterns like 'NPU (Neural Process Unit)', 'DDR (Double Data Rate)', "
        f"'DOCSIS (Data Over Cable Service Interface Specification)'.\n"
        f"Return each as 'ABBREV=Full Name', separated by commas.\n"
        f"If no abbreviation pairs found, return 'NONE'.\n\n"
        f"Text:\n{snippet}\n\n"
        f"Output only the pairs or NONE:"
    )
    try:
        result = client.generate(prompt, temperature=0.1, max_tokens=256)
        result = result.strip()
        if not result or result.upper() == "NONE":
            return ""
        return result
    except Exception:
        return ""
