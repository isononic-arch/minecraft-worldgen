"""water_hug.py — S94 spill-containment by RAISING land (not lowering water).

The global river water-level bake gives a seam-CLEAN water surface, but because
it is computed on pre-carve terrain it can sit a few blocks ABOVE the local
carved bank -> water perches / spills over dry land. Lowering the water to
contain it re-broke the seam (per-tile divergence). Instead we keep the
seam-clean water level untouched and RAISE the land that leaks: any LAND cell
lower than the water that the water would spill onto is filled UP to the water
level, propagated outward over the connected low "dip" until it meets natural
terrain already at (or above) the water level. The fill is flush with the water
on one side and flush with the natural bank on the other -> a raised terrace
border, NOT a vertical wall. Bounded reach so it never fills open valleys.

hug(sy, wy, rm, max_reach=24) -> (new_sy, raised_mask)
  sy  : (H,W) int  surface_y (terrain top), padded halo. NOT modified in place.
  wy  : (H,W) int  river water level (the seam-clean override), -999 off-river.
  rm  : (H,W) uint8 river_meta (1/2 river, 3 lake).
  returns the raised surface_y and a bool mask of cells that were raised (for
  the caller to repaint surface blocks + refill geology).

Seam note: the RAISE TARGET is the water level wy (seam-clean), so every raised
cell ends at a seam-consistent height. Only *which* cells get raised depends on
sy (marginally divergent in the halo), a far smaller seam risk than moving the
water itself — and it touches terrain, not the water the user walks.
"""

import numpy as np

SEA_Y = 63


def hug(sy, wy, rm, max_reach=24):
    sy = np.asarray(sy).astype(np.int32)
    wy = np.asarray(wy).astype(np.int32)
    rm = np.asarray(rm)
    out = sy.copy()

    river = (rm == 1) | (rm == 2)
    lake = (rm == 3)
    wet = river & (wy > SEA_Y) & (wy > sy)        # cells holding water above bed
    # FILLABLE = any DRY (non-wet) non-lake cell: dry land AND emergent river-bed
    # cells that sit BELOW an adjacent water level (a spill the water leaks over).
    # Emergent ROCKS (dry cells ABOVE the water) are never below an adjacent water
    # level, so the propagation never reaches them -> the user-liked outcrops stay.
    land = ~wet & ~lake
    if not wet.any():
        return out, np.zeros(sy.shape, dtype=bool)

    NEG = np.int32(-(1 << 20))
    # target water level reaching each cell. Wet cells hold their own water level.
    target = np.where(wet, wy, NEG).astype(np.int32)

    # Flood the water level outward over LAND dips (cells whose terrain is below
    # the incoming water level). A cell stops the spread once its terrain is >=
    # the incoming level (a natural bank that already contains the water).
    for _ in range(int(max_reach)):
        # only wet cells and already-claimed dips propagate
        active = np.where(wet | (land & (target > NEG) & (sy < target)),
                          target, NEG)
        nb = np.full(sy.shape, NEG, np.int32)
        for dz, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nb = np.maximum(nb, np.roll(np.roll(active, dz, 0), dx, 1))
        # a land cell claims the incoming level only if it is a DIP below it and
        # the new level beats what it already had
        upd = land & (nb > target) & (sy < nb)
        if not upd.any():
            break
        target = np.where(upd, nb, target)

    raised = land & (target > SEA_Y) & (sy < target)
    out[raised] = target[raised]
    return out, raised
