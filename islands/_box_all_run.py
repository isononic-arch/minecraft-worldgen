"""_box_all_run.py — on-box: bake + render a SUBSET of islands (keys from argv),
content + 1-buffer-ring tiles WITH TREES, pooled across the subset for full core
saturation. Per-tile failures are caught + logged (one bad tile never kills the
run). Writes /root/all_done when finished. Mirrors _box_geofix_run.py.

Usage (in tmux from /root/minecraft-worldgen):
    /root/venv/bin/python islands/_box_all_run.py <WORKERS> <KEY1,KEY2,...> [renderonly]
e.g. /root/venv/bin/python islands/_box_all_run.py 44 17_288,13_130 renderonly
"""
import os, sys, json, time
os.environ.setdefault("VANDIR_ISLAND_RENDER", "1")  # S98: island-only schematic filters (apine out of maquis)
from pathlib import Path
sys.path.insert(0, "/root/minecraft-worldgen")
from concurrent.futures import ProcessPoolExecutor, as_completed
import run_pipeline
from islands.render_islands import bake_island, safe_name
from islands.render_drive import _tile_args, _content_tiles, MASKS_OUT, OUT

WORKERS = int(sys.argv[1]) if len(sys.argv) > 1 else 44
KEYS = [k for k in sys.argv[2].split(",") if k] if len(sys.argv) > 2 else []
RENDER_ONLY = len(sys.argv) > 3 and sys.argv[3].lower().startswith("render")
BAKE_CONC = min(len(KEYS), 8) if KEYS else 8
BUFFER = 1
FAST = False  # trees ON (production)
lay = json.load(open("/root/minecraft-worldgen/islands/layout.json"))["islands"]
entries = [next(x for x in lay if k in x["dem_path"]) for k in KEYS]


def main():
    t0 = time.time()
    if not RENDER_ONLY:
        print(f"=== BAKE {len(entries)} islands ({BAKE_CONC} concurrent): {KEYS} ===", flush=True)
        with ProcessPoolExecutor(max_workers=BAKE_CONC) as pool:
            for k, _m in zip(KEYS, pool.map(bake_island, entries)):
                print(f"  baked {k} ({(time.time()-t0)/60:.1f}m)", flush=True)
        print(f"=== BAKE DONE ({(time.time()-t0)/60:.1f}m) ===", flush=True)
    else:
        print(f"=== RENDER-ONLY (skip bake), using existing masks_islands ===", flush=True)

    jobs = []
    for e in entries:
        name = safe_name(e["name"])
        man = json.loads((MASKS_OUT / name / "manifest.json").read_text())
        wh, ww = man["world_hw"]
        tiles = _content_tiles(MASKS_OUT / name, ww, wh, buffer_tiles=BUFFER)
        print(f"  {name}: {len(tiles)} tiles (land + {BUFFER}-buffer)", flush=True)
        (OUT / name).mkdir(parents=True, exist_ok=True)
        for f in (OUT / name).glob("r.*.mca"):
            f.unlink()
        for (tx, ty) in tiles:
            jobs.append(_tile_args(e, MASKS_OUT / name, OUT / name, tx, ty, fast=FAST))

    print(f"=== RENDER {len(jobs)} tiles WITH TREES ({WORKERS}w) ===", flush=True)
    done = 0; failed = 0
    with ProcessPoolExecutor(max_workers=WORKERS) as pool:
        futs = {pool.submit(run_pipeline._process_tile, a): (a["tile_x"], a["tile_y"]) for a in jobs}
        for f in as_completed(futs):
            done += 1
            try:
                f.result()
            except Exception as e:
                failed += 1
                print(f"  TILE {futs[f]} FAILED: {type(e).__name__}: {e}", flush=True)
            if done % 20 == 0 or done == len(futs):
                print(f"  {done}/{len(futs)} ({failed} failed, {(time.time()-t0)/60:.1f}m)", flush=True)
    Path("/root/all_done").write_text(f"done {(time.time()-t0)/60:.1f}m, {len(jobs)} tiles, {failed} failed\n")
    print(f"ALL_DONE ({(time.time()-t0)/60:.1f}m, {len(jobs)} tiles, {failed} failed)", flush=True)


if __name__ == "__main__":
    main()
