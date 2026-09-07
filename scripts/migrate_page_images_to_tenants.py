#!/usr/bin/env python3
"""Migrate page renders / chart crops from the legacy global images dir to
tenant-scoped images dirs.

Background: ingestion historically wrote page renders ({doc_id}_p{N}.png) to
the global DATA_DIR/images/, while the authenticated /images/{filename}
endpoint serves only from DATA_DIR/tenants/<tenant_id>/images/. Files in the
global dir are therefore unreachable (404) at query time. Ingestion now
writes to the tenant dir directly; this script migrates existing files.

Ownership is resolved by looking up the doc_id (filename prefix) in each
tenant's metadata.db. Files whose doc_id belongs to no known tenant, and
legacy "page_{N}.png" files without a doc_id, are left in place and reported.

Idempotent: re-running after a partial move is safe. Files already present
at the destination with identical size are treated as migrated (the source
is removed); size mismatches are reported and skipped.

Usage (from repo root, venv python):
    python3 scripts/migrate_page_images_to_tenants.py --dry-run
    python3 scripts/migrate_page_images_to_tenants.py
"""

import argparse
import re
import shutil
import sqlite3
import sys
from pathlib import Path

# {doc_id}_p{N}.png (page renders) and {doc_id}_p{N}_chart{K}.png (chart crops)
FILE_RE = re.compile(r"^([0-9a-f]{32})_p\d+(?:_chart\d+)?\.(?:png|jpg|jpeg|webp)$", re.IGNORECASE)


def find_data_dir(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    # Prefer core.config (honours OPENLAD_DATA_DIR), fall back to ./data
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from core.config import settings
        return Path(settings.DATA_DIR)
    except Exception:
        return Path("data")


def build_doc_tenant_map(tenants_dir: Path) -> dict[str, str]:
    """doc_id -> tenant_id across all tenant metadata DBs."""
    mapping: dict[str, str] = {}
    if not tenants_dir.is_dir():
        return mapping
    for tenant_dir in sorted(tenants_dir.iterdir()):
        db_path = tenant_dir / "metadata.db"
        if not db_path.is_file():
            continue
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                rows = conn.execute("SELECT id FROM documents").fetchall()
            finally:
                conn.close()
        except sqlite3.Error as e:
            print(f"WARN: cannot read {db_path}: {e}", file=sys.stderr)
            continue
        for (doc_id,) in rows:
            if doc_id in mapping and mapping[doc_id] != tenant_dir.name:
                print(f"WARN: doc {doc_id} appears in multiple tenants "
                      f"({mapping[doc_id]}, {tenant_dir.name}); keeping first",
                      file=sys.stderr)
                continue
            mapping[doc_id] = tenant_dir.name
    return mapping


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=None,
                    help="Override data dir (default: core.config DATA_DIR or ./data)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would be moved without touching files")
    args = ap.parse_args()

    data_dir = find_data_dir(args.data_dir)
    legacy_dir = data_dir / "images"
    tenants_dir = data_dir / "tenants"
    if not legacy_dir.is_dir():
        print(f"Legacy images dir not found: {legacy_dir} (nothing to do)")
        return 0

    doc_tenant = build_doc_tenant_map(tenants_dir)
    print(f"Data dir: {data_dir}")
    print(f"Tenants with documents: {len(set(doc_tenant.values()))}, "
          f"documents mapped: {len(doc_tenant)}")

    moved = skipped_orphan = skipped_conflict = already = 0
    per_tenant: dict[str, int] = {}
    for f in sorted(legacy_dir.iterdir()):
        if not f.is_file():
            continue
        m = FILE_RE.match(f.name)
        if not m:
            skipped_orphan += 1
            continue
        doc_id = m.group(1)
        tenant_id = doc_tenant.get(doc_id)
        if not tenant_id:
            skipped_orphan += 1
            continue
        dest_dir = tenants_dir / tenant_id / "images"
        dest = dest_dir / f.name
        if dest.exists():
            if dest.stat().st_size == f.stat().st_size:
                if not args.dry_run:
                    f.unlink()
                already += 1
            else:
                print(f"CONFLICT: {f.name} exists at destination with "
                      f"different size; skipped", file=sys.stderr)
                skipped_conflict += 1
            continue
        if not args.dry_run:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), str(dest))
        moved += 1
        per_tenant[tenant_id] = per_tenant.get(tenant_id, 0) + 1

    verb = "would move" if args.dry_run else "moved"
    print(f"\n{'DRY-RUN ' if args.dry_run else ''}Summary:")
    for tid, n in sorted(per_tenant.items()):
        print(f"  {verb} {n} files -> tenant '{tid}'")
    print(f"  total {verb}: {moved}")
    if already:
        print(f"  already at destination (source {'would be ' if args.dry_run else ''}removed): {already}")
    if skipped_orphan:
        print(f"  left in place (no owning tenant / no doc_id): {skipped_orphan}")
    if skipped_conflict:
        print(f"  conflicts skipped: {skipped_conflict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
