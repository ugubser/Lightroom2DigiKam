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

This suite has been tested against digiKam 9.0 using explicit sidecars in the
`filename.ext.xmp` form and a SQLite `digikam4.db` database. The XMP workflow is
the portable migration path; the optional group importer writes directly to the
digiKam database and should be treated as version-sensitive.

## Validated Migration Flow

This is the end-to-end flow tested with digiKam 9.0.

1. Make a working copy of the photo tree.

   Do not run the migration directly against the only copy of the originals.
   The exporter writes only XMP sidecars, but the workflow is easier to test
   and reset when it runs against a copied photo tree.

2. Generate digiKam explicit sidecars from the Lightroom catalog.

   ```bash
   python3 lr_to_digikam_xmp.py \
     "/path/to/Lightroom Catalog.lrcat" \
     --path-prefix "/original/photo/root=/copied/photo/root" \
     --manifest "/copied/photo/root/lightroom-xmp-migration-manifest.json" \
     --write
   ```

   For repeated full-library runs, `--no-backup` is useful to avoid creating a
   large number of `.bak` files for sidecars that are intentionally regenerated.

   The exporter writes `filename.ext.xmp` files and migrates Lightroom keywords,
   collections, ratings, pick/reject labels, capture dates, inferred dates, and
   orientation/rotation. Lightroom stacks are not written as tags; they are
   applied later as native digiKam groups.

3. Create a fresh digiKam database and add the copied photo tree as an album
   root.

   In digiKam metadata settings, make sure:

   - Reading XMP sidecars is enabled.
   - Commercial-compatible sidecar names are disabled.
   - If you want digiKam writes to stay out of image files, write metadata to
     sidecars only.

   The important sidecar setting is that digiKam reads explicit sidecars such
   as `IMG_0001.RW2.xmp`, not commercial-compatible `IMG_0001.xmp`.

4. Let digiKam finish indexing the photo tree and reading metadata.

   If needed, use digiKam maintenance to read metadata from sidecars into the
   database. Sorting and filters can be misleading while indexing is still
   running.

5. Close digiKam.

   The group importer writes directly to `digikam4.db`; digiKam must not be
   running while this script writes.

6. Apply Lightroom stacks as native digiKam groups.

   First dry-run:

   ```bash
   python3 apply_digikam_groups.py \
     "/path/to/Lightroom Catalog.lrcat" \
     "/path/to/digikam4.db" \
     --path-prefix "/original/photo/root=/copied/photo/root" \
     --digikam-root "/copied/photo/root" \
     --report "./digikam-group-dry-run.json"
   ```

   Then write:

   ```bash
   python3 apply_digikam_groups.py \
     "/path/to/Lightroom Catalog.lrcat" \
     "/path/to/digikam4.db" \
     --path-prefix "/original/photo/root=/copied/photo/root" \
     --digikam-root "/copied/photo/root" \
     --report "./digikam-group-apply-report.json" \
     --write
   ```

   The script creates a timestamped backup of `digikam4.db` before writing.

7. Reopen digiKam and validate.

   Check representative folders for collection tags, ratings, pick/reject
   labels, dates, rotated images, and grouped stacks.

## Main Scripts

- `lr_to_digikam_xmp.py`
  - Reads Lightroom catalog paths, keywords, collections, ratings, pick/reject
    labels, capture dates, and orientation/rotation.
  - Writes or updates digiKam explicit sidecars such as `IMG_0001.RW2.xmp`.
  - Uses existing Lightroom commercial RAW sidecars such as `IMG_0001.xmp` only
    as templates for the explicit digiKam sidecar.
  - Infers missing capture dates from year/month/day folder paths by default.
  - Does not write Lightroom stack or virtual-copy marker tags. Native digiKam
    groups are handled separately by `apply_digikam_groups.py`.

- `lr_prune_not_in_catalog.py`
  - Compares a copied photo tree against paths referenced by Lightroom.
  - Reports files present on disk but not referenced by Lightroom.
  - Dry-run by default; can quarantine or delete candidates when explicitly
    requested.

- `apply_digikam_groups.py`
  - Reads Lightroom stacks and applies them as native digiKam groups in
    `digikam4.db`.
  - Dry-run by default; requires `--write` before it modifies the digiKam
    database.
  - Creates a timestamped backup of `digikam4.db` before writing.
  - Should only be run with digiKam closed, after digiKam has imported the
    photo tree and read the generated XMP sidecars.

- `remove_denoise_dngs_from_groups.py`
  - Optional maintenance script, not part of the normal migration flow.
  - Finds Lightroom AI denoise DNGs named like `*-Enhanced-NR*.dng` in a
    digiKam database and removes them from native digiKam groups.
  - If a denoise DNG is the group leader, promotes a non-denoise group member
    where possible.
  - Dry-run by default; requires `--write` before it modifies `digikam4.db`.
  - Can optionally move the denoise DNG files and their XMP sidecars to a
    quarantine folder with `--move-to`.

- `fix_album_dates.py`
  - Optional maintenance script, not part of the normal migration flow.
  - Finds digiKam albums whose album date is the placeholder `1904-01-01`.
  - Proposes replacement album dates from valid image dates inside each album.
  - Skips multi-day albums by default unless one date strongly dominates.
  - Dry-run by default; requires `--write` before it modifies `digikam4.db`.
  - Creates a timestamped backup of `digikam4.db` before writing.

- `fix_image_dates.py`
  - Optional maintenance script, not part of the normal migration flow.
  - Finds digiKam image/movie rows whose creation or digitization date is the
    placeholder `1904-01-01`.
  - Proposes replacement dates from valid neighboring image dates in the same
    album.
  - Uses noon as the inferred time by default because only the date can be
    inferred reliably.
  - Skips multi-day albums by default unless one date strongly dominates.
  - Dry-run by default; requires `--write` before it modifies `digikam4.db`.
  - Creates a timestamped backup of `digikam4.db` before writing.

- `fix_dates_from_path.py`
  - Work-in-progress optional maintenance script, not part of the normal
    migration flow.
  - Compares digiKam image dates and album dates against dated folder paths.
  - Exact `/YYYY/MM/DD` paths can update image dates while preserving the time
    of day, and write matching explicit `filename.ext.xmp` sidecars.
  - Month-only or year-only paths are reported but not changed, because the
    exact day cannot be inferred safely.
  - Supports `--album-only` and `--picture-only` to restrict writes.
  - Dry-run by default; requires `--write` before it modifies `digikam4.db` or
    writes sidecars.
  - Creates a timestamped backup of `digikam4.db` before writing.

- `find_orphan_raw_jpegs.py`
  - Optional maintenance script, not part of the normal migration flow.
  - Reads digiKam read-only and finds JPG/JPEG files from a selected camera
    model that have no same-stem RAW counterpart in the same album.
  - Can scan one album or a whole album subtree.
  - Dry-run by default; with `--write`, updates only explicit JPG sidecars and
    marks candidates rejected/no-good.
  - Does not modify the digiKam database.

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
- Tested with digiKam 9.0 and SQLite-backed `digikam4.db`.

No third-party Python packages are required.

Recommended digiKam metadata settings for this workflow:

- Read metadata from sidecar files.
- Write metadata to sidecar files only, if you want digiKam writes to stay out
  of image files.
- Leave commercial-compatible sidecar names disabled. This workflow writes and
  expects explicit sidecars such as `IMG_0001.RW2.xmp`, not `IMG_0001.xmp`.

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

## digiKam Groups

After digiKam has indexed the photo tree and read the generated sidecars, you
can apply Lightroom stacks as native digiKam groups. Close digiKam before
running this with `--write`.

Lightroom calls these records stacks. digiKam stores the equivalent native
relationship as groups in `ImageRelations`. The script maps Lightroom's
top-of-stack image to the digiKam group leader and groups the remaining stack
members behind it.

Tested full-library run against digiKam 9.0:

```text
Lightroom stack rows: 4910
Candidate groups: 1224
Writable groups: 1195
Writable relations: 3097
Skipped groups: 29
```

The skipped groups in that run were already present from a prior small-folder
test. After the full run, the database contained 3,177 native digiKam group
relations.

Dry run:

```bash
python3 apply_digikam_groups.py \
  "/path/to/Lightroom Catalog.lrcat" \
  "/path/to/digikam4.db" \
  --path-prefix "/original/photo/root=/photo/root" \
  --digikam-root "/photo/root" \
  --report "./digikam-group-dry-run.json"
```

Write groups:

```bash
python3 apply_digikam_groups.py \
  "/path/to/Lightroom Catalog.lrcat" \
  "/path/to/digikam4.db" \
  --path-prefix "/original/photo/root=/photo/root" \
  --digikam-root "/photo/root" \
  --report "./digikam-group-apply-report.json" \
  --write
```

Useful options:

```text
--write                         actually write digiKam group relations
--path-prefix OLD=NEW           map Lightroom paths to digiKam photo paths
--digikam-root FOLDER           fallback photo root for digiKam album paths
--only-under FOLDER             limit to stacks under one mapped folder
--replace-existing              replace existing digiKam group relations for affected images
--limit N                       process only the first N candidate groups
```

Without `--replace-existing`, existing digiKam group relations are preserved
and conflicting Lightroom stacks are reported instead of overwritten.

## Optional Denoise DNG Cleanup

Lightroom AI Denoise creates derivative DNG files named like
`*-Enhanced-NR*.dng`. If those files were imported and grouped in digiKam, use
this optional script to remove only those denoise DNGs from native digiKam
groups before deleting or moving them.

This is not part of the default migration. Use it only if you decide that the
Lightroom denoise DNG derivatives are not useful in digiKam.

Close digiKam before running this script with `--write`.

Dry run:

```bash
python3 remove_denoise_dngs_from_groups.py \
  "/path/to/digikam4.db" \
  --digikam-root "/photo/root" \
  --report "./remove-denoise-dngs-dry-run.json"
```

Remove denoise DNGs from digiKam groups only:

```bash
python3 remove_denoise_dngs_from_groups.py \
  "/path/to/digikam4.db" \
  --digikam-root "/photo/root" \
  --report "./remove-denoise-dngs-apply-report.json" \
  --write
```

Remove them from groups and move the DNG files plus sidecars to quarantine:

```bash
python3 remove_denoise_dngs_from_groups.py \
  "/path/to/digikam4.db" \
  --digikam-root "/photo/root" \
  --move-to "/path/to/denoise-dng-quarantine" \
  --report "./remove-denoise-dngs-apply-report.json" \
  --write
```

The script creates a timestamped backup of `digikam4.db` before writing. It
does not delete image files; `--move-to` moves them out of the photo tree for
review.

## Optional Album Date Cleanup

digiKam stores a date on each album/folder separately from the image capture
dates. Some albums can end up with the placeholder date `1904-01-01`. Use this
optional script to replace that album date when the images inside the album
provide a clear single-day date.

This is not part of the default migration. It writes directly to
`digikam4.db`, so close digiKam before running it with `--write`.

The default policy is conservative:

- only albums with `Albums.date = 1904-01-01` are considered
- image dates at or before `1904-01-01` are ignored
- single-day albums are proposed for update
- multi-day albums are skipped unless one date covers at least 80% of valid
  image dates
- a timestamped database backup is created before writing

Dry run:

```bash
python3 fix_album_dates.py \
  "/path/to/digikam4.db" \
  --report "./fix-album-dates-dry-run.json"
```

Write proposed album dates:

```bash
python3 fix_album_dates.py \
  "/path/to/digikam4.db" \
  --report "./fix-album-dates-apply-report.json" \
  --write
```

Useful options:

```text
--write                         actually update Albums.date
--min-valid-images N            require at least N usable image dates
--dominant-threshold FLOAT      default 0.80 for multi-day dominance
--allow-path-day                allow /YYYY/MM/DD folder paths to set the date when present in image dates
```

## Optional Image Date Cleanup

Some imported files, especially videos, can end up with placeholder image dates
such as `1904-01-01T00:00:00.000` in digiKam. Use this optional script when
neighboring files in the same album have usable dates and the placeholder file
should at least be placed on the correct day.

This is not part of the default migration. It writes directly to
`digikam4.db`, so close digiKam before running it with `--write`.

The default policy is conservative:

- only images/movies with `creationDate` or `digitizationDate` starting with
  `1904-01-01` are considered
- valid neighboring dates are read from other active items in the same album
- single-day albums are proposed for update
- multi-day albums are skipped unless one date covers at least 80% of valid
  neighboring dates
- proposed dates use `12:00:00` as the time by default
- a timestamped database backup is created before writing

Dry run:

```bash
python3 fix_image_dates.py \
  "/path/to/digikam4.db" \
  --report "./fix-image-dates-dry-run.json"
```

Write proposed image dates:

```bash
python3 fix_image_dates.py \
  "/path/to/digikam4.db" \
  --report "./fix-image-dates-apply-report.json" \
  --write
```

Useful options:

```text
--write                         actually update ImageInformation dates
--min-valid-neighbors N         require at least N usable neighboring dates
--dominant-threshold FLOAT      default 0.80 for multi-day dominance
--default-time HH:MM:SS         default 12:00:00 for inferred dates
```

## Optional Path-Based Date Audit

`fix_dates_from_path.py` is a work-in-progress maintenance script for cases
where digiKam image dates disagree with dated folder paths. It is intentionally
more cautious than the placeholder-date scripts.

The script can act only when the folder path contains an exact day:

```text
/YYYY/MM/DD
```

For those exact paths, it can update image dates while preserving the existing
time of day, and it can write the same corrected date fields to explicit
sidecars such as `filename.ext.xmp`. Month-only or year-only paths are reported
for review but not changed.

Dry run:

```bash
python3 fix_dates_from_path.py \
  "/path/to/digikam4.db" \
  --digikam-root "/photo/root" \
  --report "./fix-dates-from-path-dry-run.json"
```

Write proposed exact-day image/date fixes:

```bash
python3 fix_dates_from_path.py \
  "/path/to/digikam4.db" \
  --digikam-root "/photo/root" \
  --report "./fix-dates-from-path-apply-report.json" \
  --write
```

Useful options:

```text
--write                         update digiKam and write XMP sidecars
--digikam-root FOLDER           photo root used to locate filename.ext.xmp sidecars
--album-only                    only write album date fixes
--picture-only                  only write image date fixes
--no-sidecars                   update digiKam only; do not write XMP sidecars
```

## Optional Orphan RAW+JPG Cleanup

`find_orphan_raw_jpegs.py` helps find JPG files that likely belonged to a
RAW+JPG pair where the RAW file was deleted or rejected, but the JPG survived.
This is useful for camera bodies that normally shoot RAW+JPG.

The script reads `digikam4.db` in read-only mode. It matches JPG/JPEG files
against RAW files by same filename stem inside the same digiKam album. Passing
an album subtree, such as a trip folder, scans all albums below that path by
default.

List camera models in a subtree:

```bash
python3 find_orphan_raw_jpegs.py \
  "/path/to/digikam4.db" \
  "/photo/root/2026/04/London Trip" \
  --photo-root "/photo/root" \
  --list-models
```

Dry run for one camera model:

```bash
python3 find_orphan_raw_jpegs.py \
  "/path/to/digikam4.db" \
  "/photo/root/2026/04/London Trip" \
  --photo-root "/photo/root" \
  --model "DC-GX9"
```

Mark candidate JPG sidecars as rejected/no-good:

```bash
python3 find_orphan_raw_jpegs.py \
  "/path/to/digikam4.db" \
  "/photo/root/2026/04/London Trip" \
  --photo-root "/photo/root" \
  --model "DC-GX9" \
  --write
```

Useful options:

```text
--list-models                   list camera models in the album/subtree
--model MODEL                   exact camera model to inspect
--make MAKE                     optional exact camera make filter
--no-recursive                  inspect only the exact album
--raw-extensions EXT[,EXT...]   override RAW extensions used for counterpart matching
--write                         mark candidate JPG sidecars rejected/no-good
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
