#!/usr/bin/env python3
"""
Find JPG files from RAW+JPG cameras whose RAW counterpart is missing.

The script reads digiKam's SQLite database in read-only mode. Given an album or
album subtree and a camera model, it finds JPG/JPEG files from that model that
do not have a same-stem RAW file in the same album. In dry-run mode it prints
the candidates.

With --write it writes or updates explicit digiKam sidecars next to the JPGs
and marks them as rejected/no-good. It does not modify the digiKam database.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


JPEG_EXTENSIONS = {".jpg", ".jpeg"}
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
NS = {
    "x": "adobe:ns:meta/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "digiKam": "http://www.digikam.org/ns/1.0/",
    "xmpDM": "http://ns.adobe.com/xmp/1.0/DynamicMedia/",
}

for prefix, uri in NS.items():
    ET.register_namespace(prefix, uri)


@dataclass
class AlbumRef:
    album_id: int
    relative_path: str
    filesystem_path: Path | None


@dataclass
class ImageRow:
    image_id: int
    name: str
    make: str | None
    model: str | None

    @property
    def suffix(self) -> str:
        return Path(self.name).suffix.lower()

    @property
    def stem_key(self) -> str:
        return Path(self.name).stem.lower()


@dataclass
class Candidate:
    album_id: int
    relative_path: str
    image_id: int
    name: str
    make: str | None
    model: str | None
    sidecar_path: Path | None


def qname(prefix: str, name: str) -> str:
    return f"{{{NS[prefix]}}}{name}"


def open_sqlite(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def sidecar_for(path: Path) -> Path:
    return path.with_name(path.name + ".xmp")


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
            # Fall back to matching by the last dated path segment used in digiKam.
            parts = album_path.parts
            relative = None
            for i, part in enumerate(parts):
                if part.isdigit() and len(part) == 4:
                    maybe = "/" + "/".join(parts[i:])
                    row = conn.execute("SELECT id, relativePath FROM Albums WHERE relativePath = ?", (maybe,)).fetchone()
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
        rows = conn.execute(
            "SELECT id, relativePath FROM Albums WHERE relativePath = ?",
            (relative,),
        ).fetchall()
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


def read_album_models(conn: sqlite3.Connection, album_ids: list[int]) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in album_ids)
    return conn.execute(
        f"""
        SELECT ImageMetadata.make, ImageMetadata.model, COUNT(*) AS count
        FROM Images
        LEFT JOIN ImageMetadata ON ImageMetadata.imageid = Images.id
        WHERE Images.album IN ({placeholders}) AND Images.status < 3
        GROUP BY ImageMetadata.make, ImageMetadata.model
        ORDER BY count DESC, ImageMetadata.model
        """,
        album_ids,
    ).fetchall()


def choose_model_interactively(models: list[sqlite3.Row]) -> tuple[str | None, str]:
    if not sys.stdin.isatty():
        raise RuntimeError("camera model is required in non-interactive mode; pass --model or use --list-models")
    print("Camera models in album:")
    for index, row in enumerate(models, start=1):
        make = row["make"] or ""
        model = row["model"] or ""
        print(f"{index:2d}. {make} {model} ({row['count']})")
    while True:
        choice = input("Select camera model number: ").strip()
        try:
            index = int(choice)
        except ValueError:
            continue
        if 1 <= index <= len(models):
            row = models[index - 1]
            return row["make"], row["model"]


def read_images(conn: sqlite3.Connection, album_id: int) -> list[ImageRow]:
    rows = conn.execute(
        """
        SELECT
            Images.id AS image_id,
            Images.name,
            ImageMetadata.make,
            ImageMetadata.model
        FROM Images
        LEFT JOIN ImageMetadata ON ImageMetadata.imageid = Images.id
        WHERE Images.album = ? AND Images.status < 3
        ORDER BY Images.name
        """,
        (album_id,),
    ).fetchall()
    return [
        ImageRow(
            image_id=int(row["image_id"]),
            name=row["name"],
            make=row["make"],
            model=row["model"],
        )
        for row in rows
    ]


def model_matches(row: ImageRow, make: str | None, model: str) -> bool:
    if (row.model or "").casefold() != model.casefold():
        return False
    if make and (row.make or "").casefold() != make.casefold():
        return False
    return True


def find_candidates(
    images: list[ImageRow],
    album: AlbumRef,
    make: str | None,
    model: str,
    raw_extensions: set[str],
) -> list[Candidate]:
    raw_stems = {
        image.stem_key
        for image in images
        if image.suffix in raw_extensions
    }
    candidates: list[Candidate] = []
    for image in images:
        if image.suffix not in JPEG_EXTENSIONS:
            continue
        if not model_matches(image, make, model):
            continue
        if image.stem_key in raw_stems:
            continue
        sidecar_path = sidecar_for(album.filesystem_path / image.name) if album.filesystem_path else None
        candidates.append(
            Candidate(
                album_id=album.album_id,
                relative_path=album.relative_path,
                image_id=image.image_id,
                name=image.name,
                make=image.make,
                model=image.model,
                sidecar_path=sidecar_path,
            )
        )
    return candidates


def ensure_xmp_tree(path: Path) -> tuple[ET.ElementTree, ET.Element]:
    if path.exists():
        try:
            tree = ET.parse(path)
            root = tree.getroot()
        except ET.ParseError as exc:
            raise ValueError(f"Cannot parse XMP sidecar {path}: {exc}") from exc
    else:
        root = ET.Element(qname("x", "xmpmeta"))
        rdf = ET.SubElement(root, qname("rdf", "RDF"))
        ET.SubElement(rdf, qname("rdf", "Description"), {qname("rdf", "about"): ""})
        tree = ET.ElementTree(root)

    desc = root.find(f".//{qname('rdf', 'Description')}")
    if desc is None:
        rdf = root.find(f".//{qname('rdf', 'RDF')}")
        if rdf is None:
            rdf = ET.SubElement(root, qname("rdf", "RDF"))
        desc = ET.SubElement(rdf, qname("rdf", "Description"), {qname("rdf", "about"): ""})
    return tree, desc


def set_attribute_property(desc: ET.Element, tag: str, value: str) -> None:
    desc.attrib.pop(tag, None)
    node = desc.find(tag)
    if node is not None:
        desc.remove(node)
    desc.set(tag, value)


def set_text_property(desc: ET.Element, tag: str, value: str) -> None:
    desc.attrib.pop(tag, None)
    node = desc.find(tag)
    if node is None:
        node = ET.SubElement(desc, tag)
    node.text = value


def mark_sidecar_rejected(path: Path) -> None:
    tree, desc = ensure_xmp_tree(path)
    set_text_property(desc, qname("digiKam", "PickLabel"), "1")
    set_attribute_property(desc, qname("xmpDM", "pick"), "-1")
    set_attribute_property(desc, qname("xmpDM", "good"), "False")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(path.name + ".bak")
        if not backup.exists():
            import shutil

            shutil.copy2(path, backup)
    ET.indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("digikam_db", type=Path, help="digiKam digikam4.db SQLite database, read-only")
    parser.add_argument("album", help="digiKam album/subtree relative path or absolute album folder")
    parser.add_argument("--photo-root", type=Path, help="Photo root; required for XMP writes unless album is absolute.")
    parser.add_argument("--make", help="Optional exact camera make filter.")
    parser.add_argument("--model", help="Exact camera model filter. If omitted in a TTY, prompts from album models.")
    parser.add_argument("--list-models", action="store_true", help="List camera models in the album and exit.")
    parser.add_argument(
        "--no-recursive",
        action="store_true",
        help="Only inspect the exact album instead of all albums below the provided path.",
    )
    parser.add_argument(
        "--raw-extensions",
        default=",".join(sorted(DEFAULT_RAW_EXTENSIONS)),
        help="Comma-separated RAW extensions that count as counterparts.",
    )
    parser.add_argument("--write", action="store_true", help="Mark candidate JPG XMP sidecars as rejected/no-good.")
    args = parser.parse_args(argv)

    db = args.digikam_db.expanduser()
    if not db.exists():
        print(f"digiKam database not found: {db}", file=sys.stderr)
        return 2
    photo_root = args.photo_root.expanduser() if args.photo_root else None

    conn = open_sqlite(db)
    try:
        albums = find_albums(conn, args.album, photo_root, recursive=not args.no_recursive)
        models = read_album_models(conn, [album.album_id for album in albums])
        if args.list_models:
            print(f"Albums: {len(albums)}")
            print(f"Root: {albums[0].relative_path}")
            for index, row in enumerate(models, start=1):
                print(f"{index:2d}. {row['make'] or ''}\t{row['model'] or ''}\t{row['count']}")
            return 0
        make = args.make
        model = args.model
        if not model:
            make, model = choose_model_interactively(models)
        if not model:
            print("camera model is required", file=sys.stderr)
            return 2
        album_images = [(album, read_images(conn, album.album_id)) for album in albums]
    finally:
        conn.close()

    raw_extensions = parse_extensions(args.raw_extensions)
    candidates: list[Candidate] = []
    for album, images in album_images:
        candidates.extend(find_candidates(images, album, make, model, raw_extensions))

    print(f"Albums inspected: {len(albums)}")
    print(f"Root: {albums[0].relative_path}")
    print(f"Camera: {make or '*'} {model}")
    print(f"JPG candidates missing RAW counterpart: {len(candidates)}")
    for candidate in candidates:
        print(
            f"{candidate.relative_path}/{candidate.name}\t"
            f"{candidate.make or ''}\t{candidate.model or ''}\t"
            f"{candidate.sidecar_path or '(no sidecar path)'}"
        )

    if not args.write:
        print("Dry run only. Re-run with --write to mark candidate JPG sidecars as rejected/no-good.")
        return 0

    if any(candidate.sidecar_path is None for candidate in candidates):
        print("--write requires --photo-root or an absolute album folder path", file=sys.stderr)
        return 2

    errors = 0
    for candidate in candidates:
        if not candidate.sidecar_path:
            errors += 1
            print(f"error: no sidecar path for {candidate.name}", file=sys.stderr)
            continue
        try:
            mark_sidecar_rejected(candidate.sidecar_path)
        except Exception as exc:  # noqa: BLE001 - continue per file.
            errors += 1
            print(f"error: {candidate.sidecar_path}: {exc}", file=sys.stderr)

    print(f"XMP sidecars updated: {len(candidates) - errors}")
    print(f"Errors: {errors}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
