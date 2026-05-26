"""
apply_s88_config.py — one-shot edit applying S88 config additions.

Inserts:
  - cap_palette, talus_palette, bedrock_drainage_palette per lithology group
  - Replaces temperate_basaltic.wash_palette with arid_basaltic-style list
  - Adds lithology.cliff_cap, lithology.talus, lithology.bedrock_drainage
  - Adds eco_gradients.aspect

Writes config/thresholds.json in place.  Preserves indent + insertion order
where possible.  Run once; idempotent (skips existing keys).
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

CFG_PATH = Path("config/thresholds.json")

# ---------- per-group palettes (integer-multiplicity lists, weight ≈ count/total) ----------

CAP_PALETTES: dict[str, list[str]] = {
    "granitic":
        ["dripstone_block"] * 5
        + ["soul_soil"] * 3
        + ["coarse_dirt"] * 1
        + ["raw_iron_block"] * 1,
    "arid_basaltic":
        ["smooth_basalt"] * 11
        + ["basalt"] * 6
        + ["cobbled_deepslate"] * 3,
    "temperate_basaltic":
        ["blackstone"] * 9
        + ["cobbled_deepslate"] * 6
        + ["black_concrete_powder"] * 5,
    "limestone":
        ["dead_horn_coral_block"] * 7
        + ["andesite"] * 6
        + ["light_gray_concrete_powder"] * 5
        + ["diorite"] * 2,
    "deepslate_metamorphic":
        ["andesite"] * 11
        + ["cobblestone"] * 9,
    "mossy_temperate":
        ["tuff"] * 8
        + ["stone"] * 7
        + ["pale_moss_block"] * 5,
}

TALUS_PALETTES: dict[str, list[str]] = {
    "granitic":
        ["gravel"] * 3
        + ["coarse_dirt"] * 3
        + ["rooted_dirt"] * 2
        + ["brown_concrete_powder"] * 2,
    "arid_basaltic":
        ["cobbled_deepslate"] * 6
        + ["basalt"] * 6
        + ["gravel"] * 5
        + ["gray_concrete_powder"] * 3,
    "temperate_basaltic":
        ["cobbled_deepslate"] * 9
        + ["deepslate"] * 6
        + ["gravel"] * 5,
    "limestone":
        ["cobblestone"] * 7
        + ["diorite"] * 6
        + ["gravel"] * 4
        + ["light_gray_concrete_powder"] * 3,
    "deepslate_metamorphic":
        ["cobblestone"] * 8
        + ["andesite"] * 5
        + ["gravel"] * 5
        + ["coarse_dirt"] * 2,
    "mossy_temperate":
        ["mossy_cobblestone"] * 7
        + ["cobblestone"] * 4
        + ["tuff"] * 3
        + ["mud"] * 2
        + ["soul_soil"] * 2
        + ["moss_block"] * 2,
}

BEDROCK_DRAINAGE_PALETTES: dict[str, list[str]] = {
    "granitic":
        ["soul_soil"] * 9
        + ["dripstone_block"] * 8
        + ["granite"] * 3,
    "arid_basaltic":
        ["smooth_basalt"] * 9
        + ["tuff"] * 7
        + ["pale_moss_block"] * 4,
    "temperate_basaltic":
        ["basalt"] * 11
        + ["tuff"] * 9,
    "limestone":
        ["andesite"] * 10
        + ["stone"] * 8
        + ["diorite"] * 2,
    "deepslate_metamorphic":
        ["andesite"] * 7
        + ["cobblestone"] * 5
        + ["mossy_cobblestone"] * 4
        + ["tuff"] * 4,
    "mossy_temperate":
        ["tuff"] * 8
        + ["mossy_cobblestone"] * 8
        + ["pale_moss_block"] * 4,
}

# Temperate_basaltic wash_palette: replaced with arid_basaltic-style values
# (independent copy — diverges if either gets tuned later).
TEMPERATE_BASALTIC_NEW_WASH: list[str] = (
    ["gravel"] * 4
    + ["gray_concrete_powder"] * 2
    + ["sand"] * 1
    + ["coarse_dirt"] * 1
)

# ---------- top-level config additions ----------

LITHOLOGY_CLIFF_CAP_CFG = {
    "_comment": "S88: resistant cap-rock palette painted on flat shelves above slope>=cliff_min_deg cliffs. NOT excluded from Phase 2A noise (hard-erosion category — block-scale weathering is realistic).",
    "search_blocks": 4,
    "cliff_min_deg": 35.0,
    "flat_max_deg": 20.0,
    "intensity_threshold": 64
}

LITHOLOGY_TALUS_CFG = {
    "_comment": "S88: rubble apron palette painted on flat ground below slope>=cliff_min_deg cliffs. EXCLUDED from Phase 2A noise (soft/depositional — like washes, surface should be smooth).",
    "search_blocks": 8,
    "cliff_min_deg": 35.0,
    "apron_max_deg": 25.0,
    "intensity_threshold": 64,
    "phase2a_exclude": True
}

LITHOLOGY_BEDROCK_DRAINAGE_CFG = {
    "_comment": "S88: water-cut polished rock palette painted on steep flow channels (flow>flow_threshold AND slope>=slope_min_deg). NOT excluded from Phase 2A noise (hard-erosion category).",
    "flow_threshold": 0.02,
    "slope_min_deg": 25.0,
    "dilation_blocks": 1,
    "fade_blocks": 3,
    "intensity_threshold": 64
}

ECO_GRADIENTS_ASPECT_CFG = {
    "_comment": "S88: rock_gap probability modulator. SW-facing (sunny/dry) slopes multiplied by 1 + amplitude*cos(aspect - peak); NE-facing get the negative. Below slope_min_deg, aspect ignored (sentinel byte 255).",
    "modifier_amplitude": 0.20,
    "peak_azimuth_deg": 225.0,
    "slope_min_deg": 5.0
}


def main() -> int:
    if not CFG_PATH.exists():
        print(f"ERROR: {CFG_PATH} not found", file=sys.stderr)
        return 2
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))

    # ---- groups: add 3 new palettes per group ----
    groups = cfg.setdefault("lithology", {}).setdefault("groups", {})
    for grp_name, grp in groups.items():
        if grp_name in CAP_PALETTES and "cap_palette" not in grp:
            grp["cap_palette"] = CAP_PALETTES[grp_name]
        if grp_name in TALUS_PALETTES and "talus_palette" not in grp:
            grp["talus_palette"] = TALUS_PALETTES[grp_name]
        if grp_name in BEDROCK_DRAINAGE_PALETTES and "bedrock_drainage_palette" not in grp:
            grp["bedrock_drainage_palette"] = BEDROCK_DRAINAGE_PALETTES[grp_name]

    # ---- temperate_basaltic wash_palette swap (REPLACES existing) ----
    if "temperate_basaltic" in groups:
        groups["temperate_basaltic"]["wash_palette"] = TEMPERATE_BASALTIC_NEW_WASH
        # tag the description so it's clear in-config
        old_desc = groups["temperate_basaltic"].get("description", "")
        if "S88:" not in old_desc:
            groups["temperate_basaltic"]["description"] = (
                old_desc + " S88: wash_palette replaced with arid_basaltic-style "
                "values (independent copy)."
            )

    # ---- top-level config blocks ----
    litho = cfg["lithology"]
    if "cliff_cap" not in litho:
        litho["cliff_cap"] = LITHOLOGY_CLIFF_CAP_CFG
    if "talus" not in litho:
        litho["talus"] = LITHOLOGY_TALUS_CFG
    if "bedrock_drainage" not in litho:
        litho["bedrock_drainage"] = LITHOLOGY_BEDROCK_DRAINAGE_CFG

    eco_grads = cfg.setdefault("eco_gradients", {})
    if "aspect" not in eco_grads:
        eco_grads["aspect"] = ECO_GRADIENTS_ASPECT_CFG

    # Write back with 2-space indent (matches existing file convention).
    # ensure_ascii=True (the default) preserves the original file's \uXXXX
    # escape sequences — without it, all em-dashes etc. get rewritten as
    # raw UTF-8 bytes and pre-existing mojibake gets revealed (cosmetic
    # noise that bloats the diff without changing semantics).
    out = json.dumps(cfg, indent=2, ensure_ascii=True)
    CFG_PATH.write_text(out + "\n", encoding="utf-8")
    print(f"OK: {CFG_PATH} updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
