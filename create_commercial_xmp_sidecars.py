#!/usr/bin/env python3
"""
Create commercial-compatible RAW XMP sidecars from digiKam explicit sidecars.

digiKam's explicit sidecar form is:

    filename.rawext.xmp

Many RAW tools expect the commercial-compatible form:

    filename.xmp

This script reads digiKam's SQLite database in read-only mode, finds RAW files
in an album or album subtree, and creates missing commercial sidecars by
copying the existing explicit digiKam sidecar. It skips JPG/JPEG files and
does not modify the digiKam database.

By default this is a dry run.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path


DEFAULT_RAW_EXTENSIONS = {
    ".3fr",
    ".arw",
    ".cr2",
    ".cr3",
    ".dng",
    ".nef",
    ".orf",
    ".raf",
    ".raw",
    ".rw2",
}


@dataclass
class AlbumRef:
    album_id: int
    relative_path: str
    filesystem_path: Path | None


@dataclass
class RawSidecarPlan:
    album_id: int
    relative_path: str
    raw_name: str
    explicit_sidecar: Path
    commercial_sidecar: Path
    action: str
    reason: str


def open_sqlite(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def normalize_relative_album(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        return stripped
    if not stripped.startswith("/"):
        stripped = "/" + stripped
    return stripped.rstrip("/") or "/"


def resolve_album_arg(conn: sqlite3.Connection, album_arg: str, photo_root: Path | None) -> tuple[str, Path | None]:
    album_path = Path(album_arg).expanduser()
    filesystem_path: Path | None = None
    if album_arg.startswith("/") and album_path.exists():
        filesystem_path = album_path
        if photo_root:
            try:
                relative = "/" + str(album_path.resolve().relative_to(photo_root.resolve()))
            except ValueError as exc:
                raise ValueError(f"album path is not under --photo-root: {album_path}") from exc
        else:
            parts = album_path.parts
            for i, part in enumerate(parts):
                if part.isdigit() and len(part) == 4:
                    maybe = "/" + "/".join(parts[i:])
                    row = conn.execute("SELECT relativePath FROM Albums WHERE relativePath = ?", (maybe,)).fetchone()
                    if row:
                        return row["relativePath"], filesystem_path
            raise ValueError("absolute album path requires --photo-root unless the relative digiKam path can be inferred")
    else:
        relative = normalize_relative_album(album_arg)
        if photo_root:
            filesystem_path = photo_root / relative.lstrip("/")
    return relative, filesystem_path


def find_albums(conn: sqlite3.Connection, album_arg: str, photo_root: Path | None, recursive: bool) -> list[AlbumRef]:
    relative, filesystem_path = resolve_album_arg(conn, album_arg, photo_root)
    if recursive:
        rows = conn.execute(
            """
            SELECT id, relativePath
            FROM Albums
            WHERE relativePath = ?
               OR relativePath LIKE ?
            ORDER BY relativePath
            """,
            (relative, relative.rstrip("/") + "/%"),
        ).fetchall()
    else:
        rows = conn.execute("SELECT id, relativePath FROM Albums WHERE relativePath = ?", (relative,)).fetchall()
    if not rows:
        raise ValueError(f"album not found in digiKam database: {relative}")

    albums: list[AlbumRef] = []
    for row in rows:
        album_fs_path = None
        if filesystem_path:
            suffix = row["relativePath"][len(relative) :].lstrip("/")
            album_fs_path = filesystem_path / suffix if suffix else filesystem_path
        albums.append(AlbumRef(int(row["id"]), row["relativePath"], album_fs_path))
    return albums


def read_image_names(conn: sqlite3.Connection, album_id: int) -> list[str]:
    rows = conn.execute(
        """
        SELECT name
        FROM Images
        WHERE album = ?
          AND status < 3
        ORDER BY name
        """,
        (album_id,),
    ).fetchall()
    return [row["name"] for row in rows]


def parse_extensions(value: str) -> set[str]:
    extensions = set()
    for part in value.split(","):
        part = part.strip().lower()
        if not part:
            continue
        if not part.startswith("."):
            part = "." + part
        extensions.add(part)
    return extensions


def explicit_sidecar_for(raw_path: Path) -> Path:
    return raw_path.with_name(raw_path.name + ".xmp")


def commercial_sidecar_for(raw_path: Path) -> Path:
    return raw_path.with_suffix(".xmp")


def build_plan(
    album: AlbumRef,
    names: list[str],
    raw_extensions: set[str],
    overwrite: bool,
) -> list[RawSidecarPlan]:
    if album.filesystem_path is None:
        raise ValueError("photo root is required to resolve sidecar paths")

    plans: list[RawSidecarPlan] = []
    for name in names:
        raw_path = album.filesystem_path / name
        if raw_path.suffix.lower() not in raw_extensions:
            continue
        explicit = explicit_sidecar_for(raw_path)
        commercial = commercial_sidecar_for(raw_path)
        if not explicit.exists():
            plans.append(
                RawSidecarPlan(
                    album.album_id,
                    album.relative_path,
                    name,
                    explicit,
                    commercial,
                    "skip",
                    "explicit digiKam sidecar is missing",
                )
            )
            continue
        if commercial.exists() and not overwrite:
            plans.append(
                RawSidecarPlan(
                    album.album_id,
                    album.relative_path,
                    name,
                    explicit,
                    commercial,
                    "skip",
                    "commercial sidecar already exists",
                )
            )
            continue
        plans.append(
            RawSidecarPlan(
                album.album_id,
                album.relative_path,
                name,
                explicit,
                commercial,
                "copy",
                "create commercial sidecar" if not commercial.exists() else "overwrite commercial sidecar",
            )
        )
    return plans


def write_plans(plans: list[RawSidecarPlan]) -> tuple[int, int]:
    copied = 0
    errors = 0
    for plan in plans:
        if plan.action != "copy":
            continue
        try:
            if plan.commercial_sidecar.exists():
                backup = plan.commercial_sidecar.with_name(plan.commercial_sidecar.name + ".bak")
                if not backup.exists():
                    shutil.copy2(plan.commercial_sidecar, backup)
            shutil.copy2(plan.explicit_sidecar, plan.commercial_sidecar)
            copied += 1
        except Exception as exc:  # noqa: BLE001 - continue per file.
            errors += 1
            print(f"error: {plan.commercial_sidecar}: {exc}", file=sys.stderr)
    return copied, errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("digikam_db", type=Path, help="digiKam digikam4.db SQLite database, read-only")
    parser.add_argument("album", help="digiKam album/subtree relative path or absolute album folder")
    parser.add_argument("--photo-root", type=Path, required=True, help="Photo root used to locate sidecar files.")
    parser.add_argument(
        "--raw-extensions",
        default=",".join(sorted(DEFAULT_RAW_EXTENSIONS)),
        help="Comma-separated RAW extensions to process.",
    )
    parser.add_argument(
        "--no-recursive",
        action="store_true",
        help="Only inspect the exact album instead of all albums below the provided path.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing commercial sidecars, making .bak backups first.",
    )
    parser.add_argument("--write", action="store_true", help="Actually create missing commercial sidecars.")
    args = parser.parse_args(argv)

    db = args.digikam_db.expanduser()
    photo_root = args.photo_root.expanduser()
    if not db.exists():
        print(f"digiKam database not found: {db}", file=sys.stderr)
        return 2
    if not photo_root.exists():
        print(f"photo root not found: {photo_root}", file=sys.stderr)
        return 2

    conn = open_sqlite(db)
    try:
        albums = find_albums(conn, args.album, photo_root, recursive=not args.no_recursive)
        raw_extensions = parse_extensions(args.raw_extensions)
        plans: list[RawSidecarPlan] = []
        for album in albums:
            plans.extend(build_plan(album, read_image_names(conn, album.album_id), raw_extensions, args.overwrite))
    finally:
        conn.close()

    copy_plans = [plan for plan in plans if plan.action == "copy"]
    missing_explicit = [plan for plan in plans if plan.reason == "explicit digiKam sidecar is missing"]
    existing_commercial = [plan for plan in plans if plan.reason == "commercial sidecar already exists"]

    print(f"Albums inspected: {len(albums)}")
    print(f"RAW files considered: {len(plans)}")
    print(f"Commercial sidecars to create/copy: {len(copy_plans)}")
    print(f"Skipped, commercial already exists: {len(existing_commercial)}")
    print(f"Skipped, explicit sidecar missing: {len(missing_explicit)}")
    for plan in copy_plans[:100]:
        print(f"COPY: {plan.relative_path}/{plan.raw_name}\t{plan.explicit_sidecar} -> {plan.commercial_sidecar}")
    if len(copy_plans) > 100:
        print(f"... and {len(copy_plans) - 100} more copy candidates")

    if not args.write:
        print("Dry run only. Re-run with --write to create commercial sidecars.")
        return 0

    copied, errors = write_plans(plans)
    print(f"Commercial sidecars copied: {copied}")
    print(f"Errors: {errors}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
