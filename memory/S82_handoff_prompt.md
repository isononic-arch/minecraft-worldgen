# S82 Handoff Prompt — Hydrology Pre-50k-Render Prep

S81 hydrology landed v8.14 ("Pass working great" — user verdict). All v8.x changes committed `6cb86e5` on master.

## Read first

1. `memory/S81_river_handoff.md` "S81 v8.2 → v8.14 PROGRESSION" section + "Tunables summary" table for full context on what landed and what was reverted.
2. `CLAUDE.md` "S81 BACKLOG (DEFER until full-world 50k regen)" — three deferred items below.
3. **DO NOT change** the v8.14 user-validated state. The tuning is locked. Tunables table in S81 handoff lists all current constants — don't touch.

## Three deferred items, in order

### 1. Tile-boundary line artifacts

**Symptom:** per-tile gaussian / EDT / 3×3-max passes can't see across tile boundaries. Visible "harsh single-X line" at tile seams in-world. User flagged but said it's a known issue and we deferred.

**Root cause:** at tile boundaries, scipy's `gaussian_filter` reflects (or zeros) the kernel reach beyond the tile, generating ghost values. `distance_transform_edt` computes distance only within the tile, so a water cell 1 cell across the boundary appears as "infinitely far" → escape-fix doesn't raise the boundary cell, EDT berm slope is wrong there.

**Fix plan (~70 lines in `run_pipeline.py`):**

- Read `height.tif` at padded coords (`pad_px=48`) → padded `pre_carve_y`
- Read `hydro_region.png` at padded coords → painted_river / painted_lake info in pad region
- Read `hydro_lake_wl.tif` at padded coords → precompute basin water_y for pad region
- Build padded `surface_y` array (inner = computed surface_y, outer = pre_carve_y from padded height)
- Build padded `river_water_y` array (inner = computed, outer = approximated):
  - Painted-lake cells in pad: `precompute_lake_wl` value
  - Painted-river cells in pad: `pre_carve_y - 1` (rough river formula)
  - Else: `-999` (no water)
- Run escape-fix + EDT berm passes on padded arrays (existing logic, just larger arrays)
- Crop back to inner tile (the `[PAD:PAD+h, PAD:PAD+w]` slice)
- Optionally do same for the smooth-brush in `core/river_carver_v2.py:7.6b` (footprint-only smoothing still has gaussian kernel reach at tile boundaries when footprint touches the boundary)

**Test:** render tiles (51,53) and (52,53) with the fix in place. Compare against v8.14 baseline (already in `memory/s81_v813_3x3_stitched.png` and `memory/s81_v814_50_53.png`). The harsh single-X line at the (51,53) ↔ (52,53) seam (X=27136) should be gone or substantially reduced. User validates in-world before approving.

### 2. Spline cache disk persistence

**Why:** `_build_spline_outline_50k` in `core/hydro_region_overlay.py` currently rebuilds the cKDTree + per-sample meander noise on every fresh Python process (~10-15 min for our paint complexity). For full-world 9409 tiles × 2-4 workers = ~3000 process boots = ~750 hours of redundant cache rebuilds.

**Fix plan (~30-50 lines in `core/hydro_region_overlay.py`):**

In `_ensure_caches`, before calling `_build_spline_outline_50k`:

```python
import pickle, hashlib
CACHE_PATH = masks_dir / "_spline_cache.pkl"

# Build cache key from paint mask + code + params
paint_bytes = hr_path.read_bytes()
paint_hash = hashlib.md5(paint_bytes).hexdigest()
import inspect
code_hash = hashlib.md5(inspect.getsource(_build_spline_outline_50k).encode()).hexdigest()
params_hash = hashlib.md5(repr({
    'smoothness': 3.0, 'periodic': True,
    'periodic_amp': 6.0, 'periodic_wavelength': 140.0,
    'phase_distortion_amp': 350.0, 'phase_distortion_wavelength': 800.0,
    'micro_amp': 1.0, 'micro_wavelength': 30.0,
    'meander_seed': 0xDEADBEEF,
}).encode()).hexdigest()
cache_key = f"{paint_hash}_{code_hash}_{params_hash}"

# Try loading
loaded = False
if CACHE_PATH.exists():
    try:
        with open(CACHE_PATH, 'rb') as f:
            data = pickle.load(f)
        if data.get('key') == cache_key:
            _river_spline_pts_50k_cache = data['pts']
            _river_spline_kdtree_cache = data['kdtree']
            _river_spline_polygons_50k_cache = data['polygons']
            loaded = True
    except Exception:
        pass

if not loaded:
    # Compute as before
    pts_50k, polygons = _build_spline_outline_50k(paint_mask, smoothness_factor=3.0)
    _river_spline_pts_50k_cache = pts_50k
    _river_spline_polygons_50k_cache = polygons
    if pts_50k.shape[0] > 0:
        from scipy.spatial import cKDTree
        _river_spline_kdtree_cache = cKDTree(pts_50k)
    else:
        _river_spline_kdtree_cache = None

    # Save with atomic write
    tmp_path = CACHE_PATH.with_suffix('.pkl.tmp')
    with open(tmp_path, 'wb') as f:
        pickle.dump({
            'key': cache_key,
            'pts': _river_spline_pts_50k_cache,
            'kdtree': _river_spline_kdtree_cache,
            'polygons': _river_spline_polygons_50k_cache,
        }, f, protocol=pickle.HIGHEST_PROTOCOL)
    import os
    os.replace(tmp_path, CACHE_PATH)
```

**Risk:** stale cache mid-iteration. If user is actively tuning spline params (smoothness, periodic_amp, etc.), the params_hash changes → cache invalidates correctly. But if `_build_spline_outline_50k` source is edited without changing the visible behavior, code_hash changes → cache invalidates. Both are correct behaviors, just slightly wasteful during iteration.

**Mitigation:** add a CLI flag `--no-spline-cache` to `run_pipeline.py` for debug runs. Or simply `rm masks/_spline_cache.pkl` to force rebuild.

**Test:** render tiles (51,53) and (52,53). First run rebuilds cache (~10-15 min). Second run loads cache (<1s). Verify visual output is identical to v8.14 baseline. User validates no regression before full-world.

### 3. OOM with --threads ≥3

Each worker holds 500-800MB (spline cache + per-tile state). On 16GB-class machines, 4 workers OOMed (3 of 9 tiles failed in our v8.11p attempt). Use `--threads 2` for stability.

**May be partially solved by #2** — if the spline cache lives on disk and is mmap'd or loaded once per worker (rather than rebuilt per process), per-worker memory drops. Could enable threads=4 again on 32GB+ machines.

**Test:** after #2 is in, try `--threads 4` on the test tiles. If memory stays under 12GB total (3GB per worker), it's safe for full-world.

## Workflow for next session

1. Implement #1 (tile-boundary padding)
2. Render 2-tile test: `--tile-x0 51 --tile-x1 53 --tile-z0 53 --tile-z1 54 --threads 2`
3. User validates tile-boundary line is gone in-world
4. Implement #2 (spline cache disk persistence)
5. Render same 2 tiles. First run = cache build, second run = cache load. Confirm both produce same output.
6. User gives green light for full-world 50k render
7. Full 50k regen — `--tile-x0 0 --tile-x1 97 --tile-z0 0 --tile-z1 97 --threads 2`. Likely overnight, ~24-48h depending on per-tile cost.

## Test tiles + coords

- **(51,53)** — lake-blob neck + south inlet. `/tp @s 26112 200 27392`
- **(52,53)** — east inlet horizontal river through middle. `/tp @s 26880 200 27392`

Tile boundary at X=27136 between them — the seam to validate the padding fix on.

## Tunables (final v8.14 — DO NOT CHANGE without user approval)

| Constant | File | Current | Notes |
|---|---|---|---|
| `_CARVE_MAX_DEPTH` | hydro_region_overlay.py | 6.0 | Plateau depth |
| `_CARVE_INWARD_BIAS` | hydro_region_overlay.py | 4.0 | Choke amount |
| `_CARVE_SOFTNESS` | hydro_region_overlay.py | 3.0 | Smoothstep transition zone |
| `smoothness_factor` | hydro_region_overlay.py:_build_spline_outline_50k call | 3.0 | Spline corner roundness |
| `periodic_amp_blocks` | hydro_region_overlay.py | 6.0 | Meander amplitude |
| `periodic_wavelength_blocks` | hydro_region_overlay.py | 140 | Meander cycle length |
| `phase_distortion_amp_blocks` | hydro_region_overlay.py | 350 | Phase wobble strength |
| `phase_distortion_wavelength_blocks` | hydro_region_overlay.py | 800 | Phase wobble scale |
| `micro_amp_blocks` | hydro_region_overlay.py | 1.0 | Bank irregularity |
| `micro_wavelength_blocks` | hydro_region_overlay.py | 30 | Bank irregularity scale |
| `BLEND_DIST` | run_pipeline.py | 24 | Lake-river water_y blend |
| `_BLEND_PROTECT_DIST` | run_pipeline.py (v8.14 cap) | 24 | Match BLEND_DIST |
| `BERM_RADIUS` | run_pipeline.py | 8 | EDT berm extent |
| `BANK_SIGMA` | river_carver_v2.py | 16.0 | Smooth-brush sigma (footprint-only) |
| `BANK_PASSES` | river_carver_v2.py | 3 | Smooth-brush iterations |
| `BIRCH_FOREST` density | schematic_placement.py | 0.36 | (was 0.30) |

## Render time reference

- Solo tile (cold spline cache): ~40-67 min
- 9-tile 3×3 with `--threads 2`: ~3-3.5h (cache built once per worker)
- Per-tile after cache built: ~25-30 min

After #2 (spline cache persistence): per-process cold start drops from ~10-15 min to <5s. Solo tile: ~25-35 min instead of 40-67. 3×3: ~2.5h instead of 3.5h. Full world 9409 tiles: estimated 7-10 days at threads=2, possibly 4-5 days at threads=4 if memory permits.

## What NOT to do

- Don't change any tunable above without user approval
- Don't touch the v8.14 final cleanup pass logic (capping rule + BLEND-cell exception) — user said "Pass working great"
- Don't try to "fix" the perched-lake stamp aesthetic at high-elevation lakes (we tried Option A in v8.10, broke painted-river cells in basins). Live with it.
- Don't try to remove the precompute basin classification at painted-river cells (v8.11 attempt — broke water visibility). Live with it.
- Don't try per-branch arclength water_y again (v8.2 attempt — broke flow direction). The `nearest_avg + bank_lift - 1` formula + v8.14 cap is the right architecture.

## Diagnostic tools available

In `tools/`:
- `diag_water_y_dump.py` — per-cell water_y / surface_y / depth analysis with phantom water + cross-section variance reports
- `diag_carve_path_image.py` — visualize carve depth field per tile
- `diag_topblock_at_painted.py` — top block at painted-river cells
- `diag_water_drops.py` — water-drop validation
- `diag_mca_topdown.py` — generate top-down PNG of MCA tile

Use `diag_water_y_dump.py` if anything water-related looks off. It dumps the per-cell state to `memory/diag_water_y/` with annotated PNGs for cross-section variance, phantom water (water above terrain), and lake-river junction mismatches.
