"""_box_render_only.py — on-box RE-render (no re-bake) of the 3 over-ocean islands
WITH TREES using the placement-offset fix. Content tiles only (no apron) to stay fast.
Clears old treeless MCAs first. Tints, crop+merge -> /root/island_regions, topdown each.
Writes /root/render_only_done. Run from /root/minecraft-worldgen in tmux.
"""
import sys, json, time
from pathlib import Path
sys.path.insert(0, "/root/minecraft-worldgen")
from concurrent.futures import ProcessPoolExecutor
import run_pipeline
from islands.render_islands import safe_name
from islands.render_drive import _tile_args, _content_tiles, MASKS_OUT, OUT
from islands import biome_tint_overlay
from islands.install_islands import crop_merge_install

TARGETS = ["17_288", "13_130", "21_395"]
WORKERS = int(sys.argv[1]) if len(sys.argv) > 1 else 36
lay = json.load(open("islands/layout.json"))["islands"]
entries = [next(x for x in lay if k in x["dem_path"]) for k in TARGETS]


def main():
    t0 = time.time()
    jobs = []
    for e in entries:
        name = safe_name(e["name"])
        man = json.loads((MASKS_OUT / name / "manifest.json").read_text())
        wh, ww = man["world_hw"]
        tiles = _content_tiles(MASKS_OUT / name, ww, wh)
        print(f"  {name}: {len(tiles)} content tiles", flush=True)
        (OUT / name).mkdir(parents=True, exist_ok=True)
        for f in (OUT / name).glob("r.*.mca"):
            f.unlink()                       # drop old treeless tiles
        for (tx, ty) in tiles:
            jobs.append(_tile_args(e, MASKS_OUT / name, OUT / name, tx, ty, fast=False))
    print(f"=== RENDER {len(jobs)} content tiles WITH TREES ({WORKERS}w) ===", flush=True)
    done = 0
    with ProcessPoolExecutor(max_workers=WORKERS) as pool:
        for _ in pool.map(run_pipeline._process_tile, jobs):
            done += 1
            if done % 10 == 0 or done == len(jobs):
                print(f"  {done}/{len(jobs)} ({(time.time()-t0)/60:.1f}m)", flush=True)
    print("=== TINT + CROP + TOPDOWN ===", flush=True)
    sys.argv = ["x"]; biome_tint_overlay.main()
    crop_merge_install(Path("/root/island_regions"),
                       names=["new_vincentia", "kostati", "grand_turk"], dilate_chunks=4)
    import subprocess
    for k in ("new_vincentia", "kostati", "grand_turk"):
        try:
            subprocess.run([sys.executable, "islands/topdown_mca.py", "--name", k, "--maxpx", "1400"],
                           cwd="/root/minecraft-worldgen", timeout=900)
        except Exception as ex:
            print(f"  topdown {k} failed: {ex}", flush=True)
    Path("/root/render_only_done").write_text(f"done {(time.time()-t0)/60:.1f}m\n")
    print(f"DONE_RENDER_ONLY ({(time.time()-t0)/60:.1f}m)", flush=True)


if __name__ == "__main__":
    main()
