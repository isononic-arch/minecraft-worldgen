"""_box_overocean_run.py — on-box: bake + FULL-island render (content tiles + a
1-tile ocean apron) WITH TREES for the over-ocean assessment islands, tint, crop+
merge install to /root/island_regions, topdown each. Run from /root/minecraft-worldgen
in tmux. Writes /root/overocean_done when finished.

Unlike _box_test_run.py (which rendered only 3 scattered diversity tiles per island
-> "only one tile per island"), this renders EVERY content tile plus one ring of
adjacent ocean tiles so the island sits in rendered ocean (the apron), and the
surrounding deep ocean is the fixed vandir:ocean noise filler (seabed ~Y-58, no lava).
"""
import sys, json, math, time
from pathlib import Path
sys.path.insert(0, "/root/minecraft-worldgen")
import rasterio
from rasterio.windows import Window
from concurrent.futures import ProcessPoolExecutor
import run_pipeline
from islands.render_islands import bake_island, safe_name
from islands.render_drive import _tile_args, _content_tiles, MASKS_OUT, OUT
from islands import biome_tint_overlay
from islands.install_islands import crop_merge_install

TARGETS = ["17_288", "13_130", "21_395"]   # new_vincentia, kostati, grand_turk
WORKERS = int(sys.argv[1]) if len(sys.argv) > 1 else 28
APRON = 1                                   # rings of adjacent ocean tiles to also render
lay = json.load(open("islands/layout.json"))["islands"]
entries = [next(x for x in lay if k in x["dem_path"]) for k in TARGETS]


def tiles_with_apron(name, world_w, world_h):
    """content tiles (any land) dilated by APRON rings of adjacent in-bounds tiles."""
    base = set(_content_tiles(MASKS_OUT / name, world_w, world_h))
    ntx = math.ceil(world_w / 512); nty = math.ceil(world_h / 512)
    out = set(base)
    for _ in range(APRON):
        ring = set()
        for (tx, ty) in out:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    nx, ny = tx + dx, ty + dy
                    if 0 <= nx < ntx and 0 <= ny < nty:
                        ring.add((nx, ny))
        out |= ring
    return sorted(out), len(base)


def main():
    t0 = time.time()
    print(f"=== BAKE {len(entries)} islands (parallel) ===", flush=True)
    with ProcessPoolExecutor(max_workers=3) as pool:
        list(pool.map(bake_island, entries))
    print(f"=== BAKE DONE ({(time.time()-t0)/60:.1f}m) ===", flush=True)

    jobs = []
    for e in entries:
        name = safe_name(e["name"])
        man = json.loads((MASKS_OUT / name / "manifest.json").read_text())
        wh, ww = man["world_hw"]
        tiles, ncontent = tiles_with_apron(name, ww, wh)
        print(f"  {name}: {ncontent} content + apron -> {len(tiles)} tiles", flush=True)
        (OUT / name).mkdir(parents=True, exist_ok=True)
        for (tx, ty) in tiles:
            jobs.append(_tile_args(e, MASKS_OUT / name, OUT / name, tx, ty, fast=False))
    print(f"=== RENDER {len(jobs)} tiles WITH TREES ({WORKERS} workers) ===", flush=True)
    done = 0
    with ProcessPoolExecutor(max_workers=WORKERS) as pool:
        for _ in pool.map(run_pipeline._process_tile, jobs):
            done += 1
            if done % 10 == 0 or done == len(jobs):
                print(f"  rendered {done}/{len(jobs)} ({(time.time()-t0)/60:.1f}m)", flush=True)
    print(f"=== RENDER DONE ({(time.time()-t0)/60:.1f}m) ===", flush=True)

    print("=== TINT (jungle grass + warm-ocean on tint-set islands) ===", flush=True)
    sys.argv = ["x"]; biome_tint_overlay.main()
    print("=== CROP + MERGE -> /root/island_regions ===", flush=True)
    crop_merge_install(Path("/root/island_regions"),
                       names=["new_vincentia", "kostati", "grand_turk"], dilate_chunks=4)

    print("=== TOPDOWN each ===", flush=True)
    import subprocess
    for k in ("new_vincentia", "kostati", "grand_turk"):
        try:
            subprocess.run([sys.executable, "islands/topdown_mca.py", "--name", k, "--maxpx", "1400"],
                           cwd="/root/minecraft-worldgen", timeout=900)
        except Exception as ex:
            print(f"  topdown {k} failed: {ex}", flush=True)
    Path("/root/overocean_done").write_text(f"done in {(time.time()-t0)/60:.1f}m\n")
    print(f"DONE_OVEROCEAN ({(time.time()-t0)/60:.1f}m)", flush=True)


if __name__ == "__main__":
    main()
