---
name: box-guard
description: Manage the Hetzner render-box TTL guard — status, arm/disarm the scheduled sweeper, set per-box TTLs, arm on-box self-destruct. Use when starting a cloud render, when checking whether boxes are (still) running, or when a render box may have leaked.
---

# box-guard — render boxes always die on schedule

Tool: `cloud_bake/box_guard.py` (config `cloud_bake/box_guard.json`, log `cloud_bake/box_guard.log`).
Run with the project Python: `C:\Users\nicho\AppData\Local\Python\pythoncore-3.14-64\python.exe`.

**Why it exists (S104):** one box's collection check false-negatived and the box idled
until the orchestrator's 3h cap. Hetzner bills stopped-but-existing servers — only
**deletion** stops billing, so `poweroff`-style self-destructs bound compute, not cost.

## Three layers — arm all three for a render

1. **Scheduled sweeper (the always-on one).** A Windows scheduled task
   (`VandirBoxGuard`, every 15 min, pythonw = silent) runs `sweep`: DELETE any server
   older than its TTL. Survives dead orchestrators, closed terminals, crashed sessions.
   It should stay armed permanently — sweep is a quiet no-op at 0 servers.
2. **Per-run TTL labels.** Right after a dispatch script creates its boxes:
   `box_guard.py set-ttl --all <expected_minutes + 30>` — tightens the default
   (config `default_ttl_min: 300`) to the actual run budget.
3. **On-box self-destruct.** `box_guard.py selfdestruct <ip> --minutes N` arms a
   poweroff timer (compute stop). Add `--delete-via-api` to push the token so the box
   self-DELETEs via the metadata service — true billing stop even if this PC is off.

## Commands

```
py cloud_bake/box_guard.py status                      # servers + age/ttl/remaining
py cloud_bake/box_guard.py sweep [--dry-run]           # delete overdue servers now
py cloud_bake/box_guard.py set-ttl <name|id|--all> <m> # label ttl_min on server(s)
py cloud_bake/box_guard.py arm [--interval 15]         # install scheduled task
py cloud_bake/box_guard.py disarm / armed              # remove / inspect task
py cloud_bake/box_guard.py selfdestruct <ip> [--minutes 180] [--delete-via-api]
```

## Dispatch-time recipe (add to every render script)

```bash
"$PY" cloud_bake/box_guard.py arm                       # idempotent
# ... create boxes ...
"$PY" cloud_bake/box_guard.py set-ttl --all 120         # islands ~80m -> 120
# per box after launch:
"$PY" cloud_bake/box_guard.py selfdestruct "$ip" --minutes 120 --delete-via-api
```

## Cautions

- `name_patterns: [".*"]` — the sweeper considers EVERY server in the project
  eligible (single-purpose Hetzner project). If a long-lived server is ever added,
  put its name in `protected_patterns` or give it a huge `ttl_min` label FIRST.
- TTL too short kills a healthy render: mainland 50k needs ~4h → TTL ≥ 300;
  island full render ~80m → 120 is safe. When extending a live run:
  `set-ttl --all <new>` (label overrides config immediately at next sweep).
- The sweeper only runs while this PC is awake; `--delete-via-api` self-destruct is
  the layer that covers a sleeping/rebooting PC.
- After any render finishes, verify: `box_guard.py status` → "0 servers running."
