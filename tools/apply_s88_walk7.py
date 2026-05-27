"""
apply_s88_walk7.py — S88 walk #7 user-feedback bundle.

After walk #6 in-world review user reported:
  - Granitic: working (the lone success).
  - Arid_basaltic: "just noise again", no other layers visible
  - Temperate_basaltic: noise + cap covering entire mountain
  - Limestone: cap covering every surface
  - Deepslate_metamorphic: cap covering everything
  - Mossy: implicitly same (not called out)
  - Veins: single-block scatter; needs to be visible streaks

Root causes:
  A. Cap threshold 8 + dilation 12 = nearly EVERY cliff pixel painted as
     cap, overpainting strata/wash/veins/bedrock_drainage.
  B. XZ_cols aspect-perpendicular hash with col_size=5 + strong terrain
     wobble (surface_y/20 per pixel) = single-pixel band noise.
  C. Veins paint individual pixels where vein_field fires; no axis-aligned
     cluster expansion (unlike speckle which has 1-4 block runs).

WALK #7 CHANGES:

A. CAP DIAL-BACK
   - cliff_cap.intensity_threshold 8 -> 48 (between walk #5 (32) and walk #6 (8))
   - cliff_cap.dilate_blocks 12 -> 4

B. XZ_cols WIDER + GENTLER WOBBLE
   - arid_basaltic.col_size_blocks 5 -> 16
   - temperate_basaltic.col_size_blocks 5 -> 16
   - NEW strata.xz_wobble_amp = 0.25 (was effective 1.0 via surface_y/(col*4);
     code now reads from config and applies as wobble * scale).
   - NEW strata.aspect_smooth_sigma = 4.0 (gaussian smoothing on aspect
     before the perpendicular hash, kills pixel-level aspect noise).

C. VEINS AS STREAKS
   - NEW strata.vein_streak_min = 3
   - NEW strata.vein_streak_max = 8
   Code (surface_decorator._apply_strata_veins_surface) will cluster vein
   pixels into 3-8 block runs along the strata axis, similar to how
   speckle is clustered into 1-4 block runs.
"""
from __future__ import annotations
import json, sys
from pathlib import Path

CFG_PATH = Path("config/thresholds.json")


def main() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    g = cfg["lithology"]["groups"]

    # A. Cap dial-back
    cc = cfg["lithology"].setdefault("cliff_cap", {})
    old_thr = cc.get("intensity_threshold", 8)
    old_dil = cc.get("dilate_blocks", 12)
    cc["intensity_threshold"] = 48
    cc["dilate_blocks"] = 4
    print(f"  cliff_cap.intensity_threshold {old_thr} -> 48")
    print(f"  cliff_cap.dilate_blocks {old_dil} -> 4")

    # B. Basaltic XZ_cols wider + gentler wobble + aspect smoothing
    for name in ("arid_basaltic", "temperate_basaltic"):
        s = g[name]["strata"]
        old_size = s.get("col_size_blocks", 5)
        s["col_size_blocks"] = 16  # 3x wider
        print(f"  {name}.strata.col_size_blocks {old_size} -> 16")

    sg = cfg["lithology"].setdefault("strata", {})
    sg["xz_wobble_amp"] = 0.25
    sg["aspect_smooth_sigma"] = 4.0
    print("  strata.xz_wobble_amp NEW = 0.25 (gentler terrain wobble)")
    print("  strata.aspect_smooth_sigma NEW = 4.0 (smooths aspect before hash)")

    # C. Vein streak length range
    sg["vein_streak_min"] = 3
    sg["vein_streak_max"] = 8
    print("  strata.vein_streak_min NEW = 3")
    print("  strata.vein_streak_max NEW = 8")

    out = json.dumps(cfg, indent=2, ensure_ascii=True)
    CFG_PATH.write_text(out + "\n", encoding="utf-8")
    print(f"OK: {CFG_PATH} updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
