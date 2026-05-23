# _stale/ — Containment for old backups and duplicates

Files in here are preserved because Vandir has been damaged by override edits
in the past and Nick explicitly wants rollback material kept. **Do not reference
these from any active script.** They are NOT part of the pipeline.

Organized:

- `override_pngs/` — historical override_*.png variants from before the
  vectorized pipeline. `override_final.png` (current master) and
  `override_final_backup.png` (canonical backup) remain at repo root.
- `copy_files/` — `... - Copy` artifacts from Windows copy-paste.
- `misc/` — throwaway debug text files.

If you need to restore one, copy it out, don't edit it in place. If this
directory grows unwieldy, add dated subdirectories like
`_stale/2026-04-10_pre-reorg/`.

Moved here: 2026-04-10 during Session 41 cleanup pass.
