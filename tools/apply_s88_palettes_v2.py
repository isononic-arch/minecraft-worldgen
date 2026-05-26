"""
apply_s88_palettes_v2.py — second-pass palette rewrite for all 6 lithology
groups across rock_gap, wash, cap, talus, bedrock_drainage.

Per user walk-feedback after first S88 wired render.  Each group's full
5-palette set is replaced atomically.  Weights inferred from listed order:
  2 blocks -> 50/50 (5x/5x)
  3 blocks -> 50/30/20 (5x/3x/2x) -- first listed is primary

Run once.  Updates config/thresholds.json in place (ensure_ascii=True so
the diff is clean).
"""
from __future__ import annotations
import json, sys
from pathlib import Path

CFG_PATH = Path("config/thresholds.json")


def _pal(*blocks: str) -> list[str]:
    """Build a 10-entry palette from listed blocks by inferred weights:
       2 -> 5,5  |  3 -> 5,3,2  |  4 -> 4,3,2,1"""
    n = len(blocks)
    if n == 2:
        weights = [5, 5]
    elif n == 3:
        weights = [5, 3, 2]
    elif n == 4:
        weights = [4, 3, 2, 1]
    else:
        # 1, 5+ -> equal split
        per = max(1, 10 // max(n, 1))
        weights = [per] * n
    out: list[str] = []
    for blk, w in zip(blocks, weights):
        out.extend([blk] * w)
    return out


NEW_PALETTES: dict[str, dict[str, list[str]]] = {
    "granitic": {
        "palette":                  _pal("granite", "rooted_dirt"),
        "wash_palette":             _pal("dripstone_block", "packed_mud"),
        "cap_palette":              _pal("dripstone_block", "raw_iron_block", "granite"),
        "talus_palette":            _pal("coarse_dirt", "brown_concrete_powder", "rooted_dirt"),
        "bedrock_drainage_palette": _pal("soul_soil", "mud"),
    },
    "arid_basaltic": {
        "palette":                  _pal("basalt", "smooth_basalt"),
        "wash_palette":             _pal("gravel", "clay"),
        "cap_palette":              _pal("tuff", "deepslate"),
        "talus_palette":            _pal("clay", "dead_fire_coral_block"),
        "bedrock_drainage_palette": _pal("tuff", "pale_moss_block"),
    },
    "temperate_basaltic": {
        "palette":                  _pal("deepslate", "cobbled_deepslate", "deepslate_coal_ore"),
        "wash_palette":             _pal("light_gray_concrete_powder", "gray_concrete_powder"),
        "cap_palette":              _pal("blackstone", "black_concrete_powder"),
        "talus_palette":            _pal("gray_concrete_powder", "gravel"),
        "bedrock_drainage_palette": _pal("andesite", "tuff"),
    },
    "limestone": {
        "palette":                  _pal("calcite", "diorite"),
        "wash_palette":             _pal("light_gray_concrete_powder", "gravel"),
        "cap_palette":              _pal("dead_horn_coral_block", "andesite"),
        "talus_palette":            _pal("gravel", "light_gray_concrete_powder", "white_concrete_powder"),
        "bedrock_drainage_palette": _pal("andesite", "clay"),
    },
    "deepslate_metamorphic": {
        "palette":                  _pal("andesite", "stone"),
        "wash_palette":             _pal("clay", "light_gray_concrete_powder"),
        "cap_palette":              _pal("dead_fire_coral_block", "dead_horn_coral_block"),
        "talus_palette":            _pal("suspicious_gravel", "gravel"),
        "bedrock_drainage_palette": _pal("tuff", "dead_bubble_coral_block"),
    },
    "mossy_temperate": {
        "palette":                  _pal("cobblestone", "mossy_cobblestone"),
        "wash_palette":             _pal("moss_block", "brown_concrete_powder"),
        "cap_palette":              _pal("tuff", "pale_moss_block"),
        "talus_palette":            _pal("mud", "brown_concrete_powder", "packed_mud"),
        "bedrock_drainage_palette": _pal("mud", "soul_soil", "moss_block"),
    },
}


def main() -> int:
    if not CFG_PATH.exists():
        print(f"ERROR: {CFG_PATH} not found", file=sys.stderr)
        return 2
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    groups = cfg.setdefault("lithology", {}).setdefault("groups", {})

    for gname, paldict in NEW_PALETTES.items():
        if gname not in groups:
            print(f"  WARN: group {gname} not in config — skipping", file=sys.stderr)
            continue
        for pkey, plist in paldict.items():
            old = groups[gname].get(pkey, [])
            groups[gname][pkey] = plist
            print(f"  {gname}.{pkey}: {len(old)} entries -> {len(plist)} entries")

    out = json.dumps(cfg, indent=2, ensure_ascii=True)
    CFG_PATH.write_text(out + "\n", encoding="utf-8")
    print(f"OK: {CFG_PATH} updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
