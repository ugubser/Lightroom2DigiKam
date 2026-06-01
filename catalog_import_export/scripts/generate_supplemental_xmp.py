#!/usr/bin/env python3
"""Create a plan, or optional files, for supplemental XMP sidecars.

By default this script only writes xmp_generation_plan.csv. Use --write-files to
write standalone XMP files into an output mirror tree; it does not modify the
original photo folders.
"""

from __future__ import annotations

import argparse
import csv
import html
from pathlib import Path


OUTPUT_DEFAULT = "/Volumes/ON1Setup/LightroomCatalogueTest/catalog_import_export/output"


def xmp_text(row: dict[str, str]) -> str:
    keywords = [k for k in row.get("keywords", "").split(";") if k]
    subjects = "\n".join(f"     <rdf:li>{html.escape(k)}</rdf:li>" for k in keywords)
    rating = row.get("rating") or "0"
    label = row.get("color_label") or ""
    caption = row.get("caption") or ""
    capture_time = row.get("capture_time") or ""
    return f"""<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:xmp="http://ns.adobe.com/xap/1.0/"
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/"
    xmp:Rating="{html.escape(str(rating))}"
    xmp:Label="{html.escape(label)}"
    photoshop:DateCreated="{html.escape(capture_time)}">
   <dc:description>
    <rdf:Alt>
     <rdf:li xml:lang="x-default">{html.escape(caption)}</rdf:li>
    </rdf:Alt>
   </dc:description>
   <dc:subject>
    <rdf:Bag>
{subjects}
    </rdf:Bag>
   </dc:subject>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=OUTPUT_DEFAULT)
    parser.add_argument("--write-files", action="store_true")
    args = parser.parse_args()

    output = Path(args.output)
    import_csv = output / "photo_supreme_import.csv"
    plan_csv = output / "xmp_generation_plan.csv"
    xmp_root = output / "supplemental_xmp"
    count = 0

    with import_csv.open(newline="", encoding="utf-8") as src, plan_csv.open(
        "w", newline="", encoding="utf-8"
    ) as dst:
        reader = csv.DictReader(src)
        writer = csv.DictWriter(
            dst,
            fieldnames=[
                "resolved_path",
                "planned_xmp_path",
                "reason",
                "rating",
                "color_label",
                "keywords",
                "caption",
            ],
        )
        writer.writeheader()
        for row in reader:
            if row.get("exists") != "1":
                continue
            if row.get("xmp_sidecar"):
                continue
            if not any(row.get(k) for k in ("rating", "color_label", "keywords", "caption")):
                continue
            source = Path(row["resolved_path"])
            planned = xmp_root / source.relative_to(source.anchor).with_suffix(".xmp")
            writer.writerow(
                {
                    "resolved_path": row["resolved_path"],
                    "planned_xmp_path": str(planned),
                    "reason": "missing_xmp_has_catalog_metadata",
                    "rating": row.get("rating", ""),
                    "color_label": row.get("color_label", ""),
                    "keywords": row.get("keywords", ""),
                    "caption": row.get("caption", ""),
                }
            )
            count += 1
            if args.write_files:
                planned.parent.mkdir(parents=True, exist_ok=True)
                planned.write_text(xmp_text(row), encoding="utf-8")

    print(f"Wrote {plan_csv}")
    if args.write_files:
        print(f"Wrote supplemental XMP files under {xmp_root}")
    print(f"Planned entries: {count}")


if __name__ == "__main__":
    main()
