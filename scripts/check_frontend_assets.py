#!/usr/bin/env python3
"""Frontend asset hygiene check for the CI gate.

Static assets under frontend/web/dist must be:
  1. valid UTF-8 without a BOM (a BOM on HTML breaks doctype sniffing and
     usually indicates a cross-platform editor round-trip);
  2. free of GBK-misdecode mojibake — artifacts like '鉁?' (was '✓') appear
     when UTF-8 text passes through a GBK codec. The pattern set is built
     from observed corruption, not guesswork.

Exit code 0 = clean, 1 = violations found (prints file:line for each).
"""
import sys
from pathlib import Path

DIST = Path(__file__).resolve().parent.parent / "frontend" / "web" / "dist"
TEXT_SUFFIXES = {".html", ".js", ".css", ".svg", ".json", ".md", ".txt"}

# Two-char mojibake sequences observed in this repo (GBK mis-decode of UTF-8).
# Keep the trailing '?' — it is a literal U+003F left behind by lossy decode,
# and it makes the pattern specific enough to avoid false positives.
MOJIBAKE = ["鈥?", "鎴?", "鉁?", "鉂?"]


def main() -> int:
    violations: list[str] = []
    for path in sorted(DIST.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        raw = path.read_bytes()
        rel = path.relative_to(DIST)
        if raw.startswith(b"\xef\xbb\xbf"):
            violations.append(f"{rel}: UTF-8 BOM present")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            violations.append(f"{rel}: not valid UTF-8 ({e})")
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for pat in MOJIBAKE:
                if pat in line:
                    violations.append(f"{rel}:{lineno}: mojibake {pat!r}")

    if violations:
        print("frontend asset check FAILED:", file=sys.stderr)
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        return 1
    print(f"frontend asset check ok ({sum(1 for p in DIST.rglob('*') if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES)} text files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
