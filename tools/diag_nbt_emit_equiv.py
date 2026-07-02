"""
diag_nbt_emit_equiv.py — byte-identical equivalence harness for the S100
chunk_writer NBT-emit perf rework.

HARD GATE for any perf work inside core/chunk_writer.py's emit path
(_chunk_to_nbt_bytes + the build_column_array scalar cleanup loops):
the rework must produce EXACTLY the same bytes as the original code.

Usage (run from project root with the pythoncore-3.14 install):

  py tools/diag_nbt_emit_equiv.py capture
      Run BEFORE editing chunk_writer.py.  Executes every case against the
      pristine module and stores the reference output bytes (compressed
      chunk NBT per case, volume bytes + palette for build_column_array
      cases) plus baseline timings into tools/_nbt_ref_cases.pkl.

  py tools/diag_nbt_emit_equiv.py verify
      Run AFTER editing.  Re-executes every case against the current module
      and asserts byte-for-byte equality with the stored reference.  Also
      re-runs the timing benchmark and reports the speedup vs capture.
      Exit code 0 = all byte-identical, 1 = any mismatch.

Cases covered (all deterministic — fixed RNG seeds, no wall-clock input):
  emit:  all-air tile, ocean tile (fluid ticks on tile edges + river-column
         tick skip), dense mixed tile (120+ block names incl. property-
         bearing blocks / bare leaves needing persistent-injection /
         namespaced names / fluids under both "water" and "minecraft:water"),
         sky-biome-override tile (BIOME_TO_MC_SKY patched non-empty so the
         per-section sky/ground Y-split runs), stony-peaks gap_mask+cfg
         override, edge-Y sections (blocks only in the lowest + highest
         sections), partial chunk coverage (40x40 tile -> chunks that
         overhang the tile bounds), negative world coordinates.
  bca:   build_column_array with river (seagrass placement loop, river
         column water-cleanup loop, floating-vegetation cleanup with both
         air-below and water-below triggers, rock-zone Y-2..Y-5 cleanup
         with mapped/unmapped lithology ids, kelp stamping, steep-void
         seal, lake exclusion) and a no-river variant.
"""

from __future__ import annotations

import pickle
import sys
import time
import zlib
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import core.chunk_writer as cw  # noqa: E402

REF_PKL = Path(__file__).resolve().parent / "_nbt_ref_cases.pkl"

Y_RANGE = cw.Y_RANGE
Y_MIN = cw.Y_MIN
SEA_Y = cw.SEA_Y
SEA_YI = SEA_Y - Y_MIN


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def tile_chunks(twx: int, twz: int, th: int, tw: int) -> list[tuple[int, int]]:
    """Chunk (cx, cz) list exactly as write_tile_to_region iterates them."""
    cx0 = twx // 16
    cx1 = (twx + tw - 1) // 16
    cz0 = twz // 16
    cz1 = (twz + th - 1) // 16
    return [(cx, cz) for cx in range(cx0, cx1 + 1) for cz in range(cz0, cz1 + 1)]


def _mixed_names() -> list[str]:
    """~124 block names incl. property-bearing, namespaced, fluid variants."""
    base = [
        "stone", "dirt", "grass_block", "sand", "gravel", "andesite",
        "diorite", "granite", "calcite", "tuff", "deepslate", "cobblestone",
        "mossy_cobblestone", "sandstone", "red_sand", "terracotta", "clay",
        "mud", "packed_mud", "coarse_dirt", "rooted_dirt", "podzol",
        "mycelium", "moss_block", "snow_block", "powder_snow", "ice",
        "packed_ice", "blue_ice", "obsidian", "basalt", "smooth_basalt",
        "blackstone", "magma_block", "prismarine", "dark_prismarine",
        "sea_lantern", "glowstone", "bone_block", "bedrock",
        "oak_log", "spruce_log", "birch_log", "dark_oak_log", "oak_planks",
        # leaves: bare (persistent injection), pre-set props, distance-only
        "oak_leaves", "spruce_leaves", "birch_leaves[persistent=true]",
        "oak_leaves[distance=3]", "spruce_leaves[persistent=true,distance=2]",
        "azalea_leaves",
        # fluids, both spellings (heightmap fluid detection)
        "water", "minecraft:water", "lava", "minecraft:lava",
        # namespaced
        "minecraft:stone", "minecraft:glass", "mymod:custom_block",
        # waterlogged / stateful
        "sea_pickle", "sea_pickle[waterlogged=true]", "seagrass",
        "kelp_plant", "kelp[age=25]",
        "tall_grass[half=lower]", "tall_grass[half=upper]",
        "large_fern[half=lower]", "large_fern[half=upper]",
        "short_grass", "fern", "dead_bush", "bush",
        "oak_stairs[facing=north,half=bottom]", "vine[north=true]", "snow",
        "structure_void", "dripstone_block",
        "pointed_dripstone[thickness=tip]",
    ]
    for c in ("white", "orange", "magenta", "light_blue", "yellow", "lime",
              "pink", "gray", "light_gray", "cyan", "purple", "blue",
              "brown", "green", "red", "black"):
        base.append(f"{c}_wool")
        base.append(f"{c}_concrete")
        base.append(f"{c}_terracotta")
    return base


# ---------------------------------------------------------------------------
# emit-case builders — each returns a FRESH dict every call (inputs are
# rebuilt from seeds, never shared between the reference and test runs)
# ---------------------------------------------------------------------------

def make_case_air() -> dict:
    pal = cw.BlockPalette()
    # register a handful of names so the palette isn't just ["air"]
    for n in ("stone", "dirt", "water", "grass_block"):
        pal.idx(n)
    th = tw = 48
    vol = np.zeros((Y_RANGE, th, tw), dtype=np.uint16)
    biome = np.full((th, tw), "MIXED_FOREST", dtype=object)
    return dict(vol=vol, pal=pal, biome_grid=biome, twx=0, twz=0,
                th=th, tw=tw, river_water_y=None, gap_mask=None, cfg=None,
                chunks=tile_chunks(0, 0, th, tw), sky_patch=None)


def make_case_ocean() -> dict:
    rng = np.random.default_rng(101)
    th = tw = 48
    pal = cw.BlockPalette()
    STONE = pal.idx("stone")
    SAND = pal.idx("sand")
    GRAVEL = pal.idx("gravel")
    WATER = pal.idx("water")
    SGRASS = pal.idx("seagrass")
    PICKLE = pal.idx("sea_pickle[waterlogged=true]")

    floor_wy = 30 + rng.integers(0, 16, size=(th, tw))     # world Y of floor
    floor_yi = (floor_wy - Y_MIN).astype(np.int64)
    yi = np.arange(Y_RANGE, dtype=np.int64)[:, None, None]

    vol = np.where(yi < floor_yi[None, :, :], STONE, 0).astype(np.uint16)
    rr = np.repeat(np.arange(th), tw)
    cc = np.tile(np.arange(tw), th)
    surf_blk = np.where(rng.random(th * tw) < 0.5, SAND, GRAVEL)
    vol[floor_yi.ravel(), rr, cc] = surf_blk.astype(np.uint16)
    wmask = (yi > floor_yi[None, :, :]) & (yi <= SEA_YI)
    vol[wmask] = WATER
    # scatter seagrass / sea pickles on the floor+1
    scat = rng.random((th, tw))
    for prob_lo, prob_hi, code in ((0.0, 0.06, SGRASS), (0.06, 0.09, PICKLE)):
        sm = (scat >= prob_lo) & (scat < prob_hi)
        sr, sc = np.where(sm)
        syi = floor_yi[sr, sc] + 1
        ok = syi <= SEA_YI
        vol[syi[ok], sr[ok], sc[ok]] = code

    biome = np.full((th, tw), "_OCEAN", dtype=object)
    biome[40:, :] = "EASTERN_TEMPERATE_COAST"
    # river_water_y stripe > 0 --> those edge columns must SKIP fluid ticks
    rw = np.zeros((th, tw), dtype=np.int16)
    rw[20:28, :] = 70
    return dict(vol=vol, pal=pal, biome_grid=biome, twx=0, twz=0,
                th=th, tw=tw, river_water_y=rw, gap_mask=None, cfg=None,
                chunks=tile_chunks(0, 0, th, tw), sky_patch=None)


def _mixed_vol(pal: cw.BlockPalette, th: int, tw: int, seed: int,
               surf_lo: int, surf_hi: int):
    """Dense random terrain: every below-surface cell a random block name."""
    rng = np.random.default_rng(seed)
    names = _mixed_names()
    codes = np.array([pal.idx(n) for n in names], dtype=np.uint16)
    surf_wy = (surf_lo + (surf_hi - surf_lo) * rng.random((th, tw))).astype(np.int32)
    surf_wy[0:8, :] = 40                                   # water strip rows
    surf_yi = (surf_wy - Y_MIN).astype(np.int64)
    yi = np.arange(Y_RANGE, dtype=np.int64)[:, None, None]
    fill = codes[rng.integers(0, len(codes), size=(Y_RANGE, th, tw))]
    vol = np.where(yi <= surf_yi[None, :, :], fill, 0).astype(np.uint16)
    rowmask = np.zeros((th, tw), dtype=bool)
    rowmask[0:8, :] = True
    wmask = (yi > surf_yi[None, :, :]) & (yi <= SEA_YI) & rowmask[None, :, :]
    vol[wmask] = pal.idx("water")
    return vol


def make_case_mixed(sky: bool = False) -> dict:
    th = tw = 48
    pal = cw.BlockPalette()
    vol = _mixed_vol(pal, th, tw, seed=20260702, surf_lo=100, surf_hi=300)

    biome = np.empty((th, tw), dtype=object)
    biome[:, :] = "MIXED_FOREST"
    biome[:24, 24:] = "BOREAL_ALPINE"
    biome[24:, :24] = "SNOWY_BOREAL_TAIGA"
    biome[24:, 24:] = "SAND_DUNE_DESERT"
    biome[0:4, :] = "_OCEAN"
    biome[4:6, :] = ""                     # S95-T4 empty -> ocean fallback
    biome[6:8, :] = "NOT_A_BIOME"          # -> _DEFAULT fallback

    gap = np.zeros((th, tw), dtype=np.uint8)
    gap[10:30, 8:26] = 5                   # rock for stony-peaks override
    cfg = {"snow_physics": {
        "rock_runtime_biome": "minecraft:stony_peaks",
        "runtime_snowy_biomes": ["SNOWY_BOREAL_TAIGA", "BOREAL_ALPINE"],
        "rock_runtime_dilate": 2,
    }}
    rw = np.zeros((th, tw), dtype=np.int16)
    rw[30:34, :] = 90
    return dict(vol=vol, pal=pal, biome_grid=biome, twx=0, twz=0,
                th=th, tw=tw, river_water_y=rw, gap_mask=gap, cfg=cfg,
                chunks=tile_chunks(0, 0, th, tw),
                sky_patch=({"BOREAL_ALPINE": "minecraft:plains"} if sky else None))


def make_case_edge_y() -> dict:
    pal = cw.BlockPalette()
    BEDROCK = pal.idx("bedrock")
    STONE = pal.idx("stone")
    GLOW = pal.idx("glowstone")
    th = tw = 32
    vol = np.zeros((Y_RANGE, th, tw), dtype=np.uint16)
    vol[0, :, :] = BEDROCK
    vol[1:21, :, :] = STONE
    vol[Y_RANGE - 18:Y_RANGE - 1, :, :] = GLOW    # top section + spill-over
    vol[Y_RANGE - 1, 5, 5] = STONE                # single block at max Y
    biome = np.full((th, tw), "BOREAL_TAIGA", dtype=object)
    return dict(vol=vol, pal=pal, biome_grid=biome, twx=0, twz=0,
                th=th, tw=tw, river_water_y=None, gap_mask=None, cfg=None,
                chunks=tile_chunks(0, 0, th, tw), sky_patch=None)


def make_case_partial() -> dict:
    th = tw = 40                                   # chunks overhang tile
    pal = cw.BlockPalette()
    vol = _mixed_vol(pal, th, tw, seed=424242, surf_lo=80, surf_hi=200)
    biome = np.full((th, tw), "TEMPERATE_DECIDUOUS", dtype=object)
    biome[:, 20:] = "KARST_BARRENS"
    rw = np.zeros((th, tw), dtype=np.int16)
    rw[10:12, :] = 80
    return dict(vol=vol, pal=pal, biome_grid=biome, twx=0, twz=0,
                th=th, tw=tw, river_water_y=rw, gap_mask=None, cfg=None,
                chunks=tile_chunks(0, 0, th, tw), sky_patch=None)


def make_case_negative() -> dict:
    th = tw = 48
    pal = cw.BlockPalette()
    vol = _mixed_vol(pal, th, tw, seed=999, surf_lo=64, surf_hi=150)
    biome = np.full((th, tw), "MANGROVE_COAST", dtype=object)
    biome[:16, :] = "_OCEAN"
    rw = np.zeros((th, tw), dtype=np.int16)
    rw[40:44, :] = 75
    twx = twz = -512
    return dict(vol=vol, pal=pal, biome_grid=biome, twx=twx, twz=twz,
                th=th, tw=tw, river_water_y=rw, gap_mask=None, cfg=None,
                chunks=tile_chunks(twx, twz, th, tw), sky_patch=None)


def make_case_bench() -> dict:
    """Dense random tile for timing: 9 chunks x 48 sections = 432 sections,
    most below-surface sections carrying 100+ distinct block names."""
    th = tw = 48
    pal = cw.BlockPalette()
    vol = _mixed_vol(pal, th, tw, seed=5150, surf_lo=250, surf_hi=600)
    biome = np.full((th, tw), "MIXED_FOREST", dtype=object)
    biome[:24, :] = "SNOWY_BOREAL_TAIGA"
    rw = np.zeros((th, tw), dtype=np.int16)
    rw[8:12, :] = 90
    return dict(vol=vol, pal=pal, biome_grid=biome, twx=0, twz=0,
                th=th, tw=tw, river_water_y=rw, gap_mask=None, cfg=None,
                chunks=tile_chunks(0, 0, th, tw), sky_patch=None)


EMIT_CASE_BUILDERS = {
    "air_tile":        make_case_air,
    "ocean":           make_case_ocean,
    "mixed_dense":     lambda: make_case_mixed(sky=False),
    "sky_override":    lambda: make_case_mixed(sky=True),
    "edge_y":          make_case_edge_y,
    "partial_tile":    make_case_partial,
    "negative_coords": make_case_negative,
}


def run_emit_case(case: dict) -> list[bytes]:
    """Emit every chunk of the case; returns per-chunk compressed NBT."""
    saved_sky = dict(cw.BIOME_TO_MC_SKY)
    try:
        if case["sky_patch"]:
            cw.BIOME_TO_MC_SKY.update(case["sky_patch"])
        out = []
        for cx, cz in case["chunks"]:
            out.append(cw._chunk_to_nbt_bytes(
                cx, cz, case["vol"], case["pal"], case["biome_grid"],
                case["twx"], case["twz"], case["th"], case["tw"],
                river_water_y=case["river_water_y"],
                gap_mask=case["gap_mask"], cfg=case["cfg"],
            ))
        return out
    finally:
        cw.BIOME_TO_MC_SKY.clear()
        cw.BIOME_TO_MC_SKY.update(saved_sky)


# ---------------------------------------------------------------------------
# build_column_array cases (cover the 4 scalar cleanup loops)
# ---------------------------------------------------------------------------

def make_bca_main() -> dict:
    H = W = 96
    rng = np.random.default_rng(777)
    xx, zz = np.meshgrid(np.arange(W), np.arange(H))

    surface_y = (70 + (xx + zz) // 8).astype(np.int16)     # gentle 70..~93
    surface_y[60:80, 60:80] += 40                          # cliff hill
    surface_y[0:24, 0:24] = (30 + 10 * rng.random((24, 24))).astype(np.int16)

    # descending river channel (cascade steps of 2 -> steep-void seal seed)
    river_water_y = np.zeros((H, W), dtype=np.int16)
    for i in range(30, 90):
        wl = 95 - 2 * ((i - 30) // 3)
        if wl <= SEA_Y + 1:
            break
        for dj in range(-2, 3):
            j = i + dj
            if 0 <= j < W:
                river_water_y[i, j] = wl
                surface_y[i, j] = wl - 3
    # lake blob (river_meta CHAN_LAKE = 3) — void-seal exclusion
    river_meta = np.zeros((H, W), dtype=np.uint8)
    river_meta[84:92, 80:92] = 3
    river_water_y[84:92, 80:92] = 80
    surface_y[84:92, 80:92] = 75

    surface_blk = np.full((H, W), "grass_block", dtype=object)
    surface_blk[0:24, 0:24] = "sand"
    surface_blk[(river_water_y > 0)] = "gravel"
    # floating-veg triggers: air-below + water-below surface cells on land
    land = (surface_y > SEA_Y) & (river_water_y == 0)
    lr, lc = np.where(land)
    pick = rng.permutation(len(lr))
    for k in pick[:40]:
        surface_blk[lr[k], lc[k]] = "air"
    for k in pick[40:80]:
        surface_blk[lr[k], lc[k]] = "water"

    sub_blk = np.full((H, W), "dirt", dtype=object)
    sub_blk[48:, 48:] = "sandstone"

    ground_cover = np.full((H, W), "", dtype=object)
    veg = ["short_grass", "tall_grass", "fern", "large_fern", "dead_bush",
           "bush", "oxeye_daisy", "poppy"]
    gc_roll = rng.random((H, W))
    gc_pick = rng.integers(0, len(veg), size=(H, W))
    on_land = gc_roll < 0.35
    for vi, vname in enumerate(veg):
        ground_cover[on_land & (gc_pick == vi) & land] = vname
    # make sure the air/water-surface cells carry veg (floater triggers)
    for k in pick[:80]:
        ground_cover[lr[k], lc[k]] = veg[int(k) % len(veg)]
    # ocean corner: underwater veg incl. kelp + tall_seagrass
    uveg = ["kelp", "seagrass", "tall_seagrass", "sea_pickle"]
    ur, uc = np.where(surface_y < SEA_Y - 2)
    upick = rng.permutation(len(ur))
    for n, k in enumerate(upick[:120]):
        ground_cover[ur[k], uc[k]] = uveg[n % len(uveg)]

    biome_grid = np.full((H, W), "MIXED_FOREST", dtype=object)
    biome_grid[:, 48:] = "KARST_BARRENS"

    gap_mask = np.zeros((H, W), dtype=np.uint8)
    gap_mask[60:80, 60:80] = 5                              # rock-zone cleanup
    lithology_tile = np.zeros((H, W), dtype=np.uint8)
    lithology_tile[:, :48] = 1
    lithology_tile[:, 48:] = 2
    lithology_tile[62:66, 62:66] = 77                       # unmapped id -> stone fb

    cfg = {
        "lithology": {
            "rock_zone_cleanup": {
                "enabled": True,
                "column_top6_cleanup": True,
                "surface_bad_blocks": ["grass_block", "dirt"],
                "subsurface_bad_blocks": ["stone", "coarse_dirt"],
            },
            "groups": {
                "granitic": {"id": 1, "palette": ["granite"]},
                "karst": {"id": 2, "palette": ["calcite"]},
                "faraway": {"id": 300, "palette": ["tuff"]},   # out of uint8 range
            },
        },
        "ocean": {"vegetation": {"kelp_min": 5, "kelp_max": 12}},
    }
    return dict(
        surface_y=surface_y, surface_blk=surface_blk, sub_blk=sub_blk,
        ground_cover=ground_cover, biome_grid=biome_grid,
        cliff_deg_thr=45.0, band_scale_y=12,
        tile_world_x=1024, tile_world_z=2048,
        river_water_y=river_water_y, lithology_tile=lithology_tile,
        use_new_geology=False, flow_tile=None, cfg=cfg,
        gap_mask=gap_mask, river_meta=river_meta,
    )


def make_bca_no_river() -> dict:
    H = W = 64
    rng = np.random.default_rng(31337)
    surface_y = (60 + 30 * rng.random((H, W))).astype(np.int16)
    surface_y[0:16, 0:16] = 40                              # ocean corner
    surface_blk = np.full((H, W), "grass_block", dtype=object)
    surface_blk[0:16, 0:16] = "gravel"
    lr, lc = np.where(surface_y > SEA_Y)
    pick = rng.permutation(len(lr))
    for k in pick[:20]:
        surface_blk[lr[k], lc[k]] = "air"
    sub_blk = np.full((H, W), "dirt", dtype=object)
    ground_cover = np.full((H, W), "", dtype=object)
    gm = rng.random((H, W)) < 0.3
    ground_cover[gm & (surface_y > SEA_Y)] = "short_grass"
    for k in pick[:20]:
        ground_cover[lr[k], lc[k]] = "tall_grass"           # double-tall floater
    biome_grid = np.full((H, W), "BIRCH_FOREST", dtype=object)
    return dict(
        surface_y=surface_y, surface_blk=surface_blk, sub_blk=sub_blk,
        ground_cover=ground_cover, biome_grid=biome_grid,
        cliff_deg_thr=45.0, band_scale_y=12,
        tile_world_x=-2048, tile_world_z=512,
        river_water_y=None, lithology_tile=None,
        use_new_geology=False, flow_tile=None, cfg=None,
        gap_mask=None, river_meta=None,
    )


BCA_CASE_BUILDERS = {
    "bca_main": make_bca_main,
    "bca_no_river": make_bca_no_river,
}


def run_bca_case(case: dict) -> dict:
    vol, pal = cw.build_column_array(**case)
    return {
        "vol_z": zlib.compress(vol.tobytes(), 6),
        "shape": tuple(vol.shape),
        "dtype": str(vol.dtype),
        "pal": list(pal._names),
    }


# ---------------------------------------------------------------------------
# stamp_schematic cases (S100 stamp-perf rework gate)
# ---------------------------------------------------------------------------
# Real schematics from schematic_index.json:
#   banyan_lg — 22x38x37 huge-footprint tree (trunk strategy A, extension,
#               root anchor, log->wood swap)
#   sbirch_lg — Sponge .schem WITH blockstate properties (axis=x/y/z logs ->
#               horizontal-log wood-swap skip; persistent=false leaves)
#   bush_sm   — zero-log bush (strategy B sink + float reject)
#   dbirch_sm — small classic tree (clip / OOB / det-seat cases)
#   dpine_lg  — dead pine (sparse canopy)

_STAMP_SCHEMS = {
    "banyan_lg": "Vegetation/lrfc_tree_banyan_d_lg.schematic",
    "sbirch_lg": "Vegetation/birch_tree_sbirch_k_lg.schem",
    "bush_sm":   "Vegetation/bush_generic_a_sm.schematic",
    "dbirch_sm": "Vegetation/birch_tree_dbirch_a_sm.schematic",
    "dpine_lg":  "Vegetation/dpine_dead_pine_e_lg.schematic",
}

# (schem_key, local_x, local_z, dy_from_surface, rot, clip_oob, det_seat)
# Sequential — stamp ORDER is semantic (protected-overwrite between
# overlapping placements) and must be preserved by any rework.
_STAMP_OPS = [
    ("banyan_lg", 40, 40,  1, 0, True,  False),  # interior big tree (A + ext + roots)
    ("sbirch_lg", 12, 50,  1, 1, True,  False),  # props + rot-1 blockstate remap
    ("sbirch_lg", 60, 26,  1, 3, True,  False),  # rot-3 remap
    ("bush_sm",   30, 20,  3, 2, True,  False),  # bush sink (strategy B)
    ("bush_sm",   50,  8,  8, 0, True,  False),  # bush float > 3 -> whole reject
    ("dbirch_sm", 90, 60,  1, 0, True,  False),  # clipped at +x tile edge
    ("dbirch_sm", -4, 30,  1, 0, True,  True),   # OOB anchor, band-style det seat
    ("dbirch_sm", -4, 66,  1, 0, False, False),  # clip_oob=False -> OOB col whole-rejects
    ("banyan_lg", 44, 44,  1, 0, True,  False),  # overlaps op#0 -> protected skips
    ("dbirch_sm", 24, 78,  1, 0, True,  False),  # pond footprint -> underwater whole reject
    ("dbirch_sm", 24, 78,  1, 0, True,  True),   # same + det seat -> per-column skip only
    ("dpine_lg",  82, 60,  1, 0, True,  False),  # river-stripe water_col_mask columns
    ("dpine_lg",  58, 12,  6, 0, True,  False),  # shelf edge, huge trunk gap -> A reject
    ("dbirch_sm", 70, 14, 14, 0, True,  False),  # floats 14 -> big sink + extension
    ("dbirch_sm", 33, 33, -6, 0, True,  False),  # buried anchor -> desink hides low blocks
]


def make_stamp_world():
    """Terrain the stamp ops land on: grass terraces + below-sea pond +
    high shelf (surface jump) + a fake river stripe in water_col_mask."""
    H = W = 96
    surface_y = np.full((H, W), 80, dtype=np.int16)
    surface_y += (np.arange(W)[None, :] // 12).astype(np.int16)   # terraces
    surface_y[70:96, 0:30] = 55           # pond (below sea level 63)
    surface_y[10:20, 60:90] = 120         # high shelf
    surface_blk = np.full((H, W), "grass_block", dtype=object)
    surface_blk[70:96, 0:30] = "gravel"
    sub_blk = np.full((H, W), "dirt", dtype=object)
    ground_cover = np.full((H, W), "", dtype=object)
    vol, pal = cw.build_column_array(
        surface_y, surface_blk, sub_blk, ground_cover,
        biome_grid=None, tile_world_x=512, tile_world_z=512)
    water_col_mask = (surface_y < 63).copy()
    water_col_mask[:, 84:87] = True       # fake carved-river stripe
    return vol, pal, surface_y, water_col_mask


def _load_stamp_schem(key: str):
    from core import schematic_loader as _sl
    return _sl.load_schem(ROOT / _STAMP_SCHEMS[key])


def run_stamp_case() -> dict:
    """Run all _STAMP_OPS sequentially on a fresh world; returns final vol +
    palette + a per-op checkpoint hash chain (pinpoints first diverging op)."""
    import hashlib
    vol, pal, surface_y, water_col_mask = make_stamp_world()
    H, W = surface_y.shape
    checkpoints = []
    for (skey, lx, lz, dy, rot, clip, seat) in _STAMP_OPS:
        sd = _load_stamp_schem(skey)
        sd._rotation = rot
        az = min(max(lz, 0), H - 1)
        ax = min(max(lx, 0), W - 1)
        place_y = int(surface_y[az, ax]) + dy
        cw.stamp_schematic(vol, pal, sd, lx, lz, place_y,
                           surface_y=surface_y,
                           water_col_mask=water_col_mask,
                           clip_oob=clip, deterministic_seat=seat)
        checkpoints.append(hashlib.sha256(vol.tobytes()).hexdigest())
    # legacy dict branch (smoke-test format)
    legacy = {"blocks": [
        (0, 0, 0, None, "minecraft:oak_log", None),
        (0, 1, 0, None, "stone", None),
        (1, 0, 1, None, "air", None),
        (2, 2, 2, None, "oak_leaves", None),
    ]}
    cw.stamp_schematic(vol, pal, legacy, 5, 5, int(surface_y[5, 5]) + 1)
    checkpoints.append(hashlib.sha256(vol.tobytes()).hexdigest())
    return {
        "vol_z": zlib.compress(vol.tobytes(), 6),
        "shape": tuple(vol.shape),
        "dtype": str(vol.dtype),
        "pal": list(pal._names),
        "checkpoints": checkpoints,
    }


def run_stamp_bench(n_stamps: int = 100, reps: int = 2) -> float:
    """Time a batch of stamps on a flat world (fresh world per rep)."""
    keys = list(_STAMP_SCHEMS)
    best = None
    for _ in range(reps):
        H = W = 160
        surface_y = np.full((H, W), 80, dtype=np.int16)
        surface_blk = np.full((H, W), "grass_block", dtype=object)
        sub_blk = np.full((H, W), "dirt", dtype=object)
        ground_cover = np.full((H, W), "", dtype=object)
        vol, pal = cw.build_column_array(
            surface_y, surface_blk, sub_blk, ground_cover,
            biome_grid=None, tile_world_x=0, tile_world_z=0)
        water_col_mask = np.zeros((H, W), dtype=bool)
        t0 = time.perf_counter()
        for i in range(n_stamps):
            sd = _load_stamp_schem(keys[i % len(keys)])
            sd._rotation = i % 4
            lx = 8 + (i * 37) % (W - 44)
            lz = 8 + (i * 53) % (H - 44)
            cw.stamp_schematic(vol, pal, sd, lx, lz, 81,
                               surface_y=surface_y,
                               water_col_mask=water_col_mask)
        dt = time.perf_counter() - t0
        best = dt if best is None else min(best, dt)
    return best


# ---------------------------------------------------------------------------
# bench
# ---------------------------------------------------------------------------

def run_bench(reps: int = 3) -> dict:
    case = make_case_bench()
    n_chunks = len(case["chunks"])
    best = None
    for _ in range(reps):
        t0 = time.perf_counter()
        out = run_emit_case(case)
        dt = time.perf_counter() - t0
        best = dt if best is None else min(best, dt)
    n_sections = n_chunks * (Y_RANGE // 16)
    # bca timing too (covers the vectorised cleanup loops)
    bca_best = None
    for _ in range(2):
        bca_case = make_bca_main()
        t0 = time.perf_counter()
        run_bca_case(bca_case)
        dt = time.perf_counter() - t0
        bca_best = dt if bca_best is None else min(bca_best, dt)
    return {
        "emit_seconds": best,
        "n_chunks": n_chunks,
        "n_sections": n_sections,
        "emit_digest": [zlib.crc32(b) for b in out],
        "bca_seconds": bca_best,
    }


# ---------------------------------------------------------------------------
# capture / verify
# ---------------------------------------------------------------------------

def do_capture() -> None:
    ref = {"emit": {}, "bca": {}, "meta": {
        "numpy": np.__version__,
        "python": sys.version,
    }}
    for name, builder in EMIT_CASE_BUILDERS.items():
        t0 = time.perf_counter()
        ref["emit"][name] = run_emit_case(builder())
        print(f"[capture] emit {name}: {len(ref['emit'][name])} chunks "
              f"({time.perf_counter() - t0:.2f}s)")
    for name, builder in BCA_CASE_BUILDERS.items():
        t0 = time.perf_counter()
        ref["bca"][name] = run_bca_case(builder())
        print(f"[capture] bca {name}: pal={len(ref['bca'][name]['pal'])} names "
              f"({time.perf_counter() - t0:.2f}s)")
    t0 = time.perf_counter()
    ref["stamp"] = run_stamp_case()
    print(f"[capture] stamp: {len(_STAMP_OPS) + 1} ops, "
          f"pal={len(ref['stamp']['pal'])} names "
          f"({time.perf_counter() - t0:.2f}s)")
    print("[capture] running baseline bench...")
    ref["bench"] = run_bench()
    ref["bench"]["stamp_seconds"] = run_stamp_bench()
    print(f"[capture] bench: {ref['bench']['emit_seconds']:.3f}s for "
          f"{ref['bench']['n_chunks']} chunks / {ref['bench']['n_sections']} sections; "
          f"bca_main {ref['bench']['bca_seconds']:.3f}s; "
          f"stamp x100 {ref['bench']['stamp_seconds']:.3f}s")
    with open(REF_PKL, "wb") as fh:
        pickle.dump(ref, fh)
    print(f"[capture] reference saved to {REF_PKL}")


def _diff_chunk_bytes(a: bytes, b: bytes, label: str) -> bool:
    if a == b:
        return True
    da, db = zlib.decompress(a), zlib.decompress(b)
    n = min(len(da), len(db))
    off = n
    for i in range(n):
        if da[i] != db[i]:
            off = i
            break
    print(f"  [FAIL] {label}: compressed {len(a)} vs {len(b)} B; "
          f"uncompressed {len(da)} vs {len(db)} B; first diff @ {off}")
    lo, hi = max(0, off - 24), min(n, off + 24)
    print(f"    ref[{lo}:{hi}] = {da[lo:hi].hex()}")
    print(f"    new[{lo}:{hi}] = {db[lo:hi].hex()}")
    return False


def do_verify() -> int:
    if not REF_PKL.exists():
        print(f"ERROR: {REF_PKL} missing — run `capture` on the pristine "
              f"module first.")
        return 2
    with open(REF_PKL, "rb") as fh:
        ref = pickle.load(fh)
    if ref["meta"]["numpy"] != np.__version__:
        print(f"WARNING: numpy version changed since capture "
              f"({ref['meta']['numpy']} -> {np.__version__}) — "
              f"differences may not be caused by the code edit.")

    failures = 0

    for name, builder in EMIT_CASE_BUILDERS.items():
        got = run_emit_case(builder())
        want = ref["emit"][name]
        if len(got) != len(want):
            print(f"[FAIL] emit {name}: chunk count {len(got)} != {len(want)}")
            failures += 1
            continue
        bad = 0
        for i, (a, b) in enumerate(zip(want, got)):
            if not _diff_chunk_bytes(a, b, f"emit {name} chunk#{i}"):
                bad += 1
        if bad:
            failures += 1
            print(f"[FAIL] emit {name}: {bad}/{len(got)} chunks differ")
        else:
            print(f"[PASS] emit {name}: {len(got)} chunks byte-identical")

    for name, builder in BCA_CASE_BUILDERS.items():
        got = run_bca_case(builder())
        want = ref["bca"][name]
        ok = True
        if got["pal"] != want["pal"]:
            print(f"[FAIL] bca {name}: palette names differ")
            # show first divergence
            for i, (a, b) in enumerate(zip(want["pal"], got["pal"])):
                if a != b:
                    print(f"    idx {i}: ref={a!r} new={b!r}")
                    break
            if len(want["pal"]) != len(got["pal"]):
                print(f"    len ref={len(want['pal'])} new={len(got['pal'])}")
            ok = False
        ra = zlib.decompress(want["vol_z"])
        rb = zlib.decompress(got["vol_z"])
        if ra != rb:
            va = np.frombuffer(ra, dtype=want["dtype"]).reshape(want["shape"])
            vb = np.frombuffer(rb, dtype=got["dtype"]).reshape(got["shape"])
            dif = np.argwhere(va != vb)
            print(f"[FAIL] bca {name}: volume differs at {len(dif)} cells; "
                  f"first: yi,r,c={dif[0].tolist()} "
                  f"ref={va[tuple(dif[0])]} new={vb[tuple(dif[0])]}")
            ok = False
        if ok:
            print(f"[PASS] bca {name}: volume + palette byte-identical")
        else:
            failures += 1

    if "stamp" in ref:
        got = run_stamp_case()
        want = ref["stamp"]
        ok = True
        if got["checkpoints"] != want["checkpoints"]:
            for i, (a, b) in enumerate(zip(want["checkpoints"],
                                           got["checkpoints"])):
                if a != b:
                    _op = (_STAMP_OPS[i] if i < len(_STAMP_OPS)
                           else "legacy-dict")
                    print(f"[FAIL] stamp: first divergence after op#{i} {_op}")
                    break
            ok = False
        if got["pal"] != want["pal"]:
            print("[FAIL] stamp: palette names differ")
            for i, (a, b) in enumerate(zip(want["pal"], got["pal"])):
                if a != b:
                    print(f"    idx {i}: ref={a!r} new={b!r}")
                    break
            if len(want["pal"]) != len(got["pal"]):
                print(f"    len ref={len(want['pal'])} new={len(got['pal'])}")
            ok = False
        ra = zlib.decompress(want["vol_z"])
        rb = zlib.decompress(got["vol_z"])
        if ra != rb:
            va = np.frombuffer(ra, dtype=want["dtype"]).reshape(want["shape"])
            vb = np.frombuffer(rb, dtype=got["dtype"]).reshape(got["shape"])
            dif = np.argwhere(va != vb)
            print(f"[FAIL] stamp: volume differs at {len(dif)} cells; "
                  f"first: yi,r,c={dif[0].tolist()} "
                  f"ref={va[tuple(dif[0])]} new={vb[tuple(dif[0])]}")
            ok = False
        if ok:
            print(f"[PASS] stamp: {len(_STAMP_OPS) + 1} ops, volume + palette "
                  f"+ per-op checkpoints byte-identical")
        else:
            failures += 1

    print("[verify] running bench on current code...")
    bench = run_bench()
    old = ref["bench"]
    if bench["emit_digest"] != old["emit_digest"]:
        print("[FAIL] bench-case output digests differ from capture!")
        failures += 1
    print(f"[bench] emit ({bench['n_chunks']} chunks / {bench['n_sections']} sections): "
          f"old {old['emit_seconds']:.3f}s -> new {bench['emit_seconds']:.3f}s "
          f"({old['emit_seconds'] / max(bench['emit_seconds'], 1e-9):.2f}x)")
    print(f"[bench] build_column_array (96x96 river/rock case): "
          f"old {old['bca_seconds']:.3f}s -> new {bench['bca_seconds']:.3f}s "
          f"({old['bca_seconds'] / max(bench['bca_seconds'], 1e-9):.2f}x)")
    if "stamp_seconds" in old:
        stamp_new = run_stamp_bench()
        print(f"[bench] stamp_schematic x100: "
              f"old {old['stamp_seconds']:.3f}s -> new {stamp_new:.3f}s "
              f"({old['stamp_seconds'] / max(stamp_new, 1e-9):.2f}x)")

    if failures:
        print(f"\nRESULT: {failures} case(s) FAILED — output is NOT byte-identical.")
        return 1
    print("\nRESULT: ALL cases byte-identical.")
    return 0


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ("capture", "verify"):
        print(__doc__)
        return 2
    if sys.argv[1] == "capture":
        do_capture()
        return 0
    return do_verify()


if __name__ == "__main__":
    sys.exit(main())
