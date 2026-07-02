"""_repaint_baby_continent.py — SANDBOX biome repaint for the southern baby
continent, plus a GLOBAL dry-woodland-maquis -> semi-arid-shrubland reband.
Works on a downsampled COPY of masks/override.tif (never touches the live mask).
Renders before/after biome views for catbox review.

Westerly model (W->E): WEST = windward/WET, EAST = leeward/DRY.
 - KEEP untouched: sand-dune-desert bowl (170) + desert-steppe ring (190),
   mountains/highlands (boreal/snowy/alpine/dry-pine/karst), fen, coastal heath.
 - DESERT OASIS (tropical patches enclosed by the bowl): kept tropical (P0 fix —
   bowl_fill guard on every pass + a decisive-last restore).
 - WEST windward coast: de-tropicalize rainforest-coast(70)/lush(160) -> warm-
   temperate Atlantic gradient by ABSOLUTE coast distance (shore<250blk -> temp-
   rainforest 20; 250-800 -> mixed 120; >=800 -> deciduous 60). Gate by nearest-
   ocean facing (NOT a latitude row-cut) so the south Kostati fringe isn't claimed.
 - SOUTH/SE tropical fringe (Kostati bridge): tapered to a ~800-block coastal band;
   deeper-inland tropical retargeted to the nearest dry zone (semi-arid).
 - EAST: DRY_WOODLAND_MAQUIS(210) -> SEMI_ARID_SHRUBLAND(200) [GLOBAL]; savanna(90)
   + continental-steppe(130) kept (palette work is separate).
 - Two mainland islands bordering Kostati -> fully tropical (separate components).
"""
import sys
from pathlib import Path
import numpy as np
from scipy import ndimage as ndi
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core.biome_assignment import OVERRIDE_BIOME_MAP
from tools.world_biome_map import BIOME_COLORS

OV8 = np.load(ROOT / "scratch_ov8.npy")
H8 = np.load(ROOT / "scratch_h8.npy")
DS = OV8.shape[0]
OCEAN = (16, 26, 44)

# zone codes
RFC, LRFC, TJF, MANGR = 70, 160, 220, 230
TREMP, ETC, MIXED, TDEC = 20, 115, 120, 60
MAQUIS, SARID, SAVANNA, CSTEPPE = 210, 200, 90, 130
BOWL, DSTEP = 170, 190
TROP = [RFC, LRFC, TJF, MANGR]   # 70,160,220,230


def lut():
    L = np.zeros((256, 3), np.uint8)
    for z, n in OVERRIDE_BIOME_MAP.items():
        L[z] = BIOME_COLORS.get(n, (150, 150, 150))
    return L


def baby_mask():
    land = OV8 > 0
    lab, n = ndi.label(land)
    sizes = np.bincount(lab.ravel())
    cand = [i for i in range(1, n + 1) if sizes[i] > 1_000_000]
    def cen(i):
        ys, xs = np.where(lab == i); return xs.mean(), ys.mean()
    baby = min(cand, key=lambda i: cen(i)[0] + (DS - cen(i)[1]))
    return lab == baby


def repaint():
    ov = OV8.copy()
    m = baby_mask()
    row = np.arange(DS, dtype=np.int32)[:, None]
    col = np.arange(DS, dtype=np.int32)[None, :]
    by, bx = np.where(m & (ov == BOWL))
    bowl_x, bowl_z = bx.mean(), by.mean()
    coast = ndi.distance_transform_edt(OV8 > 0)
    coast_blk = coast * 8.0

    # ---- GLOBAL maquis -> semi-arid ----
    ov[ov == MAQUIS] = SARID

    # ---- shared fields: bowl hull (oasis guard) + nearest-ocean facing ----
    bowl_fill = ndi.binary_fill_holes(((OV8 == BOWL) | (OV8 == DSTEP)) & m)
    oc = (OV8 == 0)
    idxo = ndi.distance_transform_edt(~oc, return_indices=True)[1]
    dz = idxo[0].astype(np.int32) - row
    dx = idxo[1].astype(np.int32) - col
    face_S = (dz > 0) & (dz >= np.abs(dx))          # nearest ocean lies to the south

    # ---- WEST windward coast -> warm-temperate Atlantic gradient ----
    # No row-cut: gate by ~face_S so the south/Kostati coast goes to the taper, and
    # ~bowl_fill so the enclosed oasis is never de-tropicalized.
    west_wet = m & np.isin(ov, [RFC, LRFC]) & (col < bowl_x) & ~face_S & ~bowl_fill
    cb = coast_blk[west_wet]
    nv = np.empty(cb.shape, np.uint8)
    nv[cb < 250] = TREMP
    nv[(cb >= 250) & (cb < 800)] = MIXED
    nv[cb >= 800] = TDEC
    ov[west_wet] = nv
    n_west = int(west_wet.sum())

    # ---- SOUTH/SE tropical fringe -> taper to a transitional coastal band ----
    ys, xs = np.where(m); mid_r = (ys.min() + ys.max()) / 2
    south = row > mid_r
    fringe = m & south & ~bowl_fill & np.isin(ov, [RFC, LRFC]) & (coast_blk > 800)
    valid_src = m & ~np.isin(ov, TROP) & (ov > 0) & (ov != BOWL)
    sidx = ndi.distance_transform_edt(~valid_src, return_indices=True)[1]
    gathered = ov[sidx[0], sidx[1]]
    ov[fringe] = gathered[fringe]
    n_fringe = int(fringe.sum())

    # ---- two mainland islands bordering Kostati -> fully tropical ----
    KX, KZ = 18487 / 8, 49824 / 8
    lab, n = ndi.label(OV8 > 0)
    sizes = np.bincount(lab.ravel())
    small = []
    for i in range(1, n + 1):
        if not (3000 <= sizes[i] <= 1_000_000):
            continue
        yy, xx = np.where(lab == i)
        dist = ((xx.mean() - KX) ** 2 + (yy.mean() - KZ) ** 2) ** 0.5
        small.append((dist, i))
    small.sort()
    trop_ids = [i for _, i in small[:2]]
    for cid in trop_ids:
        mi = lab == cid
        keep_mangrove = mi & (ov == MANGR)
        d = coast[mi]
        q = np.quantile(d, [0.34, 0.67])
        nvi = np.empty(d.shape, np.uint8)
        nvi[d <= q[0]] = TJF
        nvi[(d > q[0]) & (d <= q[1])] = LRFC
        nvi[d > q[1]] = RFC
        ov[mi] = nvi
        ov[keep_mangrove] = MANGR

    # ---- LAST: restore the desert oasis (decisive-feature-must-be-last) ----
    oasis_protect = bowl_fill & np.isin(OV8, [LRFC, RFC, TJF, MANGR]) & m
    ov[oasis_protect] = OV8[oasis_protect]
    n_oasis = int(oasis_protect.sum())

    return ov, m, (bowl_x, bowl_z), dict(west=n_west, fringe=n_fringe,
                                         oasis=n_oasis, trop_ids=trop_ids)


def render_world(ov, path, title):
    L = lut()
    rgb = L[ov]
    rgb[ov == 0] = OCEAN
    img = Image.fromarray(rgb).resize((1500, 1500), Image.NEAREST)
    d = ImageDraw.Draw(img)
    try:
        f = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 22)
    except Exception:
        f = ImageFont.load_default()
    d.rectangle([0, 0, 760, 30], fill=(0, 0, 0)); d.text((8, 5), title, fill=(255, 255, 255), font=f)
    img.save(path)


def render_baby_ba(ov_old, ov_new, m, path):
    ys, xs = np.where(m)
    y0, y1, x0, x1 = ys.min() - 12, ys.max() + 12, xs.min() - 12, xs.max() + 12
    L = lut()
    def panel(ov):
        sub = ov[y0:y1, x0:x1]; rgb = L[sub]; rgb[sub == 0] = OCEAN; return rgb
    a = panel(ov_old); b = panel(ov_new)
    h, w = a.shape[:2]; sc = max(1, 760 // max(h, w)) + 1
    A = Image.fromarray(a).resize((w * sc, h * sc), Image.NEAREST)
    B = Image.fromarray(b).resize((w * sc, h * sc), Image.NEAREST)
    gap = 24
    cv = Image.new("RGB", (A.width + B.width + gap, A.height + 44), (12, 12, 16))
    cv.paste(A, (0, 44)); cv.paste(B, (A.width + gap, 44))
    d = ImageDraw.Draw(cv)
    try:
        f = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 26)
    except Exception:
        f = ImageFont.load_default()
    d.text((8, 8), "BEFORE (current)", fill=(255, 200, 200), font=f)
    d.text((A.width + gap + 8, 8), "AFTER (oasis kept + fringe tapered + full west)", fill=(200, 255, 200), font=f)
    cv.save(path)


def render_after_hi(ov_new, m, path):
    ys, xs = np.where(m)
    y0, y1, x0, x1 = ys.min() - 14, ys.max() + 14, xs.min() - 14, xs.max() + 14
    L = lut()
    def hillshade(hh):
        gz, gx = np.gradient(hh.astype(np.float32)); slope = np.pi / 2 - np.arctan(np.hypot(gx, gz) * 0.00012)
        aspect = np.arctan2(-gz, gx); az, al = np.deg2rad(315), np.deg2rad(45)
        hs = np.sin(al) * np.sin(slope) + np.cos(al) * np.cos(slope) * np.cos(az - aspect)
        return np.clip((hs + 1) / 2, 0, 1)
    sub = ov_new[y0:y1, x0:x1]; rgb = L[sub].astype(np.float32); rgb[sub == 0] = np.array([16, 26, 44])
    hs = hillshade(H8[y0:y1, x0:x1])[..., None]
    out = np.where((sub > 0)[..., None], rgb * 0.8 + hs * 255 * 0.2, rgb)
    out = np.clip(out, 0, 255).astype(np.uint8)
    h, w = out.shape[:2]
    Image.fromarray(out).resize((w * 3, h * 3), Image.NEAREST).save(path)


if __name__ == "__main__":
    ov_new, m, (bx, bz), stats = repaint()
    out = ROOT / "islands" / "out"
    render_world(ov_new, out / "_biome_after_world.png", "AFTER v2 - maquis->semi-arid + baby redo (oasis kept, fringe tapered)")
    render_baby_ba(OV8, ov_new, m, out / "_biome_baby_beforeafter.png")
    render_after_hi(ov_new, m, out / "_baby_after_hi.png")
    np.save(ROOT / "scratch_ov8_repainted.npy", ov_new)
    chg = OV8[m] != ov_new[m]
    print(f"bowl centre (1:8) x={bx:.0f} z={bz:.0f}")
    print(f"west belt remapped: {stats['west']} px | fringe tapered: {stats['fringe']} px | oasis restored-guard: {stats['oasis']} px")
    print(f"Kostati-border islands -> tropical: comp ids {stats['trop_ids']}")
    print(f"global maquis -> semi-arid (whole world): {int((OV8==MAQUIS).sum())} px")
    print(f"baby-continent cells changed: {int(chg.sum())} / {int(m.sum())} ({100*chg.sum()/m.sum():.1f}%)")
    # quick self-checks
    bowl_fill = ndi.binary_fill_holes(((OV8 == BOWL) | (OV8 == DSTEP)) & m)
    oasis_after_trop = int((bowl_fill & np.isin(OV8,[LRFC,RFC]) & m & np.isin(ov_new,[LRFC,RFC,TJF,MANGR])).sum())
    oasis_total = int((bowl_fill & np.isin(OV8,[LRFC,RFC]) & m).sum())
    print(f"CHECK oasis: {oasis_after_trop}/{oasis_total} enclosed-bowl tropical cells still tropical")
    coast_blk = ndi.distance_transform_edt(OV8>0)*8
    ys,xs=np.where(m); mid_r=(ys.min()+ys.max())/2
    south_trop = m & (np.arange(DS)[:,None]>mid_r) & ~bowl_fill & np.isin(ov_new,TROP)
    if south_trop.any():
        print(f"CHECK south fringe inland depth: p90={np.quantile(coast_blk[south_trop],0.9):.0f}blk max={coast_blk[south_trop].max():.0f}blk (target <~800)")
