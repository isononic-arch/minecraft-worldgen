"""
S94 bank-taper, variant `rampgauss`.

Turns the abrupt TROUGH WALLS around a flood-settled river into a natural
gaussian-tapered valley. Only the LAND bank cells rising from the water are
reshaped (lowered); the bed under water and the emergent-rock cells inside the
river footprint are never touched, and terrain is only ever lowered, never raised.

taper(sy, rwy, rm) -> new_sy (int, same shape)

Variant rampgauss
-----------------
Build a target ramp surface:
    d = euclidean distance from nearest WET water cell
    ramp = W                       at the 1-cell perimeter (d ~ 1)
    ramp = W + 1                   at the 2nd ring (d ~ 2)
    ramp = (W + 1) + GRADE*(d - 2) for d >= 3   (GRADE ~ 0.34 = 1 block / 3 out)
where W is the level of the NEAREST wet water body (per-cell, terrace-safe).

new_sy = min(original sy, ramp) on bank cells (ONLY lower). Then gaussian_filter
the bank zone (sigma ~ 2) and re-clamp to [shore_floor, original]. Re-assert the
W (perimeter) and W+1 (ring1) rings EXACTLY after the blur so shore structure is
crisp.
"""

import numpy as np
from scipy import ndimage

# ---- tunables -------------------------------------------------------------
GRADE = 0.34          # blocks of rise per block out, beyond the W+1 ring
SIGMA = 2.0           # gaussian blur on the bank zone
# taper reach scales with wall height: distance needed for the ramp to reach the
# natural terrain = (wall_above_W - 1) / GRADE + 2, capped for safety.
MAX_REACH = 48        # hard cap on taper reach (cells)
SEA_Y = 63
# S94 step-limit (user (84,60) walk): the stepped ramp leaves >=2 block risers on
# the bank that expose the STONE basement (only 1 dirt block under grass = stone
# pickets). Cap every bank step to MAX_BANK_STEP so each riser shows the single
# dirt block, not stone. Propagated outward from the water; only-lower.
MAX_BANK_STEP = 1
# step-limit domain: ALL land within STEP_MIN_REACH of the water (not just the
# height-scaled bank zone), so the flat-shore terrain bumps that also expose
# stone risers get flattened, not only the trough wall.
STEP_MIN_REACH = 8
# ---------------------------------------------------------------------------


def taper(sy, rwy, rm):
    sy = np.asarray(sy)
    rwy = np.asarray(rwy)
    rm = np.asarray(rm)
    orig = sy.astype(np.int32)

    river_fp = (rm == 1) | (rm == 2)
    lake = (rm == 3)
    # wet water cells = river footprint, watered above sea, water over the bed
    wet = river_fp & (rwy > SEA_Y) & (rwy > orig)
    # S94 walk (84,60): DRY river-footprint cells -- river-tagged but with NO
    # water level assigned (rwy == -999 sentinel) -- are mis-tagged marginal
    # tendrils that stand proud as thin "stonehenge" pickets along the bank.
    # They were INVISIBLE to both passes: this taper excluded them with the
    # channel (land = ~river_fp), and despike skips them (needs rwy > SEA).
    # chunk_writer already renders them as LAND (grass on top), so fold them into
    # the bank domain here and let the ramp + step-limit lower them to blend.
    # WATERED channel + emergent-ROCK cells (rwy > SEA) stay protected so the
    # real channel and the user-liked rocky outcrops are untouched.
    dry_river = river_fp & ~(rwy > SEA_Y)
    protect = (river_fp & (rwy > SEA_Y)) | lake
    # bank/wall cells = land (NOT watered/emergent river, NOT lake) PLUS dry river
    land = (~river_fp & ~lake) | dry_river

    new_sy = orig.copy()
    if not wet.any():
        return new_sy.astype(orig.dtype)

    # per-cell distance to nearest wet cell + index of that nearest wet cell
    dist, (iy, ix) = ndimage.distance_transform_edt(~wet, return_indices=True)
    W = rwy[iy, ix].astype(np.int32)          # nearest-water level per cell (terrace-safe)

    # ----- terrace floor ---------------------------------------------------
    # The river has MULTIPLE flat terraces at different W stepping down. A bank
    # cell may ramp toward its NEAREST water W, but it must NEVER be carved below
    # the HIGHEST water level of any wet body close enough to spill into. That
    # floor (the max nearby water level) guarantees we never drain a higher
    # terrace down into a lower one nor cut a higher terrace's containment wall.
    terr_floor = np.zeros(orig.shape, dtype=np.int32)
    for lv in np.unique(rwy[wet]):
        d_lv = ndimage.distance_transform_edt(~(wet & (rwy == lv)))
        near = d_lv <= MAX_REACH
        terr_floor = np.where(near, np.maximum(terr_floor, lv), terr_floor)

    # ----- taper reach scales with local wall height -----------------------
    # wall_above_W (>=0) for every land cell; reach = how far the ramp must run
    # to climb from W+1 up to that wall height at GRADE.
    wall_above = np.maximum(orig - W, 0)
    reach = np.ceil((wall_above - 1) / GRADE) + 2.0
    reach = np.clip(reach, 2.0, MAX_REACH)
    # a cell is in the bank zone if it is land AND within its own height-scaled reach
    bank = land & (dist > 0) & (dist <= reach)

    # ----- build the target ramp surface (the shore floor) -----------------
    # ring membership by integer distance band
    ring_perim = (dist > 0) & (dist <= 1.5)    # ~1 cell from water
    ring1 = (dist > 1.5) & (dist <= 2.5)       # ~2 cells from water

    # ramp value as float for everyone (only meaningful where used)
    ramp = (W + 1).astype(np.float64) + GRADE * (dist - 2.0)
    ramp = np.where(ring_perim, W.astype(np.float64), ramp)
    ramp = np.where(ring1, (W + 1).astype(np.float64), ramp)
    ramp_i = np.floor(ramp + 1e-9).astype(np.int32)

    # shore floor: the minimum a bank cell may ever be lowered to. This is the
    # ramp itself, but never below the nearest water W AND never below the highest
    # nearby water level (terrace safety — can't carve into / drain a higher pool).
    shore_floor = np.maximum.reduce([ramp_i, W, terr_floor])

    # ----- only LOWER toward the ramp on bank cells ------------------------
    target = np.minimum(orig, ramp_i)
    new_sy = np.where(bank, target, orig)
    # never below the shore floor (don't overshoot below W / drain a terrace)
    new_sy = np.where(bank, np.maximum(new_sy, shore_floor), new_sy)

    # ----- gaussian-smooth the bank zone -----------------------------------
    # Blur a float field but only let the result write into bank cells, and
    # blend using a smoothed weight so the bank<->natural boundary is seamless.
    field = new_sy.astype(np.float64)
    blurred = ndimage.gaussian_filter(field, sigma=SIGMA)
    # smooth the bank membership to a [0,1] weight so the blur fades at the
    # outer edge of the taper rather than producing a hard seam
    w = ndimage.gaussian_filter(bank.astype(np.float64), sigma=SIGMA)
    smoothed = (1.0 - w) * field + w * blurred
    new_f = np.where(bank, smoothed, field)

    # re-clamp: never above original (only lower), never below shore floor
    new_f = np.minimum(new_f, orig.astype(np.float64))
    new_f = np.where(bank, np.maximum(new_f, shore_floor.astype(np.float64)), new_f)
    new_sy = np.rint(new_f).astype(np.int32)
    # integer re-clamp after rounding
    new_sy = np.minimum(new_sy, orig)
    new_sy = np.where(bank, np.maximum(new_sy, shore_floor), new_sy)

    # ----- re-assert the crisp shore structure -----------------------------
    # perimeter LAND flush to W; 2nd ring W+1. Only where this LOWERS (never raise)
    # and never below the terrace floor (a perimeter cell of a LOW pool that also
    # borders a HIGH pool must stay >= the high pool's level — containment wins).
    perim_land = ring_perim & land
    ring1_land = ring1 & land
    perim_tgt = np.minimum(orig, np.maximum(W, terr_floor))
    ring1_tgt = np.minimum(orig, np.maximum(W + 1, terr_floor))
    new_sy = np.where(perim_land, perim_tgt, new_sy)
    new_sy = np.where(ring1_land, ring1_tgt, new_sy)

    # only-lower + don't touch watered/emergent river or lake interior (apply
    # BEFORE the step-limit so it operates on the TRUE final bank surface, not
    # the ramp). DRY river cells are intentionally NOT protected -> tapered.
    new_sy = np.minimum(new_sy, orig)
    new_sy = np.where(protect, orig, new_sy)

    # ----- S94 (A): limit bank steps to MAX_BANK_STEP -----------------------
    # Lower any bank cell sitting more than MAX_BANK_STEP above its lowest
    # INWARD neighbour (a bank cell, or the water level W at a wet neighbour),
    # propagated outward from the water until stable, so every riser is
    # <= MAX_BANK_STEP and shows the dirt veneer, never the stone basement.
    # Containment floor is ONLY W + terrace floor (NOT the ramp): the bank may
    # legitimately sit below the ramp (orig terrain was lower), and we must not
    # raise it. Only-lower; never below W (spill) / terr_floor (drain a terrace).
    # On a multi-terrace tile terr_floor keeps the legitimate terrace risers.
    # containment floor: a bank cell only needs to stay above the water it is
    # DIRECTLY 4-adjacent to (so it contains that pool). Cells behind the
    # perimeter have no direct water contact (the perimeter at W blocks flow),
    # so they may be lowered freely by the step-limit. Using the broad
    # terr_floor (max water within MAX_REACH) was far too restrictive — it
    # pinned every cell near any high terrace and blocked the whole step-limit.
    INF = np.int32(1 << 20)
    wet_lvl = np.where(wet, rwy.astype(np.int32), -INF)
    cont_floor = np.full(orig.shape, np.int32(SEA_Y), np.int32)
    for dz, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        cont_floor = np.maximum(cont_floor, np.roll(np.roll(wet_lvl, dz, 0), dx, 1))
    # step-limit domain = ALL land within max(height-scaled reach, STEP_MIN_REACH)
    # of the water, so flat-shore bumps are flattened too (not just the wall).
    step_zone = land & (dist > 0) & (dist <= np.maximum(reach, float(STEP_MIN_REACH)))
    INF = np.int32(1 << 20)
    step = np.int32(MAX_BANK_STEP)
    for _ in range(int(MAX_REACH) + 2):
        # low-anchor height per cell: step-zone uses its own Y, wet uses W, else INF
        ref = np.where(step_zone, new_sy, np.where(wet, W, INF)).astype(np.int32)
        minnb = np.full(orig.shape, INF, np.int32)
        for dz, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            minnb = np.minimum(minnb, np.roll(np.roll(ref, dz, 0), dx, 1))
        cap = minnb + step
        over = step_zone & (minnb < INF) & (new_sy > cap)
        if not over.any():
            break
        new_sy = np.where(
            over, np.maximum(np.minimum(new_sy, cap), cont_floor), new_sy)

    # final safety: never raise anywhere, never touch watered/emergent river or
    # lake interior (dry river cells stay tapered).
    new_sy = np.minimum(new_sy, orig)
    new_sy = np.where(protect, orig, new_sy)

    return new_sy.astype(orig.dtype)


def despike_emergent_rock(sy, rwy, rm, tall_thresh=2, water_floor_off=1):
    """S94: lower THIN (1-wide) emergent-rock columns -- the 'stonehenge'
    spikes the user walked at (84,60) -- to blend into their surroundings,
    while leaving BROAD outcrops untouched. The bank-taper skips river-footprint
    cells (correctly), so isolated tall rock pixels there are never smoothed.
    Here we lower only the emergent cells that form a THIN tall component (no
    2-wide core survives erosion); broad clusters (a real outcrop) are kept.
    Only modifies emergent river-footprint cells; only lowers."""
    sy = np.asarray(sy).copy()
    rwy = np.asarray(rwy); rm = np.asarray(rm)
    river = (rm == 1) | (rm == 2)
    emergent = river & (rwy > SEA_Y) & (sy >= rwy)
    if not emergent.any():
        return sy
    tall = emergent & (sy >= rwy + tall_thresh)
    if not tall.any():
        return sy
    lab, n = ndimage.label(tall)
    struct = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], dtype=bool)
    thin = np.zeros(sy.shape, dtype=bool)
    for c in range(1, n + 1):
        comp = lab == c
        if not ndimage.binary_erosion(comp, struct).any():  # no 2-wide core => thin
            thin |= comp
    if not thin.any():
        return sy
    floor = (rwy - water_floor_off).astype(np.int64)
    syi = sy.astype(np.int64)
    INF = np.int64(1 << 30)
    for _ in range(8):
        mn = np.full(sy.shape, INF, np.int64)
        for dz, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nb = np.roll(np.roll(syi, dz, 0), dx, 1)
            nbt = np.roll(np.roll(thin, dz, 0), dx, 1)
            mn = np.where(~nbt, np.minimum(mn, nb), mn)
        tgt = thin & (syi > mn) & (mn < INF)
        if not tgt.any():
            break
        syi = np.where(tgt, np.maximum(mn, floor), syi)
    return syi.astype(sy.dtype)
