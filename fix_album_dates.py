#!/usr/bin/env python3
"""
Fix placeholder digiKam album dates from image dates.

This script targets digiKam albums whose album date is 1904-01-01 and proposes
a replacement date from the images inside the album. It is intentionally
conservative: multi-day albums are skipped by default.

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


@dataclass
class AlbumPlan:
    album_id: int
    relative_path: str
    current_date: str | None
    image_count: int
    valid_image_count: int
    placeholder_image_count: int
    missing_image_count: int
    distinct_valid_dates: int
    top_dates: list[tuple[str, int]] = field(default_factory=list)
    path_date: str | None = None
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


def path_date_from_album(relative_path: str) -> str | None:
    parts = relative_path.strip("/").split("/")
    if len(parts) >= 3 and re.fullmatch(r"\d{4}", parts[0]) and re.fullmatch(r"\d{1,2}", parts[1]):
        if re.fullmatch(r"\d{1,2}", parts[2]):
            year = int(parts[0])
            month = int(parts[1])
            day = int(parts[2])
            try:
                return dt.date(year, month, day).isoformat()
            except ValueError:
                return None
    return None


def day_from_datetime(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) < 10:
        return None
    day = value[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return None
    return day


def read_placeholder_albums(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id, relativePath, date
        FROM Albums
        WHERE date = ?
        ORDER BY relativePath
        """,
        (PLACEHOLDER_DATE,),
    ).fetchall()


def build_plan_for_album(
    conn: sqlite3.Connection,
    album: sqlite3.Row,
    min_valid_images: int,
    dominant_threshold: float,
    allow_path_day: bool,
) -> AlbumPlan:
    rows = conn.execute(
        """
        SELECT ImageInformation.creationDate, ImageInformation.digitizationDate
        FROM Images
        LEFT JOIN ImageInformation ON ImageInformation.imageid = Images.id
        WHERE Images.album = ? AND Images.status < 3
        """,
        (album["id"],),
    ).fetchall()

    dates: list[str] = []
    placeholder_count = 0
    missing_count = 0
    for row in rows:
        day = day_from_datetime(row["creationDate"]) or day_from_datetime(row["digitizationDate"])
        if day is None:
            missing_count += 1
            continue
        if day <= PLACEHOLDER_DATE:
            placeholder_count += 1
            continue
        dates.append(day)

    counts = Counter(dates)
    top_dates = counts.most_common(10)
    path_date = path_date_from_album(album["relativePath"])
    plan = AlbumPlan(
        album_id=int(album["id"]),
        relative_path=album["relativePath"],
        current_date=album["date"],
        image_count=len(rows),
        valid_image_count=len(dates),
        placeholder_image_count=placeholder_count,
        missing_image_count=missing_count,
        distinct_valid_dates=len(counts),
        top_dates=top_dates,
        path_date=path_date,
    )

    if len(dates) < min_valid_images:
        plan.reason = f"not enough valid image dates ({len(dates)} < {min_valid_images})"
        return plan

    if len(counts) == 1:
        plan.proposed_date = top_dates[0][0]
        plan.action = "update"
        plan.reason = "single valid image date"
        return plan

    if allow_path_day and path_date and path_date in counts:
        plan.proposed_date = path_date
        plan.action = "update"
        plan.reason = "folder day is present in image dates"
        return plan

    top_date, top_count = top_dates[0]
    dominance = top_count / len(dates)
    if dominance >= dominant_threshold:
        plan.proposed_date = top_date
        plan.action = "update"
        plan.reason = f"dominant date covers {top_count}/{len(dates)} valid images"
        return plan

    plan.reason = f"multi-day album skipped; top date covers {top_count}/{len(dates)} valid images"
    return plan


def build_plans(
    db: Path,
    min_valid_images: int,
    dominant_threshold: float,
    allow_path_day: bool,
) -> list[AlbumPlan]:
    conn = open_sqlite(db, readonly=True)
    try:
        require_tables(conn, {"Albums", "Images", "ImageInformation"})
        albums = read_placeholder_albums(conn)
        return [
            build_plan_for_album(conn, album, min_valid_images, dominant_threshold, allow_path_day)
            for album in albums
        ]
    finally:
        conn.close()


def backup_database(path: Path) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, backup)
    return backup


def write_album_dates(db: Path, plans: list[AlbumPlan]) -> int:
    writable = [plan for plan in plans if plan.action == "update" and plan.proposed_date]
    conn = open_sqlite(db, readonly=False)
    try:
        require_tables(conn, {"Albums"})
        changed = 0
        with conn:
            for plan in writable:
                cursor = conn.execute(
                    "UPDATE Albums SET date = ? WHERE id = ? AND date = ?",
                    (plan.proposed_date, plan.album_id, PLACEHOLDER_DATE),
                )
                changed += cursor.rowcount
        return changed
    finally:
        conn.close()


def write_report(report: Path, plans: list[AlbumPlan], backup: Path | None, wrote: bool) -> None:
    payload = {
        "wrote": wrote,
        "backup": str(backup) if backup else None,
        "candidate_album_count": len(plans),
        "update_count": sum(1 for plan in plans if plan.action == "update"),
        "skip_count": sum(1 for plan in plans if plan.action != "update"),
        "albums": [
            {
                "album_id": plan.album_id,
                "relative_path": plan.relative_path,
                "current_date": plan.current_date,
                "image_count": plan.image_count,
                "valid_image_count": plan.valid_image_count,
                "placeholder_image_count": plan.placeholder_image_count,
                "missing_image_count": plan.missing_image_count,
                "distinct_valid_dates": plan.distinct_valid_dates,
                "top_dates": plan.top_dates,
                "path_date": plan.path_date,
                "proposed_date": plan.proposed_date,
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
        default=Path("fix-album-dates-report.json"),
        help="JSON report path.",
    )
    parser.add_argument(
        "--min-valid-images",
        type=int,
        default=3,
        help="Minimum number of non-placeholder image dates required before proposing an album date.",
    )
    parser.add_argument(
        "--dominant-threshold",
        type=float,
        default=0.80,
        help="For multi-day albums, update only if one date covers this fraction of valid image dates.",
    )
    parser.add_argument(
        "--allow-path-day",
        action="store_true",
        help="Allow /YYYY/MM/DD album paths to set the album date when that day appears in image dates.",
    )
    parser.add_argument("--write", action="store_true", help="Write proposed album dates to the digiKam database.")
    args = parser.parse_args(argv)

    db = args.digikam_db.expanduser()
    if not db.exists():
        print(f"digiKam database not found: {db}", file=sys.stderr)
        return 2
    if not 0 < args.dominant_threshold <= 1:
        print("--dominant-threshold must be > 0 and <= 1", file=sys.stderr)
        return 2

    plans = build_plans(db, args.min_valid_images, args.dominant_threshold, args.allow_path_day)

    backup = None
    changed = 0
    if args.write:
        backup = backup_database(db)
        changed = write_album_dates(db, plans)

    write_report(args.report, plans, backup, args.write)

    print(f"Placeholder albums: {len(plans)}")
    print(f"Proposed updates: {sum(1 for plan in plans if plan.action == 'update')}")
    print(f"Skipped: {sum(1 for plan in plans if plan.action != 'update')}")
    print(f"Report: {args.report}")
    for plan in plans:
        marker = "UPDATE" if plan.action == "update" else "SKIP"
        print(f"{marker}: {plan.relative_path} {plan.current_date} -> {plan.proposed_date or '-'} ({plan.reason})")
    if args.write:
        print(f"Backup: {backup}")
        print(f"Album rows changed: {changed}")
    else:
        print("Dry run only. Re-run with --write after closing digiKam to modify the database.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
