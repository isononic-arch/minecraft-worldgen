"""Post-render health verifier — catches the silent-failure modes that burned
the last 50k render (exit-0 tile errors, per-chunk except-continue gaps,
OOM-killed workers with missing MCAs, bed-cache rebuild regressions).

Checks:
  1. Expected region files present (from a tile list: r.<tx>.<tz>.mca each).
  2. Per-region chunk count from the Anvil header (location table, bytes
     0..4095; 1024 non-zero entries = full region).
  3. Render logs: any "tile_error" lines -> FAIL; bed cache must show
     "MIGRATED" or "HIT" and never "_BedCacheRefusal" / "falling back".
  4. chunk_errors.log files: any content -> FAIL with counts.
  5. MCA size floor (a region under ~2 MB is suspicious for this world).

Usage:
  py tools/verify_render_health.py --out-dir DIR [--tiles memory/verify_s94_tiles.txt]
      [--logs DIR_OR_GLOB ...] [--min-chunks 1024] [--size-floor-mb 2]

Exit codes: 0 = all PASS, 1 = FAIL.
Works on Windows (laptop, post-collect) and Linux boxes (/root/venv/bin/python).
"""
import argparse
import glob
import re
import struct
import sys
from pathlib import Path


def region_chunk_count(mca_path: Path) -> int:
    """Count populated chunks via the Anvil location table (no full parse)."""
    with open(mca_path, "rb") as f:
        header = f.read(4096)
    if len(header) < 4096:
        return -1  # truncated header = corrupt
    n = 0
    for i in range(1024):
        (entry,) = struct.unpack_from(">I", header, i * 4)
        if entry != 0:
            n += 1
    return n


def parse_tiles(path: Path):
    tiles = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.replace(",", " ").split()
        if len(parts) >= 2:
            tiles.append((int(parts[0]), int(parts[1])))
    return tiles


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--tiles", default=None,
                    help="tile list file (tx tz per line); enables presence check")
    ap.add_argument("--logs", nargs="*", default=[],
                    help="render log files/globs to scan")
    ap.add_argument("--min-chunks", type=int, default=1024)
    ap.add_argument("--size-floor-mb", type=float, default=2.0)
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    failures = []
    warnings = []

    # 1+2+5: region inventory + chunk counts + size floor
    mcas = sorted(out_dir.glob("r.*.mca"))
    print(f"[mca] {len(mcas)} region files in {out_dir}")
    if args.tiles:
        tiles = parse_tiles(Path(args.tiles))
        expected = {f"r.{tx}.{tz}.mca" for tx, tz in tiles}
        present = {m.name for m in mcas}
        missing = sorted(expected - present)
        if missing:
            failures.append(f"missing region files: {missing}")
        else:
            print(f"[mca] all {len(expected)} expected regions present")

    for m in mcas:
        sz_mb = m.stat().st_size / 1e6
        nch = region_chunk_count(m)
        line = f"  {m.name}: {nch} chunks, {sz_mb:.1f} MB"
        if nch < 0:
            failures.append(f"{m.name}: truncated/corrupt header")
        elif nch < args.min_chunks:
            failures.append(f"{m.name}: only {nch}/{args.min_chunks} chunks "
                            f"(silent per-chunk drops?)")
        if sz_mb < args.size_floor_mb:
            warnings.append(f"{m.name}: {sz_mb:.1f} MB below size floor")
        print(line)

    # 3: log scans
    log_paths = []
    for pat in args.logs:
        log_paths.extend(glob.glob(pat))
    bed_ok_seen = False
    for lp in sorted(set(log_paths)):
        try:
            text = Path(lp).read_text(errors="replace")
        except OSError as e:
            warnings.append(f"log unreadable: {lp}: {e}")
            continue
        n_err = len(re.findall(r"tile_error", text))
        if n_err:
            failures.append(f"{lp}: {n_err} tile_error line(s)")
        if "_BedCacheRefusal" in text:
            failures.append(f"{lp}: _BedCacheRefusal raised")
        if re.search(r"falling back to rebuild", text):
            failures.append(f"{lp}: bed cache fell back to rebuild")
        m = re.findall(r"\[chunk_writer\] (\d+) chunk\(s\) failed", text)
        if m:
            failures.append(f"{lp}: chunk_writer reported failures: {m}")
        if re.search(r"MemoryError|hard-killed|BrokenProcessPool", text):
            failures.append(f"{lp}: worker memory/process failure")
        if re.search(r"bed cache (MIGRATED|HIT)", text):
            bed_ok_seen = True
        n_warn = len(re.findall(r"\bWARN\b", text))
        if n_warn:
            warnings.append(f"{lp}: {n_warn} WARN line(s) (padded-fallback "
                            f"razor-seam risk -- inspect)")
    if log_paths and not bed_ok_seen:
        warnings.append("no 'bed cache MIGRATED/HIT' line found in any log "
                        "(OK only if no rivers in scope)")

    # 4: chunk_errors.log audit
    for cel in list(out_dir.glob("chunk_errors.log")) + list(out_dir.glob("**/chunk_errors.log")):
        content = cel.read_text(errors="replace").strip()
        if content:
            n = content.count("\n") + 1
            failures.append(f"{cel}: {n} suppressed chunk error line(s)")

    print()
    for w in warnings:
        print(f"WARN: {w}")
    for f_ in failures:
        print(f"FAIL: {f_}")
    if failures:
        print(f"\nRESULT: FAIL ({len(failures)} failure(s), {len(warnings)} warning(s))")
        return 1
    print(f"\nRESULT: PASS ({len(mcas)} regions, {len(warnings)} warning(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
