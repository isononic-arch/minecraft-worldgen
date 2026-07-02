"""_gen_region_ownership_s101.py — per-region OWNERSHIP MANIFEST + mainland
render SKIP-LIST from the baked island masks.

Enumerates every island's render footprint with THE renderer's own rule
(imports islands.render_drive._content_tiles — land >SEA_RAW+40 per 512 window,
per-island apron_seed_min_px cull, +1 buffer ring, offset snapped to 512) and
maps tiles to world region coords. Outputs:

  islands/region_ownership_s101.json
      {"islands": {name: [[rx,rz],...]}, "mainland_collisions": [[rx,rz],...],
       "island_vs_island": {"a|b": [[rx,rz],...]}}
  cloud_bake/mainland_skip_regions_s101.txt   ("rx rz" per line, sorted) —
      regions inside the mainland 0..96 range owned by an island render: the
      mainland 50k render can SKIP these (island tiles replace them at install;
      skipping also removes the install-ordering clobber hazard).

Run AFTER any bake/offset change; commit the outputs alongside layout.json.
"""
import json
import sys
from pathlib import Path

ISL = Path(__file__).resolve().parent
ROOT = ISL.parent
sys.path.insert(0, str(ROOT))

from islands.render_drive import _content_tiles, _snap, TILE  # noqa: E402
from islands.render_islands import safe_name                  # noqa: E402

MAINLAND_R = range(0, 97)   # 97x97 tiles = regions 0..96 both axes


def main():
    layout = json.loads((ISL / "layout.json").read_text())["islands"]
    ownership: dict[str, list[list[int]]] = {}
    region_owner: dict[tuple[int, int], list[str]] = {}

    for e in layout:
        name = safe_name(e["name"])
        mdir = ISL / "masks_islands" / name
        man = json.loads((mdir / "manifest.json").read_text())
        wh, ww = man["world_hw"]
        ox, oz = _snap(e["world_offset_px"][0]), _snap(e["world_offset_px"][1])
        seed_min = int(e.get("apron_seed_min_px", 0))
        tiles = _content_tiles(mdir, ww, wh, buffer_tiles=1, apron_seed_min_px=seed_min)
        regs = sorted({(ox // TILE + tx, oz // TILE + ty) for tx, ty in tiles})
        ownership[name] = [list(r) for r in regs]
        for r in regs:
            region_owner.setdefault(r, []).append(name)
        print(f"  {name:40} {len(tiles):4} tiles  apron_min={seed_min}")

    collisions = sorted(r for r in region_owner
                        if r[0] in MAINLAND_R and r[1] in MAINLAND_R)
    ivi = {}
    for r, owners in region_owner.items():
        if len(owners) > 1:
            ivi.setdefault("|".join(sorted(owners)), []).append(list(r))
    for k in ivi:
        ivi[k].sort()

    out = {"islands": ownership,
           "mainland_collisions": [list(r) for r in collisions],
           "island_vs_island": ivi}
    (ISL / "region_ownership_s101.json").write_text(json.dumps(out, indent=1))

    skip = ROOT / "cloud_bake" / "mainland_skip_regions_s101.txt"
    skip.write_text("".join(f"{rx} {rz}\n" for rx, rz in collisions))

    print(f"\nregions total: {sum(len(v) for v in ownership.values())}")
    print(f"mainland-range collisions (skip-list): {len(collisions)}")
    for pair, regs in sorted(ivi.items()):
        print(f"island-vs-island {pair}: {len(regs)} region(s)")
    print(f"\nwrote islands/region_ownership_s101.json + {skip.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
