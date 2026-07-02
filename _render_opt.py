"""Render tile (35,79)'s masks but WRITE at a shifted world tile (k tiles east),
so multiple bush-density options land side-by-side on identical terrain.
Usage: VANDIR_SARID_BUSH_AMP=<amp> py _render_opt.py <k> <output_dir>
"""
import os, sys
sys.path.insert(0, ".")
import run_pipeline

k = int(sys.argv[1]); outdir = sys.argv[2]
masks_dir = os.environ.get("VANDIR_MASKS_DIR", "masks")
args = dict(
    tile_x=35, tile_y=79,
    config_path="config/thresholds.json",
    masks_dir=masks_dir, output_dir=outdir,
    tile_size=512, dry_run=False,
    schem_index_path="schematic_index.json",
    world_offset_x=k * 512, world_offset_z=0,
)
r = run_pipeline._process_tile(args)
print(f"opt k={k} -> region ({35+k},79)  elapsed_ms={r.get('elapsed_ms')}", flush=True)
