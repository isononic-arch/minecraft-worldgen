"""
apply_s88_strata_v2.py — S88 walk #4 strata model rewrite.

New schema per lithology group:
  axis            "Y_tilted" (horizontal/tilted bands) or "XZ_cols" (vertical columns)
  thickness_min/max  band thickness (Y_tilted only)
  col_size_blocks  XZ column size in blocks (XZ_cols only)
  tilt_per_100blocks  Y blocks per 100 horizontal blocks
  tilt_dir_deg     compass dir (0=E, 90=N)
  noise_amp_blocks ± Y wobble (organic band boundary)
  intersperse      cross-band mixing strength at boundaries
  band_a / band_b  {primary, secondary, primary_pct} — bands are MIXES,
                   not single blocks.  2-band strict alternation A-B-A-B.
  speckle_blocks   list of speckle blocks (uniform pick per cell)
  speckle_rate     per-cell speckle probability
  vein_blocks      list of vein blocks (unchanged)
  vein_amp         per-cell vein probability

Also writes:
  lithology.cliff_cap.flat_max_deg = 28  (was 20)  -- caps fire at cliff
    edge transition zone, not just flat tops.  Requires cliff_cap.tif
    mask rebuild via tools/build_terrain_derived.py --only cap.

  eco_gradients.rock_gap_5block_fade = {enabled: true, slope_solid_deg: 30,
    fade_blocks: 5}  -- new sharper distance-based fade replaces the old
    25-32° slope-fade.

  lithology.strata.surface_min_deg = 28  -- strata covers more of the
    cliff face (was 32°), kills biome surface bleed-through.
"""
from __future__ import annotations
import json, sys
from pathlib import Path

CFG_PATH = Path("config/thresholds.json")

NEW_STRATA: dict[str, dict] = {
    "granitic": {
        "axis": "Y_tilted",
        "thickness_min": 80, "thickness_max": 160,
        "tilt_per_100blocks": 30, "tilt_dir_deg": 45,
        "noise_amp_blocks": 12, "intersperse": 0.7,
        "band_a": {"primary": "granite", "secondary": "dripstone_block", "primary_pct": 75},
        "band_b": {"primary": "dripstone_block", "secondary": "granite", "primary_pct": 75},
        "speckle_blocks": ["raw_iron_block"],
        "speckle_rate": 0.10,
        "vein_blocks": ["raw_iron_block", "soul_soil"],
        "vein_amp": 0.40,
        "_comment": "Massive intrusive — 80-160 block bands, crazy 30°/100 tilt, 75/25 granite/dripstone mix with iron speckle."
    },
    "arid_basaltic": {
        "axis": "XZ_cols",
        "col_size_blocks": 2,
        "tilt_per_100blocks": 0, "tilt_dir_deg": 0,
        "noise_amp_blocks": 0, "intersperse": 0.0,
        "band_a": {"primary": "basalt", "secondary": "smooth_basalt", "primary_pct": 70},
        "band_b": {"primary": "smooth_basalt", "secondary": "basalt", "primary_pct": 70},
        "speckle_blocks": ["deepslate"],
        "speckle_rate": 0.08,
        "vein_blocks": ["dead_fire_coral_block", "dead_bubble_coral_block"],
        "vein_amp": 0.20,
        "_comment": "Volcanic — 2-block VERTICAL COLUMNS (Devil's Postpile style), 70/30 basalt/smooth_basalt with deepslate speckle."
    },
    "temperate_basaltic": {
        "axis": "XZ_cols",
        "col_size_blocks": 2,
        "tilt_per_100blocks": 0, "tilt_dir_deg": 0,
        "noise_amp_blocks": 0, "intersperse": 0.0,
        "band_a": {"primary": "deepslate", "secondary": "cobbled_deepslate", "primary_pct": 70},
        "band_b": {"primary": "cobbled_deepslate", "secondary": "deepslate", "primary_pct": 70},
        "speckle_blocks": ["deepslate_coal_ore", "smooth_basalt"],
        "speckle_rate": 0.10,
        "vein_blocks": ["deepslate_coal_ore", "tuff"],
        "vein_amp": 0.50,
        "_comment": "Dark metamorphic — 2-block VERTICAL COLUMNS, 70/30 deepslate/cobbled_deepslate with coal_ore + smooth_basalt speckle."
    },
    "limestone": {
        "axis": "Y_tilted",
        "thickness_min": 1, "thickness_max": 14,
        "tilt_per_100blocks": 6, "tilt_dir_deg": 15,
        "noise_amp_blocks": 2, "intersperse": 0.15,
        "band_a": {"primary": "calcite", "secondary": "diorite", "primary_pct": 80},
        "band_b": {"primary": "diorite", "secondary": "calcite", "primary_pct": 80},
        "speckle_blocks": ["andesite"],
        "speckle_rate": 0.10,
        "vein_blocks": ["dead_horn_coral_block", "dead_fire_coral_block"],
        "vein_amp": 0.30,
        "_comment": "Sedimentary — 1-14 block bands (highly variable), mild 6°/100 tilt @ 15°, 80/20 calcite/diorite with andesite speckle."
    },
    "deepslate_metamorphic": {
        "axis": "Y_tilted",
        "thickness_min": 6, "thickness_max": 16,
        "tilt_per_100blocks": 25, "tilt_dir_deg": 30,
        "noise_amp_blocks": 4, "intersperse": 0.20,
        "band_a": {"primary": "andesite", "secondary": "stone", "primary_pct": 90},
        "band_b": {"primary": "stone", "secondary": "andesite", "primary_pct": 90},
        "speckle_blocks": ["tuff"],
        "speckle_rate": 0.06,
        "vein_blocks": ["dead_fire_coral_block", "dead_bubble_coral_block"],
        "vein_amp": 0.60,
        "_comment": "Folded mountain metamorphic — 6-16 block bands, STRONG 25°/100 tilt @ 30°, 90/10 andesite/stone with tuff speckle."
    },
    "mossy_temperate": {
        "axis": "Y_tilted",
        "thickness_min": 8, "thickness_max": 18,
        "tilt_per_100blocks": 4, "tilt_dir_deg": 0,
        "noise_amp_blocks": 3, "intersperse": 0.30,
        "band_a": {"primary": "cobblestone", "secondary": "mossy_cobblestone", "primary_pct": 60},
        "band_b": {"primary": "mossy_cobblestone", "secondary": "cobblestone", "primary_pct": 60},
        "speckle_blocks": ["tuff", "moss_block", "andesite"],
        "speckle_rate": 0.15,
        "vein_blocks": ["moss_block"],
        "vein_amp": 0.30,
        "_comment": "Wet temperate — 8-18 block bands, mild 4°/100 tilt, 60/40 cobblestone/mossy split with triple speckle (tuff+moss+andesite)."
    },
}


def main() -> int:
    if not CFG_PATH.exists():
        print(f"ERROR: {CFG_PATH} not found", file=sys.stderr)
        return 2
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    groups = cfg.setdefault("lithology", {}).setdefault("groups", {})

    for gname, strata in NEW_STRATA.items():
        if gname not in groups:
            print(f"  WARN: group {gname} not in config, skipping")
            continue
        groups[gname]["strata"] = strata
        print(f"  {gname}.strata: replaced (axis={strata['axis']})")

    # Top-level lithology.strata config
    litho = cfg["lithology"]
    if "strata" not in litho:
        litho["strata"] = {}
    litho["strata"]["surface_min_deg"] = 28.0  # was 32
    litho["strata"]["_comment"] = (
        "S88 walk #4: surface_min_deg lowered 32->28 so strata covers more "
        "of the cliff face (kills biome dirt/podzol bleed-through).  "
        "vein_lap_threshold / vein_fault_scale_blocks / vein_fault_width "
        "unchanged."
    )
    # Preserve existing vein global knobs if present
    litho["strata"].setdefault("vein_lap_threshold", 4.0)
    litho["strata"].setdefault("vein_fault_scale_blocks", 80)
    litho["strata"].setdefault("vein_fault_width", 0.08)
    print(f"  lithology.strata.surface_min_deg = 28.0 (was 32)")

    # Cap flat_max_deg bump
    litho.setdefault("cliff_cap", {})
    litho["cliff_cap"]["flat_max_deg"] = 28.0  # was 20
    print(f"  lithology.cliff_cap.flat_max_deg = 28.0 (was 20)")

    # Rock_gap new 5-block distance fade config (replaces walk #3 floor+fade)
    eco = cfg.setdefault("eco_gradients", {})
    eco["rock_gap_5block_fade"] = {
        "_comment": "S88 walk #4: replace walk #3 slope-fade (25-32° + 0.40 floor) with sharp distance fade.  Solid rock_gap at slope >= slope_solid_deg, then fade probability over fade_blocks (distance) outside.  Sharper boundary, no Swiss-cheese holes.",
        "enabled": True,
        "slope_solid_deg": 30.0,
        "fade_blocks": 5,
    }
    print(f"  eco_gradients.rock_gap_5block_fade added (solid >= 30°, fade 5 blocks)")

    out = json.dumps(cfg, indent=2, ensure_ascii=True)
    CFG_PATH.write_text(out + "\n", encoding="utf-8")
    print(f"OK: {CFG_PATH} updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
