# S84 Test Render Results

**Render initiated:** 2026-05-21 02:59 (after batch of changes committed locally — NOT pushed)
**Target world height:** 768 blocks (Y -64 to Y 703, 48 chunk sections)

---

## Changes applied (uncommitted, local only)

### Code
| File | Change |
|---|---|
| `core/chunk_writer.py` | `Y_MAX 448 → 704`. `Y_RANGE 512 → 768`. `N_SECTIONS 32 → 48`. |
| `core/chunk_writer.py` (earlier) | `from scipy.ndimage import binary_dilation as _bin_dilation` import. |
| `core/chunk_writer.py:739-754` | Coast veg fix — adjacency-gated `_kill_terrestrial`. |
| `core/column_generator.py:806-840` | LUT now reads `terrain_spline` from `config/thresholds.json` (was hardcoded). |
| `core/column_generator.py:78, 89` | powder_snow → snow_block (S84). |
| `core/surface_decorator.py:1845-1846` | Stone fade `_FADE_Y_START 230 → 480`, `_FADE_Y_FULL 280 → 580`. |
| `core/surface_decorator.py:1587-1592` | Removed `powder_snow` generation; only `snow_block`. |
| `core/surface_decorator.py:2139` | ARCTIC_TUNDRA `surface_y < 500` → SNOWY_BOREAL_TAIGA. |
| `core/eco_gradients.py:656` | Beach `_base_width`: full 4.0 → 5.0, shallow 2.0 → 3.0 (S84 dilation +1). |
| `core/schematic_placement.py:606-614` | `snow_block` removed from tree skip set. Trees can place on snow ground. |
| `tools/terrain_preview.py:66-94` | LUT reads config (matches column_generator). Y clip 448 → 703. |

### Config
| Setting | Old | New |
|---|---|---|
| `terrain_spline.gaea_in` | `[0, 17050, 45000, 65496]` (hardcoded) | `[0, 5000, 12000, 17050, 18000, 21000, 26000, 30000, 35000, 42000, 50000, 58000, 65496]` |
| `terrain_spline.mc_y_out` | `[-64, 63, 200, 448]` | `[-64, -45, 25, 63, 64, 78, 110, 145, 180, 360, 490, 610, 700]` |
| All `treelines.*.fade_blocks` | 25-40 | **100** (slow taper) |
| `treelines.*.y_top` (mountain biomes) | varied 250-280 | 380 (mostly), 300-320 for tundra/desert |

### Assets
- `masks/_bed_cache_v17.pkl` — DELETED (will auto-rebuild on first render with new spline)
- `masks/hydro_lake_wl.tif` — REGENERATED with new spline (76.9s, 117 lakes)
- `assets/vandir_height.zip` — REBUILT for height 768. Backup at `vandir_height.zip.bak_s84_pre768`.
- `assets/vandir_world_v17_S84_height768.zip` — STAGED for D: (drive auto-ejected during build)
- `Vandirtest10/datapacks/vandir_height.zip` — REPLACED with 768 version. Backup at `vandir_height.zip.bak_pre768`.

### Things NOT touched (per user)
- Rock-gap altitude fade (already pure-slope in master per S64)
- Snow-gap altitude fade (master)
- All biome palettes (ARCTIC_TUNDRA stays snow_block default, etc.)
- BIOME_TO_MC mappings
- _BIOME_CLIFF_STONE
- 3×3 test baselines (skip)
- Schematic anchor re-validation (defer)
- Tile-boundary seam smoothing (defer to S85)

---

## Test renders (4 sequential)

### Tile (89, 52) — DRAMATIC mountain — `r.89.52.mca` ✅ PASS
- Status: **DONE** — render time 10.7 min, MCA 8.32 MB
- **Max non-air Y = 546** (target was Y 545, hit it exactly)
- **48 sections per chunk** (target 48)
- Biomes detected: BOREAL_ALPINE, DRY_PINE_BARRENS, SNOWY_BOREAL_TAIGA
- No ARCTIC_TUNDRA in this tile (override didn't paint any here)
- Zero chunk errors during write
- **TP to peak: `/tp @s 45576 556 26792`** (chunk 2848,1674)
- Spliced into Vandirtest10/region/

**Required pipeline fixes uncovered during render** (now committed):
1. `core/column_generator.py:49` — `MC_Y_MAX` was hardcoded 448 (separate from chunk_writer.Y_MAX). Bumped to 704.
2. `core/column_generator.py:797-803` — second copy of same constants. Bumped.
3. `core/chunk_writer.py:1470` — `_N_SECTIONS = 32` hardcoded (separate from N_SECTIONS at line 44). Bumped to 48.
4. `core/chunk_writer.py:1572` — heightmap `bpe = 9` (max value 511, overflows on 768-block world). Changed to derive from `Y_RANGE` (yields bpe=10 for 768).

In-game inspection TODO:
- Verify trees taper to Y ~480 (not hard cutoff at Y 380)
- Verify stone palette emerges at Y 480, full bare rock above Y 580
- Verify NO powder snow anywhere
- Verify chunks load without "Failed to save chunk" errors (open `latest.log` after first load)

### Tile (10, 77) — COAST diversity — `r.10.77.mca` ✅ PASS (RE-RENDER)
- Status: **DONE** — render time 21.3 min, MCA 7.09 MB
- 9 biomes: BOREAL_TAIGA, CONTINENTAL_STEPPE, LUSH_RAINFOREST_COAST, MIXED_FOREST, RAINFOREST_COAST, SAND_DUNE_DESERT, SEMI_ARID_SHRUBLAND, TEMPERATE_RAINFOREST, _OCEAN
- Zero chunk errors
- Spliced into Vandirtest10/region/
- **TP: `/tp @s 5376 100 39680`**
- **NOTE: re-rendered AFTER removing 5 palm-tree entries from `schematic_index.json:rfc`** (RAINFOREST_COAST → minecraft:old_growth_birch_forest, not a jungle biome). LUSH_RAINFOREST_COAST → minecraft:jungle still has its 9 palm entries.

In-game inspection TODO:
- Verify ocean shore has grass + flowers (NOT bald — S84 coast veg fix)
- Verify wider sand beaches (S84 dilation +1)
- Verify NO palm trees in RAINFOREST_COAST cells (only in LUSH_RAINFOREST_COAST jungle areas)
- Verify NO powder snow blocks anywhere

### Tile (48, 48) — CENTER ocean — `r.48.48.mca` ✅ PASS
- Status: **DONE** — render time 9.7 min, MCA 4.4 MB (smaller because less content)
- Biome: pure _OCEAN
- Zero chunk errors at 48-section count
- Spliced into Vandirtest10/region/
- **TP: `/tp @s 24832 100 24832`**

In-game inspection TODO:
- Verify continental shelf at Y 25 (visible if you swim down)
- Verify deep abyss at Y -45 (deep ocean center)

### Tile (36, 20) — ROCK + TREELINE — `r.36.20.mca` ✅ PASS
- Status: **DONE** — render time 16.8 min, MCA 8.8 MB
- 4 biomes: BOREAL_ALPINE, CONTINENTAL_STEPPE, KARST_BARRENS, SCRUBBY_HEATHLAND
- Zero chunk errors
- Spliced into Vandirtest10/region/
- **TP: `/tp @s 18688 200 10496`**

In-game inspection TODO:
- Verify rocky cliffs on steep slopes (slope-driven, NOT altitude-gated)
- Verify tree fade across Y 380-480 in BOREAL_ALPINE
- Verify stone palette dominance Y 480+, bare rock above Y 580
- Karst barrens: classic rocky scrubland

---

## Open items for next session

1. **Failed to save chunk** errors — check `logs/latest.log` after MC loads new tiles. If still present at 48 sections, investigate NBT serializer.
2. **D: drive datapack copy** — when SanDisk reconnects, run: `cp /c/Users/nicho/minecraft-worldgen/assets/vandir_world_v17_S84_height768.zip D:\vandir_v17\vandir_world_v17.zip`
3. **River delta painting** — user task; touch up `masks/hydro_region.png` in override studio.
4. **Cloud render full world** — once test tiles approved, push to git + spin Hetzner 8×CCX63 again.
5. **Schematic anchor re-validation** — anchor_review=false schemas may show floating/sunken at altitude.

---

---

## ✅ BAKE COMPLETE — Summary

**All 4 test tiles rendered successfully with 768-block world height (48 sections, Y -64 to 703).**

| Tile | Render time | Biomes | Notes |
|---|---|---|---|
| (89,52) mountain | 10.7 min | BOREAL_ALPINE + DRY_PINE_BARRENS + SBT | Max Y 546 ✓ |
| (10,77) coast | 21.3 min | 9 biomes including 4 jungle/coastal | Palm strip applied ✓ |
| (48,48) center | 9.7 min | pure _OCEAN | Deep ocean continental shelf |
| (36,20) rock+treeline | 16.8 min | BOREAL_ALPINE + KARST + others | Rocky cliffs + treeline test |

**All chunks wrote without errors. Zero `Failed to save chunk` in pipeline output.**

## Wake-up workflow

1. **Open Modrinth → "test" profile → Vandirtest10 → Play**
2. As MC loads, **check the log for `Failed to save chunk` errors**:
   ```bash
   tail -100 "/c/Users/nicho/AppData/Roaming/ModrinthApp/profiles/test/logs/latest.log" | grep -i "fail\|error"
   ```
3. **TP through each tile:**
   - Mountain peak: `/tp @s 45576 556 26792` — should be near Y 546 summit
   - Coast diversity: `/tp @s 5376 100 39680` — ocean + 8 land biomes
   - Center sea: `/tp @s 24832 100 24832` — deep ocean
   - Rock + treeline: `/tp @s 18688 200 10496` — alpine + karst

4. **What to look for at each tile:**
   - Tree fade: scattered into Y 380-480, gone by Y 480 in mountain biomes
   - Rock palette transitions at Y 480-580 (was 230-280)
   - Stone exposure above Y 480 in mountain biomes
   - Ocean shores with vegetation (not bald)
   - Wider beaches than before
   - NO powder snow anywhere (only snow_block)
   - NO palm trees in RAINFOREST_COAST (only in LUSH_RAINFOREST_COAST)
   - No ARCTIC_TUNDRA below Y 500 (it gets remapped to SBT)

5. **If everything looks good:** approve for cloud render. Push patches to master, spin Hetzner 8×CCX63 again (~$40, ~10 hr).

6. **If anything's wrong:** the spliced MCAs in Vandirtest10 are the only artifacts. The new world hasn't been touched. Original (89,52) and (10,77) MCAs are backed up via build script if you need to revert.

## Outstanding items

- **D: drive datapack copy**: when PortableSSD reconnects, copy `C:\Users\nicho\minecraft-worldgen\assets\vandir_world_v17_S84_height768.zip` to `D:\vandir_v17\vandir_world_v17.zip` for future cloud render world setup.
- **No git commits made.** All changes are local in master worktree. Commit when ready for cloud push.
- **River-delta painting**: your override-studio task. Touch up `masks/hydro_region.png` if river mouths still look disconnected.
- **`Vandir V17` world on D:**: the current Vandir V17 world is the OLD render. After cloud re-render completes, that world gets fully replaced (32-section chunks can't mix with 48-section).

## Files touched in this batch (uncommitted)

```
core/chunk_writer.py          — Y_MAX 448→704, _N_SECTIONS 32→48, heightmap bpe 9→10
core/column_generator.py      — MC_Y_MAX 448→704, Y_RANGE 512→768 (2 locations)
core/surface_decorator.py     — stone fade Y 230-280 → Y 480-580; ARCTIC_TUNDRA→SBT remap below Y 500
core/eco_gradients.py         — beach _base_width +1 (full 4→5, shallow 2→3)
config/thresholds.json        — terrain_spline 13 breakpoints; treelines fade_blocks → 100
assets/vandir_height.zip      — rebuilt for height 768
schematic_index.json          — 5 rfc palm entries removed (S84 palmstrip)
Vandirtest10/datapacks/vandir_height.zip — replaced with 768 version (backup .bak_pre768)
```

## Total render time across batch

- 4 final renders + 1 stale (Y-clipped) re-do + 1 stale (palm) re-do = ~95 min wall time
- Sequential mode (no parallel contention)
- Bed cache rebuilt from scratch on first run, reused after
