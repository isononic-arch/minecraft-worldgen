"""_drop_ocean_regions.py <world_region_dir> [--apply] — delete island region files
that have NOTHING above water (Y63), so the world's noise-ocean generator fills them
through seamlessly (no tile-stepped rendered-ocean boundary around islands).

Default = DRY RUN (reports only). Pass --apply to actually delete.
A region's source stays in islands/out/<island>/ so this is recoverable via re-install.
"""
import sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent; sys.path.insert(0, str(ROOT))
from islands.topdown_fast import read_chunk, top_rgb_for_chunk

SEA = 63


def region_has_land(mca: Path) -> bool:
    """True if ANY column in the region tops above sea level. Early-exits on the
    first land chunk (land regions are fast; only all-ocean regions scan fully)."""
    try:
        with open(mca, "rb") as f:
            # coarse pass first (every 3rd chunk) for a fast hit, then full confirm
            for stride in (3, 1):
                for lz in range(0, 32, stride):
                    for lx in range(0, 32, stride):
                        try:
                            ch = read_chunk(f, lx, lz)
                        except Exception:
                            ch = None
                        if ch is None:
                            continue
                        _, has, ty = top_rgb_for_chunk(ch)
                        if (has & (ty > SEA)).any():
                            return True
                if stride == 3:
                    # coarse found nothing; fall through to full pass to confirm
                    f.seek(0)
        return False
    except Exception:
        return True   # on any read error, keep the region (safe)


def drop_ocean_regions(reg: Path, apply: bool = True) -> int:
    """Delete every region in `reg` with nothing above water -> the world's
    noise-ocean generator fills them. Returns the number dropped. Importable so
    make_island_world can run it as the last install step."""
    reg = Path(reg)
    mcas = sorted(reg.glob("r.*.mca"))
    t0 = time.time(); land = ocean = 0; to_del = []
    for i, m in enumerate(mcas):
        if region_has_land(m):
            land += 1
        else:
            ocean += 1; to_del.append(m)
        if (i + 1) % 200 == 0:
            print(f"  drop-ocean scanned {i+1}/{len(mcas)} land={land} ocean={ocean} ({(time.time()-t0)/60:.1f}m)", flush=True)
    if apply:
        for m in to_del:
            m.unlink()
    print(f"drop-ocean: {land} land regions kept, {ocean} open-ocean dropped (noise-ocean fills) -> {reg}")
    return ocean


def main():
    reg = Path(sys.argv[1])
    apply = "--apply" in sys.argv
    mcas = sorted(reg.glob("r.*.mca"))
    t0 = time.time(); land = ocean = 0; to_del = []
    for i, m in enumerate(mcas):
        if region_has_land(m):
            land += 1
        else:
            ocean += 1; to_del.append(m)
        if (i + 1) % 100 == 0:
            print(f"  scanned {i+1}/{len(mcas)}  land={land} ocean={ocean}  ({(time.time()-t0)/60:.1f}m)", flush=True)
    print(f"\n{len(mcas)} regions: {land} have land, {ocean} are open-ocean (noise-ocean will fill these)")
    if apply:
        for m in to_del:
            m.unlink()
        print(f"DELETED {len(to_del)} open-ocean regions from {reg}")
    else:
        print(f"DRY RUN — pass --apply to delete the {ocean} open-ocean regions")


if __name__ == "__main__":
    main()
