"""river_flood_settle.py - S94 flood-settle (glass-platform) river water.

Per-cross-section spill + monotone running-min downstream.

Strategy
--------
1. Group river cells by their nearest carver-centerline (cskel) pixel. Each
   group is one perpendicular cross-section / latitude band.
2. For each cross-section, the contained flat level is:
       water = min( source level in that band,
                    MIN lateral bank = min sy_final over the non-river LAND
                    cells 4-adjacent to that band's river cells )
   This is flat-per-band (one scalar per band) and contained by construction
   (the chosen level can never exceed the lowest bank that borders the band -
   so the lowest bank spills and pulls the whole band down to it -> ZERO
   levees).
3. Order bands by cdist (LOWER dist = closer to ocean = downstream) and enforce
   monotone non-increasing toward the ocean via a running minimum walked from
   upstream (high dist) to downstream (low dist): a band can only be <= the
   band just upstream of it once we sweep, guaranteeing water only steps DOWN
   as it flows to the sea.

The bed (sy_final) is NEVER written. Off-river cells are -999.
"""

import numpy as np
from scipy import ndimage


def settle(source, bed, river, dist, skel, land=None):
    """Compute a contained, flat-per-cross-section, monotone water surface.

    Parameters
    ----------
    source : (H,W) int   - clean carver source/platform water level.
    bed    : (H,W) int   - sy_final surface (bed under river, banks beside).
                           READ ONLY - never modified.
    river  : (H,W) bool  - river cell mask.
    dist   : (H,W) float - dist-from-ocean (lower = downstream).
    skel   : (H,W) bool  - 1px carver centerline (cross-section grouping).
    land   : (H,W) bool  - OPTIONAL true-dry-land mask used for the lateral
                           containment bank. MUST exclude LAKE cells: a lake is
                           water, not a bank, and its deep bed would otherwise
                           drag an adjacent river's level down to the lake floor
                           (S94 bug: (62,61) drained to -15 depth). Defaults to
                           ~river (back-compat) but callers with lakes MUST pass
                           ~river & ~lake.

    Returns
    -------
    new_water : (H,W) int, water level on river cells, -999 elsewhere.
    """
    H, W = bed.shape
    bed = bed.astype(np.int64)
    source = source.astype(np.int64)
    river = river.astype(bool)
    land = (~river) if land is None else land.astype(bool)

    # --- 1. nearest-skel label for every cell (cross-section id) ---------
    # EDT return_indices gives, for each pixel, the coords of the nearest
    # skel pixel. That (iy,ix) flattened is a stable per-band label.
    inv = ~skel.astype(bool)
    _, (iy, ix) = ndimage.distance_transform_edt(inv, return_indices=True)
    label = iy.astype(np.int64) * W + ix.astype(np.int64)

    # --- 2. per-band source level (bands are already flat; use the min as
    #        the representative source so we never sit above the carver) ----
    river_idx = np.where(river.ravel())[0]
    lab_r = label.ravel()[river_idx]
    src_r = source.ravel()[river_idx]

    bands, inv_band = np.unique(lab_r, return_inverse=True)
    nb = len(bands)

    # representative source per band = min source in band (flat anyway)
    band_src = np.full(nb, np.iinfo(np.int64).max)
    np.minimum.at(band_src, inv_band, src_r)

    # --- 3. lateral spill: min adjacent LAND bank per band ---------------
    # For each river cell, look at its 4-neighbours; any neighbour that is
    # LAND contributes its bed height as a candidate spill level. The band's
    # spill level is the MIN over all such bank cells touching the band.
    sy_pad = np.pad(bed, 1, constant_values=np.iinfo(np.int64).max)
    # use the TRUE-LAND mask (excludes lakes) for the bank — a lake neighbour
    # is water, not a containing bank, so it must NOT pull the river down.
    land_pad = np.pad(land, 1, constant_values=False)

    # min adjacent land height for each river cell (9999 = no land neighbour)
    NOLAND = np.iinfo(np.int64).max
    min_land_adj = np.full((H, W), NOLAND, np.int64)
    for dz, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        nb_sy = sy_pad[1 + dz:1 + dz + H, 1 + dx:1 + dx + W]
        nb_is_land = land_pad[1 + dz:1 + dz + H, 1 + dx:1 + dx + W]
        cand = np.where(nb_is_land, nb_sy, NOLAND)
        min_land_adj = np.minimum(min_land_adj, cand)

    min_land_r = min_land_adj.ravel()[river_idx]
    band_bank = np.full(nb, NOLAND)
    np.minimum.at(band_bank, inv_band, min_land_r)

    # contained flat level per band = min(source, lowest bordering bank).
    # Where a band has no land neighbour (fully interior of a wide river) the
    # bank term is NOLAND and the source dominates - correct (no spill here).
    band_level = np.minimum(band_src, band_bank)

    # --- 4. monotone non-increasing downstream (running-min toward ocean) -
    # Per-band cdist = mean dist of band's river cells (stable ordering key).
    dist_r = dist.ravel()[river_idx].astype(np.float64)
    band_dist_sum = np.zeros(nb)
    band_cnt = np.zeros(nb)
    np.add.at(band_dist_sum, inv_band, dist_r)
    np.add.at(band_cnt, inv_band, 1.0)
    band_dist = band_dist_sum / np.maximum(band_cnt, 1.0)

    # Sort upstream (high dist) -> downstream (low dist); running min so the
    # level can only stay equal or drop as we move toward the ocean.
    order = np.argsort(-band_dist)  # descending dist = upstream first
    lvl_sorted = band_level[order]
    run = np.minimum.accumulate(lvl_sorted)
    band_level_mono = np.empty_like(band_level)
    band_level_mono[order] = run

    # --- 4b. per-cell monotone polish on the band-adjacency graph -------
    # The running-min above uses each band's MEAN cdist to order bands. At a
    # few band seams the mean-dist order disagrees with the local per-cell
    # cdist gradient, leaving a handful of 1-block reversals where a cell's
    # truly-downstream 4-neighbour (lower per-cell dist) sits in a band that
    # is 1 higher. Resolve by relaxing on the band graph using PER-CELL dist
    # to orient each band->band edge: if band B is downstream of band A
    # (any river cell of A 4-touches a cell of B with strictly lower dist),
    # then level[A] must be >= level[B]; clamp the upstream band DOWN. Iterate
    # to a fixed point (the graph is a thin chain so this converges fast).
    # Keeps bands perfectly flat (whole-band clamp).
    band_of_cell = inv_band  # band index per river cell (in river_idx order)
    cell_band = np.full(H * W, -1, np.int64)
    cell_band[river_idx] = band_of_cell
    cell_band = cell_band.reshape(H, W)

    # collect directed (upstream_band -> downstream_band) edges
    bp = np.pad(cell_band, 1, constant_values=-1)
    dp = np.pad(dist.astype(np.float64), 1, constant_values=np.inf)
    up_list = []
    dn_list = []
    here_band = cell_band
    here_dist = dist.astype(np.float64)
    for dz, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        nb_band = bp[1 + dz:1 + dz + H, 1 + dx:1 + dx + W]
        nb_dist = dp[1 + dz:1 + dz + H, 1 + dx:1 + dx + W]
        # neighbour is a different band, both are river, neighbour is
        # strictly downstream (lower per-cell dist) -> edge here->nb
        m = (here_band >= 0) & (nb_band >= 0) & (nb_band != here_band) \
            & (nb_dist < here_dist)
        up_list.append(here_band[m])
        dn_list.append(nb_band[m])
    if up_list:
        up_e = np.concatenate(up_list)
        dn_e = np.concatenate(dn_list)
        # dedupe edges
        if up_e.size:
            key = up_e.astype(np.int64) * nb + dn_e.astype(np.int64)
            key = np.unique(key)
            up_e = key // nb
            dn_e = key % nb
        for _ in range(64):
            # upstream level must be >= downstream level
            need = band_level_mono[up_e] < band_level_mono[dn_e]
            if not need.any():
                break
            # pull the offending DOWNSTREAM band down to the upstream level
            # (preferring to drop, never raise, to stay contained)
            np.minimum.at(band_level_mono, dn_e,
                          band_level_mono[up_e])

    # --- 5. scatter back to grid ----------------------------------------
    new_water = np.full((H, W), -999, np.int64)
    new_water.ravel()[river_idx] = band_level_mono[inv_band]

    return new_water.astype(np.int64)
