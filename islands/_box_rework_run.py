"""_box_rework_run.py — on-box: RE-BAKE the 3 islands (so the new wide shelf + wider
beach land in the masks), then render land + shelf-buffer tiles (no crop) FAST (no
schematics — this pass verifies shelf/beach/no-lava/full-island; trees are proven).
Writes /root/rework_done. Run from /root/minecraft-worldgen in tmux.
"""
import sys, json, time
from pathlib import Path
sys.path.insert(0, "/root/minecraft-worldgen")
from concurrent.futures import ProcessPoolExecutor
import run_pipeline
from islands.render_islands import bake_island, safe_name
from islands.render_drive import _tile_args, _content_tiles, MASKS_OUT, OUT

TARGETS = ["17_288", "13_130", "21_395"]      # new_vincentia, kostati, grand_turk
WORKERS = int(sys.argv[1]) if len(sys.argv) > 1 else 40
BUFFER = 1                                     # shelf-buffer ring (captures the ~380-block shelf)
FAST = True                                    # no schematics this verify pass
lay = json.load(open("/root/minecraft-worldgen/islands/layout.json"))["islands"]
entries = [next(x for x in lay if k in x["dem_path"]) for k in TARGETS]


def main():
    t0 = time.time()
    print("=== RE-BAKE 3 islands (new wide shelf + wider beach) ===", flush=True)
    with ProcessPoolExecutor(max_workers=3) as pool:
        list(pool.map(bake_island, entries))
    print(f"=== BAKE DONE ({(time.time()-t0)/60:.1f}m) ===", flush=True)

    jobs = []
    for e in entries:
        name = safe_name(e["name"])
        man = json.loads((MASKS_OUT / name / "manifest.json").read_text())
        wh, ww = man["world_hw"]
        tiles = _content_tiles(MASKS_OUT / name, ww, wh, buffer_tiles=BUFFER)
        print(f"  {name}: {len(tiles)} tiles (land + {BUFFER}-tile shelf buffer)", flush=True)
        (OUT / name).mkdir(parents=True, exist_ok=True)
        for f in (OUT / name).glob("r.*.mca"):
            f.unlink()
        for (tx, ty) in tiles:
            jobs.append(_tile_args(e, MASKS_OUT / name, OUT / name, tx, ty, fast=FAST))
    print(f"=== RENDER {len(jobs)} tiles ({'fast' if FAST else 'trees'}, {WORKERS}w) ===", flush=True)
    done = 0
    with ProcessPoolExecutor(max_workers=WORKERS) as pool:
        for _ in pool.map(run_pipeline._process_tile, jobs):
            done += 1
            if done % 15 == 0 or done == len(jobs):
                print(f"  {done}/{len(jobs)} ({(time.time()-t0)/60:.1f}m)", flush=True)
    Path("/root/rework_done").write_text(f"done {(time.time()-t0)/60:.1f}m, {len(jobs)} tiles\n")
    print(f"DONE_REWORK ({(time.time()-t0)/60:.1f}m)", flush=True)


if __name__ == "__main__":
    main()
