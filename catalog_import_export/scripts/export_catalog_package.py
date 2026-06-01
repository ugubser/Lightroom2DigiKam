#!/usr/bin/env python3
"""Export a Lightroom catalog into portable DAM import artifacts.

Outputs:
  - catalog_manifest.json: streaming JSON array with one record per Lightroom image
  - photo_supreme_import.csv: flat CSV suitable for DAM import/mapping
  - missing_files.csv: catalog entries whose originals do not resolve
  - extra_files.csv: files in the media root that are not catalog originals/sidecars
  - migration_report.md: human-readable summary
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path


CATALOG_DEFAULT = (
    "/Volumes/ON1Setup/LightroomCatalogueTest/Lightroom Catalog-2-v13-3/"
    "Lightroom Catalog-2-v13-3.lrcat"
)
OUTPUT_DEFAULT = "/Volumes/ON1Setup/LightroomCatalogueTest/catalog_import_export/output"
FROM_ROOT_DEFAULT = "/Volumes/Media/Picture Files/"
TO_ROOT_DEFAULT = "/Volumes/Media Disk/Picture Files/"
SIDECAR_EXTENSIONS = ("xmp", "on1", "pp3", "acr", "ori_xmp", "orf_xmp")


def path_key(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def connect_catalog(catalog: str) -> sqlite3.Connection:
    uri_path = Path(catalog).as_posix().replace(" ", "%20")
    conn = sqlite3.connect(f"file:{uri_path}?immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def mapped_path(root: str, rel: str, base: str, ext: str, from_root: str, to_root: str) -> str:
    original = f"{root or ''}{rel or ''}{base or ''}{('.' + ext) if ext else ''}"
    if original.startswith(from_root):
        return to_root + original[len(from_root) :]
    return original


def hierarchy_name(items: dict[int, sqlite3.Row], item_id: int) -> str:
    parts: list[str] = []
    seen: set[int] = set()
    current = item_id
    while current and current in items and current not in seen:
        seen.add(current)
        name = items[current]["name"]
        if name:
            parts.append(str(name))
        current = items[current]["parent"]
    return "|".join(reversed(parts))


def load_lookup_maps(conn: sqlite3.Connection) -> tuple[dict[int, list[str]], dict[int, list[str]]]:
    keywords = {
        row["id_local"]: row
        for row in conn.execute("SELECT id_local, name, parent FROM AgLibraryKeyword")
    }
    keyword_by_image: dict[int, list[str]] = defaultdict(list)
    for row in conn.execute("SELECT image, tag FROM AgLibraryKeywordImage"):
        name = hierarchy_name(keywords, row["tag"])
        if name:
            keyword_by_image[row["image"]].append(name)

    collections = {
        row["id_local"]: row
        for row in conn.execute(
            "SELECT id_local, name, parent FROM AgLibraryCollection WHERE COALESCE(systemOnly, 0) = 0"
        )
    }
    collection_by_image: dict[int, list[str]] = defaultdict(list)
    for row in conn.execute("SELECT image, collection FROM AgLibraryCollectionImage"):
        name = hierarchy_name(collections, row["collection"])
        if name:
            collection_by_image[row["image"]].append(name)

    return keyword_by_image, collection_by_image


def sidecar_paths_for(file_path: str) -> dict[str, str | None]:
    stem = os.path.splitext(file_path)[0]
    result: dict[str, str | None] = {}
    for ext in SIDECAR_EXTENSIONS:
        candidate = f"{stem}.{ext}"
        result[ext] = candidate if os.path.exists(candidate) else None
    return result


def export(args: argparse.Namespace) -> None:
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    conn = connect_catalog(args.catalog)
    keyword_by_image, collection_by_image = load_lookup_maps(conn)

    sql = """
        SELECT
          ai.id_local AS image_id,
          ai.id_global AS image_global_id,
          ai.rootFile AS file_id,
          ai.masterImage AS master_image_id,
          ai.fileFormat AS file_format,
          ai.fileWidth AS file_width,
          ai.fileHeight AS file_height,
          ai.captureTime AS capture_time,
          ai.originalCaptureTime AS original_capture_time,
          ai.rating AS rating,
          ai.pick AS pick,
          ai.colorLabels AS color_label,
          ai.copyName AS copy_name,
          ai.hasMissingSidecars AS has_missing_sidecars,
          ai.sidecarStatus AS sidecar_status,
          lf.baseName AS base_name,
          lf.extension AS extension,
          lf.originalFilename AS original_filename,
          lf.sidecarExtensions AS declared_sidecars,
          rf.absolutePath AS root_path,
          f.pathFromRoot AS folder_from_root,
          ex.dateYear AS exif_year,
          ex.dateMonth AS exif_month,
          ex.dateDay AS exif_day,
          ex.aperture AS aperture,
          ex.shutterSpeed AS shutter_speed,
          ex.isoSpeedRating AS iso,
          ex.focalLength AS focal_length,
          ex.hasGPS AS has_gps,
          ex.gpsLatitude AS gps_latitude,
          ex.gpsLongitude AS gps_longitude,
          cm.value AS camera_model,
          lens.value AS lens,
          iptc.caption AS caption,
          iptc.copyright AS copyright,
          ds.processVersion AS process_version,
          ds.hasMasks AS has_masks,
          ds.hasAIMasks AS has_ai_masks,
          ds.hasLensBlur AS has_lens_blur,
          CASE WHEN ds.text IS NOT NULL AND length(ds.text) > 0 THEN 1 ELSE 0 END AS has_develop_text
        FROM Adobe_images ai
        JOIN AgLibraryFile lf ON ai.rootFile = lf.id_local
        JOIN AgLibraryFolder f ON lf.folder = f.id_local
        JOIN AgLibraryRootFolder rf ON f.rootFolder = rf.id_local
        LEFT JOIN AgHarvestedExifMetadata ex ON ex.image = ai.id_local
        LEFT JOIN AgInternedExifCameraModel cm ON ex.cameraModelRef = cm.id_local
        LEFT JOIN AgInternedExifLens lens ON ex.lensRef = lens.id_local
        LEFT JOIN AgLibraryIPTC iptc ON iptc.image = ai.id_local
        LEFT JOIN Adobe_imageDevelopSettings ds ON ds.image = ai.id_local
        ORDER BY ai.id_local
    """

    manifest_path = output_dir / "catalog_manifest.json"
    csv_path = output_dir / "photo_supreme_import.csv"
    missing_path = output_dir / "missing_files.csv"

    stats = Counter()
    file_format_counts = Counter()
    extension_counts = Counter()
    sidecar_counts = Counter()
    resolved_original_keys: set[str] = set()
    resolved_base_keys: set[str] = set()

    csv_fields = [
        "resolved_path",
        "original_lightroom_path",
        "exists",
        "image_id",
        "file_id",
        "file_format",
        "extension",
        "capture_time",
        "rating",
        "pick",
        "color_label",
        "caption",
        "copyright",
        "keywords",
        "collections",
        "camera_model",
        "lens",
        "aperture",
        "shutter_speed",
        "iso",
        "focal_length",
        "has_gps",
        "gps_latitude",
        "gps_longitude",
        "process_version",
        "has_masks",
        "has_ai_masks",
        "xmp_sidecar",
        "on1_sidecar",
        "declared_sidecars",
        "copy_name",
        "master_image_id",
    ]

    with manifest_path.open("w", encoding="utf-8") as manifest_file, csv_path.open(
        "w", newline="", encoding="utf-8"
    ) as csv_file, missing_path.open("w", newline="", encoding="utf-8") as missing_file:
        writer = csv.DictWriter(csv_file, fieldnames=csv_fields)
        missing_writer = csv.DictWriter(missing_file, fieldnames=csv_fields)
        writer.writeheader()
        missing_writer.writeheader()

        manifest_file.write("[\n")
        first = True
        for row in conn.execute(sql):
            original_path = (
                f"{row['root_path'] or ''}{row['folder_from_root'] or ''}"
                f"{row['base_name'] or ''}{('.' + row['extension']) if row['extension'] else ''}"
            )
            resolved_path = mapped_path(
                row["root_path"],
                row["folder_from_root"],
                row["base_name"],
                row["extension"],
                args.from_root,
                args.to_root,
            )
            exists = os.path.exists(resolved_path)
            sidecars = sidecar_paths_for(resolved_path)
            keywords = sorted(set(keyword_by_image.get(row["image_id"], [])))
            collections = sorted(set(collection_by_image.get(row["image_id"], [])))

            stats["images"] += 1
            stats["exists" if exists else "missing"] += 1
            file_format_counts[row["file_format"] or ""] += 1
            extension_counts[(row["extension"] or "").lower()] += 1
            for ext, value in sidecars.items():
                if value:
                    sidecar_counts[ext] += 1
            if exists:
                resolved_original_keys.add(path_key(resolved_path))
            resolved_base_keys.add(path_key(os.path.splitext(resolved_path)[0]))

            record = {
                "image_id": row["image_id"],
                "image_global_id": row["image_global_id"],
                "file_id": row["file_id"],
                "original_lightroom_path": original_path,
                "resolved_path": resolved_path,
                "exists": exists,
                "file": {
                    "base_name": row["base_name"],
                    "extension": row["extension"],
                    "original_filename": row["original_filename"],
                    "file_format": row["file_format"],
                    "width": row["file_width"],
                    "height": row["file_height"],
                    "declared_sidecars": row["declared_sidecars"],
                    "sidecars": {ext: value for ext, value in sidecars.items() if value},
                },
                "catalog": {
                    "rating": row["rating"],
                    "pick": row["pick"],
                    "color_label": row["color_label"],
                    "copy_name": row["copy_name"],
                    "master_image_id": row["master_image_id"],
                    "has_missing_sidecars": row["has_missing_sidecars"],
                    "sidecar_status": row["sidecar_status"],
                    "keywords": keywords,
                    "collections": collections,
                },
                "metadata": {
                    "capture_time": row["capture_time"],
                    "original_capture_time": row["original_capture_time"],
                    "camera_model": row["camera_model"],
                    "lens": row["lens"],
                    "aperture": row["aperture"],
                    "shutter_speed": row["shutter_speed"],
                    "iso": row["iso"],
                    "focal_length": row["focal_length"],
                    "has_gps": bool(row["has_gps"]),
                    "gps_latitude": row["gps_latitude"],
                    "gps_longitude": row["gps_longitude"],
                    "caption": row["caption"],
                    "copyright": row["copyright"],
                },
                "develop": {
                    "process_version": row["process_version"],
                    "has_masks": bool(row["has_masks"]),
                    "has_ai_masks": bool(row["has_ai_masks"]),
                    "has_lens_blur": bool(row["has_lens_blur"]),
                    "has_lightroom_develop_text": bool(row["has_develop_text"]),
                },
            }

            if not first:
                manifest_file.write(",\n")
            first = False
            manifest_file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))

            flat = {
                "resolved_path": resolved_path,
                "original_lightroom_path": original_path,
                "exists": "1" if exists else "0",
                "image_id": row["image_id"],
                "file_id": row["file_id"],
                "file_format": row["file_format"],
                "extension": row["extension"],
                "capture_time": row["capture_time"],
                "rating": row["rating"],
                "pick": row["pick"],
                "color_label": row["color_label"],
                "caption": row["caption"],
                "copyright": row["copyright"],
                "keywords": ";".join(keywords),
                "collections": ";".join(collections),
                "camera_model": row["camera_model"],
                "lens": row["lens"],
                "aperture": row["aperture"],
                "shutter_speed": row["shutter_speed"],
                "iso": row["iso"],
                "focal_length": row["focal_length"],
                "has_gps": "1" if row["has_gps"] else "0",
                "gps_latitude": row["gps_latitude"],
                "gps_longitude": row["gps_longitude"],
                "process_version": row["process_version"],
                "has_masks": "1" if row["has_masks"] else "0",
                "has_ai_masks": "1" if row["has_ai_masks"] else "0",
                "xmp_sidecar": sidecars["xmp"] or "",
                "on1_sidecar": sidecars["on1"] or "",
                "declared_sidecars": row["declared_sidecars"],
                "copy_name": row["copy_name"],
                "master_image_id": row["master_image_id"],
            }
            writer.writerow(flat)
            if not exists:
                missing_writer.writerow(flat)

        manifest_file.write("\n]\n")

    extra_path = output_dir / "extra_files.csv"
    extra_counts = Counter()
    extra_total = 0
    sidecar_for_catalog = 0
    if Path(args.to_root).exists():
        with extra_path.open("w", newline="", encoding="utf-8") as extra_file:
            extra_writer = csv.DictWriter(extra_file, fieldnames=["path", "extension", "classification"])
            extra_writer.writeheader()
            for dirpath, _, filenames in os.walk(args.to_root):
                for name in filenames:
                    path = os.path.join(dirpath, name)
                    ext = os.path.splitext(name)[1].lower().lstrip(".")
                    key = path_key(path)
                    base_key = path_key(os.path.splitext(path)[0])
                    if key in resolved_original_keys:
                        continue
                    if base_key in resolved_base_keys and ext in SIDECAR_EXTENSIONS:
                        sidecar_for_catalog += 1
                        continue
                    extra_total += 1
                    extra_counts[ext or "[none]"] += 1
                    extra_writer.writerow(
                        {"path": path, "extension": ext, "classification": "extra_or_unmatched"}
                    )

    report_path = output_dir / "migration_report.md"
    with report_path.open("w", encoding="utf-8") as report:
        report.write("# Lightroom Catalog Export Report\n\n")
        report.write(f"- Catalog: `{args.catalog}`\n")
        report.write(f"- Lightroom root mapped from: `{args.from_root}`\n")
        report.write(f"- Lightroom root mapped to: `{args.to_root}`\n")
        report.write(f"- Image records exported: {stats['images']:,}\n")
        report.write(f"- Resolved originals: {stats['exists']:,}\n")
        report.write(f"- Missing originals: {stats['missing']:,}\n")
        report.write(f"- Matching sidecar files under media root: {sidecar_for_catalog:,}\n")
        report.write(f"- Extra/unmatched files under media root: {extra_total:,}\n\n")

        report.write("## File Formats\n\n")
        for key, count in file_format_counts.most_common():
            report.write(f"- {key or '[blank]'}: {count:,}\n")

        report.write("\n## Extensions\n\n")
        for key, count in extension_counts.most_common(30):
            report.write(f"- {key or '[blank]'}: {count:,}\n")

        report.write("\n## Existing Sidecars Found By Basename\n\n")
        for key, count in sidecar_counts.most_common():
            report.write(f"- .{key}: {count:,}\n")

        report.write("\n## Extra/Unmatched File Extensions\n\n")
        for key, count in extra_counts.most_common(30):
            report.write(f"- {key}: {count:,}\n")

        report.write("\n## Generated Files\n\n")
        for path in (manifest_path, csv_path, missing_path, extra_path):
            size = path.stat().st_size if path.exists() else 0
            report.write(f"- `{path}` ({size:,} bytes)\n")

    print(f"Wrote {manifest_path}")
    print(f"Wrote {csv_path}")
    print(f"Wrote {missing_path}")
    print(f"Wrote {extra_path}")
    print(f"Wrote {report_path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", default=CATALOG_DEFAULT)
    parser.add_argument("--output", default=OUTPUT_DEFAULT)
    parser.add_argument("--from-root", default=FROM_ROOT_DEFAULT)
    parser.add_argument("--to-root", default=TO_ROOT_DEFAULT)
    export(parser.parse_args())


if __name__ == "__main__":
    main()
