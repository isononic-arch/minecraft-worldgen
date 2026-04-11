# tests/

Committed truth for the validation loop. Everything here is human-curated
and git-tracked. Generated validator output lives elsewhere under
`validation_report_*/` at the project root and is gitignored.

## Layout

```
tests/
  baselines/
    3x3/                 ← 3×3 pre-MCA validator baselines
      {cx}_{cz}/
        summary.json     ← committed expected state for this center tile
        report.txt       ← human-readable snapshot at commit time
        stitched_biomes.png
        stitched_blocks.png
```

A baseline is the known-good `summary.json` (plus the stitched PNGs that
were produced alongside it) for a given 3×3 center tile. The validator
diffs current output against these.

## Creating a new baseline

1. Verify the world is in a state you want to snapshot (e.g. in-game
   tile is loading cleanly, the last round of visual inspection looked
   right).
2. Run the 3×3 validator with no `--baseline`:

   ```
   py tools/validate_3x3.py \
       --config config/thresholds.json \
       --masks masks/ --output output/ \
       --tile-x 36 --tile-z 20 \
       --report validation_report_3x3_36_20
   ```

3. Review `validation_report_3x3_36_20/report.txt`. If every check is
   PASS or a deliberate WARN, copy the outputs into a baseline directory:

   ```
   mkdir -p tests/baselines/3x3/36_20
   cp validation_report_3x3_36_20/summary.json       tests/baselines/3x3/36_20/
   cp validation_report_3x3_36_20/report.txt         tests/baselines/3x3/36_20/
   cp validation_report_3x3_36_20/stitched_biomes.png tests/baselines/3x3/36_20/
   cp validation_report_3x3_36_20/stitched_blocks.png tests/baselines/3x3/36_20/
   ```

4. Commit the baseline. Any time it needs to change, the commit
   message should explain which behavior changed and why.

## Using a baseline

```
py tools/validate_3x3.py ... \
    --report validation_report_3x3_36_20 \
    --baseline tests/baselines/3x3/36_20
```

Any check that was PASS in the baseline but FAIL in the current run is
reported as a **regression**. The validator exits 1 if regressions are
detected, even when the absolute `n_fail` count hasn't grown (i.e. a
different check started failing but an old failing one was fixed).

## What baselines do NOT cover

- In-game visual artifacts. Still requires eyeball QA.
- chunk_writer / NBT correctness. Use `validate_3x3.py --full` for that.
- schematic placement. The 3×3 pre-MCA runner skips it.
- Anything requiring Minecraft itself to be loaded.

## What lives here later

- `tests/unit/` — pure-Python unit tests for `biome_assignment.py`,
  seam math, spline interpolation, etc. Pytest-discoverable.
- `tests/fixtures/` — tiny synthetic masks used by unit tests.
- `tests/regression/` — larger end-to-end regression suites.

All gitignored output lives at the repo root as `validation_report_*/`.
Never mix the two.
