# cloud_bake/ — Hetzner cloud render workflow

Scripts + docs for running Vandir worldgen renders on Hetzner CCX63 boxes.

## Scripts

| File | Purpose |
|---|---|
| `render_s85_validation.sh` | **One-shot 36-tile S85 validation render** — pass 4 IPs, walks away. Total ~30-40 min, ~$1-2. |
| `bootstrap_master.sh` | Fresh-Ubuntu bootstrap (Python 3.12, repo clone, venv, deps). Run on each box if no snapshot. |
| `plan_render.py` | Full-world (9,409 tile) z-stripe planner. For when validation passes + you're ready to bake. |
| `warm_cache.py` | Pre-warm masks cache (optional optimization). |
| `monitor.ps1` | PowerShell monitor for parallel render. |
| `collect_outputs.sh` | Collect MCAs from worker boxes back to laptop. |
| `validation_tiles.txt` | 36-tile validation set with TP commands (human + machine readable). |

## Quick start: S85 validation render

1. **Spin 4× CCX63 from snapshot** (Hetzner Console)
   - Image: `vandir-baked-s85-validated` (or `-s85-veg` if you've saved one)
   - Quantity: 4
   - Note the 4 IPs.

2. **In Git Bash, project root:**
   ```bash
   cd /c/Users/nicho/minecraft-worldgen
   bash cloud_bake/render_s85_validation.sh IP1 IP2 IP3 IP4
   ```

3. **Wait ~30-40 min.** Script prints progress every minute. Walks away when done with MCAs copied into Vandirtest10/region/.

4. **Manually destroy boxes** in Hetzner Console to stop billing.

## What `render_s85_validation.sh` does

```
STEP 1/10  Adding host keys
STEP 2/10  Testing SSH to all 4 boxes
STEP 3/10  Copying laptop SSH key to Box 1 (for inter-box rsync)
STEP 4/10  Pulling branch 's85-cherry-picks' on all 4 boxes
STEP 5/10  Clearing stale mask caches
STEP 6/10  Uploading Vegetation/ (30 MB, 998 files) to Box 1   [skipped if present]
STEP 7/10  Mirror Vegetation/ from Box 1 -> Boxes 2,3,4         [skipped if present]
STEP 8/10  Dispatching 36-tile render (9 tiles/box, parallel inside tmux)
STEP 9/10  Monitoring (poll every 60s until done)
STEP 10/10 Collecting MCAs to laptop + installing into Vandirtest10
```

Total ~30-40 min wall time. Cost: ~$1-2 (4 boxes × ~0.5 hr × $0.57/hr).

## Future renders are cheaper

After your first render:
- Snapshot Box 1 as `vandir-baked-s85-veg` (includes uploaded Vegetation/)
- Next time, spin 4 boxes from that snapshot → STEP 6 skipped → ~20-25 min wall.

## Full world bake (when ready)

Once 36-tile validation passes:
- Spin 8× CCX63 from snapshot (more boxes = more parallel throughput for 9,409 tiles)
- Use `plan_render.py` to dispatch the world by z-stripe
- Expected: ~3-4 hours wall, ~$7-9 total
- See `SETUP.md` for the full bake workflow

## Tile list

See `validation_tiles.txt` for the 36-tile validation set with TP commands.
The render script hardcodes the same tiles in 4 round-robin groups of 9.
