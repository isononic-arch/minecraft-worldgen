import cProfile, pstats, io, sys, time
sys.path.insert(0, '.')
import json, numpy as np, importlib

cfg        = json.load(open('config/thresholds.json'))
core_col   = importlib.import_module('core.column_generator')
core_tiles = importlib.import_module('core.tile_streamer')
core_biome = importlib.import_module('core.biome_assignment')
core_noise = importlib.import_module('core.noise_fields')
core_river = importlib.import_module('core.river_carver')
core_dec   = importlib.import_module('core.surface_decorator')

masks_dir = r'C:\Users\nicho\minecraft-worldgen\masks'

t0 = time.perf_counter()
masks = core_tiles.read_tile(masks_dir, 24, 9)
print(f"read_tile:          {time.perf_counter()-t0:.2f}s")

t0 = time.perf_counter()
noise_gens = core_noise.load_noise_generators('config/thresholds.json')
print(f"load_noise_gens:    {time.perf_counter()-t0:.2f}s")

t0 = time.perf_counter()
biomes = core_biome.assign_biomes(
    masks['height'], masks['slope'], masks['flow'], masks['erosion'],
    masks['override'], noise_gens, cfg, 24, 9
)
print(f"assign_biomes:      {time.perf_counter()-t0:.2f}s")

core_chunk = importlib.import_module('core.chunk_writer')

# validate_test_tile.py-style preprocessing
H, W = masks['height'].shape
h_u16   = (masks['height']  * 65535).astype(np.uint16)
sl_u16  = (masks['slope']   * 65535).astype(np.uint16)
er_u16  = (masks['erosion'] * 65535).astype(np.uint16)
fl_u16  = (masks['flow']    * 65535).astype(np.uint16)
dep_u16 = np.zeros((H, W), dtype=np.uint16)
sh_bool = np.zeros((H, W), dtype=bool)
mc_biomes = np.full((H, W), 'minecraft:plains', dtype=object)

t0 = time.perf_counter()
col_results = core_col.process_tile_columns_v2(
    tile_height=h_u16, tile_slope=sl_u16,
    tile_erosion=er_u16, tile_flow=fl_u16,
    tile_deposits=dep_u16, tile_shore=sh_bool,
    tile_biomes=biomes, tile_mc_biomes=mc_biomes,
    cfg=cfg, tile_origin_x=24*512, tile_origin_y=9*512,
    noise_gens=noise_gens,
)
print(f"process_tile_cols:  {time.perf_counter()-t0:.2f}s")

t0 = time.perf_counter()
river_meta = core_river.carve_tile(
    tile_columns=col_results, tile_flow=masks['flow'], cfg=cfg
)
print(f"river carving:      {time.perf_counter()-t0:.2f}s")

# build_column_array timing
surface_y   = np.array([[cr.surface_y for cr in row] for row in col_results], dtype=np.int16)
surface_blk = np.full((H, W), 'grass_block', dtype=object)
sub_blk     = np.full((H, W), 'dirt',        dtype=object)
ground_cover= np.full((H, W), '',            dtype=object)

t0 = time.perf_counter()
vol, pal = core_chunk.build_column_array(
    surface_y=surface_y, surface_blk=surface_blk,
    sub_blk=sub_blk, ground_cover=ground_cover,
)
print(f"build_column_array: {time.perf_counter()-t0:.2f}s  ({vol.nbytes/(1024*1024):.0f} MB, {len(pal._names)} palette entries)")

# Profile process_tile_columns_v2 internals
print("\n--- profiling process_tile_columns_v2 ---")
pr = cProfile.Profile()
pr.enable()
col_results = core_col.process_tile_columns_v2(
    tile_height=h_u16, tile_slope=sl_u16,
    tile_erosion=er_u16, tile_flow=fl_u16,
    tile_deposits=dep_u16, tile_shore=sh_bool,
    tile_biomes=biomes, tile_mc_biomes=mc_biomes,
    cfg=cfg, tile_origin_x=24*512, tile_origin_y=9*512,
    noise_gens=noise_gens,
)
pr.disable()
s = io.StringIO()
pstats.Stats(pr, stream=s).sort_stats('tottime').print_stats(15)
print(s.getvalue())
