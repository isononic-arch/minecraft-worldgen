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
  "-17_622": A(bands=[(0.0,20),(0.10,120),(0.55,60)],          snow=1.5,  rock=28, wind=0.15, litho="mossy_temperate"),  # Efate — S99 climate remap: western-ocean westerly windward = warm-temperate Atlantic (TEMP_RAINFOREST coast → MIXED → DECIDUOUS), no tropics outside the Kostati archipelago
  "12_445":  A(bands=[(0.0,230),(0.05,160),(0.55,200)],        snow=1.5,  rock=30, wind=0.10, litho="mossy_temperate"),  # Grenadines — wet windward LUSH, dry leeward (S98: maquis 210->200 SEMI_ARID brushy)
  "49_722":  A(bands=[(0.0,10),(0.10,30),(0.55,35)],           snow=0.62, rock=30, wind=0.0,  litho="deepslate_metamorphic"),  # Fogo Island NL — boreal cold; Precambrian Canadian-Shield gneiss (northernmost; Jan Mayen deleted)
  # ── metamorphic massif → deepslate_metamorphic ──────────────────────────────
  "11_060":  A(bands=[(0.0,150),(0.10,200),(0.45,200),(0.78,90)],snow=1.5,rock=29, wind=-0.10,litho="deepslate_metamorphic", keep_box=[0.45,1.0,0.0,1.0]),# Margarita (S98: maquis 210->200 SEMI_ARID brushy) — dry continental schist/gneiss (Cerro El Copey). keep_box x>0.45: real Isla Margarita (BOTH east + Macanao west lobes are ONE connected 6.2Mpx component spanning 4238 blocks at cx0.73) sits in the right two-thirds; the crop also catches a SEPARATE 2.96Mpx off-frame mainland-coast landmass at cx0.25 whose centroid is 4085 blocks (0.34 of the bbox-diagonal) west of the real island — the v6 "land cutoff" slab. x>0.45 oceans it + keeps Coche/Cubagua (cx0.74-0.95) and the real island intact.
  # ── carbonate / coral / marble karst → limestone ────────────────────────────
  "-50_393": A(bands=[(0.0,70),(0.10,20),(0.45,100),(0.75,30)],snow=0.72, rock=28, wind=0.10, litho="limestone", keep_box=[0.0,0.80,0.0,0.80], skip_erase=True, ocean_margin_px=32),# Madre de Dios — fjord/marble KARST. keep_box (centroid x<0.80 & y<0.80) keeps the THREE real fjord islands (white center-mass cx0.53 / green lower-left cx0.44 / fragmented medium island cx0.56-0.67) and oceans the unwanted mainland-coast cluster (cx>0.83) + two bottom-center blobs (cy0.88/0.91). Replaces frag_cut_east=0.75 which leaked ~298kpx (junk straddled the 0.75 col) and could not be fixed by frag_keep_largest=3 (the largest junk blob 2.9Mpx outranks the fragmented real medium island, largest fragment 1.43Mpx).
  "18_299":  A(bands=[(0.0,150),(0.12,200),(0.55,220)],        snow=1.5,  rock=30, wind=0.05, litho="limestone"),           # Anguilla/St Maarten — limestone (S98: maquis 210->200 SEMI_ARID brushy)
  "10_941":  A(bands=[(0.0,9),(0.06,200)],                     snow=1.5,  rock=34, wind=-0.10,litho="limestone", stone_to_sand=True),           # La Tortuga — arid carbonate: SEMI_ARID_SHRUBLAND only; NO SAND_DUNE_DESERT(170) -> no dune columns. Beaches via beach.tif gap==9 (sentinel 9 -> next band 200)
  "21_395":  A(bands=[(0.0,9),(0.06,200),(0.40,150)],          snow=1.5,  rock=34, wind=-0.05,litho="limestone", height_gain=9.0, lift_cap_y=76.0, keep_box=[0.0,1.0,0.34,1.0], stone_to_sand=True),# Grand Turk — dry coral; VERY flat DEM -> bigger gain to clear sea (median land Y64.4). S104: lift_cap_y 74->76 + rolling dome (lift_roll_amp default 0.45) + raised spline = gentle Y65-74 undulation (V13 "super flat" fix). keep_box y>0.34: the height_gain=9 inflates the shallow Caicos Bank, and one inflated wedge forms a hard-edged TRIANGULAR slab (the v6 upper "land cutoff", single 96kpx component, straight diagonal hypotenuse, centroid cy0.30) that reads as junk vs the lumpy/fractal real cays (all at cy>0.37). y>0.34 oceans the triangle + a thin northern bank strip (cy0.24) and keeps Grand Turk + every real cay cluster.
  "23_887":  A(bands=[(0.0,9),(0.06,150),(0.40,200)],          snow=1.5,  rock=34, wind=0.0,  litho="limestone", height_gain=5.0, lift_cap_y=78.0),# Bahamas — coral platform; spline mapped land to Y64 pancake ("nO BAHAMAS") -> gain lifts interior. S104: lift_cap_y 74->78 SOFT cap (V13 audit: the hard clamp mesa'd the tops at exactly Y74) + rolling dome + raised spline -> rolling Y65-77 interior
  "11_863":  A(bands=[(0.0,230),(0.10,220)],                   snow=1.5,  rock=34, wind=0.0,  litho="limestone"),           # Los Roques — coral atoll
  "-20_529": A(bands=[(0.0,230),(0.10,220)],                   snow=1.5,  rock=34, wind=0.0,  litho="limestone"),           # Ouvea — raised coral atoll
  "-21_008": A(bands=[(0.0,9),(0.08,150),(0.40,200)],          snow=1.5,  rock=33, wind=0.0,  litho="limestone", stone_to_sand=True),  # Loyalty — raised coral limestone (S98: stone->sand + maquis 210->200 SEMI_ARID brushy)
}
DEFAULT_BAND = A(bands=[(0.0,9),(0.06,150),(0.4,200)], snow=1.5, rock=30, wind=0.0, litho="temperate_basaltic")  # S98: maquis 210->200 SEMI_ARID
VEX = 1.5   # vertical exaggeration of the no-override fallback peak (1.0 = geographically true 1:1;
            # islands/spline_overrides.json overrides this per-island via the spline editor)

# S98 island polish — dead coral that paints on LAND (palette bug) and the names of the
# "scree" talus blocks the user wants softened to natural soil.
_DEAD_CORAL = {"dead_bubble_coral_block", "dead_fire_coral_block", "dead_tube_coral_block",
               "dead_horn_coral_block", "dead_brain_coral_block"}
_CORAL_REPL = {"granitic": "andesite", "limestone": "andesite", "temperate_basaltic": "deepslate",
               "mossy_temperate": "tuff", "arid_basaltic": "basalt", "deepslate_metamorphic": "andesite"}
_TALUS_SOFTEN = {"suspicious_gravel": "gravel", "packed_mud": "coarse_dirt"}
_JM_BIOMES = ("MANGROVE_COAST", "TIDAL_JUNGLE_FRINGE", "LUSH_RAINFOREST_COAST")


def apply_island_polish(cfg):
    """S98 ISLAND-ONLY config deltas (written into thresholds_island.json, NOT the shared
    config/thresholds.json -> the S94-validated mainland render stays byte-identical):
      #3a de-coral: dead-coral palette entries -> stone-family (coral-on-land is a bug);
      #3b soften the talus 'scree' apron (suspicious_gravel/packed_mud -> gravel/coarse_dirt);
      #5  jungle/mangrove mud -> 40% moss / 20% grass / 25% podzol / 10% coarse_dirt, veg maxed.
    Idempotent (safe to re-apply to an already-patched thresholds_island.json)."""
    groups = cfg.get("lithology", {}).get("groups", {})
    for gname, g in groups.items():
        rep = _CORAL_REPL.get(gname, "stone")
        for pk in ("palette", "cap_palette", "concavity_palette", "wash_palette",
                   "talus_palette", "varnish_palette", "bedrock_drainage_palette"):
            lst = g.get(pk)
            if isinstance(lst, list):
                lst = [rep if b in _DEAD_CORAL else b for b in lst]
                if pk == "talus_palette":
                    lst = [_TALUS_SOFTEN.get(b, b) for b in lst]
                g[pk] = lst
        for band in ("strata",):  # strata band primaries can carry coral too
            sb = g.get(band)
            if isinstance(sb, dict):
                for bk in ("band_a", "band_b"):
                    bb = sb.get(bk, {})
                    for key in ("primary", "secondary"):
                        if bb.get(key) in _DEAD_CORAL:
                            bb[key] = rep
    # #5 mud swap (faithful redistribution): base fill -> moss; overpaint grass/podzol/coarse;
    # keep a little mud for wet patches. Approx 40/20/25/10/(5 mud).
    nlb = cfg.setdefault("noise_layers_biome", {})
    for biome in _JM_BIOMES:
        layers = nlb.get(biome)
        if not layers:
            continue
        seen_base = False
        for l in layers:
            if l.get("block") == "mud":
                if l.get("is_base") and not seen_base:
                    l["block"] = "moss_block"; l["sub"] = "dirt"; l["name"] = "moss (S98 mud-swap base)"
                    seen_base = True
                else:
                    l["block"] = "coarse_dirt"; l["coverage"] = 0.10; l["name"] = "mud->coarse (S98)"
        # add the grass/podzol overpaint layers if not already present (idempotent by name)
        names = {l.get("name") for l in layers}
        for blk, cov, nm in (("grass_block", 0.20, "grass (S98 mud-swap)"),
                             ("podzol", 0.25, "podzol (S98 mud-swap)"),
                             ("mud", 0.10, "mud wet-patch (S98)")):
            if nm not in names:
                layers.append({"name": nm, "noise": "simplex_fbm", "enabled": True,
                               "block": blk, "sub": "dirt", "coverage": cov, "scale": 28,
                               "seed": 4242, "is_base": False})
        nlb[biome] = layers
    # #5 veg density (island-only; BASE_DENSITY is shared code, radius_mult is config).
    # S98 walk: dense but slightly DIALED BACK from max (user). Plus tight bush packing
    # for the now-treeless SEMI_ARID_SHRUBLAND so the brushy fill reads (small radius ->
    # small bush exclusion -> bushes pack shoulder-to-shoulder).
    rm = cfg.setdefault("tree_spacing", {}).setdefault("radius_mult_by_biome", {})
    for biome in _JM_BIOMES:                 # MANGROVE_COAST, TIDAL_JUNGLE_FRINGE, LUSH_RAINFOREST_COAST
        rm[biome] = 0.5
    rm["SEMI_ARID_SHRUBLAND"] = 0.18   # S98: tighter -> radius//2 hits 0 -> bushes pack adjacent
    # #10 atoll mud/coral: DISABLE the MANGROVE_COAST ocean variant entirely so the
    # seabed near mangrove/atoll coasts uses the standard ocean palette (user: "default
    # to whatever the beach is" — kills the brackish mud + dead-coral override). Island-only.
    cfg.setdefault("ocean", {}).setdefault("biome_variants", {}).setdefault("MANGROVE_COAST", {})["enabled"] = False
    return cfg


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

def _near_ocean_disk(land, radius):
    """EXACT memory-lean equivalent of `distance_transform_edt(land) <= radius`
    (S101): a cell's EDT to the nearest ~land cell is <= r iff a ~land cell lies
    within the CLOSED Euclidean disk of radius r, i.e. binary_dilation of ~land
    with the exact disk footprint {dx^2+dz^2 <= r^2}. Byte-identical output;
    scipy's EDT allocates ~2.7GB of int32/float64 internals on a 10.9k^2 bbox
    (OOM'd the 7.4GB local bake box at NV), the dilation peaks at ~3 bool copies."""
    from scipy.ndimage import binary_dilation
    r = int(radius)
    yy, xx = np.mgrid[-r:r + 1, -r:r + 1]
    disk = (yy * yy + xx * xx) <= r * r
    return binary_dilation(~land, structure=disk)


# ── S101 EDGE DEPTH TAPER (bake-time) ────────────────────────────────────────
# Five islands' real DEMs carry shallow banks (Caicos Bank ~5-15 blocks deep) that
# run to the edge of the RENDER FOOTPRINT (content tiles + 1 buffer ring, see
# render_drive._content_tiles). Where the bank meets that boundary, the world shows
# a 50-80-block vertical underwater wall on a dead-straight tile line, dropping to
# the ocean-generator fill (seabed -30..-14, cap Y-14) / prerendered mainland floor
# (~Y-17). Fix at BAKE time: blend OCEAN cells within TAPER_BAND_PX of the render
# boundary DOWN toward TAPER_TARGET_MCY with a smoothstep ease, hitting the target
# exactly AT the boundary. Never raises terrain, never touches land, never touches
# cells already at/below the target. ISLAND-ONLY (bake path; mainland untouched).
TAPER_BAND_PX = 320        # band width in px; per-island override: BANDS key `taper_band_px` (0 = off)
TAPER_TARGET_MCY = -17.0   # deep target = prerendered mainland ocean floor ~Y-17 (raw via mcy_to_world_raw, NOT hardcoded)
TAPER_BUFFER_TILES = 1     # MUST mirror render_drive.render_island(buffer_tiles=1) / CLI --buffer default
_TAPER_TILE = 512          # render_drive.TILE


def _footprint_tiles_from_height(height, apron_seed_min_px=0, buffer_tiles=TAPER_BUFFER_TILES):
    """Replicate render_drive._content_tiles for an IN-MEMORY height array (the drive
    reads the WRITTEN height.tif; the taper runs before the write, but it only DEEPENS
    ocean cells — the drive's land test `> SEA_RAW+40` is unaffected, so the tile set
    computed here pre-taper == the set the drive computes post-taper). Returns a
    (nty, ntx) bool grid of rendered tiles: every tile with >=1 land px, PLUS a
    `buffer_tiles` Chebyshev ring around tiles holding >= apron_seed_min_px land px
    (sub-threshold slivers render but do NOT seed the apron — Madre's frame noise)."""
    H, W = height.shape
    landpx = height > (SEA_RAW + 40)
    cnt = np.add.reduceat(np.add.reduceat(landpx.astype(np.int64),
                                          np.arange(0, H, _TAPER_TILE), axis=0),
                          np.arange(0, W, _TAPER_TILE), axis=1)
    land_t = cnt > 0
    seed_t = land_t & (cnt >= int(apron_seed_min_px))
    if buffer_tiles <= 0:
        return land_t
    from scipy.ndimage import binary_dilation, binary_fill_holes
    k = 2 * int(buffer_tiles) + 1
    # S101b: FILL ENCLOSED HOLES — an all-ocean tile fully surrounded by rendered
    # tiles (Ouvea's atoll-lagoon center) must RENDER from the DEM (shallow
    # lagoon), not fall to the deep generator fill with a taper ring around it.
    # Mirrors render_drive._content_tiles + world_map_baked (same S101b rule).
    return binary_fill_holes(land_t | binary_dilation(seed_t, structure=np.ones((k, k), bool)))


def _edge_distance_field(sel_t, H, W, band):
    """Exact per-pixel Euclidean distance (pixel-center metric, adjacent cell = 1.0)
    from every cell to the nearest cell OUTSIDE the render footprint, where outside
    = non-rendered tiles inside the bbox + everything beyond the bbox frame (the
    footprint is clipped to the bbox, so the frame IS a render boundary wherever a
    rendered tile touches it). MEMORY-LEAN + EXACT without scipy's EDT (whose int32/
    float64 internals cost ~2.7GB on a 10.9k^2 bbox — the S101 OOM lesson at
    _near_ocean_disk): the outside region is a union of 512-aligned RECTANGLES, so
    d = min(analytic frame ramp, per-rect point-to-rect distance), and only outside
    rects 8-adjacent to a rendered tile can contain the nearest outside cell (any
    nearer cell would have a rendered neighbour). One full float32 array total."""
    from scipy.ndimage import binary_dilation
    rows = np.arange(H, dtype=np.float32)[:, None]
    cols = np.arange(W, dtype=np.float32)[None, :]
    d = np.minimum(np.minimum(rows + 1.0, np.float32(H) - rows),
                   np.minimum(cols + 1.0, np.float32(W) - cols))   # (H,W) float32 frame ramp
    near_out = (~sel_t) & binary_dilation(sel_t, structure=np.ones((3, 3), bool))
    pad = int(math.ceil(band)) + 2
    for ty, tx in zip(*np.nonzero(near_out)):
        r0, r1 = ty * _TAPER_TILE, min((ty + 1) * _TAPER_TILE, H)
        c0, c1 = tx * _TAPER_TILE, min((tx + 1) * _TAPER_TILE, W)
        wr0, wr1 = max(r0 - pad, 0), min(r1 + pad, H)
        wc0, wc1 = max(c0 - pad, 0), min(c1 + pad, W)
        rr = np.arange(wr0, wr1, dtype=np.float32)[:, None]
        cc = np.arange(wc0, wc1, dtype=np.float32)[None, :]
        dr = np.maximum(np.maximum(np.float32(r0) - rr, rr - np.float32(r1 - 1)), 0.0)
        dc = np.maximum(np.maximum(np.float32(c0) - cc, cc - np.float32(c1 - 1)), 0.0)
        np.minimum(d[wr0:wr1, wc0:wc1], np.sqrt(dr * dr + dc * dc),
                   out=d[wr0:wr1, wc0:wc1])
    return d


def _expand_tiles_to_px(sel_t, H, W):
    return np.repeat(np.repeat(sel_t, _TAPER_TILE, axis=0), _TAPER_TILE, axis=1)[:H, :W]


def _apply_edge_depth_taper(height, mcy, land, band_px, target_mcy, apron_seed_min_px):
    """Mutates height (uint16 raw) + mcy in place on OCEAN cells within `band_px` of
    the render-footprint boundary: mcy_new = mcy - (mcy - target)*w, w = smoothstep of
    t = (band - d)/(band - 1) so w=1 (exact target) at the boundary ring (d=1) and
    w=0 at the inner band edge. Gates: rendered-footprint px only, ~land only (after
    the bake's re-snap, ~land <=> raw <= SEA_RAW; this deliberately INCLUDES cells at
    exactly SEA_RAW — frag-kill/keep_box slabs are snapped to SEA_RAW = Y63 and are
    the TALLEST walls, e.g. Grand Turk's oceaned bank triangle at 80 blocks above the
    deep floor), and only cells above the target (never raise, never touch land).
    Returns a stats dict for the manifest, or None if the island has no land."""
    H, W = height.shape
    sel_t = _footprint_tiles_from_height(height, apron_seed_min_px)
    if not sel_t.any():
        return None
    band = float(band_px)
    d = _edge_distance_field(sel_t, H, W, band)
    rend = _expand_tiles_to_px(sel_t, H, W)
    # S101c: ALL pre-taper Y values below derive from the RAW height, NOT the
    # caller's mcy — the bake's re-snap (masks['height'][~land]=min(.,SEA_RAW))
    # mutates height WITHOUT updating mcy, so gray-zone cells (mcy 63..63.4,
    # snapped to raw 17050) carry a stale mcy up to 0.4 above their true Y63.
    # v1 wrote the taper output from that stale mcy, which RE-RAISED snapped
    # cells (raw 17050 -> up to 17145) wherever w~0, pushing 390 px above the
    # drive's land test (raw 17090) on Grand Turk -> 6 phantom content tiles ->
    # the render boundary MOVED off the tapered edge. Raw-domain gating is also
    # exact: the spline is monotone, so raw > target_raw <=> mcy > target.
    tgt_raw = int(mcy_to_world_raw(np.array([target_mcy]))[0])
    ring = rend & (d < 1.5)                       # outermost rendered ring (d=1 or sqrt2)
    ring_oc = ring & ~land
    pre_max_above = (float(np.max(raw_to_mcy(height[ring_oc]) - target_mcy))
                     if ring_oc.any() else 0.0)
    pre_n_above3 = (int(np.sum(raw_to_mcy(height[ring_oc]) > target_mcy + 3.0))
                    if ring_oc.any() else 0)
    n_land_ring = int((ring & land).sum())
    selm = d <= band
    selm &= rend
    n_land_band = int(np.sum(selm & land))        # land inside the band: NEVER touched, report only
    selm &= ~land
    selm &= height > tgt_raw
    del rend, ring, ring_oc
    n_sel = int(selm.sum())
    if n_sel:
        dsel = d[selm]
        del d
        t = np.clip((band - dsel) / max(band - 1.0, 1.0), 0.0, 1.0)
        w = t * t * (3.0 - 2.0 * t)               # smoothstep: C1 at both band edges
        del dsel, t
        mcy_sel = raw_to_mcy(height[selm])
        mcy_new = mcy_sel - (mcy_sel - float(target_mcy)) * w
        height[selm] = mcy_to_world_raw(mcy_new)
        mcy[selm] = mcy_new
        max_deep = float(np.max(mcy_sel - mcy_new))
        del w, mcy_sel, mcy_new
    else:
        del d
        max_deep = 0.0
    del selm
    return dict(band_px=band, target_mcy=float(target_mcy),
                target_raw=int(mcy_to_world_raw(np.array([target_mcy]))[0]),
                buffer_tiles=TAPER_BUFFER_TILES, apron_seed_min_px=int(apron_seed_min_px),
                tapered_px=n_sel, max_deepened_blocks=round(max_deep, 2),
                ring_ocean_max_above_target_before=round(pre_max_above, 2),
                ring_ocean_n_above3_before=pre_n_above3,
                land_px_on_ring=n_land_ring, land_px_in_band=n_land_band)


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
    shore = (land & _near_ocean_disk(land, 6)).astype(np.uint8)
    return shore, beach


def _coarse_world_noise01(H, W, ox, oz, cell, salt):
    """Low-frequency organic noise in [0,1]: splitmix64 hash on a WORLD-ALIGNED
    lattice (node id = world_px // cell), bilinear-upscaled to full res.
    Deterministic + offset-stable: ox/oz are the SNAPPED island offsets (multiples
    of 512) and cell must divide 512, so lattice nodes sit on fixed world
    coordinates -> re-bakes and any future tiled re-baker agree at every pixel.
    MEMORY-LEAN (S101b discipline): the hash runs on the tiny (H/cell, W/cell)
    lattice; only the final bilinear upscale materializes ONE full float32 array
    (the full-res hash + gaussian pipeline it replaces held 2-3)."""
    from scipy.ndimage import zoom
    nz = H // cell + 2
    nx = W // cell + 2
    gx = (np.int64(ox) // cell + np.arange(nx, dtype=np.int64))[None, :].astype(np.uint64)
    gz = (np.int64(oz) // cell + np.arange(nz, dtype=np.int64))[:, None].astype(np.uint64)
    s64 = np.uint64((salt * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF)
    h = (gx * np.uint64(0x9E3779B97F4A7C15) + gz * np.uint64(0xBF58476D1CE4E5B9) + s64)
    h = (h ^ (h >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
    h = (h ^ (h >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
    h = h ^ (h >> np.uint64(31))
    grid = (h.astype(np.float64) / np.float64(np.iinfo(np.uint64).max)).astype(np.float32)
    return zoom(grid, cell, order=1, prefilter=False)[:H, :W]


def synth_shore_beach_wide(land, mcy, slope_u16, world_offset_px=(0, 0),
                           beach_core_width=6.0, beach_dither_mult=4.0,
                           gentle_slope_deg=14.0,
                           beach_max_width=8.0, flat_slope_deg=6.0,
                           steep_slope_deg=20.0, width_jitter=0.20,
                           width_mod_amp=0.45, width_mod_cell=64,
                           max_elev_blocks=7.0, min_pond_radius_px=6):
    """S104 BEACH REDO — slope-driven realistic shoreline (replaces the S95-T2
    core+wide-dithered-apron recipe, which read wrong: a fixed ~9-block always-sand
    core hugged EVERY coast — including steep volcanic headlands — plus a salt-and-
    pepper dithered apron out to ~45 blocks that scattered sand far inland and up
    gentle cliffs. Real coasts don't work that way: a flat bay grows a wide beach,
    a steep rocky headland has thin-to-no beach with rock running to the water.)

    NEW MODEL — beach WIDTH is a smooth function of the COASTAL slope, and the beach
    is a CONTINUOUS strip from the waterline inward (no per-pixel dither coin, so no
    speckle):
      1. dist_from_ocean = EDT into land (blocks from the true waterline).
      2. coastal slope: the true monotone island slope (deg = slope_u16/65535*45,
         confirmed TRUE-monotone for islands) lightly blurred (sigma=2) to a coherent
         "how steep is this stretch of shore" read, then PROPAGATED inland from the
         waterline by grey-erosion running-minimum (memory-lean; see code comment)
         so the whole band inherits the slope OF ITS COAST — not the cell's own
         inland slope, which climbs going up and would wrongly starve a wide flat
         beach the moment the land began to rise.
      3. width(slope) = beach_max_width * smoothstep(flat_slope_deg .. steep_slope_deg,
         inverted) -> full width on flat shore (<=flat_slope_deg), tapering to 0 on
         steep shore (>=steep_slope_deg). Steep volcanic/karst coasts get NO sand.
      4. ALONG-SHORE width modulation, two smooth world-coord lattice-noise
         scales (NO per-pixel coin -> no speckle, ever):
           - bay-scale swell/pinch: cell=width_mod_cell (64) noise, +-width_mod_amp
             (45%) -> the band organically widens and narrows over ~100-300-block
             stretches of coast (V13 walk: old width was near-uniform);
           - fine edge wobble: cell=16 noise, +-width_jitter (20%) -> the inner
             edge is naturally ragged at the 10-30-block scale.
      5. ELEVATION GATE: sand only where mcy <= 63 + max_elev_blocks. The V13
         audit measured old sand up to 8-25 blocks ABOVE sea (climbing coastal
         rises); the gate stops the band at the toe of any rise.
      6. PINHOLE-POND suppression: only the sea + large inland water bodies
         (inradius > min_pond_radius_px, i.e. real lagoons/salinas) anchor
         sand; 1-15px DEM pinholes no longer grow doughnut rims.
      7. beach = 1<=dist<=width & elev-gate. Clean continuous sand at the
         waterline.

    Deterministic + offset-stable: all noise hashes WORLD-aligned lattice nodes
    (ox,oz = the SNAPPED island offset _ox/_oz, multiples of 512; cells divide
    512). beach.tif drives gap==9 downstream via the config-gated from_mask path
    in eco_gradients; shore stays dist<=6 (feeds the column_generator near-water
    hard band, kept for mainland parity).

    Back-compat: beach_core_width/beach_dither_mult/gentle_slope_deg are still
    accepted (legacy callers) — beach_core_width seeds beach_max_width only if
    beach_max_width is falsy. Mainland is UNTOUCHED (this function has exactly
    one caller, the island bake)."""
    from scipy.ndimage import distance_transform_edt, gaussian_filter
    ox, oz = int(world_offset_px[0]), int(world_offset_px[1])
    H, W = land.shape
    # waterline at MC Y63 (strict): the passed `land` is mcy>63.4 and the final
    # height.tif re-snaps ocean to <=SEA_RAW with the SAME >63.4 test, so anchoring
    # the beach to mcy>63.4 lands the first sand ring on the first RENDER-land ring
    # (water meets sand; a >=63.0 anchor would sit one ring seaward = bare shore).
    beach_land = mcy > 63.4
    ocean = ~beach_land
    if not ocean.any() or not beach_land.any():
        shore = (land & _near_ocean_disk(land, 6)).astype(np.uint8)
        return shore, np.zeros((H, W), np.uint8)
    # S104b PINHOLE-POND FIX (V13 after-preview): 1-15px inland mcy pinholes
    # (DEM noise + micro salinas) each seeded a sand DOUGHNUT ring — read as
    # acne on la_tortuga/efate flats. Anchor beaches only to the SEA + LARGE
    # inland water (real lagoons/salinas, e.g. Ouvea's atoll lagoon): geodesic
    # reconstruction (erosion seed + binary_propagation, memory-lean, no full
    # int32 label array) keeps enclosed water bodies whose inradius survives
    # min_pond_radius_px; smaller ponds count as LAND for the distance field
    # (they still render as water — they just grow no sand rim).
    from scipy.ndimage import binary_erosion, binary_propagation, binary_fill_holes
    _filled = binary_fill_holes(beach_land)
    _holes = _filled & ~beach_land        # inland water only (the sea touches the frame)
    del _filled                                         # S101b
    _edt_land = beach_land
    if _holes.any():
        # S105 DRY-DONUT FIX (V14 walk): anchor sand only at inland water that will
        # RENDER as water — cells below Y63.0 (>=1 block of depth). Basins in the
        # 63.0..63.4 gray zone render as LAND (surface at water level, nothing on
        # top), so their rims read as a sand doughnut around a grass bowl. Size the
        # pond test on the WET extent: a broad dry basin holding a puddle anchors
        # nothing unless the puddle itself has inradius >= min_pond_radius_px.
        _wet = _holes & (mcy < np.float32(63.0))
        _surv = binary_erosion(_wet, iterations=int(min_pond_radius_px))
        _bigp = binary_propagation(_surv, mask=_wet)
        del _surv, _wet                                 # S101b
        _edt_land = beach_land | (_holes & ~_bigp)
        del _bigp                                       # S101b
    del _holes                                          # S101b
    # EDT distance into land (blocks from the true waterline). NO return_indices —
    # its int32/float64 internals cost ~1GB on a 7.8k^2 bbox and OOM the 7.4GB box
    # (the S101 lesson). Coastal slope is propagated inland by a cheap min-dilation
    # instead (below).
    dist_from_ocean = distance_transform_edt(_edt_land).astype(np.float32)
    if _edt_land is not beach_land:
        del _edt_land                                   # S101b

    # coastal slope, propagated inland from the shore. The coast's steepness lives on
    # the FIRST land ring; a beach cell up to `max_w` blocks inland should inherit
    # THAT slope (a flat bay stays "flat coast" for the whole band), not its own
    # inland slope which climbs going up a hill. Memory-lean propagation: seed the
    # blurred true slope on the near-shore band, +inf elsewhere, then take the running
    # MINIMUM over `ceil(max_w)` grey-erosion steps (3x3) so each cell inherits the
    # gentlest slope reachable within the beach reach toward the water -> a genuine
    # flat bay backed by a hill still reads flat across the sand band. One float32.
    from scipy.ndimage import grey_erosion
    deg = gaussian_filter(slope_u16.astype(np.float32) / 65535.0 * 45.0, sigma=2.0)
    max_w0 = float(beach_max_width if beach_max_width else beach_core_width)
    _INF = np.float32(1e6)
    seed = np.where(dist_from_ocean <= (max_w0 + 2.0), deg, _INF).astype(np.float32)
    del deg
    reach = int(math.ceil(max_w0)) + 1
    coastal_deg = grey_erosion(seed, size=(3, 3))          # min over 3x3
    for _ in range(reach - 1):
        coastal_deg = grey_erosion(coastal_deg, size=(3, 3))
    # cells whose reach never touched a seeded shore cell keep _INF -> width 0 (deep
    # interior, never in the band anyway). Restore finite where dist small.
    coastal_deg = np.where(coastal_deg >= _INF, np.float32(90.0), coastal_deg)
    del seed, ocean

    # width(slope): flat -> beach_max_width, steep -> 0, smoothstep between.
    max_w = float(beach_max_width if beach_max_width else beach_core_width)
    flo, fhi = float(flat_slope_deg), float(steep_slope_deg)
    t = np.clip((coastal_deg - flo) / max(fhi - flo, 1e-6), 0.0, 1.0)
    del coastal_deg
    slope_w = (1.0 - t * t * (3.0 - 2.0 * t)).astype(np.float32)   # 1 flat -> 0 steep
    del t
    width = (np.float32(max_w) * slope_w)
    del slope_w

    # (4) along-shore width modulation — two smooth world-aligned lattice-noise
    # scales, both MULTIPLICATIVE (scale the width, never punch interior holes):
    #   bay-scale (cell=width_mod_cell): the band swells/pinches organically
    #   along the coast; fine (cell=16): ragged-but-smooth inner edge.
    wm = _coarse_world_noise01(H, W, ox, oz, int(width_mod_cell), 0xBEAC4)
    width *= (np.float32(1.0) + np.float32(width_mod_amp)
              * (wm * np.float32(2.0) - np.float32(1.0)))
    del wm                                              # S101b: dead past here
    ej = _coarse_world_noise01(H, W, ox, oz, 16, 0xD17BEA)
    width *= (np.float32(1.0) + np.float32(width_jitter)
              * (ej * np.float32(2.0) - np.float32(1.0)))
    del ej                                              # S101b

    # (5) elevation gate + (6) final band
    beach = (beach_land & (dist_from_ocean >= 1.0) & (dist_from_ocean <= width)
             & (mcy <= np.float32(63.0 + float(max_elev_blocks))))
    del beach_land, dist_from_ocean, width
    beach = (beach.astype(np.uint8)) * 255
    shore = (land & _near_ocean_disk(land, 6)).astype(np.uint8)
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
    del out, dem, a, arr   # S101b marginal-OOM: DEM + pre-rotation pyramids are dead here (~270MB)
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
    # S104 flat-island variation (V13 walk: "super flat island splines need more
    # variation") — two dome upgrades, both no-op for islands without height_gain:
    #   a. ROLLING dome: the interior lift is modulated by a low-freq WORLD-COORD
    #      lattice noise (x0.55..x1.45 over ~200-400-block wavelengths,
    #      `lift_roll_amp` knob) so a truly-flat DEM (Grand Turk: nearly all land
    #      at frac<0.1 where spline edits can't bite) gets gentle rolling relief
    #      instead of one uniform plateau dome.
    #   b. SOFT cap: mcy above lift_cap_y is COMPRESSED (x0.35), not min-clamped.
    #      The V13 audit caught the hard clamp red-handed on Bahamas: land
    #      p99.9 == max == exactly 74.0 = lift_cap_y -> visible flat mesa tops.
    _hgain = float(bands.get("height_gain", 1.0))
    if _hgain != 1.0:
        from scipy.ndimage import distance_transform_edt, gaussian_filter
        _lift_amp = max(_hgain - 1.0, 0.0)                       # +blocks at the dome centre
        _feather = float(bands.get("lift_feather_px", 60.0))     # shore->full ramp width (blocks)
        _cap_y = float(bands.get("lift_cap_y", 74.0))            # interior elevation soft ceiling
        _roll = float(bands.get("lift_roll_amp", 0.45))          # rolling modulation +- fraction
        _seed_land = mcy > 63.4                                  # lift the CURRENT land only
        if _seed_land.any():
            # distance inward from the coast, normalized + smoothstepped -> 0 at the
            # shore, 1 deep in the interior. smoothstep(3t^2-2t^3) gives a C1 dome
            # with zero slope at both ends (no rim cliff, no centre spike).
            _d = distance_transform_edt(_seed_land).astype(np.float32)
            _t = np.clip(_d / max(_feather, 1.0), 0.0, 1.0)
            _w = _t * _t * (3.0 - 2.0 * _t)                      # smoothstep
            _lift = _lift_amp * _w
            del _d, _t, _w                                       # S101b
            if _roll > 0:
                # (a) rolling interior: world-aligned lattice noise (cell=128 ~
                # 130-260-block undulations), feather-weighted so the SHORE stays
                # exactly at its DEM height (roll rides the dome weight _lift).
                _oxr = round(entry["world_offset_px"][0] / 512) * 512
                _ozr = round(entry["world_offset_px"][1] / 512) * 512
                _rn = _coarse_world_noise01(mcy.shape[0], mcy.shape[1],
                                            _oxr, _ozr, 128, 0xD03E)
                _lift = _lift * (np.float32(1.0) + np.float32(_roll)
                                 * (_rn * np.float32(2.0) - np.float32(1.0)))
                del _rn                                          # S101b
            mcy = mcy + _lift
            del _lift                                            # S101b
            # smooth away any residual stair (small sigma so the dome shape survives)
            mcy = gaussian_filter(mcy, sigma=3.0).astype(np.float32)
            # (b) SOFT cap: compress (not clamp) above lift_cap_y -> rounded rises,
            # no mesa. 0.35x keeps a couple of blocks of variation above the cap.
            _over = mcy > _cap_y
            mcy[_over] = _cap_y + (mcy[_over] - _cap_y) * np.float32(0.35)
            del _over                                            # S101b
            masks["height"] = mcy_to_world_raw(mcy)
            _lm = mcy > 63.4
            print(f"[bake]   height_gain={_hgain} (feathered dome amp=+{_lift_amp:.0f} "
                  f"feather={_feather:.0f}px roll=+-{_roll:.2f} softcap=Y{_cap_y:.0f}): "
                  f"land>63.4 now {int(_lm.sum())}px, interior median "
                  f"MC-Y={np.median(mcy[_lm]):.1f}, max MC-Y={mcy[_lm].max():.1f}", flush=True)
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

    # ── S101 EDGE DEPTH TAPER ── deepen ocean shelf toward TAPER_TARGET_MCY at the
    # render-footprint boundary (kills the 50-80-block underwater tile-line walls
    # where a real shallow bank runs off the footprint edge). INSERTION POINT: after
    # ALL land/height mutations (erase, frag kills, keep_box, margin rim, mainland
    # removal, re-snap) so the content-tile set is FINAL, and before every derived-
    # mask synth + the height.tif write, so override/bog/rock/snow/shore/beach AND
    # the build_lithology/build_terrain_derived subprocesses all see ONE coherent
    # tapered height. (shore/beach are bit-identical either way: their land test is
    # mcy>63.4 and the taper only lowers cells already <= SEA_RAW.) slope/flow/
    # erosion are NOT recomputed — same convention as every other post-derive height
    # edit (height_gain dome, frag kills), and deliberate: the stale-gentle slope
    # keeps synth_rock_gap from painting rock on the new offshore ramp (which would
    # also re-trip the S97 flow_erosion rock-clamp on ocean cells).
    _taper_band = float(bands.get("taper_band_px", TAPER_BAND_PX))
    _taper_stats = None
    if _taper_band > 0:
        _taper_stats = _apply_edge_depth_taper(
            masks["height"], mcy, land, band_px=_taper_band,
            target_mcy=TAPER_TARGET_MCY,
            apron_seed_min_px=int(entry.get("apron_seed_min_px", 0)))
        if _taper_stats:
            print(f"[bake]   edge depth taper: band={_taper_band:.0f}px -> Y{TAPER_TARGET_MCY:.0f} "
                  f"(raw {_taper_stats['target_raw']}): {_taper_stats['tapered_px']}px deepened "
                  f"(max {_taper_stats['max_deepened_blocks']:.1f} blocks); boundary-ring ocean was "
                  f"{_taper_stats['ring_ocean_n_above3_before']}px >3 blocks above target "
                  f"(max {_taper_stats['ring_ocean_max_above_target_before']:.1f})", flush=True)
            if _taper_stats["land_px_in_band"]:
                print(f"[bake]   WARNING: {_taper_stats['land_px_in_band']}px of LAND inside the "
                      f"taper band ({_taper_stats['land_px_on_ring']}px on the boundary ring) — "
                      f"left untouched; nearby ocean deepens around it", flush=True)

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
    # S105b LIMESTONE DESPECKLE (user directive, V15 Anguilla walk @31916,59111):
    # the blue-noise dither band peppers isolated rock specks across every 33-43°
    # stretch of rolling limestone; each speck then grows rock tiers + wash + a
    # legit <=40-block talus halo -> hundreds of overlapping halos read as a
    # cobblestone/clay/gravel/concrete-powder scatter "firing infinitely off the
    # rock gap". Drop connected components < min_px (keeps real cliff faces + a
    # soft dithered EDGE around them; kills core-less pepper and, via the talus
    # run-out clamp below, its halos). LIMESTONE ISLANDS ONLY — the user approved
    # every other island's read as-is.
    # (bands litho read directly — isl_litho is assigned further down, after the
    # mask synth block; referencing it here was the V16-b* instant crash)
    if bands.get("litho", "temperate_basaltic") == "limestone" and rock_gap.any():
        from scipy.ndimage import label as _lbl_rg
        _l_rg, _n_rg = _lbl_rg(rock_gap.astype(bool))
        if _n_rg:
            _sz_rg = np.bincount(_l_rg.ravel())
            _keep_rg = _sz_rg >= int(bands.get("rock_despeckle_min_px", 120))
            _keep_rg[0] = False
            _b4 = int(rock_gap.sum())
            rock_gap = _keep_rg[_l_rg].astype(np.uint8)
            print(f"[bake]   limestone rock_gap despeckle: {_b4:,} -> {int(rock_gap.sum()):,} px "
                  f"({_n_rg:,} components -> {int(_keep_rg.sum()):,})", flush=True)
        del _l_rg
    snow_gap = synth_snow_gap(mcy, land, bands)
    # S104 beach redo: slope-driven continuous sand strip. Width tapers with the
    # COASTAL slope — flat bays get up to beach_max_width blocks of clean sand
    # (x the +-45% bay-scale swell), steep volcanic/karst headlands get none
    # (rock to the water). No dither apron, no speckle; sand never sits more
    # than 7 blocks above sea (V13 audit: old sand climbed 8-25 blocks up).
    shore, beach = synth_shore_beach_wide(land, mcy, masks["slope"],
                                          world_offset_px=(_ox, _oz),
                                          beach_max_width=24.0,
                                          flat_slope_deg=6.0,
                                          steep_slope_deg=20.0,
                                          width_jitter=0.20,
                                          width_mod_amp=0.45,
                                          width_mod_cell=64,
                                          max_elev_blocks=7.0)

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
                    peak_m=peak_m, mcy_peak=mcy_peak, rot=rot, flipx=flipx, flipz=flipz,
                    edge_depth_taper=_taper_stats)
    (od / "manifest.json").write_text(json.dumps(manifest, indent=2))

    # ---- ROUTE THROUGH THE REAL MAINLAND GENERATOR ----
    # No more synth_lithology / build_island_terrain_derived. The island config is
    # the REAL config/thresholds.json with MINIMAL deltas (the stripped config was
    # what lost feature parity). Then the REAL builders run against this island's
    # masks dir at the island footprint -> rock tiers / talus / cliff_cap / aspect /
    # bedrock / lithology all fire identically to the mainland.
    snowy = bands["snow"] < 1.0
    isl_litho = bands.get("litho", "temperate_basaltic")          # per-island real-world geology (default grey volcanic)
    # S105b (user directive, V15 walk): on mossy_temperate islands the ARID biome
    # zones (dry leeward slopes of tropical volcanics — Grenadines/Efate leeward,
    # semi-arid brush) must NOT read as wet mossy cobble. Those zones keep the
    # mainland's arid_basaltic mapping; every other zone still forces isl_litho.
    # Limestone / deepslate / basaltic islands are untouched (whole-island as before).
    _mainland_z2g = cfg["lithology"]["zone_to_group"]
    if isl_litho == "mossy_temperate":
        cfg["lithology"]["zone_to_group"] = {
            z: ("arid_basaltic" if _mainland_z2g.get(z) == "arid_basaltic" else isl_litho)
            for z in _mainland_z2g}
        _n_arid = sum(1 for v in cfg["lithology"]["zone_to_group"].values() if v == "arid_basaltic")
        print(f"[bake]   lithology group = {isl_litho} (arid zones -> arid_basaltic: {_n_arid})", flush=True)
    else:
        cfg["lithology"]["zone_to_group"] = {z: isl_litho for z in _mainland_z2g}
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
    if isl_litho == "limestone":
        # S105b: talus OFF on limestone islands (user directive, V15 Anguilla walk).
        # ELIMINATION-PROVEN: the talus painter was the island-wide ~10-11% uniform
        # cobblestone/gravel/clay/concrete-powder scatter ("fires infinitely off the
        # rock gap") — far-land scatter 10.2% -> 0.1% with the overlay off, all other
        # rock features (tiers/wash/strata/cap) intact. Bare karst = the realistic
        # carbonate read. toe_rounding=floor kept as belt-and-suspenders for any
        # future re-enable; despeckle + run-out clamp above already tightened the
        # mask side. Other islands keep talus (dirt-family palettes read organic).
        cfg.setdefault("lithology", {}).setdefault("rock_layers", {}).setdefault("overlays", {})["talus"] = False
        cfg.setdefault("lithology", {}).setdefault("talus", {})["toe_rounding"] = "floor"
        print("[bake]   limestone: talus overlay OFF (elimination-proven scatter source)", flush=True)
    cfg.setdefault("gaea_gaps", {})["use_query_time"] = False     # no 8k Gaea -> baked rock/snow_gap drive gaps
    cfg.setdefault("eco_gradients", {}).setdefault("beach_gap", {})["from_mask"] = True   # S95-T2: island sand <- baked beach.tif
    cfg["eco_gradients"]["beach_gap"]["max_surface_y"] = 72        # S104: mask self-gates at Y<=70 (max_elev_blocks=7); 72 = +2 headroom for decorate-time surface_y drift (was 80 for the old climbing apron)
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
    cfg["surface_stone_to_sand"] = bool(bands.get("stone_to_sand", False))  # S98: La Tortuga + Grand Turk flat coral platforms -> swap bare-stone surface to sand (run_pipeline reads this). Other islands/mainland: absent/False -> no-op.
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
    apply_island_polish(cfg)   # S98 island-only: de-coral + soften talus + jungle/mangrove mud-swap + max veg
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
    # S105b TALUS RUN-OUT CLAMP (island-only; user directive, V15 Anguilla walk):
    # on low-relief limestone build_terrain_derived's apron fired across flats up to
    # 130 blocks from any rock_gap px (measured r.62.115: 42k px >12 blocks out at
    # median intensity 80 > the 64 paint threshold) -> gravel/cobblestone/clay/
    # concrete-powder scatter "everywhere". Physically scree ends within the run-out
    # of its source face (talus.search_blocks = 40), so: zero the apron beyond 40
    # blocks of rock_gap, linear-fade 24..40. EDT at 1:4 (S101 OOM lesson), NEAREST up.
    _tal_p, _rg_p = od / "talus_apron.tif", od / "rock_gap.tif"
    if _tal_p.exists() and _rg_p.exists():
        import rasterio as _rio2
        from scipy.ndimage import distance_transform_edt as _edt_t
        _tal = _rio2.open(_tal_p).read(1)
        _rg4 = _rio2.open(_rg_p).read(1)[::4, ::4] > 0
        _d4 = _edt_t(~_rg4).astype(np.float32) * 4.0            # blocks, 1:4 grid
        _d = np.repeat(np.repeat(_d4, 4, axis=0), 4, axis=1)[:_tal.shape[0], :_tal.shape[1]]
        del _rg4, _d4
        _REACH, _FADE0 = 40.0, 24.0
        _w = np.clip((_REACH - _d) / (_REACH - _FADE0), 0.0, 1.0).astype(np.float32)
        del _d
        _n_before = int((_tal > 0).sum())
        _tal = (_tal.astype(np.float32) * _w).astype(np.uint8)
        del _w
        _wtif(_tal_p, _tal)
        print(f"[bake]   talus run-out clamp: {_n_before:,} -> {int((_tal>0).sum()):,} px "
              f"(zeroed beyond {_REACH:.0f} blocks of rock_gap)", flush=True)
        del _tal
    # S101: DEM-derived windthrow (gap 2) + floodplain (gap 4) + clearing mask
    # — islands previously had NONE of these (mainland-only precomputes), so
    # those gaps never fired on island trees. Reads the just-written masks.
    from islands.synth_eco_masks import synth_eco_masks as _synth_eco
    _synth_eco(od, world_offset_px=(_ox, _oz))
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
