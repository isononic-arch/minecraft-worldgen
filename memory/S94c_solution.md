# S94c — RIVER WATER: SEAM + REALISM SOLVED (read this)

The seam saga's resolution. Branch `s85-cherry-picks`. Supersedes the
global-override + spill-cleanup approach in [[S94_river_seam_handoff]] (that
over-leveled = "water touching air"; the bank-taper left "steep banks dry").

## ROOT CAUSE (3 subagent audit + instrumentation)
1. **Seam** = the flood-settle's monotone ordered cross-sections by a PER-TILE
   distance-from-ocean EDT; when the ocean is past the 48px halo it falls back to
   the lowest in-window cell, so the ordering (and level) diverged ~1 block across
   seams on flowing rivers (52,53|52,54: 387 +/-1 water-step columns).
2. **"Water touching air"** = the global-override bake over-leveled (measured the
   bank 30px out on the valley wall, real channel ~2px). A fixed bank distance
   can't fit both narrow and wide channels.
3. **"Steep banks dry"** = bank-taper lowers the valley floor below the channel
   waterline but the water mask is the narrow carved channel -> tapered bench is
   dry below the water.

## THE FIX (all committed; override RETIRED)
1. **Global ocean-distance** — `rebuild_ocean_dist.py` bakes dist-from-ocean at
   1:8 (`masks/hydro_ocean_dist8.tif`, 28MB, fast, LOCAL — EDT OOMs at 50k).
   `run_pipeline.py` reads the padded window, upscales, feeds it to `settle()` as
   the ordering. Seam-consistent by construction. **52,53 seam 387->5, 12,80|13,80
   0.** Settle is now seam-clean AND contained (it measures the real channel-edge
   bank), so the over-leveling global override is gated OFF (opt-in
   `RIVER_WL_OVERRIDE=1`).
2. **Fill-to-banks** — `core/water_fill.py:fill_to_banks`, called in run_pipeline
   after bank-taper. Floods water OUTWARD to every dry cell below an adjacent water
   level, bounded at the bank. NO terrain raised (no walls). Unit-tested. Protected
   from the post-crop re-locks (river_water_y>63 added to their water zones).
3. **Inland-drain guard** — the global ocean-distance runs OPPOSITE to flow for the
   rare headwater that drains AWAY from the nearest ocean (35,21), draining it.
   run_pipeline computes BOTH settles (global + per-tile) and restores the per-tile
   level on cells the global drained. Big ocean-draining rivers keep the global
   (seam-clean) level.

## VALIDATED (all LOCAL renders; 1 box run = 11min/EUR0.10 for the now-unused
## scale-1 river_wl bake)
| tile | perch before | perch after | water | note |
|---|---|---|---|---|
| 52,53 | 0 | 0 | 89193 | full wide, seam 387->5 |
| 52,54 | 0 | 0 | 14569 | full |
| 12,80 | 17 | 12 (0.04%) | 29549 | seam 0 vs 13,80 |
| 13,80 | 131 | 82 (1.03%) | 7948 | wide wash, contained |
| 16,76 | 0.42% | 0.37% | 103902 | lake |
| 20,63 | 0.45% | 0.22% | 34928 | delta |
| 89,58 | 0.20% | 0.08% | 51177 | big high-alt, FULL |
| 69,62 | 0.25% | 0.02% | 43113 | big high-alt, FULL |
| 35,21 | 20.8% | (drain-guard) | — | tiny inland headwater |

## RESIDUALS (minor, shippable)
- 52,53 seam = 5 (not 0) from 1:8 dist coarseness; finer dist would close it.
- 13,80 perch 82 (1%) — flat-wash bank is ill-defined; flood filled 7168.
- dry-below-median metric still 29-55 on descending rivers (16,76/20,63) — that
  metric is noisy for rivers that descend across a cross-section (median != local
  level); the PERCH metric (real "water touching air") is 0.2-0.4%.

## FOR THE 50k REGEN (box)
- Box must run `python rebuild_ocean_dist.py` (fast) BEFORE run_pipeline. The
  scale-1 `rebuild_river_wl.py` is NO LONGER needed (override retired) unless
  RIVER_WL_OVERRIDE is set. Update cloud_bake job accordingly.
- `masks/hydro_ocean_dist8.tif` is NOT in git (mask) — bake on the box.
- Commits: ocean-dist + override-off = `d029b42`; flood/cap/bake = `af51c7c`;
  drain-guard = (this session, pending).
