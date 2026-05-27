"""
apply_s88_walk12.py — S88 walk #12 user-feedback bundle.

Per user direction after walk #11 in-world review.  Per-litho palette
fixes + global vein/varnish frequency dial-back + cap-tree suppression
prep.
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

    # ─── ARID_BASALTIC palette fixes ───────────────────────────────────
    ab = g["arid_basaltic"]
    ab["talus_palette"] = weighted([("packed_mud", 5), ("coarse_dirt", 5)])
    ab["strata"]["vein_blocks"] = ["iron_block", "dripstone_block", "soul_soil"]
    ab["concavity_palette"] = weighted([("tuff", 5), ("pale_moss_block", 5)])
    ab["varnish_palette"] = weighted([("mud", 5), ("blackstone", 5)])
    ab["cap_palette"] = weighted([("tuff", 5), ("deepslate", 5)])  # already was
    print("  arid_basaltic: talus/veins/concavity/varnish/cap reworked")

    # ─── GRANITIC palette fixes ────────────────────────────────────────
    gr = g["granitic"]
    # User: band A/B both = band_b (granite 80% / rooted_dirt 20%)
    band_b_copy = {
        "primary": "granite", "secondary": "rooted_dirt", "primary_pct": 80
    }
    gr["strata"]["band_a"] = dict(band_b_copy)
    gr["strata"]["band_b"] = dict(band_b_copy)
    gr["concavity_palette"] = weighted([("soul_soil", 4), ("soul_sand", 3), ("coarse_dirt", 3)])
    gr["bedrock_drainage_palette"] = weighted([("coarse_dirt", 5), ("dripstone_block", 5)])
    gr["varnish_palette"] = weighted([("andesite", 5), ("stone", 5)])
    print("  granitic: band A = band B, concavity/bedrock/varnish reworked")

    # ─── GLOBAL: vein frequency WAY less common, keep streak size ─────
    sg = cfg["lithology"]["strata"]
    old_thr = sg.get("vein_mask_threshold", 96)
    sg["vein_mask_threshold"] = 192  # was 96 — much stricter (top ~1-2% of mask)
    print(f"  strata.vein_mask_threshold {old_thr} -> 192 (way less common)")
    # Reduce per-litho vein_amp too
    for n, gd in g.items():
        old = gd.get("strata", {}).get("vein_amp", 0.4)
        gd["strata"]["vein_amp"] = 0.18  # was 0.4
        print(f"  {n}.strata.vein_amp {old} -> 0.18")

    # ─── GLOBAL: varnish ONLY at drip-level steep slopes ──────────────
    # User: "1 over, 4 up" = arctan(4/1) = ~76° slopes.  Only sharp cliffs.
    # Lowering dilation so varnish doesn't bleed.
    varn = cfg["lithology"]["varnish"]
    old_amp = varn.get("amp", 0.5)
    old_dil = varn.get("dilate_blocks", 6)
    varn["amp"] = 0.25  # was 0.5 (half coverage)
    varn["dilate_blocks"] = 1  # was 6 (tight)
    # Note: slope_min/max in MASK build, this config tightens runtime amp
    print(f"  varnish.amp {old_amp} -> 0.25 (sparser)")
    print(f"  varnish.dilate_blocks {old_dil} -> 1 (no bleed)")

    # ─── CLIFF_CAP — tree suppression flag ─────────────────────────────
    cc = cfg["lithology"]["cliff_cap"]
    cc["suppress_trees"] = True  # NEW: schematic_placement skips on cap pixels
    cc["kill_ground_cover"] = True  # NEW: zero plants on cap pixels
    print("  cliff_cap.suppress_trees + kill_ground_cover NEW = True")

    out = json.dumps(cfg, indent=2, ensure_ascii=True)
    CFG_PATH.write_text(out + "\n", encoding="utf-8")
    print(f"OK: {CFG_PATH} updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
