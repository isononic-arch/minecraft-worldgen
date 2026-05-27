"""
apply_s88_walk6.py — S88 walk #6 user-feedback bundle.

Per user direction after walk #5 in-world review:

A. CAPS — INSANELY BIGGER
   - lithology.cliff_cap.intensity_threshold 32 -> 8
   - lithology.cliff_cap.dilate_blocks       NEW key = 12  (runtime
     binary_dilation on paint_zone before painting)
   - Combined: ~4x more base pixels qualify + 12-block outward expansion
     = dramatically more cap coverage on peaks.

B. VEIN AMPLITUDE 2x GLOBAL
   - granitic              vein_amp 0.4  -> 0.85
   - arid_basaltic         vein_amp 0.2  -> 0.50
   - temperate_basaltic    vein_amp 0.5  -> 1.0
   - limestone             vein_amp 0.3  -> 0.70
   - deepslate_metamorphic vein_amp 0.6  -> 1.0
   - mossy_temperate       vein_amp 0.3  -> 0.70
   - strata.vein_lap_threshold 4.0 -> 2.0 (lower thresh = more pixels
     qualify as veins, so vein_field is denser).

C. NEW CONCAVITY PASS
   - lithology.concavity NEW config block:
       enabled: true
       lap_threshold: 1.5  (positive Laplacian = concave bowl)
       palette_key: bedrock_drainage_palette
   - At runtime, _apply_concavity_drainage paints bedrock_drainage
     palette per-lithology on pixels with strong concavity.  Catches
     gully/bowl/depression features the existing bedrock_drainage MASK
     misses.

D. WASH FREQUENCY + AMP
   - washes.min_flow      0.002 -> 0.0005 (4x more pixels pass flow gate)
   - washes.dilation      2     -> 5     (wider wash channels)
   - washes.fade_blocks   5     -> 8     (gentler outer fade)

E. BEDROCK DRAINAGE FREQUENCY
   - lithology.bedrock_drainage.intensity_threshold 64 -> 24 (~3x more
     pixels qualify in the existing mask).

F. PER-GROUP STRATA THICKNESS (realism-biased)
   "Make bands 8-40 with bias by lithology"
   - granitic              160-320 -> 40-100 (largest, granite plutons
                           are thick-bedded)
   - limestone             40-100  -> 8-20  (karst = thin, well-defined
                           sedimentary layers)
   - deepslate_metamorphic 40-100  -> 16-40 (medium, foliated metamorphic)
   - mossy_temperate       40-100  -> 20-50 (medium-large)
   (arid + temperate basaltic stay XZ_cols; geometry edits below.)

G. BASALTIC COLUMN STRAIGHTNESS
   XZ_cols currently hashes (x//col_size, z//col_size) which creates
   2D PATCHES not stripes.  Change to diagonal-stripe hash so columns
   read as vertical on any cliff face orientation.  This is a CODE
   change in core.surface_decorator; we only set a flag in config so
   the code knows to switch.
   - arid_basaltic     strata.col_hash_mode = "diagonal" (new key)
   - temperate_basaltic strata.col_hash_mode = "diagonal"
   - col_size_blocks 6 -> 5 (slight tightening so diagonal stripes
     visible at typical cliff scale)

(H + I are CODE changes only, no config knobs needed:)
H. Strata-surface paint gated to rock_gap pixels (gap==5) only.
I. NEW cap_edge_stroke pass: 3-5 block inward fade band at rock_zone
   edges, painted with each pixel's lithology cap_palette.

Implemented in core/surface_decorator.py edits, NOT config.
"""
from __future__ import annotations
import json, sys
from pathlib import Path

CFG_PATH = Path("config/thresholds.json")

VEIN_AMP_NEW = {
    "granitic":              0.85,
    "arid_basaltic":         0.50,
    "temperate_basaltic":    1.00,
    "limestone":             0.70,
    "deepslate_metamorphic": 1.00,
    "mossy_temperate":       0.70,
}

STRATA_THICKNESS_NEW = {  # Y_tilted only
    "granitic":              (40, 100),
    "limestone":             ( 8,  20),
    "deepslate_metamorphic": (16,  40),
    "mossy_temperate":       (20,  50),
}


def main() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    g = cfg["lithology"]["groups"]

    # A. Caps INSANELY bigger
    cc = cfg["lithology"].setdefault("cliff_cap", {})
    old_thr = cc.get("intensity_threshold", 32)
    cc["intensity_threshold"] = 8
    cc["dilate_blocks"] = 12  # NEW: runtime dilation
    print(f"  cliff_cap.intensity_threshold {old_thr} -> 8")
    print(f"  cliff_cap.dilate_blocks NEW = 12")

    # B. Vein amp 2x global
    for name, new_amp in VEIN_AMP_NEW.items():
        s = g[name]["strata"]
        old_amp = s.get("vein_amp", 0.0)
        s["vein_amp"] = new_amp
        print(f"  {name}.strata.vein_amp {old_amp} -> {new_amp}")
    strata_global = cfg["lithology"].setdefault("strata", {})
    old_lap = strata_global.get("vein_lap_threshold", 4.0)
    strata_global["vein_lap_threshold"] = 2.0
    print(f"  strata.vein_lap_threshold {old_lap} -> 2.0")

    # C. NEW concavity pass
    cfg["lithology"]["concavity"] = {
        "_comment": (
            "S88 walk #6 NEW PASS: detects concave terrain (positive "
            "Laplacian of surface_y) and paints lithology bedrock_drainage_"
            "palette.  Complements the existing bedrock_drainage mask "
            "painter by catching gully/bowl/depression features the mask "
            "misses (the mask is flow-derived; this is curvature-derived)."
        ),
        "enabled": True,
        "lap_threshold": 1.5,
        "palette_key": "bedrock_drainage_palette",
    }
    print("  lithology.concavity NEW pass added (lap_threshold=1.5)")

    # D. Wash freq + amp
    w = cfg.setdefault("washes", {})
    old_min = w.get("min_flow", 0.002)
    old_dil = w.get("dilation", 2)
    old_fade = w.get("fade_blocks", 5)
    w["min_flow"] = 0.0005
    w["dilation"] = 5
    w["fade_blocks"] = 8
    print(f"  washes.min_flow {old_min} -> 0.0005")
    print(f"  washes.dilation {old_dil} -> 5")
    print(f"  washes.fade_blocks {old_fade} -> 8")

    # E. Bedrock drainage frequency
    bd = cfg["lithology"].setdefault("bedrock_drainage", {})
    old_bd_thr = bd.get("intensity_threshold", 64)
    bd["intensity_threshold"] = 24
    print(f"  bedrock_drainage.intensity_threshold {old_bd_thr} -> 24")

    # F. Per-group strata thickness biases
    for name, (mn, mx) in STRATA_THICKNESS_NEW.items():
        s = g[name]["strata"]
        if s.get("axis") == "Y_tilted":
            old = (s.get("thickness_min"), s.get("thickness_max"))
            s["thickness_min"] = mn
            s["thickness_max"] = mx
            print(f"  {name}.strata thickness {old} -> ({mn},{mx})")

    # G. Basaltic XZ_cols → diagonal-stripe hash + tighter col_size
    for name in ("arid_basaltic", "temperate_basaltic"):
        s = g[name]["strata"]
        if s.get("axis") == "XZ_cols":
            old_size = s.get("col_size_blocks", 6)
            s["col_size_blocks"] = 5
            s["col_hash_mode"] = "diagonal"
            print(f"  {name}.strata col_size {old_size}->5, col_hash_mode=diagonal")

    out = json.dumps(cfg, indent=2, ensure_ascii=True)
    CFG_PATH.write_text(out + "\n", encoding="utf-8")
    print(f"OK: {CFG_PATH} updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
