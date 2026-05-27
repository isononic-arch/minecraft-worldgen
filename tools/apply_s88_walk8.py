"""
apply_s88_walk8.py — S88 walk #8 user-approved bundle.

Per user direction after walk #7 in-world review:

A. CAP RESTORE
   - Walk #7 dialed cap back (threshold 48, dilate 4) on the WRONG diagnosis.
     The real culprit of "cap covering everything" was the cap_edge_stroke
     pass (4-block inward fade band at every rock_gap boundary using
     cap_palette).  Removing that pass in code; restoring cap to walk #6's
     larger proportions:
       cliff_cap.intensity_threshold 48 -> 8
       cliff_cap.dilate_blocks 4 -> 12

B. ROCK VARNISH — NEW PASS
   Per-group varnish_palette finalised below.  Each palette is RELATIVELY
   2-3 shades darker than that group's rock_gap base, in the SAME color
   family.  Detection: rock_gap pixels in local crevices/corners
   (surface_y < gaussian_smoothed surface_y).  Per-pixel coin gated at
   varnish.amp (0.5 default).
"""
from __future__ import annotations
import json, sys
from pathlib import Path

CFG_PATH = Path("config/thresholds.json")


def weighted(pairs: list[tuple[str, int]]) -> list[str]:
    """Expand [('a', 4), ('b', 3)] -> ['a','a','a','a','b','b','b']."""
    out: list[str] = []
    for blk, w in pairs:
        out.extend([blk] * w)
    return out


# User-approved final varnish palettes (per-litho, relative-darkness)
VARNISH = {
    "granitic": weighted([
        ("mud", 4),
        ("muddy_mangrove_roots", 3),
        ("brown_concrete_powder", 3),
    ]),
    "arid_basaltic": weighted([
        ("brown_concrete_powder", 35),
        ("rooted_dirt", 35),
        ("red_sand", 15),
        ("red_sandstone", 15),
    ]),
    "temperate_basaltic": weighted([
        ("coal_block", 4),
        ("black_concrete_powder", 3),
        ("tuff", 3),
    ]),
    "limestone": weighted([
        ("tuff", 4),
        ("andesite", 3),
        ("stone", 3),
    ]),
    "deepslate_metamorphic": weighted([
        ("basalt", 1),
        ("smooth_basalt", 1),
    ]),
    "mossy_temperate": weighted([
        ("moss_block", 1),
    ]),
}


def main() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    g = cfg["lithology"]["groups"]

    # A. Cap restore to walk #6 values
    cc = cfg["lithology"].setdefault("cliff_cap", {})
    old_thr = cc.get("intensity_threshold", 48)
    old_dil = cc.get("dilate_blocks", 4)
    cc["intensity_threshold"] = 8
    cc["dilate_blocks"] = 12
    print(f"  cliff_cap.intensity_threshold {old_thr} -> 8 (walk #6 value restored)")
    print(f"  cliff_cap.dilate_blocks {old_dil} -> 12 (walk #6 value restored)")

    # B. Add varnish_palette per group
    for gname, pal in VARNISH.items():
        if gname not in g:
            print(f"  WARNING: {gname} not in lithology.groups; skipping")
            continue
        g[gname]["varnish_palette"] = pal
        # Show compact summary
        from collections import Counter
        c = Counter(pal)
        total = len(pal)
        compact = ", ".join(f"{blk}{round(cnt/total*100)}%" for blk, cnt in c.most_common())
        print(f"  {gname}.varnish_palette = {compact}")

    # Add global varnish config block
    cfg["lithology"]["varnish"] = {
        "_comment": (
            "S88 walk #8: NEW rock varnish pass.  Paints per-litho "
            "varnish_palette on rock_gap pixels in local crevices/corners "
            "(surface_y < gaussian_smoothed surface_y).  Per-pixel coin "
            "gated at amp.  Each varnish_palette is relatively 2-3 shades "
            "darker than that group's rock_gap base, in the same color "
            "family — reads as natural rock staining/oxidation."
        ),
        "enabled": True,
        "amp": 0.5,
        "crevice_sigma": 2.0,
        "crevice_threshold": 0.5,
    }
    print(f"  lithology.varnish NEW config (enabled, amp=0.5)")

    out = json.dumps(cfg, indent=2, ensure_ascii=True)
    CFG_PATH.write_text(out + "\n", encoding="utf-8")
    print(f"OK: {CFG_PATH} updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
