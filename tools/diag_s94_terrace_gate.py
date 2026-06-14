"""S94 terrace-restore gate. Run on the dump dir after rendering.
Checks (84,60): cross-section uniformity up, levels down, NO new dry pokes,
water still <= bank (no levees). Seam (29,12)|(30,12): water continuous across
the shared X=15360 boundary.
Usage: py tools/diag_s94_terrace_gate.py <dump_dir>
"""
import sys, numpy as np
d = sys.argv[1] if len(sys.argv) > 1 else "diag_s94_tr"

def load(tx, tz):
    rwy = np.load(f"{d}/rwy_{tx}_{tz}.npy").astype(np.int32)
    sy = np.load(f"{d}/sy_final_{tx}_{tz}.npy").astype(np.int32)
    rm = np.load(f"{d}/rmeta9_{tx}_{tz}.npy")
    riv = ((rm == 1) | (rm == 2)) & (rwy > 63)
    return rwy, sy, riv

def uniformity(tag, rwy, sy, riv):
    n = int(riv.sum())
    if n == 0:
        print(f"  [{tag}] no river"); return
    big = 1 << 20; rv = np.where(riv, rwy, big); dd = np.zeros_like(rwy)
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            if dr == dc == 0: continue
            sh = np.roll(np.roll(rv, dr, 0), dc, 1); ok = riv & (sh < big)
            dd = np.where(ok, np.maximum(dd, np.abs(rwy - sh)), dd)
    s = dd[riv]
    depth = (rwy - sy)[riv]
    drypokes = int((depth < 1).sum())
    print(f"  [{tag}] cells={n} levels={len(np.unique(rwy[riv]))} "
          f"uniform0={100*(s==0).sum()/n:.0f}% >=2={100*(s>=2).sum()/n:.0f}% maxspread={s.max()} "
          f"| depth p50={int(np.median(depth))} min={int(depth.min())} DRY_POKES={drypokes}")

print(f"=== (84,60) wide channel ===")
rwy, sy, riv = load(84, 60)
uniformity("84,60", rwy, sy, riv)

# seam: (29,12) right edge col 511 vs (30,12) left edge col 0, same z rows
print(f"=== seam (29,12)|(30,12) at X=15360 ===")
try:
    a_rwy, a_sy, a_riv = load(29, 12)   # left tile, right edge = col 511
    b_rwy, b_sy, b_riv = load(30, 12)   # right tile, left edge = col 0
    aE = a_rwy[:, 511]; aR = a_riv[:, 511]
    bE = b_rwy[:, 0];   bR = b_riv[:, 0]
    both = aR & bR
    nb = int(both.sum())
    if nb:
        jump = np.abs(aE[both] - bE[both])
        print(f"  shared river rows on seam: {nb}; |waterΔ| max={int(jump.max())} "
              f"mean={jump.mean():.2f} ; rows with Δ>=2: {int((jump>=2).sum())}")
        print(f"  -> {'SEAM CLEAN' if jump.max()<=1 else 'SEAM JUMP — investigate'}")
    else:
        print("  no shared river rows on this seam (river may not cross here)")
except FileNotFoundError as e:
    print(f"  seam tiles not both present: {e}")
