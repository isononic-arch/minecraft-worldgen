# Full World Bake Session — NEEDS USER NOTES

**Status:** STUB. Has gaps the AI does not have in compacted context. User to fill in tomorrow when the SSD with bake artifacts is plugged in.

This document captures what we know about the first full-world cloud bake (executed between S83 and S84). Some sections are confirmed; others are placeholder for user to complete.

---

## What we know (from conversation + cloud_bake/SETUP.md)

- **Hardware:** 8× Hetzner CCX63 (48 vCPU / 192 GB RAM / ~$0.57/hr each)
- **Total wall time:** ~8 hours
- **Total cost:** ~$15
- **Workers per box:** 48 (one per vCPU)
- **Partitioning:** z-stripe via `cloud_bake/plan_render.py --boxes 8 --ips IP1 ... IP8`
- **World version baked:** S83 / 512-height (Y -64 to Y 447, 32 sections per chunk)
- **Code source:** master branch of `github.com/isononic-arch/minecraft-worldgen`
- **Snapshot taken FROM staging box** with masks/ + spline cache + S83 code installed → spun N=8 worker boxes from that snapshot
- **Result:** All 9,409 tiles rendered. Collected via `cloud_bake/collect_outputs.sh`.
- **In-world testing:** Some "Failed to save chunk" errors in MC log during fly-through (sporadic, low frequency). Pipeline-side rendering was clean.

---

## Sections needing user notes

### A. Endless-ocean datapack (CRITICAL FOR NEXT BAKE)

User mentioned: *"the datapack also had endless ocean generated outside of the precreated map, so that immersion would be consistent if a player were to fly beyond our bounds."*

**Unknown:**
- [ ] Datapack name (folder or zip name)
- [ ] Where it lives — separate from `vandir_height.zip`?
- [ ] Mechanism: custom `worldgen/noise_settings/<name>.json`? Custom dimension type? Custom biome override beyond a coord boundary?
- [ ] Trigger boundary: world edge (> 25000 abs?) or replace generator outside loaded MCAs entirely?
- [ ] Other functionality: spawn settings, gamerules, function tags, advancements?
- [ ] **Where is the source** — only on the unplugged SSD, or also somewhere else (Modrinth profile / git / gist)?

**User action item:** drop the datapack zip into this worktree once SSD is plugged in. AI will inspect and document.

### B. Snapshot creation specifics

- [ ] Staging box: which Hetzner image was used? Default Ubuntu?
- [ ] Mask upload time: how long did the ~10 GB masks/ rsync take?
- [ ] Any extra files baked into the snapshot beyond masks + code? (additional cache pickles, etc.)
- [ ] Snapshot name used
- [ ] Region/location of snapshot (Ashburn? Falkenstein?)

### C. IPs / region selection

- [ ] Which Hetzner region did the 8 boxes run in?
- [ ] Any IP whitelisting or networking setup beyond default public IPv4?
- [ ] Were there any rate limit / spin-up issues with Hetzner?

### D. Issues encountered

- [ ] Any boxes that OOMed or stalled?
- [ ] Any tile-rendering errors that required re-running?
- [ ] Did `plan_render.py`'s z-stripe partition produce balanced wall times across the 8 boxes, or was one box much slower (more painted-river density on its stripe)?

### E. Collection / final assembly

- [ ] How long did `collect_outputs.sh` take to rsync ~50 GB back to laptop?
- [ ] Where do the final MCAs live now? (Vandirtest10? A backup folder? On the SSD?)
- [ ] Was there any post-processing (recompression, MCA repair, etc.) between collection and shipping to MC?

### F. In-world "Failed to save chunk" investigation

- [ ] How many distinct chunks showed the error?
- [ ] Did it cluster geographically (e.g., near painted-river tiles, near tile boundaries) or random?
- [ ] Reproducible across multiple world loads, or transient?

---

## For S84 re-bake — what's different

**Pipeline-level changes since the last bake:**
- World height bumped 512 → 768 (Y -64 to Y 703, 48 sections per chunk). **Requires updated `vandir_height.zip`** at height=768. Already in `assets/vandir_height.zip`.
- Painted-river carver fixes (paint-always-carves, PAD LUT, tanh depth saturation, coast taper). User-visible improvements at coastal tiles.
- Lithology palette repaint, vegetation polish, schematic placement tweaks (palm coast gate, etc.).
- Skip-empty-sections optimization in chunk_writer (~5-15% wall savings expected).

**Snapshot-level changes needed:**
- Snapshot is at master/S69 from last bake. S84 lives in `sweet-margulis-6fbeed` (24+ commits ahead).
- Either merge `sweet-margulis-6fbeed` → master first (preferred) OR modify `cloud_bake/bootstrap_master.sh` to checkout that branch.
- Re-run smoke test on (51,53) on staging box to confirm S84 produces "it's perfect" result before snapshotting.
- Re-snapshot, name something like `vandir-baked-s84-{date}`.

**Wall-time expectations:**
- S83 / 512-height: ~8 hours on 8× CCX63 = €11.50 / ~$15
- S84 / 768-height + skip-empty: estimated ~9-10 hours on 8× CCX63 = ~$17
- (The 768-height bump adds ~25-30% chunk_writer cost; skip-empty claws back ~5-15%; net ~20% slower than S83.)
- Could go to 16 boxes if user wants ~5-hour wall time at ~$20.

---

## Next-bake pre-flight checklist

- [ ] S84 merged to master (or bootstrap_master.sh updated)
- [ ] Endless-ocean datapack confirmed present in build artifacts
- [ ] `assets/vandir_height.zip` is the 768-height version
- [ ] Smoke test (51,53) passes on staging
- [ ] Smoke test (49,53) passes on staging (S84 coastal river fix verification)
- [ ] `masks/_bed_cache_v17.pkl` and `masks/_spline_cache.pkl` regenerated with S84 changes (will auto-rebuild on first render if not)
- [ ] Snapshot created with name like `vandir-baked-s84-{YYYYMMDD}`
- [ ] Old snapshot deleted (or kept as fallback, but storage costs accrue)
- [ ] 8 boxes spun from new snapshot
- [ ] `plan_render.py --boxes 8 --ips IP1 ... IP8` generates commands
- [ ] Detached tmux sessions started on all 8 boxes
- [ ] Monitor every 30-60 min
- [ ] `collect_outputs.sh` runs on completion
- [ ] **All 8 boxes destroyed** (billing stops)
- [ ] Output MCAs verified — load into Vandirtest world and fly-through, check for "Failed to save" frequency

---

## Action items for user (cross-reference)

The above checklist gets filled in once user has SSD + memory of the specifics. AI will incorporate notes into [memory/S84_river_carver_geomorphology.md](memory/S84_river_carver_geomorphology.md) (river-related) and the canonical bake doc once filled in.
