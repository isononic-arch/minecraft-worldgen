#!/usr/bin/env python3
"""
merge_anchor_index.py — Vandir Pipeline Step 1d
================================================
Merges anchor_offsets.json into schematic_index.json, adding
anchor_y and inset_depth to every entry.

Usage:
    py merge_anchor_index.py
    py merge_anchor_index.py --index schematic_index.json --anchors anchor_offsets.json

After this step, each schematic_index.json entry looks like:
    {
      "path": "C:/Users/nicho/Vegetation/train_tree_sitka_a_sm.schematic",
      "type": "tree",
      "species": "sitka",
      "variant": "a",
      "size": "sm",
      "weight": 3,
      "anchor_y": 3,       ← Y coord of ground anchor in schematic space
      "inset_depth": 2,    ← sink this many blocks below terrain surface
      "anchor_review": false
    }

The placement engine in Step 8 uses:
    place_y = terrain_surface_y - anchor_y + inset_depth
"""

import argparse
import json
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Vandir Step 1d — Merge anchor offsets into schematic index"
    )
    parser.add_argument("--index",   default="schematic_index.json")
    parser.add_argument("--anchors", default="anchor_offsets.json")
    parser.add_argument("--out",     default="schematic_index.json",
                        help="Output path (default: overwrites index in-place)")
    args = parser.parse_args()

    index_path   = Path(args.index)
    anchors_path = Path(args.anchors)

    if not index_path.exists():
        print(f"ERROR: {index_path} not found — run validate_schematics.py --write-index first")
        sys.exit(1)
    if not anchors_path.exists():
        print(f"ERROR: {anchors_path} not found — run scan_schematics.py first")
        sys.exit(1)

    with open(index_path,   encoding="utf-8") as f: index   = json.load(f)
    with open(anchors_path, encoding="utf-8") as f: anchors = json.load(f)

    matched = unmatched = review_remaining = 0

    for biome, entries in index.items():
        for entry in entries:
            fname = Path(entry["path"]).name
            anchor_data = anchors.get(fname)
            if anchor_data:
                entry["anchor_y"]      = anchor_data["anchor_y"]
                entry["inset_depth"]   = anchor_data["inset_depth"]
                entry["lowest_leaf_y"] = anchor_data.get("lowest_leaf_y", 0)
                entry["anchor_review"] = anchor_data["review"]
                matched += 1
                if anchor_data["review"]:
                    review_remaining += 1
            else:
                # No anchor data — default to flush placement, flag for review
                entry["anchor_y"]      = 0
                entry["inset_depth"]   = 0
                entry["anchor_review"] = True
                unmatched += 1

    out_path = Path(args.out)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2)

    print(f"Merged anchor data into {out_path}")
    print(f"  Matched   : {matched}")
    print(f"  Unmatched : {unmatched}  (defaulted to anchor_y=0, flagged review)")
    print(f"  Still need review: {review_remaining}")

    if review_remaining or unmatched:
        print(f"\n  ⚠  Check anchor_offsets.csv for flagged entries before Step 8.")
    else:
        print(f"\n  All anchors resolved — schematic_index.json ready for Step 3.")


if __name__ == "__main__":
    main()
