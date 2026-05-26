"""
apply_s88_strata.py — add `strata` sub-block to each of the 6 lithology groups.

Per-group strata config (final approved spec):
  palette                3 blocks, all from the group's existing palette set
  thickness_min/max      band thickness range in blocks (deterministic per
                         band via world-seeded RNG -> no repeating pattern)
  noise_amp_blocks       ± Y wobble for organic band edges
  tilt_per_100blocks     dip in blocks of Y per 100 horizontal blocks
  tilt_dir_deg           compass direction of tilt (0=E, 90=N)
  speckle_rate           probability per cell of swapping to palette[0]
  vein_block             contrast block for cross-band veins (must be in palette)
  vein_amp               per-cell probability of painting vein at vein-eligible
                         cells (multiplied by terrain/fault gating in chunk_writer)
"""
from __future__ import annotations
import json, sys
from pathlib import Path

CFG_PATH = Path("config/thresholds.json")

STRATA: dict[str, dict] = {
    "granitic": {
        "palette": ["granite", "dripstone_block", "raw_iron_block"],
        "thickness_min": 16,
        "thickness_max": 32,
        "noise_amp_blocks": 5,
        "tilt_per_100blocks": 0,
        "tilt_dir_deg": 0,
        "speckle_rate": 0.10,
        "vein_blocks": ["raw_iron_block", "soul_soil"],
        "vein_amp": 0.40,
        "_comment": "Massive intrusive — thick irregular bands, no tilt, high speckle, iron+soul-soil pegmatite veins."
    },
    "arid_basaltic": {
        "palette": ["basalt", "smooth_basalt", "tuff"],
        "thickness_min": 8,
        "thickness_max": 16,
        "noise_amp_blocks": 3,
        "tilt_per_100blocks": 2,
        "tilt_dir_deg": 45,
        "speckle_rate": 0.08,
        "vein_blocks": ["dead_fire_coral_block", "dead_bubble_coral_block"],
        "vein_amp": 0.20,
        "_comment": "Volcanic lava flows — medium bands, mild tilt NE, fire+bubble coral dike veins (vein-only palette exception)."
    },
    "temperate_basaltic": {
        "palette": ["deepslate", "cobbled_deepslate", "deepslate_coal_ore"],
        "thickness_min": 6,
        "thickness_max": 14,
        "noise_amp_blocks": 2,
        "tilt_per_100blocks": 5,
        "tilt_dir_deg": 90,
        "speckle_rate": 0.05,
        "vein_blocks": ["deepslate_coal_ore", "tuff"],
        "vein_amp": 0.50,
        "_comment": "Metamorphic foliation — thin sharp bands, gentle E tilt, coal+tuff seams in fault zones."
    },
    "limestone": {
        "palette": ["calcite", "diorite", "dead_horn_coral_block"],
        "thickness_min": 5,
        "thickness_max": 12,
        "noise_amp_blocks": 1,
        "tilt_per_100blocks": 1,
        "tilt_dir_deg": 0,
        "speckle_rate": 0.10,
        "vein_blocks": ["dead_horn_coral_block", "dead_fire_coral_block"],
        "vein_amp": 0.30,
        "_comment": "Sedimentary bedding — thin bands, near-flat, horn+fire coral seams along faults (fire is vein-only palette exception)."
    },
    "deepslate_metamorphic": {
        "palette": ["andesite", "stone", "tuff"],
        "thickness_min": 8,
        "thickness_max": 16,
        "noise_amp_blocks": 4,
        "tilt_per_100blocks": 8,
        "tilt_dir_deg": 30,
        "speckle_rate": 0.06,
        "vein_blocks": ["dead_fire_coral_block", "dead_bubble_coral_block"],
        "vein_amp": 0.60,
        "_comment": "Folded mountain metamorphic — medium bands, strong NNE tilt, prominent fire+bubble coral veins."
    },
    "mossy_temperate": {
        "palette": ["cobblestone", "mossy_cobblestone", "tuff"],
        "thickness_min": 10,
        "thickness_max": 18,
        "noise_amp_blocks": 3,
        "tilt_per_100blocks": 2,
        "tilt_dir_deg": 0,
        "speckle_rate": 0.12,
        "vein_blocks": ["moss_block"],
        "vein_amp": 0.30,
        "_comment": "Wet temperate — medium-thick bands, mild dip, high weathering speckle, moss-filled fractures."
    },
}

# Top-level strata config (vein-detection knobs shared by all groups)
STRATA_GLOBAL = {
    "_comment": "S88: per-cell strata banding in basement column.  Vein detection: terrain Laplacian > lap_threshold AND |simplex_fault| < fault_width => vein candidate.  Veins follow ridge lines + organic fault curves -- not flat diagonals.",
    "vein_lap_threshold": 4.0,
    "vein_fault_scale_blocks": 80,
    "vein_fault_width": 0.08,
    "noise_scale_blocks": 80,
    "n_bands_per_palette": 3,
}


def main() -> int:
    if not CFG_PATH.exists():
        print(f"ERROR: {CFG_PATH} not found", file=sys.stderr)
        return 2
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    groups = cfg.setdefault("lithology", {}).setdefault("groups", {})

    # Add per-group strata block
    for gname, strata_cfg in STRATA.items():
        if gname not in groups:
            print(f"  WARN: group {gname} not in config — skipping", file=sys.stderr)
            continue
        groups[gname]["strata"] = strata_cfg
        print(f"  {gname}.strata: added")

    # Add top-level strata config
    litho = cfg["lithology"]
    if "strata" not in litho:
        litho["strata"] = STRATA_GLOBAL
        print(f"  lithology.strata: added (global vein/noise knobs)")

    out = json.dumps(cfg, indent=2, ensure_ascii=True)
    CFG_PATH.write_text(out + "\n", encoding="utf-8")
    print(f"OK: {CFG_PATH} updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
