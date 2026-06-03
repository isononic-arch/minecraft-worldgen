# S89 Full Validation Sweep — Tile List

Generated 2026-06-03 by `tools/diag_biome_sampler.py` (land-aware). Walk each TP
in-world, mark the checklist in `BIOME_VALIDATOR_CHECKLIST.md`. All TP-Y are
derived from real terrain + headroom (you land above ground, never in ocean).

**Render batches:** group by region for cache locality, or just render the whole
list. Flags are committed ON, so plain `run_pipeline.py` produces the S89 stack.

---

## A. Biomes — 26 clean land tiles (vegetation / ground-cover / palette / schematics)

| Biome | Tile | Land% | TP |
|---|---|---|---|
| COASTAL_HEATH | (37,8) | 100 | `/tp @s 19196 121 4348` |
| TEMPERATE_RAINFOREST | (23,29) | 100 | `/tp @s 12028 111 15100` |
| BOREAL_TAIGA | (64,54) | 100 | `/tp @s 33020 219 27900` |
| SNOWY_BOREAL_TAIGA | (30,10) | 100 | `/tp @s 15612 525 5372` |
| BOREAL_ALPINE | (21,23) | 100 | `/tp @s 11004 147 12028` |
| ARCTIC_TUNDRA | (32,13) | 100 | `/tp @s 16636 670 6908` |
| FROZEN_FLATS | (33,6) | 93 | `/tp @s 17132 132 3308` |
| TEMPERATE_DECIDUOUS | (32,31) | 100 | `/tp @s 16636 120 16124` |
| RAINFOREST_COAST | (8,67) | 100 | `/tp @s 4348 123 34556` |
| RIPARIAN_WOODLAND | (80,50) | 49 | `/tp @s 41316 113 25836` |
| DRY_OAK_SAVANNA | (29,76) | 99 | `/tp @s 15100 133 39172` |
| KARST_BARRENS | (34,9) | 100 | `/tp @s 17660 362 4860` |
| BIRCH_FOREST | (60,41) | 100 | `/tp @s 30972 116 21244` |
| EASTERN_TEMPERATE_COAST | (28,35) | 41 | `/tp @s 14588 106 18180` |
| MIXED_FOREST | (50,50) | 100 | `/tp @s 25852 112 25852` |
| CONTINENTAL_STEPPE | (39,23) | 100 | `/tp @s 20220 149 12028` |
| DRY_PINE_BARRENS | (30,49) | 100 | `/tp @s 15612 141 25340` |
| SCRUBBY_HEATHLAND | (85,79) | 100 | `/tp @s 43772 123 40700` |
| LUSH_RAINFOREST_COAST | (6,68) | 100 | `/tp @s 3324 110 35068` |
| SAND_DUNE_DESERT | (18,66) | 100 | `/tp @s 9468 154 34044` |
| DESERT_STEPPE_TRANSITION | (19,63) | 100 | `/tp @s 9980 137 32508` |
| SEMI_ARID_SHRUBLAND | (27,65) | 100 | `/tp @s 14076 111 33532` |
| DRY_WOODLAND_MAQUIS | (30,90) | 100 | `/tp @s 15612 113 46332` |
| TIDAL_JUNGLE_FRINGE | (31,89) | 82 | `/tp @s 16116 131 45868` |
| MANGROVE_COAST | (30,86) | 30 | `/tp @s 15692 109 44372` |
| FRESHWATER_FEN | (8,73) | 52 | `/tp @s 4356 122 37644` |

Coastal/transitional biomes (MANGROVE/ETC/RIPARIAN/FEN) share their tile with
ocean — that's the purest land tile available; TP still lands on biome pixels.

## B. Mountain / rock — 6 tiles, one per lithology group (rock palette + snow + relief + krummholz)

Keyed off the painted `lithology.tif`. Highest-altitude exposed-rock tile per group.

| Litho group (biome at peak) | Tile | Peak surfY | TP |
|---|---|---|---|
| deepslate_metamorphic (ARCTIC_TUNDRA) | (31,21) | 694 | `/tp @s 16228 734 11068` |
| granitic (ARCTIC_TUNDRA) | (74,66) | 677 | `/tp @s 38028 717 33940` |
| limestone (ARCTIC_TUNDRA) | (33,18) | 672 | `/tp @s 17052 712 9436` |
| arid_basaltic (ARCTIC_TUNDRA) | (29,20) | 652 | `/tp @s 15108 692 10700` |
| temperate_basaltic (ARCTIC_TUNDRA) | (29,12) | 643 | `/tp @s 15276 683 6580` |
| mossy_temperate (BOREAL_TAIGA) | (72,68) | 321 | `/tp @s 37284 361 35012` |

## C. Snow-system reference (where snow actually manifests)

`SNOWY_BOREAL_TAIGA` deep-snow / carpet at altitude (reaches Y582):
- `(74,65)` — `/tp @s 38000 622 33500` (SBT snow_carpet + gully on a high SBT massif)

ARCTIC_TUNDRA high peak (reaches Y699) — covered by the deepslate rock tile (31,21).

---

## TUNING IDEAS

- **✅ IMPLEMENTED 2026-06-03 — Edge-localized "noisy stroke" at the snow/rock border (user).**
  Lives in `core/surface_decorator.py` right after the patchy-band cut
  (`snow_px &= (_t + 0.5*_micro) > 0.5`): captures the pre-band gap==7 candidate
  pool, finds the post-band snow boundary via EDT, and in a ring `edge_stroke_blocks`
  wide flips pixels with prob `edge_stroke_amp` (fading to 0 at ring edge) —
  biting rock flecks INTO snow, growing snow flecks OUT onto candidate cells.
  Config: `snow_lines.edge_stroke_blocks=2.0`, `edge_stroke_amp=0.45` (ON). Set
  amp=0 to disable. NOT in the first sweep render (predates it) — shows next render.
  Original idea text below.

- **Edge-localized "noisy stroke" at the snow/rock border (2026-06-03, user).**
  The jagged look the user likes (ref: Building-101 mountain screenshot) reads
  most strongly *right at the snow/bare-rock boundary*. Idea: on TOP of the
  existing wide stochastic transition band (`transition_blocks=25`), add a narrow
  **2-3 block-wide stroke of INTENSIFIED salt-and-pepper dither hugging the snow
  edge itself** — i.e. detect the snow-presence boundary, dilate ±2-3 blocks, and
  crank the per-pixel snow/rock coin probability inside that thin ring so the very
  fade-line is maximally ragged. Distinct from `transition_blocks` (which controls
  the *width* of the gradual band); this is an edge-following high-frequency
  accent. Likely lives near the patchy-band survival calc in
  `core/surface_decorator.py` (snow assignment) — compute snow_edge via
  binary_dilation(snow) XOR binary_erosion(snow) or EDT, then boost coin amp in
  that ring. Pending in-world review of current edge first.

## STRAGGLERS — investigated and RULED OUT (don't waste render time)

- **Dry/warm-biome high snowcaps (the handoff's "desert @760") = NON-EVENT.**
  No dry/warm biome ever reaches its snow line. Measured peak vs line:
  KARST 461/665, SEMI_ARID 372/705, DRY_PINE 416/655, SAND_DUNE_DESERT 160/760,
  CONTINENTAL_STEPPE 172/675, etc. The high "peaks only" lines fully disable snow
  on these biomes (intended). Nothing to render.
- **Mid vegetated biomes never snow either.** BOREAL_ALPINE (maxY 171 vs line 540),
  BOREAL_TAIGA (348/600), MIXED_FOREST (426/635), all temperate (≤170 vs 625-650):
  none reach their snow line. ⚠ **Flag for review:** if BOREAL_ALPINE is *supposed*
  to show snow, its line (540) is far above its actual max altitude (171) — either
  the line is mis-set or the painted BA regions sit lower than intended. Decide
  before the 50k regen. (Snow currently appears ONLY on the 2 snowy biomes + high
  tundra peaks, i.e. tiles already in sections B + C.)
