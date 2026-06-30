"""_patch_island_cfg.py — apply the S98 island-polish config deltas (apply_island_polish:
de-coral + soften talus + jungle/mangrove mud-swap + max veg) to every already-baked
islands/masks_islands/<isl>/thresholds_island.json IN PLACE, so the fixes take effect on a
RENDER-ONLY pass (no re-bake). bake_island also applies it for fresh bakes; this just brings
the existing baked configs up to date. Idempotent.

Usage: py islands/_patch_island_cfg.py [--names a,b]   (default: all islands)
"""
import sys, json, argparse
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from islands.render_islands import apply_island_polish, safe_name
MASKS_OUT = ROOT / "islands" / "masks_islands"


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--names", default=""); a = ap.parse_args()
    names = [n.strip() for n in a.names.split(",") if n.strip()] or None
    n = 0
    for d in sorted(MASKS_OUT.iterdir()):
        cfgp = d / "thresholds_island.json"
        if not cfgp.exists():
            continue
        if names and not any(nm in d.name for nm in names):
            continue
        cfg = json.loads(cfgp.read_text())
        apply_island_polish(cfg)
        cfgp.write_text(json.dumps(cfg))
        # quick assertion: no dead coral left in any group palette
        coral = sum(str(g).count("coral") for g in cfg.get("lithology", {}).get("groups", {}).values())
        print(f"  patched {d.name:42s} (coral-refs now={coral})")
        n += 1
    print(f"patched {n} island configs")


if __name__ == "__main__":
    main()
