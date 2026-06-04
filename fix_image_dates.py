#!/usr/bin/env python3
"""
Fix placeholder digiKam image dates from neighboring files in the same album.

This script targets digiKam Images whose creationDate or digitizationDate is
1904-01-01. It proposes a replacement date from other non-placeholder image
dates in the same album. The actual time is not inferred; proposed dates use a
configurable neutral time, noon by default.

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
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path


PLACEHOLDER_DATE = "1904-01-01"
PLACEHOLDER_DATETIME_PREFIX = "1904-01-01"


@dataclass
class ImageDatePlan:
    image_id: int
    relative_path: str
    name: str
    current_creation_date: str | None
    current_digitization_date: str | None
    album_item_count: int
    valid_neighbor_count: int
    distinct_neighbor_dates: int
    top_dates: list[tuple[str, int]] = field(default_factory=list)
    proposed_date: str | None = None
    proposed_datetime: str | None = None
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


def day_from_datetime(value: str | None) -> str | None:
    if not value or len(value) < 10:
        return None
    day = value[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return None
    return day


def is_placeholder(value: str | None) -> bool:
    return bool(value and value.startswith(PLACEHOLDER_DATETIME_PREFIX))


def valid_day_from_row(row: sqlite3.Row) -> str | None:
    for column in ("creationDate", "digitizationDate"):
        day = day_from_datetime(row[column])
        if day and day > PLACEHOLDER_DATE:
            return day
    return None


def read_placeholder_images(conn: sqlite3.Connection) -> list[sqlite3.Row]:
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
          AND (
                ImageInformation.creationDate LIKE ?
             OR ImageInformation.digitizationDate LIKE ?
          )
        ORDER BY Albums.relativePath, Images.name
        """,
        (f"{PLACEHOLDER_DATETIME_PREFIX}%", f"{PLACEHOLDER_DATETIME_PREFIX}%"),
    ).fetchall()


def neighbor_rows_for_album(conn: sqlite3.Connection, album_id: int, exclude_image_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT ImageInformation.creationDate, ImageInformation.digitizationDate
        FROM Images
        JOIN ImageInformation ON ImageInformation.imageid = Images.id
        WHERE Images.album = ?
          AND Images.id != ?
          AND Images.status < 3
        """,
        (album_id, exclude_image_id),
    ).fetchall()


def build_plan_for_image(
    conn: sqlite3.Connection,
    image: sqlite3.Row,
    min_valid_neighbors: int,
    dominant_threshold: float,
    default_time: str,
) -> ImageDatePlan:
    neighbors = neighbor_rows_for_album(conn, int(image["album_id"]), int(image["image_id"]))
    valid_days = [day for row in neighbors if (day := valid_day_from_row(row))]
    counts = Counter(valid_days)
    top_dates = counts.most_common(10)
    plan = ImageDatePlan(
        image_id=int(image["image_id"]),
        relative_path=image["relativePath"],
        name=image["name"],
        current_creation_date=image["creationDate"],
        current_digitization_date=image["digitizationDate"],
        album_item_count=len(neighbors) + 1,
        valid_neighbor_count=len(valid_days),
        distinct_neighbor_dates=len(counts),
        top_dates=top_dates,
    )

    if len(valid_days) < min_valid_neighbors:
        plan.reason = f"not enough valid neighboring dates ({len(valid_days)} < {min_valid_neighbors})"
        return plan

    if len(counts) == 1:
        plan.proposed_date = top_dates[0][0]
        plan.proposed_datetime = f"{plan.proposed_date}T{default_time}.000"
        plan.action = "update"
        plan.reason = "single neighboring date"
        return plan

    top_date, top_count = top_dates[0]
    dominance = top_count / len(valid_days)
    if dominance >= dominant_threshold:
        plan.proposed_date = top_date
        plan.proposed_datetime = f"{plan.proposed_date}T{default_time}.000"
        plan.action = "update"
        plan.reason = f"dominant neighboring date covers {top_count}/{len(valid_days)} valid neighbors"
        return plan

    plan.reason = f"multi-day album skipped; top date covers {top_count}/{len(valid_days)} valid neighbors"
    return plan


def validate_time(value: str) -> str:
    try:
        parsed = dt.datetime.strptime(value, "%H:%M:%S")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected time as HH:MM:SS") from exc
    return parsed.strftime("%H:%M:%S")


def build_plans(
    db: Path,
    min_valid_neighbors: int,
    dominant_threshold: float,
    default_time: str,
) -> list[ImageDatePlan]:
    conn = open_sqlite(db, readonly=True)
    try:
        require_tables(conn, {"Albums", "Images", "ImageInformation"})
        images = read_placeholder_images(conn)
        return [
            build_plan_for_image(conn, image, min_valid_neighbors, dominant_threshold, default_time)
            for image in images
        ]
    finally:
        conn.close()


def backup_database(path: Path) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, backup)
    return backup


def write_image_dates(db: Path, plans: list[ImageDatePlan]) -> int:
    writable = [plan for plan in plans if plan.action == "update" and plan.proposed_datetime]
    conn = open_sqlite(db, readonly=False)
    try:
        require_tables(conn, {"ImageInformation"})
        changed = 0
        with conn:
            for plan in writable:
                cursor = conn.execute(
                    """
                    UPDATE ImageInformation
                    SET creationDate = ?, digitizationDate = ?
                    WHERE imageid = ?
                      AND (creationDate LIKE ? OR digitizationDate LIKE ?)
                    """,
                    (
                        plan.proposed_datetime,
                        plan.proposed_datetime,
                        plan.image_id,
                        f"{PLACEHOLDER_DATETIME_PREFIX}%",
                        f"{PLACEHOLDER_DATETIME_PREFIX}%",
                    ),
                )
                changed += cursor.rowcount
        return changed
    finally:
        conn.close()


def write_report(report: Path, plans: list[ImageDatePlan], backup: Path | None, wrote: bool) -> None:
    payload = {
        "wrote": wrote,
        "backup": str(backup) if backup else None,
        "candidate_image_count": len(plans),
        "update_count": sum(1 for plan in plans if plan.action == "update"),
        "skip_count": sum(1 for plan in plans if plan.action != "update"),
        "images": [
            {
                "image_id": plan.image_id,
                "path": f"{plan.relative_path}/{plan.name}",
                "current_creation_date": plan.current_creation_date,
                "current_digitization_date": plan.current_digitization_date,
                "album_item_count": plan.album_item_count,
                "valid_neighbor_count": plan.valid_neighbor_count,
                "distinct_neighbor_dates": plan.distinct_neighbor_dates,
                "top_dates": plan.top_dates,
                "proposed_date": plan.proposed_date,
                "proposed_datetime": plan.proposed_datetime,
                "action": plan.action,
                "reason": plan.reason,
            }
            for plan in plans
        ],
    }
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("digikam_db", type=Path, help="digiKam digikam4.db SQLite database")
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("fix-image-dates-report.json"),
        help="JSON report path.",
    )
    parser.add_argument(
        "--min-valid-neighbors",
        type=int,
        default=1,
        help="Minimum number of non-placeholder neighboring dates required before proposing an image date.",
    )
    parser.add_argument(
        "--dominant-threshold",
        type=float,
        default=0.80,
        help="For multi-day albums, update only if one date covers this fraction of valid neighboring dates.",
    )
    parser.add_argument(
        "--default-time",
        type=validate_time,
        default="12:00:00",
        help="Time to use for inferred dates, as HH:MM:SS.",
    )
    parser.add_argument("--write", action="store_true", help="Write proposed image dates to the digiKam database.")
    args = parser.parse_args(argv)

    db = args.digikam_db.expanduser()
    if not db.exists():
        print(f"digiKam database not found: {db}", file=sys.stderr)
        return 2
    if args.min_valid_neighbors < 1:
        print("--min-valid-neighbors must be >= 1", file=sys.stderr)
        return 2
    if not 0 < args.dominant_threshold <= 1:
        print("--dominant-threshold must be > 0 and <= 1", file=sys.stderr)
        return 2

    plans = build_plans(db, args.min_valid_neighbors, args.dominant_threshold, args.default_time)

    backup = None
    changed = 0
    if args.write:
        backup = backup_database(db)
        changed = write_image_dates(db, plans)

    write_report(args.report, plans, backup, args.write)

    print(f"Placeholder images/movies: {len(plans)}")
    print(f"Proposed updates: {sum(1 for plan in plans if plan.action == 'update')}")
    print(f"Skipped: {sum(1 for plan in plans if plan.action != 'update')}")
    print(f"Report: {args.report}")
    for plan in plans:
        marker = "UPDATE" if plan.action == "update" else "SKIP"
        print(
            f"{marker}: {plan.relative_path}/{plan.name} "
            f"{plan.current_creation_date} -> {plan.proposed_datetime or '-'} ({plan.reason})"
        )
    if args.write:
        print(f"Backup: {backup}")
        print(f"Image rows changed: {changed}")
    else:
        print("Dry run only. Re-run with --write after closing digiKam to modify the database.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
