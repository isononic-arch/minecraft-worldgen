"""
render_islands.py — bake + offset-render the islands from islands/layout.json.

ISOLATED: writes only under islands/ (masks_islands/<name>/, out/<name>/). Reuses
the mainland pipeline (run_pipeline._process_tile) UNCHANGED except a backward-
compatible world_offset_x/z (defaults 0 -> mainland identical).

Flow per island:
  BAKE  -> derive masks from DEM, flip/rotate to the world bbox, hand-paint override
           (biome bands by altitude + windward/leeward + aspect), synth rock_gap/
           snow_gap/shore/beach, write masks_islands/<name>/ + manifest + island cfg.
  RENDER-> for each content tile (skip all-ocean), call _process_tile with masks read
           LOCALLY and blocks written at the world offset -> islands/out/<name>/r.*.mca.

Usage:
  py islands/render_islands.py --bake  <key|all>
  py islands/render_islands.py --render <key|all> [--threads N]
  py islands/render_islands.py --list
"""
from __future__ import annotations
import argparse, json, math, re, sys, shutil
from pathlib import Path
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from derive_masks_from_height import (derive, load_dem, _GIN, _YOUT,
                                      SEA_LEVEL_RAW16 as SEA_RAW,
                                      clean_edge_fragments, detect_sea_raw,
                                      mcy_to_world_raw)

ISL = ROOT / "islands"
MASKS_OUT = ISL / "masks_islands"
OUT = ISL / "out"
ERASE_DIR = ISL / "erase_masks"
DOWNLOADS = Path.home() / "Downloads"
import importlib.util
_spec = importlib.util.spec_from_file_location("wls", ISL / "world_layout_studio.py")

def _registry():
    # pull REGISTRY without launching Qt: eval just the dict expression (dict()+literals)
    src = (ISL / "world_layout_studio.py").read_text()
    m = re.search(r"REGISTRY\s*=\s*\{", src)
    i = src.index("{", m.start())
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{": depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return eval(src[i:j+1], {"dict": dict, "__builtins__": {}})
    raise RuntimeError("REGISTRY not found")

REGISTRY = _registry()

def raw_to_mcy(raw):
    return np.interp(raw.astype(np.float64), _GIN, _YOUT)


# ── per-island biome bands (encodes BIOME_PLAN.md) ───────────────────────────
# key substring -> dict(bands=[(frac_lo, zone), ...] low->high, snowline_frac (>=1 = none),
#   rock_deg (slope threshold), windward (eastward biome-band shift fraction))
A = dict  # alias
# `litho` = per-island lithology group (real-world geology). The bake forces the
# whole island to ONE group (user: "most islands basaltic; flag the others").
#   temperate_basaltic = grey volcanic (default)  | limestone = coral/carbonate/karst
#   deepslate_metamorphic = schist/gneiss massif.  Group names: config.lithology.groups.
# CARIBBEAN ACCURACY (user 2026-06-23): the dense inland jungle is
# LUSH_RAINFOREST_COAST(160) — 19 tropical trees (palm/fig/banyan/breadfruit) +
# densest veg floor. TIDAL_JUNGLE_FRINGE(220) has only 2 tree schematics → a thin
# sparse FRINGE, never a primary band. MANGROVE_COAST(230) = tidal-waterline only.
# TEMPERATE_DECIDUOUS(60) = highland broadleaf cap. So wet tropical islands stack:
# thin mangrove coast -> LUSH jungle workhorse -> broadleaf cap (RAINFOREST_COAST
# 70 is *temperate* per user, kept only for the cool fjord, not the tropics).
BANDS = {
  # ── litho per real-world geology (S95-T1): NV + Fogo → deepslate_metamorphic; tropical volcanics → mossy_temperate (wet moss mix) ──
  "17_288":  A(bands=[(0.0,110),(0.10,120),(0.55,60),(0.85,150)],snow=1.5, rock=27, wind=0.18, litho="deepslate_metamorphic", frag_keep_largest=3),  # New Vincentia — IRISH (validated); metamorphic massif (re-walk rock). frag_keep_largest=3: this DEM crop holds exactly THREE real islands — St Kitts(443k px)/Nevis(264k)/Statia(63k) — that dominate every spurious component (next is a 14k coastal islet, 4.6x gap). The earlier frag_keep_cluster=600 dropped STATIA: Statia(700px) and Nevis(668px) are near-equidistant from St Kitts, but Nevis has a chain of stepping-stone islets within 600px hops while isolated Statia does not, so the chain gate kept Nevis and oceaned Statia. Top-3-by-AREA keeps all three regardless of distance. (Saba/St-Barth/St-Maarten are OFF-FRAME in this 75km/4096px crop — never present.)
  "13_130":  A(bands=[(0.0,230),(0.04,160),(0.60,60)],         snow=1.5,  rock=30, wind=0.12, litho="mossy_temperate", keep_box=[0.0,0.78,0.0,1.0]),  # Kostati St Vincent — lush tropical: mangrove coast → LUSH jungle → highland broadleaf. keep_box x<0.78: the real St Vincent + Grenadines chain all sit at centroid x<0.75; the rot=80 crop drags an off-frame secondary landmass into the far-right (comp cx0.82-0.87, centroid 0.33 of the bbox-diagonal from St Vincent) — the v6 "eastern boundary cutoff/artifacts" slab. x<0.78 oceans it while keeping every real Grenadine.
  "-1_509":  A(bands=[(0.0,230),(0.04,160),(0.72,60)],         snow=1.5,  rock=32, wind=0.10, litho="mossy_temperate"),  # Admiralty PNG — equatorial rainforest, LUSH-dominant
  "-17_622": A(bands=[(0.0,230),(0.04,160),(0.55,60)],         snow=1.5,  rock=28, wind=0.15, litho="mossy_temperate"),  # Efate Vanuatu — tropical + tradewind (W-wet/E-dry) windward/leeward
  "12_445":  A(bands=[(0.0,230),(0.05,160),(0.55,210)],        snow=1.5,  rock=30, wind=0.10, litho="mossy_temperate"),  # Grenadines — wet windward LUSH, dry leeward maquis
  "49_722":  A(bands=[(0.0,10),(0.10,30),(0.55,35)],           snow=0.62, rock=30, wind=0.0,  litho="deepslate_metamorphic"),  # Fogo Island NL — boreal cold; Precambrian Canadian-Shield gneiss (northernmost; Jan Mayen deleted)
  # ── metamorphic massif → deepslate_metamorphic ──────────────────────────────
  "11_060":  A(bands=[(0.0,150),(0.10,200),(0.45,210),(0.78,90)],snow=1.5,rock=29, wind=-0.10,litho="deepslate_metamorphic", keep_box=[0.45,1.0,0.0,1.0]),# Margarita — dry continental schist/gneiss (Cerro El Copey). keep_box x>0.45: real Isla Margarita (BOTH east + Macanao west lobes are ONE connected 6.2Mpx component spanning 4238 blocks at cx0.73) sits in the right two-thirds; the crop also catches a SEPARATE 2.96Mpx off-frame mainland-coast landmass at cx0.25 whose centroid is 4085 blocks (0.34 of the bbox-diagonal) west of the real island — the v6 "land cutoff" slab. x>0.45 oceans it + keeps Coche/Cubagua (cx0.74-0.95) and the real island intact.
  # ── carbonate / coral / marble karst → limestone ────────────────────────────
  "-50_393": A(bands=[(0.0,70),(0.10,20),(0.45,100),(0.75,30)],snow=0.72, rock=28, wind=0.10, litho="limestone", keep_box=[0.0,0.80,0.0,0.80], skip_erase=True, ocean_margin_px=32),# Madre de Dios — fjord/marble KARST. keep_box (centroid x<0.80 & y<0.80) keeps the THREE real fjord islands (white center-mass cx0.53 / green lower-left cx0.44 / fragmented medium island cx0.56-0.67) and oceans the unwanted mainland-coast cluster (cx>0.83) + two bottom-center blobs (cy0.88/0.91). Replaces frag_cut_east=0.75 which leaked ~298kpx (junk straddled the 0.75 col) and could not be fixed by frag_keep_largest=3 (the largest junk blob 2.9Mpx outranks the fragmented real medium island, largest fragment 1.43Mpx).
  "18_299":  A(bands=[(0.0,150),(0.12,210),(0.55,220)],        snow=1.5,  rock=30, wind=0.05, litho="limestone"),           # Anguilla/St Maarten — limestone-dominant
  "10_941":  A(bands=[(0.0,9),(0.06,200)],                     snow=1.5,  rock=34, wind=-0.10,litho="limestone"),           # La Tortuga — arid carbonate: SEMI_ARID_SHRUBLAND only; NO SAND_DUNE_DESERT(170) -> no dune columns. Beaches via beach.tif gap==9 (sentinel 9 -> next band 200)
  "21_395":  A(bands=[(0.0,9),(0.06,200),(0.40,150)],          snow=1.5,  rock=34, wind=-0.05,litho="limestone", height_gain=9.0, keep_box=[0.0,1.0,0.34,1.0]),# Grand Turk — dry coral; VERY flat DEM -> bigger gain to clear sea (median land Y64.4). keep_box y>0.34: the height_gain=9 inflates the shallow Caicos Bank, and one inflated wedge forms a hard-edged TRIANGULAR slab (the v6 upper "land cutoff", single 96kpx component, straight diagonal hypotenuse, centroid cy0.30) that reads as junk vs the lumpy/fractal real cays (all at cy>0.37). y>0.34 oceans the triangle + a thin northern bank strip (cy0.24) and keeps Grand Turk + every real cay cluster.
  "23_887":  A(bands=[(0.0,9),(0.06,150),(0.40,200)],          snow=1.5,  rock=34, wind=0.0,  litho="limestone", height_gain=5.0),# Bahamas — coral platform; spline mapped land to Y64 pancake ("nO BAHAMAS") -> gain lifts interior to ~Y66-78
  "11_863":  A(bands=[(0.0,230),(0.10,220)],                   snow=1.5,  rock=34, wind=0.0,  litho="limestone"),           # Los Roques — coral atoll
  "-20_529": A(bands=[(0.0,230),(0.10,220)],                   snow=1.5,  rock=34, wind=0.0,  litho="limestone"),           # Ouvea — raised coral atoll
  "-21_008": A(bands=[(0.0,9),(0.08,150),(0.40,210)],          snow=1.5,  rock=33, wind=0.0,  litho="limestone"),           # Loyalty — raised coral limestone
}
DEFAULT_BAND = A(bands=[(0.0,9),(0.06,150),(0.4,210)], snow=1.5, rock=30, wind=0.0, litho="temperate_basaltic")
VEX = 1.5   # vertical exaggeration of the no-override fallback peak (1.0 = geographically true 1:1;
            # islands/spline_overrides.json overrides this per-island via the spline editor)


def _key(dem_path):
    n = Path(dem_path).name
    for k in BANDS:
        if k in n:
            return k
    return None

def safe_name(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _organic_noise(H, W, cell_px, amp, seed):
    """Cheap low-frequency organic noise field (coarse random grid bilinear-upscaled),
    normalized to std=amp. For wobbling hard altitude-band boundaries into curves."""
    from scipy.ndimage import zoom
    rng = np.random.default_rng(seed)
    sh = (max(2, H // cell_px), max(2, W // cell_px))
    big = zoom(rng.random(sh, dtype=np.float32) - 0.5, (H / sh[0], W / sh[1]), order=1)[:H, :W]
    return (big / (float(big.std()) or 1.0) * amp).astype(np.float32)


def paint_override(mcy, land, bands, wind):
    """Zone-code override painted by altitude band, shifted by windward gradient.
    Beach (sentinel 9 in bands) is left to the procedural beach mask -> painted as
    the lowest forest zone here; the real beach comes from beach.tif gap==9.
    Band boundaries are SOFTENED (organic wobble + ragged dither) so they read as
    natural ecotones, not hard concentric rings that staircase around the peak."""
    H, W = mcy.shape
    ov = np.zeros((H, W), np.uint8)
    land_mcy = mcy[land]
    if land_mcy.size == 0:
        ov[~land] = 254   # S95-T4: all-ocean bbox -> uniform _OCEAN sentinel
        return ov
    lo, hi = 63.0, max(np.percentile(land_mcy, 99.5), 70.0)
    frac = np.clip((mcy - lo) / max(hi - lo, 1.0), 0, 1)
    # windward shift: west (low col) wetter -> bands sit lower; east drier -> higher.
    if wind:
        colpos = (np.arange(W)[None, :] / max(W - 1, 1)) - 0.5     # -0.5 west .. +0.5 east
        frac = frac + wind * colpos * 2.0
    # soften: low-freq organic wobble (~4.5% relief) + fine per-pixel dither (~3%)
    rng = np.random.default_rng(0xB10E)
    frac = np.clip(frac + _organic_noise(H, W, 40, 0.045, 0xB10E)
                   + (rng.random((H, W), dtype=np.float32) - 0.5) * 0.03, 0, 1)
    seq = bands["bands"]
    for flo, zone in seq:
        z = zone if zone != 9 else seq[1][1] if len(seq) > 1 else 150   # beach sentinel -> next band
        ov[land & (frac >= flo)] = z
    ov[~land] = 254   # S95-T4: open-ocean sentinel -> _OCEAN (see OVERRIDE_BIOME_MAP). Bands write land-only above, so this fills only ocean; survives the raster/painted composites (np.where(_x>0,...)) and _rov[~land]=0.
    return ov


def synth_rock_gap(slope_u16, rock_deg, world_offset_px=(0, 0), dither_deg=10.0,
                   land=None, max_land_frac=None):
    """ISLAND rock_gap. COPIES THE MAINLAND query-time edge (core/gaea_gap_sampler.
    sample_gap_at_tile + config.gaea_gaps.slope_dither=blue_noise / slope_dither_width
    =40000): instead of a HARD `deg >= rock_deg` cut (which gave the island a razor
    rock/forest edge), threshold the slope with a blue-noise probability RAMP across a
    dither band [rock_deg - dither_deg/2, rock_deg + dither_deg/2]. prob = clip((deg-lo)
    /(hi-lo),0,1); rock where prob > blue_noise_coin -> a soft salt-and-pepper fade
    exactly like the mainland's rock_gap mask. The coin is tiled by WORLD coords
    (ox+col, oz+row) so it is seam-safe for the offset render (same discipline as the
    island beach hash + the mainland's world-coord rock coin). dither_deg is the band
    width in degrees: the mainland band is ~56% wider than its solid core (slope_8k:
    7.65% in-band vs 4.91% above threshold), so a ~10deg band on the 0-45deg island
    scale matches that proportion. ISLAND-ONLY: synth_rock_gap has exactly one caller
    (render_islands bake); the mainland is byte-untouched."""
    from core.upscale import make_blue_noise_tile
    deg = slope_u16.astype(np.float32) / 65535.0 * 45.0
    H, W = deg.shape
    lo = float(rock_deg) - 0.5 * float(dither_deg)
    hi = float(rock_deg) + 0.5 * float(dither_deg)
    prob = np.clip((deg - lo) / max(hi - lo, 1e-6), 0.0, 1.0).astype(np.float32)
    # blue-noise coin, tiled by WORLD coords (seam-safe), same generator + size 512
    # + seed 42 as config.gaea_gaps (so island dither grain matches the mainland's).
    bn = make_blue_noise_tile(512, seed=42)
    ox, oz = int(world_offset_px[0]), int(world_offset_px[1])
    ys = (oz + np.arange(H, dtype=np.int64))[:, None] % 512
    xs = (ox + np.arange(W, dtype=np.int64))[None, :] % 512
    coin = bn[ys, xs]
    rg = (prob > coin).astype(np.uint8)
    # ── ISLAND COVERAGE CAP (B / Madre) ──────────────────────────────────────
    # Islands derive a TRUE-MONOTONE slope (clip(deg/45)), so a steep volcanic/karst
    # island (Madre: land slope median 43deg, 42% saturated at the 45deg clip) floods
    # rock_gap onto ~76% of land -> rock/wash/talus paint everywhere (the "noise
    # everywhere" complaint). The MAINLAND's band-pass Gaea slope gives a SPARSE
    # ~17-20% rock_gap. Cap island rock_gap to `max_land_frac` of land by keeping the
    # STEEPEST cells; the 45deg saturation ties are broken by a 2nd world-coord blue-
    # noise coin so the selection stays seam-safe + dithered (no per-tile RNG). No-op
    # on gentle islands (coverage already below the cap). Mainland never calls this.
    if land is not None and max_land_frac is not None and bool(np.asarray(land).any()):
        _ln = np.asarray(land, bool)
        n_target = int(float(max_land_frac) * int(_ln.sum()))
        if 0 < n_target < int(rg[_ln].sum()):
            bn2 = make_blue_noise_tile(512, seed=99)
            coin2 = bn2[ys, xs].astype(np.float32)
            score = deg + coin2 * 1.5                      # tie-break 45deg saturation, seam-safe
            thr = float(np.partition(score[_ln], -n_target)[-n_target])
            rg = (rg.astype(bool) & _ln & (score >= thr)).astype(np.uint8)
    return rg

def synth_snow_gap(mcy, land, bands):
    sf = bands["snow"]
    if sf >= 1.0:
        return np.zeros(mcy.shape, np.uint8)
    land_mcy = mcy[land]
    hi = max(np.percentile(land_mcy, 99.5), 70.0)
    snowline = 63.0 + sf * (hi - 63.0)
    return (land & (mcy >= snowline)).astype(np.uint8)

def synth_shore_beach(land, mcy, slope_u16, beach_band=3.0, gentle_deg=10.0):
    """Mainland rebuild_beach formula (height-derived): sand where ocean meets land
    near sea level on GENTLE slopes — elev-gate (tight band above Y63) x gentle-slope
    x land. The hard Y=63 filter is applied downstream in eco_gradients (same as the
    mainland). Replaces the old coast-distance synth."""
    from scipy.ndimage import distance_transform_edt
    elev = np.clip(mcy.astype(np.float32) - 63.0, 0, None)         # blocks above sea
    gate = np.clip(1.0 - elev / beach_band, 0.0, 1.0)
    deg = slope_u16.astype(np.float32) / 65535.0 * 45.0
    gentle = np.clip(1.0 - deg / gentle_deg, 0.0, 1.0)
    beach = (gate * gentle * land * 255).astype(np.uint8)
    dist = distance_transform_edt(land)
    shore = (land & (dist <= 6)).astype(np.uint8)
    return shore, beach


def synth_shore_beach_wide(land, mcy, slope_u16, world_offset_px=(0, 0),
                           beach_core_width=6.0, beach_dither_mult=4.0,
                           gentle_slope_deg=14.0):
    """S95-T2: ports the MAINLAND soft-beach recipe (core/eco_gradients.py:826-957)
    to a full-island bake so island sand reads like the mainland's organic shoreline
    instead of a hard band. EDT distance-from-ocean -> a per-pixel core width drawn
    from a WORLD-COORD splitmix64 hash blurred at sigma=12 (normalized by the fixed
    analytic std, NOT per-tile min-max), a dither zone = core*beach_dither_mult, a
    linear place_prob clamped to [0.15,0.85], and a second finer splitmix64 dither
    coin (sigma=1). Result: ~6 core + ~24 dither ~= 30 blocks of soft, ragged-edged
    sand, gentle-slope gated so steep volcanic cliffs do not grow a 30-block apron.
    The hash uses (ox+col, oz+row) -> seam-safe for any future tiled re-baker;
    ox,oz = the SNAPPED island offset (_ox/_oz). beach.tif drives gap==9 downstream
    via the config-gated from_mask path in eco_gradients. shore stays dist<=6 (feeds
    the column_generator near-water hard band, kept for mainland parity)."""
    from scipy.ndimage import distance_transform_edt, gaussian_filter
    ox, oz = int(world_offset_px[0]), int(world_offset_px[1])
    H, W = land.shape
    # S95 #5a: waterline at MC Y63 (strict). The passed `land` is mcy>63.4, so the
    # ANCHOR the beach to the RENDER's actual coastline. The final height.tif re-snaps
    # `height[~land]=min(.,SEA_RAW)` with land=mcy>63.4 (line ~488), so the rendered
    # land starts at mcy>63.4 and the mcy in [63,63.4) ring is OCEAN. A prior cut used
    # beach_land=mcy>=63.0, which put the beach band one ring SEAWARD of the real shore
    # (on cells that then become ocean) -> the true first land ring rendered BARE
    # rock/grass (measured 92-100% bare) = "land cuts off into ocean". Use the SAME
    # >63.4 land test so the beach core lands on the first RENDER-land ring -> water
    # meets sand.
    beach_land = mcy > 63.4
    ocean = ~beach_land
    if not ocean.any() or not beach_land.any():
        shore = (land & (distance_transform_edt(land) <= 6)).astype(np.uint8)
        return shore, np.zeros((H, W), np.uint8)
    dist_from_ocean = distance_transform_edt(~ocean).astype(np.float32)
    deg = slope_u16.astype(np.float32) / 65535.0 * 45.0
    gentle = deg < float(gentle_slope_deg)

    # world-coord splitmix64 hash -> [0,1], same kernel as eco_gradients beach
    wx = (np.int64(ox) + np.arange(W, dtype=np.int64))[None, :].astype(np.uint64)
    wz = (np.int64(oz) + np.arange(H, dtype=np.int64))[:, None].astype(np.uint64)

    def _hash01(salt):
        s64 = np.uint64((salt * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF)
        h = (wx * np.uint64(0x9E3779B97F4A7C15)
             + wz * np.uint64(0xBF58476D1CE4E5B9) + s64)
        h = (h ^ (h >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
        h = (h ^ (h >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
        h = h ^ (h >> np.uint64(31))
        return (h.astype(np.float64) / np.float64(np.iinfo(np.uint64).max)).astype(np.float32)

    BCH_SIGMA = 12.0
    wn = gaussian_filter(_hash01(0xBEAC4), sigma=BCH_SIGMA)
    std_wn = np.float32((1.0 / np.sqrt(12.0)) / (2.0 * np.sqrt(np.pi) * BCH_SIGMA))
    width_noise = np.clip((wn - np.float32(0.5)) / (np.float32(2.5) * std_wn),
                          -1.0, 1.0).astype(np.float32)
    amp = np.float32(beach_core_width * 0.4)            # +-40% width jitter
    core_width = np.maximum(np.float32(beach_core_width) + amp * width_noise,
                            0.0).astype(np.float32)
    dither_width = core_width * np.float32(beach_dither_mult)
    total_width = core_width + dither_width

    # S95 #6b: CORE sand always hugs the waterline (NO gentle gate) so steep
    # volcanic coasts still get a continuous thin beach instead of inland specks,
    # and it starts at the first land ring (dist==1 from true water), not offset
    # inland. The wide dither apron KEEPS the gentle gate so cliffs don't grow a
    # 30-block sand apron.
    core = beach_land & (dist_from_ocean >= 1.0) & (dist_from_ocean <= core_width)
    in_dither = (beach_land & gentle & (dist_from_ocean > core_width)
                 & (dist_from_ocean <= total_width))
    t = np.clip((dist_from_ocean - core_width) / np.maximum(dither_width, np.float32(0.5)),
                0.0, 1.0)
    place_prob = np.clip(1.0 - t, 0.15, 0.85).astype(np.float32)

    DC_SIGMA = 1.0
    dc = gaussian_filter(_hash01(0xD17BEA), sigma=DC_SIGMA)
    std_dc = np.float32((1.0 / np.sqrt(12.0)) / (2.0 * np.sqrt(np.pi) * DC_SIGMA))
    coin = np.clip(np.float32(0.5) + (dc - np.float32(0.5)) / (np.float32(2.5) * std_dc) * np.float32(0.5),
                   0.0, 1.0).astype(np.float32)
    dithered = in_dither & (coin < place_prob)

    beach = ((core | dithered).astype(np.uint8)) * 255
    shore = (land & (distance_transform_edt(land) <= 6)).astype(np.uint8)
    return shore, beach


def _wtif(path, arr):
    import rasterio
    prof = dict(driver="GTiff", height=arr.shape[0], width=arr.shape[1], count=1,
                dtype=arr.dtype, compress="deflate")
    with rasterio.open(str(path), "w", **prof) as d:
        d.write(arr, 1)


# NOTE (S95): _resize_nn / _resize_lin / synth_lithology / build_island_terrain_derived
# were ARCHIVED to islands/_archive_island_maskgen.py. The island bake now ROUTES
# THROUGH THE REAL mainland generator — tools/build_lithology.py + build_terrain_derived.py
# run (size-parametric) against the island masks dir with the real config — so every
# surface feature (rock dark/mid/light tiers, talus, cliff_cap, aspect, ecotone) fires
# identically to the mainland. Bespoke kept = spline, derived base masks, override
# paint, clearings, bog, snow_gap, shore.


def _remove_over_mainland(height, mcy, land, ox, oz):
    """Ocean out island land that sits over MAINLAND LAND (mainland ocean is fine to
    overwrite — that's the whole point of placing islands in the sea). ox,oz = the
    snapped world offset the render uses. Mutates height/mcy/land in place; returns px.
    Local cell (r,c) maps to world (x=ox+c, z=oz+r), aligning with mainland[r,c] in the
    same window — so removal is a direct boolean AND, no resampling."""
    import rasterio
    from rasterio.windows import Window
    mh = ROOT / "masks" / "height.tif"
    if not mh.exists():
        return 0
    H, W = land.shape
    with rasterio.open(mh) as s:
        MW, MH = s.width, s.height
        c0, c1 = max(0, -ox), min(W, MW - ox)
        r0, r1 = max(0, -oz), min(H, MH - oz)
        if c1 <= c0 or r1 <= r0:
            return 0                                   # island fully outside the mainland -> all ocean, keep
        msub = s.read(1, window=Window(ox + c0, oz + r0, c1 - c0, r1 - r0))
    rem = land[r0:r1, c0:c1] & (msub > SEA_RAW)         # mainland land = raw above sea level (Y63)
    if rem.any():
        full = np.zeros_like(land)
        full[r0:r1, c0:c1] = rem
        height[full] = SEA_RAW; mcy[full] = 63.0; land[full] = False
    return int(rem.sum())


def _keep_largest_components(land, n_keep):
    """ISLAND-ONLY (per-island `frag_keep_largest` flag). Keep ONLY the `n_keep`
    largest connected land components; ocean everything else. Returns (keep_mask,
    killed_px). Used when a real-world DEM crop contains a FIXED, KNOWN number of
    real islands whose areas dominate every spurious component (off-frame neighbour
    leftovers, rotation-halo specks, coastal islets) by a wide margin, so a
    distance/chain gate (_keep_archipelago_cluster) cannot separate them — e.g. New
    Vincentia (17_288): St Kitts / Nevis / Statia are the 3 largest land masses
    (443k / 264k / 63k px) with a 4.6x area gap to the next component (14k px), but
    Statia (700px from St Kitts) and Nevis (668px) are near-equidistant and Statia
    has no stepping-stone islets, so chain-hop keeps Nevis and drops Statia. Top-N
    by AREA keeps exactly the 3 real islands regardless of inter-island distance.
    Exactly one caller (bake_island, gated on the per-island flag) -> mainland
    byte-untouched."""
    from scipy.ndimage import label as _label
    lbl, n = _label(land)
    if n <= n_keep:
        return land.copy(), 0
    sz = np.bincount(lbl.ravel()); sz[0] = 0
    keep_ids = list(np.argsort(sz)[::-1][:int(n_keep)])   # n_keep largest labels
    keep_mask = np.isin(lbl, keep_ids)
    return keep_mask, int((land & ~keep_mask).sum())


def _keep_archipelago_cluster(land, link_gap, min_keep_px=2000):
    """ISLAND-ONLY (per-island `frag_keep_cluster` flag). Some real-world DEM crops
    contain a NEIGHBOURING real island that does NOT touch the frame border, so the
    default border-touch frag-kill (bake's else-branch) leaves it as a stray landmass
    (e.g. the Saba/St-Barths triangle caught in the St Kitts DEM, ~1400px N of the
    cluster). COPIES THE MAINLAND'S clean_edge_fragments primitives (derive_masks_
    from_height.clean_edge_fragments: scipy.ndimage.label + np.bincount -> keep the
    real landmass, drop the crop leftover) but grows the kept set along the real
    ARCHIPELAGO CHAIN instead of border-touch: keep the main component + every
    component reachable from it by edge-to-edge hops <= link_gap px. Sub-pixel rotation
    specks (< min_keep_px) never bridge (so they can't relay the kept set across the
    open-water gap to the straggler). Returns (keep_mask, killed_px). Exactly one
    caller (bake_island, gated on the per-island flag) -> mainland byte-untouched."""
    from scipy.ndimage import label as _label, distance_transform_edt as _edt
    lbl, n = _label(land)
    if n <= 1:
        return land.copy(), 0
    sz = np.bincount(lbl.ravel()); sz[0] = 0
    main = int(sz.argmax())
    big = [c for c in range(1, n + 1) if sz[c] >= min_keep_px]   # candidate real members
    kept = {main}
    changed = True
    while changed:
        changed = False
        d = _edt(~np.isin(lbl, list(kept)))                      # px-dist to nearest kept-land cell
        for c in big:
            if c in kept:
                continue
            if float(d[lbl == c].min()) <= link_gap:
                kept.add(c); changed = True
    keep_mask = np.isin(lbl, list(kept))
    return keep_mask, int((land & ~keep_mask).sum())


def _keep_centroid_box(land, box):
    """ISLAND-ONLY (per-island `keep_box=[x0,x1,y0,y1]` flag, fractions of W/H).
    Ocean every connected land component whose CENTROID falls OUTSIDE the box; keep
    those inside. Use when a real-world DEM crop interleaves the real islands with
    UNWANTED land (off-frame mainland coast, outer islets) that a single x-cut cannot
    separate because the junk straddles the cut column AND wraps around below the real
    islands (Madre de Dios -50_393: the 3 real fjord islands occupy the left/center;
    a right-third coastal cluster + two bottom-center blobs at cx 0.65/0.79 straddle
    any vertical cut). frag_keep_largest fails too: the largest spurious right-cluster
    component (2.9M px) outranks the real medium island, which is FRAGMENTED into a
    few touching sub-components (largest 1.43M, rank #7) -> top-3-by-area keeps the
    junk and drops the real island. A centroid box keeps every fragment of the real
    islands (all centroids x<0.75, y<0.75) and oceans all junk (centroids x>0.83 or
    y>0.85) in one pass. Returns (keep_mask, killed_px). Exactly one caller
    (bake_island, gated on the per-island flag, default None) -> mainland byte-
    untouched."""
    from scipy.ndimage import label as _label, center_of_mass as _com
    H, W = land.shape
    x0, x1, y0, y1 = box
    lbl, n = _label(land)
    if n == 0:
        return land.copy(), 0
    coms = _com(land, lbl, range(1, n + 1))      # list of (cy, cx) per label, in px
    keep_ids = []
    for i, (cy, cx) in enumerate(coms, start=1):
        if (x0 <= cx / W <= x1) and (y0 <= cy / H <= y1):
            keep_ids.append(i)
    keep_mask = np.isin(lbl, keep_ids)
    return keep_mask, int((land & ~keep_mask).sum())


def _resolve_reg(dem_name):
    """REGISTRY is keyed by the ORIGINAL DEM filename. When an island's dem_path
    points at an OFFLINE PRE-ROTATED DEM (prerotate_dems.py -> rot baked in, layout
    rot_deg=0), the filename no longer matches a REGISTRY key, so a direct
    REGISTRY.get returns {} and silently loses peak_m/declip/edge_clean. Fall back to
    matching the REGISTRY key whose leading `{lat}_{lon}` token is a prefix of the
    DEM filename (the pre-rotated name keeps that token, e.g.
    `12_445_..._prerot_16bit.png`). ISLAND-ONLY (bake-time)."""
    if dem_name in REGISTRY:
        return REGISTRY[dem_name]
    for k, v in REGISTRY.items():
        tok = "_".join(k.split("_")[:2])          # e.g. "12_445"
        if dem_name.startswith(tok + "_"):
            return v
    return {}


def bake_island(entry):
    reg = _resolve_reg(Path(entry["dem_path"]).name)
    key = _key(entry["dem_path"])
    bands = BANDS.get(key, DEFAULT_BAND)
    name = safe_name(entry["name"])
    od = MASKS_OUT / name
    od.mkdir(parents=True, exist_ok=True)
    print(f"[bake] {name}  key={key}", flush=True)

    dem = load_dem(Path(entry["dem_path"]))
    sea0 = detect_sea_raw(dem)
    dem, nrm = clean_edge_fragments(dem, sea0, max_frac=reg.get("edge_clean", 0.05))
    if nrm:
        print(f"[bake]   cleaned {nrm}px frame-edge slivers", flush=True)
    bpp = float(entry["blocks_per_px"])
    footprint = round(bpp * dem.shape[1])
    peak_m = reg.get("peak_m", 500.0)
    mcy_peak = 63.0 + peak_m / 9.14 * VEX
    # HAND-EDITED VERTICAL SPLINE (islands/island_spline_studio.py): per-island
    # (frac -> MC-Y) breakpoints that REPLACE the parametric coast/gamma shape.
    # The one bespoke per-island knob. Absent => parametric default (unchanged).
    spline_curve = None
    _spov_path = ISL / "spline_overrides.json"
    if _spov_path.exists():
        _ov = json.loads(_spov_path.read_text()).get(key)
        if _ov and _ov.get("fracs") and _ov.get("mcys"):
            spline_curve = (_ov["fracs"], _ov["mcys"])
            mcy_peak = float(_ov["mcys"][-1])
            print(f"[bake]   spline override applied: peak MC-Y={mcy_peak:.0f}, {len(_ov['fracs'])} pts", flush=True)
    # native_cap=4096: keep the DEM's FULL detail (real ridges/gullies). The old
    # 2048 downsample smoothed the flanks into a clean cone -> slope/altitude
    # boundaries fell on concentric rings. Full detail = boundaries follow terrain.
    out, _ = derive(dem, footprint_blocks=footprint, mcy_sea=63.0, mcy_peak=mcy_peak,
                    sea_raw=sea0, native_cap=4096, seabed="vandir", seabed_base=-60.0,
                    declip=reg.get("declip", True), declip_frac=0.12, clean_fragments=False,
                    curve=spline_curve)
    # apply user erasing (set erased pixels to ocean). Per-island `skip_erase=True`
    # (default absent) bypasses a STALE hand-painted erase mask: Madre's (-50_393)
    # erase_masks/*.png predates keep_box and erases 63.7% of the DEM land INCLUDING
    # the largest real fjord island (precut comp 105, 13.3Mpx, the center-left mass) —
    # leaving only the lower island + the junk that keep_box now removes anyway. With
    # keep_box doing the junk-kill cleanly, skip the destructive erase so all three real
    # islands survive. ISLAND-ONLY (flag absent everywhere else) -> mainland untouched.
    em = ERASE_DIR / (Path(entry["dem_path"]).stem + ".png")
    if em.exists() and not bands.get("skip_erase", False):
        e = np.asarray(Image.open(em).convert("L"))
        ef = np.array(Image.fromarray(e).resize((out["height"].shape[1], out["height"].shape[0]), Image.NEAREST))
        out["height"][ef > 0] = SEA_RAW
        print(f"[bake]   applied erase mask ({int((ef>0).sum())} px -> ocean)", flush=True)
    elif em.exists():
        print(f"[bake]   skip_erase=True: bypassed stale erase mask ({em.name})", flush=True)

    flipx, flipz = entry.get("flipx", False), entry.get("flipz", False)
    rot = float(entry.get("rot_deg", 0.0))
    from scipy.ndimage import rotate as nd_rotate
    masks = {}
    for nm, arr in out.items():
        a = arr
        if flipx: a = np.fliplr(a)
        if flipz: a = np.flipud(a)
        if rot % 360:
            cval = float(SEA_RAW) if nm == "height" else 0.0
            a = nd_rotate(a.astype(np.float32), -rot, reshape=True, order=1,
                          cval=cval, prefilter=False)
            a = np.clip(a, 0, 65535).astype(np.uint16)
        masks[nm] = np.ascontiguousarray(a)
    H, W = masks["height"].shape
    mcy = raw_to_mcy(masks["height"])
    # ── CORAL-PLATFORM HEIGHT GAIN (per-island `height_gain`, default 1.0 = NO-OP) ──
    # Flat coral platforms (San Salvador/Rum Cay = "23_887", Grand Turk = "21_395")
    # have so little real relief that the hand-edited spline (spline_overrides.json)
    # maps the WHOLE island to ~1 block above sea (median MC-Y ~64): the rendered
    # land is a pancake that water laps right over -> the user walked it and saw "nO
    # BAHAMAS" (all ocean). Multiply the above-sea height by `height_gain` so the
    # platform INTERIOR rises to a visible Y66-78 while the shoreline (mcy~63) stays
    # low -> a believable low, flat-topped coral island with beaches. Multiplicative
    # (vs additive) keeps the coast a beach and only lifts where there is real DEM
    # relief; it also grows the land mask (more cells clear the 63.4 threshold) so
    # the island is contiguous, not stray specks. The boosted mcy is written BACK to
    # masks["height"] (the rendered terrain reads height.tif, not mcy) so columns
    # render at the lifted elevation. ISLAND-ONLY: gated on the per-island BANDS
    # field which is ABSENT for every other island and the mainland -> byte-identical
    # there (gain=1.0 -> 63+(mcy-63)*1 == mcy, and masks["height"] is rewritten to the
    # same raw it already held).
    # V7-fix: the OLD uniform multiply `mcy = 63 + (mcy-63)*gain` turned the flat
    # coral platform into a flat-topped MESA with CLIFF sides. It applies the SAME
    # gain to every above-sea cell, so (a) the platform interior and the shoreline
    # rise by wildly different absolute amounts (a hard step at the coast), and (b)
    # it AMPLIFIES the shallow bank's micro-relief gain-fold, inflating submerged
    # bank into smooth rounded plateaus with long straight/circular hard edges
    # (grand_turk land-edge 20px->218px) whose steep flanks tripped the slope-driven
    # rock surface (5%->26% of land). FIX: a FEATHERED COASTAL DOME instead —
    #   1. lift = lift_amp * smoothstep(EDT(land) / feather_px)
    #      so the lift is 0 at the existing shoreline (NO cliff) and ramps to full
    #      over `feather_px` blocks inland -> a gentle dome, not a flat-top.
    #   2. ADD the lift (not multiply) so the bank's micro-relief is NOT amplified.
    #   3. gaussian-blur the lifted mcy (no residual hard steps).
    #   4. cap interior at `lift_cap_y` so land sits a few blocks proud of Y63
    #      (~Y66-72), NOT a 200px mesa.
    # Per-island knob: `height_gain` (float; default 1.0 = NO-OP, mainland + every
    # other island byte-identical). For back-compat it is REINTERPRETED as a peak
    # lift amplitude in blocks: lift_amp = (height_gain - 1.0) so the old 5.0/9.0
    # become a +4/+8-block interior dome. Optional `lift_feather_px` (default 60)
    # and `lift_cap_y` (default 74) fine-tune the dome. ISLAND-ONLY.
    _hgain = float(bands.get("height_gain", 1.0))
    if _hgain != 1.0:
        from scipy.ndimage import distance_transform_edt, gaussian_filter
        _lift_amp = max(_hgain - 1.0, 0.0)                       # +blocks at the dome centre
        _feather = float(bands.get("lift_feather_px", 60.0))     # shore->full ramp width (blocks)
        _cap_y = float(bands.get("lift_cap_y", 74.0))            # interior elevation ceiling
        _seed_land = mcy > 63.4                                  # lift the CURRENT land only
        if _seed_land.any():
            # distance inward from the coast, normalized + smoothstepped -> 0 at the
            # shore, 1 deep in the interior. smoothstep(3t^2-2t^3) gives a C1 dome
            # with zero slope at both ends (no rim cliff, no centre spike).
            _d = distance_transform_edt(_seed_land).astype(np.float32)
            _t = np.clip(_d / max(_feather, 1.0), 0.0, 1.0)
            _w = _t * _t * (3.0 - 2.0 * _t)                      # smoothstep
            _lift = _lift_amp * _w
            mcy = mcy + _lift
            # smooth away any residual stair (small sigma so the dome shape survives)
            mcy = gaussian_filter(mcy, sigma=3.0).astype(np.float32)
            # cap the interior so it stays a low coral island, not a mesa
            np.minimum(mcy, _cap_y, out=mcy, where=(mcy > _cap_y))
            masks["height"] = mcy_to_world_raw(mcy)
            _lm = mcy > 63.4
            print(f"[bake]   height_gain={_hgain} (feathered dome amp=+{_lift_amp:.0f} "
                  f"feather={_feather:.0f}px cap=Y{_cap_y:.0f}): land>63.4 now "
                  f"{int(_lm.sum())}px, interior median MC-Y={np.median(mcy[_lm]):.1f}", flush=True)
    land = mcy > 63.4
    # KILL border-clipped stragglers: every land component that touches the FINAL frame
    # border EXCEPT the main island -> ocean. These (erase-job leftovers) are what slice
    # off at the island region's edge and read as a harsh seam against the noise-ocean.
    _cut = bands.get("frag_cut_east")
    if _cut is not None:                              # per-island exception: ocean everything east (right) of a
        _xc = int(_cut * W)                           # column fraction (Madre de Dios mainland-coast leftover)
        _km = np.zeros(land.shape, bool); _km[:, _xc:] = land[:, _xc:]
        if _km.any():
            masks["height"][_km] = SEA_RAW; mcy[_km] = 63.0; land = mcy > 63.4
            print(f"[bake]   frag_cut_east={_cut}: oceaned {int(_km.sum())}px east of col {_xc}", flush=True)
    else:                                             # default: drop land clipped at the frame border
        from scipy.ndimage import label as _label
        _lbl, _n = _label(land)
        if _n > 1:
            _sz = np.bincount(_lbl.ravel()); _sz[0] = 0
            _main = int(_sz.argmax())
            _bord = set(_lbl[0, :]) | set(_lbl[-1, :]) | set(_lbl[:, 0]) | set(_lbl[:, -1])
            _bord.discard(0)
            _kill = [int(l) for l in _bord if l != _main]
            if _kill:
                _km = np.isin(_lbl, _kill)
                masks["height"][_km] = SEA_RAW; mcy[_km] = 63.0; land = mcy > 63.4
                print(f"[bake]   killed {len(_kill)} border-clipped straggler(s) ({int(_km.sum())}px) -> ocean", flush=True)
            if _main in _bord:
                print("[bake]   WARNING: MAIN island touches the frame border -> widen the DEM crop", flush=True)
    # INTERIOR-straggler kill, OPTION A (per-island `frag_keep_largest`): keep ONLY the
    # N largest land components; ocean everything else. Use when the DEM crop holds a
    # KNOWN fixed number of real islands whose areas dominate all spurious land (off-frame
    # neighbour leftovers, rotation specks, coastal islets) by a wide margin -> top-N-by-
    # area is unambiguous where a distance gate fails. New Vincentia (17_288): n=3 keeps
    # St Kitts(443k)/Nevis(264k)/Statia(63k); next comp is 14k (4.6x gap). Statia(700px)
    # and the outliers are near-equidistant from St Kitts, so chain-hop can't separate
    # them; area can. Absent => skipped (all other islands + mainland unchanged).
    _keep_n = bands.get("frag_keep_largest")
    if _keep_n is not None and land.any():
        _keepm, _killpx = _keep_largest_components(land, int(_keep_n))
        if _killpx:
            _km = land & ~_keepm
            masks["height"][_km] = SEA_RAW; mcy[_km] = 63.0; land = mcy > 63.4
            print(f"[bake]   frag_keep_largest={_keep_n}: oceaned {_killpx}px outside the {_keep_n} largest islands", flush=True)
    # INTERIOR-straggler kill, OPTION B (per-island `frag_keep_cluster`): keep only land
    # within `frag_keep_cluster` px chain-hops of the main island (archipelago chain).
    # Absent => skipped. Prefer frag_keep_largest when island count is fixed/known.
    _keep_gap = bands.get("frag_keep_cluster")
    if _keep_gap is not None and land.any():
        _keepm, _killpx = _keep_archipelago_cluster(land, float(_keep_gap))
        if _killpx:
            _km = land & ~_keepm
            masks["height"][_km] = SEA_RAW; mcy[_km] = 63.0; land = mcy > 63.4
            print(f"[bake]   frag_keep_cluster={_keep_gap}: oceaned {_killpx}px of off-cluster straggler land", flush=True)
    # INTERIOR-straggler kill, OPTION C (per-island `keep_box=[x0,x1,y0,y1]`, W/H
    # fractions): keep only land components whose CENTROID is inside the box; ocean the
    # rest. Use when junk land STRADDLES any single cut (Madre -50_393: real islands at
    # left/center, mainland-coast cluster at right + two bottom-center blobs that cross
    # a 0.75 x-cut) so frag_cut_east leaks ~298kpx AND frag_keep_largest keeps the
    # biggest junk blob over the fragmented real medium island. Absent => skipped (every
    # other island + mainland byte-identical). Runs LAST so it also sweeps any leftover
    # from frag_cut_east if both are set.
    _kbox = bands.get("keep_box")
    if _kbox is not None and land.any():
        _keepm, _killpx = _keep_centroid_box(land, [float(v) for v in _kbox])
        if _killpx:
            _km = land & ~_keepm
            masks["height"][_km] = SEA_RAW; mcy[_km] = 63.0; land = mcy > 63.4
            print(f"[bake]   keep_box={_kbox}: oceaned {_killpx}px of land with centroid outside the box", flush=True)
    # OCEAN-MARGIN RIM (per-island `ocean_margin_px`, default 0 = no-op): force an N-px
    # rim around ALL 4 bbox borders to ocean so NO kept land touches a frame edge. A
    # coastline that hits the bbox edge renders as a hard "staircase wall" against the
    # noise-ocean. Madre's restored center island reaches within 1px of the TOP border,
    # so a small rim guarantees a clean water margin on every side. ISLAND-ONLY (absent
    # everywhere else -> mainland byte-identical).
    _margin = int(bands.get("ocean_margin_px", 0))
    if _margin > 0 and land.any():
        _rim = np.zeros(land.shape, bool)
        _rim[:_margin, :] = True; _rim[-_margin:, :] = True
        _rim[:, :_margin] = True; _rim[:, -_margin:] = True
        _rk = land & _rim
        if _rk.any():
            masks["height"][_rk] = SEA_RAW; mcy[_rk] = 63.0; land = mcy > 63.4
            print(f"[bake]   ocean_margin_px={_margin}: oceaned {int(_rk.sum())}px of border-rim land", flush=True)
    # remove island land that overwrites MAINLAND land (ocean is fine to overwrite)
    _ox = round(entry["world_offset_px"][0] / 512) * 512
    _oz = round(entry["world_offset_px"][1] / 512) * 512
    _nrem = _remove_over_mainland(masks["height"], mcy, land, _ox, _oz)
    if _nrem:
        print(f"[bake]   removed {_nrem}px of island land over MAINLAND land -> ocean", flush=True)
    # re-snap rotation halo: ocean-ish cells forced below sea so coastline is crisp
    masks["height"][~land] = np.minimum(masks["height"][~land], SEA_RAW)

    cfg = json.loads((ROOT / "config" / "thresholds.json").read_text())
    rock_deg = float(cfg["lithology"]["rock_layers"].get("t1_deg", 38.0))   # align synth rock_gap to rock_layers t1

    override = paint_override(mcy, land, bands, bands["wind"])
    # AUTO real land-cover raster (islands/island_geo_data/<key>/) over the bands —
    # plug-and-play: drop a raster + re-bake. Bands stay the fallback where unmapped.
    try:
        from islands.import_island_raster import load_override_from_geo
        _rov = load_override_from_geo(name, override.shape)
        if _rov is not None:
            _rov[~land] = 0                                   # never paint ocean cells
            override = np.where(_rov > 0, _rov, override)
            print(f"[bake]   real land-cover raster applied ({int((_rov>0).sum())} px)", flush=True)
    except Exception as e:
        print(f"[bake]   raster auto-import skipped: {e}", flush=True)
    # painted biome layer (island_painter manual touch-up) WINS over raster + bands.
    _op = od / "override_painted.tif"
    if _op.exists():
        import rasterio as _rio
        _pov = _rio.open(_op).read(1)
        if _pov.shape == override.shape:
            override = np.where(_pov > 0, _pov, override)
            print(f"[bake]   painted biome override applied ({int((_pov>0).sum())} px)", flush=True)
    # IRISH BLANKET BOG (island-only, terrain-derived): flat + waterlogged ground
    # -> FRESHWATER_FEN (swamp tint, alder/willow carr). Bog isn't in the tropical
    # land-cover, so derive it from slope+flow for the Irish climate. Targets a
    # fraction of land (Ireland is ~20% peatland); overrides pasture/forest on the
    # flattest, wettest ground (drained -> pasture, undrained -> bog).
    bog = np.zeros(land.shape, bool)
    _bog_frac = float(reg.get("bog_frac", 0.0))        # user: NO bog on islands (was 0.15)
    if _bog_frac > 0:
        _deg = masks["slope"].astype(np.float32) / 65535.0 * 45.0
        _flow = masks["flow"].astype(np.float32) / 65535.0
        _flat = land & (_deg < 7.0)
        _ntar = int(_bog_frac * int(land.sum()))
        if int(_flat.sum()) > _ntar > 0:
            _thr = np.partition(_flow[_flat], -_ntar)[-_ntar]      # wettest flat cells
            bog = _flat & (_flow >= _thr)
        else:
            bog = _flat
        override[bog] = 240        # FRESHWATER_FEN
        print(f"[bake]   Irish bog (FRESHWATER_FEN) on {int(bog.sum())} px "
              f"({int(bog.sum())/max(int(land.sum()),1)*100:.0f}% of land)", flush=True)

    rock_gap = synth_rock_gap(masks["slope"], rock_deg, world_offset_px=(_ox, _oz),
                              land=land, max_land_frac=float(bands.get("rock_max_frac", 0.25)))
    snow_gap = synth_snow_gap(mcy, land, bands)
    # wider beach fringe: capture the coastal rise (Y63..71) + gentler-but-not-flat
    # flanks so steep volcanic coasts still get a continuous sand band, not specks.
    shore, beach = synth_shore_beach_wide(land, mcy, masks["slope"],
                                          world_offset_px=(_ox, _oz),
                                          beach_core_width=9.0,
                                          beach_dither_mult=4.0,
                                          gentle_slope_deg=14.0)

    _wtif(od / "height.tif", masks["height"])
    _wtif(od / "slope.tif", masks["slope"])
    _wtif(od / "flow.tif", masks["flow"])
    _wtif(od / "erosion.tif", masks["erosion"])
    _wtif(od / "override.tif", override)
    _wtif(od / "rock_gap.tif", rock_gap)
    _wtif(od / "snow_gap.tif", snow_gap)
    _wtif(od / "shore.tif", shore)
    _wtif(od / "beach.tif", beach)

    # NO-TREE CLEARING (island-only): non-forest land-cover classes ('_notree' in
    # classmap) -> the island's hydro_floodplain.tif (gap==4) so the SHARED
    # eco_gradients force-grasses + suppresses trees there -> green tree-less
    # pasture. Written to od (island folder); never touches masks/ (mainland).
    try:
        from islands.import_island_raster import load_notree_from_geo
        _nt = load_notree_from_geo(name, override.shape)
        if _nt is not None:
            _ntm = (((_nt > 0) & land & ~bog).astype(np.uint8)) * 255   # bog (FEN) is wet, not cleared pasture
            _wtif(od / "hydro_floodplain.tif", _ntm)
            print(f"[bake]   no-tree clearing -> hydro_floodplain on {int((_ntm>0).sum())} px", flush=True)
    except Exception as e:
        print(f"[bake]   no-tree clearing skipped: {e}", flush=True)

    manifest = dict(name=entry["name"], dem=entry["dem_path"],
                    world_offset_px=[int(entry["world_offset_px"][0]), int(entry["world_offset_px"][1])],
                    world_hw=[H, W], land_px=int(land.sum()),
                    peak_m=peak_m, mcy_peak=mcy_peak, rot=rot, flipx=flipx, flipz=flipz)
    (od / "manifest.json").write_text(json.dumps(manifest, indent=2))

    # ---- ROUTE THROUGH THE REAL MAINLAND GENERATOR ----
    # No more synth_lithology / build_island_terrain_derived. The island config is
    # the REAL config/thresholds.json with MINIMAL deltas (the stripped config was
    # what lost feature parity). Then the REAL builders run against this island's
    # masks dir at the island footprint -> rock tiers / talus / cliff_cap / aspect /
    # bedrock / lithology all fire identically to the mainland.
    snowy = bands["snow"] < 1.0
    isl_litho = bands.get("litho", "temperate_basaltic")          # per-island real-world geology (default grey volcanic)
    cfg["lithology"]["zone_to_group"] = {z: isl_litho for z in cfg["lithology"]["zone_to_group"]}
    print(f"[bake]   lithology group = {isl_litho} (whole island)", flush=True)
    # S95 #3 + mossy-palette-target: island-only mossy_temperate repaint (tropical
    # basalt geology). The VISIBLE rock surface on islands comes from _apply_rock_layers
    # reading lithology.rock_layers.groups.<g> tiers (rock_layers.enabled=TRUE on
    # islands), NOT lithology.groups.<g>.palette. The mossy dark tier (split 75%, the
    # dominant read) was [tuff, pale_moss_block]; the WASH comes from
    # lithology.groups.<g>.wash_palette. User directive: rock palette tuff->smooth_basalt,
    # pale_moss_block->deepslate, then make pale_moss & tuff the new WASH. Every target
    # block is already a mainland-validated rock/wash block (smooth_basalt =
    # arid/temperate_basaltic.dark; deepslate = arid_basaltic.mid; pale_moss_block =
    # deepslate_metamorphic.wash) so this COPIES mainland palettes. cfg here is a fresh
    # per-island json.loads of config/thresholds.json (line ~424) -> ISLAND-GATED, the
    # on-disk mainland config is never written.
    if isl_litho == "mossy_temperate":
        _rl_g = cfg.get("lithology", {}).get("rock_layers", {}).get("groups", {}).get("mossy_temperate")
        if _rl_g is not None:
            # dark = the dominant visible rock tier (split 75%). tuff->smooth_basalt,
            # pale_moss_block->deepslate. mid/light tiers (mossy_cobblestone/moss_block)
            # are left as-is so the steeper bands keep their wet-moss read.
            _swap = {"tuff": "smooth_basalt", "pale_moss_block": "deepslate"}
            for _tier in ("dark", "mid", "light"):
                _pal = _rl_g.get(_tier)
                if _pal:
                    _rl_g[_tier] = [_swap.get(b, b) for b in _pal]
            print(f"[bake]   mossy_temperate rock tiers -> {_rl_g.get('dark')} (island-only)", flush=True)
        _mt = cfg.get("lithology", {}).get("groups", {}).get("mossy_temperate")
        if _mt is not None:
            # WASH = the two blocks the rock used to be (pale_moss + tuff) so the drainage
            # channels now read as the OLD mossy texture against the new basalt cliffs.
            _mt["wash_palette"] = ["pale_moss_block", "tuff"]
            print("[bake]   mossy_temperate wash -> pale_moss_block/tuff (island-only)", flush=True)
    cfg.setdefault("lithology", {}).setdefault("rock_layers", {})["suppress_all_gap5_trees"] = True  # E: islands' broad monotone-slope rock_gap -> NO trees on gap==5 (mainland keeps the S89 dark-tier sparse-tree exemption byte-for-byte)
    cfg.setdefault("gaea_gaps", {})["use_query_time"] = False     # no 8k Gaea -> baked rock/snow_gap drive gaps
    cfg.setdefault("eco_gradients", {}).setdefault("beach_gap", {})["from_mask"] = True   # S95-T2: island sand <- baked beach.tif
    cfg["eco_gradients"]["beach_gap"]["max_surface_y"] = 80        # S95-T2: wide island beach climbs above the mainland Y65 contour
    cfg["eco_gradients"]["island_treeline_breaks"] = {            # S95-polish: climate-aware forest-edge meadow fingers near the island treeline (boreal breaks hard, jungle barely)
        "enabled": True, "band_lo": 0.45, "wind_mix": 0.65, "meadow_freq_boost": 0.8,
        "fringe_concavity_gain": 0.5, "fringe_wind_gain": 1.0, "climate_default": 0.6,
        "climate_by_biome": {
            "LUSH_RAINFOREST_COAST": 0.25, "TIDAL_JUNGLE_FRINGE": 0.25,
            "RAINFOREST_COAST": 0.3, "MANGROVE_COAST": 0.2,
            "TEMPERATE_DECIDUOUS": 0.7, "MIXED_FOREST": 0.7, "BIRCH_FOREST": 0.7,
            "TEMPERATE_RAINFOREST": 0.6, "COASTAL_HEATH": 0.8,
            "BOREAL_TAIGA": 1.0, "SNOWY_BOREAL_TAIGA": 1.0, "BOREAL_ALPINE": 1.0,
        },
        "biome_meadow_freq": {
            "TEMPERATE_DECIDUOUS": 0.18, "MIXED_FOREST": 0.16, "BIRCH_FOREST": 0.16,
            "BOREAL_TAIGA": 0.18, "SNOWY_BOREAL_TAIGA": 0.10, "BOREAL_ALPINE": 0.18,
            "COASTAL_HEATH": 0.20, "TEMPERATE_RAINFOREST": 0.06,
            "LUSH_RAINFOREST_COAST": 0.05, "TIDAL_JUNGLE_FRINGE": 0.05,
            "RAINFOREST_COAST": 0.05, "MANGROVE_COAST": 0.04,
        }}
    cfg.setdefault("ocean", {})["seam_safe_noise"] = True          # S95-T3: continuous ocean-floor noise across tile seams
    cfg.setdefault("sand_dune_smoothing", {})["seam_symmetric"] = False  # S95 REVERTED: seam_symmetric=True is CATASTROPHIC on islands (efate harness A/B: interior seam mean|dY| 1.74 -> 22-42 w/ 100-block cliffs). The symmetric pre-carve-blend path references a field that is garbage at the island seam. Default iterative-blur path (False) keeps the faint ~1.7 seam.
    cfg["sand_dune_smoothing"]["full_halo"] = True  # S96 interior-seam fix: blend toward a full-halo LIVE-Y field so the iterative blend can't desync each tile-side to its own interior mean. Island-gated (key absent in mainland config -> default False at surface_decorator.py:2919 -> mainland byte-identical). De-risked offline before render (NOT the broken seam_symmetric pre-carve path).
    cfg.setdefault("flow_erosion", {})["rock_only_sea_clamp"] = True  # S97 ARCHIPELAGO-FLOOD fix: apply_flow_erosion's final `clip(sy2, SEA_LEVEL+1, ..)` was raising EVERY cell (incl the flat below-sea ocean shelf) to Y64 on any tile that merely contained rock -> dense archipelagos (Grenadines/Kostati/NV) flooded to solid land. The flag scopes that sea floor to ROCK cells only (the cells `dy` touched), so the inter-island ocean keeps its seabed. Island-gated (key absent in mainland config -> default False at flow_erosion.py -> mainland byte-identical whole-array clip).
    # S95 #7: calmer mangrove/tropical coasts (island delta only; mainland reefs +
    # mangroves unchanged). Cut the coral scatter (user: "weird coral") and shrink
    # the underwater mangrove mud apron (user: "too much mud"). The land-surface mud
    # palette taste + a dead-tree species filter are deferred to the walk.
    cfg["ocean"].setdefault("features", {}).setdefault("coral", {})["probability"] = 0.0003
    cfg["ocean"].setdefault("biome_variants", {}).setdefault("MANGROVE_COAST", {})["max_reach_blocks"] = 12
    cfg.setdefault("snow_physics", {})["enabled"] = bool(snowy)
    cfg["snow_physics"].setdefault("depth", {})["enabled"] = bool(snowy)
    cfg.setdefault("river_carving", {}).update(                   # rivers/lakes OFF (hand-painted, not carved)
        {"stream_threshold": 2.0, "river_threshold": 2.0, "lake_threshold": 2.0})
    cfg.setdefault("hydrology", {}).update(
        {"flow_river_threshold": 2.0, "flow_wetland_threshold": 2.0})
    cfg.setdefault("hydrology_engine", {})["min_stream_flow"] = 2.0
    # WASH handling: NO per-island re-cal. The island flow.tif is now LINEAR
    # band-pass like Gaea (derive flow_to_uint16), so the mainland wash knobs
    # (washes.min_flow=0.003, width_max) fire thin drainage channels on islands
    # identically to the mainland -> dark/mid/light rock_layers tiers read
    # through. The old _WASH_ROCK_COVERAGE percentile band-aid is deleted.
    isl_cfg = od / "thresholds_island.json"
    isl_cfg.write_text(json.dumps(cfg))

    import subprocess as _sp
    _common = ["--masks", str(od), "--config", str(isl_cfg)]
    print(f"[bake]   routing through real builders (lithology + terrain_derived, WS={W})...", flush=True)
    _sp.run([sys.executable, str(ROOT / "tools" / "build_lithology.py"), *_common], check=True)
    _lp = od / "lithology_painted.tif"                            # painted litho wins over the default
    if _lp.exists():
        import rasterio as _rio
        l8 = _rio.open(od / "lithology.tif").read(1)
        pl = _rio.open(_lp).read(1)[::8, ::8]
        ph, pw = min(l8.shape[0], pl.shape[0]), min(l8.shape[1], pl.shape[1])
        l8[:ph, :pw] = np.where(pl[:ph, :pw] > 0, pl[:ph, :pw], l8[:ph, :pw])
        _wtif(od / "lithology.tif", l8)
        print(f"[bake]   painted lithology applied over temperate_basaltic default", flush=True)
    _sp.run([sys.executable, str(ROOT / "tools" / "build_terrain_derived.py"),
             *_common, "--world-size", str(W), "--scale", "4"], check=True)
    print(f"[bake]   wrote {od}  bbox={W}x{H} land={int(land.sum())}px ({land.mean()*100:.1f}%)", flush=True)
    return manifest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bake"); ap.add_argument("--render"); ap.add_argument("--list", action="store_true")
    ap.add_argument("--threads", type=int, default=6)
    # S95-fix: production islands render WITH schematics by default, matching the
    # mainland (which always places trees). --fast skips them (RAM-saver for the
    # 7.4GB box; trees were the cause of the "no trees on islands" report).
    ap.add_argument("--fast", action="store_true",
                    help="skip schematics (RAM-saver); default places trees to match the mainland")
    a = ap.parse_args()
    layout = json.loads((ISL / "layout.json").read_text())
    islands = layout["islands"]
    if a.list:
        for i in islands:
            print(f"  {safe_name(i['name']):26} key={_key(i['dem_path'])}  off={i['world_offset_px']}")
        return
    if a.bake:
        sel = islands if a.bake == "all" else [i for i in islands if a.bake in safe_name(i["name"]) or a.bake in i["dem_path"]]
        for i in sel:
            bake_island(i)
    if a.render:
        from islands.render_drive import render_island   # built next
        sel = islands if a.render == "all" else [i for i in islands if a.render in safe_name(i["name"]) or a.render in i["dem_path"]]
        for i in sel:
            render_island(i, threads=a.threads, fast=a.fast)


if __name__ == "__main__":
    main()
