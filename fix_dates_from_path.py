#!/usr/bin/env python3
"""
Fix digiKam dates when image dates conflict with dated folder paths.

This script compares digiKam album paths with stored image dates. If an album
path contains an exact date in the form /YYYY/MM/DD, the script can update image
dates to that path date while preserving each stored time of day. It can also
set the digiKam album date to the same path date.

Less precise paths such as /YYYY/MM/Event or /YYYY/Event are reported when
stored dates fall outside the path constraint, but they are not written because
the exact day cannot be determined from the folder path alone.

By default this is a dry run. Close digiKam before using --write.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import sqlite3
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


PLACEHOLDER_DATE = "1904-01-01"
DEFAULT_TIME = "12:00:00.000"
NS = {
    "x": "adobe:ns:meta/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "exif": "http://ns.adobe.com/exif/1.0/",
    "xmp": "http://ns.adobe.com/xap/1.0/",
    "photoshop": "http://ns.adobe.com/photoshop/1.0/",
}

for prefix, uri in NS.items():
    ET.register_namespace(prefix, uri)


@dataclass
class PathConstraint:
    kind: str
    year: int | None = None
    month: int | None = None
    day: int | None = None
    date: str | None = None
    prefix: str | None = None


@dataclass
class ImagePathPlan:
    image_id: int
    album_id: int
    relative_path: str
    name: str
    path_constraint: PathConstraint
    current_creation_date: str | None
    current_digitization_date: str | None
    proposed_creation_date: str | None = None
    proposed_digitization_date: str | None = None
    sidecar_path: str | None = None
    action: str = "skip"
    reason: str = ""


@dataclass
class AlbumPathPlan:
    album_id: int
    relative_path: str
    path_constraint: PathConstraint
    current_date: str | None
    proposed_date: str | None = None
    action: str = "skip"
    reason: str = ""


def open_sqlite(path: Path, readonly: bool) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def table_names(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def require_tables(conn: sqlite3.Connection, names: set[str]) -> None:
    missing = names - table_names(conn)
    if missing:
        raise RuntimeError(f"digiKam database is missing required tables: {', '.join(sorted(missing))}")


def parse_path_constraint(relative_path: str) -> PathConstraint:
    parts = relative_path.strip("/").split("/")
    if not parts or not re.fullmatch(r"\d{4}", parts[0]):
        return PathConstraint(kind="none")

    year = int(parts[0])
    if len(parts) < 2 or not re.fullmatch(r"\d{1,2}", parts[1]):
        return PathConstraint(kind="year", year=year, prefix=f"{year:04d}")

    month = int(parts[1])
    try:
        dt.date(year, month, 1)
    except ValueError:
        return PathConstraint(kind="year", year=year, prefix=f"{year:04d}")

    if len(parts) >= 3 and re.fullmatch(r"\d{1,2}", parts[2]):
        day = int(parts[2])
        try:
            date = dt.date(year, month, day).isoformat()
        except ValueError:
            return PathConstraint(kind="month", year=year, month=month, prefix=f"{year:04d}-{month:02d}")
        return PathConstraint(
            kind="day",
            year=year,
            month=month,
            day=day,
            date=date,
            prefix=date,
        )

    return PathConstraint(kind="month", year=year, month=month, prefix=f"{year:04d}-{month:02d}")


def date_part(value: str | None) -> str | None:
    if not value or len(value) < 10:
        return None
    day = value[:10]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return day
    return None


def time_part(value: str | None) -> str:
    if value and len(value) >= 19 and re.fullmatch(r"\d{2}:\d{2}:\d{2}", value[11:19]):
        if len(value) > 19 and value[19] == ".":
            fraction = value[20:]
            if fraction and re.fullmatch(r"\d+", fraction):
                return value[11:]
        return value[11:19] + ".000"
    return DEFAULT_TIME


def replace_date_keep_time(value: str | None, new_date: str) -> str:
    return f"{new_date}T{time_part(value)}"


def qname(prefix: str, name: str) -> str:
    return f"{{{NS[prefix]}}}{name}"


def sidecar_for(path: Path) -> Path:
    return path.with_name(path.name + ".xmp")


def date_matches_constraint(day: str | None, constraint: PathConstraint) -> bool:
    if day is None:
        return False
    if constraint.kind == "day":
        return day == constraint.date
    if constraint.kind == "month":
        return day.startswith(f"{constraint.year:04d}-{constraint.month:02d}-")
    if constraint.kind == "year":
        return day.startswith(f"{constraint.year:04d}-")
    return True


def relevant_mismatch(day: str | None, constraint: PathConstraint) -> bool:
    if day is None:
        return True
    if day <= PLACEHOLDER_DATE:
        return True
    return not date_matches_constraint(day, constraint)


def read_images(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            Images.id AS image_id,
            Images.album AS album_id,
            Images.name,
            Albums.relativePath,
            ImageInformation.creationDate,
            ImageInformation.digitizationDate
        FROM Images
        JOIN Albums ON Albums.id = Images.album
        JOIN ImageInformation ON ImageInformation.imageid = Images.id
        WHERE Images.status < 3
        ORDER BY Albums.relativePath, Images.name
        """
    ).fetchall()


def read_albums(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id AS album_id, relativePath, date
        FROM Albums
        ORDER BY relativePath
        """
    ).fetchall()


def build_image_plan(row: sqlite3.Row) -> ImagePathPlan | None:
    constraint = parse_path_constraint(row["relativePath"])
    if constraint.kind == "none":
        return None

    creation_day = date_part(row["creationDate"])
    digitization_day = date_part(row["digitizationDate"])
    creation_mismatch = relevant_mismatch(creation_day, constraint)
    digitization_mismatch = relevant_mismatch(digitization_day, constraint)
    if not creation_mismatch and not digitization_mismatch:
        return None

    plan = ImagePathPlan(
        image_id=int(row["image_id"]),
        album_id=int(row["album_id"]),
        relative_path=row["relativePath"],
        name=row["name"],
        path_constraint=constraint,
        current_creation_date=row["creationDate"],
        current_digitization_date=row["digitizationDate"],
    )

    if constraint.kind != "day" or not constraint.date:
        plan.reason = f"path only gives {constraint.kind} constraint {constraint.prefix}; exact day not inferred"
        return plan

    plan.proposed_creation_date = replace_date_keep_time(row["creationDate"], constraint.date)
    plan.proposed_digitization_date = replace_date_keep_time(row["digitizationDate"], constraint.date)
    plan.action = "update"
    plan.reason = "exact /YYYY/MM/DD folder date"
    return plan


def build_album_plan(row: sqlite3.Row) -> AlbumPathPlan | None:
    constraint = parse_path_constraint(row["relativePath"])
    if constraint.kind == "none":
        return None

    current = row["date"]
    current_day = date_part(current)
    if current_day and date_matches_constraint(current_day, constraint):
        return None

    plan = AlbumPathPlan(
        album_id=int(row["album_id"]),
        relative_path=row["relativePath"],
        path_constraint=constraint,
        current_date=current,
    )

    if constraint.kind != "day" or not constraint.date:
        plan.reason = f"path only gives {constraint.kind} constraint {constraint.prefix}; exact album date not inferred"
        return plan

    plan.proposed_date = constraint.date
    plan.action = "update"
    plan.reason = "exact /YYYY/MM/DD folder date"
    return plan


def build_plans(db: Path) -> tuple[list[ImagePathPlan], list[AlbumPathPlan]]:
    conn = open_sqlite(db, readonly=True)
    try:
        require_tables(conn, {"Albums", "Images", "ImageInformation"})
        image_plans = [plan for row in read_images(conn) if (plan := build_image_plan(row))]
        album_plans = [plan for row in read_albums(conn) if (plan := build_album_plan(row))]
        return image_plans, album_plans
    finally:
        conn.close()


def attach_sidecar_paths(image_plans: list[ImagePathPlan], digikam_root: Path) -> None:
    for plan in image_plans:
        rel = plan.relative_path.lstrip("/")
        plan.sidecar_path = str(sidecar_for(digikam_root / rel / plan.name))


def backup_database(path: Path) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, backup)
    return backup


def write_image_dates(conn: sqlite3.Connection, plans: list[ImagePathPlan]) -> int:
    changed = 0
    for plan in plans:
        if plan.action != "update" or not plan.proposed_creation_date or not plan.proposed_digitization_date:
            continue
        cursor = conn.execute(
            """
            UPDATE ImageInformation
            SET creationDate = ?, digitizationDate = ?
            WHERE imageid = ?
            """,
            (plan.proposed_creation_date, plan.proposed_digitization_date, plan.image_id),
        )
        changed += cursor.rowcount
    return changed


def write_album_dates(conn: sqlite3.Connection, plans: list[AlbumPathPlan]) -> int:
    changed = 0
    for plan in plans:
        if plan.action != "update" or not plan.proposed_date:
            continue
        cursor = conn.execute(
            "UPDATE Albums SET date = ? WHERE id = ?",
            (plan.proposed_date, plan.album_id),
        )
        changed += cursor.rowcount
    return changed


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


def set_property(desc: ET.Element, tag: str, value: str) -> None:
    desc.attrib.pop(tag, None)
    node = desc.find(tag)
    if node is not None:
        desc.remove(node)
    desc.set(tag, value)


def write_xmp_sidecars(plans: list[ImagePathPlan], backup: bool = True) -> tuple[int, list[str]]:
    changed = 0
    errors: list[str] = []
    for plan in plans:
        if plan.action != "update" or not plan.proposed_creation_date or not plan.proposed_digitization_date:
            continue
        if not plan.sidecar_path:
            errors.append(f"{plan.relative_path}/{plan.name}: missing sidecar path; pass --digikam-root")
            continue
        path = Path(plan.sidecar_path)
        try:
            tree, desc = ensure_xmp_tree(path)
            set_property(desc, qname("exif", "DateTimeOriginal"), plan.proposed_creation_date)
            set_property(desc, qname("exif", "DateTimeDigitized"), plan.proposed_digitization_date)
            set_property(desc, qname("xmp", "CreateDate"), plan.proposed_creation_date)
            set_property(desc, qname("xmp", "ModifyDate"), plan.proposed_creation_date)
            set_property(desc, qname("photoshop", "DateCreated"), plan.proposed_creation_date)
            path.parent.mkdir(parents=True, exist_ok=True)
            if backup and path.exists():
                backup_path = path.with_name(path.name + ".bak")
                if not backup_path.exists():
                    shutil.copy2(path, backup_path)
            ET.indent(tree, space="  ")
            tree.write(path, encoding="utf-8", xml_declaration=True)
            changed += 1
        except Exception as exc:  # noqa: BLE001 - collect per-file errors.
            errors.append(f"{path}: {exc}")
    return changed, errors


def write_changes(
    db: Path,
    image_plans: list[ImagePathPlan],
    album_plans: list[AlbumPathPlan],
    write_sidecars: bool,
) -> tuple[int, int, int, list[str]]:
    conn = open_sqlite(db, readonly=False)
    try:
        require_tables(conn, {"Albums", "ImageInformation"})
        with conn:
            image_changed = write_image_dates(conn, image_plans)
            album_changed = write_album_dates(conn, album_plans)
    finally:
        conn.close()
    sidecar_changed = 0
    sidecar_errors: list[str] = []
    if write_sidecars:
        sidecar_changed, sidecar_errors = write_xmp_sidecars(image_plans)
    return image_changed, album_changed, sidecar_changed, sidecar_errors


def image_payload(plan: ImagePathPlan) -> dict[str, object]:
    return {
        "image_id": plan.image_id,
        "album_id": plan.album_id,
        "path": f"{plan.relative_path}/{plan.name}",
        "path_constraint": {
            "kind": plan.path_constraint.kind,
            "date": plan.path_constraint.date,
            "prefix": plan.path_constraint.prefix,
        },
        "current_creation_date": plan.current_creation_date,
        "current_digitization_date": plan.current_digitization_date,
        "proposed_creation_date": plan.proposed_creation_date,
        "proposed_digitization_date": plan.proposed_digitization_date,
        "sidecar_path": plan.sidecar_path,
        "action": plan.action,
        "reason": plan.reason,
    }


def album_payload(plan: AlbumPathPlan) -> dict[str, object]:
    return {
        "album_id": plan.album_id,
        "relative_path": plan.relative_path,
        "path_constraint": {
            "kind": plan.path_constraint.kind,
            "date": plan.path_constraint.date,
            "prefix": plan.path_constraint.prefix,
        },
        "current_date": plan.current_date,
        "proposed_date": plan.proposed_date,
        "action": plan.action,
        "reason": plan.reason,
    }


def write_report(
    report: Path,
    image_plans: list[ImagePathPlan],
    album_plans: list[AlbumPathPlan],
    backup: Path | None,
    wrote: bool,
    picture_enabled: bool,
    album_enabled: bool,
    image_rows_changed: int,
    album_rows_changed: int,
    sidecars_changed: int,
    sidecar_errors: list[str],
) -> None:
    payload = {
        "wrote": wrote,
        "backup": str(backup) if backup else None,
        "picture_enabled": picture_enabled,
        "album_enabled": album_enabled,
        "image_candidate_count": len(image_plans),
        "image_update_count": sum(1 for plan in image_plans if plan.action == "update"),
        "image_report_only_count": sum(1 for plan in image_plans if plan.action != "update"),
        "album_candidate_count": len(album_plans),
        "album_update_count": sum(1 for plan in album_plans if plan.action == "update"),
        "album_report_only_count": sum(1 for plan in album_plans if plan.action != "update"),
        "image_rows_changed": image_rows_changed,
        "album_rows_changed": album_rows_changed,
        "sidecars_changed": sidecars_changed,
        "sidecar_error_count": len(sidecar_errors),
        "sidecar_errors": sidecar_errors,
        "images": [image_payload(plan) for plan in image_plans],
        "albums": [album_payload(plan) for plan in album_plans],
    }
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("digikam_db", type=Path, help="digiKam digikam4.db SQLite database")
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("fix-dates-from-path-report.json"),
        help="JSON report path.",
    )
    parser.add_argument(
        "--digikam-root",
        type=Path,
        help="Photo root for writing explicit filename.ext.xmp sidecars for image date fixes.",
    )
    parser.add_argument("--write", action="store_true", help="Write proposed dates to the digiKam database.")
    parser.add_argument("--album-only", action="store_true", help="Only change album dates; image mismatches are reported.")
    parser.add_argument("--picture-only", action="store_true", help="Only change image dates; album mismatches are reported.")
    parser.add_argument(
        "--no-sidecars",
        action="store_true",
        help="With --write, update digiKam only and do not create/update XMP sidecars.",
    )
    args = parser.parse_args(argv)

    if args.album_only and args.picture_only:
        print("--album-only and --picture-only are mutually exclusive", file=sys.stderr)
        return 2

    db = args.digikam_db.expanduser()
    if not db.exists():
        print(f"digiKam database not found: {db}", file=sys.stderr)
        return 2
    if args.write and not args.album_only and not args.no_sidecars and args.digikam_root is None:
        print("--digikam-root is required when writing image date fixes to XMP sidecars", file=sys.stderr)
        return 2
    if args.digikam_root is not None and not args.digikam_root.expanduser().exists():
        print(f"digiKam photo root not found: {args.digikam_root}", file=sys.stderr)
        return 2

    picture_enabled = not args.album_only
    album_enabled = not args.picture_only
    image_plans, album_plans = build_plans(db)
    if args.digikam_root is not None:
        attach_sidecar_paths(image_plans, args.digikam_root.expanduser())

    image_write_plans = image_plans if picture_enabled else []
    album_write_plans = album_plans if album_enabled else []
    backup = None
    image_rows_changed = 0
    album_rows_changed = 0
    sidecars_changed = 0
    sidecar_errors: list[str] = []
    if args.write:
        backup = backup_database(db)
        image_rows_changed, album_rows_changed, sidecars_changed, sidecar_errors = write_changes(
            db,
            image_write_plans,
            album_write_plans,
            write_sidecars=picture_enabled and not args.no_sidecars,
        )

    write_report(
        args.report,
        image_plans,
        album_plans,
        backup,
        args.write,
        picture_enabled,
        album_enabled,
        image_rows_changed,
        album_rows_changed,
        sidecars_changed,
        sidecar_errors,
    )

    image_updates = sum(1 for plan in image_plans if plan.action == "update")
    album_updates = sum(1 for plan in album_plans if plan.action == "update")
    print(f"Image date mismatches: {len(image_plans)}")
    print(f"Image updates proposed: {image_updates}{'' if picture_enabled else ' (report only; --album-only)'}")
    print(f"Image report-only mismatches: {len(image_plans) - image_updates}")
    print(f"Album date mismatches: {len(album_plans)}")
    print(f"Album updates proposed: {album_updates}{'' if album_enabled else ' (report only; --picture-only)'}")
    print(f"Album report-only mismatches: {len(album_plans) - album_updates}")
    print(f"Report: {args.report}")

    for plan in image_plans[:30]:
        marker = "UPDATE" if plan.action == "update" else "REPORT"
        print(
            f"IMAGE {marker}: {plan.relative_path}/{plan.name} "
            f"{plan.current_creation_date} -> {plan.proposed_creation_date or '-'} ({plan.reason})"
        )
    if len(image_plans) > 30:
        print(f"... and {len(image_plans) - 30} more image mismatches")

    for plan in album_plans[:30]:
        marker = "UPDATE" if plan.action == "update" else "REPORT"
        print(
            f"ALBUM {marker}: {plan.relative_path} "
            f"{plan.current_date} -> {plan.proposed_date or '-'} ({plan.reason})"
        )
    if len(album_plans) > 30:
        print(f"... and {len(album_plans) - 30} more album mismatches")

    if args.write:
        print(f"Backup: {backup}")
        print(f"Image rows changed: {image_rows_changed}")
        print(f"Album rows changed: {album_rows_changed}")
        print(f"XMP sidecars changed: {sidecars_changed}")
        print(f"XMP sidecar errors: {len(sidecar_errors)}")
    else:
        print("Dry run only. Re-run with --write after closing digiKam to modify the database.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
