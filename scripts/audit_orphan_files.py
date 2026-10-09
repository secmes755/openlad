#!/usr/bin/env python3
"""Audit (and optionally purge) orphaned on-disk artifacts.

An orphan is a file on disk whose owning document no longer exists in the
tenant's metadata DB -- typically left behind by deletes performed before the
storage-cleanup fix, or by a document whose doc_id no longer resolves.

Two classes are audited:

  1. Per-tenant dirs:  data/tenants/<tid>/documents/*  and  .../images/*
     A file is orphaned when its doc_id prefix (or, for docs, the id embedded
     in "<doc_id>_<name>") belongs to no live document row in that tenant.

  2. The legacy global dir:  data/images/*
     Page renders historically landed here instead of the tenant dir. Files
     are classified as: LIVE (doc_id belongs to a live document -- must NOT be
     deleted), ORPHAN (doc_id unknown), or UNPARSED (no doc_id in the name).

SAFETY
    * Read-only by default. Pass --delete to actually remove orphans.
    * --delete NEVER removes a LIVE file; that is enforced regardless of flags.
    * The global dir is only swept with an extra explicit --include-global, so
      a per-tenant sweep can never nuke the shared dir by accident.

Usage:
    python3 scripts/audit_orphan_files.py --data-dir /path/to/data
    python3 scripts/audit_orphan_files.py --data-dir /path/to/data --delete
    python3 scripts/audit_orphan_files.py --data-dir /path/to/data \
        --include-global --delete
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

# {doc_id}_p{N}.png (page renders) and {doc_id}_p{N}_chart{K}.png (chart crops)
GLOBAL_FILE_RE = re.compile(
    r"^([0-9a-f]{32})_p\d+(?:_chart\d+)?\.(?:png|jpg|jpeg|webp)$", re.IGNORECASE
)
# Tenant documents dir: "<doc_id>_<original name>" (retained for reference;
# classification now uses the DB's recorded original_path, not this prefix).
DOC_FILE_RE = re.compile(r"^([0-9a-f]{32})_", re.IGNORECASE)


def find_data_dir(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from core.config import settings
        return Path(settings.DATA_DIR)
    except Exception:
        return Path("data")


def _live_doc_ids(tenants_dir: Path) -> dict[str, str]:
    """doc_id -> tenant_id across every tenant metadata DB (read-only)."""
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
            mapping.setdefault(doc_id, tenant_dir.name)
    return mapping


def _live_files(tenants_dir: Path) -> tuple[set[str], set[str]]:
    """Return (live_filenames, live_doc_ids) across every tenant DB.

    Classification is deliberately based on what the DB *records* as owned by
    a live document -- ``documents.original_path`` and
    ``doc_pages.page_image_path`` / ``.charts[].image_path`` -- NOT on the
    filename prefix. Filenames and row ids are not always the same UUID
    (uploaded files are named after a different uuid than the row id), so
    prefix matching misclassifies live files as orphans and would delete them.

    live_filenames: basenames the DB claims a live document owns.
    live_doc_ids:   raw document ids (used for the legacy global dir, whose
                    page renders are named "<doc_id>_pN.png").
    """
    filenames: set[str] = set()
    doc_ids: set[str] = set()
    if not tenants_dir.is_dir():
        return filenames, doc_ids
    for tenant_dir in sorted(tenants_dir.iterdir()):
        db_path = tenant_dir / "metadata.db"
        if not db_path.is_file():
            continue
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                for r in conn.execute("SELECT id, original_path FROM documents"):
                    doc_ids.add(r["id"])
                    if r["original_path"]:
                        filenames.add(os.path.basename(r["original_path"]))
                for r in conn.execute(
                    "SELECT page_image_path, content_json FROM doc_pages"
                ):
                    if r["page_image_path"]:
                        filenames.add(os.path.basename(r["page_image_path"]))
                    cj = r["content_json"]
                    if cj and "_chart" in cj:
                        try:
                            data = json.loads(cj)
                        except (ValueError, TypeError):
                            data = None
                        charts = data.get("charts") if isinstance(data, dict) else None
                        for ch in charts or []:
                            if isinstance(ch, dict) and ch.get("image_path"):
                                filenames.add(os.path.basename(ch["image_path"]))
            finally:
                conn.close()
        except sqlite3.Error as e:
            print(f"WARN: cannot read {db_path}: {e}", file=sys.stderr)
            continue
    return filenames, doc_ids


def audit(data_dir: Path) -> dict:
    """Return a structured report of orphaned artifacts (no files touched).

    A file is an orphan unless the DB records it as owned by a live document.
    Per-tenant dirs are matched by basename against the live file set; the
    legacy global dir is matched by the doc_id embedded in its name (its page
    renders use the row id, and the row id IS the doc_id for ingested docs).
    """
    tenants_dir = data_dir / "tenants"
    live_files, live_doc_ids = _live_files(tenants_dir)

    report = {
        "data_dir": str(data_dir),
        "live_doc_count": len(live_doc_ids),
        "tenants": {},
        "global": {"live": [], "orphan": [], "unparsed": []},
    }

    # --- per-tenant dirs: a file is live iff the DB records its basename ---
    if tenants_dir.is_dir():
        for tenant_dir in sorted(tenants_dir.iterdir()):
            if not tenant_dir.is_dir():
                continue
            entry = {"documents": [], "images": []}
            for sub in ("documents", "images"):
                d = tenant_dir / sub
                if not d.is_dir():
                    continue
                for f in sorted(d.iterdir()):
                    if not f.is_file():
                        continue
                    if f.name in live_files:
                        continue  # recorded as owned by a live document
                    entry[sub].append(str(f))
            if entry["documents"] or entry["images"]:
                report["tenants"][tenant_dir.name] = entry

    # --- legacy global dir ---
    gdir = data_dir / "images"
    if gdir.is_dir():
        for f in sorted(gdir.iterdir()):
            if not f.is_file():
                continue
            m = GLOBAL_FILE_RE.match(f.name)
            if not m:
                report["global"]["unparsed"].append(str(f))
            elif m.group(1) in live_doc_ids or f.name in live_files:
                report["global"]["live"].append(str(f))
            else:
                report["global"]["orphan"].append(str(f))

    return report


def _purge(paths: list[str], apply: bool) -> int:
    removed = 0
    for p in paths:
        if apply:
            try:
                os.unlink(p)
                removed += 1
            except FileNotFoundError:
                continue
            except OSError as e:
                print(f"WARN: could not remove {p}: {e}", file=sys.stderr)
        else:
            removed += 1
    return removed


def _print_report(report: dict) -> None:
    print(f"Data dir: {report['data_dir']}")
    print(f"Live documents across tenants: {report['live_doc_count']}\n")

    total_orphans = 0
    for tid, entry in report["tenants"].items():
        n = len(entry["documents"]) + len(entry["images"])
        if not n:
            continue
        total_orphans += n
        print(f"[tenant {tid}] {n} orphan(s) "
              f"(documents={len(entry['documents'])}, images={len(entry['images'])})")
        for p in (entry["documents"] + entry["images"])[:5]:
            print(f"    {p}")
        if n > 5:
            print(f"    ... and {n - 5} more")

    g = report["global"]
    print("\n[global images dir]")
    print(f"  LIVE (kept, never deleted):  {len(g['live'])}")
    print(f"  orphan (unknown doc_id):     {len(g['orphan'])}")
    print(f"  unparsed (no doc_id):        {len(g['unparsed'])}")

    print(f"\nPer-tenant orphans: {total_orphans}")
    print(f"Global orphans:     {len(g['orphan'])}")
    print(f"Global unparsed:    {len(g['unparsed'])}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=None,
                    help="Data dir to audit (default: core.config DATA_DIR or ./data)")
    ap.add_argument("--delete", action="store_true",
                    help="Actually remove orphans (default: report only)")
    ap.add_argument("--include-global", action="store_true",
                    help="Also sweep the legacy global images dir for orphans")
    ap.add_argument("--include-unparsed", action="store_true",
                    help="With --include-global, also delete unparsed files (no doc_id)")
    args = ap.parse_args()

    data_dir = find_data_dir(args.data_dir)
    report = audit(data_dir)
    _print_report(report)

    total = 0
    for entry in report["tenants"].values():
        total += _purge(entry["documents"] + entry["images"], args.delete)

    if args.include_global:
        total += _purge(report["global"]["orphan"], args.delete)
        if args.include_unparsed:
            total += _purge(report["global"]["unparsed"], args.delete)

    verb = "Deleted" if args.delete else "Would delete"
    print(f"\n{verb} {total} orphan file(s).")
    if not args.delete:
        print("(dry-run -- pass --delete to remove; add --include-global for the shared dir)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
