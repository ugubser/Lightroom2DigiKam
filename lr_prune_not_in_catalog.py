#!/usr/bin/env python3
"""
Find files under a disk tree that are not referenced by a Lightroom catalog.

By default this is a dry run. Use --quarantine to move unreferenced files into
a review folder, or --delete to permanently remove them.

The comparison is path-based after applying --path-prefix mappings. This is
intentional: duplicate filenames in different folders are not assumed to be the
same photo.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path


DEFAULT_MEDIA_EXTENSIONS = {
    ".3fr",
    ".ari",
    ".arw",
    ".avif",
    ".bay",
    ".bmp",
    ".cr2",
    ".cr3",
    ".crw",
    ".dcr",
    ".dng",
    ".erf",
    ".fff",
    ".gif",
    ".heic",
    ".heif",
    ".iiq",
    ".jpeg",
    ".jpg",
    ".jxl",
    ".k25",
    ".kdc",
    ".m4v",
    ".mef",
    ".mos",
    ".mov",
    ".mp4",
    ".mrw",
    ".nef",
    ".nrw",
    ".orf",
    ".pef",
    ".png",
    ".ptx",
    ".pxn",
    ".raf",
    ".raw",
    ".rwl",
    ".rw2",
    ".sr2",
    ".srf",
    ".srw",
    ".tif",
    ".tiff",
    ".webp",
    ".x3f",
}

RAW_EXTENSIONS = {
    ".3fr",
    ".ari",
    ".arw",
    ".bay",
    ".cr2",
    ".cr3",
    ".crw",
    ".dcr",
    ".dng",
    ".erf",
    ".fff",
    ".iiq",
    ".k25",
    ".kdc",
    ".mef",
    ".mos",
    ".mrw",
    ".nef",
    ".nrw",
    ".orf",
    ".pef",
    ".ptx",
    ".pxn",
    ".raf",
    ".raw",
    ".rwl",
    ".rw2",
    ".sr2",
    ".srf",
    ".srw",
    ".x3f",
}

IGNORED_NAMES = {
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
    "digikam.uuid",
    "lightroom-xmp-migration-manifest.json",
}


@dataclass(frozen=True)
class PruneCandidate:
    path: Path
    size: int


@dataclass(frozen=True)
class PrunePlan:
    candidates: list[PruneCandidate]
    preserved_raw_companions: list[Path]


def parse_prefix_mapping(value: str) -> tuple[Path, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("expected OLD=NEW")
    old, new = value.split("=", 1)
    if not old or not new:
        raise argparse.ArgumentTypeError("expected non-empty OLD=NEW")
    return Path(old).expanduser(), Path(new).expanduser()


def apply_path_prefixes(path: Path, mappings: list[tuple[Path, Path]]) -> Path:
    raw = str(path)
    for old, new in mappings:
        old_s = str(old)
        if raw == old_s or raw.startswith(old_s.rstrip(os.sep) + os.sep):
            return Path(str(new) + raw[len(old_s) :])
    return path


def catalog_paths(catalog: Path, mappings: list[tuple[Path, Path]]) -> set[Path]:
    conn = sqlite3.connect(f"file:{catalog}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        sql = """
            SELECT rf.absolutePath || fo.pathFromRoot || fi.baseName ||
                CASE WHEN fi.extension IS NULL OR fi.extension = ''
                     THEN ''
                     ELSE '.' || fi.extension
                END AS full_path
            FROM Adobe_images i
            JOIN AgLibraryFile fi ON i.rootFile = fi.id_local
            JOIN AgLibraryFolder fo ON fi.folder = fo.id_local
            JOIN AgLibraryRootFolder rf ON fo.rootFolder = rf.id_local
        """
        return {apply_path_prefixes(Path(row["full_path"]), mappings) for row in conn.execute(sql)}
    finally:
        conn.close()


def sidecar_image_path(path: Path) -> Path | None:
    if path.suffix.lower() != ".xmp":
        return None
    return Path(str(path)[: -len(".xmp")])


def should_consider(path: Path, media_exts: set[str], include_xmp: bool) -> bool:
    if path.name in IGNORED_NAMES:
        return False
    if any(part in {".dtrash", ".Trashes", ".Spotlight-V100", ".fseventsd"} for part in path.parts):
        return False
    suffix = path.suffix.lower()
    return suffix in media_exts or (include_xmp and suffix == ".xmp")


def raw_companion_keys(root: Path, media_exts: set[str]) -> set[tuple[Path, str]]:
    keys: set[tuple[Path, str]] = set()
    raw_exts = RAW_EXTENSIONS & media_exts
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in raw_exts:
            keys.add((path.parent, path.stem.lower()))
    return keys


def find_candidates(
    root: Path,
    referenced: set[Path],
    media_exts: set[str],
    include_xmp: bool,
) -> PrunePlan:
    candidates: list[PruneCandidate] = []
    preserved_raw_companions: list[Path] = []
    raw_keys = raw_companion_keys(root, media_exts)

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if not should_consider(path, media_exts, include_xmp):
            continue

        if path in referenced:
            continue

        image_path = sidecar_image_path(path)
        if image_path is not None and image_path in referenced:
            continue

        if path.suffix.lower() not in RAW_EXTENSIONS and (path.parent, path.stem.lower()) in raw_keys:
            preserved_raw_companions.append(path)
            continue

        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        candidates.append(PruneCandidate(path=path, size=size))
    return PrunePlan(
        candidates=sorted(candidates, key=lambda c: str(c.path)),
        preserved_raw_companions=sorted(preserved_raw_companions, key=lambda p: str(p)),
    )


def quarantine_path(path: Path, root: Path, quarantine_root: Path) -> Path:
    rel = path.relative_to(root)
    target = quarantine_root / rel
    if not target.exists():
        return target

    stem = target.name
    for idx in range(1, 100000):
        candidate = target.with_name(f"{stem}.duplicate-{idx}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not find available quarantine path for {path}")


def write_report(path: Path, plan: PrunePlan) -> None:
    payload = {
        "count": len(plan.candidates),
        "total_bytes": sum(c.size for c in plan.candidates),
        "preserved_raw_companion_count": len(plan.preserved_raw_companions),
        "files": [{"path": str(c.path), "size": c.size} for c in plan.candidates],
        "preserved_raw_companions": [str(path) for path in plan.preserved_raw_companions],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path, help="Lightroom .lrcat SQLite catalog")
    parser.add_argument("root", type=Path, help="Disk tree to inspect")
    parser.add_argument(
        "--path-prefix",
        action="append",
        default=[],
        type=parse_prefix_mapping,
        metavar="OLD=NEW",
        help="Map Lightroom source paths to the disk tree. Can be repeated.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("lightroom-prune-candidates.json"),
        help="JSON report path.",
    )
    parser.add_argument(
        "--include-xmp",
        action="store_true",
        help="Also prune orphan .xmp files whose corresponding image is not in the catalog.",
    )
    parser.add_argument(
        "--extension",
        action="append",
        help="Restrict media extensions, e.g. --extension jpg --extension dng.",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--quarantine", type=Path, help="Move candidates to this review folder.")
    action.add_argument("--delete", action="store_true", help="Permanently delete candidates.")
    args = parser.parse_args(argv)

    if not args.catalog.exists():
        print(f"Catalog not found: {args.catalog}", file=sys.stderr)
        return 2
    if not args.root.exists():
        print(f"Root not found: {args.root}", file=sys.stderr)
        return 2

    media_exts = DEFAULT_MEDIA_EXTENSIONS
    if args.extension:
        media_exts = {("." + ext.lower().lstrip(".")) for ext in args.extension}

    referenced = catalog_paths(args.catalog, args.path_prefix)
    plan = find_candidates(args.root, referenced, media_exts, args.include_xmp)
    write_report(args.report, plan)

    candidates = plan.candidates
    total_bytes = sum(c.size for c in candidates)
    print(f"Lightroom referenced files after mapping: {len(referenced)}")
    print(f"Unreferenced candidates under {args.root}: {len(candidates)}")
    print(f"Preserved unreferenced RAW companions: {len(plan.preserved_raw_companions)}")
    print(f"Candidate bytes: {total_bytes}")
    print(f"Report: {args.report}")

    if args.quarantine:
        args.quarantine.mkdir(parents=True, exist_ok=True)
        moved = 0
        for candidate in candidates:
            target = quarantine_path(candidate.path, args.root, args.quarantine)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(candidate.path), str(target))
            moved += 1
        print(f"Moved to quarantine: {moved}")
    elif args.delete:
        deleted = 0
        for candidate in candidates:
            candidate.path.unlink()
            deleted += 1
        print(f"Deleted: {deleted}")
    else:
        print("Dry run only. Use --quarantine REVIEW_FOLDER or --delete to act.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
