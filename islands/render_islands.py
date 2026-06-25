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
                                      clean_edge_fragments, detect_sea_raw)

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
  # ── volcanic → temperate_basaltic (grey, default) ───────────────────────────
  "17_288":  A(bands=[(0.0,110),(0.10,120),(0.55,60),(0.85,150)],snow=1.5, rock=27, wind=0.18, litho="temperate_basaltic"),  # New Vincentia — IRISH (validated, untouched)
  "13_130":  A(bands=[(0.0,230),(0.04,160),(0.60,60)],         snow=1.5,  rock=30, wind=0.12, litho="temperate_basaltic"),  # Kostati St Vincent — lush tropical: mangrove coast → LUSH jungle → highland broadleaf
  "-1_509":  A(bands=[(0.0,230),(0.04,160),(0.72,60)],         snow=1.5,  rock=32, wind=0.10, litho="temperate_basaltic"),  # Admiralty PNG — equatorial rainforest, LUSH-dominant
  "-17_622": A(bands=[(0.0,230),(0.04,160),(0.55,60)],         snow=1.5,  rock=28, wind=0.15, litho="temperate_basaltic"),  # Efate Vanuatu — tropical + tradewind (W-wet/E-dry) windward/leeward
  "12_445":  A(bands=[(0.0,230),(0.05,160),(0.55,210)],        snow=1.5,  rock=30, wind=0.10, litho="temperate_basaltic"),  # Grenadines — wet windward LUSH, dry leeward maquis
  "49_722":  A(bands=[(0.0,10),(0.10,30),(0.55,35)],           snow=0.62, rock=30, wind=0.0,  litho="temperate_basaltic"),  # Fogo Island NL — boreal cold (northernmost; Jan Mayen deleted)
  # ── metamorphic massif → deepslate_metamorphic ──────────────────────────────
  "11_060":  A(bands=[(0.0,150),(0.10,200),(0.45,210),(0.78,90)],snow=1.5,rock=29, wind=-0.10,litho="deepslate_metamorphic"),# Margarita — dry continental schist/gneiss (Cerro El Copey)
  # ── carbonate / coral / marble karst → limestone ────────────────────────────
  "-50_393": A(bands=[(0.0,70),(0.10,20),(0.45,100),(0.75,30)],snow=0.72, rock=28, wind=0.10, litho="limestone", frag_cut_east=0.75),# Madre de Dios — fjord/marble KARST; ocean everything east of 0.75W (mainland-coast erase leftover), keep main island + surrounding islets
  "18_299":  A(bands=[(0.0,150),(0.12,210),(0.55,220)],        snow=1.5,  rock=30, wind=0.05, litho="limestone"),           # Anguilla/St Maarten — limestone-dominant
  "10_941":  A(bands=[(0.0,9),(0.06,170),(0.30,200)],          snow=1.5,  rock=34, wind=-0.10,litho="limestone"),           # La Tortuga — arid carbonate platform (9=beach sentinel)
  "21_395":  A(bands=[(0.0,9),(0.06,200),(0.40,150)],          snow=1.5,  rock=34, wind=-0.05,litho="limestone"),           # Grand Turk — dry coral
  "23_887":  A(bands=[(0.0,9),(0.06,150),(0.40,200)],          snow=1.5,  rock=34, wind=0.0,  litho="limestone"),           # Bahamas — coral platform
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
    return ov


def synth_rock_gap(slope_u16, rock_deg):
    deg = slope_u16.astype(np.float32) / 65535.0 * 45.0
    rng = np.random.default_rng(0x52CC)
    deg = deg + (rng.random(deg.shape, dtype=np.float32) - 0.5) * 6.0   # +-3deg ragged forest/rock edge
    return (deg >= rock_deg).astype(np.uint8)

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


def bake_island(entry):
    reg = REGISTRY.get(Path(entry["dem_path"]).name, {})
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
    # apply user erasing (set erased pixels to ocean)
    em = ERASE_DIR / (Path(entry["dem_path"]).stem + ".png")
    if em.exists():
        e = np.asarray(Image.open(em).convert("L"))
        ef = np.array(Image.fromarray(e).resize((out["height"].shape[1], out["height"].shape[0]), Image.NEAREST))
        out["height"][ef > 0] = SEA_RAW
        print(f"[bake]   applied erase mask ({int((ef>0).sum())} px -> ocean)", flush=True)

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

    rock_gap = synth_rock_gap(masks["slope"], rock_deg)
    snow_gap = synth_snow_gap(mcy, land, bands)
    # wider beach fringe: capture the coastal rise (Y63..71) + gentler-but-not-flat
    # flanks so steep volcanic coasts still get a continuous sand band, not specks.
    shore, beach = synth_shore_beach(land, mcy, masks["slope"], beach_band=8.0, gentle_deg=16.0)

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
    cfg.setdefault("gaea_gaps", {})["use_query_time"] = False     # no 8k Gaea -> baked rock/snow_gap drive gaps
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
