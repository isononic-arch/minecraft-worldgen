# S92 Handoff — checkered river water + treeline lanes + river geometry audit — **REVERTED after walk (3cd7376)**

**POSTMORTEM (read this first).** All S92 *code* (commits `1fa322a` + `b45dd20`) was REVERTED in `3cd7376` after the box-render walk. Code is back to the S91 tip `6b26cd7` state exactly. The S92 box render DID confirm: the S91 high-relief seam fix LANDED (73|66/74|66 border clean — note the user's observed seam near z≈34299/34304 is the border against (73,67)/(74,67), which are STALE in Vandir50k_verify — not re-rendered since pre-S91), and snowy massif (30,10) + lithology-at-elevation (31,21) look great.

**Walk verdicts that triggered the revert (what the local metrics MISSED — every artifact below passed my numeric gates):**
1. Estuary (tidal clamp): water no longer NARROWS naturally — flows awkwardly to an end; at every 1-Y step a row of straggler water is left behind. The flat-pool pin + coherence median preserve the 1D profile but break the channel's VISUAL taper and step cleanliness in ways column-level metrics (incoherent/floating counts) don't capture.
2. River: a "crossguard" — two rows of floating water escaped PAST the channel diameter. Likely the tidal 50k seed-extension or the coherence median's +1 raises interacting with the containment passes.
3. Lake junction (62,61): a straggler lake border crossing into the river — less natural than before (suspect: headwater taper reshaping the channel mask at the junction, or the band/taper interaction with CHAN_LAKE tagging).
4. Forest border (band placement): WORSE than the S91 lane — wide density bands + striped single rows. The band's reduced feature set (no eco modulation, no dupe-rerolls, uniform world-hash texture) reads as a visibly different forest TEXTURE in a 48px stripe, even at matched average density; the strip-sequential exclusion produces row-aligned patterns the interior's permuted iteration doesn't.

**LESSONS for the rework (user: "take a step back, evaluate on a smaller scale; tree seams were 90% fine before today"):**
- Numeric seam metrics (identity %, density bins, canopy medians) are NOT sufficient gates for these systems — the failures were *qualitative* (taper shape, texture stripes, straggler rows). Any rework needs TOP-DOWN VISUAL diffs (rendered-block maps of the artifact zones) as a first-class gate before any box render.
- One change at a time, one tile pair at a time, in-world walk per change. No stacked multi-system sessions.
- The tree-seam problem to actually solve is SMALL: the S91 edge-guard lane (~10px, one-sided). Candidate minimal fixes to evaluate separately: (a) S92's substitution alone (was measured: canopy holds 23-26% to the last 2 cols — never walked in isolation!); (b) relax the guard to allow ≤2-block overhang clipping on md/lg.
- River checkered water: rethink at the SOURCE (the carver's water_y rounding / 7.7b implicit plateaus) rather than Step-9 post-passes stacked on top of containment.
- Keep: tools/diag_water_coherence.py, diag_veg_seam.py (measurement stays useful); the v18 cache concept died with the revert (back to v17; boxes' snapshot v17 remains valid).

---
*Original S92 log below (now historical — code reverted):*


**Branch:** `s85-cherry-picks` on top of S91 `6b26cd7`. Committed end of S92, NOT pushed (push before next box render — boxes will auto-build the v18 bed cache, ~12 min first render).
**FINAL v9 VERDICTS:** border water agreement 20/20 river columns identical across (27,33)|(27,34) (was 18/23 mismatched); (27,34) estuary = one flat Y63 pool (2604 cells pinned), remaining 702 Y64 cells ALL have bed ≥ 63 = the river's legitimate first above-sea reach; floating cells 57 (0.9%, the thin physically-required 63|64 cascade line — was broad checkered rectangles); (62,61) tributary creeks width 4/7/12 tips 4.0, junction/lake intact (raised 84 max +2, wl 95).
**User reports (screenshots 2026-06-10 14:33/14:39):** (1) checkered/raised rectangles of water at river elevation steps, recurring (seen at the (27,34) estuary, world 14218,73,17479); (2) straight bare lanes through forest canopy tracing single x/z lines (the 50|51 border — S91's edge-guard rejection lane). Plus: audit river depth/width realism (no deep trenches, thin headwaters widening downstream).

## #1 Checkered water — THREE-PART FIX (Step 9 + global cache)
**Mechanism:** river `water_y` is per-pixel `round()` of a smooth float field (carver 7.7b "implicit plateaus" — S73 design) + Pass-0.05 nearest-bank cap jitter → adjacent channel cells disagree ±1 → MC renders odd-high cells as floating water patches; AND at sea-level estuaries the profile creeps to 64-65 over a flat pool whose bed is below sea → written 1-block ledges across wide water (the "solid rectangles"). Verified in the installed box .mca: 1936 columns at Y64/65 around the screenshot zone; flat elsewhere.
1. **Pass 0.1 TIDAL CLAMP:** reaches whose bed < SEA and which connect to the ocean get `rwy = SEA` exactly. Membership from a **GLOBAL 8k tidal mask** computed in the bed cache (flood ocean → footprint cells with bed_8k < 63). Per-tile connectivity was tried first and FAILED the pair test — (27,33) has no ocean in its padded window and the pad ring carries PRE-CARVE heights (bed<SEA never true in the ring) → 18/23 border columns disagreed 63|64. Global oracle = same verdict both sides by construction.
2. **Pass 0.15 COHERENCE:** river-masked 3×3 median (2 iters, nearest-river fill so banks don't vote, raises clamped to +1) — kills the ±1 checker, straightens step contours into clean 1-row tiers. Runs AFTER tidal so the pin contour gets cleaned too.
3. (27,34) v2 verdict: 4183 cells pinned; rwy histogram 63:5454/64:1818/65:73 → 63:7285/64:50/65:17; incoherent 1→0.

## #2 Treeline lanes — SUBSTITUTION fix
S91's edge-guard *rejected* placements whose actual extent crossed the tile bound → bare ~10px trunk+canopy lane along every border (screenshot). Now: re-roll among the already-filtered entries whose extent FITS (`min(W-col, H-row)`), weighted; radius recomputed for the substitute. Trees march to the border with progressively smaller schematics; canopy overhangs the rest. Veg-pair AFTER: tree bin [-8,0) recovered 2→12 (far-field ~23), crossings still 0. Bushes (all extent 8) still gap the last ~7px — invisible under canopy. Block-space canopy check on a fresh r.50.50.mca: IN FLIGHT.

## #3 River geometry audit + HEADWATER TAPER
**Audit findings (rendered, 1 block = 1m):** water depth p50 1-4, max 5 anywhere measured ((27,34)/(62,61)/(19,76)); bank-above-bed relief ≤7 → **no deep trenches**. Widths: mouth ~35-45, inland p50 21-29, **minimum 9-11 — no thin headwaters anywhere**: 1 paint px ≈ 6 blocks at 50k = a hard width floor. Strahler masks (hydro_order/width/depth) are DEAD (only braid markers — rivers are fully paint-driven). `_FLOW_WIDTH_SCALE` (S81 Hack's-law additive widening) is a dead constant — never wired.
**Fix: S92 headwater taper** — `factor = clip((accum/_HW_REF_PX)^0.4, 0.18, 1)` from the existing skeleton flow accumulation, EDT-propagated. **Calibration history (3 iterations, each measured):**
- v4: taper applied only to the 8k bed → ZERO width effect — the 50k per-tile carve (`carve_depth_50k`, the footprint source) is computed SEPARATELY from the 8k bed. Lesson: the bed cache shapes depth, the 50k carve shapes width; both need the factor.
- v5: full factor on both at REF=300 → (62,61)'s whole short-tributary network (accum ~30; world accum p50=30!) collapsed to 1-deep ditches, width p50 21→7. REF was mis-set vs the actual accumulation distribution.
- v5b/v7: attempts to decouple width (full factor) from depth (sqrt) FLIP-FLOPPED — v7's widths bounced back to near-untapered while v6's simple multiplication had delivered exactly the asked-for thin creeks. Root insight: **river_meta's width does NOT derive from carve_depth alone** (spline outline polygons consume only the paint mask; width_8k feeds yet another path) — the carver's width derivation needs its own mapped-out session before any width/depth decoupling. STOP-rule applied; **SHIPPED FORM = v6 simple multiplication** (8k bed × t, 50k carve × t): (62,61) tributaries (accum~30) = width p10/50/90 4/7/12, depth 1 — thin shallow creeks per the user's ask; mid rivers (accum 100-200) grade up; trunks (≥200) untouched.
- **FOLLOW-UP (own session): carver width-map deep-dive** if mid-tributaries (1-2 deep) should be deeper — requires tracing river_full_mask/river_meta through splines + width_8k + braid fill.
- Global-tidal holes closed across v6-v8: (a) per-tile flooding → global 8k oracle (border 18/23 mismatched → 20-23/23 PERFECT agreement); (b) flood field must be **PAINT-based, not carve-footprint-based** — the S84 coast factor zeroes the 8k carve exactly at mouths, so the footprint is empty where the estuary is (v6/v7: 0 pinned cells at (27,34)); condition now `(paint | footprint) & (bed<63 | terrain<63)`.
Knobs: `_HW_*` module constants in core/hydro_region_overlay.py. v8 chain (ship candidate) VALIDATING.
**Bed cache bumped v17 → v18** (carries the new `tidal_8k` field + taper) — forces clean regen locally AND on boxes (kills the stale-snapshot-cache hazard: render_verify.sh never cleared the box's untracked v17 from the S86 snapshot; `git reset --hard` does not remove untracked files).

## VALIDATION STATE
- **#2 LANE: VALIDATED in block space.** Canopy% across the last 32 columns at the 50|51 border: S91-reject render collapses 35→16→7→1→0 (the bare corridor); S92-substitution holds 23-26% to the last 2 cols (14/7%) — no corridor, soft edge meeting the neighbor's full canopy. Veg-pair metrics: tree bin [-8,0) 2→12, crossings 0.
- **junction (62,61) v4/v5: tidal+coherence+cap all healthy** (raises 69-84 max +2, lake wl 95, cascade intact across all iterations).
- v5b chained validation IN FLIGHT: cache rebuild → (62,61) headwater calibration verdict → (27,34)+(27,33) border-agreement + estuary-flat verdict.
- NO commits yet. No boxes.
