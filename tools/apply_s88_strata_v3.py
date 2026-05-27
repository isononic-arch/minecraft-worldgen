"""
apply_s88_strata_v3.py — S88 walk #4c iteration.

Changes per user direction:
  1. Strata surface_min_deg 28 -> 32, plus NEW surface_fade_max_deg = 35.
     Strata fades in from 32-35 degrees instead of solid >=28.
  2. Speckle rates reduced globally to 0.02 ("very rare").
  3. Arid_basaltic strata bands restructured:
       band_a: basalt + gray_concrete_powder (was basalt + smooth_basalt)
       band_b: smooth_basalt + tuff (was smooth_basalt + basalt)
       speckle_blocks: ["mud"] (was ["deepslate"])
  4. Temperate_basaltic strata bands restructured:
       band_a: cobbled_deepslate + deepslate (was deepslate + cobbled)
       band_b: dead_horn_coral_block + suspicious_gravel (NEW, vein-exception
               blocks not in temperate's main palette set).
"""
from __future__ import annotations
import json, sys
from pathlib import Path

CFG_PATH = Path("config/thresholds.json")


def main() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    g = cfg["lithology"]["groups"]

    # ---- Top-level strata: fade-in 32-35 ----
    strata_global = cfg["lithology"].setdefault("strata", {})
    strata_global["surface_min_deg"] = 32.0  # was 28
    strata_global["surface_fade_max_deg"] = 35.0  # NEW
    strata_global["_comment"] = (
        "S88 walk #4c: surface_min_deg bumped 28->32 + new surface_fade_max_deg=35. "
        "Strata FADES IN over slope 32-35 (probabilistic) instead of solid >=28 "
        "(was overpainting too much).  Below 32: no strata.  Above 35: solid."
    )
    print(f"  strata.surface_min_deg = 32, surface_fade_max_deg = 35")

    # ---- Speckle reduction (very rare per user) ----
    GLOBAL_SPECKLE_RATE = 0.02
    for name in g:
        s = g[name].get("strata")
        if not s:
            continue
        old = s.get("speckle_rate", 0)
        s["speckle_rate"] = GLOBAL_SPECKLE_RATE
        print(f"  {name}.strata.speckle_rate {old} -> {GLOBAL_SPECKLE_RATE}")

    # ---- Arid basaltic restructure ----
    ab = g["arid_basaltic"]["strata"]
    ab["band_a"] = {"primary": "basalt", "secondary": "gray_concrete_powder", "primary_pct": 70}
    ab["band_b"] = {"primary": "smooth_basalt", "secondary": "tuff", "primary_pct": 70}
    ab["speckle_blocks"] = ["mud"]
    ab["_comment"] = (
        "S88 walk #4c: bands restructured -- A=basalt/gray_concrete_powder, "
        "B=smooth_basalt/tuff.  speckle=[mud] (was [deepslate])."
    )
    print(f"  arid_basaltic.strata: band_a=basalt+gray_concrete_powder, "
          f"band_b=smooth_basalt+tuff, speckle=[mud]")

    # ---- Temperate basaltic restructure ----
    tb = g["temperate_basaltic"]["strata"]
    tb["band_a"] = {"primary": "cobbled_deepslate", "secondary": "deepslate", "primary_pct": 70}
    tb["band_b"] = {"primary": "dead_horn_coral_block", "secondary": "suspicious_gravel", "primary_pct": 70}
    tb["_comment"] = (
        "S88 walk #4c: bands restructured -- A=cobbled_deepslate/deepslate, "
        "B=dead_horn_coral_block/suspicious_gravel (latter two are vein-exception "
        "blocks outside the temperate palette set, per user permission).  speckle "
        "unchanged (deepslate_coal_ore + smooth_basalt) but rate dropped to 0.02."
    )
    print(f"  temperate_basaltic.strata: band_a=cobbled_deepslate+deepslate, "
          f"band_b=dead_horn_coral_block+suspicious_gravel")

    out = json.dumps(cfg, indent=2, ensure_ascii=True)
    CFG_PATH.write_text(out + "\n", encoding="utf-8")
    print(f"OK: {CFG_PATH} updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
