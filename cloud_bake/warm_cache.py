"""
cloud_bake/warm_cache.py — Pre-warm the bed cache on a render box.

Runs `_ensure_caches` in a single process to build + save the bed
cache pickle (~5 min, peak ~3-4 GB memory). After this completes,
subsequent worker processes load the cache from disk in ~5-10 sec
with ~1.5 GB peak memory — enabling safe multi-worker (--threads 8+)
parallelism on memory-constrained boxes.

Usage (on a Hetzner/DO worker box):
    cd /root/minecraft-worldgen
    source /root/venv/bin/activate
    python3 cloud_bake/warm_cache.py

Or via SSH from your laptop:
    ssh root@<box-ip> "cd /root/minecraft-worldgen && \\
        source /root/venv/bin/activate && \\
        python3 cloud_bake/warm_cache.py"

The cache file is written to:
    <masks_dir>/_bed_cache_v17.pkl

Invalidation key includes md5(hydro_region.png) + md5(_ensure_caches
source) + repr(all tunables). If you re-paint the river overlay or
change carve parameters, the next run rebuilds automatically.

Set env VANDIR_NO_BED_CACHE=1 to bypass caching entirely (debug).
"""
from __future__ import annotations
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from core.hydro_region_overlay import _ensure_caches


def main() -> int:
    masks_dir = REPO / "masks"
    hr_path = masks_dir / "hydro_region.png"
    if not hr_path.exists():
        print(f"ERROR: hydro_region.png missing at {hr_path}")
        return 1

    print(f"Warming bed cache from: {hr_path}")
    print(f"Cache will be saved to: {masks_dir}/_bed_cache_v17.pkl")
    print()
    t0 = time.time()
    _ensure_caches(hr_path)
    elapsed = time.time() - t0
    print()
    print(f"Done in {elapsed:.1f}s.")
    cache_path = masks_dir / "_bed_cache_v17.pkl"
    if cache_path.exists():
        size_mb = cache_path.stat().st_size / (1024 * 1024)
        print(f"Cache file: {cache_path} ({size_mb:.0f} MB)")
        print("Subsequent worker processes will load from this in ~5-10 sec.")
        return 0
    else:
        print("WARNING: cache file was not written (check logs above for errors)")
        return 1


if __name__ == "__main__":
    sys.exit(main())
