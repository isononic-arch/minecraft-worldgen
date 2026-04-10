#!/usr/bin/env python3
"""
upscale_override_vectorized.py
Upscales override_vectorized.png (RGBA, 8192×8192) to 50k×50k BigTIFF.

Uses NEAREST-neighbour resizing for zone codes. Bilinear was tried but
creates intermediate values (e.g. 50 between zone 0 and zone 100) that
LUT-snap to phantom biomes (ARCTIC_TUNDRA, ALPINE_MEADOW, etc.) throughout
every zone boundary. Nearest-neighbor eliminates this entirely.

Zone boundary organics come from the jitter pass (JITTER_PASSES=3) applied
at source resolution — not from interpolation. At 50k/8192≈6× scale, each
jitter-scattered source pixel maps to a ~6×6 output block, giving ample
natural-looking transition zones with no phantom codes.

Usage: python upscale_override_vectorized.py
"""

import os
import numpy as np
from pathlib import Path
from PIL import Image, ImageDraw
import rasterio
from rasterio.windows import Window
from scipy.interpolate import splprep, splev
from skimage.measure import find_contours

# ── Config ────────────────────────────────────────────────────────────────────
INPUT        = Path(r"C:\Users\nicho\minecraft-worldgen\override_vectorized.png")
OVERRIDE_BASE = Path(r"C:\Users\nicho\minecraft-worldgen\override_final.png")
OUTPUT       = Path(r"C:\Users\nicho\minecraft-worldgen\masks\override.tif")
TARGET  = 50_000        # output pixel dimensions (square)
CHUNK_H = 256           # output rows per chunk — ~100MB RAM per pass

# Pre-upscale jitter — applied at 8192 source resolution before upscaling.
# Only swaps boundary pixels to existing neighbour zone codes: no phantom biomes.
# Each pass adds ~1 source pixel (~6 MC blocks) of scatter radius.
JITTER_PASSES = 3       # number of scatter passes (3 -> ~7–8 block radius at 50k)
JITTER_PROB   = 0.5     # probability per boundary pixel per pass of adopting a neighbour
JITTER_SEED   = 42      # set to None for non-deterministic

# Post-upscale jitter — applied at 50k resolution AFTER median smooth.
# Adds organic micro-scatter to the smoothed boundaries.
POST_JITTER_PASSES = 8
POST_JITTER_PROB   = 0.5
POST_JITTER_SEED   = 137  # different seed from pre-jitter for independent noise

# 2-stage upscale: 8192 -> INTERMEDIATE -> 50k with median filter at intermediate res.
# Median at intermediate (16384) with kernel 17 ≈ same smoothing as kernel 51 at 50k,
# but runs in ~30 seconds instead of ~100 minutes.
INTERMEDIATE_RES  = 16384   # intermediate resolution for median filter
MEDIAN_KERNEL     = 17      # kernel at intermediate res (17 @ 16k ≈ 51 @ 50k)

# Contour smoothing — applied at source resolution before jitter/upscale.
CONTOUR_SMOOTH    = False   # DISABLED — median filter at 50k is more effective
CONTOUR_SMOOTH_S  = 800
CONTOUR_MIN_PTS   = 20

# Alignment (from override_aligner.py interactive session)
FLIP_Z        = False   # toggled Session 25 — contour smooth pass corrected orientation
ALIGN_SCALE   = 1.01    # override covers 1% more world space; pre-crops source to 8192/1.01 px

# Valid zone codes from OVERRIDE_BIOME_MAP
VALID_ZONES = [
    0, 10, 20, 30, 35, 40, 50, 55, 60, 70, 80, 90, 100,
    110, 115, 120, 130, 140, 150, 160, 170, 190, 200, 210, 220, 230, 240,
]
# ─────────────────────────────────────────────────────────────────────────────


def smooth_contours(arr: np.ndarray, lut: np.ndarray,
                    s: float = CONTOUR_SMOOTH_S,
                    min_pts: int = CONTOUR_MIN_PTS) -> np.ndarray:
    """Smooth zone boundaries by fitting B-splines to contours and re-rasterizing.

    For each zone code, extracts boundary contours, fits a smoothing B-spline,
    and re-fills the zone polygon with the smoothed boundary. Processes zones
    largest-area-first so smaller zones correctly overwrite larger ones.

    Every pixel in the output is a valid zone code (re-rasterized from polygons).
    """
    h, w = arr.shape
    # Quantize first to ensure clean zone codes
    arr = lut[arr]
    codes = sorted(np.unique(arr).tolist())
    # Skip zone 0 (ocean/background) — it's the default canvas
    land_codes = [c for c in codes if c != 0]

    # Sort by area descending — largest zones painted first, smallest on top
    areas = {c: int((arr == c).sum()) for c in land_codes}
    land_codes.sort(key=lambda c: areas[c], reverse=True)

    print(f"  contour smooth: {len(land_codes)} zones, s={s}")

    # Start with zone-0 canvas
    result = np.zeros((h, w), dtype=np.uint8)

    # Paint each zone with smoothed contours
    total_contours = 0
    for code in land_codes:
        mask = (arr == code).astype(np.float64)
        # find_contours at 0.5 level on binary mask
        contours = find_contours(mask, 0.5)

        # Create PIL image for this zone's polygons
        zone_img = Image.new("L", (w, h), 0)
        draw = ImageDraw.Draw(zone_img)

        for contour in contours:
            n = len(contour)
            if n < min_pts:
                continue

            # contour is (N, 2) with (row, col) — convert to (x, y) for PIL
            y_pts = contour[:, 0]
            x_pts = contour[:, 1]

            try:
                # Fit periodic B-spline (closed contour)
                # k=3 cubic, s controls smoothing
                tck, u = splprep([x_pts, y_pts], s=s, per=True, k=3)
                # Evaluate at enough points for smooth rendering
                n_eval = max(n, 200)
                u_new = np.linspace(0, 1, n_eval)
                x_smooth, y_smooth = splev(u_new, tck)

                # Build polygon point list for PIL
                poly = list(zip(x_smooth.astype(float), y_smooth.astype(float)))
                draw.polygon(poly, fill=255)
                total_contours += 1
            except Exception:
                # Spline fit can fail on degenerate contours — fall back to raw
                poly = list(zip(x_pts.astype(float), y_pts.astype(float)))
                draw.polygon(poly, fill=255)
                total_contours += 1

        # Write this zone where the smoothed polygon covers
        zone_mask = np.array(zone_img, dtype=bool)
        result[zone_mask] = code

    print(f"  contour smooth: {total_contours} contours smoothed")
    return result


def apply_jitter(arr: np.ndarray, passes: int, prob: float, seed) -> np.ndarray:
    """Scatter zone boundaries by randomly swapping boundary pixels with neighbours.

    Only ever adopts a value that already exists in the 4-connected neighbourhood,
    so the output contains only zone codes present in the input — no phantom biomes.

    For large arrays (>10k px), processes in horizontal strips with overlap to
    stay within memory.  Overlap = passes so edge artifacts are fully trimmed.
    """
    h, w = arr.shape
    STRIP_H = 2500  # rows per strip (≈125 MB for uint8 50k-wide)
    overlap = passes  # each pass can propagate 1px, so `passes` overlap is safe

    if h <= STRIP_H + 2 * overlap:
        # Small array — process in one shot
        return _jitter_core(arr, passes, prob, seed)

    # Large array — strip-based processing (in-place to avoid OOM)
    print(f"  strip-based jitter: {STRIP_H}px strips, {overlap}px overlap")
    out = arr  # work in-place — caller's array is modified
    n_strips = (h + STRIP_H - 1) // STRIP_H
    for s in range(n_strips):
        r0 = s * STRIP_H
        r1 = min(h, r0 + STRIP_H)
        # Add overlap on both sides
        r0_ext = max(0, r0 - overlap)
        r1_ext = min(h, r1 + overlap)

        strip = _jitter_core(arr[r0_ext:r1_ext].copy(), passes, prob,
                             seed + s if seed is not None else None)

        # Trim overlap and write back only the non-overlapped core
        trim_top = r0 - r0_ext
        trim_bot = r1_ext - r1
        core = strip[trim_top : strip.shape[0] - trim_bot if trim_bot > 0 else strip.shape[0]]
        out[r0:r1] = core
        print(f"  strip {s+1}/{n_strips} done (rows {r0}-{r1})")
    return out


def _jitter_core(arr: np.ndarray, passes: int, prob: float, seed) -> np.ndarray:
    """Run jitter passes on a (possibly small) array. Caller owns the copy.

    Memory-efficient: processes in vertical column chunks to keep peak RAM low.
    Each chunk is ~5000 columns wide — boundary detection + swap fits in ~30MB.
    """
    rng = np.random.default_rng(seed)
    h, w = arr.shape
    CHUNK_W = 5000  # columns per chunk (~30MB for bool arrays)

    for p in range(passes):
        # Process in column chunks to avoid full-width boolean allocation
        for c0 in range(0, w - 2, CHUNK_W):
            c1 = min(c0 + CHUNK_W, w - 2)
            cw = c1 - c0

            # Slice views into arr (with 1px border on all sides)
            a = arr[1:-1, c0 + 1:c1 + 1]  # interior chunk

            # Boundary detection — only this chunk
            is_boundary = np.zeros((h - 2, cw), dtype=bool)
            is_boundary |= (arr[:-2, c0 + 1:c1 + 1] != a)  # up
            is_boundary |= (arr[2:,  c0 + 1:c1 + 1] != a)  # down
            is_boundary |= (arr[1:-1, c0:c1]         != a)  # left
            is_boundary |= (arr[1:-1, c0 + 2:c1 + 2] != a)  # right

            by, bx = np.where(is_boundary)
            del is_boundary
            if len(by) == 0:
                continue

            # Sparse swap: only boundary pixels
            do_swap = rng.random(len(by), dtype=np.float32) < prob
            by = by[do_swap]
            bx = bx[do_swap]
            if len(by) == 0:
                continue

            ch = rng.integers(0, 4, size=len(by), dtype=np.uint8)
            ay = by + 1
            ax = bx + c0 + 1  # convert chunk-local to arr coords
            ny = np.where(ch == 0, ay - 1, np.where(ch == 1, ay + 1, ay))
            nx = np.where(ch == 2, ax - 1, np.where(ch == 3, ax + 1, ax))
            arr[ay, ax] = arr[ny, nx]

    return arr


def build_lut() -> np.ndarray:
    """Build a 256-entry uint8 LUT: arbitrary value -> nearest valid zone code."""
    zones = np.array(VALID_ZONES, dtype=np.int32)
    lut = np.zeros(256, dtype=np.uint8)
    for v in range(256):
        diffs = np.abs(zones - v)
        lut[v] = zones[np.argmin(diffs)]
    return lut


def main():
    lut = build_lut()

    print(f"Loading {INPUT}...")
    img = Image.open(INPUT)
    # Accept RGBA or L
    orig_w, orig_h = img.size
    print(f"Source: {orig_w}x{orig_h}  mode={img.mode}")
    print(f"Target: {TARGET}x{TARGET}")

    scale = TARGET / orig_w  # nominal; actual scale applied via source crop above

    # No pre-blur: blurring discrete zone values (0,10,20,...,120,...) creates
    # intermediate values that snap to phantom biomes (e.g. FROZEN_FLATS appearing
    # at a COASTAL_HEATH/MIXED_FOREST boundary). Use the raw channel directly.
    src_ch  = img.split()[0] if img.mode in ("RGBA", "RGB") else img.convert("L")
    vec_arr = np.array(src_ch, dtype=np.uint8)

    # Composite: use vectorized smooth borders where non-zero,
    # fall back to override_final for zone interiors (correct filled areas).
    print(f"Loading base fill from {OVERRIDE_BASE.name}...")
    base_img = Image.open(OVERRIDE_BASE)
    base_ch  = base_img.split()[0] if base_img.mode in ("RGBA", "RGB") else base_img.convert("L")
    base_arr = np.array(base_ch, dtype=np.uint8)
    if base_arr.shape != vec_arr.shape:
        # Resize base to match source dimensions if they differ
        base_arr = np.array(
            Image.fromarray(base_arr).resize((orig_w, orig_h), Image.NEAREST),
            dtype=np.uint8)
    composite = np.where(vec_arr > 0, vec_arr, base_arr).astype(np.uint8)
    vec_px   = int((vec_arr  > 0).sum())
    base_px  = int((vec_arr == 0).sum())
    print(f"  composite: {vec_px:,} px from vectorized borders, {base_px:,} px from base fill")
    # Flip axes to match height.tif coordinate system
    # fliplr removed — backup source PNG is already in correct X orientation
    if FLIP_Z:
        composite = np.flipud(composite)

    # Apply scale: crop to effective source region so the override covers ALIGN_SCALE× more world
    if ALIGN_SCALE != 1.0:
        eff_px = int(round(orig_h / ALIGN_SCALE))
        composite = composite[:eff_px, :eff_px]
        print(f"Scale {ALIGN_SCALE}×: cropped source to {eff_px}×{eff_px} px")

    # Smooth zone boundaries via B-spline contour fitting at source resolution
    if CONTOUR_SMOOTH:
        print("Smoothing zone contours at source resolution...")
        composite = smooth_contours(composite, lut, s=CONTOUR_SMOOTH_S,
                                    min_pts=CONTOUR_MIN_PTS)

    # Jitter zone boundaries at source resolution (safe: only swaps to neighbour codes)
    if JITTER_PASSES > 0:
        print(f"Applying jitter ({JITTER_PASSES} passes, prob={JITTER_PROB})...")
        composite = apply_jitter(composite, JITTER_PASSES, JITTER_PROB, JITTER_SEED)

    src_h, src_w = composite.shape
    img = Image.fromarray(composite, mode="L")
    del vec_arr, base_arr, composite

    os.makedirs(OUTPUT.parent, exist_ok=True)

    # ── Phase 1: NEAREST upscale to intermediate resolution ────────────────
    inter = INTERMEDIATE_RES
    print(f"Phase 1: NEAREST upscale {src_w}->{inter}...")
    inter_img = img.resize((inter, inter), Image.NEAREST)
    if inter_img.mode in ("RGBA", "RGB"):
        inter_arr = np.array(inter_img.split()[0], dtype=np.uint8)
    else:
        inter_arr = np.array(inter_img.convert("L"), dtype=np.uint8)
    inter_arr = lut[inter_arr]
    del img
    print(f"  Intermediate array: {inter_arr.shape}")

    # ── Phase 2: Median filter at intermediate resolution ────────────────
    if MEDIAN_KERNEL > 1:
        print(f"Phase 2: Median filter at {inter} (kernel={MEDIAN_KERNEL})...")
        from scipy.ndimage import median_filter
        import time as _time
        _t0 = _time.time()
        inter_arr = median_filter(inter_arr, size=MEDIAN_KERNEL)
        inter_arr = lut[inter_arr]  # re-quantize
        print(f"  Median filter done in {_time.time()-_t0:.1f}s")

    # ── Phase 3: NEAREST upscale intermediate -> 50k (chunked, pure numpy) ──
    print(f"Phase 3: NEAREST upscale {inter}->{TARGET}...")
    # Build row/col index maps for NEAREST sampling
    row_idx = np.clip((np.arange(TARGET) * inter / TARGET).astype(np.int32), 0, inter - 1)
    col_idx = np.clip((np.arange(TARGET) * inter / TARGET).astype(np.int32), 0, inter - 1)
    full = np.empty((TARGET, TARGET), dtype=np.uint8)

    row_out = 0
    while row_out < TARGET:
        chunk_h = min(CHUNK_H, TARGET - row_out)
        # Gather rows from inter_arr using precomputed index, then index columns
        src_rows = inter_arr[row_idx[row_out:row_out + chunk_h]]  # (chunk_h, inter)
        full[row_out:row_out + chunk_h] = lut[src_rows[:, col_idx]]
        row_out += chunk_h
        pct = row_out / TARGET * 100
        print(f"  {pct:5.1f}%  (row {row_out}/{TARGET})", end="\r")

    del inter_arr, row_idx, col_idx
    import gc; gc.collect()
    print(f"\nUpscale to {TARGET} complete.")

    # ── Phase 4: Post-upscale jitter at 50k resolution ───────────────────────
    import gc; gc.collect()
    if POST_JITTER_PASSES > 0:
        print(f"Phase 4: Post-upscale jitter ({POST_JITTER_PASSES} passes, "
              f"prob={POST_JITTER_PROB})...")
        full = apply_jitter(full, POST_JITTER_PASSES, POST_JITTER_PROB,
                            POST_JITTER_SEED)
        # Re-quantize in-place (chunked to avoid OOM from lut[full] copy)
        for r0 in range(0, TARGET, CHUNK_H):
            r1 = min(r0 + CHUNK_H, TARGET)
            full[r0:r1] = lut[full[r0:r1]]

    # ── Phase 5: Write to BigTIFF ────────────────────────────────────────────
    print("Phase 5: Writing BigTIFF...")
    profile = dict(
        driver="GTiff",
        height=TARGET,
        width=TARGET,
        count=1,
        dtype=np.uint8,
        compress="deflate",
        tiled=True,
        blockxsize=512,
        blockysize=512,
        bigtiff="YES",
    )

    with rasterio.open(OUTPUT, "w", **profile) as dst:
        row_out = 0
        while row_out < TARGET:
            chunk_h = min(CHUNK_H, TARGET - row_out)
            window = Window(col_off=0, row_off=row_out, width=TARGET, height=chunk_h)
            dst.write(full[row_out:row_out + chunk_h][np.newaxis, :, :], window=window)
            row_out += chunk_h
            pct = row_out / TARGET * 100
            print(f"  {pct:5.1f}%  (row {row_out}/{TARGET})", end="\r")

    size_mb = OUTPUT.stat().st_size / 1024 / 1024
    print(f"\nDone.  {OUTPUT}  ({size_mb:.1f} MB)")

    # Quick sanity: sample a few regions and report unique zone values found
    print("\nSanity check (unique zone values per sample region):")
    with rasterio.open(OUTPUT) as src:
        for (col, row, label) in [
            (5000,  5000,  "NW land"),
            (25000, 25000, "center"),
            (40000, 40000, "SE land"),
            (10000,  1000, "top edge"),
        ]:
            w = Window(col, row, 256, 256)
            tile = src.read(1, window=w)
            vals = sorted(np.unique(tile).tolist())
            print(f"  ({col:>5},{row:>5}) [{label}]: {vals}")


if __name__ == "__main__":
    main()
