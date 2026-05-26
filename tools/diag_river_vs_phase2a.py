"""
diag_river_vs_phase2a.py — Empirically check whether Phase 2A is breaking
rivers on the (51,53) / (33,7) / (13,82) regression family.

For each problem tile:
  1. Read masks via tile_streamer + apply hydro_region overlay.
  2. Generate surface_y via column_generator.
  3. Carve rivers via river_carver_v2 → produces `river_meta` + new `surface_y`.
  4. Compute slope (cliff_deg) via eco_gradients.
  5. Build the SAME amp_scale that Phase 2A uses:
        - slope-fade band 35°-45°
        - river_meta dilated 8 blocks → fade over 14 blocks
        - wash exclusion at gap==5 & flow>0.002
  6. Output a PNG overlay per tile:
        - red    = river_meta > 0 (river/lake)
        - blue   = pixels Phase 2A *would* displace (probability > 0)
        - green  = overlap = river bank pixels Phase 2A touches BEYOND
                   the 8-block exclusion (this is the smoking gun)

Run:
    py tools/diag_river_vs_phase2a.py
        --config config/thresholds.json
        --masks  masks/
        --out    diag_river_phase2a/

No MCAs written, no schematics placed.  Pure mask diff.  Should run in
~30 s per tile.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Put project root on sys.path so `core.*` imports work when run from anywhere.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import numpy as np

PROBLEM_TILES = [(51, 53), (33, 7), (13, 82)]
TILE_SIZE = 512


def analyse_tile(
    tile_x: int,
    tile_y: int,
    cfg: dict,
    masks_dir: Path,
    out_dir: Path,
) -> dict:
    """Run pipeline up to Phase 2A amp_scale computation, dump overlay PNG."""
    from core.tile_streamer import read_tile
    from core.hydro_region_overlay import apply_hydro_region_overlay
    from core.gaea_gap_sampler import build_gap_config
    from core.biome_assignment import assign_biomes
    from core.column_generator import generate_columns
    from core.river_carver_v2 import carve_rivers
    from core.eco_gradients import compute_cliff_deg
    from core.noise_fields import load_noise_generators
    from scipy.ndimage import binary_dilation, distance_transform_edt

    col_off = tile_x * TILE_SIZE
    row_off = tile_y * TILE_SIZE
    w = h = TILE_SIZE

    noise = load_noise_generators(_PROJECT_ROOT / "config" / "thresholds.json")
    gap_cfg = build_gap_config(cfg.get("gaea_gaps", {}), masks_dir)

    masks = read_tile(
        masks_dir=masks_dir,
        col_off=col_off,
        row_off=row_off,
        width=w,
        height=h,
        gap_config=gap_cfg,
    )
    apply_hydro_region_overlay(masks, masks_dir, col_off, row_off, w)

    biome_grid = assign_biomes(
        height_tile=masks["height"],
        slope_tile=masks["slope"],
        flow_tile=masks["flow"],
        erosion_tile=masks["erosion"],
        override_tile=masks["override"],
        noise_fields=noise,
        cfg=cfg,
    )
    height_u16 = np.round(masks["height"] * 65535.0).astype(np.uint16)
    surface_y = generate_columns(
        height_tile=height_u16,
        slope_tile=masks["slope"],
        biome_grid=biome_grid,
        shore_tile=masks["shore"],
        noise_fields=noise,
        cfg=cfg,
        tile_x=tile_x,
        tile_y=tile_y,
    )

    pre_carve_y = surface_y.copy()
    surface_y, river_meta, _conn, _water_y_field = carve_rivers(
        surface_y=surface_y,
        flow_tile=masks["flow"],
        river_tile=masks["river"],
        cfg=cfg,
        hydro_order=masks.get("hydro_order"),
        hydro_width=masks.get("hydro_width"),
        hydro_depth=masks.get("hydro_depth"),
        hydro_lake=masks.get("hydro_lake"),
        hydro_lkdep=masks.get("hydro_lkdep"),
        hydro_lake_wl=masks.get("hydro_lake_wl"),
        hydro_centerline=masks.get("hydro_centerline"),
        height_norm=masks["height"],
        hydro_river_bed=masks.get("hydro_river_bed"),
        hydro_river_water_y=masks.get("hydro_river_water_y"),
        masks_dir=masks_dir,
        tile_x=tile_x,
        tile_z=tile_y,
    )

    # S88: dry-river check.  Per user: rivers look "dry / staircased" with
    # "terrain blocks INSIDE the river channel."  For a river to render
    # correctly, surface_y at river_meta>0 pixels MUST be < river_water_y.
    # If equal/greater, chunk_writer's `(abs_y > surface_y) & (abs_y <= rw)`
    # mask is empty -> no water placed -> terrain stays in the channel.
    # We need river_water_y to check this, which is built post-carve in
    # run_pipeline.py lines 545-551 (not exposed by carve_rivers itself).
    # Replicate the same logic here against water_y_field.
    pre_carve_y_for_diag = surface_y.copy()
    if _water_y_field is not None:
        river_water_y_diag = np.where(
            _water_y_field > 0, _water_y_field, np.int16(-999)
        ).astype(np.int16)
    else:
        carved_diag = (river_meta > 0) & (surface_y < pre_carve_y)
        river_water_y_diag = np.where(
            carved_diag, pre_carve_y - 1, np.int16(-999)
        ).astype(np.int16)
    # Pixels where the river *should* have water but the column is
    # blocked: river_meta>0 AND river_water_y_diag>0 AND surface_y >= river_water_y_diag.
    dry_river_px = (
        (river_meta > 0)
        & (river_water_y_diag > 0)
        & (surface_y.astype(np.int16) >= river_water_y_diag)
    )
    # Also a softer warn: river_water_y_diag == -999 at a river_meta>0 pixel
    # (carver couldn't set water_y for the pixel).
    no_water_y_px = (river_meta > 0) & (river_water_y_diag <= 0)

    cliff_deg = compute_cliff_deg(surface_y)

    # ---- Reconstruct Phase 2A amp_scale per current run_pipeline.py ----
    crunch_cfg = cfg.get("peak_crunch", {}) if isinstance(cfg, dict) else {}
    fade_start = float(crunch_cfg.get("slope_fade_start_deg", 35.0))
    slope_full = float(crunch_cfg.get("slope_full_deg", 45.0))
    bank_radius = int(crunch_cfg.get("river_bank_blocks", 8))
    river_fade_blocks = float(crunch_cfg.get("river_fade_blocks", 14.0))

    amp_scale = np.clip(
        (cliff_deg - fade_start) / max(0.1, slope_full - fade_start),
        0.0, 1.0,
    ).astype(np.float32)

    # River exclusion (current 8-block dilation + 14-block fade)
    river_zone = np.zeros_like(river_meta, dtype=bool)
    river_fade = np.ones_like(amp_scale, dtype=np.float32)
    if (river_meta > 0).any():
        river_zone = binary_dilation(river_meta > 0, iterations=bank_radius)
        river_dist = distance_transform_edt(~river_zone).astype(np.float32)
        river_fade = np.clip(
            river_dist / max(0.5, river_fade_blocks), 0.0, 1.0
        )
        amp_scale = amp_scale * river_fade

    # Wash exclusion (current HARD logic from the S88 patch)
    wash_cfg = cfg.get("washes", {}) if isinstance(cfg, dict) else {}
    wash_min_flow = float(wash_cfg.get("min_flow", 0.002))
    wash_dilation = int(wash_cfg.get("dilation", 2))
    # Use the same gap as run_pipeline (placeholder: gap==5 is rock_gap)
    gap_mask_raw = masks.get("rock_gap")
    if gap_mask_raw is not None:
        wash_core = (gap_mask_raw > 0) & (masks["flow"] > wash_min_flow)
        if wash_core.any():
            wash_zone_full = binary_dilation(
                wash_core, iterations=wash_dilation + 2
            )
            amp_scale[wash_zone_full] = 0.0

    # ---- Build overlay arrays ----
    river_mask = river_meta > 0
    phase2a_active = amp_scale > 0.01  # any non-trivial displacement chance
    overlap = phase2a_active & ~river_mask  # Phase 2A would touch non-river pixels

    # The smoking-gun zone: Phase 2A active OUTSIDE the 8-block river dilation
    # but WITHIN a typical bank widening radius (say 30 blocks).
    if river_mask.any():
        dist_from_river = distance_transform_edt(~river_mask).astype(np.float32)
        bank_zone_30 = (dist_from_river > bank_radius) & (dist_from_river < 30)
        smoking_gun = phase2a_active & bank_zone_30
    else:
        bank_zone_30 = np.zeros_like(river_mask)
        smoking_gun = np.zeros_like(river_mask)

    # ---- Render PNG overlay ----
    try:
        from PIL import Image
    except ImportError:
        print("[diag_river] Pillow not available — saving NPZ instead")
        out_path = out_dir / f"diag_river_{tile_x}_{tile_y}.npz"
        np.savez_compressed(
            out_path,
            river_meta=river_meta,
            amp_scale=amp_scale,
            phase2a_active=phase2a_active,
            smoking_gun=smoking_gun,
            cliff_deg=cliff_deg.astype(np.float32),
        )
        return {
            "tile": (tile_x, tile_y),
            "river_px": int(river_mask.sum()),
            "phase2a_active_px": int(phase2a_active.sum()),
            "smoking_gun_px": int(smoking_gun.sum()),
            "out_path": str(out_path),
        }

    # Compose RGB image: background = grey by surface_y, overlay = colored masks
    sy_norm = ((surface_y - 60).clip(0, 200) / 200.0 * 255).astype(np.uint8)
    rgb = np.stack([sy_norm, sy_norm, sy_norm], axis=-1)

    # Red overlay: river pixels
    rgb[river_mask] = [255, 30, 30]

    # Blue overlay: Phase 2A would touch this pixel (outside river area)
    blue_only = phase2a_active & ~river_mask & ~smoking_gun
    rgb[blue_only] = [60, 60, 220]

    # Green/yellow overlay: smoking gun — Phase 2A on bank zone (9-30 blocks from river)
    rgb[smoking_gun] = [255, 220, 30]

    # ---- Overlay dry-river pixels in MAGENTA (drawn LAST so they win) ----
    if dry_river_px.any():
        rgb[dry_river_px] = [255, 30, 200]  # magenta = staircased
    if no_water_y_px.any():
        only_no_wy = no_water_y_px & ~dry_river_px
        rgb[only_no_wy] = [255, 180, 30]  # orange = river_water_y not set

    out_path = out_dir / f"diag_river_{tile_x}_{tile_y}.png"
    out_dir.mkdir(parents=True, exist_ok=True)
    img = Image.fromarray(rgb, mode="RGB")
    img.save(out_path)

    # Per-value breakdown: CHAN_STREAM=1, CHAN_RIVER=2, CHAN_LAKE=3, CHAN_WADI=4
    # For each value, count: total, has-water-y, no-water-y
    chan_breakdown = {}
    for chan_val, chan_name in [(1, "STREAM"), (2, "RIVER"), (3, "LAKE"),
                                 (4, "WADI")]:
        mask_chan = river_meta == chan_val
        if mask_chan.any():
            n_total = int(mask_chan.sum())
            n_has_wy = int((mask_chan & (river_water_y_diag > 0)).sum())
            n_no_wy = int((mask_chan & (river_water_y_diag <= 0)).sum())
            chan_breakdown[chan_name] = {
                "total": n_total,
                "has_wy": n_has_wy,
                "no_wy": n_no_wy,
            }

    # Also dump water_y_field stats overall
    wy_set_px = int((_water_y_field > 0).sum()) if _water_y_field is not None else 0

    return {
        "tile": (tile_x, tile_y),
        "river_px": int(river_mask.sum()),
        "phase2a_active_px": int(phase2a_active.sum()),
        "smoking_gun_px": int(smoking_gun.sum()),
        "dry_river_px": int(dry_river_px.sum()),
        "no_water_y_px": int(no_water_y_px.sum()),
        "water_y_field_set_px": wy_set_px,
        "channels": chan_breakdown,
        "out_path": str(out_path),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/thresholds.json")
    ap.add_argument("--masks", default="masks/")
    ap.add_argument("--out", default="diag_river_phase2a/")
    ap.add_argument("--tiles", default=None,
                    help='Comma-sep tile coords, e.g. "51,53;33,7;13,82". '
                         "Defaults to the regression family.")
    args = ap.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(f"config not found: {cfg_path}")
        return 2
    with open(cfg_path) as f:
        cfg = json.load(f)

    masks_dir = Path(args.masks)
    out_dir = Path(args.out)

    if args.tiles:
        tiles = []
        for chunk in args.tiles.split(";"):
            x, z = chunk.strip().split(",")
            tiles.append((int(x), int(z)))
    else:
        tiles = PROBLEM_TILES

    out_dir.mkdir(parents=True, exist_ok=True)
    summary: list[dict] = []
    for tx, tz in tiles:
        print(f"[diag_river] analysing tile ({tx},{tz})...")
        try:
            res = analyse_tile(tx, tz, cfg, masks_dir, out_dir)
        except Exception as exc:
            print(f"  FAILED: {exc!r}")
            import traceback as _tb
            _tb.print_exc()
            continue
        summary.append(res)
        print(f"  river_px={res['river_px']:6d}  "
              f"phase2a_active_px={res['phase2a_active_px']:6d}  "
              f"smoking_gun_px={res['smoking_gun_px']:6d}")
        print(f"  dry_river_px={res.get('dry_river_px', 0):6d}  "
              f"no_water_y_px={res.get('no_water_y_px', 0):6d}  "
              f"water_y_field_set_px={res.get('water_y_field_set_px', 0):6d}")
        if res.get("channels"):
            for cn, cd in res["channels"].items():
                print(f"    {cn:7s}: total={cd['total']:6d}  "
                      f"has_wy={cd['has_wy']:6d}  no_wy={cd['no_wy']:6d}")
        print(f"  -> {res['out_path']}")

    print("")
    print("==== summary ====")
    hdr = (f"{'tile':<10} {'river_px':>10} {'p2a_act':>10} "
           f"{'smoke_gun':>10} {'dry_river':>10} {'no_wY':>8}")
    print(hdr)
    for r in summary:
        tx, tz = r["tile"]
        print(f"({tx:>2},{tz:>2})     {r['river_px']:>10d} "
              f"{r['phase2a_active_px']:>10d} "
              f"{r['smoking_gun_px']:>10d} "
              f"{r.get('dry_river_px', 0):>10d} "
              f"{r.get('no_water_y_px', 0):>8d}")
    print("")
    print("interpretation:")
    print("  smoking_gun_px > ~500 = Phase 2A displaces river-bank pixels.")
    print("  dry_river_px   > ~50  = river_meta>0 but surface_y >= "
          "river_water_y")
    print("    -> chunk_writer cannot place water in the channel -> ")
    print("       staircased / dry rivers (THE current symptom).")
    print("  no_water_y_px  > ~50  = river_meta>0 but river_water_y unset")
    print("    -> S86 fluid-tick gate fails to skip; column may have water")
    print("       but the carver did not register it.")

    summary_path = out_dir / "summary.json"
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\nsummary -> {summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
