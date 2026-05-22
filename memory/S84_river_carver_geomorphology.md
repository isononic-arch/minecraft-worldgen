# S84 River Carver Geomorphology — Reference Document

**Status:** Production-ready as of S84 (2026-05-22). User verdict on (49,53) coastal + (51,53) inland: *"It's perfect."*

This document is the authoritative reference for how rivers work in the Vandir pipeline. It covers:
1. The geomorphology theory we're modeling
2. How the carver pipeline produces a river bed
3. Every tunable, what it does, and what we learned tuning it
4. The full S80→S84 evolution — what we tried, what worked, what didn't
5. Pointers to all the code

If you're picking this up cold, read sections 1-3. Sections 4-5 are archaeology — useful when something breaks and you need to know why a knob is set the way it is.

---

## 1. River geomorphology — what we're modeling

Real rivers obey downstream hydraulic geometry. Three relationships matter for our carver:

| Variable | Relation | Effect |
|---|---|---|
| Width W | W ∝ Q^0.5 | Wider where discharge is higher (downstream) |
| Depth D | D ∝ Q^0.4 | Deeper where discharge is higher |
| Velocity V | V ∝ Q^0.1 | Faster slightly downstream (small effect) |

Q = discharge = flow rate. It increases downstream as tributaries feed in. So **wider painted river = deeper carved bed**. We don't compute Q directly; we use the user's paint width as a proxy.

### Cross-section shape

Within one slice across a river:
- **Edges:** depth ≈ 0 (terrain just slopes into water)
- **Cut-bank** (outside of meander curve): undercut, steeper, deeper
- **Point-bar** (inside of meander curve): gentle, shallower (sediment accumulates here)
- **Thalweg** (deepest line, doesn't follow centerline exactly): offset toward cut-bank
- **Bed shape:** smooth bowl, asymmetric due to thalweg offset

### Longitudinal shape (going downstream)

- **Riffles** (shallow, faster, sometimes rocky) — separated by:
- **Pools** (deeper, slower, on outside of meanders)
- Wavelength ~5-7× channel width. We use 250 blocks, matching typical mid-reach rivers.

### Mouth (where river meets ocean)

This is where Vandir's earlier carver was wrong. Real river mouths **shallow** approaching the sea due to:
- **Sediment deposition** — slowing flow drops its sediment load, building a delta
- **Mouth bars** — depositional ridges across the outlet
- **Continental shelf bathymetry** — sea floor itself is shallow at the shelf

(Fjord-type mouths stay deep, but Vandir's gentle continental shelf doesn't have those.)

S84 added a **coast taper** so painted rivers ramp to natural sea-floor depth over ~60 blocks approaching the shore.

---

## 2. The carver pipeline

Five mechanisms stack to produce the final river bed. All gated on the user's painted mask (`masks/hydro_region.png`, value 2 = river, value 1 = lake). Pipeline runs in two phases.

### Phase A: Global precompute (once per Python process)

In `core/hydro_region_overlay.py:_ensure_caches`. Bakes everything into `_river_bed_8k_cache` (a float32 8192×8192 array of MC-Y values for every painted cell).

1. **Spline-fit outline** — `skimage.find_contours(paint_mask, 0.5)` → sub-pixel boundary points → `scipy.interpolate.splprep` periodic B-spline fit → dense sampled point cloud → `scipy.spatial.cKDTree`. Eliminates the 8K pixel staircase in the binary paint mask.

2. **Periodic meander injection** (S81 v8.1) — adds correlated sinusoidal displacement to both banks: `disp = sin((arclen + simplex_macro × phase_amp) × 2π/λ) × periodic_amp + simplex_micro × micro_amp`. Both banks move together, so the channel meanders rather than fattening/thinning.

3. **Signed distance field (SDF)** — for every cell, cKDTree.query gives distance to nearest spline curve point. Sign via point-in-polygon. SDF > 0 = inside river, SDF < 0 = outside.

4. **Power-curve carve depth (S83 v17)** — `depth = SCALE × max(0, SDF)^EXP`. Sub-linear power produces a natural bowl: shallow at edges, deeper toward center. Real rivers follow D ∝ W^0.4-0.8, this matches Hack's law.

5. **Tanh saturation (S84)** — `depth = MAX × tanh(depth_raw / MAX)`. Soft asymptotic cap toward `_DEPTH_MAX_BLOCKS=10`. Preserves bowl shape (monotonic increase from edge to center) but prevents wide rivers from carving 30-50 block troughs.

6. **Coast taper (S84)** — `depth_final = depth × tanh(coast_edt_blocks / TAPER)`. Coast EDT computed from height < SEA_LEVEL mask. Painted cells at the ocean shoreline get factor=0 (no carve); ~60 blocks inland get factor≈0.76; ~200 blocks inland get factor≈1.0 (full carve). Real delta look.

7. **Bed gaussian (σ=4)** — single-pass weighted gaussian inside footprint. Smooths the bed seed.

8. **WorldEdit-brush smoothing (5×σ=2)** — five passes of weighted gaussian. Equivalent to running `//brush smooth ×5` over the bed. Removes residual high-frequency artifacts.

9. **Geomorph apply** — thalweg asymmetry, bedform, riffle-pool. All driven by skeleton-arclength position (not polygon — polygon would create cross-channel chessboard).
   - **Thalweg:** `cos(skeleton_perp_axis) × _THALWEG_AMP_BLOCKS` — outside of curve deeper, inside shallower
   - **Bedform:** `sin(arclen × 2π/30) × _BEDFORM_AMP_BLOCKS` — micro texture, 30-block wavelength
   - **Riffle-pool:** `sin(arclen × 2π/250) × _RIFFLE_AMP_BLOCKS` — pools and riffles, 250-block wavelength

10. **Bank asymmetry** — point-bar σ=2.5 gaussian smooth (gentle ramp), cut-bank σ=0 (sharp cliff), applied via 6-cell ring around banks. Bbox-optimized.

11. **σ=4 melt gaussian** — final whole-cache gaussian on the bed. Polishes everything, removes any remaining lattice artifacts. Bbox-optimized to the painted-cell bounding box.

After all this, `_river_bed_8k_cache` holds the finished bed in MC-Y values. The result gets pickled to `masks/_bed_cache_v17.pkl` (~1.4 GB) so subsequent worker processes load it in 30s instead of rebuilding in 10 min.

### Phase B: Per-tile carve (`core/river_carver_v2.py`)

Each tile sees its slice of the world. For each painted cell in the tile:

1. **Footprint construction** — `river_full_mask = centerline & ~lake_mask`. NOTE: S84 dropped the `above_sea` gate here so painted cells at or below sea level are included.

2. **above_sea redefined (S84)** — at l.292: `above_sea = (surface_out > SEA_LEVEL) | (hydro_centerline > 0)`. This single line propagates through 6 downstream gates (footprint at l.1000, zone at l.1195, orphan at l.1348, river_strict at l.1367, water_mask at l.1469). Means: paint always carves regardless of sea level.

3. **Gravity carve at 50K** — `surface_y = min(surface_y, original − depth_at_cell)`. Only lowers terrain, never raises. Computed at 50K from re-sampled SDF + same tanh saturation + coast factor (sampled from 8K cache via bilinear).

4. **Bed override at 50K** — `surface_y[footprint] = min(bed_cache_50k_sampled, surface_y)`. Bilinear-samples `_river_bed_8k_cache` to the 50K grid. Only ever lowers (never raises terrain).

5. **water_y assignment** — `water_y = nearest_avg + min(slope×width, 4) − 1`. nearest_avg is averaged natural terrain in skeleton neighborhood. Bank-relative water surface; slope×width modifier prevents water sitting above bank on tilted ground.

### Phase C: Three tile-level passes (`run_pipeline.py`)

Run after the carver inside the PAD=48 escape-fix block:

- **PASS 0: carve_completion** — cells where `water_y > SEA & surface ≥ water_y & ~lake` get surface pushed to `water_y − 1`. Fixes boundary cells where smoothing pulled bed up to water-level.

- **PASS 0.25: bed_melt σ=2 at 50K** — weighted gaussian on `surface_y` at water cells. σ=2 (not σ=4) so it preserves variation in narrow 3-5 block channels that are sub-pixel at 8K.

- **PASS 0.5: bank_smooth σ=4** — gaussian on `surface_y` at LAND cells within 12 blocks of water. Smooths banks while preserving natural silhouette.

Then the legacy passes: iterative escape-fix, EDT berm (radius 8), water-level cap (river water_y ≤ adjacent natural bank − 1, BLEND_DIST=24 near lakes).

---

## 3. Tunables — what to touch when

All in `core/hydro_region_overlay.py` module level (line ~75-130).

### Depth shape

| Constant | Default | What it does | When to touch |
|---|---|---|---|
| `_DEPTH_POWER_SCALE` | 2.0 | Coefficient on power curve | Globally deeper/shallower; rare |
| `_DEPTH_POWER_EXPONENT` | 0.7 | Exponent on SDF | Lower = more sublinear (narrow ≈ wide). Don't go below 0.4 |
| `_DEPTH_MAX_BLOCKS` | 10.0 | Tanh saturation ceiling | **Most common tuning knob.** Maximum river depth in blocks. 10 = matches Vandir's ~Y 54 coastal shelf |
| `_COAST_TAPER_BLOCKS` | 60.0 | Distance over which coast taper saturates | Lower = sharper coastal shallowing. Higher = gentler ramp inland |

### Geomorph

| Constant | Default | What it does |
|---|---|---|
| `_THALWEG_AMP_BLOCKS` | 2.5 | Cut-bank depth bonus / point-bar shallowing magnitude |
| `_THALWEG_SCALE` | (internal) | Arclength scale for thalweg sinusoid |
| `_BEDFORM_AMP_BLOCKS` | 1.2 | Micro-texture amplitude, λ=30 blocks |
| `_RIFFLE_AMP_BLOCKS` | 2.5 | Riffle-pool depth variation, λ=250 blocks |

### Bank shaping

| Constant | Default | What it does |
|---|---|---|
| `_BANK_ASYM_SIGMA_POINTBAR_8K` | 2.5 | Smooth ramp on point-bar side |
| `_BANK_ASYM_SIGMA_CUTBANK_8K` | 0.0 | Sharp cliff on cut-bank side |
| `_BANK_ASYM_RING_RADIUS_8K` | 6 | Ring width for bank asymmetry application (~36 blocks at 50K) |

### Smoothing

| Constant | Default | What it does |
|---|---|---|
| `_MELT_GAUSSIAN_SIGMA_8K` | 4.0 | Final 8K melt — polish, removes lattice |
| `_RIVER_BED_GAUSS_SIGMA_8K` | 4.0 | Initial bed σ for first smoothing pass |
| `_RIVER_TROUGH_SMOOTH_PASSES` | 5 | Count of WorldEdit-brush σ=2 passes |
| `_RIVER_TROUGH_SMOOTH_SIGMA_8K` | 2.0 | σ for each smooth-brush pass |

### Meander (in `_build_spline_outline_50k` call site)

| Param | Default | What it does |
|---|---|---|
| `smoothness_factor` | 3.0 | B-spline `s` factor × paint length. Higher = rounder corners. 5+ over-rounds bends |
| `periodic_amp_blocks` | 6.0 | Meander wave amplitude. Bigger = taller bends |
| `periodic_wavelength_blocks` | 140.0 | Meander cycle spacing |
| `phase_distortion_amp_blocks` | 350.0 | How non-uniform the sine wave is across the world. Smaller = more uniform, bigger = more random |
| `micro_amp_blocks` | 1.0 | Per-pixel bank irregularity from simplex noise |
| `micro_wavelength_blocks` | 30.0 | Wavelength of the micro noise |

### Per-tile passes (in `run_pipeline.py`)

| Constant | Default | What it does |
|---|---|---|
| `_BED_MELT_SIGMA_50K` | 2.0 | PASS 0.25 — narrow-channel bed smoothing |
| `_BANK_SMOOTH_SIGMA` | 4.0 | PASS 0.5 — bank smoothing |
| `_BANK_SMOOTH_RADIUS_BLOCKS` | 12 | PASS 0.5 — how far from water to smooth |

### Cache invalidation

Every depth-shaping tunable is in `_make_bed_cache_key`'s tunables tuple. **Change any of them → bed cache invalidates → next render rebuilds (~10 min)**. The spline cache invalidates separately on `_build_spline_outline_50k` source/params change.

To force-rebuild without code change: set env var `VANDIR_NO_BED_CACHE=1` or `VANDIR_NO_SPLINE_CACHE=1`.

---

## 4. S80 → S84 evolution

### S80 — Pivot to painted rivers

User abandoned the previous WP-findPath/Strahler corridor system. `masks/hydro_region.png` (8192×8192, value 2 = river, value 1 = lake) became the canonical hydrology source.

Legacy spline pickle disabled (`river_splines.pkl.disabled_v32_paint_only`). Carver detects paint presence and switches to paint-driven mode automatically.

Issues confronted:
- **Endpoint pruning was chopping ~49 blocks off river tips.** Removed in `core/region_overlay_smoothing.py:clean_painted_river_mask`.
- **Spline pickle from a previous WP run was overriding paint.** Renamed off.
- **Pre-S80 carver chose old WP centerline.** Carver gated on `_has_any_hydro` now.

### S81 — Spline-fit outline (the staircase saga)

The 8K painted mask is binary — every NEAREST upsample showed visible pixel staircase at MC scale. Eight attempts:

| Version | Approach | Result |
|---|---|---|
| v1 | Float-passthrough cache (gaussian σ=1.5) | Staircase remained |
| v2 | σ=2.5 50K post-bilinear gaussian | Slight improvement, staircase visible |
| v3 | SDF cache (signed distance from paint, σ=0.7) | Integer-step lattice artifacts |
| v4 | SDF σ=8 + threshold 0 | Thin rivers ERASED entirely |
| v5 | Lake-mechanism (subtract from cell's own y) | Helped wiggle boundary |
| v6 | Heavy bank smooth σ=16, zone=24, 3 passes | Threshold contour still visible |
| v7 | SDF→sigmoid continuous carve | Eliminated binary contour, minor lattice fingerprint |
| **v8** | **Spline-fit + cKDTree analytical distance** | **Boundary smooth, no lattice** |

v8 became the production approach. Subsequent v8.1-v8.14 refined:
- Periodic meander injection (v8.1) — "looks great. Beautiful"
- Lake-river BLEND=24 (v8.12) — bridges 19-block lake/river elevation gap
- Footprint-only bank smooth (v8.13) — eliminates valley dip
- Water-level cap (v8.14) — `river water_y ≤ adjacent natural bank − 1`, BLEND-protected near lakes
- User verdict: **"Pass working great."**

### S82 — Tile-boundary correctness

PAD=48 ring added around each tile in the escape-fix block. All cross-tile passes (escape-fix, EDT berm, water-level cap) run on padded array, then crop back.

### S83 v17 — Real-river geomorphology

Beyond just smoothing, S83 added:
- **Power-curve depth** replacing smoothstep + plateau
- **σ=4 + 5×σ=2 smoothing** combination (one seed pass + five brush passes)
- **Thalweg / bedform / riffle-pool** geomorph
- **Bank asymmetry** (point-bar smooth, cut-bank cliff)
- **Final σ=4 melt** at the 8K cache
- **Three new tile-level passes** (carve_completion, bed_melt, bank_smooth)

User verdict: **"It's perfect"** on (51,53).

### S84 — Coastal river fixes (this session)

Three problems surfaced post-S83 at coastal tile (49,53):

**Problem 1: Painted coastal cells showed as land, not water.**

User: *"If the river is painted there, regardless of if it matches ocean height- it should have water!"*

Root cause: `above_sea = surface_out > SEA_LEVEL` at `core/river_carver_v2.py:292` propagated through 6 downstream gates. Painted cells at or below sea level were excluded from EVERY river path → no water_y, no bed override, no carve.

**Failed first attempt — PASS 0.1 brute-force coastal hack:** slammed every painted coastal cell to Y 55 floor + water_y=63. User: *"the river is carved in giant, stepwise, non-smoothed chunks. And there is no RIVER- it is just one single layer surface block of water."* Reverted.

**Final fix:** at line 292, `above_sea = (surface_out > SEA_LEVEL) | painted_mask`. One line, propagates through all downstream gates. Bank widening (l.1503/1510/1517) stays strict with `surface_out > SEA_LEVEL` directly so banks don't extend into ocean.

**Problem 2: Tile-boundary walls.**

User: *"two weird blocky 'borders' where the tile cuts off and land rises abruptly with the ocean surface."*

Root cause: `run_pipeline.py:610-611` had a hardcoded 4-point LUT `[0,17050,45000,65496]→[-64,63,200,448]` for the PAD reconstruction. Inner tile used the new 13-point spline via `core_col_gen._LUT` (config-driven). For inland raw values, the LUTs disagreed by 10-36 MC-Y. Escape-fix saw the inner/pad discontinuity and "fixed" perceived water spillage by walling inner-edge cells.

**Fix:** replaced both `np.interp` calls with `core_col_gen._LUT[int_idx]`. One LUT source of truth.

**Problem 3: Wide rivers carving to Y 1 (~62 blocks below sea level).**

User: *"The river carver melts too intensely- all the way down to a y of 1! Make rivers a more realistic depth (matches coastal shelf ~Y 54 = 9 blocks below sea). Don't unnaturally flatten out like a pool."*

Root cause: power curve `2.0 × sdf^0.7` is unbounded. For wide paint (SDF=100), depth = 50 blocks.

**Rejected approach: hard cap.** `min(depth, 10)` would create flat plateaus through the middle 80% of any wide river. Pool-bottomed look.

**Final fix: tanh saturation.** `depth = MAX × tanh(depth_raw / MAX)`. Soft asymptotic approach to the cap. Preserves bowl shape (monotonic increase from edge to center) while preventing unbounded growth.

**Problem 4 (preventive): Rivers didn't shallow at the mouth.**

User: *"Don't rivers generally shallow out as they hit the ocean?"*

Yes — sediment deposition + shelf bathymetry. Added **coast distance taper** so depth modulates by `tanh(coast_edt_blocks / 60)`. At the ocean shoreline: factor=0 → bed = natural sea floor. Far inland: factor≈1 → full natural carve.

User verdict on coastal (49,53) + inland (51,53) reference: **"It's perfect."**

---

## 5. Code pointers

| Functionality | File | Notes |
|---|---|---|
| **Spline outline build** | `core/hydro_region_overlay.py:_build_spline_outline_50k` | Skimage contours + scipy splprep + cKDTree |
| **Global bed precompute** | `core/hydro_region_overlay.py:_ensure_caches` | Builds `_river_bed_8k_cache`. ~10 min cold |
| **Bed cache disk persistence** | `core/hydro_region_overlay.py:_load_bed_cache_from_disk` and `_save_bed_cache_to_disk` | ~1.4 GB pickle. Hash key invalidates on paint/code/tunable change |
| **Spline cache disk persistence** | Embedded in `_ensure_caches` (lines ~1100-1205) | 68 MB pickle. Falls under bed cache pickle when bed cache HITs |
| **Per-tile carve** | `core/river_carver_v2.py` | Gravity carve + bed override |
| **above_sea gate (S84 fix)** | `core/river_carver_v2.py:292` | Now `| painted_mask` |
| **Tile-level passes** | `run_pipeline.py:585-940` | PAD=48 block. PASS 0/0.25/0.5 then escape-fix/EDT/cap |
| **PAD LUT (S84 fix)** | `run_pipeline.py:609-622` | Now uses `core_col_gen._LUT` instead of hardcoded 4-point |
| **Hydrology Pixel Editor** | `tools/override_studio.py` Hydrology tab | Paint the 8192 mask at native resolution |

---

## 6. What we DON'T model (deliberate gaps)

- **Hydraulic simulation.** No Q, no Manning's equation, no shear stress. We approximate via the user's painted width.
- **Sediment transport budget.** No erosion/deposition over time. The delta-shallowing is a coast-distance taper, not actual physics.
- **Subsurface aquifers, hyporheic flow.** Out of scope.
- **Headwater taper.** Narrow paint at headwaters self-tapers via SDF. We don't add an additional headwater-specific taper. Coast taper handles the downstream end.
- **Tidal mixing zones.** No salinity, no tidal variation. Sea level is fixed at Y=63.
- **Distributary delta networks.** User would need to paint multiple river arms.
- **River braiding.** Same — paint multiple parallel channels if you want braids.

---

## 7. Reproducing a render with the S84 carver

```
PYTHONUNBUFFERED=1 python run_pipeline.py \
    --config config/thresholds.json \
    --masks C:/Users/nicho/minecraft-worldgen/masks/ \
    --schem-index schematic_index.json \
    --output output_s84/ \
    --tile-x0 X --tile-x1 X+1 --tile-z0 Z --tile-z1 Z+1
```

Reference tiles for regression testing:
- **(49,53)** — coastal painted river. Tests above_sea fix + coast taper + LUT fix.
- **(51,53)** — inland painted river + lakes. Tests v17 baseline + tanh saturation cap.
- **(48,48)** — pure ocean. Tests sea-level rendering.
- **(16,73)** — meander reference.

Expected wall time per tile: ~30 min (chunk_writer-bound on 768-height world).

First render of a fresh process rebuilds bed cache (~10 min); subsequent processes HIT in ~30s.
