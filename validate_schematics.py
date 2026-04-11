#!/usr/bin/env python3
"""
validate_schematics.py — Vandir Pipeline Step 1b
=================================================
Scans the vegetation schematic directory and validates that:
  1. All files match the naming convention:
       {biome_code}_{type}_{species}_{variant}_{size}.schem/.schematic
       bush_generic_{variant}_{size}.schem/.schematic
       {biome_code}_dead_{species}_{variant}_{size}.schem/.schematic
  2. Every biome that appears in the override map has at least one
     tree or bush schematic assigned to it (where vegetation is expected).
  3. No two files produce identical (biome, type, species, variant, size) keys.
  4. All .schem files are valid NBT (not corrupt/empty).

Outputs:
  - Console report with pass/warn/fail per check
  - schematic_index.json  — consumed by schematic_loader.py (Step 3)

Usage:
    py validate_schematics.py
    py validate_schematics.py --schem-dir "C:/Users/nicho/Vegetation"
    py validate_schematics.py --schem-dir "C:/Users/nicho/Vegetation" --write-index
"""

import argparse
import json
import re
import struct
import sys
import zlib
from collections import defaultdict
from pathlib import Path

# ── Locked constants from Project Bible V4 ───────────────────────────────────

SCHEM_DIR_DEFAULT = r"C:\Users\nicho\minecraft-worldgen\Vegetation"
INDEX_OUTPUT      = "schematic_index.json"

# NOTE FOR STEP 7 (Surface Decoration):
# A ground cover layer separate from schematic placement is planned.
# Uses individual blocks (tall_grass, fern, dead_bush, grass variants) with
# per-biome palettes + density. Handles fine-grained understorey that schematics
# can't — karst scrub, steppe grasses, heathland mat, rainforest floor ferns, etc.
# Biomes with special ground cover needs (non-exhaustive):
#   karst   — dead_bush, grass, short_grass mix on bare stone
#   cstep   — tall_grass, dead_bush (feather grass proxy)
#   heath   — short_grass, dead_bush
#   dpine   — dead_bush, fern on sandy floor
#   desert  — dead_bush only, very sparse
#   tundra  — NO ground cover (bare stone/snow)
#   fflats  — NO ground cover

# All valid biome codes (from override map + pipeline use)
VALID_BIOME_CODES = {
    "cheath", "train", "btaiga", "sbtaiga", "alpine", "tundra", "fflats",
    "tdec", "rfc", "ripar", "dosav", "karst", "birch", "etcoast", "mixed",
    "cstep", "dpine", "heath", "lrfc", "desert", "dstep", "sarid", "maquis",
    "tjf", "mangr", "fen", "generic",
}

# Biomes where we expect vegetation (exclude bare/water/ice zones)
BIOMES_EXPECTING_TREES = {
    "train", "btaiga", "sbtaiga", "tdec", "rfc", "ripar", "dosav",
    "birch", "mixed", "cstep", "dpine", "lrfc", "dstep", "sarid",
    "maquis", "tjf", "mangr", "fen",
}
BIOMES_EXPECTING_BUSHES = {
    # Full understorey
    "cheath", "train", "btaiga", "sbtaiga", "alpine", "tdec", "rfc",
    "ripar", "dosav", "birch", "etcoast", "mixed", "cstep", "dpine",
    "heath", "lrfc", "dstep", "sarid", "maquis", "tjf", "fen",
    # Sparse/marginal — generic bushes at low density in Step 8
    "karst",   # spiny spurge, rock cistus
    "mangr",   # mangrove seedlings, coastal succulents
    # Excluded (no vegetation):
    # "tundra"  — bare stone/ice/snow
    # "fflats"  — flat icy plain, nothing grows
    # "desert"  — sand dune, too arid
    "generic",
}

VALID_TYPES    = {"tree", "bush", "dead"}
VALID_SIZES    = {"sm", "md", "lg"}
VALID_VARIANTS = set("abcdefghijklmnopqrstuvwxyz")
VALID_EXTS     = {".schem", ".schematic"}

# Size → placement weight (used in schematic_index.json)
SIZE_WEIGHT = {"sm": 3, "md": 2, "lg": 1}

# Biomes that borrow trees from other biomes' species pools.
# Key = borrowing biome, value = list of (donor_biome, species) pairs to include.
# These create virtual index entries pointing at real schematic files.
# Based on vegetation reference sheet species lists.
BIOME_BORROW_TREES: dict[str, list[tuple[str, str]]] = {
    "mixed": [
        # Oak, Spruce, Birch, Pine, Lime tree (veg ref)
        ("tdec",  "eoak"),     # English oak → oak
        ("btaiga","wspruce"),  # White spruce
        ("birch", "sbirch"),   # Silver birch
        ("birch", "dbirch"),   # Downy birch
        ("dpine", "scotsp"),   # Scots pine → pine
        ("tdec",  "lime"),     # Lime tree
    ],
    "fen": [
        # Black alder, Crack willow, Swamp oak (veg ref)
        ("ripar", "alder"),    # Alder
        ("ripar", "cwillow"),  # Crack willow
        ("tdec",  "eoak"),     # Swamp oak — borrow english oak
    ],
}

# Biomes that use generic bushes (bush_generic_*) for understorey.
# All biomes expecting bushes but without per-biome schematics fall back here.
GENERIC_BUSH_BIOMES: set[str] = {
    "cheath", "train", "btaiga", "sbtaiga", "alpine", "tdec", "rfc",
    "ripar", "dosav", "birch", "etcoast", "mixed", "cstep", "dpine",
    "heath", "lrfc", "dstep", "sarid", "maquis", "tjf", "fen",
    "karst", "mangr",
    # tundra, fflats, desert excluded
}

# Regex for both name patterns:
#   bush_generic_{variant}_{size}
#   {biome}_{type}_{species}_{variant}_{size}
RE_BUSH    = re.compile(r'^bush_generic_([a-z])_(sm|md|lg)$')
RE_NORMAL  = re.compile(r'^([a-z]+)_(tree|bush|dead)_([a-z]+)_([a-z])_(sm|md|lg)$')


# ── NBT validity check ────────────────────────────────────────────────────────

def _is_valid_nbt(path: Path) -> tuple[bool, str]:
    """
    Minimal NBT validity check: file must be non-empty and either:
      - start with gzip magic (1f 8b) for .schematic/.schem
      - or be valid zlib-wrapped NBT
    Does not fully parse — just confirms the file is not corrupt/empty.
    """
    try:
        data = path.read_bytes()
    except OSError as e:
        return False, f"read error: {e}"

    if len(data) == 0:
        return False, "empty file"

    # gzip magic
    if data[:2] == b'\x1f\x8b':
        try:
            import gzip
            with gzip.open(path, 'rb') as f:
                header = f.read(3)
            if len(header) < 1:
                return False, "gzip decompresses to nothing"
            return True, "gzip NBT"
        except Exception as e:
            return False, f"gzip error: {e}"

    # zlib magic (78 9c, 78 da, 78 01)
    if data[0] == 0x78:
        try:
            decompressed = zlib.decompress(data)
            if len(decompressed) == 0:
                return False, "zlib decompresses to nothing"
            return True, "zlib NBT"
        except Exception as e:
            return False, f"zlib error: {e}"

    # Raw NBT — first byte is tag type (1-12), second/third are name length
    if 1 <= data[0] <= 12:
        return True, "raw NBT"

    return False, f"unrecognised format (first byte: 0x{data[0]:02x})"


# ── Filename parser ───────────────────────────────────────────────────────────

def parse_filename(stem: str) -> dict | None:
    """
    Parse a schematic stem into components.
    Returns dict or None if it doesn't match either pattern.
    """
    m = RE_BUSH.match(stem)
    if m:
        return {
            "biome":   "generic",
            "type":    "bush",
            "species": "generic",
            "variant": m.group(1),
            "size":    m.group(2),
        }

    m = RE_NORMAL.match(stem)
    if m:
        return {
            "biome":   m.group(1),
            "type":    m.group(2),
            "species": m.group(3),
            "variant": m.group(4),
            "size":    m.group(5),
        }

    return None


# ── Main validation ───────────────────────────────────────────────────────────

def validate(schem_dir: Path, check_nbt: bool, write_index: bool, index_path: Path) -> int:
    """
    Run all checks. Returns exit code (0=pass, 1=failures found).
    """
    print("=" * 60)
    print("  Vandir Step 1b — validate_schematics.py")
    print("=" * 60)
    print(f"\n  Scanning: {schem_dir}\n")

    if not schem_dir.exists():
        print(f"ERROR: directory not found: {schem_dir}")
        return 1

    # Collect all schematic files (skip subdirs, zips, etc.)
    all_files = sorted(
        f for f in schem_dir.iterdir()
        if f.is_file() and f.suffix.lower() in VALID_EXTS
    )
    print(f"  Found {len(all_files)} schematic files\n")

    failures  = []
    warnings  = []
    parsed    = []   # list of (path, components_dict)
    key_seen  = {}   # (biome, type, species, variant, size) → first path

    biome_trees  = defaultdict(list)
    biome_bushes = defaultdict(list)

    # ── Check 1: Naming convention + duplicates ───────────────────────────────
    print("[1/4] Checking naming convention...")
    unrecognised = []
    for f in all_files:
        stem = f.stem
        parts = parse_filename(stem)
        if parts is None:
            unrecognised.append(f.name)
            failures.append(f"BAD NAME: {f.name}")
            continue

        biome   = parts["biome"]
        vtype   = parts["type"]
        species = parts["species"]
        variant = parts["variant"]
        size    = parts["size"]

        # Validate individual fields
        if biome not in VALID_BIOME_CODES:
            failures.append(f"UNKNOWN BIOME '{biome}': {f.name}")
        if vtype not in VALID_TYPES:
            failures.append(f"UNKNOWN TYPE '{vtype}': {f.name}")
        if size not in VALID_SIZES:
            failures.append(f"UNKNOWN SIZE '{size}': {f.name}")
        if variant not in VALID_VARIANTS:
            failures.append(f"UNKNOWN VARIANT '{variant}': {f.name}")

        # Duplicate key check
        key = (biome, vtype, species, variant, size)
        if key in key_seen:
            failures.append(
                f"DUPLICATE KEY {key}: {f.name}  (first: {key_seen[key].name})"
            )
        else:
            key_seen[key] = f
            parsed.append((f, parts))
            if vtype in ("tree", "dead"):
                biome_trees[biome].append(f)
            elif vtype == "bush":
                biome_bushes[biome].append(f)

    if unrecognised:
        print(f"  {len(unrecognised)} files with unrecognised names:")
        for n in unrecognised[:10]:
            print(f"    {n}")
        if len(unrecognised) > 10:
            print(f"    ... and {len(unrecognised)-10} more")
    else:
        print(f"  All {len(all_files)} filenames valid ✓")

    # ── Check 2: Biome coverage ───────────────────────────────────────────────
    print("\n[2/4] Checking biome coverage...")

    # Biomes covered by borrowing rules are not missing
    borrowed_tree_biomes = set(BIOME_BORROW_TREES.keys())
    # Biomes covered by generic bush fallback are not missing
    bush_covered = set(biome_bushes.keys()) | GENERIC_BUSH_BIOMES | {"generic"}

    missing_trees  = sorted(
        (BIOMES_EXPECTING_TREES - set(biome_trees.keys())) - borrowed_tree_biomes
    )
    missing_bushes = sorted(BIOMES_EXPECTING_BUSHES - bush_covered)

    if BIOME_BORROW_TREES:
        print(f"  Biome borrowing active: {', '.join(sorted(BIOME_BORROW_TREES.keys()))}")
    print(f"  Generic bush fallback covers: {len(GENERIC_BUSH_BIOMES)} biomes")

    if missing_trees:
        for b in missing_trees:
            warnings.append(f"NO TREES for biome: {b}")
        print(f"  WARN — {len(missing_trees)} biomes have no trees and no borrow rule:")
        for b in missing_trees:
            print(f"    {b}")
    else:
        print(f"  All tree-expecting biomes covered ✓")

    if missing_bushes:
        for b in missing_bushes:
            warnings.append(f"NO BUSHES for biome: {b}")
        print(f"  WARN — {len(missing_bushes)} biomes have no bushes:")
        for b in missing_bushes:
            print(f"    {b}")
    else:
        print(f"  All bush-expecting biomes covered ✓")

    # ── Check 3: NBT validity ─────────────────────────────────────────────────
    print(f"\n[3/4] Checking NBT validity {'(enabled)' if check_nbt else '(skipped — use --check-nbt)'}...")
    nbt_failures = 0
    if check_nbt:
        for f, _ in parsed:
            ok, reason = _is_valid_nbt(f)
            if not ok:
                failures.append(f"CORRUPT NBT: {f.name} — {reason}")
                nbt_failures += 1
        if nbt_failures == 0:
            print(f"  All {len(parsed)} files passed NBT check ✓")
        else:
            print(f"  {nbt_failures} corrupt files found")
    else:
        print("  Skipped.")

    # ── Check 4: Size distribution ────────────────────────────────────────────
    print("\n[4/4] Size distribution summary...")
    size_counts = defaultdict(int)
    type_counts = defaultdict(int)
    for _, p in parsed:
        size_counts[p["size"]] += 1
        type_counts[p["type"]] += 1
    for t in sorted(type_counts):
        print(f"  {t:8s}: {type_counts[t]}")
    print(f"  sm/md/lg: {size_counts['sm']}/{size_counts['md']}/{size_counts['lg']}")

    # ── Build schematic_index.json ────────────────────────────────────────────
    if write_index:
        print(f"\nBuilding {index_path.name}...")
        index: dict[str, list] = defaultdict(list)

        # Build species lookup: (donor_biome, species) → list of entries
        species_lookup: dict[tuple, list] = defaultdict(list)
        for f, p in parsed:
            key = (p["biome"], p["species"])
            entry = {
                "path":    str(f),
                "type":    p["type"],
                "species": p["species"],
                "variant": p["variant"],
                "size":    p["size"],
                "weight":  SIZE_WEIGHT[p["size"]],
            }
            species_lookup[key].append(entry)

        # Add directly-owned schematics
        for f, p in parsed:
            biome  = p["biome"]
            weight = SIZE_WEIGHT[p["size"]]
            entry  = {
                "path":    str(f),
                "type":    p["type"],
                "species": p["species"],
                "variant": p["variant"],
                "size":    p["size"],
                "weight":  weight,
            }
            index[biome].append(entry)

        # Inject borrowed tree entries (marked with borrowed=true for schematic_loader)
        for borrower, donor_specs in BIOME_BORROW_TREES.items():
            for donor_biome, species in donor_specs:
                for entry in species_lookup.get((donor_biome, species), []):
                    borrowed_entry = dict(entry, borrowed=True, donor_biome=donor_biome)
                    index[borrower].append(borrowed_entry)
            if index[borrower]:
                print(f"  {borrower}: injected {sum(1 for e in index[borrower] if e.get('borrowed'))} borrowed tree entries")

        # Inject generic bush entries for all fallback biomes
        generic_bushes = [e for e in index.get("generic", []) if e["type"] == "bush"]
        if generic_bushes:
            for biome in GENERIC_BUSH_BIOMES:
                existing_bushes = [e for e in index[biome] if e["type"] == "bush"]
                if not existing_bushes:
                    for entry in generic_bushes:
                        index[biome].append(dict(entry, borrowed=True, donor_biome="generic"))
            print(f"  Generic bush fallback injected into {len(GENERIC_BUSH_BIOMES)} biomes ({len(generic_bushes)} variants each)")

        # Sort each biome's list for determinism
        for biome in index:
            index[biome].sort(key=lambda e: (e["type"], e["species"], e["variant"]))

        total_entries = sum(len(v) for v in index.values())
        with open(index_path, "w", encoding="utf-8") as fp:
            json.dump(dict(index), fp, indent=2)
        print(f"  Written: {index_path}  ({total_entries} entries across {len(index)} biomes)")

    # ── Final report ──────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print(f"  Failures : {len(failures)}")
    print(f"  Warnings : {len(warnings)}")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(f"  ✗ {f}")
    if warnings:
        print("\nWARNINGS:")
        for w in warnings:
            print(f"  ⚠ {w}")

    if not failures:
        print("\n  STEP 1b PASSED ✓")
        if warnings:
            print("  (warnings above are non-blocking — add schematics to fill gaps)")
        if write_index:
            print(f"  schematic_index.json ready for Step 3 (schematic_loader.py)")
    else:
        print("\n  STEP 1b FAILED — fix failures before proceeding")
    print("=" * 60)

    return 1 if failures else 0


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Vandir Step 1b — Validate vegetation schematics and build index"
    )
    parser.add_argument(
        "--schem-dir", default=SCHEM_DIR_DEFAULT,
        help=f"Schematic directory (default: {SCHEM_DIR_DEFAULT})"
    )
    parser.add_argument(
        "--write-index", action="store_true",
        help=f"Write schematic_index.json after validation"
    )
    parser.add_argument(
        "--index-path", default=INDEX_OUTPUT,
        help=f"Output path for schematic index (default: {INDEX_OUTPUT})"
    )
    parser.add_argument(
        "--check-nbt", action="store_true",
        help="Check every file for NBT validity (slower, but catches corrupt files)"
    )
    args = parser.parse_args()

    schem_dir  = Path(args.schem_dir)
    index_path = Path(args.index_path)

    exit_code = validate(schem_dir, args.check_nbt, args.write_index, index_path)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
