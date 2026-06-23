# S94e HANDOFF — no-snow datapack + BT split + bedrock + noise ocean; FINAL RENDER ready; ISLANDS next

## TL;DR
S94e shipped on top of the signed-off S93 river base. Branch `s85-cherry-picks`,
**tip `62e1603` (pushed). 0 cloud servers.** Everything below is committed.
Next: (1) fire the FINAL world render, (2) start the ISLAND work.

## WHAT SHIPPED THIS SESSION (all committed + pushed)
- `db9ea60` **steep-river void seal reworked as a connected steep-zone MASK**
  (core/chunk_writer.py [s93-void-seal]): seed = above-sea river cells where
  terrain slope ≥22° OR water drops ≥2 to a lower river neighbour, dilate ×4,
  seal every lateral air face of river water with structure_void. Excludes
  ocean (ctop>SEA) + lakes (river_meta==CHAN_LAKE, dilated 2). **USER WALKED +
  APPROVED.**
- `42c14ae` **no-snow biome datapack + BOREAL_TAIGA→minecraft:taiga.** 18 vanilla
  biome overrides in `assets/vandir_height.zip` (temp 1.0 / has_precipitation),
  colors pinned to current values so tint is unchanged; deserts flip precip
  true (rain, never snow). BT remapped meadow→taiga so it can carry snowy_taiga
  grey-green (0x80b497) distinct from BA's meadow green — REQUIRES re-render.
  Stage-1 walked: meadow snow-free, desert rains. See [[nosnow-biome-datapack]].
- `c006467` **render_50k_final.sh** (the SAFE full-render script). render_50k.sh
  marked DEPRECATED/UNSAFE.
- `ba20ef7` **bedrock floor guaranteed** at Y_MIN in every column (re-assert
  vol[0]=bedrock LAST, so abyssal-ocean columns don't overwrite it).
- `a3d5d42`+`62e1603` **noise ocean filler** (replaces superflat): dimension
  generator → minecraft:noise, custom `vandir:ocean` settings, fixed ocean
  biome, **seabed Y-60 ±4** (measured the canvas-border deep ocean: 100% ocean,
  median Y-60, range -55..-63 — so -60 matches; the first cut at -36 was wrong).
  1-block bedrock floor + sand seabed, water to 63.

## VALIDATED (don't re-litigate)
- Void seal walked + approved. No-snow datapack Stage-1 walked. Seam-pair tiles
  (35,21+36,21 cold w/ BT-taiga, 89,57+58, 20,63+21,63 desert, 51,53+52,53
  forest) rendered 8/8 OK + walked: BT taiga distinct from BA meadow, seams
  clean. 8 tiles installed in Vandir50k_verify.

## 4-AGENT PRE-RENDER AUDIT — outcomes (READ before the final render)
1. **Painted snowline snow STILL hits the "no-snow" biomes' terrain above their
   snow_lines** (BOREAL_ALPINE 540, BOREAL_TAIGA 600, forests 625-650) — the
   datapack only stops vanilla WEATHER snow, not the S89 snow_block paint
   (surface_decorator.py:4243 gap==7 consumer, only SBT+FROZEN_FLATS exempt;
   snow_lines at config/thresholds.json ~4832). **USER DECISION: LEAVE IT —
   intended high-altitude snow caps creeping onto non-snowy zones is wanted.
   Do NOT raise the snow_lines.**
2. **render_50k.sh is UNSAFE** (uploads stale local override over snapshot's S87
   banding, no height-heal, no bed-regen) → DEPRECATED. **Use render_50k_final.sh.**
3. BT→taiga safe; biome coverage complete (22 emitted, 18 overridden, the 4
   un-overridden are intended-snowy ×3 + sea-level ocean). Void-seal cost
   bounded (~600MB worst transient), memory fine at 40 workers/192GB.

## THE FINAL RENDER (ready to fire)
`DEST=/d/modrinth_vandir/saves/<freshworld>/region bash cloud_bake/render_50k_final.sh`
- Defaults: NBOXES=8, THREADS=40, TTL_MIN=300 (~3.5-4h, ~$4-5 box-hours).
- Self-provisions from snapshot 396927540; per box: git reset → heal_height_seams
  --inplace → rm bed caches → single-thread PRE-WARM (builds global 8192² bed
  cache so the 40 workers HIT not race) → render all 97 z-rows → verify_render_health.
- **WATCH box logs for MIGRATED/HIT — abort on `rebuilding`/`MISMATCH`** (bed-cache law).
- KILL boxes when satisfied (live_boxes.txt has the kill loop); auto-killer 300m backstop.

## CRITICAL GOTCHAS
- **Local masks/override.tif is STALE (S86 banding) ≠ the box snapshot (S87).**
  The full render MUST run via render_50k_final.sh (uses snapshot override,
  does NOT upload local). NEVER render the full world from local masks. See
  [[override-tif-stale-vs-render]].
- **The datapack generator is creation-baked into level.dat** → the noise ocean
  filler + no-snow biome overrides apply to NEWLY-CREATED worlds. Create the
  final world WITH vandir_height.zip in datapacks/ BEFORE first load (also
  mandatory for the 768 height, or chunks OOB-crash). dimension_type (height)
  re-reads each load; the generator does not.
- Datapack = `assets/vandir_height.zip`: height(768) + 18 no-snow biomes +
  vandir:ocean noise gen. Rollbacks: .bak_pre_biomes, .bak_pre_ocean. NEEDS an
  in-world load-test on a fresh world (fly past the rendered edge → confirm the
  noise seabed/water look right; restore .bak_pre_ocean if MC rejects it).

## NEXT BIG THING — THE ISLANDS (offset-render, minimize Gaea)
User wants real-island heightmaps added as offset regions in the world:
- **St Kitts & Nevis + St Eustatius** — west of Vandir, slightly N, edge of horizon.
- **Kostati archipelago (NE):** St Vincent + the Grenadines (LARGEST) + Tobago
  Cays / Carriacou as islets.
- **Lore scale: Vandir = Great Britain (UK minus Ireland, ~209,000 km²) by AREA.**
  Vandir is 50×50km physical (2,500 km²) → linear 9.15× compression → **1 lore-km
  ≈ 109 blocks.** St Kitts ~3,200 blocks long (~6 tiles), Nevis ~1,300 (~2.5),
  Statia ~875 (~2); all-three bbox ~9-11 tiles. St Vincent+Grenadines is the
  large one in the NE cluster.
- **Heightmaps:** unrealheightmap.github.io (16-bit PNG, real-world SRTM) or
  OpenTopography (30m SRTM GeoTIFF). Export **~2048px 16-bit** (SRTM 30m has only
  ~1,500px real detail for a 50km box; 2048 = all detail + headroom). St Kitts
  group bbox: 17.10-17.55°N, 62.53-63.05°W. Volcanic cones are steep (Nevis Peak
  985m, Liamuiga 1150m) → per-island height spline OR let an eroder exaggerate so
  they aren't pancakes under the 9× compression.
- **Approach = OFFSET-RENDER:** each island gets its own small mask set + override
  paint, rendered at a world-coord offset (chunk_writer is coordinate-agnostic),
  ocean filler between. NOT a unified-canvas regen (that'd need all masks +
  global hydrology re-done at a bigger size).
- **KEY IDEA (user wants this): minimize Gaea almost entirely.** Confirmed in
  code: rivers/lakes/flow are D8-derived from height (hydrology_precompute), slope
  is computable (eco_gradients.compute_cliff_deg / np.gradient), floodplain/
  windthrow/beach/dunes are rebuild-script-derived, override+lithology are paint.
  Gaea's only unique value = EROSION SIM — and a real DEM is already eroded, so
  it's redundant for the islands. **Build `derive_masks_from_height.py`** that
  generates flow (the D8 accumulation we already compute), slope (gradient),
  erosion-proxy (curvature×flow) FROM the DEM, then either MATCH Gaea's 0-1
  normalization so existing thresholds work OR retune the slope/flow thresholds
  (rock-gap, windthrow, biome moisture in biome_assignment). Contained module +
  calibration, not a rewrite. (Raw-noise invented terrain would still need an
  eroder — keep that option.)

## OPEN/PENDING from earlier (still true)
- validate_3x3 48_48 baseline drifted since S72 (validator, not production) — re-sync own session.
- Memory written this session (.claude auto-memory): override_tif_stale_vs_render,
  nosnow_biome_datapack.
