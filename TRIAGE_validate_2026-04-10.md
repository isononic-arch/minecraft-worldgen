# Validation Triage — 2026-04-10

> **⚠️ POLARITY CORRECTION (added 2026-04-10 after cowork investigation):**
> This document's original override analysis used inverted height polarity
> (`raw ≤ 17050` labeled as "land"). **That is wrong.** Canonical rule from
> `CLAUDE.md` HEIGHT POLARITY: **HIGH raw = HIGH terrain**, sea level = raw 17050,
> so **land = `height > 17050`**.
>
> Corrected numbers (measured 2026-04-10 from `masks/override.tif` directly):
> - land fraction = **30.00%** of world (NOT 70%)
> - override covers **99.63%** of land
> - only **0.37%** of land is unzoned (coastal sliver, expected)
> - global override nonzero = 39.90% = 30% land × 99.6% + ~14% ocean coastal zones
>
> **There is no "30% unzoned land" regression.** Both override.tif and shore.tif
> are healthy. Both `validate_masks.py` failures below were incorrect validator
> default bounds (my writing), fixed in the same pass. See
> `memory/project_vandir_status.md` log entry 2026-04-10 for the evidence trail.
>
> **Source of truth for override correctness:** visual comparison against
> `output/override_worldview.png`. The current `masks/override.tif` matches that
> reference on continental shape and coverage.
>
> The rest of this document is preserved verbatim as the original runtime triage.
> Treat all override/shore conclusions below as superseded.

---

Two validators were run against `masks/`. Both produced misleading or unusable results. This report is for cowork triage.

## TL;DR

1. **`validate_masks.py` is buggy** — it samples only the top-left corner `Window(0, 0, 4096, 4096)` to compute coverage. For a 50k×50k world where the NW corner is ocean/empty, almost every sparse-content mask gets a false 0.0% coverage.
2. **`validate_3x3.py` hung silently** for ~1h27m at tile (36, 20) producing zero stdout and zero report files. CPU active (~26 min user time, ~1.1 GB RSS) — not deadlocked, but no progress visible. Killed at user request.
3. Real data integrity is mostly fine; only one mask (`sand_dunes`) appears genuinely empty in the center sample.

---

## Run 1: `validate_masks.py --masks masks/ --report validation_report_masks`

Result: **13 PASS / 7 FAIL**.

### Reported failures (all "coverage 0.000 < min")
| Mask              | Reported cov (NW corner) | Center 2k×2k cov (this report) | Verdict        |
|-------------------|--------------------------|--------------------------------|----------------|
| override          | 0.000                    | **0.6455**                     | False positive |
| shore             | 0.000                    | **0.3690**                     | False positive |
| river             | 0.000                    | 0.0036                         | False positive (sparse but present) |
| hydro_floodplain  | 0.000                    | 0.0446                         | False positive |
| wind_windthrow    | 0.000                    | 0.0023                         | False positive |
| rock_exposure     | 0.000                    | 0.0175                         | False positive |
| sand_dunes        | 0.000                    | **0.0000**                     | **Possibly real — needs follow-up** |

### Root cause
`tools/validate_masks.py:144`:
```python
sample = src.read(1, window=Window(0, 0, w, h))   # SAMPLE_WINDOW = 4096
```
The NW 4096×4096 of a 50k×50k Vandir world is ocean. So any mask whose content is land-only (override zones, shore, rivers, vegetation, dunes, etc.) reads as all-zero in that window and falls below its min-coverage threshold.

### Inspection method (this report)
For each failing mask, opened with rasterio and read a 2000×2000 window centered at (25000, 25000). All counts above are from that center window. `override.tif` confirmed: 4 zone codes present (0, 10, 110, 120), file 8.2 MB, dtype uint8, shape 50000×50000 — healthy.

### Recommended fix
Replace the corner sample with either:
- A multi-window sample (e.g. 5 windows: NW, NE, SW, SE, center) and use the **max** coverage, OR
- A strided full-mask read (e.g. `src.read(1, out_shape=(2000, 2000), resampling=Resampling.average)`) — cheap on overviewed TIFs and gives a true global estimate.

The min-coverage thresholds themselves look reasonable; only the sampling strategy is wrong.

### Remaining real concern
- **`sand_dunes.tif`** — center sample is all-zero too. May be empty everywhere, or dunes may be confined to a narrow desert strip outside both sample windows. **Action:** strided full-mask scan to confirm before trusting.

---

## Run 2: `validate_3x3.py --tile-x 36 --tile-z 20 --report validation_report_3x3_36_20`

Result: **HUNG / killed.**

### Symptoms
- Started 15:14 local, killed 16:42 local — **1h 27m wall**
- CPU: ~26 min user time, ~3.4 s kernel, ~1.1 GB RSS — actively computing
- Stdout: completely empty (just two rasterio warnings, see `b3f5v94sk.output`)
- `validation_report_3x3_36_20/` directory created at start, then **0 files written**
- For comparison: last good single-tile validation (48, 48, Session 26-ish) was ~80 s

### Possible causes (unverified — please triage)
1. **Stdout buffering** — Python without `-u` may buffer everything until exit. We have no progress signal at all, so we can't tell if it's looping or just slow. **Easy fix to test next run:** add `-u` or `PYTHONUNBUFFERED=1`.
2. **Tile (36, 20) inputs are pathological.** Worth checking what's at this tile — `output/rock_surface_topdown_36_20.png` exists from Apr 9 16:25, so the tile was reachable then. Has anything in `masks/` been regenerated since?
3. **Hydrology stage runaway.** Several hydro masks (per validate_masks) had non-zero data in center samples but the values are sparse. If `validate_3x3` triggers a flood-fill or river-trace that gets stuck on a degenerate basin near (36, 20), it could spin without producing output.
4. **Eco/decoration stage.** Memory note from Session 27: `process_tile_columns_v2` is **not** yet eco-aware, but `validate_test_tile.py` may still try to run eco conditions. If `validate_3x3` shares that path, it could be doing redundant or unbounded work.

### Recommended next steps
1. **Re-run with unbuffered stdout + a smaller scope** to get visibility:
   ```
   PYTHONUNBUFFERED=1 python tools/validate_3x3.py \
     --config config/thresholds.json --masks masks/ --output output/ \
     --tile-x 36 --tile-z 20 --report validation_report_3x3_36_20
   ```
   If still silent for >2 min, attach a profiler / py-spy dump.
2. **Try a known-good tile first** — re-run validate_3x3 at (48, 48) (last known good single-tile coordinate) to confirm the validator itself works at all in its current state.
3. **Diff `validate_3x3.py` vs `validate_test_tile.py`** — find where 3×3 differs from the working single-tile path. The 3×3 wrapper may be doing per-tile setup 9× without sharing caches.

---

## Mask file health snapshot (center 2k×2k @ tile space)

| Mask              | Size     | dtype  | Center nz%  | Min/Max     | Notes |
|-------------------|----------|--------|-------------|-------------|-------|
| height            | 1803.7 MB| uint16 | 100.00%     | 5978/29710  | Healthy. Range matches "land + ocean" (see polarity rules in PROJECT_MEMORY). |
| override          | 8.2 MB   | uint8  | 64.55%      | 0/120       | 4 zones in center window (0, 10, 110, 120). Healthy. |
| shore             | 8.5 MB   | uint16 | 36.90%      | 0/65535     | Binary 0/65535 — healthy. |
| river             | 6.0 MB   | uint16 | 0.36%       | 0/65535     | Sparse but present. |
| hydro_floodplain  | 10.6 MB  | uint8  | 4.46%       | 0/1         | Healthy. |
| wind_windthrow    | 9.6 MB   | uint8  | 0.23%       | 0/1         | Sparse. |
| rock_exposure     | 72.7 MB  | uint8  | 1.75%       | 0/75        | Multi-value, healthy. |
| sand_dunes        | 23.9 MB  | uint8  | **0.00%**   | 0/0         | **Suspicious — center fully empty. Verify with strided full read.** |

---

## Self-inspection notes (for cowork)

What I (Claude) checked directly:
- Killed PID 11456 cleanly.
- Read center 2000×2000 windows of 8 masks via rasterio.
- Read `tools/validate_masks.py:120-180` to find the corner-sampling bug.
- Confirmed `validation_report_3x3_36_20/` directory is empty (no JSON / no PNG produced before kill).
- `b3f5v94sk.output` contains only two `NotGeoreferencedWarning` lines — no progress markers, no tracebacks.

What I did **not** check (left for cowork triage):
- Source of `validate_3x3.py` itself — did not open it. Suggested next step: read the entry point and confirm whether it iterates 9 tiles in a single process or spawns subprocesses.
- Whether `sand_dunes.tif` is truly empty or just empty in the (25000, 25000) window.
- Whether `masks/override.tif` is the latest upscale (file mtime not checked).
- Whether the spline / thresholds.json has been touched since last successful run.
