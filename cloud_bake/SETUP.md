# Vandir Cloud Bake — Hetzner Parallel Render

Render the full 50k Vandir world (9,409 tiles) on Hetzner Cloud in **~3 hours for ~$15**.

This is parallel-by-z-stripe: split the world into N stripes, render each on its own CCX63 box. Each box runs 48 workers (the CCX63's full vCPU count). 8 boxes is the sweet spot; 4 boxes is half-cost-half-speed.

## Prerequisites

- Hetzner Cloud account (https://www.hetzner.com/cloud) — new accounts often get €20 free credit
- SSH key on your laptop (or willingness to use password login)
- Your local `masks/` directory (~10 GB) ready to upload
- The `_spline_cache.pkl` file inside `masks/` (~65 MB) — saves 10-15 min per box

---

## Step 1 — Provision the staging box (5 min)

1. Hetzner Cloud Console → New Project → New Server
2. Location: **Ashburn (US-East)** if you're US, **Falkenstein** if EU
3. Image: **Ubuntu 24.04**
4. Type: **CCX13** is enough for staging (2 vCPU, 8 GB, $0.025/hr) — this box exists only to set up the snapshot
5. SSH key: upload your public key (or use root password)
6. Networking: keep default (public IPv4 + IPv6)
7. Create — note the public IP

## Step 2 — Bootstrap the staging box (5 min)

SSH in:
```bash
ssh root@<staging-ip>
```

Run the bootstrap (auto-downloads from GitHub):
```bash
curl -fsSL https://raw.githubusercontent.com/isononic-arch/minecraft-worldgen/master/cloud_bake/bootstrap_master.sh | bash
```

This installs Python 3.12 + venv, clones the repo, installs all deps. Takes ~3-5 min.

## Step 3 — Upload masks/ (30-90 min, depends on your upload speed)

From your laptop:
```bash
scp -r C:/Users/nicho/minecraft-worldgen/masks/ root@<staging-ip>:/root/minecraft-worldgen/
```

Or use rclone if scp is flaky:
```bash
# On laptop (one-time):
rclone config  # set up a remote called "hetzner" pointing at the box
rclone copy C:/Users/nicho/minecraft-worldgen/masks/ hetzner:/root/minecraft-worldgen/masks/
```

The biggest file is `slope.tif` (4 GB). At 10 Mbps upload that's ~1 hour just for that file. Plan accordingly.

## Step 4 — Smoke-test one tile (5 min)

SSH in, run a single-tile test to confirm everything works:
```bash
ssh root@<staging-ip>
cd /root/minecraft-worldgen
source /root/venv/bin/activate

# Test the painted-river tile we tuned against
python3 run_pipeline.py \
    --config config/thresholds.json \
    --masks /root/minecraft-worldgen/masks/ \
    --schem-index schematic_index.json \
    --output /root/minecraft-worldgen/output_smoke/ \
    --tile-x0 51 --tile-x1 52 --tile-z0 53 --tile-z1 54 \
    --threads 1
```

This should complete in ~10 min on the small staging box and produce `output_smoke/r.51.53.mca` (~5 MB). If you see log lines for `geomorph`, `bank asymmetry`, `melt gaussian`, `carve_completion`, `bed_melt_50k`, `bank_smooth` — **you're good to snapshot**.

## Step 5 — Snapshot the box (5 min)

In Hetzner Console:
1. Select your staging server
2. Tab: **Snapshots**
3. **Create Snapshot** → name it `vandir-baked-{date}`
4. Wait until it shows "Available" (~2-5 min)
5. Delete the staging server — the snapshot survives, ~$0.013/GB/month storage so a ~11 GB snapshot is ~$0.15/mo

## Step 6 — Spin N=8 worker boxes (10 min)

In Hetzner Console:
1. Add Server → location **same as snapshot**
2. Image tab → **Snapshots** → select your `vandir-baked-...` snapshot
3. Type: **CCX63** (48 vCPU / 192 GB / $0.57/hr) ← critical for parallelism
4. Quantity: **8**
5. SSH key + create — Hetzner spins them all up in ~2 min

Note all 8 IPs.

## Step 7 — Generate per-box commands (locally)

On your laptop:
```bash
cd C:/Users/nicho/minecraft-worldgen
python cloud_bake/plan_render.py --boxes 8 \
    --ips IP1 IP2 IP3 IP4 IP5 IP6 IP7 IP8
```

(In the order you want z-stripes assigned. The script prints exact `ssh root@IP "tmux ..."` commands ready to paste.)

You'll see output like:
```
VANDIR CLOUD BAKE PLAN
======================
Worker boxes:        8× CCX63 (48 vCPU / 192 GB)
Workers per box:     48
Total workers:       384
Tiles to render:     9409 (97 × 97)
Expected wall time:  2.9 hours
Estimated cost:      ~$13.68

PER-BOX COMMANDS
----------------

# Box 1: z=[0, 13)  (1261 tiles)
ssh root@IP1 "tmux new-session -d -s render-box1 '...'"

# Box 2: z=[13, 25)  (1164 tiles)
ssh root@IP2 "tmux new-session -d -s render-box2 '...'"

... (etc)
```

## Step 8 — Kick off all 8 boxes

Copy-paste each `ssh root@IP "..."` command into your terminal. Each starts a detached `tmux` session on the corresponding box. Total kickoff: ~30 seconds.

## Step 9 — Monitor (every 30-60 min)

The plan_render.py output also gave you monitor commands. Pick any box and check:
```bash
ssh root@IP1 'tail -20 /root/render-box1.log'
# or
ssh root@IP1 'ls /root/minecraft-worldgen/output_s83v17_world/*.mca | wc -l'
```

Each box should produce ~150-160 MCAs per hour (48 workers × ~3 tiles/hour each).

If a box shows MemoryError or worker silently crashes, you can re-run the same command — pipeline is idempotent and skips existing MCAs.

## Step 10 — Collect outputs (~30-60 min)

When the last box finishes its slice, on your laptop:
```bash
cd C:/Users/nicho/minecraft-worldgen
bash cloud_bake/collect_outputs.sh output_s83v17_world \
    IP1 IP2 IP3 IP4 IP5 IP6 IP7 IP8
```

This rsyncs all MCAs from all 8 boxes into your local `./output_s83v17_world/`. Expect ~50 GB total transfer.

When it finishes, you'll see:
```
ALL TILES COLLECTED. Ready to ship!
```

## Step 11 — Destroy the boxes (1 min)

Hetzner Console → select all 8 worker servers → Delete. **Billing stops immediately.**

Keep the snapshot if you might want to re-render (e.g. after a config tweak); delete it if you're done.

## Step 12 — Drop the MCAs into your Minecraft world

```bash
# Make sure Minecraft is fully closed (not just leaving world — quit to launcher)
cp output_s83v17_world/r.*.mca \
   'C:/Users/nicho/AppData/Roaming/ModrinthApp/profiles/test/saves/YourWorld/region/'
```

Or zip them up and ship to wherever your game distribution lives.

---

## Troubleshooting

**"MemoryError: Unable to allocate ..." in render.log:**
- Lower `--threads` from 48 to 32 on the affected box and re-run the same command (resumes idempotently).
- This shouldn't happen on CCX63 (192 GB / 3 GB per worker = 64-worker headroom), but if it does, drop threads.

**"spline cache key build failed" or cache rebuild every process:**
- Confirm `masks/_spline_cache.pkl` was uploaded. Without it, each fresh worker process spends 10-15 min building (still works, just slower).

**One box finished, others stuck:**
- Different tile mixes per stripe — z-stripes with more painted rivers take longer. Be patient or rebalance partitions next time.

**SSH connection drops mid-render:**
- That's why we use tmux. Re-attach with `ssh root@IP` then `tmux attach -t render-box1`. The render is unaffected by SSH disconnects.

**Total cost balloons past estimate:**
- Check you don't have orphan servers running. Hetzner UI shows all active servers with hourly rate.

---

## Faster — 16 boxes ($16, ~90 min)

If you want SUB-2-HOUR turnaround:
```bash
python cloud_bake/plan_render.py --boxes 16 --ips IP1 ... IP16
```

Each box does ~600 tiles. Same workflow; just more boxes to manage. Worth it if you're shipping today.

## Even faster — 32 boxes ($19, ~45 min)

Diminishing returns past 16 boxes due to Hetzner's 1-hour billing minimum, but doable:
```bash
python cloud_bake/plan_render.py --boxes 32 --ips IP1 ... IP32
```

## Cheaper — 4 boxes ($13, ~6 hours)

If you're not in a rush:
```bash
python cloud_bake/plan_render.py --boxes 4 --ips IP1 IP2 IP3 IP4
```

---

## Full timeline (8-box plan)

| Step | Time |
|---|---|
| 1. Provision staging box | 5 min |
| 2. Bootstrap | 5 min |
| 3. Upload masks | 30-90 min |
| 4. Smoke test | 10 min |
| 5. Snapshot | 5 min |
| 6. Spin 8 workers | 5 min |
| 7. Generate commands | 1 min |
| 8. Kick off | 1 min |
| 9. Monitor + wait | **~3 hours** |
| 10. Collect outputs | 30-60 min |
| 11. Destroy boxes | 1 min |
| **TOTAL** | **~5 hours hands-on + ~3 hours unattended** |

Cost: **~$15** total.
