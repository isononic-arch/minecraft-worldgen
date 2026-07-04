---
name: render-script-review
description: Review a cloud render/dispatch script (islands/*.sh, cloud_bake/*.sh) before dispatch — lint for completion-flag mismatches, remote shell-quoting hazards, unbounded poll/retry loops, and missing box-cleanup safety nets. Use before running any new or edited cloud render script.
---

# render-script-review — pre-dispatch check for cloud scripts

Run the linter first, then do the manual pass below on anything it can't see.
Linter: `cloud_bake/lint_render_scripts.py` (project Python).

```
py cloud_bake/lint_render_scripts.py islands/_cloud_render_vNN.sh   # one script
py cloud_bake/lint_render_scripts.py --all                          # every *.sh
```

Exit 1 = HIGH findings. Findings are review prompts, not verdicts — verify each
against the script before changing anything. Suppress a checked line with `# lint-ok`.

## What the linter catches (our actual past failures)

- **FLAG** — poll loop tests a `/root/<flag>` that neither the script nor its
  referenced box drivers (`islands/_box_*.py`, `*.sh`) ever write (S101c bug:
  test-render polled the wrong done-flag name). Convention: islands drivers write
  `/root/all_done`; mainland writes `/root/done` (`touch` after md5s).
- **QUOTE** — unquoted heredocs feeding `ssh bash -s` (every `$` expands LOCALLY;
  remote-side vars need `\$` or `<<'EOF'`); `$(...)` inside double-quoted ssh
  commands; tmux payloads inside heredocs = triple expansion.
- **ORPHAN** — `while :;` poll loops with no elapsed-time cap; failure branches
  that "retry next pass" without a bounded counter (S104: a persistently failing
  collect kept a box alive until the 3h cap); backgrounded jobs without `wait`.
- **NET** — script creates servers but lacks: any DELETE, an end-of-script
  delete-by-name-pattern sweep, `set -u`, or a box_guard arm. Local
  `nohup sleep+DELETE` auto-killers are flagged: they die if this PC reboots.

## Manual pass (linter-blind spots)

1. **Never edit a script while it's running** — bash re-reads from disk mid-run.
2. Every ssh probe in the poll loop: what happens on a TRANSIENT ssh failure?
   `test -f` via ssh returning non-zero must be treated as "unknown", not "not done".
3. Collect step: is success verified (`[ -s tarball ]`, md5) before the box is
   deleted? Is failure BOUNDED (N retries then force-collect logs + delete)?
4. Completion flags: `rm -f` the flag before launch (stale flag from a previous
   run = instant false DONE).
5. Location fallback loops (capacity): confirm the fallback list can't create the
   same box twice.
6. Dispatch backgrounding: orchestrators via `run_in_background`/`&` need their pid
   recorded and a kill path (memory: box_dispatch_hang — `wait` after ssh+tmux
   dispatch can hang forever; prefer polling with a deadline over bare `wait`).
7. Cost check at the end of the session: `box_guard.py status` must show 0 servers.
