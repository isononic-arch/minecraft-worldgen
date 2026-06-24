"""
derive_masks_from_height.py — derive Gaea-equivalent input masks from a real DEM
================================================================================
Part of the ISLAND-EXPANSION track (offset-render real-world islands into Vandir
without running Gaea).  A real DEM (SRTM 30m etc.) is ALREADY eroded, so Gaea's
one unique value — its hydraulic-erosion sim — is redundant.  Everything the
tile pipeline needs as a *primary* input can be derived from the heightfield:

    height.tif   raw uint16 in Vandir's world convention (raw->MC-Y via terrain_spline)
    slope.tif    uint16, TRUE MONOTONE slope_norm*65535  (NOT Gaea's band-pass field)
    flow.tif     uint16, log-scaled D8 flow accumulation
    erosion.tif  uint16, curvature x flow proxy  (one consumer: block_mixing threshold)

The remaining masks (hydro_*, rock_gap, snow_gap, windthrow, beach, dunes, ...)
are then produced by the EXISTING hydrology_precompute + rebuild_* scripts run
against these four, and override/lithology are hand-painted.  This module is the
first stage of that chain.

WHY true-monotone slope (not a Gaea match): masks/slope.tif is a BAND-PASS field
(peaks at moderate slope, low on flats AND cliffs — see diag_slope_calib/FINDINGS.md).
Its three consumers (column_generator stone/grass + snow line, hydrology lake-flat
detection) all want monotone "steeper = higher", so we emit
slope_norm = clip(true_slope_deg / SLOPE_NORM_DEG, 0, 1) — the same convention
eco_gradients.compute_cliff_deg uses.  Existing [0,1] thresholds then fire sensibly.

METHODOLOGY: compute slope/flow at the DEM's NATIVE resolution (where the real
detail lives, no upscale ringing), THEN upscale the derived mask to 1 px/block —
mirroring how Gaea computed-then-upscaled.

Usage:
    py derive_masks_from_height.py --dem path/to/island.png \
        --out-dir masks_islands/st_kitts \
        --footprint-blocks 3200 \
        --elev-range 0 1156 \
        --vertical Y63 Y520 \
        --offset 9000 23000          # world-pixel top-left of the island region
    py derive_masks_from_height.py --self-test
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

# ── world / spline constants (mirror config/thresholds.json terrain_spline) ──
SEA_LEVEL_RAW16 = 17050          # raw uint16 at MC Y=63
SLOPE_NORM_DEG = 45.0            # deg mapped to slope_norm 1.0 (eco_gradients convention)

# live 13-pt monotone terrain spline: world raw uint16 <-> MC-Y (blocks = metres)
_GIN = np.array([0, 5000, 12000, 17050, 18000, 21000, 26000, 30000, 35000,
                 42000, 50000, 58000, 65496], dtype=np.float64)
_YOUT = np.array([-64, -45, 25, 63, 67, 78, 110, 145, 180, 360, 490, 610, 700],
                 dtype=np.float64)

# D8 (index -> row,col offset): 0=N 1=NE 2=E 3=SE 4=S 5=SW 6=W 7=NW
D8_DR = np.array([-1, -1, 0, 1, 1, 1, 0, -1], dtype=np.int64)
D8_DC = np.array([0, 1, 1, 1, 0, -1, -1, -1], dtype=np.int64)
D8_DIST = np.array([1, 1.4142, 1, 1.4142, 1, 1.4142, 1, 1.4142], dtype=np.float64)


def _log(m: str) -> None:
    print(f"[derive] {m}", file=sys.stderr, flush=True)


# ═══════════════════════════════════════════════════════════════════════════
# Height: DEM real metres -> intended MC-Y -> world raw uint16
# ═══════════════════════════════════════════════════════════════════════════

def dem_to_metres(dem: np.ndarray, elev_lo_m: float, elev_hi_m: float) -> np.ndarray:
    """Deprecated min/max mapping — kept for reference. Use dem_to_mcy."""
    d = dem.astype(np.float64)
    dmin, dmax = float(d.min()), float(d.max())
    if dmax <= dmin:
        return np.full_like(d, elev_lo_m)
    return elev_lo_m + (d - dmin) / (dmax - dmin) * (elev_hi_m - elev_lo_m)


def reconstruct_clipped_peaks(dem: np.ndarray, sea_raw: float, frac: float = 0.12,
                              min_px: int = 40, noise_frac: float = 0.0):
    """Downloaded DEMs SATURATE summits at the 16-bit ceiling (radar void filled
    flat) -> a flat tabletop.  Detect each clipped plateau and either DOME it
    (frac>0: rise rim->apex, restores a volcanic peak) and/or roughen it
    (noise_frac>0: low-amplitude relief, for flat coral/atoll caps that should be
    near-flat but not a perfect saturated plane).  Returns (dem, n_pixels_fixed)."""
    from scipy.ndimage import label, distance_transform_edt, gaussian_filter
    d = dem.astype(np.float64)
    mx = float(d.max())
    clipped = d >= mx - 1.0
    if int(clipped.sum()) < min_px:
        return dem, 0
    lbl, n = label(clipped)
    out = d.copy(); fixed = 0
    amp = frac * (mx - sea_raw)
    for i in range(1, n + 1):
        m = lbl == i
        if int(m.sum()) < min_px:
            continue
        dist = distance_transform_edt(m)          # 0 at rim -> max at plateau centre
        dm = float(dist.max())
        if dm < 2:
            continue
        out[m] = mx + amp * (dist[m] / dm)        # dome: rim=mx, apex=mx+amp (flat if frac=0)
        fixed += int(m.sum())
    if fixed:
        out = np.where(clipped, gaussian_filter(out, 2.0), out)   # soften
        if noise_frac > 0:                        # gentle natural relief on flat caps
            rng = np.random.default_rng(12345)
            cm = clipped
            out[cm] += rng.normal(0.0, noise_frac * (mx - sea_raw), int(cm.sum()))
    return out, fixed


def clean_edge_fragments(dem: np.ndarray, sea_raw: float, max_frac: float = 0.05):
    """Remove small land components that TOUCH the frame border — slivers of a
    neighbouring island accidentally caught at the export edge — by setting them
    to sea level.  Interior islets (real archipelago members) are kept.  Returns
    (dem, n_pixels_removed)."""
    from scipy.ndimage import label
    d = dem.astype(np.float64)
    land = d > sea_raw + (float(d.max()) - sea_raw) * 0.02
    lbl, n = label(land)
    if n == 0:
        return dem, 0
    sizes = np.bincount(lbl.ravel())
    largest = int(sizes[1:].max())
    border = set(lbl[0, :]) | set(lbl[-1, :]) | set(lbl[:, 0]) | set(lbl[:, -1])
    out = d.copy(); removed = 0
    for i in border:
        if i != 0 and sizes[i] < max_frac * largest:
            out[lbl == i] = sea_raw; removed += int(sizes[i])
    return out, removed


def detect_sea_raw(dem: np.ndarray) -> float:
    """Real-world island DEMs clamp the ocean to one dominant low value, NOT the
    array minimum (which may be deep-bathymetry/nodata outliers).  Return the
    EXACT modal value of the low 60% (not a histogram bin center — a center
    slightly below the true ocean value leaks ocean above sea level)."""
    lo = np.round(dem[dem <= np.percentile(dem, 60)]).astype(np.int64)
    vals, counts = np.unique(lo, return_counts=True)
    return float(vals[counts.argmax()])


def dem_to_mcy(dem: np.ndarray, *, sea_raw: float, mcy_sea: float,
               mcy_peak: float, coast_eps: float = 0.005,
               coast_frac: float = 0.06, coast_rise: float = 0.015,
               gamma: float = 2.2,
               curve: tuple | None = None) -> np.ndarray:
    """Per-island vertical map: DEM raw -> MC-Y blocks, anchored on the detected
    sea-level raw value (-> mcy_sea) and the DEM max (-> mcy_peak).

    HAND-EDITED SPLINE: if ``curve=(fracs, mcys)`` is given (from
    islands/island_spline_studio.py via spline_overrides.json), it REPLACES the
    parametric coast_frac/coast_rise/gamma shape — the land map is a direct
    interpolation of the user's breakpoints in (elevation-fraction -> MC-Y) space.
    The below-sea linear shelf and the coastal sea-snap are preserved either way.

    COASTAL-FLATTENING profile (replaces the old linear ramp, which made the
    whole coast a ~20-block incline and over-flagged the flanks as rock).
    Piecewise on frac = (raw - sea_raw) / (peak - sea_raw):
      [0, coast_frac]  ->  [0, coast_rise]  LINEAR  (a near-flat beach/coastal
                            plain: e.g. coast_frac 0.06 of relief rises only
                            coast_rise 1.5% of total height = a real beach)
      [coast_frac, 1]  ->  [coast_rise, 1]  GAMMA   (gentle mid-flank, sharp
                            uptick to the summit; tune gamma 1.8 gentle .. 2.6
                            flat-flank+spiky-peak, per island)
    Below-sea raw stays LINEAR below mcy_sea so the seabed apron is unaffected.
    A thin coastal band (within coast_eps above sea) is SNAPPED to exactly sea
    level so the ocean lands at raw<=sea and the pipeline treats it as ocean."""
    d = dem.astype(np.float64)
    raw_peak = float(d.max())
    span = max(raw_peak - sea_raw, 1.0)
    frac = (d - sea_raw) / span                 # 0 at sea, 1 at peak, <0 below sea
    fp = np.clip(frac, 0.0, 1.0)
    if curve is not None:                        # hand-edited breakpoints win
        cf_fr = np.asarray(curve[0], dtype=np.float64)
        cf_my = np.asarray(curve[1], dtype=np.float64)
        mcy = np.interp(fp, cf_fr, cf_my)        # land shape from the user's curve
        peak_y = float(cf_my[-1])
        mcy = np.where(frac <= 0.0, mcy_sea + frac * (peak_y - mcy_sea), mcy)
        return np.where(frac <= coast_eps, np.minimum(mcy, mcy_sea), mcy)
    cf = max(coast_frac, 1e-3)
    shelf = (fp / cf) * coast_rise                                  # near-flat beach band
    t = np.clip((fp - cf) / max(1.0 - cf, 1e-3), 0.0, 1.0)
    upper = coast_rise + (1.0 - coast_rise) * (t ** max(gamma, 1e-3))
    shape = np.where(fp <= cf, shelf, upper)
    mcy = mcy_sea + shape * (mcy_peak - mcy_sea)
    mcy = np.where(frac <= 0.0, mcy_sea + frac * (mcy_peak - mcy_sea), mcy)   # below-sea linear shelf
    return np.where(frac <= coast_eps, np.minimum(mcy, mcy_sea), mcy)


def mcy_to_world_raw(mcy: np.ndarray) -> np.ndarray:
    """Invert the world terrain_spline: intended MC-Y -> raw uint16, so the tile
    pipeline's raw->MC-Y read reproduces the intended elevation."""
    raw = np.interp(mcy, _YOUT, _GIN)   # _YOUT monotone increasing -> invertible
    return np.clip(np.round(raw), 0, 65535).astype(np.uint16)


_SEABED_PATCH = Path(__file__).resolve().parent / "islands" / "cache" / "vandir_seabed_patch.npy"


def load_seabed_texture():
    """Vandir deep-ocean relief as a UNIT-STD high-frequency texture (submarine
    canyons), detrended of its large-scale slope.  None if the patch is missing
    (run islands/extract_seabed_patch.py)."""
    if not _SEABED_PATCH.exists():
        return None
    from scipy.ndimage import gaussian_filter
    m = np.interp(np.load(_SEABED_PATCH).astype(float), _GIN, _YOUT)
    tex = m - gaussian_filter(m, 60)        # remove the shelf ramp -> just canyon bumps
    s = float(tex.std())
    return tex / s if s > 1e-6 else None


def apply_seabed_transplant(mcy, mcy_sea, texture, base_depth, amp, apron_px):
    """Replace the island's flat ocean floor with Vandir-derived gently-canyoned
    seabed: base_depth + amp*texture in open water, tapering up a coastal apron to
    the shoreline.  base_depth ~ -60 matches both Vandir's deep ocean AND the
    noise-ocean filler (-60 +-4), so the rim blends seamlessly."""
    from scipy.ndimage import distance_transform_edt
    ocean = mcy <= mcy_sea
    if not ocean.any():
        return mcy
    dist = distance_transform_edt(ocean)                 # px into ocean from land
    H, W = mcy.shape; th, tw = texture.shape
    yy, xx = np.mgrid[0:H, 0:W]
    tex = np.clip(texture[yy % th, xx % tw], -2.0, 2.0)  # bound canyon tails
    deep = base_depth + amp * tex
    t = np.clip(dist / max(apron_px, 1), 0.0, 1.0)       # 0 at shore -> 1 open water
    seabed = (1 - t) * (mcy_sea - 2.0) + t * deep        # shelf rises to waterline
    seabed = np.clip(seabed, -63.0, mcy_sea - 1.0)       # never below bedrock / above sea
    out = mcy.copy(); out[ocean] = seabed[ocean]
    return out


# ═══════════════════════════════════════════════════════════════════════════
# Slope: TRUE monotone, in-world rise/run
# ═══════════════════════════════════════════════════════════════════════════

def compute_slope_norm(mcy: np.ndarray, hstep_blocks: float) -> np.ndarray:
    """True physical slope as slope_norm in [0,1].  Rise = d(MC-Y) blocks,
    run = hstep_blocks (world blocks per DEM pixel).  Normalized clip(deg/45,0,1)
    to match eco_gradients.compute_cliff_deg."""
    gy, gx = np.gradient(mcy.astype(np.float64), hstep_blocks)
    deg = np.degrees(np.arctan(np.hypot(gx, gy)))
    return np.clip(deg / SLOPE_NORM_DEG, 0.0, 1.0)


# ═══════════════════════════════════════════════════════════════════════════
# Flow: D8 accumulation (topological, Kahn-by-elevation)
# ═══════════════════════════════════════════════════════════════════════════

def compute_d8(mcy: np.ndarray) -> np.ndarray:
    """Steepest-descent D8 direction (0-7) per cell, -1 for a pit/edge sink."""
    H, W = mcy.shape
    best = np.full((H, W), -1e-6, dtype=np.float64)   # require a real drop
    bdir = np.full((H, W), -1, dtype=np.int64)
    for i in range(8):
        dr, dc = int(D8_DR[i]), int(D8_DC[i])
        nb = np.full((H, W), np.nan)
        sr = slice(max(0, -dr), H - max(0, dr)); sc = slice(max(0, -dc), W - max(0, dc))
        tr = slice(max(0, dr), H - max(0, -dr)); tc = slice(max(0, dc), W - max(0, -dc))
        nb[sr, sc] = mcy[tr, tc]
        s = (mcy - nb) / D8_DIST[i]
        m = (s > best) & ~np.isnan(s)
        best[m] = s[m]; bdir[m] = i
    return bdir


def route_pits(bdir: np.ndarray, mcy: np.ndarray, sea_y: float) -> np.ndarray:
    """Route land pits (bdir==-1, above sea) to their lowest neighbour so flow
    reaches the coast instead of orphaning.  Below-sea cells stay sinks."""
    H, W = bdir.shape
    pr, pc = np.where((bdir == -1) & (mcy > sea_y))
    for r, c in zip(pr.tolist(), pc.tolist()):
        bh, bd = np.inf, -1
        for i in range(8):
            nr, nc = r + int(D8_DR[i]), c + int(D8_DC[i])
            if 0 <= nr < H and 0 <= nc < W and mcy[nr, nc] < bh:
                bh, bd = mcy[nr, nc], i
        if bd >= 0:
            bdir[r, c] = bd
    return bdir


def compute_flow_accum(mcy: np.ndarray, sea_y: float) -> np.ndarray:
    """D8 flow accumulation (cell count draining through each cell), processed
    high-elevation -> low so every upstream cell is added before its outlet."""
    H, W = mcy.shape
    bdir = route_pits(compute_d8(mcy), mcy, sea_y)
    acc = np.ones((H, W), dtype=np.float64)
    order = np.argsort(mcy.ravel())[::-1]            # high -> low
    dr = D8_DR; dc = D8_DC
    for flat in order.tolist():
        r, c = divmod(flat, W)
        d = bdir[r, c]
        if d < 0:
            continue
        nr, nc = r + int(dr[d]), c + int(dc[d])
        if 0 <= nr < H and 0 <= nc < W:
            acc[nr, nc] += acc[r, c]
    return acc


def flow_to_uint16(acc: np.ndarray) -> np.ndarray:
    """Log-scale accumulation to uint16 (consumers are percentile/relative, so
    only the monotone ordering matters)."""
    la = np.log1p(acc)
    la /= (la.max() + 1e-9)
    return np.clip(np.round(la * 65535), 0, 65535).astype(np.uint16)


# ═══════════════════════════════════════════════════════════════════════════
# Erosion proxy: curvature x flow (incised valleys read high)
# ═══════════════════════════════════════════════════════════════════════════

def compute_erosion_proxy(mcy: np.ndarray, acc: np.ndarray) -> np.ndarray:
    """Concave-and-wet cells (valley incision) read high.  Single consumer is a
    threshold in column_generator block_mixing, so a monotone proxy suffices."""
    lap = (np.gradient(np.gradient(mcy, axis=0), axis=0)
           + np.gradient(np.gradient(mcy, axis=1), axis=1))
    concave = np.clip(lap, 0, None)                  # basins/channels only
    wet = np.log1p(acc)
    e = concave * wet
    pos = e[e > 0]
    if pos.size:
        e = np.clip(e, 0.0, float(np.percentile(pos, 90)))   # only deepest channels read "eroded"
    if e.max() > 0:
        e /= e.max()
    return np.clip(np.round(e * 65535), 0, 65535).astype(np.uint16)


# ═══════════════════════════════════════════════════════════════════════════
# Upscale helper (native -> block resolution)
# ═══════════════════════════════════════════════════════════════════════════

def upscale_bilinear(a: np.ndarray, out_hw: tuple[int, int]) -> np.ndarray:
    from scipy.ndimage import zoom
    zy = out_hw[0] / a.shape[0]; zx = out_hw[1] / a.shape[1]
    return zoom(a.astype(np.float64), (zy, zx), order=1)


# ═══════════════════════════════════════════════════════════════════════════
# DEM loading
# ═══════════════════════════════════════════════════════════════════════════

def load_dem(path: Path) -> np.ndarray:
    if path.suffix.lower() in (".tif", ".tiff"):
        import rasterio
        with rasterio.open(str(path)) as src:
            return src.read(1).astype(np.float64)
    from PIL import Image
    im = Image.open(str(path))
    if im.mode not in ("I", "I;16", "L", "F"):
        im = im.convert("I")
    return np.asarray(im, dtype=np.float64)


# ═══════════════════════════════════════════════════════════════════════════
# Driver
# ═══════════════════════════════════════════════════════════════════════════

def derive(dem: np.ndarray, *, footprint_blocks: int, mcy_sea: float,
           mcy_peak: float, sea_raw: float | None = None,
           native_cap: int | None = None, seabed: str = "vandir",
           seabed_base: float = -60.0, seabed_amp: float = 5.0,
           seabed_apron_px: float = 40.0, declip: bool = True,
           declip_frac: float = 0.12, clean_fragments: bool = True,
           coast_frac: float = 0.06, coast_rise: float = 0.015,
           gamma: float = 2.2, curve: tuple | None = None):
    """Returns dict of uint16 masks {height, slope, flow, erosion} at
    footprint_blocks x (footprint_blocks*aspect).  Compute is done at the DEM's
    native resolution (capped at native_cap on the long side to match real SRTM
    detail and bound the flow-loop cost), then upscaled to block resolution.
    seabed='vandir' transplants Vandir-derived canyon texture under the ocean
    (base_depth+-amp, blends to -60 noise-ocean); 'flat' keeps the bare shelf."""
    if sea_raw is None:
        sea_raw = detect_sea_raw(dem)           # measure on the crisp full-res DEM
    if clean_fragments:
        dem, nrm = clean_edge_fragments(dem, sea_raw)
        if nrm:
            _log(f"cleaned {nrm} px of frame-edge land slivers")
    if declip:
        dem, nfix = reconstruct_clipped_peaks(dem, sea_raw, frac=declip_frac)
        if nfix:
            _log(f"de-clip: domed {nfix} clipped summit px back into peak(s)")
    else:
        dem, nfix = reconstruct_clipped_peaks(dem, sea_raw, frac=0.0, noise_frac=0.015)
        if nfix:
            _log(f"de-clip: roughened {nfix} flat clipped cap px (no dome)")
    if native_cap and max(dem.shape) > native_cap:
        from scipy.ndimage import zoom
        f = native_cap / max(dem.shape)
        dem = zoom(dem.astype(np.float64), f, order=1)
        _log(f"downsampled DEM to {dem.shape[1]}x{dem.shape[0]} (native_cap={native_cap})")

    dh, dw = dem.shape
    mcy = dem_to_mcy(dem, sea_raw=sea_raw, mcy_sea=mcy_sea, mcy_peak=mcy_peak,
                     coast_frac=coast_frac, coast_rise=coast_rise, gamma=gamma,
                     curve=curve)

    if seabed == "vandir":
        tex = load_seabed_texture()
        if tex is not None:
            mcy = apply_seabed_transplant(mcy, mcy_sea, tex, seabed_base, seabed_amp, seabed_apron_px)
            _log(f"seabed transplant ON (base={seabed_base} amp={seabed_amp} apron={seabed_apron_px}px)")
        else:
            _log("seabed=vandir but no patch -> run islands/extract_seabed_patch.py; keeping flat")

    # in-world horizontal step: world blocks per DEM pixel (longest side -> footprint)
    hstep_blocks = footprint_blocks / max(dh, dw)
    _log(f"DEM {dw}x{dh}  sea_raw={sea_raw:.0f}  hstep={hstep_blocks:.3f} blocks/px  "
         f"MC-Y range {mcy.min():.0f}..{mcy.max():.0f}  "
         f"land(>Y63)={float(np.mean(mcy > mcy_sea))*100:.1f}%")

    slope_n = compute_slope_norm(mcy, hstep_blocks)        # native res
    acc = compute_flow_accum(mcy, sea_y=mcy_sea)
    acc[mcy <= mcy_sea] = 0.0      # ocean flow is unused (hydrology gates on land) + noisy -> zero it
    ero = compute_erosion_proxy(mcy, acc)

    out_hw = (round(dh * hstep_blocks), round(dw * hstep_blocks))
    height_raw = mcy_to_world_raw(mcy)
    out = {
        "height": np.clip(np.round(upscale_bilinear(height_raw, out_hw)), 0, 65535).astype(np.uint16),
        "slope": np.clip(np.round(upscale_bilinear(slope_n, out_hw) * 65535), 0, 65535).astype(np.uint16),
        "flow": np.clip(np.round(upscale_bilinear(flow_to_uint16(acc), out_hw)), 0, 65535).astype(np.uint16),
        "erosion": np.clip(np.round(upscale_bilinear(ero, out_hw)), 0, 65535).astype(np.uint16),
    }
    return out, out_hw


def write_masks(out: dict, out_dir: Path, out_hw, offset, footprint_blocks,
                meta_extra: dict | None = None) -> None:
    import rasterio
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, arr in out.items():
        prof = dict(driver="GTiff", height=arr.shape[0], width=arr.shape[1],
                    count=1, dtype=arr.dtype, compress="deflate")
        with rasterio.open(str(out_dir / f"{name}.tif"), "w", **prof) as dst:
            dst.write(arr, 1)
    manifest = {
        "out_hw": list(out_hw),
        "world_offset_px": list(offset) if offset else None,
        "footprint_blocks": footprint_blocks,
        "masks": list(out.keys()),
        "note": "offset-render: read at world_offset_px; slope is TRUE monotone "
                "(clip(deg/45,0,1)*65535), NOT Gaea band-pass.",
    }
    if meta_extra:
        manifest.update(meta_extra)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    _log(f"wrote {len(out)} masks + manifest.json to {out_dir}  ({out_hw[1]}x{out_hw[0]})")


# ═══════════════════════════════════════════════════════════════════════════
# Self-test: synthetic volcanic cone
# ═══════════════════════════════════════════════════════════════════════════

def _synthetic_cone(n=256, peak=1.0):
    """Realistic stratovolcano: raised-cosine bell -> gentle summit, steep
    mid-flank (r~0.5), gentle base.  (A pure (1-r)^k cone is steepest at the tip
    and would not exhibit flank>summit slope.)"""
    yy, xx = np.mgrid[0:n, 0:n].astype(np.float64)
    cx = cy = (n - 1) / 2
    r = np.clip(np.hypot(xx - cx, yy - cy) / (n / 2), 0, 1)
    bell = 0.5 * (1.0 + np.cos(np.pi * r))            # 1 at summit, 0 at rim
    # radial gullies so flow has somewhere to accumulate
    theta = np.arctan2(yy - cy, xx - cx)
    bell -= 0.04 * np.clip(np.cos(8 * theta), 0, 1) * (1.0 - r)
    return (np.clip(bell, 0, 1) * peak * 1156).astype(np.float64)  # metres, ~Liamuiga


def self_test() -> int:
    _log("SELF-TEST: synthetic volcanic cone")
    dem = _synthetic_cone()
    out, out_hw = derive(dem, footprint_blocks=3200, mcy_sea=63, mcy_peak=520, seabed="flat")
    H = out["height"].astype(np.float64); S = out["slope"]; F = out["flow"]
    n = out_hw[0]; cy = cx = n // 2

    # geometry helpers
    yy, xx = np.mgrid[0:n, 0:n].astype(float)
    rad = np.hypot(xx - cx, yy - cy) / (n / 2)
    summit = rad < 0.10
    flank = (rad > 0.35) & (rad < 0.65)
    base = rad > 0.92

    ok = True
    def check(name, cond):
        nonlocal ok
        print(("  PASS " if cond else "  FAIL ") + name)
        ok = ok and cond

    # 1. height: sea anchored ~Y63 at the rim, peak near summit
    mcy_summit = np.interp(np.median(H[summit]), _GIN, _YOUT)
    mcy_base = np.interp(np.median(H[base]), _GIN, _YOUT)
    print(f"   summit MC-Y~{mcy_summit:.0f}  base MC-Y~{mcy_base:.0f}")
    check("summit higher than base", mcy_summit > mcy_base + 200)
    check("base near sea level (|MC-Y-63|<40)", abs(mcy_base - 63) < 40)

    # 2. slope: flanks steeper than the (near-flat) summit and the base shelf
    s_sum, s_flank, s_base = np.median(S[summit]), np.median(S[flank]), np.median(S[base])
    print(f"   slope summit/flank/base = {s_sum:.0f}/{s_flank:.0f}/{s_base:.0f}")
    check("flank slope > summit slope", s_flank > s_sum)
    check("flank slope > base slope", s_flank > s_base)

    # 3. flow: accumulates downslope -> outer ring carries more than the summit
    f_sum, f_flank = np.median(F[summit]), np.percentile(F[flank], 95)
    print(f"   flow summit_med={f_sum:.0f}  flank_p95={f_flank:.0f}")
    check("flow accumulates below summit", f_flank > f_sum)

    print("\nSELF-TEST: " + ("ALL PASS" if ok else "FAILURES"))
    return 0 if ok else 1


# ═══════════════════════════════════════════════════════════════════════════

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dem", type=Path, help="DEM PNG (16-bit) or GeoTIFF")
    ap.add_argument("--out-dir", type=Path, help="island mask output dir")
    ap.add_argument("--footprint-blocks", type=int, default=3200,
                    help="world-block size of the DEM frame's longest side")
    ap.add_argument("--vertical", nargs=2, metavar=("MCY_SEA", "MCY_PEAK"),
                    default=["63", "520"], help="MC-Y at sea and at the DEM peak "
                    "(exaggeration = choose MCY_PEAK; real peak metres go in --peak-metres for the manifest)")
    ap.add_argument("--sea-raw", type=float, default=None,
                    help="DEM raw value at sea level (auto-detected as the modal low if omitted)")
    ap.add_argument("--peak-metres", type=float, default=None,
                    help="real-world metres at the DEM max (manifest only; informational)")
    ap.add_argument("--native-cap", type=int, default=2048,
                    help="downsample DEM long side to <= this before compute (SRTM ~1500px real detail)")
    ap.add_argument("--seabed", choices=["vandir", "flat"], default="vandir",
                    help="vandir = transplant Vandir canyon texture under the ocean (toggle); flat = bare shelf")
    ap.add_argument("--seabed-base", type=float, default=-60.0, help="mean ocean-floor MC-Y (matches noise-ocean)")
    ap.add_argument("--seabed-amp", type=float, default=5.0, help="seabed bump amplitude (blocks)")
    ap.add_argument("--declip", choices=["on", "off"], default="on",
                    help="dome clipped/saturated summit plateaus back into peaks (toggle)")
    ap.add_argument("--declip-frac", type=float, default=0.12,
                    help="reconstructed peak height as a fraction of island relief")
    ap.add_argument("--offset", type=int, nargs=2, metavar=("X", "Y"),
                    help="world top-left pixel for the offset-render (recorded in manifest)")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        return self_test()
    if not a.dem or not a.out_dir:
        ap.error("--dem and --out-dir required (or use --self-test)")

    dem = load_dem(a.dem)
    out, out_hw = derive(
        dem, footprint_blocks=a.footprint_blocks,
        mcy_sea=float(a.vertical[0]), mcy_peak=float(a.vertical[1]),
        sea_raw=a.sea_raw, native_cap=a.native_cap,
        seabed=a.seabed, seabed_base=a.seabed_base, seabed_amp=a.seabed_amp,
        declip=(a.declip == "on"), declip_frac=a.declip_frac)
    write_masks(out, a.out_dir, out_hw, a.offset, a.footprint_blocks,
                meta_extra={"source_dem": str(a.dem),
                            "vertical_mcy": [float(a.vertical[0]), float(a.vertical[1])],
                            "sea_raw": a.sea_raw, "peak_metres": a.peak_metres,
                            "native_cap": a.native_cap, "seabed": a.seabed,
                            "seabed_base": a.seabed_base, "seabed_amp": a.seabed_amp,
                            "declip": a.declip})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
