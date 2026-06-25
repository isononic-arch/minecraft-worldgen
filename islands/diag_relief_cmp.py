"""Compare two relief dumps at their shared edge: pre_relief, post_relief, delta,
amp, smooth_gain, n. A is LEFT tile (its col -1 abuts B's col 0)."""
import sys, numpy as np
A = np.load(sys.argv[1]); B = np.load(sys.argv[2])
eA = (slice(None), -1); eB = (slice(None), 0)
rock = A["rock"][eA] & B["rock"][eB]
land = (A["post_relief"][eA] > 63) & (B["post_relief"][eB] > 63)
print(f"rough_used A={int(A['rough_used'][0])} B={int(B['rough_used'][0])}")
print(f"edge land cells={int(land.sum())}  shared-rock={int(rock.sum())}")
for f in ("pre_relief", "post_relief", "delta", "amp_eff", "smooth_gain", "n", "slope_gain", "tier", "existing_rough"):
    va = A[f][eA].astype(float); vb = B[f][eB].astype(float)
    m = rock if f in ("delta", "amp_eff", "smooth_gain", "n", "slope_gain", "tier", "existing_rough") else land
    if m.sum() == 0:
        print(f"  {f:13s}: no cells"); continue
    d = np.abs(va[m] - vb[m])
    print(f"  {f:13s} |A-B| mean={d.mean():.4f} p95={np.percentile(d,95):.4f} max={d.max():.4f}  ge3={int((d>=3).sum())}")
# total surface seam = post_relief edge diff (land)
d = np.abs(A["post_relief"][eA].astype(float) - B["post_relief"][eB].astype(float))
print(f"\nTOTAL post_relief seam over land: mean={d[land].mean():.3f} p95={np.percentile(d[land],95):.2f} max={d[land].max():.1f} ge3={int((d[land]>=3).sum())}/{int(land.sum())}")
# decompose: pre_relief seam vs delta seam
dp = np.abs(A["pre_relief"][eA].astype(float) - B["pre_relief"][eB].astype(float))
dd = np.abs(A["delta"][eA].astype(float) - B["delta"][eB].astype(float))
print(f"  of which PRE_RELIEF seam: mean={dp[land].mean():.3f} max={dp[land].max():.1f} ge3={int((dp[land]>=3).sum())}")
print(f"  of which DELTA(relief) seam: mean={dd[land].mean():.3f} max={dd[land].max():.1f} ge3={int((dd[land]>=3).sum())}")
# worst pre_relief cells
worst = np.argsort(dp * land)[::-1][:6]
print("  worst pre_relief-step cells (row, |pА-pB|, preA, preB, postA, postB):")
for w in worst:
    if not land[w]:
        continue
    print(f"    r={w:3d} step={dp[w]:.0f}  preA={A['pre_relief'][eA][w]:.0f} preB={B['pre_relief'][eB][w]:.0f}"
          f"  postA={A['post_relief'][eA][w]:.0f} postB={B['post_relief'][eB][w]:.0f}")
