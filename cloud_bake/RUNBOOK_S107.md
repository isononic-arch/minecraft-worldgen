# RUNBOOK — S107 full-world render → Bloomhost → D: zip (overnight autonomous)

**Goal:** render S107 (commit `b742f58`, seam-bush fix `13e68f5`) as a fresh full
world, deploy it box-direct to the Bloomhost Paper server as `VandirWorld_S107`,
then save a zipped copy to `D:\VandirWorld_S107.zip`. Cheap (one clean render,
~€52), bounded (< €60 hard), self-healing, no re-renders.

## Who does what (three independent layers, decreasing token/€ cost)

1. **`s107_driver.py`** (detached `pythonw`) — the engine. Runs every phase in
   order, resume-safe via `s107_status/state.json`. ZERO Claude tokens.
2. **`s107_watchdog.py`** (detached `pythonw`) — relaunches the driver if its
   process dies (driver resumes from checkpoint); sweeps boxes + writes
   `s107_status/ALERT.txt` on unrecoverable failure. ZERO tokens.
3. **`box_guard`** scheduled task (`VandirBoxGuard`, every 15 min) — deletes any
   `vandir-50k-*` / `v18-*` box past its `ttl_min` label. Home-side billing net,
   survives session/Claude exit. Plus per-box **on-box self-destruct** (metadata
   API) survives even this PC dying.

An **Opus check-in cron** (this session) wakes periodically ONLY to police box
billing + report — it is insurance, not load-bearing.

## Phase state machine (driver)

`mainland_dispatch → mainland_runspec → gate_start(async) → mainland_monitor →
mainland_refire? → islands_dispatch → islands_monitor → islands_refire? →
finalize → stitch_audit(non-fatal) → pull_zip → final sweep`

Each phase writes `state.json.phases[<name>] = {status: done|skipped, at}`.
Rerunning the driver skips completed phases (idempotent).

## The one irreversible action: deleting S106

`s107_gate.py` is the ONLY thing that deletes `VandirWorld_S106`, and only after:
1. `D:\VandirWorld_S107_staging\metadata_saved.flag` exists (S106 level.dat,
   datapacks, playerdata already saved locally — DONE at setup), AND
2. a **quorum of 4** mainland boxes each rendered ≥ their region count with no
   crash. All boxes run identical code, so 4 independent successes ⇒ the pipeline
   is sound. A SYSTEMIC bug crashes the quorum too ⇒ gate times out ⇒ **S106 is
   never deleted** ⇒ boxes `PUSH_GATE_TIMEOUT` ⇒ monitor reaps ⇒ driver aborts.
   The old world stays live. There is NO local S106 world copy, so this guard is
   what prevents stranding the server on a bad render.

After the gate opens (`.go` on the server), boxes push their regions via
`box_push.py` (16 conns, `.push_lock` SFTP mutex serializes them, size-verified,
3 re-push rounds). Contested island-overlap regions are NOT pushed — they ride
home in the collect tarball and `s107_finalize.py` chunk-merges + uploads them.

## Cost controls (the $250 lesson: yesterday = ~5 render attempts × ~€50)

- ccx63 = **€1.6138/hr, rounds UP to the hour**. This is one clean render.
- TTLs: mainland box **135m** (monitor reaps 120m), islands box **140m** (monitor
  reaps 130m). Worst case all-hang = 8×135 + 8×140 boxes-min × rate ≈ **€59 < 60**.
- Refires: **≤1 per phase**, surgical (mainland 1 box; islands only the failed
  box indices), **TTL 90m**. Budget gate blocks a refire if est spend > €53.
- No `keep_alive` — boxes deleted the instant their manifest is collected.
- Structural win: box-direct push removes the home-collection bottleneck that
  lost S106's collection yesterday and forced a full re-render.

## Live status — read these

| File | Meaning |
|---|---|
| `s107_status/state.json` | phase checkpoints + `spend_est_eur` + `completed`/`failed` |
| `s107_status/driver.log` | heartbeat (grows every 60s while a phase runs) |
| `s107_status/watchdog.log` | relaunch/sweep actions |
| `s107_status/ALERT.txt` | present ⇒ watchdog hit an unrecoverable failure |
| `s107_status/gate_done.json` | present ⇒ S106 deleted, `.go` open |
| `s107_status/finalize_done.json` | audit pass/fail + region stats |
| `s107_status/ALL_DONE.flag` | pull+zip complete → `D:\VandirWorld_S107.zip` |
| `<collect_dir>/monitor.log` | per-box probe/collect/reap trail |
| `<collect_dir>/monitor_state.json` | `refire` list = work units that failed |

Quick health: `py cloud_bake/box_guard.py status` (want ≤8 boxes, none age>ttl),
`py -c "import json;print(json.load(open('s107_status/state.json'))['phases'])"`.

## Opus takeover playbook (if you are woken to intervene)

1. `box_guard status`. **If any box age > ttl_min → it should already be dying;
   if >20m over, delete it now** (`box_guard sweep` or API DELETE). Billing first.
2. Read `state.json`. Is `failed` set? Read the reason + the relevant phase log.
   Is `completed` set? Then verify `ALL_DONE.flag` + report; nothing to do.
3. Is the driver process alive? `tasklist /FI "PID eq <driver.pid>"`. If dead and
   not completed/failed and the watchdog didn't relaunch → relaunch it:
   `Start-Process -WindowStyle Hidden pythonw cloud_bake\s107_driver.py`
   (it resumes from the last checkpoint).
4. `ALERT.txt` present? It names the failure. Common cases:
   - **gate timeout / S106 intact** → render failed systemically. S106 is still
     the live world (safe). Investigate the box job logs; do NOT hand-delete S106.
   - **relaunch budget exhausted** → driver kept crashing. Read `driver.log` tail
     for the exception; fix + relaunch, or sweep boxes and stop for the morning.
5. **Never** delete `VandirWorld_S106` by hand — only the gate, with its quorum,
   may. If S107 is a partial mess and S106 is gone, the recovery is: re-run the
   driver (resumes) or re-render — the world is reproducible from commit `b742f58`.

## Manual full restart (clean slate)

```
py cloud_bake/box_guard.py sweep                      # kill any stray boxes
rm -rf s107_status  islands/_collect_v18*  D:/render_s107/_collect*
py cloud_bake/s107_preserve_meta.py                   # if staging was cleared
Start-Process -WindowStyle Hidden `
  "C:\Users\nicho\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe" `
  "cloud_bake\s107_driver.py"
Start-Process -WindowStyle Hidden `
  "C:\Users\nicho\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe" `
  "cloud_bake\s107_watchdog.py"
```

## Facts / IDs

- Bloomhost SFTP creds: `C:\Users\nicho\.bloom_creds.json` (NOT in git).
- Hetzner token: `C:\Users\nicho\.hetzner_token`. Snapshot `396927540`.
- Islands: 15 islands, 8 boxes, Madre (`-50_393`) alone on b3. 1369 island regions;
  10 contested island-vs-island regions merged at finalize; 206 island-owned
  mainland-grid regions in `cloud_bake/mainland_skip_regions_s101.txt`.
- Expected final world ≈ 10,572 regions (mainland 9,203 + islands 1,369),
  audited against `cloud_bake/_bloom_s106_regions.json`.
- server.properties `level-name` → `VandirWorld_S107` (backup
  `server.properties.pre_s107.bak`). Server is STOPPED (user confirmed) — safe to
  overwrite; user starts it in the morning.
