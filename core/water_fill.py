"""water_fill.py — S94c "wet banks" fill (Agent-B realism fix).

After bank-taper has reshaped the valley, the water mask is still the NARROW
carved channel, so the tapered bench — terrain the taper lowered BELOW the
channel water surface — renders DRY. Real rivers fill up to their banks. This
pass floods the water OUTWARD from the existing channel: any dry land cell that
sits below an adjacent water cell's level is filled to that level, propagating
outward, BOUNDED where the terrain rises to/above the level (= the true bank).
Flat per band (the neighbour's level is copied unchanged). No terrain is raised
(no walls) — only water is added on cells that are already below the surface.

PRECONDITION: the water level must be CONTAINED (<= the real bank). If the level
over-shoots the terrain, this fill will pour over the bank into the valley. The
global bake's level-containment (rebuild_river_wl bank measured at the channel
edge) is what keeps this bounded.

fill_to_banks(water_y, surface_y, lake_mask) -> (new_water_y, filled_mask)
"""

import numpy as np

SEA_Y = 63


def fill_to_banks(water_y, surface_y, lake_mask, max_iters=64):
    wy = np.asarray(water_y).astype(np.int32).copy()
    sy = np.asarray(surface_y).astype(np.int32)
    lake = np.asarray(lake_mask).astype(bool)
    orig = wy.copy()
    for _ in range(int(max_iters)):
        # highest adjacent WATER-surface level (water cells = wy > SEA)
        wlev = np.where(wy > SEA_Y, wy, SEA_Y).astype(np.int32)
        nbr = np.full(wy.shape, SEA_Y, np.int32)
        for dz, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nbr = np.maximum(nbr, np.roll(np.roll(wlev, dz, 0), dx, 1))
        # fill a cell when: currently DRY (no surface water), not a lake, its
        # terrain sits ABOVE sea (not ocean) but BELOW an adjacent water level
        # -> the water would stand here. Assign that neighbour's (band) level.
        fill = ((wy <= SEA_Y) & ~lake
                & (sy >= SEA_Y) & (nbr > SEA_Y) & (sy < nbr))
        if not fill.any():
            break
        wy = np.where(fill, nbr, wy)
    filled = wy != orig
    return wy, filled
