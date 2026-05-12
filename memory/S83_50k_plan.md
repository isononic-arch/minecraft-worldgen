# S83 Full-World 50k Generation Plan

**Goal:** Render all 9,409 tiles (97 × 97 grid) of Vandir with the v17 pipeline.

**Pre-flight gate:** All v17 features verified per `memory/S83_v17_handoff.md`. Test tile (51,53) reviewed in-world, user verdict "it's perfect."

## Math

- World: 50,000 × 50,000 blocks = 9,409 tiles
- Per-tile single-worker: ~1,800s (30 min)
- Serial total: 9,409 × 30 min = **195 days**
- 2 workers (if memory allows): ~98 days
- 4 workers: ~49 days
- 8 workers: ~25 days (requires significant memory headroom)

## The hard constraints

1. **Memory:** Single worker peaks ~2.5-3GB for `_ensure_caches`. 2-worker OOMed during v12 dev. v17's footprint is similar to v15-v16. Conservative answer: **start with 1 worker until profiled.**

2. **Spline cache:** `masks/_spline_cache.pkl` is on-disk and pickled by `_ensure_caches`. Cache key = md5(hydro_region.png bytes) + md5(spline source) + params hash. First process rebuilds (~10-15 min), subsequent processes HIT instantly. Critical for full-world cost.

3. **Tile-boundary correctness:** PAD=48 padding around each tile inside the escape-fix block. All passes that touch surface_y or river_water_y run on the padded array, then crop back. Tile seams should be invisible. **Validated on (51,53) ↔ (52,53) boundary** during S82 work.

## Plan

### Phase 1 — Two-worker memory probe (45 min)

Before full render, prove 2 workers can run together without OOM on v17's lighter memory profile:

```
py run_pipeline.py --threads 2 --tile-x0 50 --tile-x1 52 --tile-z0 53 --tile-z1 54
```

That's 4 tiles, 2 workers, ~45 min wall-clock. If clean → proceed at 2 workers. If OOM → 1 worker only.

### Phase 2 — Full-world render

Single command:
```
py run_pipeline.py --threads N --tile-x0 0 --tile-x1 97 --tile-z0 0 --tile-z1 97
```

Where N is the safe worker count from Phase 1.

ETAs:
- 1 worker: 195 days
- 2 workers: 98 days (if Phase 1 passes)

**This won't fit in a session.** Two paths:
- **Local long-running** — accept it'll run for weeks/months
- **Cloud bake** — copy code + masks to a beefy box, run there

### Phase 3 — In-world validation

After Phase 2:
1. Copy region/ to a fresh test world.
2. Spawn at void (12000, 100, 12000).
3. Walk the 26 biome reference tiles per `memory/BIOME_VALIDATOR_CHECKLIST.md`.
4. Spot-check painted rivers at multiple bends, lakes at multiple basins.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Mid-run failure (OOM, crash) | The pipeline is per-tile idempotent: re-run with same args and existing MCAs are skipped. Resume from where it stopped. |
| Spline cache stale | Cache key includes md5(hydro_region.png) — if user re-paints, cache invalidates and rebuilds. Set `VANDIR_NO_SPLINE_CACHE=1` to force rebuild. |
| Tile-boundary seams | PAD=48 in run_pipeline.py escape-fix block already handles. If seams appear: bug. |
| Disk space | Each MCA is ~5-6MB. 9,409 tiles → ~50GB output. Check disk before starting. |
| Hardware lockup | Limit `--threads` conservatively, expect long-running process. |

## Pre-flight checklist (do this before kicking off Phase 2)

- [ ] `git status` clean and v17 committed/pushed
- [ ] Phase 1 (2-worker memory probe) ran clean (or chose to use 1 worker)
- [ ] At least 60 GB free disk in target output dir
- [ ] `masks/_spline_cache.pkl` exists (avoids cold-cache cost per worker)
- [ ] Test rendering 1 tile end-to-end works (validate the toolchain isn't broken)
- [ ] Pick output directory name (suggest: `output_s83v17_world/`)
- [ ] If running unattended for days: configure auto-restart on machine reboot, monitor disk

## Kickoff command (Phase 2)

```bash
# 1 worker (safe):
nohup C:/Users/nicho/AppData/Local/Python/pythoncore-3.14-64/python.exe \
    run_pipeline.py \
    --config config/thresholds.json \
    --masks C:/Users/nicho/minecraft-worldgen/masks/ \
    --schem-index schematic_index.json \
    --output output_s83v17_world/ \
    --tile-x0 0 --tile-x1 97 --tile-z0 0 --tile-z1 97 \
    --threads 1 \
    > output_s83v17_world/render.log 2>&1 &

# 2 workers (if Phase 1 passes):
... --threads 2 ...
```
