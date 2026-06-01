# Catalog Import Export Package

This folder contains scripts and generated artifacts for migrating the extracted
Lightroom catalog toward digiKam, Photo Supreme, or another DAM.

## Scripts

- `scripts/export_catalog_package.py`
  - Reads the Lightroom `.lrcat` in immutable/read-only mode.
  - Maps `/Volumes/Media/Picture Files/` to `/Volumes/Media Disk/Picture Files/`.
  - Writes JSON, CSV, missing-file, extra-file, and report outputs.

- `scripts/generate_supplemental_xmp.py`
  - Reads `output/photo_supreme_import.csv`.
  - Writes `output/xmp_generation_plan.csv` for cataloged files that have useful
    metadata but no XMP sidecar.
  - With `--write-files`, writes supplemental XMP files under
    `output/supplemental_xmp` without changing the original photo folders.

- `scripts/lr_to_digikam_xmp.py`
  - Reads the Lightroom `.lrcat` in read-only mode.
  - Writes digiKam explicit sidecars next to copied image files:
    `filename.ext.xmp`.
  - Uses existing Lightroom commercial RAW sidecars such as `filename.xmp` only
    as templates; it does not modify them.
  - Migrates Lightroom keywords, collection membership, collection hierarchy,
    ratings, pick/reject labels, stack/virtual-copy marker tags, and capture
    dates into XMP.
  - Infers missing capture dates from year/month/day folder paths by default.

- `scripts/lr_prune_not_in_catalog.py`
  - Compares a copied disk tree against Lightroom catalog paths after applying
    path mappings.
  - Reports media files that are present on disk but not referenced by
    Lightroom.
  - Dry-run by default; can move candidates to quarantine or permanently delete
    them when explicitly requested.

## Regenerate

```bash
python3 scripts/export_catalog_package.py
python3 scripts/generate_supplemental_xmp.py
```

## digiKam XMP Export

Dry run:

```bash
python3 scripts/lr_to_digikam_xmp.py \
  "Lightroom Catalog-2-v13-3/Lightroom Catalog-2-v13-3.lrcat" \
  --path-prefix "/Volumes/Media/Picture Files=/Volumes/Media Disk/Picture Files"
```

Write sidecars:

```bash
python3 scripts/lr_to_digikam_xmp.py \
  "Lightroom Catalog-2-v13-3/Lightroom Catalog-2-v13-3.lrcat" \
  --path-prefix "/Volumes/Media/Picture Files=/Volumes/Media Disk/Picture Files" \
  --manifest "/Volumes/Media Disk/Picture Files/lightroom-xmp-migration-manifest.json" \
  --write
```

Run only for one folder:

```bash
python3 scripts/lr_to_digikam_xmp.py \
  "Lightroom Catalog-2-v13-3/Lightroom Catalog-2-v13-3.lrcat" \
  --path-prefix "/Volumes/Media/Picture Files=/Volumes/Media Disk/Picture Files" \
  --only-under "/Volumes/Media Disk/Picture Files/2026/04/London Trip/27" \
  --write
```
