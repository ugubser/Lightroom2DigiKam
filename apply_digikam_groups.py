#!/usr/bin/env python3
"""
Apply Lightroom stacks as native digiKam groups.

This script reads Lightroom stacks from a Lightroom Classic catalog and writes
digiKam group relations into a digiKam SQLite database. It does not write XMP
sidecars and it does not write to the Lightroom catalog.

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
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path


DIGIKAM_GROUPED_RELATION = 2


@dataclass(frozen=True)
class LightroomImage:
    image_id: int
    source_path: Path
    target_path: Path


@dataclass(frozen=True)
class LightroomStackMember:
    image_id: int
    stack_id: int
    position: float


@dataclass
class GroupPlan:
    stack_id: int
    leader_lr_image_id: int
    leader_path: Path
    leader_digikam_id: int | None = None
    member_paths: list[Path] = field(default_factory=list)
    member_digikam_ids: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def relation_count(self) -> int:
        return len(self.member_digikam_ids)


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


def norm_path(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def table_names(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def require_tables(conn: sqlite3.Connection, names: set[str], db_label: str) -> None:
    missing = names - table_names(conn)
    if missing:
        raise RuntimeError(f"{db_label} is missing required tables: {', '.join(sorted(missing))}")


def open_sqlite(path: Path, readonly: bool) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def read_lightroom_images(
    catalog: Path,
    path_mappings: list[tuple[Path, Path]],
) -> dict[int, LightroomImage]:
    conn = open_sqlite(catalog, readonly=True)
    try:
        require_tables(
            conn,
            {"Adobe_images", "AgLibraryFile", "AgLibraryFolder", "AgLibraryRootFolder"},
            "Lightroom catalog",
        )
        sql = """
            SELECT
                i.id_local AS image_id,
                rf.absolutePath || fo.pathFromRoot || fi.baseName ||
                    CASE WHEN fi.extension IS NULL OR fi.extension = ''
                         THEN ''
                         ELSE '.' || fi.extension
                    END AS full_path
            FROM Adobe_images i
            JOIN AgLibraryFile fi ON i.rootFile = fi.id_local
            JOIN AgLibraryFolder fo ON fi.folder = fo.id_local
            JOIN AgLibraryRootFolder rf ON fo.rootFolder = rf.id_local
        """
        images: dict[int, LightroomImage] = {}
        for row in conn.execute(sql):
            source = Path(row["full_path"])
            images[int(row["image_id"])] = LightroomImage(
                image_id=int(row["image_id"]),
                source_path=source,
                target_path=apply_path_prefixes(source, path_mappings),
            )
        return images
    finally:
        conn.close()


def read_lightroom_stack_members(catalog: Path) -> list[LightroomStackMember]:
    conn = open_sqlite(catalog, readonly=True)
    try:
        if "AgLibraryFolderStackImage" not in table_names(conn):
            return []
        rows = []
        for row in conn.execute("SELECT image, stack, position FROM AgLibraryFolderStackImage"):
            if row["image"] is None or row["stack"] is None:
                continue
            try:
                position = float(row["position"])
            except (TypeError, ValueError):
                position = 999999.0
            rows.append(
                LightroomStackMember(
                    image_id=int(row["image"]),
                    stack_id=int(row["stack"]),
                    position=position,
                )
            )
        return rows
    finally:
        conn.close()


def candidate_digikam_paths(
    specific_path: str | None,
    relative_path: str,
    name: str,
    fallback_roots: list[Path],
) -> list[Path]:
    relative = relative_path.strip("/")
    candidates: list[Path] = []

    if specific_path:
        candidates.append(Path(specific_path) / relative / name)

    for root in fallback_roots:
        candidates.append(root / relative / name)

    deduped: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = norm_path(candidate)
        if key not in seen:
            deduped.append(candidate)
            seen.add(key)
    return deduped


def read_digikam_image_index(
    digikam_db: Path,
    fallback_roots: list[Path],
) -> tuple[dict[str, int], dict[str, list[int]]]:
    conn = open_sqlite(digikam_db, readonly=True)
    try:
        require_tables(conn, {"AlbumRoots", "Albums", "Images"}, "digiKam database")
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
        path_to_ids: dict[str, list[int]] = defaultdict(list)
        for row in conn.execute(sql):
            for candidate in candidate_digikam_paths(
                row["specific_path"],
                row["relative_path"] or "",
                row["name"],
                fallback_roots,
            ):
                path_to_ids[norm_path(candidate)].append(int(row["image_id"]))

        unique: dict[str, int] = {}
        ambiguous: dict[str, list[int]] = {}
        for path, ids in path_to_ids.items():
            deduped = sorted(set(ids))
            if len(deduped) == 1:
                unique[path] = deduped[0]
            else:
                ambiguous[path] = deduped
        return unique, ambiguous
    finally:
        conn.close()


def build_group_plans(
    images: dict[int, LightroomImage],
    stack_members: list[LightroomStackMember],
    digikam_paths: dict[str, int],
    ambiguous_digikam_paths: dict[str, list[int]],
    only_under: Path | None,
    limit: int | None,
) -> list[GroupPlan]:
    by_stack: dict[int, list[LightroomStackMember]] = defaultdict(list)
    for member in stack_members:
        if member.image_id in images:
            by_stack[member.stack_id].append(member)

    plans: list[GroupPlan] = []
    only_key = norm_path(only_under) if only_under else None

    for stack_id, members in sorted(by_stack.items()):
        if len(members) < 2:
            continue

        resolved = [member for member in members if member.image_id in images]
        if len(resolved) < 2:
            continue

        resolved.sort(key=lambda member: (member.position, member.image_id))
        leader = resolved[0]
        leader_image = images[leader.image_id]

        unique_members: dict[str, LightroomStackMember] = {}
        for member in resolved:
            path = images[member.image_id].target_path
            unique_members.setdefault(norm_path(path), member)

        if len(unique_members) < 2:
            continue

        if only_key and not any(norm_path(images[m.image_id].target_path).startswith(only_key) for m in unique_members.values()):
            continue

        plan = GroupPlan(
            stack_id=stack_id,
            leader_lr_image_id=leader.image_id,
            leader_path=leader_image.target_path,
        )

        leader_key = norm_path(leader_image.target_path)
        if leader_key in ambiguous_digikam_paths:
            plan.notes.append(
                f"Leader path is ambiguous in digiKam DB: {leader_image.target_path}"
            )
        else:
            plan.leader_digikam_id = digikam_paths.get(leader_key)
            if plan.leader_digikam_id is None:
                plan.notes.append(f"Leader not found in digiKam DB: {leader_image.target_path}")

        for member_key, member in unique_members.items():
            if member.image_id == leader.image_id:
                continue
            target_path = images[member.image_id].target_path
            plan.member_paths.append(target_path)
            if member_key in ambiguous_digikam_paths:
                plan.notes.append(f"Member path is ambiguous in digiKam DB: {target_path}")
                continue
            member_id = digikam_paths.get(member_key)
            if member_id is None:
                plan.notes.append(f"Member not found in digiKam DB: {target_path}")
                continue
            plan.member_digikam_ids.append(member_id)

        if plan.leader_digikam_id is not None and plan.member_digikam_ids:
            plans.append(plan)
            if limit is not None and len(plans) >= limit:
                break

    return plans


def existing_group_relations(conn: sqlite3.Connection) -> dict[int, int]:
    require_tables(conn, {"ImageRelations"}, "digiKam database")
    rows = conn.execute(
        "SELECT subject, object FROM ImageRelations WHERE type = ?",
        (DIGIKAM_GROUPED_RELATION,),
    )
    return {int(row["subject"]): int(row["object"]) for row in rows}


def classify_plans(
    plans: list[GroupPlan],
    existing: dict[int, int],
    replace_existing: bool,
) -> tuple[list[GroupPlan], list[dict[str, object]]]:
    writable: list[GroupPlan] = []
    skipped: list[dict[str, object]] = []

    for plan in plans:
        conflicting = [
            member_id
            for member_id in plan.member_digikam_ids
            if member_id in existing and existing[member_id] != plan.leader_digikam_id
        ]
        already_present = [
            member_id
            for member_id in plan.member_digikam_ids
            if existing.get(member_id) == plan.leader_digikam_id
        ]

        if conflicting and not replace_existing:
            skipped.append(
                {
                    "stack_id": plan.stack_id,
                    "leader": str(plan.leader_path),
                    "reason": "existing different digiKam group relation",
                    "conflicting_member_ids": conflicting,
                }
            )
            continue

        remaining = [
            member_id
            for member_id in plan.member_digikam_ids
            if replace_existing or member_id not in existing or member_id not in already_present
        ]
        if not remaining:
            skipped.append(
                {
                    "stack_id": plan.stack_id,
                    "leader": str(plan.leader_path),
                    "reason": "already grouped",
                }
            )
            continue

        plan.member_digikam_ids = remaining
        writable.append(plan)

    return writable, skipped


def backup_database(path: Path) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, backup)
    return backup


def write_group_relations(
    digikam_db: Path,
    plans: list[GroupPlan],
    replace_existing: bool,
) -> int:
    conn = open_sqlite(digikam_db, readonly=False)
    try:
        require_tables(conn, {"ImageRelations"}, "digiKam database")
        relation_count = 0
        with conn:
            for plan in plans:
                assert plan.leader_digikam_id is not None
                if replace_existing:
                    affected = [plan.leader_digikam_id, *plan.member_digikam_ids]
                    conn.executemany(
                        "DELETE FROM ImageRelations WHERE type = ? AND (subject = ? OR object = ?)",
                        [
                            (DIGIKAM_GROUPED_RELATION, image_id, image_id)
                            for image_id in affected
                        ],
                    )
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO ImageRelations (subject, object, type)
                    VALUES (?, ?, ?)
                    """,
                    [
                        (member_id, plan.leader_digikam_id, DIGIKAM_GROUPED_RELATION)
                        for member_id in plan.member_digikam_ids
                    ],
                )
                relation_count += len(plan.member_digikam_ids)
        return relation_count
    finally:
        conn.close()


def write_report(
    report_path: Path,
    plans: list[GroupPlan],
    writable: list[GroupPlan],
    skipped: list[dict[str, object]],
    backup_path: Path | None,
    wrote: bool,
) -> None:
    payload = {
        "wrote": wrote,
        "backup": str(backup_path) if backup_path else None,
        "candidate_groups": len(plans),
        "writable_groups": len(writable),
        "writable_relations": sum(plan.relation_count for plan in writable),
        "skipped_groups": len(skipped),
        "skipped": skipped,
        "groups": [
            {
                "stack_id": plan.stack_id,
                "leader_lr_image_id": plan.leader_lr_image_id,
                "leader_path": str(plan.leader_path),
                "leader_digikam_id": plan.leader_digikam_id,
                "member_paths": [str(path) for path in plan.member_paths],
                "member_digikam_ids": plan.member_digikam_ids,
                "notes": plan.notes,
            }
            for plan in plans
        ],
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path, help="Lightroom .lrcat SQLite catalog")
    parser.add_argument("digikam_db", type=Path, help="digiKam digikam4.db SQLite database")
    parser.add_argument(
        "--path-prefix",
        action="append",
        default=[],
        type=parse_prefix_mapping,
        metavar="OLD=NEW",
        help="Map Lightroom source paths to the photo paths known by digiKam. Can be repeated.",
    )
    parser.add_argument(
        "--digikam-root",
        action="append",
        default=[],
        type=Path,
        help="Photo root to combine with digiKam Albums.relativePath if AlbumRoots.specificPath is not enough.",
    )
    parser.add_argument(
        "--only-under",
        type=Path,
        help="Only apply stacks with at least one mapped image under this folder.",
    )
    parser.add_argument("--limit", type=int, help="Limit to the first N candidate groups.")
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help="Replace existing digiKam group relations for affected images.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("digikam-group-apply-report.json"),
        help="JSON report path.",
    )
    parser.add_argument("--write", action="store_true", help="Write group relations to the digiKam database.")
    args = parser.parse_args(argv)

    catalog = args.catalog.expanduser()
    digikam_db = args.digikam_db.expanduser()
    if not catalog.exists():
        print(f"Catalog not found: {catalog}", file=sys.stderr)
        return 2
    if not digikam_db.exists():
        print(f"digiKam database not found: {digikam_db}", file=sys.stderr)
        return 2

    fallback_roots = [root.expanduser() for root in args.digikam_root]
    only_under = args.only_under.expanduser() if args.only_under else None

    images = read_lightroom_images(catalog, args.path_prefix)
    stack_members = read_lightroom_stack_members(catalog)
    digikam_paths, ambiguous_paths = read_digikam_image_index(digikam_db, fallback_roots)
    plans = build_group_plans(
        images,
        stack_members,
        digikam_paths,
        ambiguous_paths,
        only_under,
        args.limit,
    )

    conn = open_sqlite(digikam_db, readonly=True)
    try:
        existing = existing_group_relations(conn)
    finally:
        conn.close()

    writable, skipped = classify_plans(plans, existing, args.replace_existing)

    backup_path = None
    wrote_relations = 0
    if args.write and writable:
        backup_path = backup_database(digikam_db)
        wrote_relations = write_group_relations(digikam_db, writable, args.replace_existing)

    write_report(args.report, plans, writable, skipped, backup_path, args.write and bool(writable))

    print(f"Lightroom stack rows: {len(stack_members)}")
    print(f"Candidate groups: {len(plans)}")
    print(f"Writable groups: {len(writable)}")
    print(f"Writable relations: {sum(plan.relation_count for plan in writable)}")
    print(f"Skipped groups: {len(skipped)}")
    print(f"Report: {args.report}")
    if args.write:
        print(f"Backup: {backup_path}" if backup_path else "Backup: not created")
        print(f"Relations written: {wrote_relations}")
    else:
        print("Dry run only. Re-run with --write after closing digiKam to modify the database.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
