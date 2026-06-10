# S90 Handoff — Lake carve, coastal lift, beaches + NEW regressions to fix

**Branch:** `s85-cherry-picks`  **tip:** `52d5a77` (all work below committed + pushed).
**Test world:** `D:\modrinth_vandir\saves\Vandir50k_verify\region` (install target; D: is a portable SSD — unplugged drops the `/d` bash mount, use PowerShell for D: ops).
**Boxes:** ALL DEAD (0 servers, billing NONE). Hetzner API token supplied by user in chat. User kills manually or via an armed background auto-killer.

---

## DONE & PUSHED (validated locally + walked in-world, "looking good")
1. **73|74 height seam — FIXED** (`beba297`). Root cause: snow `drift_fill` used a per-tile `gaussian_filter(mode='reflect')` → systematic ~3-block step. Disabled (`snow_lines.drift_fill_blocks=0`); the halo-aware concavity path kept dormant. Vertical seam 369/512 → **0/512** (local dry-run, both axes).
2. **Organic lake shoreline** (`bdd9dfa`). De-staircase the 1:8 NEAREST outline: smooth a SIGNED distance field (a distance field, NOT the lake-ID mask → NEAREST hard rule intact) + 2-octave world-coord domain warp (seam-safe) + terrain-follow. **Flat scalar water level** (median over basin) — the per-cell NEAREST water mask was re-injecting the staircase after smoothing (the key bug). Balmorhea → Nasworthy, validated on 33,33/81,63/19,76.
3. **Lake depth = global EDT** (`b27c82b`). Distance-to-shore computed at a LARGE pad (reaches centre of lakes wider than a tile → seam-safe), linear-to-cap depth: gradual to centre (no flat bottom-out), scales with size (~35 med / ~50 huge vs old flat 18). `hydro_lkdep` was flat/unusable. Knobs: `hydrology_engine.river_geometry.lake_carve.{depth_per_px=0.15, depth_cap=70, depth_curve_pow=0.85, depth_edt_pad_px=640, shore_warp_amp_px=14, shore_warp_scale_px=50, shore_smooth_sigma=6, terrain_follow=1}`.
4. **Coastal land lift off Y63** (`b27c82b`). `terrain_spline.mc_y_out[4]` 64→**67** so near-sea-level land banks up off Y63 instead of a flat sea-level plain (was "all of Y63 = lakeshore dirt" + sea-level lakes colliding with ocean code). Sea-level-lake land now Y65-66, 0 sand blanket. **Ocean beaches confirmed PRESERVED** by this lift (in-render: 1.5k–2.5k ocean-adjacent sand cells across COASTAL_HEATH/E_TEMP_COAST).
5. **Infra** (`0a04b42`,`52d5a77`): `cloud_bake/render_monitor.sh` (live render-failure monitor); `render_verify.sh` `SKIP_BUILD=1` fast-path (reuse warm-box masks, skip ~6min rebuild).

**Cleanup pending:** old lake `_bowl`/terrain-intersection branches kept dormant behind flags (`LAKE_V2_OFF`) as a one-render fallback — DELETE after in-world confirmation (the "simpler" goal).

---

## OPEN REGRESSIONS — flagged this session, FIX before the full 50k render
1. **Underwater tile-boundary seams at underwater coastlines (NEW).** Suspect: ocean-depth correction in `core/column_generator.py` (~line 1032, `distance_transform_edt(~land_mask)` per-tile, applies only below sea level) — NOT halo-aware → underwater seam. The coastal spline lift likely widened the near-sea band, exposing it. FIX: route that EDT through the padded halo (same pattern as the surface_y seam fixes — `core_tile_stream.read_tile` with pad).
2. **World-wide SURFACE-GENERATOR seams (NEW, NOT terrain): trees / schematics / vegetation coverage seam at tile boundaries.** ASSESS first. Likely a per-tile (non-world-coord) RNG seed or per-tile neighborhood field in `core/schematic_placement.py` (placement coin) and/or `core/surface_decorator.py` ground-cover density / ecotone. Same antipattern already fixed for rock (S86) + snow edge-stroke (use splitmix64 on WORLD coords, not `default_rng(tile_x^tile_y)`). The global spline lift shifted surface_y everywhere → may have shifted treelines/density and exposed a latent per-tile seam. **Reproduce across a tile boundary via SURF_DUMP (sblk) + a vegetation/schematic diff; find the per-tile seed/field.**
3. **Beaches propagate out too far.** Beach band too wide — `eco_gradients` beach `_dither_width = _core_width * 3.0` (~line 859) and/or the coastal lift lets beach reach further inland. FIX: tighten width / re-gate.
4. **Beaches should apply at Y64 OR Y65** (currently effectively one level). With coastal land now Y65-66, allow beach at 64–65. FIX: beach Y-eligibility in `eco_gradients` beach block (~line 771-916) / `rebuild_beach.py`.

---

## NEXT STEPS (ordered)
1. **ASSESS #2 (surface-gen seams)** — biggest unknown, highest priority. Dry-run two adjacent tiles (SURF_DUMP), diff vegetation/schematic/ground-cover across the seam; locate the per-tile randomness. Convert to world-coord splitmix64 / halo.
2. **FIX #1 (underwater seam)** — halo the ocean-depth EDT in `column_generator`.
3. **FIX #3 (beach width) + #4 (beach Y64/65).**
4. **RE-VALIDATE locally** (dry-run + `diag_lake_shape.py` / a vegetation-seam diff) on a coastal + beach + vegetation tile boundary.
5. **RE-RENDER verify set** on boxes (SKIP_BUILD for iteration): lakes + beaches + a vegetation-seam boundary + an underwater coastline; walk.
6. **FULL 50k render** — gated on all above clean. Then delete the dormant old-carve branches.

---

## CURRENT VALIDATION TPs (installed in Vandir50k_verify; reload chunks first)
**Lakes (deep gradual bowl + organic shore + coastal lift, all NEW-depth now):**
- 38,35 (sea-level lake): `/tp @s 19712 90 18176`
- 33,33: `/tp @s 17150 90 17360`   · 19,76: `/tp @s 9984 110 39168`
- 20,75: `/tp @s 10496 110 38656`  · 62,61 (lake+river): `/tp @s 32000 100 31488`  · 36,33: `/tp @s 18688 100 17152`

**Beaches (ocean beaches preserved by spline lift):**
- COASTAL_HEATH N: `/tp @s 18180 80 3377`  · E_TEMP_COAST: `/tp @s 14075 80 17596`  · COASTAL_HEATH 46,51: `/tp @s 23828 80 26360`

---

## INFRA NOTES
- **Collect to `/c`** (e.g. `/c/Users/nicho/<name>_out`), NOT `/d` — the Bash tool's `/d` mount is unreliable (and the SSD can unplug). Install to D: via **PowerShell** `Copy-Item ... D:\modrinth_vandir\saves\Vandir50k_verify\region`.
- **render_verify.sh:** `TILES=<list> OUT_DIR=/c/... THREADS=40 OMP=1 [SKIP_BUILD=1] bash cloud_bake/render_verify.sh <IPs>`. Boxes `git reset --hard origin/s85-cherry-picks` → **push before rendering.** ccx63 / fsn1 / snapshot 390525743 / ssh_key 112518810. Arm a background auto-killer (`sleep <secs>; curl -X DELETE …`) after ssh-ready.
- **Diag tools:** `diag_lake_shape.py` (plan-view + cross-section + shore-step/fill gate, reads SURF_DUMP); `diag_seam_local.py` (dry-run surface_y dump for seam bisect, no OOM); `diag_hillshade.py` (convexity-correct, self-tested); `diag_satellite.py` (**known bug: Y VALUES off by −64** — `syb+ly+Y_MIN` double-subtracts; hillshade/colours fine, Y readouts wrong; fix or account for it).
- **SURF_DUMP_DIR** env → `run_pipeline._process_tile` dumps `sy_pre/sy_post/snow/rmeta/sblk/halo` then early-returns (dry_run, no chunk_writer → no OOM on the 7.5GB box). The gate for surface-gen seam #2.
