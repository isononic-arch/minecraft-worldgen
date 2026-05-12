# S83 v17 Handoff — Painted Rivers FINAL

**Status:** v17 user verdict: **"it's perfect."** Pipeline is ready for full-world 50k regen.

## What v17 Is

Painted rivers (`masks/hydro_region.png` id=2) carve naturalistic, lake-bowl-style basins driven by:

1. **Power-curve carve** (`core/hydro_region_overlay.py`): `carve_depth = _DEPTH_POWER_SCALE × sdf_blocks^_DEPTH_POWER_EXPONENT` with SCALE=2.0, POWER=0.7. No plateau. No hard cap. Soft sub-linear so wide rivers don't blow up.

2. **Smoothing pipeline** (in `core/hydro_region_overlay.py` `_ensure_caches`):
   - σ=4 weighted gaussian on bed inside footprint (the legacy seed pass).
   - 5-pass σ=2 weighted gaussian inside footprint (smooth-brush ×5).
   - Geomorph: thalweg asymmetry ±2.5b, bedform ±1.2b/λ=30, riffle-pool ±2.5b/λ=250 (skeleton-based arclength).
   - Bank asymmetry (bbox-optimized): point-bar σ=2.5, cut-bank σ=0.0 (= no smoothing → sharp cliff), 6-cell ring.
   - σ=4 unmasked melt gaussian (bbox-optimized).

3. **Tile-level passes** (`run_pipeline.py`, gated `if _paint_river_pad.any()`):
   - **Carve completion (Pass 0):** lower surface_y where water_y > SEA & surface ≥ water_y — eliminates "wall in middle of water" artifacts. v17 typically hits ~17,000 cells.
   - **Bed melt at 50k (Pass 0.25):** weighted σ=2 gauss at water cells, clamped ≤ water_y−1. Catches narrow channels invisible to 8k cache. ~14,000-18,000 cells per tile.
   - **Bank smoothing (Pass 0.5):** σ=4 gauss on surface_y at bank cells within 12 blocks of water, clamps preserve natural silhouette. ~11,000-13,000 cells per tile.
   - Then v8.6 iterative escape-fix, v8.8 EDT berm, v8.14 cap.

## All Tunables (Module-Level in `core/hydro_region_overlay.py`)

```python
# Depth profile (v17, replaces v15's smoothstep + linear-past-plateau)
_DEPTH_POWER_SCALE = 2.0       # depth = SCALE × sdf^POWER
_DEPTH_POWER_EXPONENT = 0.7    # sublinear (Hack's law-ish)

# Legacy compatibility (unused by v17 carve, kept for legacy code paths)
_CARVE_MAX_DEPTH = 6.0
_CARVE_SOFTNESS = 3.0
_CARVE_INWARD_BIAS = 4.0
_DEPTH_LINEAR_RATE = 0.5

# Bed cache smoothing pipeline
_RIVER_BED_GAUSS_SIGMA_8K = 4.0       # initial weighted gauss (legacy seed pass)
_RIVER_TROUGH_SMOOTH_PASSES = 5       # WorldEdit smooth-brush x5
_RIVER_TROUGH_SMOOTH_SIGMA_8K = 2.0   # per-pass sigma
_MELT_GAUSSIAN_SIGMA_8K = 4.0         # final unmasked melt at 8k

# Geomorph
_THALWEG_AMP_BLOCKS = 2.5
_THALWEG_SCALE = 25.0
_THALWEG_SIGN = +1.0
_BEDFORM_AMP_BLOCKS = 1.2
_BEDFORM_WAVELEN_BLOCKS = 30.0
_RIFFLE_AMP_BLOCKS = 2.5
_RIFFLE_WAVELEN_BLOCKS = 250.0

# Bank asymmetry (bbox-optimized in _apply_asymmetric_bank_smoothing_8k)
_S83_V12_BANK_ASYM_ENABLED = True
_BANK_ASYM_SIGMA_POINTBAR_8K = 2.5
_BANK_ASYM_SIGMA_CUTBANK_8K = 0.0
_BANK_ASYM_RING_RADIUS_8K = 6
_BANK_ASYM_SIGN = +1.0

# Trough widening
_TROUGH_EXPAND_BIAS_8K = 1.0           # widens 8k footprint vs 50k carver

# Legacy bank ring (used if asymmetry gated off)
_BANK_RING_RADIUS_8K = 2
_BANK_RING_GAUSS_SIGMA_8K = 0.7
```

In `run_pipeline.py`:
```python
_BANK_SMOOTH_SIGMA = 4.0              # bank-above-water smoothing sigma
_BANK_SMOOTH_RADIUS_BLOCKS = 12
_BED_MELT_SIGMA_50K = 2.0             # 50k bed_melt weighted sigma
```

## The v9→v17 Progression (for context)

| Version | Change | User verdict |
|---|---|---|
| v8c3 | 5-pass smooth + stochastic rounding | "90% perfect" checkpoint |
| v11 | 5-pass smooth + bank ring, np.minimum clamp | "Wall with water on both sides" — escape-fix wall placed at narrow boundary |
| v12 | Thalweg + bedform + riffle (8k bed cache, polygon-based geometry) | Carve completion fix (17k dry-bed-in-water cells) made the wall track final water extent |
| v13 | Depth halved 12→6, riffle/bedform amplitudes bumped, bank asym + bbox enabled | "Floor flattened aggressively, slope-down not smooth, looks melted-but-flat" |
| v14 | (reverted) SOFTNESS 6, bowl bonus — Walls smoother but floor still flat | User: "Undo this" |
| v15 | Uncap depth (linear past plateau), σ=4 melt gaussian, σ=4 bank smoothing | "Trench, not bowl. Match how depth works in the lake." |
| v16 | 50k bed_melt pass σ=4 weighted at water cells | "Now it's a trench, should be a bowl" — too much smoothing was killing bowl variation in narrow channels |
| **v17** | **Power-curve carve (replaces smoothstep + plateau), σ=2 bed_melt to preserve variation** | **"it's perfect."** |

## Active Code (Modified Files)

- `core/hydro_region_overlay.py` — power-curve carve, bbox-optimized bank-asym, 8k melt gaussian, geomorph (thalweg/bedform/riffle via skeleton arclength + polygon curvature)
- `core/river_carver_v2.py` — np.minimum clamp on bed override (only lower terrain, never raise)
- `run_pipeline.py` — three new passes in escape-fix block, all gated on `_paint_river_pad.any()`:
  - PASS 0: carve completion (~lines 678-720)
  - PASS 0.25: bed melt at 50k (~lines 721-790)
  - PASS 0.5: bank smoothing (~lines 791-860)
  - Then existing v8.6 escape-fix, v8.8 EDT berm, v8.14 cap

## What's NOT in v17 (deferred)

- Surface block palette swap (sand/gravel vs grass) at cut-bank vs point-bar. User said no substrate mosaic; bank palette swap can be a future iteration if needed.
- Surface block re-evaluation underwater (still uses generic dirt/sand from biome — could add sediment-specific palette).
- Spline cache disk persistence audit — works fine for tested tiles (51,53)+(52,53), needs validation for the full 9409-tile render.

## How to Reproduce v17 from Scratch

1. Ensure `masks/hydro_region.png` has paint (id=2 for rivers, id=1 for lakes).
2. Ensure `masks/_spline_cache.pkl` exists (or accept ~10-15 min spline build per process).
3. Render any painted tile: `py run_pipeline.py --tile-x0 X --tile-x1 X+1 --tile-z0 Z --tile-z1 Z+1 --threads 1`.
4. Expected log lines per tile:
   - `[hydro_region_overlay] spline cache HIT` (or "SAVED" first time)
   - `[hydro_region_overlay] geomorph applied: thalweg +/-2.50b, bedform +/-1.2b/lambda=30, riffle +/-2.5b/lambda=250`
   - `[hydro_region_overlay] bank asymmetry (bbox): point_bar=N (sigma=2.5), cut_bank=N (sigma=0.0), bbox=HxW (pad=10)`
   - `[hydro_region_overlay] melt gaussian: sigma=4.0 at 8k (~24b at 50k)`
   - `[hydro_region_overlay] global river bed precomputed at 8k`
   - `[s83v13] tile=(X,Z) carve_completion: lowered N cells`
   - `[s83v16] tile=(X,Z) bed_melt_50k: smoothed N cells (sigma=2.0)`
   - `[s83v15] tile=(X,Z) bank_smooth: lowered N cells (sigma=4.0, radius=12b)`
   - `tile_complete elapsed_ms ~1800000` (= 30 min single-tile)

## Memory & Performance

Per-worker peak: ~2.5-3 GB for `_ensure_caches`. **1 worker is safe**, 2 workers OOMed during v12 development (recorded in S83 history). For full-world 50k render with 2-4 workers, the worker memory needs to drop ~30% — TBD whether bbox optimization extends to enable that. Conservative: **start with 1 worker for full-world**. Per-tile carve ~30 min × 9409 tiles = 280 days serial. Need to enable safe parallelism (target: 2-3 workers) OR set up cloud render.

## Next: Full-World 50k Generation

See "50k Generation Plan" section in CLAUDE.md and `memory/S83_50k_plan.md`.

---

**Commit hash:** (set after commit)
**Tested tiles:** (51,53), (52,53) — both painted-river + lake-interaction tiles
**World tested in:** `Vandirtest10` Modrinth profile, Minecraft 1.21.10 Java
