"""water_cleanup.py — S94 spill-row cleanup (user-specified).

The seam-clean GLOBAL water level is correct except that, being global, it sits a
block or two ABOVE the local terrain in some spots -> water perches/spills over a
lower bank. Rather than try to CONTAIN it (which forces either a wall or a seam),
we simply DELETE the spilling water: scan widthwise rows of surface water; if a
row's EDGE block is not backed by a solid bank but instead exposed to AIR (the
land just beyond it is BELOW the water surface), replace that whole widthwise row
of water with air. The perching slices vanish; the contained river stays.

cleanup_spill_rows(water_y, surface_y, river_mask) -> (new_water_y, deleted_mask)
  water_y    : (H,W) int  river water-surface level, -999 off-river (padded ok)
  surface_y  : (H,W) int  terrain/bed top
  river_mask : (H,W) bool river-footprint cells
Both scan axes are processed so either flow orientation is covered; a run along
the flow ends on more water (not air) so it is never deleted mid-river.
"""

import numpy as np

SEA_Y = 63


def cleanup_spill_rows(water_y, surface_y, river_mask):
    wy = np.asarray(water_y).astype(np.int32).copy()
    sy = np.asarray(surface_y).astype(np.int32)
    river = np.asarray(river_mask).astype(bool)
    H, W = wy.shape
    # a cell shows SURFACE water if the river water sits above its own bed
    sw = river & (wy > SEA_Y) & (wy > sy)
    if not sw.any():
        return wy, np.zeros((H, W), bool)
    delete = np.zeros((H, W), bool)

    def scan(axis):
        n_out, n_in = (H, W) if axis == 0 else (W, H)
        for o in range(n_out):
            i = 0
            row_sw = sw[o, :] if axis == 0 else sw[:, o]
            row_sy = sy[o, :] if axis == 0 else sy[:, o]
            row_wy = wy[o, :] if axis == 0 else wy[:, o]
            while i < n_in:
                if not row_sw[i]:
                    i += 1
                    continue
                i0 = i
                while i < n_in and row_sw[i]:
                    i += 1
                i1 = i - 1
                # is an END exposed? = the cell just beyond the run is NOT water
                # and its terrain top is BELOW this end's water surface (air there)
                exposed = False
                for ie, de in ((i0, -1), (i1, +1)):
                    jb = ie + de
                    if 0 <= jb < n_in and (not row_sw[jb]) and row_sy[jb] < row_wy[ie]:
                        exposed = True
                        break
                if exposed:
                    if axis == 0:
                        delete[o, i0:i1 + 1] = True
                    else:
                        delete[i0:i1 + 1, o] = True

    scan(0)
    scan(1)
    wy[delete] = np.int32(-999)
    return wy, delete
