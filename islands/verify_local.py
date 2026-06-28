"""verify_local.py — FREE, FAST island sanity gate (no cloud, no full render).

The S97 archipelago flood (apply_flow_erosion's whole-array sea clamp lifting the
inter-island OCEAN to Y64 = land) was invisible to mask-level checks: the bake
masks were organic and correct; the bug only appeared after the render's
geology/hydrology stages. A full render is ~10-15 min PER TILE here, so it's a
terrible iteration gate. This tool runs only the CHEAP surface_y stages that
actually exercise the bug — generate_columns -> carve_rivers -> apply_flow_erosion
— per content tile (seconds each, no decorate, no chunk write), and flags any tile
whose rendered LAND fraction blows past the mask's true land fraction (a flood).

It is the gate to run BEFORE ever spending a Hetzner box: if verify_local flags a
flood, you never needed the render.

Usage:
    py islands/verify_local.py --name grenada            # bake + audit all content tiles
    py islands/verify_local.py --name kostati --no-bake  # audit existing masks
    py islands/verify_local.py --name new_vincentia --sample 25
"""
from __future__ import annotations
import sys, json, time, argparse, re
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
ISL = ROOT / "islands"
MASKS_OUT = ISL / "masks_islands"
TILE = 512
SEA_RAW = 17050


def _safe(n):
    return re.sub(r"[^a-z0-9]+", "_", n.lower()).strip("_")


def _resolve_entry(token: str):
    """Match an island by DEM token (e.g. 12_445) or unique safe-name fragment.
    NOTE: name substrings are ambiguous ('grenad' hits BOTH Grenada and Kostati's
    'Grenadines') -> prefer the DEM token or a safe_name prefix."""
    lay = json.loads((ISL / "layout.json").read_text())["islands"]
    hits = [i for i in lay if token in i["dem_path"] or token in _safe(i["name"])]
    if len(hits) != 1:
        names = [f"{_safe(i['name'])} ({Path(i['dem_path']).name})" for i in hits]
        raise SystemExit(f"token {token!r} matched {len(hits)} islands: {names}\n"
                         f"  -> disambiguate with a DEM token (e.g. 12_445) or full safe_name")
    return hits[0]


def audit_tile(mdir: Path, cfg: dict, tx: int, ty: int):
    """Run the cheap surface_y stages on one tile; return (mask_land, rendered_land).
    rendered_land = land fraction AFTER flow_erosion (the stage that floods) — a
    faithful proxy for the final render's land/ocean split (decorate barely shifts
    it). No decorate, no schematics, no chunk write."""
    import core.tile_streamer as ts
    from core.column_generator import generate_columns, SEA_LEVEL
    import core.river_carver_v2 as rc
    import core.flow_erosion as fe
    co, ro = tx * TILE, ty * TILE
    m = ts.read_tile(masks_dir=str(mdir), col_off=co, row_off=ro,
                     width=TILE, height=TILE, gap_config=None)
    hu = np.round(m["height"] * 65535.0).astype(np.uint16)
    mask_land = float((hu > (SEA_RAW + 40)).mean())
    bg = np.full(hu.shape, "_OCEAN", dtype=object)
    sy = generate_columns(height_tile=hu, slope_tile=m["slope"], biome_grid=bg,
                          shore_tile=m.get("shore"), noise_fields=None, cfg=cfg,
                          tile_x=tx, tile_y=ty)
    g = m.get
    out = rc.carve_rivers(
        surface_y=sy, flow_tile=m.get("flow"), river_tile=m.get("river"), cfg=cfg,
        hydro_order=g("hydro_order"), hydro_width=g("hydro_width"), hydro_depth=g("hydro_depth"),
        hydro_lake=g("hydro_lake"), hydro_lkdep=g("hydro_lkdep"), hydro_lake_wl=g("hydro_lake_wl"),
        hydro_centerline=g("hydro_centerline"), height_norm=m["height"],
        masks_dir=str(mdir), tile_x=tx, tile_z=ty)
    sy2 = out[0] if isinstance(out, tuple) else out
    rmeta = out[1] if isinstance(out, tuple) and len(out) > 1 else None
    sy3 = fe.apply_flow_erosion(sy2, m.get("flow"), m.get("rock_layers"), rmeta, cfg, tx, ty)
    rendered_land = float((sy3 > SEA_LEVEL).mean())
    return mask_land, rendered_land


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help="DEM token (e.g. 12_445) or unique safe_name")
    ap.add_argument("--no-bake", action="store_true", help="audit existing masks (skip re-bake)")
    ap.add_argument("--sample", type=int, default=0, help="audit only N evenly-spaced content tiles (0=all)")
    ap.add_argument("--flood-thresh", type=float, default=0.15,
                    help="flag a tile if rendered_land - mask_land exceeds this")
    a = ap.parse_args()
    t0 = time.time()
    entry = _resolve_entry(a.name)
    name = _safe(entry["name"])
    print(f"[verify_local] {name}", flush=True)

    if not a.no_bake:
        from islands.render_islands import bake_island
        print("[verify_local] baking...", flush=True)
        bake_island(entry)
        print(f"[verify_local] baked ({(time.time()-t0)/60:.1f}m)", flush=True)

    mdir = MASKS_OUT / name
    cfg = json.loads((mdir / "thresholds_island.json").read_text())
    flag = cfg.get("flow_erosion", {}).get("rock_only_sea_clamp")
    print(f"[verify_local] flow_erosion.rock_only_sea_clamp = {flag}", flush=True)
    from islands.render_drive import _content_tiles
    man = json.loads((mdir / "manifest.json").read_text())
    wh, ww = man["world_hw"]
    tiles = _content_tiles(mdir, ww, wh, buffer_tiles=0)   # land tiles only (where the bug bites)
    if a.sample and a.sample < len(tiles):
        idx = np.linspace(0, len(tiles) - 1, a.sample).astype(int)
        tiles = [tiles[i] for i in idx]
    print(f"[verify_local] auditing {len(tiles)} land tiles...", flush=True)

    floods = []
    worst = (0.0, None)
    for (tx, ty) in tiles:
        try:
            ml, rl = audit_tile(mdir, cfg, tx, ty)
        except Exception as e:
            print(f"  tile ({tx},{ty}) ERROR {type(e).__name__}: {e}", flush=True)
            continue
        excess = rl - ml
        if excess > worst[0]:
            worst = (excess, (tx, ty, ml, rl))
        if excess > a.flood_thresh:
            floods.append((tx, ty, ml, rl, excess))

    print("\n=== verify_local: %s ===" % name)
    print("flow_erosion.rock_only_sea_clamp = %s" % flag)
    print("land tiles audited: %d   flooded (excess>%.2f): %d" % (len(tiles), a.flood_thresh, len(floods)))
    if worst[1]:
        tx, ty, ml, rl = worst[1]
        print("worst tile (%d,%d): mask_land=%.3f rendered_land=%.3f excess=%.3f" % (tx, ty, ml, rl, worst[0]))
    for (tx, ty, ml, rl, ex) in floods[:12]:
        print("  FLOOD (%2d,%2d): mask=%.3f rendered=%.3f (+%.3f)" % (tx, ty, ml, rl, ex))
    verdict = "PASS (no floods)" if not floods else "FAIL (%d flooded tiles)" % len(floods)
    print("VERDICT: %s   (%.1fm)" % (verdict, (time.time() - t0) / 60))
    return 0 if not floods else 1


if __name__ == "__main__":
    raise SystemExit(main())
