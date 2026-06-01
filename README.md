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
  - Earlier catalog export/report script for JSON and CSV analysis.

- `generate_supplemental_xmp.py`
  - Earlier supplemental XMP planning script for cataloged files without
    sidecars.

## Requirements

- Python 3.10 or newer.
- A Lightroom Classic `.lrcat` file.
- The directory where the photos are stored. Make backups first.
- digiKam configured to read XMP sidecars.

No third-party Python packages are required.

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
