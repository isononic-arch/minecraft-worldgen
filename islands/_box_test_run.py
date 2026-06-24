"""_box_test_run.py — on-box: bake 3 diverse test islands (parallel), auto-pick
test tiles (peak elevation / most biome zones / coastline), render them WITH trees
(parallel), apply the jungle/warm-ocean tint to the tint-set islands, crop+merge to
/root/island_regions. Run from /root/minecraft-worldgen in tmux."""
import sys, json, math
from pathlib import Path
sys.path.insert(0, "/root/minecraft-worldgen")
import rasterio
from rasterio.windows import Window
from concurrent.futures import ProcessPoolExecutor
import run_pipeline
from islands.render_islands import bake_island, safe_name
from islands.render_drive import _tile_args, MASKS_OUT, OUT
from islands import biome_tint_overlay
from islands.install_islands import crop_merge_install

TESTS = ["13_130", "-50_393", "23_887"]   # Kostati (tropical), Madre (cut/karst/cull), Bahamas (flat coral)
lay = json.load(open("islands/layout.json"))["islands"]
entries = [next(x for x in lay if k in x["dem_path"]) for k in TESTS]


def pick_tiles(name, n=3):
    d = MASKS_OUT / name
    with rasterio.open(d / "override.tif") as ov, rasterio.open(d / "height.tif") as h:
        H, W = ov.height, ov.width; T = 512; tiles = []
        for ty in range(math.ceil(H / T)):
            for tx in range(math.ceil(W / T)):
                w = min(T, W - tx * T); hh = min(T, H - ty * T)
                if w <= 0 or hh <= 0:
                    continue
                ht = h.read(1, window=Window(tx * T, ty * T, w, hh))
                land = ht > 17050; lf = float(land.mean())
                if lf < 0.03:
                    continue
                o = ov.read(1, window=Window(tx * T, ty * T, w, hh))
                tiles.append((tx, ty, lf, len(set(o[land].tolist())), int(ht.max())))
    if not tiles:
        return []
    picks = {}
    picks[tuple(max(tiles, key=lambda t: t[4])[:2])] = 1     # peak elevation
    picks[tuple(max(tiles, key=lambda t: t[3])[:2])] = 1     # most biome zones (transition)
    picks[tuple(min(tiles, key=lambda t: abs(t[2] - 0.5))[:2])] = 1   # coastline (~50% land)
    return list(picks)[:n]


def main():
    print("=== BAKE 3 islands (parallel) ===", flush=True)
    with ProcessPoolExecutor(max_workers=3) as pool:
        list(pool.map(bake_island, entries))
    print("=== BAKE DONE ===", flush=True)

    jobs = []
    for e in entries:
        name = safe_name(e["name"]); tiles = pick_tiles(name)
        print(f"  {name}: test tiles {tiles}", flush=True)
        (OUT / name).mkdir(parents=True, exist_ok=True)
        for (tx, ty) in tiles:
            jobs.append(_tile_args(e, MASKS_OUT / name, OUT / name, tx, ty, fast=False))
    print(f"=== RENDER {len(jobs)} tiles WITH TREES (parallel) ===", flush=True)
    with ProcessPoolExecutor(max_workers=min(len(jobs), 12)) as pool:
        for i, _ in enumerate(pool.map(run_pipeline._process_tile, jobs)):
            print(f"  rendered {i + 1}/{len(jobs)}", flush=True)
    print("=== RENDER DONE ===", flush=True)

    print("=== TINT (jungle grass + warm-ocean on tint-set: kostati, bahamas) ===", flush=True)
    sys.argv = ["x"]; biome_tint_overlay.main()
    print("=== CROP + MERGE -> /root/island_regions ===", flush=True)
    crop_merge_install(Path("/root/island_regions"), names=["kostati", "madre", "bahamas"], dilate_chunks=4)
    print("DONE_BOX_RUN", flush=True)


if __name__ == "__main__":
    main()
