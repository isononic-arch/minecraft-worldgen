# Smoke Test Prompt v2 — DELETE AFTER USE

> Temporary scratch file. Paste the block below (everything between the two `---` rules) into a fresh Claude Code session opened at `C:\Users\nicho\minecraft-worldgen\`, then delete this file.

---

Read `CLAUDE.md` first, then read `memory/project_vandir_status.md` (most recent entries) and `TRIAGE_validate_2026-04-10.md` polarity banner before doing anything.

Cowork just fixed 4 things:
1. `config/validation_affects.json` mask bounds for `override` (→ 0.35/0.50) and `shore` (→ 0.25/0.40, now discrete).
2. `tools/validate_masks.py` `DEFAULT_BOUNDS` synced to the same values with inline comments.
3. `TRIAGE_validate_2026-04-10.md` got a polarity correction banner at the top.
4. `tools/validate_masks.py` was **truncated** at line 252 with an unclosed `write_text(` and no `main()` return; cowork completed it. Run `py -m py_compile tools/validate_masks.py` first as a sanity check — it should compile clean.

**Ground truth reminder:** land fraction is **30%** of world (`height > 17050`), not 70%. Source of truth for override correctness is visual match against `output/override_worldview.png`, not numerical guesses.

Steps:

1. Sanity check the validator file compiles:
   ```
   py -m py_compile tools/validate_masks.py
   ```
   If this errors, stop and show me the error.

2. Run mask sanity:
   ```
   py tools/validate_masks.py --masks masks/ --report validation_report_masks
   ```
   Expected: **20 PASS, 0 FAIL**. Read `validation_report_masks/report.txt` and `checks.json`. If anything fails, stop and show me the failing mask(s) with expected vs observed bounds — do NOT proceed to step 3.

3. Run the 3×3 pre-MCA validator on tile **(48, 48)** — the center sea tile. This is intentional: ocean-heavy tile, hydrology is nearly a no-op, should finish in ~10 min. We're proving the validator itself works before touching dense-hydro tiles like (36, 20).
   ```
   set PYTHONUNBUFFERED=1
   py tools/validate_3x3.py --config config/thresholds.json --masks masks/ --output output/ --tile-x 48 --tile-z 48 --report validation_report_3x3_48_48
   ```
   Expected wall time ~10-15 min. If it's silent past 20 min with no progress lines in stdout, kill it and capture a py-spy dump (`pip install py-spy --break-system-packages` if missing, then `py-spy dump --pid <PID>`). Don't kill blind — we need the stack trace this time.

4. When it finishes, read `validation_report_3x3_48_48/summary.json` and `report.txt`. Show me:
   - Exit code
   - Per-tile PASS/FAIL/WARN counts
   - Every FAIL (check name, tile, message)
   - Every seam check result (the 3 multi-tile ones: biome seam, block palette seam, surface_y seam step)
   - Elapsed time (total + per-tile if available)

5. Open the three stitched PNGs (`stitched_biomes.png`, `stitched_blocks.png`, `stitched_surface_y.png`) and describe anything visually off — seam lines, biome bleed, obvious artifacts at tile boundaries. (48, 48) is mostly ocean so expect blue with minor coastal fringing.

6. If everything is PASS and the stitches look clean, **ask me** whether to snapshot it as the baseline at `tests/baselines/3x3/48_48/` per `tests/baselines/3x3/README.md`. Do not create the baseline without my confirmation.

Hard rules while you work:
- Do NOT modify any `core/*.py`, `rebuild_*.py`, `masks/*`, or `override*` files.
- Do NOT run `run_pipeline.py` or write `.mca` files — pre-MCA only.
- Do NOT use `height ≤ 17050` as "land" anywhere. Land is `height > 17050`. If you catch yourself about to report a "land fraction", double-check the comparator against `CLAUDE.md` HEIGHT POLARITY first.
- If a check fails in a way that looks like a bug in `validate_3x3.py` / `_pipeline_runner.py` / `validate_masks.py` themselves (import error, shape mismatch, truncated file, key error), fix it in place and re-run that step only. **Log the fix to `memory/project_vandir_status.md` with a timestamp BEFORE retrying.**
- If a check fails in a way that looks like real pipeline data (biome boundary wrong, surface block wrong, seam step too large), STOP and report — don't try to fix the pipeline.
- 2 failed fixes for the same symptom → STOP, investigate with a diagnostic script, or ask me.

Python interpreter: `C:\Users\nicho\AppData\Local\Python\pythoncore-3.14-64\python.exe` (aliased `py`).
