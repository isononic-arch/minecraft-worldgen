"""water_cleanup.py — S94 overspill TRIM (user-specified).

The seam-clean GLOBAL water level is correct except that, being global, it sits a
block or two ABOVE the local terrain in spots -> the TOP water layer overspills a
lower bank. The fix is NOT to delete the whole water column (that drains the
trough dry) and NOT to raise land (walls). It is to REMOVE ONLY THE EXPOSED TOP
LAYER(S): lower the water surface down to the lowest surrounding wall so it stops
spilling, leaving the rest of the water sitting in the trough.

Mechanically: a surface-water cell overspills if its water level is higher than
the lowest of its 4 neighbours' tops (water surface if the neighbour is water,
else terrain top). Lower it to that wall (never below its own bed). Iterate so the
drop propagates inward across a flooded sheet until the surface is contained. Pure
terrain/water driven -> the same result on both sides of a seam.

cleanup_spill_rows(water_y, surface_y, river_mask) -> (new_water_y, changed_mask)
"""

import numpy as np

SEA_Y = 63


def cleanup_spill_rows(water_y, surface_y, river_mask, max_iters=400):
    wy = np.asarray(water_y).astype(np.int32).copy()
    sy = np.asarray(surface_y).astype(np.int32)
    river = np.asarray(river_mask).astype(bool)
    orig = wy.copy()
    INF = np.int32(1 << 20)
    for _ in range(int(max_iters)):
        sw = river & (wy > SEA_Y) & (wy > sy)          # cells with surface water
        if not sw.any():
            break
        top = np.where(sw, wy, sy).astype(np.int32)    # top of each cell (water or land)
        support = np.full(wy.shape, INF, np.int32)      # lowest surrounding wall
        for dz, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            support = np.minimum(support, np.roll(np.roll(top, dz, 0), dx, 1))
        # overspill = surface water sitting above its lowest surrounding wall
        spill = sw & (wy > support)
        if not spill.any():
            break
        # lower the surface to the wall (the contained level); never below the bed
        wy = np.where(spill, np.maximum(support, sy), wy)
    changed = wy != orig
    return wy, changed
