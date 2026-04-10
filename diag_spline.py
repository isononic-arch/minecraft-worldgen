import sys
sys.path.insert(0, r'C:\Users\nicho\minecraft-worldgen')
from core.column_generator import gaea_to_mc_y

# Test the top-level spline function
test_vals = [0, 1000, 8000, 17050, 45000, 65496, 65535]
print("=== gaea_to_mc_y() results ===")
for v in test_vals:
    print(f"  {v:6d} -> MC Y {gaea_to_mc_y(v)}")

# Test the LUT built by process_tile_columns_v2
import core.column_generator as cg
lut = None
if hasattr(cg, '_LUT'):
    lut = cg._LUT
    print("\n=== _LUT (module-level) ===")
    for v in test_vals:
        print(f"  {v:6d} -> MC Y {lut[v]}")
else:
    print("\n_LUT not found at module level — it may be built inline in process_tile_columns_v2")
    import inspect
    src = inspect.getsource(cg)
    idx = src.find('_build_lut_vectorized')
    if idx >= 0:
        print(src[idx:idx+600])
    else:
        idx2 = src.find('gaea_in')
        if idx2 >= 0:
            print(src[max(0,idx2-100):idx2+400])
