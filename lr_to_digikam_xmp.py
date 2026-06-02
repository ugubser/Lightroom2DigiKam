#!/usr/bin/env python3
"""
Export Lightroom catalog-only organization data into XMP sidecars for digiKam.

This script never writes into image files and never writes to the Lightroom
catalog. It reads the catalog, groups Lightroom image rows by physical file,
then appends migration tags and pick labels to XMP sidecars.

Examples:
  Dry run:
    python3 lr_to_digikam_xmp.py "/path/to/Catalog.lrcat"

  Write to a copied library whose root path changed:
    python3 lr_to_digikam_xmp.py "/path/to/Catalog.lrcat" \
      --path-prefix "/Original/Photo Root=/Copy/Photo Root" --write

Missing Lightroom capture dates are inferred from year/month/day path segments
by default. Use --no-infer-missing-dates-from-path to disable that behavior.

Sidecar policy:
  * Existing exact sidecars are updated: IMG_0001.JPG.xmp.
  * New sidecars use digiKam's explicit form: IMG_0001.JPG.xmp.
  * Existing Lightroom commercial RAW sidecars such as IMG_0001.xmp are used
    only as templates for new explicit RAW sidecars and are not modified.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


NS = {
    "x": "adobe:ns:meta/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dc": "http://purl.org/dc/elements/1.1/",
    "exif": "http://ns.adobe.com/exif/1.0/",
    "lr": "http://ns.adobe.com/lightroom/1.0/",
    "photoshop": "http://ns.adobe.com/photoshop/1.0/",
    "digiKam": "http://www.digikam.org/ns/1.0/",
    "tiff": "http://ns.adobe.com/tiff/1.0/",
    "xmp": "http://ns.adobe.com/xap/1.0/",
    "xmpDM": "http://ns.adobe.com/xmp/1.0/DynamicMedia/",
}

for prefix, uri in NS.items():
    ET.register_namespace(prefix, uri)

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


@dataclass
class ImageRow:
    image_id: int
    path: Path
    rating: int | None = None
    pick: int | None = None
    color_label: str | None = None
    capture_time: str | None = None
    orientation: str | None = None
    master_image: int | None = None
    copy_name: str | None = None


LIGHTROOM_ORIENTATION_TO_EXIF = {
    "AB": "1",  # Horizontal / normal
    "BA": "2",  # Mirror horizontal
    "CD": "3",  # Rotate 180
    "DC": "4",  # Mirror vertical
    "CB": "5",  # Mirror horizontal and rotate 270 CW
    "BC": "6",  # Rotate 90 CW
    "AD": "7",  # Mirror horizontal and rotate 90 CW
    "DA": "8",  # Rotate 270 CW
}


@dataclass
class FilePlan:
    source_path: Path
    target_path: Path
    image_ids: set[int] = field(default_factory=set)
    tags: set[str] = field(default_factory=set)
    picks: set[int] = field(default_factory=set)
    capture_times: set[str] = field(default_factory=set)
    inferred_capture_time: str | None = None
    orientations: set[str] = field(default_factory=set)
    ratings: set[int] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)


def qname(prefix: str, name: str) -> str:
    return f"{{{NS[prefix]}}}{name}"


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row[0] for row in rows}


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.DatabaseError:
        return set()


def has_table(conn: sqlite3.Connection, name: str) -> bool:
    names = {n.lower(): n for n in table_names(conn)}
    return name.lower() in names


def sql_value_expr(table_alias: str, col: str, fallback: str = "NULL") -> str:
    return f"{table_alias}.{col}" if col else fallback


def read_images(conn: sqlite3.Connection) -> list[ImageRow]:
    image_cols = columns(conn, "Adobe_images")
    file_cols = columns(conn, "AgLibraryFile")

    rating = "rating" if "rating" in image_cols else ""
    pick = "pick" if "pick" in image_cols else ""
    color = "colorLabels" if "colorLabels" in image_cols else ""
    master = "masterImage" if "masterImage" in image_cols else ""
    copy_name = "copyName" if "copyName" in image_cols else ""
    capture = "captureTime" if "captureTime" in image_cols else ""
    orientation = "orientation" if "orientation" in image_cols else ""
    extension_expr = "fi.extension" if "extension" in file_cols else "''"

    sql = f"""
        SELECT
            i.id_local,
            rf.absolutePath || fo.pathFromRoot || fi.baseName ||
                CASE WHEN {extension_expr} IS NULL OR {extension_expr} = ''
                     THEN ''
                     ELSE '.' || {extension_expr}
                END AS full_path,
            {sql_value_expr('i', rating)} AS rating,
            {sql_value_expr('i', pick)} AS pick,
            {sql_value_expr('i', color)} AS color_label,
            {sql_value_expr('i', capture)} AS capture_time,
            {sql_value_expr('i', orientation)} AS orientation,
            {sql_value_expr('i', master)} AS master_image,
            {sql_value_expr('i', copy_name)} AS copy_name
        FROM Adobe_images i
        JOIN AgLibraryFile fi ON i.rootFile = fi.id_local
        JOIN AgLibraryFolder fo ON fi.folder = fo.id_local
        JOIN AgLibraryRootFolder rf ON fo.rootFolder = rf.id_local
    """
    rows = []
    for row in conn.execute(sql):
        if not row["full_path"]:
            continue
        rows.append(
            ImageRow(
                image_id=int(row["id_local"]),
                path=Path(row["full_path"]),
                rating=row["rating"],
                pick=row["pick"],
                color_label=row["color_label"],
                capture_time=row["capture_time"],
                orientation=row["orientation"],
                master_image=row["master_image"],
                copy_name=row["copy_name"],
            )
        )
    return rows


def read_keyword_paths(conn: sqlite3.Connection) -> dict[int, str]:
    if not has_table(conn, "AgLibraryKeyword"):
        return {}

    rows = conn.execute(
        """
        SELECT id_local, name, parent
        FROM AgLibraryKeyword
        """
    ).fetchall()
    names = {int(row["id_local"]): row["name"] or "" for row in rows}
    parents = {int(row["id_local"]): row["parent"] for row in rows}

    def build(keyword_id: int, seen: set[int] | None = None) -> str:
        seen = seen or set()
        if keyword_id in seen:
            return names.get(keyword_id, str(keyword_id))
        seen.add(keyword_id)
        parent = parents.get(keyword_id)
        name = names.get(keyword_id, str(keyword_id))
        if parent is None or parent == "" or parent not in names:
            return name
        parent_path = build(int(parent), seen)
        if not parent_path:
            return name
        return f"{parent_path}/{name}"

    return {keyword_id: build(keyword_id) for keyword_id in names}


def read_keyword_members(conn: sqlite3.Connection) -> dict[int, set[int]]:
    result: dict[int, set[int]] = defaultdict(set)
    if not has_table(conn, "AgLibraryKeywordImage"):
        return result
    for row in conn.execute("SELECT image, tag FROM AgLibraryKeywordImage"):
        if row["image"] is not None and row["tag"] is not None:
            result[int(row["image"])].add(int(row["tag"]))
    return result


def read_collection_paths(conn: sqlite3.Connection) -> dict[int, str]:
    if not has_table(conn, "AgLibraryCollection"):
        return {}

    rows = conn.execute(
        """
        SELECT id_local, name, parent
        FROM AgLibraryCollection
        """
    ).fetchall()
    names = {int(row["id_local"]): row["name"] or f"Collection {row['id_local']}" for row in rows}
    parents = {int(row["id_local"]): row["parent"] for row in rows}

    def build(collection_id: int, seen: set[int] | None = None) -> str:
        seen = seen or set()
        if collection_id in seen:
            return names.get(collection_id, str(collection_id))
        seen.add(collection_id)
        parent = parents.get(collection_id)
        name = names.get(collection_id, str(collection_id))
        if parent is None or parent == "" or parent not in names:
            return name
        return f"{build(int(parent), seen)}/{name}"

    return {collection_id: build(collection_id) for collection_id in names}


def read_collection_members(conn: sqlite3.Connection) -> dict[int, set[int]]:
    result: dict[int, set[int]] = defaultdict(set)
    names = table_names(conn)
    lower_to_actual = {name.lower(): name for name in names}

    table = lower_to_actual.get("aglibrarycollectionimage")
    if table:
        for row in conn.execute(f"SELECT collection, image FROM {table}"):
            if row["collection"] is not None and row["image"] is not None:
                result[int(row["image"])].add(int(row["collection"]))

    table = lower_to_actual.get("aglibrarycollectioncontent")
    if table:
        for row in conn.execute(f"SELECT collection, content FROM {table}"):
            if row["collection"] is None or row["content"] is None:
                continue
            try:
                image_id = int(row["content"])
            except (TypeError, ValueError):
                # Smart collections can store serialized criteria in content.
                continue
            result[image_id].add(int(row["collection"]))

    return result


def read_stack_rows(conn: sqlite3.Connection) -> dict[int, tuple[int, int | None]]:
    if not has_table(conn, "AgLibraryFolderStackImage"):
        return {}

    out = {}
    for row in conn.execute("SELECT image, stack, position FROM AgLibraryFolderStackImage"):
        if row["image"] is not None and row["stack"] is not None:
            out[int(row["image"])] = (int(row["stack"]), row["position"])
    return out


def read_smart_collection_definitions(conn: sqlite3.Connection) -> list[dict[str, object]]:
    if not has_table(conn, "AgLibraryCollection"):
        return []

    col_names = columns(conn, "AgLibraryCollection")
    interesting = [
        col
        for col in sorted(col_names)
        if any(token in col.lower() for token in ("smart", "search", "rule", "query", "criteria"))
    ]
    if not interesting:
        return []

    select_cols = ["id_local", "name", "parent", *interesting]
    sql = "SELECT " + ", ".join(select_cols) + " FROM AgLibraryCollection"
    rows = []
    for row in conn.execute(sql):
        payload = {col: row[col] for col in select_cols if row[col] not in (None, "")}
        if any(col in payload for col in interesting):
            rows.append(payload)
    return rows


def apply_path_prefixes(path: Path, mappings: list[tuple[Path, Path]]) -> Path:
    raw = str(path)
    for old, new in mappings:
        old_s = str(old)
        if raw == old_s or raw.startswith(old_s.rstrip(os.sep) + os.sep):
            return Path(str(new) + raw[len(old_s) :])
    return path


def lr_pick_to_digikam(pick: int | None) -> int | None:
    if pick is None:
        return None
    try:
        value = int(pick)
    except (TypeError, ValueError):
        return None
    if value == 1:
        return 3  # AcceptedLabel
    if value == -1:
        return 1  # RejectedLabel
    return None


def sidecar_for(path: Path) -> Path:
    return Path(str(path) + ".xmp")


def template_sidecar_for(path: Path, sidecar: Path) -> Path | None:
    """Return an existing Lightroom-style sidecar to seed a new explicit sidecar."""
    if sidecar.exists() or path.suffix.lower() not in RAW_EXTENSIONS:
        return None

    commercial = path.with_suffix(".xmp")
    if commercial.exists() and commercial != sidecar:
        return commercial
    return None


def add_tag(plan: FilePlan, tag: str) -> None:
    clean = "/".join(part.strip() for part in tag.split("/") if part and part.strip())
    if clean:
        plan.tags.add(clean)


def infer_capture_time_from_path(path: Path) -> tuple[str | None, str | None]:
    """Infer a sortable capture date from year/month/day path segments."""
    parts = path.parts
    for idx, part in enumerate(parts):
        if not re.fullmatch(r"(19|20)\d{2}", part):
            continue
        year = int(part)
        month = 1
        day = 1
        precision = "year"

        if idx + 1 < len(parts) and re.fullmatch(r"\d{1,2}", parts[idx + 1]):
            candidate = int(parts[idx + 1])
            if 1 <= candidate <= 12:
                month = candidate
                precision = "month"

        if idx + 2 < len(parts) and re.fullmatch(r"\d{1,2}", parts[idx + 2]):
            candidate = int(parts[idx + 2])
            if 1 <= candidate <= 31:
                day = candidate
                precision = "day"

        return f"{year:04d}-{month:02d}-{day:02d}T00:00:00", precision

    return None, None


def build_plan(
    images: list[ImageRow],
    collection_paths: dict[int, str],
    members: dict[int, set[int]],
    keyword_paths: dict[int, str],
    keyword_members: dict[int, set[int]],
    path_mappings: list[tuple[Path, Path]],
    tag_prefix: str,
    infer_missing_dates: bool,
) -> dict[Path, FilePlan]:
    plans: dict[Path, FilePlan] = {}

    for img in images:
        target = apply_path_prefixes(img.path, path_mappings)
        plan = plans.setdefault(
            target,
            FilePlan(source_path=img.path, target_path=target),
        )
        plan.image_ids.add(img.image_id)

        for collection_id in members.get(img.image_id, set()):
            path = collection_paths.get(collection_id)
            if path:
                add_tag(plan, f"{tag_prefix}/Collections/{path}")

        for keyword_id in keyword_members.get(img.image_id, set()):
            path = keyword_paths.get(keyword_id)
            if path:
                add_tag(plan, path)

        pick = lr_pick_to_digikam(img.pick)
        if pick is not None:
            plan.picks.add(pick)

        if img.capture_time:
            plan.capture_times.add(img.capture_time)
        elif infer_missing_dates:
            inferred, precision = infer_capture_time_from_path(target)
            if inferred:
                plan.inferred_capture_time = inferred
                plan.capture_times.add(inferred)
                plan.notes.append(f"Capture time inferred from path with {precision} precision.")

        if img.orientation:
            mapped = LIGHTROOM_ORIENTATION_TO_EXIF.get(str(img.orientation))
            if mapped:
                plan.orientations.add(mapped)
            else:
                plan.notes.append(f"Unsupported Lightroom orientation value: {img.orientation}.")

        if img.rating is not None:
            try:
                plan.ratings.add(int(img.rating))
            except (TypeError, ValueError):
                pass

        # Native digiKam groups are applied separately by apply_digikam_groups.py.
        # Do not create extra _Lightroom marker tags for stacks or virtual copies.

    for plan in plans.values():
        if len(plan.image_ids) > 1:
            plan.notes.append(
                "Multiple Lightroom image rows map to this file; sidecar metadata was merged."
            )
        if len(plan.picks) > 1:
            plan.notes.append(
                "Conflicting Lightroom pick/reject values for this file; pick label was not written."
            )
        if len(plan.capture_times) > 1:
            plan.notes.append(
                "Conflicting Lightroom capture times for this file; capture date was not written."
            )
        if len(plan.orientations) > 1:
            plan.notes.append(
                "Conflicting Lightroom orientations for this file; orientation was not written."
            )
        if len(plan.ratings) > 1:
            plan.notes.append(
                "Conflicting Lightroom ratings for this file; rating was not written."
            )

    return plans


def ensure_xmp_tree(path: Path, template_path: Path | None = None) -> tuple[ET.ElementTree, ET.Element]:
    read_path = path if path.exists() else template_path
    if read_path and read_path.exists():
        try:
            tree = ET.parse(read_path)
            root = tree.getroot()
        except ET.ParseError as exc:
            raise ValueError(f"Cannot parse XMP sidecar {read_path}: {exc}") from exc
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


def get_or_create_container(desc: ET.Element, tag: str, container: str) -> ET.Element:
    prop = desc.find(tag)
    if prop is None:
        prop = ET.SubElement(desc, tag)

    for child_name in ("Bag", "Seq", "Alt"):
        child = prop.find(qname("rdf", child_name))
        if child is not None:
            return child

    return ET.SubElement(prop, qname("rdf", container))


def existing_container_values(container: ET.Element) -> set[str]:
    values = set()
    for item in container.findall(qname("rdf", "li")):
        if item.text:
            values.add(item.text)
    return values


def append_unique(container: ET.Element, values: Iterable[str]) -> None:
    existing = existing_container_values(container)
    for value in sorted(v for v in values if v):
        if value in existing:
            continue
        ET.SubElement(container, qname("rdf", "li")).text = value
        existing.add(value)


def set_text_or_remove(desc: ET.Element, tag: str, value: str | None) -> None:
    node = desc.find(tag)
    if value is None:
        if node is not None:
            desc.remove(node)
        return
    if node is None:
        node = ET.SubElement(desc, tag)
    node.text = value


def set_property_or_remove(desc: ET.Element, tag: str, value: str | None) -> None:
    """Set an XMP scalar once, using RDF attribute form."""
    desc.attrib.pop(tag, None)
    node = desc.find(tag)
    if node is not None:
        desc.remove(node)
    if value is not None:
        desc.set(tag, value)


def set_text_property_or_remove(desc: ET.Element, tag: str, value: str | None) -> None:
    desc.attrib.pop(tag, None)
    set_text_or_remove(desc, tag, value)


def update_xmp(
    path: Path,
    plan: FilePlan,
    write: bool,
    backup: bool,
    template_path: Path | None = None,
) -> str:
    tree, desc = ensure_xmp_tree(path, template_path=template_path)

    if plan.tags:
        digi = get_or_create_container(desc, qname("digiKam", "TagsList"), "Seq")
        append_unique(digi, plan.tags)

        lr = get_or_create_container(desc, qname("lr", "hierarchicalSubject"), "Bag")
        append_unique(lr, [tag.replace("/", "|") for tag in plan.tags])

        dc = get_or_create_container(desc, qname("dc", "subject"), "Bag")
        append_unique(dc, [tag.split("/")[-1] for tag in plan.tags])

    if len(plan.picks) == 1:
        pick = next(iter(plan.picks))
        set_text_or_remove(desc, qname("digiKam", "PickLabel"), str(pick))
        if pick == 3:
            set_property_or_remove(desc, qname("xmpDM", "pick"), "1")
            set_property_or_remove(desc, qname("xmpDM", "good"), "True")
        elif pick == 1:
            set_property_or_remove(desc, qname("xmpDM", "pick"), "-1")
            set_property_or_remove(desc, qname("xmpDM", "good"), "False")

    if len(plan.ratings) == 1:
        rating = next(iter(plan.ratings))
        set_property_or_remove(desc, qname("xmp", "Rating"), str(rating))

    if len(plan.capture_times) == 1:
        capture_time = next(iter(plan.capture_times))
        set_property_or_remove(desc, qname("exif", "DateTimeOriginal"), capture_time)
        set_property_or_remove(desc, qname("exif", "DateTimeDigitized"), capture_time)
        set_property_or_remove(desc, qname("xmp", "CreateDate"), capture_time)
        set_property_or_remove(desc, qname("xmp", "ModifyDate"), capture_time)
        set_property_or_remove(desc, qname("photoshop", "DateCreated"), capture_time)

    if len(plan.orientations) == 1:
        orientation = next(iter(plan.orientations))
        set_property_or_remove(desc, qname("tiff", "Orientation"), orientation)

    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        if backup and path.exists():
            backup_path = path.with_name(path.name + ".bak")
            if not backup_path.exists():
                shutil.copy2(path, backup_path)
        ET.indent(tree, space="  ")
        tree.write(path, encoding="utf-8", xml_declaration=True)

    return "updated" if path.exists() else "created"


def parse_prefix_mapping(value: str) -> tuple[Path, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("expected OLD=NEW")
    old, new = value.split("=", 1)
    if not old or not new:
        raise argparse.ArgumentTypeError("expected non-empty OLD=NEW")
    return Path(old).expanduser(), Path(new).expanduser()


def is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def write_manifest(
    manifest_path: Path,
    plans: dict[Path, FilePlan],
    smart_definitions: list[dict[str, object]],
    write: bool,
) -> None:
    payload = {
        "files": [
            {
                "source_path": str(plan.source_path),
                "target_path": str(plan.target_path),
                "sidecar_path": str(sidecar_for(plan.target_path)),
                "lightroom_image_ids": sorted(plan.image_ids),
                "tags": sorted(plan.tags),
                "digikam_pick_label": next(iter(plan.picks)) if len(plan.picks) == 1 else None,
                "capture_time": next(iter(plan.capture_times)) if len(plan.capture_times) == 1 else None,
                "capture_time_inferred": plan.inferred_capture_time is not None,
                "orientation": next(iter(plan.orientations)) if len(plan.orientations) == 1 else None,
                "rating": next(iter(plan.ratings)) if len(plan.ratings) == 1 else None,
                "notes": plan.notes,
            }
            for plan in sorted(plans.values(), key=lambda p: str(p.target_path))
        ],
        "smart_collection_definitions": smart_definitions,
    }

    if write:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path, help="Lightroom .lrcat SQLite catalog")
    parser.add_argument(
        "--path-prefix",
        action="append",
        default=[],
        type=parse_prefix_mapping,
        metavar="OLD=NEW",
        help="Map Lightroom source paths to a copied library path. Can be repeated.",
    )
    parser.add_argument(
        "--tag-prefix",
        default="_Lightroom",
        help="Top-level hierarchical tag prefix to add to XMP.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("lightroom-xmp-migration-manifest.json"),
        help="JSON manifest path.",
    )
    parser.add_argument("--write", action="store_true", help="Actually write sidecars and manifest.")
    parser.add_argument(
        "--infer-missing-dates-from-path",
        dest="infer_missing_dates_from_path",
        action="store_true",
        default=True,
        help="For catalog rows without captureTime, infer year/month/day from mapped file path. Enabled by default.",
    )
    parser.add_argument(
        "--no-infer-missing-dates-from-path",
        dest="infer_missing_dates_from_path",
        action="store_false",
        help="Do not infer missing capture dates from mapped file paths.",
    )
    parser.add_argument("--no-backup", action="store_true", help="Do not create .bak files for existing sidecars.")
    parser.add_argument(
        "--no-import-lightroom-sidecar",
        action="store_true",
        help="Do not seed new RAW filename.ext.xmp sidecars from existing Lightroom BASENAME.xmp sidecars.",
    )
    parser.add_argument("--limit", type=int, help="Limit number of target files, useful for testing.")
    parser.add_argument(
        "--only-under",
        type=Path,
        help="Only create/update sidecars for mapped target files under this folder.",
    )
    parser.add_argument(
        "--allow-missing",
        action="store_true",
        help="Create sidecars even if the target image file does not exist.",
    )
    args = parser.parse_args(argv)

    if not args.catalog.exists():
        print(f"Catalog not found: {args.catalog}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(f"file:{args.catalog}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        images = read_images(conn)
        collection_paths = read_collection_paths(conn)
        members = read_collection_members(conn)
        keyword_paths = read_keyword_paths(conn)
        keyword_members = read_keyword_members(conn)
        smart_definitions = read_smart_collection_definitions(conn)
    finally:
        conn.close()

    plans = build_plan(
        images,
        collection_paths,
        members,
        keyword_paths,
        keyword_members,
        args.path_prefix,
        args.tag_prefix,
        args.infer_missing_dates_from_path,
    )
    if args.only_under is not None:
        root = args.only_under.expanduser()
        plans = {path: plan for path, plan in plans.items() if is_under(path, root)}

    if args.limit is not None:
        limited = dict(list(sorted(plans.items(), key=lambda kv: str(kv[0])))[: args.limit])
        plans = limited

    missing = [plan for plan in plans.values() if not plan.target_path.exists()]
    if missing and not args.allow_missing:
        for plan in missing[:20]:
            print(f"missing target image, skipped: {plan.target_path}", file=sys.stderr)
        if len(missing) > 20:
            print(f"... and {len(missing) - 20} more missing target images", file=sys.stderr)
        plans = {path: plan for path, plan in plans.items() if plan.target_path.exists()}

    created = updated = errors = 0
    for plan in plans.values():
        sidecar = sidecar_for(plan.target_path)
        existed = sidecar.exists()
        template = None
        if not args.no_import_lightroom_sidecar:
            template = template_sidecar_for(plan.target_path, sidecar)
        try:
            update_xmp(
                sidecar,
                plan,
                write=args.write,
                backup=not args.no_backup,
                template_path=template,
            )
        except Exception as exc:  # noqa: BLE001 - report and continue per file.
            errors += 1
            print(f"error: {sidecar}: {exc}", file=sys.stderr)
            continue
        if existed:
            updated += 1
        else:
            created += 1

    write_manifest(args.manifest, plans, smart_definitions, write=args.write)

    mode = "WRITE" if args.write else "DRY RUN"
    print(f"{mode}: {len(images)} Lightroom image rows, {len(plans)} target files")
    print(f"sidecars to create: {created}")
    print(f"sidecars to update: {updated}")
    print(f"files with notes/conflicts: {sum(1 for p in plans.values() if p.notes)}")
    print(f"errors: {errors}")
    print(f"manifest: {args.manifest}{' written' if args.write else ' (not written in dry-run)'}")
    if not args.write:
        print("Re-run with --write to create/update sidecars.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
