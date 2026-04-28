# S71 Handoff — Pick up here next session

**Status: S71 multi-day arc shipped. WP-style river overhaul JUST LANDED — needs walk validation. Plenty of biome polish + lithology fix + FF↔AT swap also shipped this session.**

## Tldr for next session

1. **First action: ask user about COASTAL_HEATH (37,8) walk.** The final tile render (8m26s, exit 0) was the FULL WP river upgrade test. User had not yet walked it at session end. Tile shipped to `Vandirtest10`. TP `/tp @s 19200 90 4352`. F3+A reload first.

2. **Verify the river slope-water issue is fixed.** User explicitly noted at handoff time: "we still haven't fixed the river slope water issue." That feedback was given pre-S71-final-river plumbing. The full WP package (water_y_field carry-through + delta connectivity + meander + iterative gravity + edge water-skip) only landed at the very end of the session. **The river fix is UNVALIDATED in-world.**

3. **If river is fixed:** continue with whatever else user has on the list (BIRCH_FOREST extra walk, schem editor cleanups, more biome tuning).

4. **If river is still tilted/broken:** likely culprits to check:
   - Edge water-skip threshold may be too tight — `factor < 0.45 + 0.1 × (1 − clamp(slope×8, 0, 1))`. Try widening to 0.55.
   - Per-pixel water_y_field may be overwritten downstream — verify `chunk_writer` actually uses it per-column when filling water (not just at centerline).
   - `run_pipeline.py:388` lake-blend zone may smear water_y_field by mixing with old `pre_carve_y - 1` for connectivity channels — re-trace logic.
   - Meander amplitude may be too aggressive — drop from 4.0 to 2.0 if rivers look spaghetti-like.

## Critical "carry-forward" rules

- **NOISE_PATTERNS.md is the canonical doc** for noise/dither code. ALWAYS READ before writing noise/random/probability. Specifically §4 plateau-clamp pattern `[0.15, swap_cap]` for boundary dither — the formula bug I had was ramping to 0 instead of clipping. Fixed S71-final.
- **Lithology mask is now source-of-truth** for cliff banding + high-elevation stone fade in surface_decorator. Per-biome `zone_to_group` is FALLBACK only when lithology id == 0 (unpainted). Don't revert to per-biome unless explicitly told.
- **FF and AT are SWAPPED conceptually as of S71:** FF = "Tundra Valley" lowland permafrost meadow (badlands MC, scattered snow_carpet, NO snow on tree leaves, smallest pines very-very-sparse). AT = mountain-snowy harsh (snow_block surface, gap==7 snowgap applied).
- **FF is removed from `_SNOWY_BIOMES`** (chunk_writer:2059) — no snow-on-trees post-pass for FF.
- **FF is removed from `cfg.snow_carpet.biomes`** (thresholds.json) — explicit GC palette controls snow_carpet placement.
- **FF + AT are in `_NO_SWAP_BIOMES`** (schematic_placement.py) — neighbor biomes' md/lg trees can't leak in via ecotone seam swap.
- **Leaf-column blacklist** (schematic_placement.py:284): 4 schematics globally rejected. Don't re-add.
- **BOREAL_TAIGA was removed from `BIOME_ALTITUDE_REMAPS`** (chunk_writer:113). MC tag = `minecraft:stony_shore` directly. Trade-off accepted: stony_shore temp=0.2 may freeze rivers in BT regions.
- **AT keeps its high-elevation stone-fade EXEMPTION** (surface_decorator:1907 has `& (biome_grid != "ARCTIC_TUNDRA")`). Don't revert.

## Major code areas modified this session

| File | Highlights |
|---|---|
| `core/river_carver_v2.py` | Full WP-style guardrails carve. Returns 4-tuple now (added `water_y_field`). Section 7 + 7a + delta extension all rewritten. |
| `core/surface_decorator.py` | Plateau-clamp ecotone dither. Lithology source-of-truth in gap==5 + high-elevation fade. AT/FF palette swap. WHITE-noise FF surface palette. |
| `core/schematic_placement.py` | Leaf-column blacklist. SARID juniper filter (acacia variants out). FF/AT no-swap-into. BIRCH clumping fix. AT/FF SBT-mirror size=sm only. PlacementRecord adds `species` field. |
| `core/chunk_writer.py` | BIOME_ALTITUDE_REMAPS removed BT. BIOME_TO_MC: COASTAL_HEATH→savanna_plateau, CONTINENTAL_STEPPE→cherry_grove, SCRUBBY_HEATHLAND→badlands, FROZEN_FLATS→badlands, RIPARIAN_WOODLAND→mangrove_swamp, BOREAL_TAIGA→stony_shore. _SNOWY_BIOMES = {SBT, AT} (FF removed). water_col_mask threading. |
| `core/eco_gradients.py` | Reverted snowgap mask exception for AT (snowgap applies again). |
| `config/thresholds.json` | ecotone_width_px 24→40, ecotone_swap_cap 0.5→0.75. AT noise_layers FF-cloned. FF noise_layers white-noise palette. snow_carpet biomes: FF removed. |
| `run_pipeline.py` + `tools/validate_test_tile.py` | water_y_field threading from carve_rivers → chunk_writer. |

## Tile state (Vandirtest10/region/)

All shipped at session end:
- (24,80), (25,80), (32,13) AT, (33,6) FF, (37,8) COASTAL_HEATH — most recent renders
- Plus the 22-tile main roster from S70 carry-forward + leaf-column verification (29,76), (19,63), BIRCH (60,41)

User reload via F3+A required since MC was open during recent copies.

## Walk-feedback tracker

### Last walk feedback (pre-final-river)

- (24,80) plateau dither verification — pending
- AT (32,13) — no pines visible, user OK with that
- FF (33,6) — beautiful EXCEPT user wanted no snow on trees + change MC to badlands. Both shipped in next render.
- COASTAL_HEATH (37,8) — river was "trench better but water still wrong" — full WP river upgrade landed AFTER this feedback.

### Pending validation
- (37,8) post-S71-final-river — **river slope-water + delta connectivity + meander all unverified.**

## Open carry-forward

1. **River slope-water final verification** — most important.
2. **MANGROVE_COAST floating roots** — schematic-level fix, user editing in schem_viewer.
3. **DRY-biome small trees still bushy** on some non-blacklisted schemas (`_b_sm`, `_f_lg`). Possibly more entries to blacklist.
4. **Override Studio Save→upscale auto-trigger** (CLAUDE.md backlog).
5. **Full 50k regen** — final stage, after walks pass.
6. **Future: meander amplitude tuning** — current MEANDER_AMP=4.0 blocks. May need adjustment.
7. **Future: BOREAL_TAIGA frozen-rivers issue** — stony_shore temp=0.2 may cause rivers/lakes to freeze in BT. Watch for this in walks. If problematic, swap MC to `minecraft:windswept_savanna` (temp 1.1) or revert remap.

## Quick commands

### Re-render single tile
```bash
PYTHONUNBUFFERED=1 "C:/Users/nicho/AppData/Local/Python/pythoncore-3.14-64/python.exe" run_pipeline.py --config config/thresholds.json --masks "C:/Users/nicho/minecraft-worldgen/masks/" --schem-index schematic_index.json --output output/ --tile-x0 X --tile-x1 X+1 --tile-z0 Z --tile-z1 Z+1
```

### Copy to test world
```bash
cp output/r.X.Z.mca "C:/Users/nicho/AppData/Roaming/ModrinthApp/profiles/test/saves/Vandirtest10/region/"
```

### Diagnostic .mca biome NBT inspector
```bash
"C:/Users/nicho/AppData/Local/Python/pythoncore-3.14-64/python.exe" tools/diag_mca_biomes.py output/r.X.Z.mca
```

### Diagnostic .mca surface block inspector
```bash
"C:/Users/nicho/AppData/Local/Python/pythoncore-3.14-64/python.exe" tools/diag_mca_surface.py output/r.X.Z.mca
```

## Mood notes

User got terse a few times when I missed obvious things (NOISE_PATTERNS.md doc existed, I was reinventing the formula). Lesson: **always check existing docs first** for noise/dither/random work. CLAUDE.md mentions NOISE_PATTERNS.md explicitly — re-read CLAUDE.md§HARD RULES + NOISE_PATTERNS.md at session start.

User likes the workshop pattern: triage + propose options + get sign-off before implementing. Auto mode lets me skip this for low-risk work but **explicitly ask before any architectural change** (e.g., FF↔AT swap was a strategic decision, not low-risk).

User noted I "got a little stupid" near end of session (high context). Bug: tile_y vs tile_z NameError. Caught and fixed but should've been right first time. Trust user catches → fix fast.

## Files added this session
- `tools/diag_mca_biomes.py` — biome NBT dumper
- `tools/diag_mca_surface.py` — surface block dumper
- `memory/lithology_region_labeled.png` — labeled lithology preview
- `river_script1.7.js` — WorldPainter river script reference (sijmen_v_b)
- `wGmzovc.jpeg` — "Tundra Valley" reference image (FF redesign target)
- `memory/S71_handoff.md` — this file

## Worktree state
- Worktree: `C:\Users\nicho\minecraft-worldgen\.claude\worktrees\naughty-ardinghelli-3259c9`
- Branch: `claude/naughty-ardinghelli-3259c9`
- Status at handoff: 8 modified, 6 untracked, ready to commit + push
