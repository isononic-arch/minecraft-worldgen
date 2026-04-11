#!/usr/bin/env python3
"""
scan_schematics.py — Vandir Pipeline Step 1c
=============================================
Scans all vegetation schematics to determine the ground anchor Y offset
for each file. The anchor is the Y coordinate that should sit flush with
the terrain surface when the schematic is placed.

Algorithm:
  1. Parse schematic NBT, extract all non-air blocks with their Y coords.
  2. Identify MARKER blocks (wool, concrete, glass, terracotta) — these are
     build-guide blocks placed by schematic authors to mark underground
     portions (e.g. gray wool trunk extensions below grade).
  3. Identify TRUNK blocks (logs, stripped logs, mangrove roots, bamboo).
  4. Identify ROOT blocks (dirt, rooted_dirt, coarse_dirt in a tree context).
  5. Ground anchor Y = lowest TRUNK block that either:
       a) sits directly above a MARKER block  →  anchor = that trunk Y
       b) sits directly above air/void/non-solid  →  anchor = that trunk Y
       c) fallback: lowest trunk Y if none of the above match
  6. Inset depth = number of MARKER block rows below anchor Y (how deep to sink).
  7. Flag REVIEW if:
       - No trunk blocks found (might be bush/dead tree with no log)
       - Anchor is ambiguous (multiple candidate Ys)
       - Markers found but no clear trunk above them

Outputs:
  - anchor_offsets.csv     — one row per schematic, reviewable + editable
  - anchor_offsets.json    — consumed by schematic_index merger (Step 1d)

Usage:
    py scan_schematics.py --schem-dir "C:/Users/nicho/Vegetation"
    py scan_schematics.py --schem-dir "C:/Users/nicho/Vegetation" --verbose

Dependencies:
    pip install nbtlib
"""

import argparse
import csv
import gzip
import io
import json
import struct
import sys
from collections import defaultdict
from pathlib import Path


# ── Block classification sets ─────────────────────────────────────────────────

# Blocks used as underground/build-guide markers by schematic authors
MARKER_BLOCKS = {
    # Wool (all colours — gray most common but others used too)
    "white_wool", "orange_wool", "magenta_wool", "light_blue_wool",
    "yellow_wool", "lime_wool", "pink_wool", "gray_wool",
    "light_gray_wool", "cyan_wool", "purple_wool", "blue_wool",
    "brown_wool", "green_wool", "red_wool", "black_wool",
    # Concrete (all colours)
    "white_concrete", "orange_concrete", "magenta_concrete",
    "light_blue_concrete", "yellow_concrete", "lime_concrete",
    "pink_concrete", "gray_concrete", "light_gray_concrete",
    "cyan_concrete", "purple_concrete", "blue_concrete",
    "brown_concrete", "green_concrete", "red_concrete", "black_concrete",
    # Glass (sometimes used as markers)
    "glass", "white_stained_glass", "orange_stained_glass",
    "magenta_stained_glass", "light_blue_stained_glass",
    "yellow_stained_glass", "lime_stained_glass", "pink_stained_glass",
    "gray_stained_glass", "light_gray_stained_glass", "cyan_stained_glass",
    "purple_stained_glass", "blue_stained_glass", "brown_stained_glass",
    "green_stained_glass", "red_stained_glass", "black_stained_glass",
    # Terracotta (sometimes used)
    "terracotta", "white_terracotta", "gray_terracotta",
    "brown_terracotta", "red_terracotta",
    # Sponge / structure void (explicit build tools)
    "sponge", "structure_void", "barrier",
}

# Log/trunk block fragments — matched by substring
TRUNK_FRAGMENTS = {
    "_log", "_wood", "stripped_",
    "mangrove_roots", "muddy_mangrove_roots",
    "bamboo_block", "bamboo_stalk",
    "crimson_stem", "warped_stem",
    "mushroom_stem",
}

# Root/base fragments — blocks that appear at tree bases
ROOT_FRAGMENTS = {
    "rooted_dirt",
}

# Solid non-marker, non-trunk blocks that could appear at tree base
BASE_FRAGMENTS = {
    "dirt", "coarse_dirt", "podzol", "mud", "rooted_dirt",
    "moss_block", "mycelium",
}

# Leaf blocks — used for clearance check at placement time
LEAF_FRAGMENTS = {
    "_leaves", "nether_wart_block", "warped_wart_block",
}
LEGACY_LEAF_IDS = {"legacy_18", "legacy_161"}  # pre-1.13 leaves

def is_leaf(block_id: str) -> bool:
    b = block_id.split(":")[-1]
    if b in LEGACY_LEAF_IDS: return True
    return any(f in b for f in LEAF_FRAGMENTS)


# Definitely NOT trunks even if they contain log-like strings
TRUNK_EXCLUSIONS = {
    "log_fire", "soul_fire",
}


def is_trunk(block_id: str) -> bool:
    b = block_id.split(":")[-1]  # strip namespace
    if b in TRUNK_EXCLUSIONS:
        return False
    return any(f in b for f in TRUNK_FRAGMENTS)


def is_marker(block_id: str) -> bool:
    b = block_id.split(":")[-1]
    # Exact match (modern named blocks)
    if b in MARKER_BLOCKS:
        return True
    # Legacy classic parser maps all wool to bare "wool", glass to "stained_glass" etc.
    if b in ("wool", "stained_glass", "glass", "terracotta", "sponge", "hay_block"):
        return True
    return False


def is_root_base(block_id: str) -> bool:
    b = block_id.split(":")[-1]
    return any(f in b for f in ROOT_FRAGMENTS | BASE_FRAGMENTS)


# ── Minimal self-contained NBT parser ────────────────────────────────────────
# (no nbtlib dependency — pure stdlib gzip + struct)

class _NBTReader:
    TAG_END = 0; TAG_BYTE = 1; TAG_SHORT = 2; TAG_INT = 3; TAG_LONG = 4
    TAG_FLOAT = 5; TAG_DOUBLE = 6; TAG_BYTE_ARRAY = 7; TAG_STRING = 8
    TAG_LIST = 9; TAG_COMPOUND = 10; TAG_INT_ARRAY = 11; TAG_LONG_ARRAY = 12

    def __init__(self, data: bytes):
        self.buf = data
        self.pos = 0

    def read(self, n: int) -> bytes:
        out = self.buf[self.pos:self.pos+n]
        self.pos += n
        return out

    def read_tag(self, tag_type: int):
        if tag_type == self.TAG_END:       return None
        if tag_type == self.TAG_BYTE:      return struct.unpack(">b", self.read(1))[0]
        if tag_type == self.TAG_SHORT:     return struct.unpack(">h", self.read(2))[0]
        if tag_type == self.TAG_INT:       return struct.unpack(">i", self.read(4))[0]
        if tag_type == self.TAG_LONG:      return struct.unpack(">q", self.read(8))[0]
        if tag_type == self.TAG_FLOAT:     return struct.unpack(">f", self.read(4))[0]
        if tag_type == self.TAG_DOUBLE:    return struct.unpack(">d", self.read(8))[0]
        if tag_type == self.TAG_STRING:
            length = struct.unpack(">H", self.read(2))[0]
            return self.read(length).decode("utf-8", errors="replace")
        if tag_type == self.TAG_BYTE_ARRAY:
            length = struct.unpack(">i", self.read(4))[0]
            return list(struct.unpack(f">{length}b", self.read(length)))
        if tag_type == self.TAG_INT_ARRAY:
            length = struct.unpack(">i", self.read(4))[0]
            return list(struct.unpack(f">{length}i", self.read(length * 4)))
        if tag_type == self.TAG_LONG_ARRAY:
            length = struct.unpack(">i", self.read(4))[0]
            return list(struct.unpack(f">{length}q", self.read(length * 8)))
        if tag_type == self.TAG_LIST:
            elem_type = struct.unpack(">b", self.read(1))[0]
            length    = struct.unpack(">i", self.read(4))[0]
            return [self.read_tag(elem_type) for _ in range(length)]
        if tag_type == self.TAG_COMPOUND:
            result = {}
            while True:
                child_type = struct.unpack(">b", self.read(1))[0]
                if child_type == self.TAG_END:
                    break
                name_len = struct.unpack(">H", self.read(2))[0]
                name = self.read(name_len).decode("utf-8", errors="replace")
                result[name] = self.read_tag(child_type)
            return result
        raise ValueError(f"Unknown tag type: {tag_type}")

    def read_root(self) -> tuple[str, dict]:
        tag_type = struct.unpack(">b", self.read(1))[0]
        name_len = struct.unpack(">H", self.read(2))[0]
        name = self.read(name_len).decode("utf-8", errors="replace")
        return name, self.read_tag(tag_type)


def _load_nbt(path: Path) -> tuple[str, dict] | None:
    """Load NBT from gzip, zlib, or raw. Returns (root_name, root_dict) or None."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None

    # Try gzip
    if raw[:2] == b'\x1f\x8b':
        try:
            raw = gzip.decompress(raw)
        except Exception:
            return None
    # Try zlib
    elif raw[0] == 0x78:
        try:
            import zlib
            raw = zlib.decompress(raw)
        except Exception:
            pass  # might be raw NBT starting with 0x78 (unlikely)

    try:
        reader = _NBTReader(raw)
        return reader.read_root()
    except Exception:
        return None


# ── Varint decoder (Sponge .schem BlockData) ─────────────────────────────────

def _decode_varints(data: list) -> list:
    result, i = [], 0
    raw = bytes([b & 0xFF for b in data])
    while i < len(raw):
        value, shift = 0, 0
        while True:
            byte = raw[i]; i += 1
            value |= (byte & 0x7F) << shift
            if not (byte & 0x80): break
            shift += 7
        result.append(value)
    return result


# ── Schematic block extractors ────────────────────────────────────────────────

def _extract_blocks_schem(root: dict) -> list[tuple[int,int,int,str]] | None:
    """Extract (x, y, z, block_id) from Sponge v2 .schem format."""
    try:
        schem = root.get("Schematic", root)
        w = int(schem["Width"]); h = int(schem["Height"]); l = int(schem["Length"])
        palette = {int(v): str(k) for k, v in schem["Palette"].items()}
        indices = _decode_varints(schem["BlockData"])
        blocks = []
        for i, idx in enumerate(indices):
            if i >= w * h * l: break
            y, zz, x = i // (w * l), (i % (w * l)) // w, i % w
            block_id = palette.get(idx, "minecraft:air").split("[")[0]
            if "air" not in block_id:
                blocks.append((x, y, zz, block_id))
        return blocks
    except Exception:
        return None


def _extract_blocks_classic(root: dict) -> list[tuple[int,int,int,str]] | None:
    """Extract blocks from MCEdit .schematic format."""
    # Legacy ID → block string map (trunk + marker detection only)
    WOOD_IDS = {
        # ID 17: oak/spruce/birch/jungle logs (data 0-3, treat all as log)
        17: "oak_log",
        # ID 162: acacia/dark_oak logs
        162: "acacia_log",
        # No stripped logs pre-1.13
    }
    MARKER_IDS = {
        35:  "wool",        # all 16 wool colours → marker
        95:  "stained_glass",
        159: "terracotta",
        19:  "sponge",
        20:  "glass",
        170: "hay_block",   # sometimes used as filler
    }
    ROOT_IDS = {
        2: "dirt",   # grass block
        3: "dirt",
        60: "dirt",  # farmland
        110: "dirt", # mycelium
    }
    try:
        schem = root.get("Schematic", root)
        # Check for Sponge format disguised as .schematic
        if "Palette" in schem and "BlockData" in schem:
            return _extract_blocks_schem(root)
        w = int(schem["Width"]); h = int(schem["Height"]); l = int(schem["Length"])
        block_ids   = schem.get("Blocks", [])
        block_data  = schem.get("Data", [])
        blocks = []
        for i, bid in enumerate(block_ids):
            bid = bid & 0xFF
            if bid == 0: continue  # air
            y, zz, x = i // (w * l), (i % (w * l)) // w, i % w
            if bid in WOOD_IDS:
                block_id = WOOD_IDS[bid]
            elif bid in MARKER_IDS:
                block_id = MARKER_IDS[bid]
            elif bid in ROOT_IDS:
                block_id = ROOT_IDS[bid]
            else:
                block_id = f"legacy_{bid}"
            blocks.append((x, y, zz, block_id))
        return blocks
    except Exception:
        return None


def extract_blocks(path: Path) -> tuple[list | None, str]:
    """
    Load schematic and return (blocks, format_name).
    blocks = list of (x, y, z, block_id) or None on failure.
    """
    result = _load_nbt(path)
    if result is None:
        return None, "nbt_load_failed"

    _, root = result
    ext = path.suffix.lower()

    if ext == ".schem":
        blocks = _extract_blocks_schem(root)
        fmt = "sponge_v2"
    elif ext == ".schematic":
        blocks = _extract_blocks_classic(root)
        fmt = "classic"
    else:
        blocks = _extract_blocks_schem(root) or _extract_blocks_classic(root)
        fmt = "unknown"

    return blocks, fmt


# ── Anchor detection ──────────────────────────────────────────────────────────

def detect_anchor(blocks: list[tuple[int,int,int,str]]) -> dict:
    """
    Analyse block list and return anchor info dict:
      anchor_y     : int   — Y coord of ground surface anchor
      inset_depth  : int   — blocks to sink below surface (marker depth)
      method       : str   — how anchor was determined
      notes        : str   — human-readable explanation
      review       : bool  — needs manual check
    """
    if not blocks:
        return {"anchor_y": 0, "lowest_leaf_y": lowest_leaf_y,
            "inset_depth": 0, "method": "empty",
                "notes": "no blocks", "review": True}

    # Index blocks by Y
    by_y: dict[int, list[str]] = defaultdict(list)
    for x, y, z, bid in blocks:
        by_y[y].append(bid)

    all_ys = sorted(by_y.keys())
    min_y  = all_ys[0]
    max_y  = all_ys[-1]

    # Classify each Y layer
    trunk_ys  = sorted(y for y, ids in by_y.items() if any(is_trunk(b) for b in ids))
    marker_ys = sorted(y for y, ids in by_y.items() if any(is_marker(b) for b in ids))

    # Precompute lowest leaf Y for clearance checks at placement time
    leaf_ys = [y for y, ids in by_y.items() if any(is_leaf(b) for b in ids)]
    lowest_leaf_y = min(leaf_ys) if leaf_ys else max_y

    # ── Case 1: No trunk blocks (bush, dead tree, grass clump) ───────────────
    if not trunk_ys:
        # Anchor = lowest solid block
        return {
            "anchor_y":    min_y,
            "inset_depth": 0,
            "lowest_leaf_y": lowest_leaf_y,
            "method":      "no_trunk_lowest_solid",
            "notes":       "no log blocks found — likely bush/dead. anchor=lowest block.",
            "review":      False,
        }

    lowest_trunk = trunk_ys[0]

    # ── Case 2: Marker blocks below lowest trunk ──────────────────────────────
    markers_below = [y for y in marker_ys if y < lowest_trunk]
    if markers_below:
        inset = lowest_trunk - min(markers_below)
        return {
            "anchor_y":    lowest_trunk,
            "lowest_leaf_y": lowest_leaf_y,
            "inset_depth": inset,
            "method":      "marker_below_trunk",
            "notes":       f"marker blocks at Y{min(markers_below)}–Y{lowest_trunk-1}, trunk starts Y{lowest_trunk}. sink {inset} blocks.",
            "review":      False,
        }

    # ── Case 3: Marker blocks at same Y as trunk ─────────────────────────────
    markers_at_trunk = [y for y in marker_ys if y == lowest_trunk]
    if markers_at_trunk:
        # Mixed layer — ambiguous, flag for review
        return {
            "anchor_y":    lowest_trunk,
            "lowest_leaf_y": lowest_leaf_y,
            "inset_depth": 0,
            "method":      "marker_mixed_with_trunk",
            "notes":       f"markers and trunk both at Y{lowest_trunk} — ambiguous layer.",
            "review":      True,
        }

    # ── Case 4: Trunk starts above min_y with non-trunk, non-marker below ────
    non_trunk_below = [y for y in all_ys if y < lowest_trunk and y not in marker_ys]
    if non_trunk_below:
        # Could be root blocks (dirt, rooted_dirt) or decoration below trunk
        root_ys = [y for y in non_trunk_below
                   if any(is_root_base(b) for b in by_y[y])]
        if root_ys:
            inset = lowest_trunk - min(root_ys)
            return {
                "anchor_y":    lowest_trunk,
                "lowest_leaf_y": lowest_leaf_y,
            "inset_depth": inset,
                "method":      "root_blocks_below_trunk",
                "notes":       f"root/dirt blocks at Y{min(root_ys)}–Y{lowest_trunk-1}. sink {inset} blocks.",
                "review":      False,
            }
        # Unknown non-trunk blocks below — flag
        unknown_ids = set()
        for y in non_trunk_below[:3]:
            unknown_ids.update(b.split(":")[-1] for b in by_y[y][:3])
        return {
            "anchor_y":    lowest_trunk,
            "lowest_leaf_y": lowest_leaf_y,
            "inset_depth": 0,
            "method":      "unknown_blocks_below_trunk",
            "notes":       f"non-trunk blocks below trunk at Y{non_trunk_below}: {unknown_ids}",
            "review":      True,
        }

    # ── Case 5: Trunk starts at absolute bottom ───────────────────────────────
    return {
        "anchor_y":    lowest_trunk,
        "lowest_leaf_y": lowest_leaf_y,
            "inset_depth": 0,
        "method":      "trunk_at_bottom",
        "notes":       f"trunk starts at Y{lowest_trunk} with nothing below — place flush.",
        "review":      False,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Vandir Step 1c — Scan schematics for ground anchor offsets"
    )
    parser.add_argument(
        "--schem-dir", default=r"C:\Users\nicho\minecraft-worldgen\Vegetation",
        help="Schematic directory"
    )
    parser.add_argument(
        "--csv-out", default="anchor_offsets.csv",
        help="Output CSV path (default: anchor_offsets.csv)"
    )
    parser.add_argument(
        "--json-out", default="anchor_offsets.json",
        help="Output JSON path (default: anchor_offsets.json)"
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print per-file results to console"
    )
    args = parser.parse_args()

    schem_dir = Path(args.schem_dir)
    if not schem_dir.exists():
        print(f"ERROR: directory not found: {schem_dir}")
        sys.exit(1)

    VALID_EXTS = {".schem", ".schematic"}
    files = sorted(f for f in schem_dir.iterdir()
                   if f.is_file() and f.suffix.lower() in VALID_EXTS)

    print("=" * 60)
    print("  Vandir Step 1c — scan_schematics.py")
    print("=" * 60)
    print(f"\n  Scanning {len(files)} files in {schem_dir}\n")

    results = []
    review_count = 0
    method_counts = defaultdict(int)

    for i, f in enumerate(files, 1):
        blocks, fmt = extract_blocks(f)

        if blocks is None:
            info = {
                "anchor_y": 0, "lowest_leaf_y": lowest_leaf_y,
            "inset_depth": 0,
                "method": "parse_failed", "notes": f"NBT parse failed ({fmt})",
                "review": True,
            }
        else:
            info = detect_anchor(blocks)
            info["block_count"] = len(blocks)

        info["file"]   = f.name
        info["format"] = fmt
        results.append(info)

        if info["review"]:
            review_count += 1
        method_counts[info["method"]] += 1

        if args.verbose or info["review"]:
            flag = " ⚠ REVIEW" if info["review"] else ""
            print(f"  [{i:3d}] {f.name:<55} anchor=Y{info['anchor_y']:3d}  inset={info['inset_depth']}{flag}")
            if info["review"] and not args.verbose:
                print(f"         → {info['notes']}")

    # ── Write CSV ─────────────────────────────────────────────────────────────
    csv_fields = ["file", "anchor_y", "inset_depth", "lowest_leaf_y", "method", "review", "notes", "format", "block_count"]
    with open(args.csv_out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=csv_fields, extrasaction="ignore")
        w.writeheader()
        # Sort: review items first for easy editing
        for r in sorted(results, key=lambda r: (not r["review"], r["file"])):
            w.writerow(r)
    print(f"\nWritten: {args.csv_out}")

    # ── Write JSON ────────────────────────────────────────────────────────────
    json_out = {r["file"]: {
        "anchor_y":      r["anchor_y"],
        "inset_depth":   r["inset_depth"],
        "lowest_leaf_y": r.get("lowest_leaf_y", 0),
        "method":        r["method"],
        "review":        r["review"],
    } for r in results}
    with open(args.json_out, "w", encoding="utf-8") as f:
        json.dump(json_out, f, indent=2)
    print(f"Written: {args.json_out}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  Total files   : {len(results)}")
    print(f"  Need review   : {review_count}")
    print(f"\n  Detection methods:")
    for method, count in sorted(method_counts.items(), key=lambda x: -x[1]):
        print(f"    {method:<35} {count}")

    if review_count:
        print(f"\n  ⚠  {review_count} files flagged — open anchor_offsets.csv,")
        print(f"     correct anchor_y / inset_depth, set review=False when done.")
    else:
        print(f"\n  All anchors resolved automatically ✓")

    print(f"\n  Next: merge anchor_offsets.json into schematic_index.json")
    print(f"  Run: py merge_anchor_index.py")
    print("=" * 60)


if __name__ == "__main__":
    main()
