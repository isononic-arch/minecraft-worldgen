"""render_drive.py — drive run_pipeline._process_tile over an island's content
tiles, reading masks LOCALLY and writing blocks at the world offset."""
from __future__ import annotations
import json, sys, time, math
from pathlib import Path
import numpy as np
import rasterio

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
ISL = ROOT / "islands"
MASKS_OUT = ISL / "masks_islands"
OUT = ISL / "out"
SEA_RAW = 17050
SCHEM_INDEX = ROOT / "schematic_index.json"
TILE = 512


def _safe(name):
    import re
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _content_tiles(mdir: Path, world_w: int, world_h: int):
    """Tiles whose local window contains any land (height>sea). Skip all-ocean."""
    tiles = []
    hpath = mdir / "height.tif"
    with rasterio.open(str(hpath)) as src:
        ntx = math.ceil(world_w / TILE); nty = math.ceil(world_h / TILE)
        for ty in range(nty):
            for tx in range(ntx):
                from rasterio.windows import Window
                w = min(TILE, world_w - tx*TILE); h = min(TILE, world_h - ty*TILE)
                if w <= 0 or h <= 0:
                    continue
                a = src.read(1, window=Window(tx*TILE, ty*TILE, w, h))
                if a.size and (a > SEA_RAW + 40).any():
                    tiles.append((tx, ty))
    return tiles


def _snap(v):
    return int(round(v / TILE) * TILE)   # snap offset to region (512) boundary

def _tile_args(entry, mdir, odir, tx, ty, fast=False):
    ox, oz = _snap(entry["world_offset_px"][0]), _snap(entry["world_offset_px"][1])
    # fast mode: point at a missing index -> load_index falls back to {} -> no
    # schematics (halves time + memory on a 7.4GB box; trees are a follow-up).
    schem = str(ISL / "_no_schem.json") if fast else str(SCHEM_INDEX)
    return dict(tile_x=tx, tile_y=ty,
                config_path=str(mdir / "thresholds_island.json"),
                masks_dir=str(mdir), output_dir=str(odir),
                tile_size=TILE, dry_run=False,
                schem_index_path=schem,
                world_offset_x=int(ox), world_offset_z=int(oz))


def render_island(entry, threads=2, only_tile=None, fast=True):
    import run_pipeline
    # fast mode relies on ISL/_no_schem.json being ABSENT (load_index raises -> {}).
    p = ISL / "_no_schem.json"
    if fast and p.exists():
        p.unlink()
    name = _safe(entry["name"])
    mdir = MASKS_OUT / name
    man = json.loads((mdir / "manifest.json").read_text())
    world_h, world_w = man["world_hw"]
    odir = OUT / name; odir.mkdir(parents=True, exist_ok=True)
    tiles = [only_tile] if only_tile else _content_tiles(mdir, world_w, world_h)
    print(f"[render] {name}: {len(tiles)} content tiles, offset={entry['world_offset_px']} fast={fast} threads={threads}", flush=True)
    t0 = time.time()
    if only_tile or threads <= 1:
        for t in tiles:
            r = run_pipeline._process_tile(_tile_args(entry, mdir, odir, *t, fast=fast))
            print(f"  tile {t} -> {r.get('elapsed_ms')}ms biomes={len(r.get('biomes',[]))}", flush=True)
    else:
        from concurrent.futures import ProcessPoolExecutor, as_completed
        args = [_tile_args(entry, mdir, odir, *t, fast=fast) for t in tiles]
        done = 0
        with ProcessPoolExecutor(max_workers=threads) as pool:
            futs = {pool.submit(run_pipeline._process_tile, a): (a["tile_x"], a["tile_y"]) for a in args}
            for f in as_completed(futs):
                done += 1
                try:
                    f.result()
                except Exception as e:
                    print(f"  tile {futs[f]} FAILED: {e}", flush=True)
                if done % 10 == 0 or done == len(futs):
                    print(f"  {done}/{len(futs)} tiles ({(time.time()-t0)/60:.1f}m)", flush=True)
    n_mca = len(list(odir.glob("r.*.mca")))
    print(f"[render] {name} done in {(time.time()-t0)/60:.1f}m -> {n_mca} region files in {odir}", flush=True)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--tile", help="tx,ty single tile for debug")
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--trees", action="store_true", help="enable schematic vegetation (slower, more memory)")
    a = ap.parse_args()
    layout = json.loads((ISL / "layout.json").read_text())
    entry = next(i for i in layout["islands"] if a.name in _safe(i["name"]) or a.name in i["dem_path"])
    only = tuple(int(x) for x in a.tile.split(",")) if a.tile else None
    render_island(entry, threads=a.threads, only_tile=only, fast=not a.trees)
