# S72–S79 Handoff Log (2026-04-28 → 2026-04-30)

Long arc — 8 sessions, 6 commits on master, ~3-day stretch.

## TL;DR

| Session | Commit | What landed | Status |
|---------|--------|-------------|--------|
| S72 | `91519cc` | 6-bug river fix (water clamp, gravity sort, fallback drop, depth tune, dead code) | ✅ shipped |
| S73 v1-v9 | `e4b79bf` + `43e04dd` | Plateau-then-per-pixel iterations, 1D smoothing, full-trench fill, kernel=17 bank-lift, hole fill, +1 trench drop | ✅ shipped, "beautiful" per user |
| S74 | `5ce28a3` | `vandir_height.zip` auto-install + bushes-only canopy reject (later reverted) | ✅ shipped |
| (S74-batch) | — | All 26 biome reference tiles rendered + Vandir_BiomeRoster.zip distribution | ✅ shipped to friends |
| S75 | `d31bce6` | SEMI_ARID grass + connectivity height-cost JS port + canopy-reject revert | ⚠️ connectivity broke |
| S76 | `c8e6e08` | More SEMI_ARID grass + connectivity meander/widen/extend | ⚠️ connectivity worse |
| S77+S78+S79 | `b5635c2` | **Reverted connectivity to S73-v9** + ARCHIVED behind flag + lake `-1` fix + post-process passes | ✅ shipped, awaiting rebuild |

---

## What worked (still shipped)

### Rivers (S73-v9 final recipe — `core/river_carver_v2.py`)

For mainstem rivers (NOT connectivity), the recipe is:

1. **1D centerline path smoothing σ=8** — water_y values smoothed ALONG each centerline component's flow path, then re-propagated via EDT.  Result: every Voronoi cell takes its nearest centerline pixel's smoothed water_y → cross-sections are uniform → no transverse Y variation, even at curves/meanders.  Y drops only along flow direction.
2. **Full trench fill** — `water_zone = footprint & ~lake_mask` (entire footprint).  Drops the old `factor < edge_threshold` gate.  Banks bounded by surface_y vs water_y comparison, not edge gate.
3. **Bank-lift kernel size=17** — radius 8 covers MC's 7-block water-flow distance + 1 safety cell.  Uses `binary_fill_holes` to detect interior holes (filled with water, not lifted as terrain) and lifts only cells OUTSIDE the water hull.
4. **Trench drop = 1** — `depth_blocks += 1.0` and `water_y -= 1.0` (subtract before SEA_LEVEL clamp so coast meets ocean cleanly).  Bed and water both 1 block lower; banks visibly above water.
5. **S72 6-bug recipe carried forward** — water_y clamps to SEA_LEVEL not surface_out+1; gravity per-component topological walk via dist_from_ocean (NOT argsort by surface_y); legacy `pre_carve_y - 1` fallback dropped; `RIVER_DEPTH_FRAC=0.25, DEPTH_BASE=1.0` with slope_atten + elev_atten; dead `river_depth_map` removed.

### Datapack auto-install (S74 — `run_pipeline.py`)

`assets/vandir_height.zip` checked into repo.  `run_pipeline.py:577-595` auto-copies to `output/datapacks/vandir_height.zip` on every run.  When user copies output → world, datapack travels with MCAs.

### SEMI_ARID grass overhaul (S75/S76 surviving piece)

**`core/surface_decorator.py` SEMI_ARID_SHRUBLAND palette:** all `coarse_dirt`/`podzol` swapped to `grass_block`.  8 of 10 channels now grass; sand+gravel kept on dry/erosion only.

**`config/thresholds.json` noise_layers_biome SEMI_ARID:** base `coarse_dirt` → `grass_block`, `podzol` (noise3) → `grass_block`, sand coverage 38%→28%, moisture grass 45%→55%.

### 26-biome reference render + distribution package

All 26 biome reference tiles rendered with `--threads 2` parallel → 137 min wall time, 0 errors.  Copied to `Vandirtest10/region/`.  Packaged as `Vandir_BiomeRoster.zip` (127MB):
- `level.dat` from Vandirtest10
- `region/` with all 26 reference MCAs
- `datapacks/vandir_height.zip`
- `README.txt` with TP commands grouped by climate
- `world_map.png` — 6250×6250 enhanced topographic map with terrain colormap (tan→green→brown→gray→snow), hillshade (NW light), biome tint at 30%, lithology overlay where rock_gap>0, slate-gray slope highlight, beach overlay, lakes blue, rivers blue, snow override per `snow_gap.tif` + soft peak blend

Uploaded to gofile.io: https://gofile.io/d/kZO7Zl (zip), https://files.catbox.moe/qsv1vs.png (map standalone).

### Lake water -1 fix (S77 — `run_pipeline.py:419`)

Was `lake_water = pre_carve_y[lk].min() - 1` rendering lakes 1 block below the actual hydro_lake_wl spill elevation.  Now drops the `-1`: `lake_water = pre_carve_y[lk].min()`.  Lake source matches column_generator's underwater surface_y (set to `water_level - 1`) → lake surface visible at expected `water_level` Y.

### S78 connectivity post-process passes (`run_pipeline.py:504-525`)

Two passes that run AFTER all river/lake water-Y assignments:

1. **Wall-to-wall water fill in conn footprint** — dilate `conn_channel_mask` by 4 cells, exclude lakes, find cells where `surface_y >= river_water_y` (water source buried under terrain), lower surface to `river_water_y - 1` so water visible.
2. **Lake water overrides connector vertically** — when a connectivity channel overlaps lake cells, those cells keep `lake_water_levels` (set on line 422); channel water/carve only applies to `ch & ~lake_mask`.  No more columns of mismatched-height connector water inside lakes.

Both passes are no-ops when `_CONNECTIVITY_ENABLED = False` (current state) — kept active for when connectivity is rebuilt.

---

## What didn't work (archived / reverted)

### S75 connectivity height-cost JS port

Replaced the original `1/(flow + concavity*5)` cost surface with a JS-style height-based cost: `1 + 150 * height_norm - 30 * concavity - 50 * flow_norm + 35 * meander_noise`.  Intended fix for "Dry Pine Barrens connectivity climbs ridges" (because flow_tile is uniformly ~0 in dry biomes → 1/(0+0+ε) = uniform ~1000 cost → Dijkstra picks geometric shortest path).

**Bug:** `height_norm` is normalized 0..1 with raw 0..65535 values, so 1 MC block of elevation = 1/65535 ≈ 0.000015 normalized → cost contribution = 150 × 0.000015 = **0.0023 per cell**.  Versus noise contribution **≈ 17 per cell**.  Height penalty was effectively ZERO; paths were 100% noise-driven, climbing whatever terrain the simplex noise had a low spot at.

### S76 connectivity meander+widen+extend

Built on S75's broken cost surface.  Added:
- Post-smoothing perpendicular meander offset in `_draw_tapered_channel` (sinusoid + simplex modulation, amp 4.5, period 22, tapered to 0 at endpoints).
- `_extend_into_lake` helper appending 3 cells INTO lake interior (path was ending on perimeter, leaving 1-cell gap to lake water).
- Wider channels: `channel_order=3`, `base_width=4`, `taper_max_w=5-6` (mouth flares 9-10 cells).

User feedback: still floating columns + no meander + dam bands.  The double-meander (cost surface noise + post-process offset) plus heavy spline smoothing created arbitrary S-curves that didn't follow valleys.  Wide channels looked out of proportion to their parent rivers.

### S77 connectivity full rewrite + R2R + lake-interior target + cost-units fix

Stripped back to JS-minimal: `1 + 150*h_norm + 35*noise`, lake×5 penalty, target=lake interior.  Added river-to-river connector pass for orphan endpoints (5-120 cell distance threshold).

Then S77 v2 fixed the cost-units bug: switched to `h_blocks` (surface_y - SEA_LEVEL in MC blocks) with `HEIGHT_PENALTY = 5.0`.

User feedback: "its worse lol" — artifact channels, dam bands, air gaps, no meander, "nothing is right."  Best guess on root cause: 
- R2R pass connecting fragments that shouldn't be connected (DRY_PINE_BARRENS has intentionally patchy rivers)
- Skinny channels (back to natural Strahler width) leaving more bank zone with terrain above water = "air gaps"
- No post-process meander (cost-surface noise alone after spline smoothing produces straight-ish paths)
- Possibly the trench drop on rivers (avg-1.5) + raised lake level (post `-1` fix) creating 1-block junction step

### S77+S78+S79 final outcome

User: "Undo. delete the connectivity layer... rebuild it from ground up in a bit."

Action: 
1. Reverted `core/river_carver_v2.py` to S73-v9 commit (`43e04dd`) — drops all S75/S76/S77 connectivity changes.
2. Added `_CONNECTIVITY_ENABLED = False` flag at line 727.  Original code preserved in-place at lines 727-895.
3. Kept S78 + S78b post-process in run_pipeline.py — no-op while flag is off, ready when rebuilt.
4. Kept lake `-1` fix from S77.

(51,53) MIXED_FOREST rendered cleanly with connectivity disabled.  Rivers + lakes from Gaea hydro masks, no artificial connectors, no bugs.

---

## Top carry-forward for next session

### 🔴 PRIORITY 1: Rebuild connectivity layer ground-up

**Spec:** when a Strahler-detected river end doesn't reach a lake or ocean, draw a connector channel.  When two river fragments are nearby but disconnected, optionally bridge them.  When a lake has a clear spill point, draw an outflow to the nearest river.

**What broke last 3 attempts:**
1. S75 — used `height_norm` (0..1) where JS uses MC blocks, cost surface flatlined, paths random.
2. S76 — added bandaids (post-process meander, wider channels, lake-interior extend) on top of the broken S75 cost.  Made artifacts worse.
3. S77 — full rewrite, fixed cost units, but R2R pass + skinny channels + no post-process meander combined to produce different artifacts.

**What to do differently this time:**

- **Start with diagnostics, not iteration.**  Before writing any pathfinding code, build a tool that visualizes the cost surface as a 2D heatmap.  Render Dijkstra paths on top.  Walk through dry biomes (DRY_PINE_BARRENS 30,49) and rich-flow biomes (51,53) separately and compare cost shapes.
- **Validate per-tile, not full-rebuild.**  Render ONE tile at a time, compare WITH connectivity vs WITHOUT, walk in-world.  Don't push to master until walked.
- **Keep changes additive.**  The post-process passes (S78 wall-to-wall, S78b lake override) are decoupled from the path-drawing — they work or no-op cleanly.  Build the new path-drawing code as opt-in via the `_CONNECTIVITY_ENABLED` flag.
- **Reference both:** original `1/(flow+concavity)` cost — handles flow-rich biomes well, fails dry biomes — and JS pathFindDown — handles all biomes via height penalty, but JS uses block units not normalized.  Maybe blend: prefer flow-aware cost where flow exists, fall back to height-based when flow is sparse.
- **Validate diagnostics first.**  The Explore agents reported JS specs cleanly enough — but I trusted those reports too much without verifying behavior. Include a render comparison in the validation loop.

**Code locations:**
- Archived path-drawing code: `core/river_carver_v2.py:727-895` (gated by `_CONNECTIVITY_ENABLED`)
- Active post-process passes: `run_pipeline.py:478-485` (lake-override) + `run_pipeline.py:504-525` (wall-to-wall fill)
- Cost-surface helpers used: `_least_cost_path` (Dijkstra), `_smooth_path` (cubic spline), `_draw_tapered_channel` (stamp width per-pixel from local tangent)

### Other carry-forwards (lower priority)

- **Per-biome schematic + vegetation walk** — NICK PRIORITY #1 from prior sessions.  Reference tiles checked into the 26-biome batch + Vandir_BiomeRoster.zip.  Walk-in pass NOT YET done.
- **Floating leaves on downhill side of trees** — S74 added a bush-only canopy reject, then user said it wasn't needed for current renders.  Reverted in S75.  Real fix: schematic-by-schematic via `schem_viewer.py` Y-editor.
- **BOREAL_ALPINE altitude-snow follow-up** — S62 landed per-section sky-biome override.  Validation pending.
- **Override Studio `Save` silently skips upscale** — `Ctrl+S` saves only override_final.png; `override.tif` goes stale, poisons downstream renders.
- **`upscale_override_vectorized.py:272` stale-composite bug** — `np.where(vec > 0, vec, base)` makes stale borders dominate fresh paint.
- **`_BIOME_CLIFF_STONE`** missing BOREAL_ALPINE entry → cliff-face seams at alpine boundaries.
- **RIPARIAN_WOODLAND + FRESHWATER_FEN routing prune** — 42 schematic entries, found at (73,53) 51% and (8,74) 45%.

### Always-available

- **World-wide 50k regen** — ready whenever called.  S60+S69+S70+S71+S72+S73+S74 all landed since last full regen.  Re-render with current code = current state of all biomes.

---

## Key file references

| File | Purpose |
|------|---------|
| `core/river_carver_v2.py` | Main river carving.  Rivers active, connectivity gated off (line 727). |
| `run_pipeline.py` | Pipeline entry.  Lake -1 fix (419), datapack auto-install (577-595), S78 post-process (504-525), S78b lake override (478-485). |
| `assets/vandir_height.zip` | Datapack for 32-section vertical extension (Y -64 to 448).  MANDATORY for chunk loading. |
| `memory/biome_reference_tiles.csv` | 26-biome reference tile coordinates + purity. |
| `memory/BIOME_VALIDATOR_CHECKLIST.md` | Walk checklist for biome validation. |
| `dist/Vandir_BiomeRoster/` | Local copy of distribution package. |
| `dist/make_world_map.py` | World map renderer (heightmap + slope + lithology + snow + rivers/lakes overlay). |

---

## Commit hashes (for `git log` lookup)

- `b5635c2` — S77+S78+S79: revert connectivity, archive, lake fix, post-process
- `c8e6e08` — S76: SEMI_ARID grass + connectivity meander/widen (REJECTED later)
- `d31bce6` — S75: connectivity height-cost (REJECTED later) + canopy revert
- `5ce28a3` — S74: vandir_height datapack + bushes-only canopy float reject
- `43e04dd` — S73-v9: 1D smoothing + full-trench fill + 1-block trench drop
- `e4b79bf` — S73 (v1-v8 iterations): plateau quant attempts
- `91519cc` — S72: river water surface + trench depth six-bug fix

Master is at `b5635c2` and matches Vandirtest10's current MCA state.
