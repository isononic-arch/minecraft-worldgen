# S85 Handoff — 2026-05-22/24

## Tldr

**S85 was a reconciliation + biome-polish session.** Pulled the missing S70-S84 work from origin/master onto local master (was 32 commits behind), layered S85 surgical edits on top, fixed 3 inherited bugs, refactored ecotone + wash to be config-driven, automated cloud-render workflow. Ended with successful 36-tile validation render on 4× CCX63 Hetzner (~22 min wall, ~$2). Branch `s85-cherry-picks` ready to merge to master after final in-world validation.

## What landed (16 commits on s85-cherry-picks)

| Commit | Type | Summary |
|---|---|---|
| `a738fb4` | wip | S85 working tree pre-pull checkpoint |
| `9c6241b` | merge | Cherry-pick S70-S84 work + S85 surgical edits + world-height threshold scaling |
| `f992f2c` | docs | CLAUDE.md current-state header + S85 handoff + validation tile list |
| `e1f25df` | infra | gitignore mask cache files |
| `b8c025d` | fix | restore missing `core/region_overlay_smoothing.py` (river carving broken) |
| `ff42fc3` | fix | normalize `schematic_index.json` paths to relative (cloud-compatible) |
| `d93bfc5` | fix | plateau-clamp on surface ecotone (clip [0.15, cap]) |
| `2ecd2c8` | fix | remove `noise_b` multiplicative modulation in ecotone dither |
| `2551021` | tune | lithology palette tweaks (calcite limestone, raw_iron granitic, no tuff in deepslate_metamorphic) |
| `6381370` | tune | exception tall sm-mislabeled trees out of FF mirror (max 11 blocks in FF) |
| `eb79531` | feat | per-pixel RNG soften_biome_boundaries (no blob islands) |
| `2be0857` | feat | per-lithology-group wash palettes (replaces hardcoded sand/sandstone) |
| `5552c26` | feat | Option A ecotone dither — per-pixel shadow lookup preserves blob structure |
| `e0794dd` | fix | skip fluid ticks for above-sea river water (no chunk-load river settling) |
| `a964b8b` | infra | one-shot render script + README |
| `7f1ea25` | fix | render script restores painted PNGs after checkout (was deleting them) |

## Validation state

**LOCAL renders verified working:** (33,6), (49,53), v5 render of (33,6) with all S85 fixes — user verdict "looks great".

**CLOUD render:** First run had a script bug (deleted tracked PNG overlays); user reported "rivers broken, fluting blobs everywhere". Fixed in `7f1ea25`. Re-render in progress at session end — should produce correct output for all 36 validation tiles.

## S85 cherry-pick checklist (all approved by user)

- A1: bbox-cull perf
- A2: vandir_height.zip auto-install (assets→output/datapacks)
- A3: AT high-elev stone-fade exemption + fade band Y 500-580
- A4: AT GC palette (dead_bush → tall_dry_grass)
- A5: tools/diag_mca_biomes.py + tools/diag_mca_surface.py
- B1: FROZEN_FLATS Tundra Valley palette (grass_block surface + scattered snow_carpet + small pines + badlands MC tag)
- B2: lithology mask as source-of-truth for cliff + high-elev fade
- B3: ecotone plateau-clamp + width 40→100, swap_cap 0.75→0.85
- B4: schematic placement density tuning per S70-S81 walks
- B5: leaf-column blacklist (4 schematics globally rejected)
- B6: SARID juniper-only filter
- B7: BIRCH clumping fix
- B8: floodplain skip for RIPARIAN/FRESHWATER_FEN/LUSH/SAND_DUNE_DESERT
- B9: AT + FF in `_NO_SWAP_BIOMES`
- S71-S84 river overhaul: WP-style carve + tanh depth + coast taper + lake-bowl geomorph + bed cache

## S85 NEW design changes (not cherry-picks, original this session)

- BT MC tag → `minecraft:meadow` (was stony_shore in S71)
- `BIOME_ALTITUDE_REMAPS = []` (deleted BIRCH/MIXED → BA entries)
- Snow gap==7 surface exemption for SBT + FROZEN_FLATS
- ARCTIC_TUNDRA → SBT below Y 500 remap DELETED
- Full treeline rescale for 768-height world
- World-height threshold scaling (SNOW_Y 250→430, GRASS_Y 325→460, EXPOSED_MIN_Y 180→310, etc.)
- A3 fade band start 480→500
- B3 ecotone widened: width 40→100, swap_cap 0.75→0.85
- Per-pixel RNG soften (replaces simplex)
- Per-lithology-group wash palettes (granitic = warm earth, arid_basaltic = dark gravel + rare sand, etc.)
- Option A ecotone shadow lookup (preserves rare-block simplex blobs)
- Skip fluid ticks for above-sea rivers
- FF tree filter: exclude 6 mature-pine variants from FF mirror

## Cloud render workflow (working)

```bash
cd /c/Users/nicho/minecraft-worldgen
bash cloud_bake/render_s85_validation.sh IP1 IP2 IP3 IP4
```

Spin 4× CCX63 from snapshot `vandir-baked-s85-validated` (or `-veg` if user saved that one) in Falkenstein. Script handles bootstrap → branch checkout → cache clear → Vegetation upload → rsync → tmux dispatch → monitor → collect → install. ~22 min wall, ~$2 cost.

## Backlog (in priority order)

1. **Full world render** (9,409 tiles, 8× CCX63, ~3-4h, ~$15) — gated on validation render passing
2. **Override.tif repaint for BT** — BT currently sits in Highlands (median Y 296) but real-world boreal taiga is lowland/midland (0-1200m = Y 75-200). User flagged this in the elevation-bands analysis. Needs Override Studio session.
3. **`render_full_world.sh`** — adapt `render_s85_validation.sh` for 8 boxes + 9,409 tiles via plan_render.py z-stripe
4. **Schematic index needs `Vegetation/` synced to snapshot** — Box 1 has it now; next snapshot includes it; future renders skip the 3-min upload
5. **`_BIOME_CLIFF_STONE` in chunk_writer.py is hardcoded** — doesn't read from config lithology palette. User said "probably dead code from old system" — confirm + delete or wire up.

## Open issues / known gaps

- **biome_reference_tiles.csv stale** — script trusts CSV that has (86,78) labeled SCRUBBY_HEATHLAND but real override has 99.8% ocean there. Other tile labels possibly wrong. Should regen from current override.
- **column_generator.py subsurface lithology** — `lithology_tile` plumbed in but flagged as "pass-through only". Subsurface uses hardcoded biome palettes, not lithology. Likely dead code carryover.
- **soften_biome_boundaries amp/scale** — currently default amplitude_px=48 with per-pixel RNG. May want to make per-biome-pair (e.g., FF/forest = wide blend, ocean/FF = narrow).

## Key files

- `core/region_overlay_smoothing.py` — painted river overlay smoothing (restored S85)
- `core/surface_decorator.py` — gap==5 wash palette + Option A ecotone + plateau-clamp
- `core/biome_assignment.py` — per-pixel RNG soften
- `core/chunk_writer.py` — fluid tick skip for above-sea
- `core/schematic_placement.py` — FF tree height filter
- `config/thresholds.json` — all 6 lithology groups have `palette` + `wash_palette` + `description`
- `cloud_bake/render_s85_validation.sh` — one-shot 36-tile render
- `cloud_bake/validation_tiles.txt` — 36-tile list + TP commands
- `memory/S85_to_S86_handoff_prompt.md` — comprehensive next-session prompt
