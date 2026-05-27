"""
apply_s88_walk5.py — S88 walk #5 user-feedback bundle.

Per user direction after walk #4d in-world review:

A. PER-GROUP PALETTE OVERHAUL
   granitic:
     strata.band_a       = 60/40 dripstone_block + packed_mud
     strata.band_b       = 80/20 granite + rooted_dirt
     wash_palette        = dead_bubble_coral_block + dead_horn_coral_block (50/50)
     bedrock_drainage    = soul_soil + soul_sand + brown_concrete_powder
     talus               = coarse_dirt + brown_concrete_powder
     strata.vein_blocks  = muddy_mangrove_roots + mud
     cap                 = dead_fire_coral_block + dead_tube_coral_block + andesite
   temperate_basaltic:
     strata.speckle      = dripstone_block + packed_mud
     wash_palette        = light_gray_concrete_powder + pale_moss_block
     talus               = gravel + clay
     strata.vein_blocks  = black_concrete_powder + coal_block
   deepslate_metamorphic:
     wash_palette        = packed_mud + brown_mushroom_block
     bedrock_drainage    = tuff + mud
     strata.vein_blocks  = basalt + smooth_basalt
     cap                 = basalt + smooth_basalt

B. STRATA BAND SCALE — MASSIVE BUMP (all groups)
   User: "We need to up the scale of veins and strata bands MASSIVELY
   across the board.  right now they get swallowed by the blocky form
   and details are lost."
   - Y_tilted:  thickness_min/max bumped to make bands much wider.
                limestone 1-14 -> 40-100,
                deepslate 6-16 -> 40-100,
                mossy     8-18 -> 40-100,
                granitic  80-160 -> 160-320.
   - XZ_cols:   col_size_blocks 2 -> 6 (3x wider vertical columns).
                primary_pct 70 -> 85 (sharper, less internal speckling
                so columns don't blend into noise).

C. VEIN SCALE — MASSIVE BUMP
   strata.vein_fault_scale_blocks 80 -> 240
   strata.vein_fault_width        0.08 -> 0.15
   (no per-group vein_amp change -- already 0.2-0.6, scale was the real
    issue per user.)

D. ROCK_GAP FADE TIGHTER
   "Adjust rock gap mask globally to fade more tightly (lower distance)
    and closer to bounds of strata formation."
   eco_gradients.rock_gap_5block_fade:
     slope_solid_deg 30 -> 32 (matches strata fade-in start)
     fade_blocks     5  -> 2

E. TREE SLOPE BOUNDS — MORE PERMISSIVE
   "Up the incline/slope to which trees can grow globally."
   Real-world: alpine pines on 50-70° scree, krummholz on 80°+.
   eco_placement.slope_penalty_start_deg 35 -> 45
   eco_placement.slope_penalty_full_deg  50 -> 65

F. WIDER CLIFF_CAP
   "render a much wider rock cap so we actually get rocky peaks unique
    to their surroundings."
   lithology.cliff_cap.intensity_threshold 64 -> 32
   (Lower threshold = more pixels in the existing mask qualify.  No
    mask rebuild needed.)
"""
from __future__ import annotations
import json, sys
from pathlib import Path

CFG_PATH = Path("config/thresholds.json")


def weighted(blocks: list[tuple[str, int]]) -> list[str]:
    """Expand [('a', 5), ('b', 5)] -> ['a','a','a','a','a','b','b','b','b','b']."""
    out: list[str] = []
    for blk, w in blocks:
        out.extend([blk] * w)
    return out


def main() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    g = cfg["lithology"]["groups"]

    # ---- A. Granitic palette overhaul ----
    gr = g["granitic"]
    gr["wash_palette"] = weighted([
        ("dead_bubble_coral_block", 5), ("dead_horn_coral_block", 5),
    ])
    gr["bedrock_drainage_palette"] = weighted([
        ("soul_soil", 4), ("soul_sand", 3), ("brown_concrete_powder", 3),
    ])
    gr["talus_palette"] = weighted([
        ("coarse_dirt", 5), ("brown_concrete_powder", 5),
    ])
    gr["cap_palette"] = weighted([
        ("dead_fire_coral_block", 4),
        ("dead_tube_coral_block", 3),
        ("andesite", 3),
    ])
    gr_strata = gr["strata"]
    gr_strata["band_a"] = {
        "primary": "dripstone_block", "secondary": "packed_mud",
        "primary_pct": 60,
    }
    gr_strata["band_b"] = {
        "primary": "granite", "secondary": "rooted_dirt",
        "primary_pct": 80,
    }
    gr_strata["vein_blocks"] = ["muddy_mangrove_roots", "mud"]
    print("  granitic: palettes + strata overhauled")

    # ---- A. Temperate basaltic palette ----
    tb = g["temperate_basaltic"]
    tb["wash_palette"] = weighted([
        ("light_gray_concrete_powder", 5), ("pale_moss_block", 5),
    ])
    tb["talus_palette"] = weighted([
        ("gravel", 5), ("clay", 5),
    ])
    tb_strata = tb["strata"]
    tb_strata["speckle_blocks"] = ["dripstone_block", "packed_mud"]
    tb_strata["vein_blocks"] = ["black_concrete_powder", "coal_block"]
    print("  temperate_basaltic: wash/talus + strata.speckle/veins updated")

    # ---- A. Deepslate metamorphic palette ----
    dm = g["deepslate_metamorphic"]
    dm["wash_palette"] = weighted([
        ("packed_mud", 5), ("brown_mushroom_block", 5),
    ])
    dm["bedrock_drainage_palette"] = weighted([
        ("tuff", 5), ("mud", 5),
    ])
    dm["cap_palette"] = weighted([
        ("basalt", 5), ("smooth_basalt", 5),
    ])
    dm_strata = dm["strata"]
    dm_strata["vein_blocks"] = ["basalt", "smooth_basalt"]
    print("  deepslate_metamorphic: wash/bedrock/cap + strata.veins updated")

    # ---- B. Strata band scale (Y_tilted thickness bump) ----
    Y_TILTED_TARGET = {
        "granitic":              (160, 320),  # was 80-160 (2x)
        "limestone":             ( 40, 100),  # was 1-14
        "deepslate_metamorphic": ( 40, 100),  # was 6-16
        "mossy_temperate":       ( 40, 100),  # was 8-18
    }
    for name, (mn, mx) in Y_TILTED_TARGET.items():
        s = g[name]["strata"]
        if s.get("axis") == "Y_tilted":
            old = (s.get("thickness_min"), s.get("thickness_max"))
            s["thickness_min"] = mn
            s["thickness_max"] = mx
            print(f"  {name}.strata thickness {old} -> ({mn},{mx})")

    # ---- B. Strata XZ_cols (basaltic) widening ----
    for name in ("arid_basaltic", "temperate_basaltic"):
        s = g[name]["strata"]
        if s.get("axis") == "XZ_cols":
            old_size = s.get("col_size_blocks", 2)
            s["col_size_blocks"] = 6  # 3x wider columns
            # Sharpen primary_pct on both bands so columns don't blend
            for band_key in ("band_a", "band_b"):
                if band_key in s and "primary_pct" in s[band_key]:
                    s[band_key]["primary_pct"] = 85  # was 70
            print(f"  {name}.strata col_size {old_size}->6, primary_pct 70->85")

    # ---- C. Vein scale massive bump (global) ----
    strata_global = cfg["lithology"].setdefault("strata", {})
    old_scale = strata_global.get("vein_fault_scale_blocks", 80)
    old_width = strata_global.get("vein_fault_width", 0.08)
    strata_global["vein_fault_scale_blocks"] = 240  # 3x
    strata_global["vein_fault_width"] = 0.15        # ~2x wider zones
    print(f"  strata.vein_fault_scale_blocks {old_scale} -> 240")
    print(f"  strata.vein_fault_width        {old_width} -> 0.15")

    # ---- D. Rock_gap fade tighter ----
    eg_cfg = cfg.setdefault("eco_gradients", {})
    rg_cfg = eg_cfg.setdefault("rock_gap_5block_fade", {})
    old_solid = rg_cfg.get("slope_solid_deg", 30)
    old_fade  = rg_cfg.get("fade_blocks", 5)
    rg_cfg["slope_solid_deg"] = 32.0
    rg_cfg["fade_blocks"] = 2
    rg_cfg["_comment"] = (
        "S88 walk #5: tightened.  slope_solid 30->32 to align with strata "
        "fade-in floor (strata.surface_min_deg=32).  fade_blocks 5->2 for "
        "a sharper boundary closer to the strata formation zone."
    )
    print(f"  rock_gap_5block_fade: slope_solid {old_solid}->32, fade_blocks {old_fade}->2")

    # ---- E. Tree slope bounds ----
    ep_cfg = cfg.setdefault("eco_placement", {})
    old_start = ep_cfg.get("slope_penalty_start_deg", 35)
    old_full  = ep_cfg.get("slope_penalty_full_deg", 50)
    ep_cfg["slope_penalty_start_deg"] = 45.0  # was 35
    ep_cfg["slope_penalty_full_deg"]  = 65.0  # was 50
    print(f"  eco_placement: slope_penalty start {old_start}->45, full {old_full}->65")

    # ---- F. Wider cliff_cap (lower intensity_threshold) ----
    cc_cfg = cfg["lithology"].setdefault("cliff_cap", {})
    old_thr = cc_cfg.get("intensity_threshold", 64)
    cc_cfg["intensity_threshold"] = 32  # was 64
    print(f"  cliff_cap.intensity_threshold {old_thr} -> 32 (wider cap)")

    out = json.dumps(cfg, indent=2, ensure_ascii=True)
    CFG_PATH.write_text(out + "\n", encoding="utf-8")
    print(f"OK: {CFG_PATH} updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
