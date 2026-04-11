# tests/baselines/3x3/

Committed baselines for the 3×3 pre-MCA validator. One subdirectory per
center tile, named `{cx}_{cz}` (e.g. `36_20`).

Each subdirectory contains:
- `summary.json` — the `summary.json` produced by a known-good validator run
- `report.txt` — human-readable snapshot at commit time
- `stitched_biomes.png` — 1536×1536 pseudocolor biome map of the 3×3
- `stitched_blocks.png` — 1536×1536 pseudocolor surface block map

**Never hand-edit these files.** To update a baseline:

1. Run the validator fresh: `py tools/validate_3x3.py --tile-x X --tile-z Z ...`
2. Review the new output. Confirm the diff is intentional (e.g. you
   deliberately changed a palette) and not a regression.
3. Copy the new files over the baseline.
4. Commit with a clear message explaining what changed and why.

See `tests/README.md` for the full workflow.

**Recommended baseline tiles** (drawn from CLAUDE.md reference set):

| Center | Reason |
|---|---|
| 36_20  | Rock exposure / treeline transition |
| 24_80  | Desert rock + alpine (current Session 41 focus) |
| 51_53  | Floodplain / lakes / schematic reference |
| 59_53  | Windthrow |
| 16_73  | Meander reference |
| 25_72  | Flat sand desert |
| 48_48  | Center sea tile |

Not every tile needs a baseline on day 1. Start with the 1-2 tiles you
actively work on, and add the rest as they become relevant.
