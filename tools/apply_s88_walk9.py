"""
apply_s88_walk9.py — S88 walk #9 final-iteration bundle.

User direction after walk #8 in-world review.  CONFIG portion below;
code changes (varnish cliff-base detection, _rock_zone_cleanup pass,
chunk_writer Y-2..Y-5 force-clean) land in surface_decorator.py and
chunk_writer.py edits.
"""
from __future__ import annotations
import json, sys
from pathlib import Path

CFG_PATH = Path("config/thresholds.json")


def weighted(pairs: list[tuple[str, int]]) -> list[str]:
    out: list[str] = []
    for blk, w in pairs:
        out.extend([blk] * w)
    return out


def main() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    g = cfg["lithology"]["groups"]

    # =====================================================================
    # A. CONCAVITY — new per-group concavity_palette (copy spec from user)
    # =====================================================================
    # granitic: copy bedrock_drainage_palette
    # arid_basaltic: copy varnish_palette
    # temperate_basaltic: copy talus_palette
    # limestone: copy vein_blocks
    # deepslate_metamorphic: copy vein_blocks
    # mossy_temperate: copy speckle_blocks

    # Apply arid_basaltic palette edits FIRST so concavity copy uses NEW varnish
    # (user asked talus / bedrock / veins all changed)
    ab = g["arid_basaltic"]
    ab["talus_palette"] = weighted([
        ("packed_mud", 4), ("rooted_dirt", 4), ("brown_concrete_powder", 2),
    ])
    ab["bedrock_drainage_palette"] = weighted([
        ("red_sand", 4), ("granite", 3), ("rooted_dirt", 3),
    ])
    ab["strata"]["vein_blocks"] = [
        "smooth_red_sandstone", "red_sand", "brown_concrete_powder", "granite",
    ]
    print("  arid_basaltic: talus / bedrock / veins overhauled")

    # temperate_basaltic: remove coal_block, varnish = tuff+mud, veins contrasting
    tb = g["temperate_basaltic"]
    tb["varnish_palette"] = weighted([
        ("tuff", 1), ("mud", 1),
    ])
    tb["strata"]["vein_blocks"] = ["black_concrete_powder", "tuff"]
    # Sanity check: no coal_block anywhere in temperate_basaltic
    _coal_locations = []
    for k, v in tb.items():
        if isinstance(v, list) and "coal_block" in v:
            _coal_locations.append(k)
        if isinstance(v, dict):
            for kk, vv in v.items():
                if isinstance(vv, list) and "coal_block" in vv:
                    _coal_locations.append(f"strata.{kk}")
    if _coal_locations:
        print(f"  WARNING: coal_block still in temperate_basaltic: {_coal_locations}")
    else:
        print("  temperate_basaltic: coal_block fully removed, varnish=tuff+mud, veins=blk_concrete+tuff")

    # deepslate_metamorphic: talus swap
    dm = g["deepslate_metamorphic"]
    dm["talus_palette"] = weighted([
        ("packed_mud", 4), ("coarse_dirt", 3), ("suspicious_gravel", 3),
    ])
    print("  deepslate_metamorphic: talus = packed_mud + coarse_dirt + suspicious_gravel")

    # NOW add concavity_palette per group (copy from specified source AFTER
    # arid + temperate + deepslate edits above so we pick up the new lists).
    CONCAVITY_SOURCE = {
        "granitic":              "bedrock_drainage_palette",
        "arid_basaltic":         "varnish_palette",
        "temperate_basaltic":    "talus_palette",
        "limestone":             "vein_blocks",          # in strata sub-dict
        "deepslate_metamorphic": "vein_blocks",
        "mossy_temperate":       "speckle_blocks",
    }
    for gname, src_key in CONCAVITY_SOURCE.items():
        gdata = g[gname]
        if src_key in ("vein_blocks", "speckle_blocks"):
            src = list(gdata.get("strata", {}).get(src_key, []))
        else:
            src = list(gdata.get(src_key, []))
        if not src:
            print(f"  WARNING: {gname}.{src_key} empty; concavity_palette will be empty")
        gdata["concavity_palette"] = src
        print(f"  {gname}.concavity_palette = copy of {src_key}  ({len(src)} blocks)")

    # Update concavity config to use new key + lower threshold + dilation
    conv = cfg["lithology"].setdefault("concavity", {})
    conv["palette_key"] = "concavity_palette"
    old_lap = conv.get("lap_threshold", 1.5)
    conv["lap_threshold"] = 0.75
    conv["dilate_blocks"] = 2
    print(f"  concavity.palette_key -> concavity_palette")
    print(f"  concavity.lap_threshold {old_lap} -> 0.75")
    print(f"  concavity.dilate_blocks NEW = 2")

    # =====================================================================
    # B. VARNISH v2 — cliff-base detection + slope gate + dilation
    # =====================================================================
    varn = cfg["lithology"].setdefault("varnish", {})
    varn["slope_min_deg"] = 40.0
    varn["dilate_blocks"] = 3
    varn["cliff_base_window"] = 12   # local-min surface_y lookup window radius
    varn["cliff_base_band"] = 6      # only varnish if (sy - local_min) < band
    print(f"  varnish.slope_min_deg NEW = 40.0 (sharp slopes only)")
    print(f"  varnish.dilate_blocks NEW = 3")
    print(f"  varnish.cliff_base_window NEW = 12")
    print(f"  varnish.cliff_base_band NEW = 6")

    # =====================================================================
    # C. ROCK_GAP fade sharper + higher slope floor
    # =====================================================================
    eg = cfg.setdefault("eco_gradients", {})
    rg = eg.setdefault("rock_gap_5block_fade", {})
    old_solid = rg.get("slope_solid_deg", 32)
    old_fade = rg.get("fade_blocks", 2)
    rg["slope_solid_deg"] = 38.0
    rg["fade_blocks"] = 1
    print(f"  rock_gap.slope_solid_deg {old_solid} -> 38.0 (no rocks on flatter slopes)")
    print(f"  rock_gap.fade_blocks {old_fade} -> 1 (sharper cutoff)")

    # =====================================================================
    # D. TREE SLOPE BUMPS
    # =====================================================================
    ep = cfg.setdefault("eco_placement", {})
    old_start = ep.get("slope_penalty_start_deg", 45)
    old_full = ep.get("slope_penalty_full_deg", 65)
    ep["slope_penalty_start_deg"] = 55.0
    ep["slope_penalty_full_deg"] = 75.0
    print(f"  trees.slope_penalty_start_deg {old_start} -> 55.0")
    print(f"  trees.slope_penalty_full_deg {old_full} -> 75.0")

    # =====================================================================
    # E. CLEANUP PASS — config knob for what gets cleared on rock_gap
    # =====================================================================
    cfg["lithology"]["rock_zone_cleanup"] = {
        "_comment": (
            "S88 walk #9 NEW pass: on rock_gap (gap_mask==5) pixels, "
            "overwrite any surviving GRASS_FAMILY surface blocks or DIRT_FAMILY "
            "subsurface blocks with the per-litho rock_gap palette.  Catches "
            "grassy/dirt slip-through from ecotone dither, biome surface paint, "
            "etc.  Y-2..Y-5 (column basement) handled separately by "
            "chunk_writer cleanup."
        ),
        "enabled": True,
        "surface_bad_blocks": [
            "grass_block", "podzol", "mycelium",
            "snow", "snow_block", "powder_snow",
        ],
        "subsurface_bad_blocks": [
            "grass_block", "podzol", "mycelium",
            "dirt", "coarse_dirt",
        ],
        "column_top6_cleanup": True,  # chunk_writer-side Y-2..Y-5 force-clean
    }
    print("  lithology.rock_zone_cleanup NEW config (enabled, includes col-top6)")

    out = json.dumps(cfg, indent=2, ensure_ascii=True)
    CFG_PATH.write_text(out + "\n", encoding="utf-8")
    print(f"OK: {CFG_PATH} updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
