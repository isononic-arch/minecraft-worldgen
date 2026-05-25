"""S86: Run upscale_override_vectorized pipeline against the BT-banded composite.

Wraps upscale_override_vectorized.main() with paths swapped so:
  - INPUT (vec borders)   -> override_banded_s86.png  (already has bands baked in)
  - OVERRIDE_BASE (fill)  -> override_banded_s86.png  (identical -> composite == file)
  - OUTPUT                -> masks/override_s86_BT_bands.tif (NOT override.tif yet)

Once you've reviewed override_s86_BT_bands.tif and are happy with it, swap it in:
  mv masks/override_s86_BT_bands.tif masks/override.tif
"""
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\nicho\minecraft-worldgen")
sys.path.insert(0, str(ROOT))

import upscale_override_vectorized as u

u.INPUT         = ROOT / "override_banded_s86.png"
u.OVERRIDE_BASE = ROOT / "override_banded_s86.png"
u.OUTPUT        = ROOT / "masks" / "override_s86_BT_bands.tif"

if not u.INPUT.exists():
    raise SystemExit(f"Missing {u.INPUT}. Run tools/apply_BT_banding.py first.")

print(f"  INPUT         = {u.INPUT}")
print(f"  OVERRIDE_BASE = {u.OVERRIDE_BASE}")
print(f"  OUTPUT        = {u.OUTPUT}")
print()

u.main()
