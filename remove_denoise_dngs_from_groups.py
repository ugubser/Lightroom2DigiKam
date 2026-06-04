#!/usr/bin/env python3
"""
Remove Lightroom AI denoise DNGs from digiKam groups.

This script targets files named like *-Enhanced-NR*.dng in a digiKam SQLite
database. It preserves the remaining group by choosing a non-denoise group
member as the new leader when a denoise DNG is currently the group leader.

By default this is a dry run. Close digiKam before using --write.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path


DIGIKAM_GROUPED_RELATION = 2
DEFAULT_PATTERN = "%-enhanced-nr%.dng"


@dataclass(frozen=True)
class DigiKamImage:
    image_id: int
    path: Path
    name: str
    relative_path: str


@dataclass
class DenoisePlan:
    denoise: DigiKamImage
    current_leader_id: int | None = None
    current_member_ids: list[int] = field(default_factory=list)
    new_leader_id: int | None = None
    regroup_member_ids: list[int] = field(default_factory=list)
    remove_relation_subject_ids: list[int] = field(default_factory=list)
    move_paths: list[Path] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def norm_path(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


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


def candidate_paths(
    specific_path: str | None,
    relative_path: str,
    name: str,
    fallback_roots: list[Path],
) -> list[Path]:
    relative = relative_path.strip("/")
    paths: list[Path] = []
    if specific_path:
        paths.append(Path(specific_path) / relative / name)
    for root in fallback_roots:
        paths.append(root / relative / name)

    out: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = norm_path(path)
        if key not in seen:
            out.append(path)
            seen.add(key)
    return out


def choose_existing_path(paths: list[Path]) -> Path:
    for path in paths:
        if path.exists():
            return path
    return paths[0]


def read_images(
    digikam_db: Path,
    fallback_roots: list[Path],
    pattern: str,
) -> tuple[dict[int, DigiKamImage], list[DigiKamImage]]:
    conn = open_sqlite(digikam_db, readonly=True)
    try:
        require_tables(conn, {"AlbumRoots", "Albums", "Images"})
        sql = """
            SELECT
                Images.id AS image_id,
                Images.name AS name,
                Albums.relativePath AS relative_path,
                AlbumRoots.specificPath AS specific_path
            FROM Images
            JOIN Albums ON Images.album = Albums.id
            JOIN AlbumRoots ON Albums.albumRoot = AlbumRoots.id
            WHERE Images.status < 3
        """
        all_images: dict[int, DigiKamImage] = {}
        denoise_images: list[DigiKamImage] = []
        for row in conn.execute(sql):
            paths = candidate_paths(
                row["specific_path"],
                row["relative_path"] or "",
                row["name"],
                fallback_roots,
            )
            image = DigiKamImage(
                image_id=int(row["image_id"]),
                path=choose_existing_path(paths),
                name=row["name"],
                relative_path=row["relative_path"] or "",
            )
            all_images[image.image_id] = image

        for image in all_images.values():
            if image.name.lower().endswith(".dng") and "-enhanced-nr" in image.name.lower():
                denoise_images.append(image)
        return all_images, sorted(denoise_images, key=lambda image: str(image.path))
    finally:
        conn.close()


def read_group_relations(digikam_db: Path) -> tuple[dict[int, int], dict[int, list[int]]]:
    conn = open_sqlite(digikam_db, readonly=True)
    try:
        require_tables(conn, {"ImageRelations"})
        subject_to_object: dict[int, int] = {}
        object_to_subjects: dict[int, list[int]] = {}
        for row in conn.execute(
            "SELECT subject, object FROM ImageRelations WHERE type = ?",
            (DIGIKAM_GROUPED_RELATION,),
        ):
            subject = int(row["subject"])
            obj = int(row["object"])
            subject_to_object[subject] = obj
            object_to_subjects.setdefault(obj, []).append(subject)
        for subjects in object_to_subjects.values():
            subjects.sort()
        return subject_to_object, object_to_subjects
    finally:
        conn.close()


def related_sidecars(path: Path) -> list[Path]:
    candidates = [Path(str(path) + ".xmp")]
    if path.suffix:
        candidates.append(path.with_suffix(".xmp"))
    return [candidate for candidate in candidates if candidate.exists()]


def choose_new_leader(member_ids: list[int], images: dict[int, DigiKamImage], denoise_ids: set[int]) -> int | None:
    candidates = [image_id for image_id in member_ids if image_id not in denoise_ids and image_id in images]
    if not candidates:
        return None

    def key(image_id: int) -> tuple[int, str]:
        suffix = images[image_id].path.suffix.lower()
        raw_rank = 0 if suffix in {".orf", ".rw2", ".cr2", ".cr3", ".nef", ".arw", ".raf", ".dng"} else 1
        return raw_rank, str(images[image_id].path)

    return sorted(candidates, key=key)[0]


def build_plans(
    images: dict[int, DigiKamImage],
    denoise_images: list[DigiKamImage],
    subject_to_object: dict[int, int],
    object_to_subjects: dict[int, list[int]],
    move_to: Path | None,
) -> list[DenoisePlan]:
    denoise_ids = {image.image_id for image in denoise_images}
    plans: list[DenoisePlan] = []

    for denoise in denoise_images:
        plan = DenoisePlan(denoise=denoise)
        if denoise.image_id in subject_to_object:
            plan.current_leader_id = subject_to_object[denoise.image_id]
            plan.remove_relation_subject_ids.append(denoise.image_id)

        if denoise.image_id in object_to_subjects:
            subjects = object_to_subjects[denoise.image_id]
            plan.current_member_ids = subjects
            new_leader = choose_new_leader(subjects, images, denoise_ids)
            plan.new_leader_id = new_leader
            if new_leader is None:
                plan.notes.append("Denoise DNG is group leader, but no non-denoise member is available as replacement.")
            else:
                plan.regroup_member_ids = [
                    image_id
                    for image_id in subjects
                    if image_id != new_leader and image_id not in denoise_ids
                ]
                plan.remove_relation_subject_ids.extend(subjects)

        if move_to:
            plan.move_paths = [denoise.path, *related_sidecars(denoise.path)]

        plans.append(plan)

    return plans


def backup_database(path: Path) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, backup)
    return backup


def move_to_quarantine(path: Path, root: Path, quarantine_root: Path) -> Path:
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = Path(path.name)
    target = quarantine_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        shutil.move(str(path), str(target))
        return target
    for index in range(1, 10000):
        candidate = target.with_name(f"{target.name}.{index}")
        if not candidate.exists():
            shutil.move(str(path), str(candidate))
            return candidate
    raise RuntimeError(f"Could not find available quarantine path for {path}")


def apply_plans(digikam_db: Path, plans: list[DenoisePlan]) -> int:
    conn = open_sqlite(digikam_db, readonly=False)
    try:
        require_tables(conn, {"ImageRelations"})
        changed = 0
        with conn:
            for plan in plans:
                for subject_id in sorted(set(plan.remove_relation_subject_ids)):
                    cursor = conn.execute(
                        "DELETE FROM ImageRelations WHERE type = ? AND subject = ?",
                        (DIGIKAM_GROUPED_RELATION, subject_id),
                    )
                    changed += cursor.rowcount

                if plan.new_leader_id is not None:
                    for member_id in plan.regroup_member_ids:
                        cursor = conn.execute(
                            """
                            INSERT OR REPLACE INTO ImageRelations (subject, object, type)
                            VALUES (?, ?, ?)
                            """,
                            (member_id, plan.new_leader_id, DIGIKAM_GROUPED_RELATION),
                        )
                        changed += cursor.rowcount
        return changed
    finally:
        conn.close()


def write_report(
    report_path: Path,
    plans: list[DenoisePlan],
    backup_path: Path | None,
    moved: list[dict[str, str]],
    wrote: bool,
) -> None:
    payload = {
        "wrote": wrote,
        "backup": str(backup_path) if backup_path else None,
        "denoise_dng_count": len(plans),
        "grouped_as_member_count": sum(1 for plan in plans if plan.current_leader_id is not None),
        "grouped_as_leader_count": sum(1 for plan in plans if plan.current_member_ids),
        "move_count": len(moved),
        "moved": moved,
        "files": [
            {
                "image_id": plan.denoise.image_id,
                "path": str(plan.denoise.path),
                "current_leader_id": plan.current_leader_id,
                "current_member_ids": plan.current_member_ids,
                "new_leader_id": plan.new_leader_id,
                "regroup_member_ids": plan.regroup_member_ids,
                "remove_relation_subject_ids": sorted(set(plan.remove_relation_subject_ids)),
                "move_paths": [str(path) for path in plan.move_paths],
                "notes": plan.notes,
            }
            for plan in plans
        ],
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("digikam_db", type=Path, help="digiKam digikam4.db SQLite database")
    parser.add_argument(
        "--digikam-root",
        action="append",
        default=[],
        type=Path,
        help="Photo root to combine with digiKam Albums.relativePath.",
    )
    parser.add_argument(
        "--move-to",
        type=Path,
        help="Move denoise DNG files and their XMP sidecars to this quarantine folder.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("remove-denoise-dngs-report.json"),
        help="JSON report path.",
    )
    parser.add_argument("--write", action="store_true", help="Modify the digiKam database and move files if requested.")
    args = parser.parse_args(argv)

    digikam_db = args.digikam_db.expanduser()
    if not digikam_db.exists():
        print(f"digiKam database not found: {digikam_db}", file=sys.stderr)
        return 2

    fallback_roots = [root.expanduser() for root in args.digikam_root]
    move_to = args.move_to.expanduser() if args.move_to else None

    images, denoise_images = read_images(digikam_db, fallback_roots, DEFAULT_PATTERN)
    subject_to_object, object_to_subjects = read_group_relations(digikam_db)
    plans = build_plans(images, denoise_images, subject_to_object, object_to_subjects, move_to)

    backup_path = None
    moved: list[dict[str, str]] = []
    relation_changes = 0

    if args.write:
        backup_path = backup_database(digikam_db)
        relation_changes = apply_plans(digikam_db, plans)
        if move_to:
            root = fallback_roots[0] if fallback_roots else Path("/")
            for plan in plans:
                for path in plan.move_paths:
                    if not path.exists():
                        continue
                    target = move_to_quarantine(path, root, move_to)
                    moved.append({"source": str(path), "target": str(target)})

    write_report(args.report, plans, backup_path, moved, args.write)

    print(f"Denoise DNGs found: {len(plans)}")
    print(f"Grouped as member: {sum(1 for plan in plans if plan.current_leader_id is not None)}")
    print(f"Grouped as leader: {sum(1 for plan in plans if plan.current_member_ids)}")
    print(f"Relations to remove: {sum(len(set(plan.remove_relation_subject_ids)) for plan in plans)}")
    print(f"Relations to recreate: {sum(len(plan.regroup_member_ids) for plan in plans)}")
    print(f"Report: {args.report}")
    if args.write:
        print(f"Backup: {backup_path}")
        print(f"Relation changes executed: {relation_changes}")
        print(f"Files moved: {len(moved)}")
    else:
        print("Dry run only. Re-run with --write after closing digiKam to modify the database.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
