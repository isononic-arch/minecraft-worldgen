#!/usr/bin/env python3
"""
Vandir Vegetation Rename Tool
==============================
Convention:
  Trees:  {biome}_{type}_{species}_{variant}_{size}.schem
  Bushes: bush_generic_{variant}_{size}.schem
  Dead:   {biome}_dead_{species}_{variant}_{size}.schem

Usage:
  py rename_vegetation.py               # Preview → rename_plan.csv
  py rename_vegetation.py --apply       # Execute renames from rename_plan.csv
  py rename_vegetation.py --apply --csv my_plan.csv
"""

import os, re, csv, sys
from pathlib import Path
from collections import defaultdict

VEGETATION_DIR = r"C:\Users\nicho\minecraft-worldgen\Vegetation"

SKIP = {
    "dead trees", "templates", "savanna_oak_template.schem",
    "tree pack v1.6 by mr wizz.zip", "willowvalidate.schem",
}

# species_fragment → (biome_code, species_short, size_hint)
# Sorted longest-first for greedy matching
SPECIES = [
    # --- Temperate Rainforest ---
    ("western_red_cedar",       "train", "cedar",    None),
    ("bigleaf_maple",           "train", "maple",    None),
    ("douglas_fir",             "train", "dfir",     None),
    ("sitka_spruce",            "train", "sitka",    None),
    ("hemlock",                 "train", "hemlock",  None),
    # --- Snowy Boreal Taiga ---
    ("krummholz_spruce",        "sbtaiga","kspruce", "sm"),
    ("krummholz_fir",           "sbtaiga","kfir",    "sm"),
    ("subalpine_fir",           "sbtaiga","salfir",  None),
    ("snowy_spruce",            "sbtaiga","spruce",  None),
    # --- Boreal Taiga ---
    ("black_spruce",            "btaiga", "bspruce", None),
    ("white_spruce",            "btaiga", "wspruce", None),
    ("balsam_fir",              "btaiga", "balfir",  None),
    ("jack_pine",               "btaiga", "jpine",   None),
    ("tamarack",                "btaiga", "tamarack",None),
    # --- Lush Rainforest Coast ---
    ("coastal_rainforest_palm", "lrfc",  "rfpalm",  None),
    ("coastal_hardwood",        "lrfc",  "hardwood",None),
    ("mangrove_palm",           "lrfc",  "mpalm",   None),
    ("breadfruit_tree",         "lrfc",  "breadfruit",None),
    ("giant_fig",               "lrfc",  "gfig",    "lg"),
    ("banyan",                  "lrfc",  "banyan",  "lg"),
    # --- Rainforest Coast ---
    ("coastal_palm",            "rfc",   "cpalm",   None),
    ("tropical_fig",            "rfc",   "tfig",    None),
    ("kapok",                   "rfc",   "kapok",   "lg"),
    ("teak",                    "rfc",   "teak",    None),
    # --- Riparian ---
    ("weeping_willow",          "ripar", "wwillow", None),
    ("white_willow",            "ripar", "willow",  None),
    ("crack_willow",            "ripar", "cwillow", None),
    ("black_poplar",            "ripar", "poplar",  None),
    ("alder",                   "ripar", "alder",   None),
    # --- Mangrove Coast ---
    ("red_mangrove_small",      "mangr", "rmangsm", "sm"),
    ("red_mangrove",            "mangr", "rmang",   None),
    ("black_mangrove",          "mangr", "bmang",   None),
    ("white_mangrove",          "mangr", "wmang",   None),
    ("mangrove_root",           "mangr", "mroot",   None),
    ("mangrove_roots",          "mangr", "mroot",   None),
    ("mangrove_small",          "mangr", "mangsm",  "sm"),
    # --- Tidal Jungle Fringe ---
    ("coastal_fig",             "tjf",   "cfig",    None),
    # --- Temperate Deciduous ---
    ("english_oak",             "tdec",  "eoak",    None),
    ("cherry_tree",             "tdec",  "cherry",  None),
    ("lime_tree",               "tdec",  "lime",    None),
    ("hornbeam",                "tdec",  "hornbeam",None),
    ("beech",                   "tdec",  "beech",   None),
    ("ash",                     "tdec",  "ash",     None),
    # --- Birch Forest ---
    ("silver_birch",            "birch", "sbirch",  None),
    ("downy_birch",             "birch", "dbirch",  None),
    ("downy_burch",             "birch", "dbirch",  None),   # typo
    ("rowan",                   "birch", "rowan",   "sm"),
    # --- Dry Oak Savanna ---
    ("savanna_oak",             "dosav", "soak",    None),
    # --- Dry Pine Barrens ---
    ("ponderosa_pine",          "dpine", "ppine",   None),
    ("scots_pine",              "dpine", "scotsp",  None),
    ("pitch_pine",              "dpine", "pitchp",  None),
    # --- Semi-Arid Shrubland ---
    ("lone_juniper",            "sarid", "juniper", None),
    ("pinon_pine",              "sarid", "pinon",   None),
    # --- Dry Woodland Maquis ---
    ("aleppo_pine",             "maquis","apine",   None),
    ("holm_oak",                "maquis","hoak",    None),
    ("olive_tree",              "maquis","olive",   None),
    ("carob_tree",              "maquis","carob",   None),
    # --- Desert Steppe Transition ---
    ("lone_desert_acacia",      "dstep", "acacia",  None),
    # --- Continental Steppe ---
    ("lone_steppe_pine",        "cstep", "steppeP", None),
]

DEAD_SPECIES = {
    "dead_oak":    ("tdec",  "oak"),
    "dead_pine":   ("dpine", "pine"),
    "willow_dead": ("ripar", "willow"),
}

def normalise(s):
    s = s.lower()
    s = re.sub(r'[\s\-]+', '_', s)
    s = re.sub(r'[()]', '', s)
    s = re.sub(r'_+', '_', s)
    return s.strip('_')

def match_species(norm):
    for frag, biome, short, size_hint in SPECIES:
        if norm == frag or norm.startswith(frag + '_') or norm.startswith(frag):
            remainder = norm[len(frag):].lstrip('_')
            return biome, short, size_hint, remainder
    return None

def extract_num(s):
    m = re.search(r'\d+', s)
    return int(m.group()) if m else None

def parse_file(fname):
    p = Path(fname)
    if p.suffix.lower() not in ('.schem', '.schematic'):
        return None
    if fname.lower() in SKIP or p.stem.lower() in SKIP:
        return None
    norm = normalise(p.stem)

    # Dead trees
    for key, (biome, sp) in DEAD_SPECIES.items():
        if norm.startswith(key):
            remainder = norm[len(key):].lstrip('_')
            num = extract_num(remainder) or 1
            return dict(original=fname, biome=biome, type="dead",
                        species=sp, num=num, size_hint=None, notes="")

    # Bushes
    if norm.startswith('bushes'):
        remainder = norm[6:].lstrip('_')
        num = extract_num(remainder) or 1
        return dict(original=fname, biome="generic", type="bush",
                    species="generic", num=num, size_hint=None, notes="")

    # Trees
    m = match_species(norm)
    if m:
        biome, short, size_hint, remainder = m
        num = extract_num(remainder) or 1
        return dict(original=fname, biome=biome, type="tree",
                    species=short, num=num, size_hint=size_hint, notes="")

    return dict(original=fname, biome="UNKNOWN", type="UNKNOWN",
                species=norm, num=1, size_hint=None,
                notes="Needs manual review")

def assign_variants(records):
    """
    Per (biome, type, species) group: sort by num, assign sequential
    variant letters a,b,c... and size sm/md/lg.
    """
    groups = defaultdict(list)
    for r in records:
        groups[(r['biome'], r['type'], r['species'])].append(r)

    for key, grp in groups.items():
        grp.sort(key=lambda r: r['num'])
        n = len(grp)
        for i, r in enumerate(grp):
            r['variant'] = chr(ord('a') + i)
            if r['size_hint']:
                r['size'] = r['size_hint']
            elif r['type'] in ('bush',):
                # spread bushes sm/md/lg
                if i < n//3: r['size'] = 'sm'
                elif i < 2*n//3: r['size'] = 'md'
                else: r['size'] = 'lg'
            else:
                # spread trees across sm(40%) md(40%) lg(20%)
                frac = i / max(n-1, 1)
                if frac < 0.4: r['size'] = 'sm'
                elif frac < 0.8: r['size'] = 'md'
                else: r['size'] = 'lg'

def build_new_name(r):
    if r['biome'] == 'UNKNOWN':
        return 'REVIEW_' + Path(r['original']).stem + '.schem'
    if r['type'] == 'bush':
        return f"bush_{r['species']}_{r['variant']}_{r['size']}.schem"
    return f"{r['biome']}_{r['type']}_{r['species']}_{r['variant']}_{r['size']}.schem"

def write_csv(records, path):
    fields = ['original','new_name','biome','type','species','variant','size','notes']
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in records:
            w.writerow({k: r.get(k,'') for k in fields})
    print(f"Wrote {len(records)} rows → {path}")

def apply_renames(csv_path, dry_run=False):
    veg_dir = Path(VEGETATION_DIR)
    renamed = skipped = errors = 0
    with open(csv_path, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            orig = row['original'].strip()
            new  = row['new_name'].strip()
            if not orig or not new or new.startswith('REVIEW_'):
                skipped += 1; continue
            src = veg_dir / orig
            dst = veg_dir / new
            if not src.exists():
                print(f"  MISSING: {orig}"); errors += 1; continue
            if src == dst:
                skipped += 1; continue
            if dst.exists():
                print(f"  CONFLICT: {new} already exists — skipping {orig}")
                errors += 1; continue
            if dry_run:
                print(f"  DRY: {orig} → {new}")
            else:
                src.rename(dst)
                print(f"  ✓ {orig} → {new}")
            renamed += 1
    print(f"\nDone: {renamed} renamed, {skipped} skipped, {errors} errors")

def main():
    apply   = '--apply' in sys.argv
    dry_run = '--dry'   in sys.argv
    csv_arg = sys.argv[sys.argv.index('--csv')+1] if '--csv' in sys.argv else None

    if apply:
        csv_path = csv_arg or 'rename_plan.csv'
        print(f"Applying renames from {csv_path}…")
        apply_renames(csv_path, dry_run=dry_run)
        return

    # Build file list
    veg_dir = Path(VEGETATION_DIR)
    if veg_dir.exists():
        files = sorted(f.name for f in veg_dir.iterdir()
                       if f.suffix.lower() in ('.schem','.schematic'))
    else:
        fl = Path('filelist.txt')
        if not fl.exists():
            print("ERROR: directory not found and no filelist.txt"); sys.exit(1)
        files = [l.strip() for l in fl.read_text(encoding='utf-8').splitlines() if l.strip()]

    records = [r for f in files if (r := parse_file(f))]
    assign_variants(records)
    for r in records:
        r['new_name'] = build_new_name(r)

    records.sort(key=lambda r: (r['biome'], r['type'], r['species'], r['variant']))
    csv_path = csv_arg or 'rename_plan.csv'
    write_csv(records, csv_path)

    from collections import Counter
    tc = Counter(r['type'] for r in records)
    bc = Counter(r['biome'] for r in records)
    rv = sum(1 for r in records if r['biome']=='UNKNOWN')
    print(f"\nSummary: {len(records)} files | {dict(tc)} | {rv} need review")
    print("\nBy biome:")
    for b,c in sorted(bc.items()): print(f"  {b:12s} {c}")
    print(f"\nReview rename_plan.csv, edit any REVIEW_ rows, then:\n  py rename_vegetation.py --apply")

if __name__ == '__main__':
    main()
