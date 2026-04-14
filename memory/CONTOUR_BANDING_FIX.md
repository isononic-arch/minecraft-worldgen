# Contour-Line Stone Banding Fix

**Session:** S54 (2026-04-14)
**Status:** Fixed
**File changed:** `run_pipeline.py` line 207-208

---

## Symptom

Thin bands of stone/andesite/cobblestone traced every terrain contour line (every 1-block Y change) across all biomes. Most visible on flat valley floors where stone contrasted sharply with grass/podzol. Visible from aerial view as dense parallel lines covering entire mountainsides and plains alike.

## Root cause

`run_pipeline.py` computed `cliff_deg` using raw `np.gradient` on integer `surface_y`:

```python
_gy, _gx = np.gradient(surface_y.astype(np.float32))
cliff_deg = np.degrees(np.arctan(np.hypot(_gx, _gy))).astype(np.float32)
```

Integer terrain is a staircase. Every 1-block height change between adjacent pixels produces a gradient of 1.0, which `arctan` maps to **45 degrees**. This unsmoothed `cliff_deg` was passed into the surface pipeline context (`SurfaceContext.eco_grads["cliff_deg"]`), where slope-gated layers fired on every terrain step:

- `temperate_cliff_face` (threshold: 35 deg) placed stone/andesite/cobblestone
- `temperate_talus_apron` (threshold: 18-35 deg) placed cobblestone
- `vertical_fluting` (threshold: 35 deg) placed stone variants
- `grass_terrace` (threshold: 8-18 deg) placed biome-keyed scatter

A smoothed version (`compute_cliff_deg()` in `core/eco_gradients.py`, sigma=1.5 Gaussian) already existed and was used by `chunk_writer.py` for subsurface banding. The surface pipeline just never called it.

## Fix

One-line replacement in `run_pipeline.py`:

```python
# Before:
_gy, _gx = np.gradient(surface_y.astype(np.float32))
cliff_deg = np.degrees(np.arctan(np.hypot(_gx, _gy))).astype(np.float32)

# After:
cliff_deg = core_eco.compute_cliff_deg(surface_y)
```

The Gaussian pre-smooth (sigma=1.5, effective kernel ~3 blocks) collapses staircase edges to their true regional slope. Flat valley floors now read as 2-5 degrees instead of 45, so cliff/talus layers only fire on genuinely steep terrain.

## Why this kept recurring

Sessions S49 through S53 each attempted to fix visible stone banding by removing stone blocks from noise_layers configs, eco overlay palettes, and slope zone assignments. These were real stone-on-surface sources, but eliminating them never fully resolved the banding because the dominant source was the surface pipeline layers — which were invisible to the `diag_stone_trace.py` diagnostic that only checked the legacy decorator path, not the pipeline layer outputs.

The clue that cracked it: banding appeared ONLY at 1-2 block Y transitions on flat valley floors, and the block palette matched `temperate_cliff_face` output (stone + andesite + cobblestone mix). That pointed to the surface pipeline, and tracing the `cliff_deg` data flow revealed the missing smoothing step.

## Related

- `core/eco_gradients.py::compute_cliff_deg()` — the smoothed version (canonical)
- `core/chunk_writer.py` lines 364, 568 — subsurface uses smoothed version correctly
- `PHYSICAL_REALISM_REFACTOR.md` section 18, S54 entry — full implementation log
