# S65-S68 carry-forward items

Last updated: 2026-04-21 (S68 wrap)

## 🔥 New for S69 (from S68 in-world walk)

### G1. Gaussian smoothing too aggressive — mountain interior artifacts
**Issue:** S68 cranked sigma=16, passes=6, buffer=36 to fix dune/beach seams.  Side effect: smoothing now bleeds into mountain interiors (NOT just biome boundaries) and creates NEW seams where preserved ridges meet smoothed slopes.  User calls out this artifact on (24,80) + (25,80).

**Root cause:** `_smooth_all_biome_boundaries_y` uses buffer=36 from boundary — this reaches into mountain interior where there's no seam to smooth.  Also, the taper weight is currently max at boundary, fading with distance — but 36 blocks is too wide for steep alpine ridges.

**Fix options (S69):**
- Drop back to S67 intensity: sigma=8, passes=3, buffer=24 (was fine for most seams)
- Keep high intensity but LIMIT buffer to 10-15 blocks — only near-boundary smoothing
- Selective: apply high intensity only at SAND_DUNE_DESERT + BEACH boundaries, normal intensity elsewhere
- Add slope-aware weight: don't smooth where local surface gradient is already steep (mountain ridges) — only smooth where Y-step is a discontinuity

### G2. KARST_BARRENS bushes — user wants ~50% coverage
**Issue:** S67.1 raised `KARST` bush GC density 0.05→0.30 + added `eco_density_mod >= 0.9` override on KARST pixels.  But in-world, bushes are only ~10-20% coverage.  User says "the ground should be at least like 50% bushes."

**Candidates for S69:**
- Bump bush density 0.30 → 0.55 or 0.65
- Investigate other suppressors (canopy_proxy, slope_density_mod) that might be reducing KARST density
- Add a KARST-specific density FLOOR (like the eco_density_mod floor but for density_mult overall)

---

## 🔴 Confirmed work for S69+

Items discussed during S65/S66 sessions that are deferred or still open.
Pick from this list next session when the current fixes are validated.

---

## 🔴 Confirmed work for S67+

### 1. Rock groups by geography (lithology refactor)
**User intent:** replace biome-based stone classification with a geographic overlay.  Real-world geology is regional (tectonic plates, volcanic arcs), not biome-specific.

**Implementation:**
- Paint `lithology_region.png` at 8k (4-6 region codes, by hand)
- Upscale to 50k (nearest) → `masks/lithology_region.tif`
- Config: `lithology.region_to_group` dict
- `core/surface_decorator.py`: rock-gap surface block lookup uses `lithology_region` pixel value → group instead of biome zone
- Result: "jungle on granite" vs "jungle on basalt" look different

**Estimate:** ~100 lines code + 30-60 min user painting

### 2. Snow NW-only (altitude gate + override repaint)
**User intent:** snow should be rare, high-altitude, concentrated on the NW island.

**Two-part:**
- **Override repaint:** user paints, move SNOWY_BOREAL_TAIGA + ARCTIC_TUNDRA + FROZEN_FLATS outside NW to warmer biomes (`BOREAL_TAIGA`, `BIRCH_FOREST`, etc.)
- **Altitude gate (code):** `snow_carpet.min_altitude_y = 300` (snow carpet only fires at Y ≥ 300).  5-line edit in `_apply_snow_carpet`.

Note: S66 partially addressed this via `BIOME_ALTITUDE_REMAPS` (BT→BA at Y>200) — snow prevention via MC biome tag.  Full NW-only work still pending.

### 3. Biome roster recheck (walk-through)
NICK PRIORITY #1 from S60.  Walk every biome using `memory/BIOME_VALIDATOR_CHECKLIST.md` (already built).  Verify placements correct.  Note any biome where the override-painted location doesn't match intent → override repaints.

### 4. LRFC ↔ TEMP_RAINFOREST transition
Options:
- Paint a new transition biome between them (content cost)
- Soft-mix ecotone (relax `ECOTONE_DENY_PAIRS` from 0.0 to 0.3 swap probability)

Lean option 2 — 5 lines.  Revisit after biome roster recheck.

### 5. Full world-wide 50k regen
Ready whenever user calls it.  Expected ~8 hours wall-clock at `--threads 4` for 9,409 tiles.  Recent globals landed: S62-S66 fixes, palette edits, ocean features, mangrove variant, fence connections, altitude remap, boundary smoothing, kelp clumping.

### 6. World overview map refresh
After 50k regen, regenerate `memory/s62_world_overview.jpg` from new mask state.  Script: `tools/render_world_overview.py`.  3-5 min.

---

## 🟠 Sidebar ideas (low priority)

### 7. `vandir_height.zip` datapack auto-install
S60 mandated.  Add pre-flight check in `run_pipeline.py` that copies datapack to target world's `datapacks/` if missing.  ~10 lines.

### 8. Cross-tile ecotone dither symmetry
S59 notes: GC + schematic ecotone dither inner-only (no padding), causing 1-pixel seam asymmetry at tile boundaries.  Defer until visible in-game after 50k regen.

### 9. Per-schematic authoring cleanup (schem_viewer workflow)
Several schematic files have authoring bugs surfaced during walks:
- `mpalm_a_sm.schematic` — 0 trunk blocks (pruned from LRFC in S64)
- `lrfc_tree_hardwood_*` — spruce_leaves in jungle biome (pruned S64)
- glowstone in one LRFC tree (S66 remapped globally to oak_wood)
- Misaligned anchors across multiple schematics

S60 `schem_viewer.py` supports Y-offset + "Save & Approve" (writes `anchor_y` + `anchor_review=false`).  Use for per-schematic fixes where simple remap isn't enough.

### 10. Per-pixel boundary sea-column check
User reported Y=64 columns "sticking out" of water at (84,84).  S66 ocean coastline smoothing should mostly handle this.  If still visible, add a post-pass that checks isolated land pixels (Y>=63) surrounded by ocean and lowers them.

---

## 🟢 Punt indefinitely (user said skip)

### 11. Floatclean post-stamp pass
User said "live with floater".  Skip unless individual cases really bother us.

### 12. Raw Gaea dune approach
User reversed this one — we're using Gaussian smoothing instead.

---

## ❓ Open questions / user clarifications needed

### Q1: end_stone in limestone — does it look OK?
S66 added `end_stone` to the limestone lithology palette (KARST_BARRENS + DRY_WOODLAND_MAQUIS).  User called it a gamble.  Walk (37,22) to validate; if ugly, remove in palette_editor.

### Q2: ARCTIC_TUNDRA 91% snow look
User said more snow, less everything else.  S65 dropped overlay coverages to 0.03 each.  Validate at (25,80) / (37,22).  If too uniform, nudge one layer up (gravel to 0.05 maybe).

### Q3: BIRCH_FOREST altitude remap
S66 remaps BT + BIRCH + MIXED_FOREST to BOREAL_ALPINE at high altitudes for snow prevention.  BIRCH threshold is 220 (higher than BT 200).  Validate visible effect at mountain tiles.

---

## Past sessions reference

- S62 (Apr 20): BOREAL_ALPINE fix, patches, ocean decorator, overview JPG
- S63 (Apr 20): trees-in-water, sand dune flag, ecotone deny, rock Y-fade, LRFC prune
- S64 (Apr 20): second-pass walk — BA→plains, footprint gate, snow carpet, ocean veg, mangrove variant, fence connect
- S65 (Apr 20): third-pass walk — ARCTIC snow bump, palette edits, dune smoothing, kelp clumping groundwork
- S66 (Apr 20): log root anchor, boundary smoothing extended, limestone palette, altitude remap, bush/grass tuning, glowstone fix, kelp clumping
