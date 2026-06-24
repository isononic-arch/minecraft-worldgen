"""build_lithology.py — Phase 0.5 (S44).

Spec: PHYSICAL_REALISM_REFACTOR.md §6 Pass 0 "Lithology precompute".

Produces `masks/lithology.tif` at 1:8 precompute scale (6250×6250) uint8 with
values 1..6 matching `config/thresholds.json → lithology.groups[*].id`.

Derivation:
    1. Read `masks/override.tif` (50k×50k uint8 zone codes) and NEAREST-
       downsample to 6250×6250 by taking the top-left pixel of every 8×8
       block (matches the rest of the 1:8 precompute pipeline — slope,
       eco_gradients, hydrology all use this alignment).
    2. Map each zone code → zone name via `core.biome_assignment.OVERRIDE_BIOME_MAP`.
    3. Map zone name → lithology group id via
       `config/thresholds.json → lithology.zone_to_group` + `groups[name].id`.
    4. Apply elevation overrides (currently none) from
       `lithology.elevation_overrides.rules`.
    5. Write uint8 GeoTIFF, identity transform, same pattern as sibling masks.

Feature flag: lithology itself is gated by
`config/thresholds.json → lithology.feature_flag_enabled` (False for S44).
This builder runs regardless so the mask exists for diagnostics and
`validate_masks.py` — consumers check the flag before honoring it.

Usage:
    py tools/build_lithology.py
    py tools/build_lithology.py --masks masks/ --config config/thresholds.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import Affine

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.biome_assignment import OVERRIDE_BIOME_MAP  # noqa: E402

PRECOMPUTE_SCALE = 8  # 1:8, shared across the precompute pipeline


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--masks", type=Path, default=REPO_ROOT / "masks")
    p.add_argument("--config", type=Path, default=REPO_ROOT / "config" / "thresholds.json")
    p.add_argument("--out", type=Path, default=None,
                   help="Output path; default = masks/lithology.tif")
    return p.parse_args()


def _load_config(config_path: Path) -> dict:
    with open(config_path) as f:
        return json.load(f)


def _build_zone_to_id_lut(config: dict) -> dict[str, int]:
    """{zone_name: group_id} for fast ndarray remapping."""
    groups = config["lithology"]["groups"]
    zone_to_group = config["lithology"]["zone_to_group"]
    out: dict[str, int] = {}
    for zone_name, group_name in zone_to_group.items():
        if group_name not in groups:
            raise KeyError(
                f"lithology.zone_to_group[{zone_name}]={group_name!r} "
                f"not in lithology.groups")
        out[zone_name] = int(groups[group_name]["id"])
    return out


def _downsample_nearest(arr: np.ndarray, scale: int) -> np.ndarray:
    """Take the top-left of every scale×scale block (matches precompute alignment)."""
    return arr[::scale, ::scale]


def _read_downsampled_nearest(path: Path, scale: int) -> np.ndarray:
    """Memory-efficient 1:scale NEAREST downsample — reads rows on demand.

    Avoids holding the full 50k×50k raster in RAM (2.5GB uint8 × 2 copies).
    """
    with rasterio.open(path) as src:
        H, W = src.height, src.width
        # ceil sizing so row[::scale] matches out_W when W isn't divisible by scale
        # (mainland 50000/8=6250 exact -> unchanged; island 10867/8 -> 1359 not 1358)
        out_H, out_W = len(range(0, H, scale)), len(range(0, W, scale))
        dtype = src.dtypes[0]
        out = np.empty((out_H, out_W), dtype=dtype)
        # Read one source row at a time from the top-left of each scale×scale block.
        for oy in range(out_H):
            sy = oy * scale
            window = rasterio.windows.Window(0, sy, W, 1)
            row = src.read(1, window=window)[0]  # (W,)
            out[oy] = row[::scale]
    return out


def build_lithology(
    override_path: Path,
    height_path: Path,
    config: dict,
    lithology_region_path: Path | None = None,
) -> np.ndarray:
    """Return (6250, 6250) uint8 lithology mask.

    Pass 0 of the lithology derivation:
      - Zone → group assignment from config.
      - Elevation overrides from config.lithology.elevation_overrides.rules.

    S69: If ``lithology_region_path`` exists (typically
    ``masks/lithology_region.png`` at 8192×8192), it serves as a per-pixel
    override on top of the biome-derived assignment.  Non-zero pixels in
    the region image replace the derived group_id; zero pixels fall
    through.  Written by ``tools/override_studio.py`` Lithology tab.
    """
    override_lo = _read_downsampled_nearest(override_path, PRECOMPUTE_SCALE)
    H, W = override_lo.shape
    # size-agnostic: mainland override -> 6250², island override -> footprint/8.
    # Just require square (the 1:8 precompute alignment); don't hardcode 6250.
    assert H == W, f"lithology source must be square at 1:8; got {override_lo.shape}"

    zone_to_id = _build_zone_to_id_lut(config)

    # Build (256,) LUT: zone_code → group_id. 0 = unclassified (water/unknown).
    code_to_gid = np.zeros(256, dtype=np.uint8)
    for code, zone_name in OVERRIDE_BIOME_MAP.items():
        if zone_name in zone_to_id:
            code_to_gid[code] = zone_to_id[zone_name]
        # else leave 0 (e.g. water codes). Consumers treat 0 as "no lithology".

    out = code_to_gid[override_lo]  # vectorized remap, (6250, 6250) uint8

    # S69: optional per-pixel region override from lithology_region.png.
    if lithology_region_path is not None and lithology_region_path.exists():
        from PIL import Image as _PILImage
        valid_ids = set(int(g["id"]) for g in config["lithology"]["groups"].values())
        valid_ids.add(0)
        lr_img = _PILImage.open(lithology_region_path).convert("L")
        # Resize NEAREST to (6250, 6250). PIL uses (W, H) order.
        lr_img_lo = lr_img.resize((W, H), _PILImage.NEAREST)
        lr_arr = np.asarray(lr_img_lo, dtype=np.uint8)
        # Validate: only group IDs we know about (or 0 = pass-through).
        unique = set(np.unique(lr_arr).tolist())
        invalid = unique - valid_ids
        if invalid:
            print(f"[build_lithology] WARN: lithology_region.png has invalid "
                  f"IDs {sorted(invalid)} — zeroing them.")
            mask = np.zeros_like(lr_arr, dtype=bool)
            for gid in valid_ids:
                mask |= (lr_arr == gid)
            lr_arr = np.where(mask, lr_arr, 0).astype(np.uint8)
        overlay_pixels = int((lr_arr > 0).sum())
        if overlay_pixels > 0:
            out = np.where(lr_arr > 0, lr_arr, out).astype(np.uint8)
            print(f"[build_lithology] lithology_region.png applied: "
                  f"{overlay_pixels:,} pixels overridden")
        else:
            print(f"[build_lithology] lithology_region.png present but empty "
                  f"(all zeros) — no override applied")

    # Elevation overrides — currently empty rules list. Wire structure for Phase 2.
    rules = config["lithology"].get("elevation_overrides", {}).get("rules", [])
    if rules:
        height_lo = _read_downsampled_nearest(height_path, PRECOMPUTE_SCALE)
        groups = config["lithology"]["groups"]
        for rule in rules:
            mask = np.ones_like(out, dtype=bool)
            if "min_raw" in rule:
                mask &= height_lo >= rule["min_raw"]
            if "max_raw" in rule:
                mask &= height_lo < rule["max_raw"]
            if "only_zones" in rule:
                zone_mask = np.zeros_like(out, dtype=bool)
                for z in rule["only_zones"]:
                    code = next(
                        (c for c, n in OVERRIDE_BIOME_MAP.items() if n == z), None)
                    if code is not None:
                        zone_mask |= (override_lo == code)
                mask &= zone_mask
            target_gid = int(groups[rule["group"]]["id"])
            out[mask] = target_gid

    return out


def _write_tif(arr: np.ndarray, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = {
        "driver": "GTiff",
        "height": arr.shape[0],
        "width": arr.shape[1],
        "count": 1,
        "dtype": "uint8",
        "compress": "deflate",
        "predictor": 2,
        "transform": Affine.identity(),
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(arr, 1)


def main() -> int:
    args = _parse_args()
    config = _load_config(args.config)

    override_path = args.masks / "override.tif"
    height_path = args.masks / "height.tif"
    lithology_region_path = args.masks / "lithology_region.png"
    out_path = args.out or (args.masks / "lithology.tif")

    print(f"[build_lithology] reading {override_path}")
    if lithology_region_path.exists():
        print(f"[build_lithology] region overlay found: {lithology_region_path}")
    lith = build_lithology(override_path, height_path, config, lithology_region_path)

    unique, counts = np.unique(lith, return_counts=True)
    total = lith.size
    dist = {int(u): f"{c / total * 100:.2f}%" for u, c in zip(unique, counts)}
    print(f"[build_lithology] group distribution: {dist}")

    print(f"[build_lithology] writing {out_path} ({lith.shape} {lith.dtype})")
    _write_tif(lith, out_path)

    flag = config["lithology"].get("feature_flag_enabled", False)
    print(f"[build_lithology] done. feature_flag_enabled={flag} "
          f"(consumers must honor this before applying the mask)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
