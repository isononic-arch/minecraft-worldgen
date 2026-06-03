"""Per-biome best reference tile sampler (LAND-AWARE, S89 sweep).

For each biome in OVERRIDE_BIOME_MAP, scan the tile grid and pick the tile with
the most pixels of that biome **that are also land under the CURRENT terrain
spline**. Then add a rock/mountain section: one high-rock-exposure land tile per
lithology group (so all 6 litho palettes get a mountain reference tile).

Why land-aware (S89): the terrain spline was re-tuned, so the SAME height.tif
raw values now map to different MC-Y. Tiles that used to be land can now sit
below sea level (ocean) without height.tif changing. The old sampler never
applied the spline, so its centroids TP'd into ocean. We now apply
`config.terrain_spline` to height.tif, gate on surface MC-Y >= 63, pick the TP
point from actual land pixels, and derive TP-Y from the real terrain height.

All scanning is done on decimated 1:8 reads (6250x6250) for memory safety.

Emits:
- memory/biome_reference_tiles.csv   (kind,biome/group,zone,tile_x,tile_z,pct,tp)
- memory/BIOME_VALIDATOR_CHECKLIST.md (checkable markdown table)

Usage:
    py tools/diag_biome_sampler.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling

_WORKTREE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_WORKTREE))
from core.biome_assignment import OVERRIDE_BIOME_MAP  # noqa: E402

MASKS = Path(r"C:/Users/nicho/minecraft-worldgen/masks")
OVERRIDE_PATH = MASKS / "override.tif"
HEIGHT_PATH = MASKS / "height.tif"
ROCKGAP_PATH = MASKS / "rock_gap.tif"
LITHO_PATH = MASKS / "lithology.tif"
CONFIG_PATH = _WORKTREE / "config" / "thresholds.json"
CSV_PATH = _WORKTREE / "memory" / "biome_reference_tiles.csv"
CHECKLIST_PATH = _WORKTREE / "memory" / "BIOME_VALIDATOR_CHECKLIST.md"

FULL = 50000
DEC = 8
DIM = FULL // DEC          # 6250
TILE = 512
TILE_D = TILE // DEC       # 64
GRID = 97                  # full tiles that fit in 49664 < 50000
SEA_Y = 63                 # MC-Y land threshold
TP_HEADROOM = 40           # blocks above terrain for the TP
TP_Y_MAX = 760


def _load_spline():
    cfg = json.loads(CONFIG_PATH.read_text())
    sp = cfg["terrain_spline"]
    return np.asarray(sp["gaea_in"], np.float32), np.asarray(sp["mc_y_out"], np.float32)


def _read_dec(path, resampling):
    with rasterio.open(path) as s:
        return s.read(1, out_shape=(DIM, DIM), resampling=resampling)


def _read_dec_frac(path):
    """Average-resample a {0,1} mask into a float fraction (uint8 avg rounds to 0)."""
    out = np.zeros((DIM, DIM), np.float32)
    with rasterio.open(path) as s:
        s.read(1, out=out, resampling=Resampling.average)
    return out


def _tile_world_point(local_y_d, local_x_d, tx, tz):
    """1:8 tile-local pixel -> approximate world-coord center of that 1:8 cell."""
    wx = tx * TILE + local_x_d * DEC + DEC // 2
    wz = tz * TILE + local_y_d * DEC + DEC // 2
    return int(wx), int(wz)


def main() -> int:
    gi, mo = _load_spline()

    print(f"[sampler] reading decimated masks ({DIM}x{DIM})", flush=True)
    override = _read_dec(OVERRIDE_PATH, Resampling.nearest)
    height = _read_dec(HEIGHT_PATH, Resampling.average).astype(np.float32)
    rockfrac = _read_dec_frac(ROCKGAP_PATH)

    mcy = np.interp(height, gi, mo).astype(np.float32)
    land = mcy >= SEA_Y
    print(f"[sampler] land frac={land.mean():.3f}", flush=True)

    biomes = [(z, n) for z, n in OVERRIDE_BIOME_MAP.items() if n]

    # ---- Section 1: one clean LAND tile per biome -----------------------
    biome_rows = []
    for zone, name in biomes:
        bmask = (override == zone) & land
        if not bmask.any():
            present = (override == zone).any()
            note = "(present but all below sea level)" if present else "(biome not present)"
            biome_rows.append({
                "kind": "biome", "name": name, "zone": zone, "tx": None, "tz": None,
                "pct": 0.0, "tp": note,
            })
            print(f"  {name:26s} zone={zone:3d} {note}", flush=True)
            continue

        best_cnt, best_tx, best_tz = 0, -1, -1
        for tz in range(GRID):
            for tx in range(GRID):
                blk = bmask[tz * TILE_D:(tz + 1) * TILE_D, tx * TILE_D:(tx + 1) * TILE_D]
                c = int(blk.sum())
                if c > best_cnt:
                    best_cnt, best_tx, best_tz = c, tx, tz

        pct = 100.0 * best_cnt / (TILE_D * TILE_D)
        sub = bmask[best_tz * TILE_D:(best_tz + 1) * TILE_D,
                    best_tx * TILE_D:(best_tx + 1) * TILE_D]
        ys, xs = np.where(sub)
        ly, lx = int(np.median(ys)), int(np.median(xs))
        wx, wz = _tile_world_point(ly, lx, best_tx, best_tz)
        gy = mcy[best_tz * TILE_D + ly, best_tx * TILE_D + lx]
        ty = int(min(gy + TP_HEADROOM, TP_Y_MAX))
        tp = f"/tp @s {wx} {ty} {wz}"
        biome_rows.append({
            "kind": "biome", "name": name, "zone": zone, "tx": best_tx, "tz": best_tz,
            "pct": pct, "tp": tp,
        })
        print(f"  {name:26s} zone={zone:3d} tile=({best_tx:2d},{best_tz:2d}) land%={pct:5.1f} surfY={int(gy):3d}  {tp}", flush=True)

    # ---- Section 2: one high-rock LAND tile per lithology group ---------
    # The per-pixel lithology group is the PAINTED `lithology.tif` group-id raster
    # (S88 walk #4d: SOLE source of truth, no biome->group fallback). Inferring the
    # group from the dominant biome via zone_to_group is WRONG — e.g. temperate_basaltic
    # rock sits under high-altitude conifer biomes, so a biome-based scan finds only
    # the low coastal tiles. We key off lithology.tif directly and pick, per group,
    # the HIGHEST-ALTITUDE tile that has enough exposed rock of that group.
    print("[sampler] scanning rock/mountain tiles per lithology group (painted lithology.tif)", flush=True)
    litho = _read_dec(LITHO_PATH, Resampling.nearest)
    cfg = json.loads(CONFIG_PATH.read_text())
    gid_of = {n: int(d.get("id", 0)) for n, d in cfg["lithology"]["groups"].items()}
    MIN_ROCK_PX = 30  # 1:8 px of exposed group-rock required in a tile
    group_best = {g: None for g in gid_of}  # (peak_alt, tx, tz, npx, wx, wz, dom_name)

    exposed = (rockfrac > 0.5) & land
    for tz in range(GRID):
        for tx in range(GRID):
            sl = (slice(tz * TILE_D, (tz + 1) * TILE_D), slice(tx * TILE_D, (tx + 1) * TILE_D))
            eblk = exposed[sl]
            if not eblk.any():
                continue
            gblk = litho[sl]
            myblk = mcy[sl]
            for g, gid in gid_of.items():
                gmask = eblk & (gblk == gid)
                npx = int(gmask.sum())
                if npx < MIN_ROCK_PX:
                    continue
                masked = np.where(gmask, myblk, -9999.0)
                py, px = np.unravel_index(int(masked.argmax()), masked.shape)
                peak_alt = float(myblk[py, px])
                cur = group_best[g]
                if cur is None or peak_alt > cur[0]:
                    dom_name = OVERRIDE_BIOME_MAP.get(int(override[sl][py, px]), "")
                    wx, wz = _tile_world_point(py, px, tx, tz)
                    group_best[g] = (peak_alt, tx, tz, npx, wx, wz, dom_name)

    rock_rows = []
    for g in sorted(gid_of):
        b = group_best[g]
        if b is None:
            rock_rows.append({"kind": "rock", "name": g, "zone": "", "tx": None,
                              "tz": None, "pct": 0.0, "tp": "(no rock tile found)"})
            print(f"  ROCK {g:22s} (none found)", flush=True)
            continue
        peak_alt, tx, tz, npx, wx, wz, dom = b
        peak_y = int(min(peak_alt + TP_HEADROOM, TP_Y_MAX))
        tp = f"/tp @s {wx} {peak_y} {wz}"
        rock_rows.append({"kind": "rock", "name": f"{g} ({dom})", "zone": "",
                          "tx": tx, "tz": tz, "pct": peak_alt, "tp": tp})
        print(f"  ROCK {g:22s} tile=({tx:2d},{tz:2d}) peakSurfY={int(peak_alt):3d} rockpx={npx:4d}  {tp}", flush=True)

    # ---- write CSV ------------------------------------------------------
    CSV_PATH.parent.mkdir(exist_ok=True)
    lines = ["kind,name,zone,tile_x,tile_z,pct,tp_command"]
    for r in biome_rows + rock_rows:
        tx = r["tx"] if r["tx"] is not None else ""
        tz = r["tz"] if r["tz"] is not None else ""
        lines.append(f"{r['kind']},{r['name']},{r['zone']},{tx},{tz},{r['pct']:.2f},{r['tp']}")
    CSV_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"[sampler] CSV -> {CSV_PATH}", flush=True)

    # ---- write checklist ------------------------------------------------
    md = [
        "# Biome Validator Checklist (S89 land-aware sweep)",
        "",
        "Auto-generated by `tools/diag_biome_sampler.py`. Land-gated against the",
        "CURRENT `config.terrain_spline` (surface MC-Y >= 63). TP-Y is derived from",
        "real terrain height + headroom, so every command lands you above ground.",
        "",
        "## Biomes (clean land tiles)",
        "",
        "| Biome | Zone | Best tile | Land % | TP | Visually OK | GC OK | Schematics OK | Palette OK | Notes |",
        "|-------|------|-----------|--------|----|-------------|-------|---------------|------------|-------|",
    ]
    for r in biome_rows:
        ts = f"({r['tx']},{r['tz']})" if r["tx"] is not None else "(absent)"
        md.append(f"| {r['name']} | {r['zone']} | {ts} | {r['pct']:.1f}% | `{r['tp']}` | [ ] | [ ] | [ ] | [ ] |  |")
    md += [
        "",
        "## Mountain / rock reference tiles (one per lithology group, painted lithology.tif)",
        "",
        "| Litho group (biome at peak) | Best tile | Peak surfY | TP | Rock OK | Snow OK | Relief OK | Notes |",
        "|-----------------------------|-----------|------------|----|---------|---------|-----------|-------|",
    ]
    for r in rock_rows:
        ts = f"({r['tx']},{r['tz']})" if r["tx"] is not None else "(absent)"
        md.append(f"| {r['name']} | {ts} | {r['pct']:.0f} | `{r['tp']}` | [ ] | [ ] | [ ] |  |")
    md += [
        "",
        "## Legend",
        "- **Land %** = fraction of the 512px tile that is this biome AND above sea level.",
        "- **Peak surfY** = MC-Y of the highest exposed-rock pixel of that painted lithology group.",
        "- Mark Y/N in-world; log follow-ups under NICK PRIORITIES.",
    ]
    CHECKLIST_PATH.write_text("\n".join(md), encoding="utf-8")
    print(f"[sampler] checklist -> {CHECKLIST_PATH}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
