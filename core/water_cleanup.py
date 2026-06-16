"""water_cleanup.py — S94 overspill TRIM (user-specified).

The seam-clean GLOBAL water level is correct except that, being global, it sits a
block or two ABOVE the local terrain in spots -> the TOP water layer overspills a
lower LAND bank. The fix is to remove ONLY that overspill: lower a surface-water
cell that sits above an adjacent LAND top down to that land, so it stops spilling
over the bank. Everything else (the flood body) is kept.

CRITICAL: support is the min over the 4 neighbours that are LAND (terrain top),
NEVER water neighbours. Including water neighbours makes a tiny edge-perch CASCADE
inward and drain the whole flooded sheet to its lowest outflow (13,80 went
10081 -> 2943 water cells that way). Land-only => only the cells actually perching
over a lower bank are trimmed; the flood body stays. Pure terrain/level driven, no
cross-water propagation -> identical on both sides of a seam.

This is NOT delete-the-column (drains the trough dry) and NOT raise-land (walls).

cleanup_spill_rows(water_y, surface_y, river_mask) -> (new_water_y, changed_mask)
"""

import numpy as np

SEA_Y = 63


def cleanup_spill_rows(water_y, surface_y, river_mask, max_iters=12):
    wy = np.asarray(water_y).astype(np.int32).copy()
    sy = np.asarray(surface_y).astype(np.int32)
    river = np.asarray(river_mask).astype(bool)
    orig = wy.copy()
    INF = np.int32(1 << 20)
    for _ in range(int(max_iters)):
        sw = river & (wy > SEA_Y) & (wy > sy)          # cells with surface water
        if not sw.any():
            break
        # lowest adjacent LAND top (terrain). water neighbours contribute INF so
        # they never pull the level down -> no cross-water cascade.
        land_top = np.where(sw, INF, sy).astype(np.int32)
        support = np.full(wy.shape, INF, np.int32)
        for dz, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            support = np.minimum(support, np.roll(np.roll(land_top, dz, 0), dx, 1))
        # overspill = surface water sitting above an adjacent lower LAND bank
        spill = sw & (wy > support)
        if not spill.any():
            break
        # lower to the bank (the contained level); never below the cell's own bed
        wy = np.where(spill, np.maximum(support, sy), wy)
    changed = wy != orig
    return wy, changed
