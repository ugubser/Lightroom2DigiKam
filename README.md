# Lightroom2DigiKam

Utilities for migrating organization metadata from an Adobe Lightroom Classic
catalog into digiKam-readable sidecar files.

The main workflow reads a Lightroom `.lrcat` SQLite database in read-only mode
and writes explicit digiKam XMP sidecars next to copied image files:

```text
filename.ext.xmp
```

The scripts do not write to the Lightroom catalog. The main exporter also does
not write into image files.

## Main Scripts

- `lr_to_digikam_xmp.py`
  - Reads Lightroom catalog paths, keywords, collections, ratings, pick/reject
    labels, stack markers, virtual-copy markers, and capture dates.
  - Writes or updates digiKam explicit sidecars such as `IMG_0001.RW2.xmp`.
  - Uses existing Lightroom commercial RAW sidecars such as `IMG_0001.xmp` only
    as templates for the explicit digiKam sidecar.
  - Infers missing capture dates from year/month/day folder paths by default.

- `lr_prune_not_in_catalog.py`
  - Compares a copied photo tree against paths referenced by Lightroom.
  - Reports files present on disk but not referenced by Lightroom.
  - Dry-run by default; can quarantine or delete candidates when explicitly
    requested.

## Supporting Scripts

- `export_catalog_package.py`
  - Exports a read-only Lightroom catalog analysis package.
  - Writes `catalog_manifest.json`, `photo_supreme_import.csv`,
    `missing_files.csv`, `extra_files.csv`, `folder_labels.csv`,
    `folder_labels.json`, and `migration_report.md`.
  - Useful for auditing the catalog before or after XMP migration, including
    Lightroom folder color labels such as blue/yellow cleanup markers.

- `generate_supplemental_xmp.py`
  - Reads `photo_supreme_import.csv` from the analysis package.
  - Creates `xmp_generation_plan.csv` for cataloged files that have useful
    metadata but no existing XMP sidecar.
  - Optionally writes standalone supplemental XMP files under
    `supplemental_xmp/`. This is separate from the main digiKam exporter.

## Requirements

- Python 3.10 or newer.
- A Lightroom Classic `.lrcat` file.
- The directory where the photos are stored. Make backups first.
- digiKam configured to read XMP sidecars.

No third-party Python packages are required.

## Catalog Analysis Report

Generate the report package:

```bash
python3 export_catalog_package.py \
  --catalog "/path/to/Lightroom Catalog.lrcat" \
  --from-root "/original/photo/root/" \
  --to-root "/photo/root/" \
  --output "./output"
```

Generated files:

```text
output/catalog_manifest.json       detailed JSON record per Lightroom image
output/photo_supreme_import.csv    flat CSV export for analysis/import mapping
output/missing_files.csv           catalog records whose originals are missing
output/extra_files.csv             files under the photo root not matched to catalog originals
output/folder_labels.csv           Lightroom folder color labels
output/folder_labels.json          same folder labels in JSON
output/migration_report.md         human-readable summary
```

The folder-label outputs come from Lightroom's folder color labels. They are
not written to XMP and are intended as migration bookkeeping.

Plan supplemental XMP files from the analysis CSV:

```bash
python3 generate_supplemental_xmp.py --output "./output"
```

That writes:

```text
output/xmp_generation_plan.csv
```

Optionally create standalone supplemental XMP files in an output mirror tree:

```bash
python3 generate_supplemental_xmp.py --output "./output" --write-files
```

This writes under:

```text
output/supplemental_xmp/
```

It does not modify the original photo folders.

## digiKam Export

Dry run:

```bash
python3 lr_to_digikam_xmp.py \
  "/path/to/Lightroom Catalog.lrcat" \
  --path-prefix "/original/photo/root=/copied/photo/root"
```

Write sidecars:

```bash
python3 lr_to_digikam_xmp.py \
  "/path/to/Lightroom Catalog.lrcat" \
  --path-prefix "/original/photo/root=/copied/photo/root" \
  --manifest "/copied/photo/root/lightroom-xmp-migration-manifest.json" \
  --write
```

Run only for one folder:

```bash
python3 lr_to_digikam_xmp.py \
  "/path/to/Lightroom Catalog.lrcat" \
  --path-prefix "/original/photo/root=/copied/photo/root" \
  --only-under "/copied/photo/root/2026/04/London Trip/27" \
  --write
```

Useful options:

```text
--write                         actually write sidecars
--path-prefix OLD=NEW           map Lightroom paths to copied photo paths
--only-under FOLDER             limit writes to one mapped target folder
--no-backup                     do not create .xmp.bak backups
--no-import-lightroom-sidecar   do not seed RAW sidecars from BASENAME.xmp
--no-infer-missing-dates-from-path
--limit N                       process only the first N mapped target files
--allow-missing                 create sidecars even if target images are absent
```

## Prune Report

Dry run:

```bash
python3 lr_prune_not_in_catalog.py \
  "/path/to/Lightroom Catalog.lrcat" \
  "/copied/photo/root" \
  --path-prefix "/original/photo/root=/copied/photo/root" \
  --report "/copied/photo/root/lightroom-prune-candidates.json"
```

Move candidates to a review folder:

```bash
python3 lr_prune_not_in_catalog.py \
  "/path/to/Lightroom Catalog.lrcat" \
  "/copied/photo/root" \
  --path-prefix "/original/photo/root=/copied/photo/root" \
  --quarantine "/path/to/review-folder"
```

Permanently delete candidates:

```bash
python3 lr_prune_not_in_catalog.py \
  "/path/to/Lightroom Catalog.lrcat" \
  "/copied/photo/root" \
  --path-prefix "/original/photo/root=/copied/photo/root" \
  --delete
```

Use `--delete` carefully. The comparison is path-based, not hash-based.
