"""Correlate rendered surface block vs rock_layers tier over a patch."""
import sys, numpy as np
from collections import Counter, defaultdict
sys.path.insert(0, "tools")
from diag_mca_surface import read_chunk, unpack_section

MCA = "output_s89_rocksnow/r.36.15.mca"
x0, x1, z0, z1 = 18700, 18811, 7850, 7936
ROCK = {"diorite", "cobblestone", "andesite", "stone", "calcite",
        "white_concrete_powder", "light_gray_concrete_powder", "gravel",
        "tuff", "deepslate"}

tier = np.load("tier_patch.npy")  # (86,111) z,x

cache = {}
def top(wx, wz):
    rx, rz = x0 // 512, z0 // 512
    lxb, lzb = wx - rx * 512, wz - rz * 512
    ck = (lxb // 16, lzb // 16)
    if ck not in cache:
        try:
            ch = read_chunk(MCA, ck[0], ck[1]); sa = {}
            secs = ch.get("sections") or ch.root.get("sections")
            for sec in secs:
                arr = unpack_section(sec)
                if arr is not None:
                    sa[int(sec.get("Y", 0))] = arr
        except Exception:
            sa = {}
        cache[ck] = sa
    sa = cache[ck]
    for sy in sorted(sa.keys(), reverse=True):
        arr = sa[sy]
        for ly in range(15, -1, -1):
            b = arr[ly][lzb % 16][lxb % 16]
            if b is None or "air" in b:
                continue
            return b.replace("minecraft:", "").split("[")[0]
    return None

# cross-tab: for each rendered rock-block pixel, what tier is it on?
rock_by_tier = Counter()
tier_block = defaultdict(Counter)
total_by_tier = Counter()
for iz, wz in enumerate(range(z0, z1)):
    for ix, wx in enumerate(range(x0, x1)):
        t = int(tier[iz, ix])
        total_by_tier[t] += 1
        b = top(wx, wz)
        if b in ROCK:
            rock_by_tier[t] += 1
            tier_block[t][b] += 1

print("Rendered ROCK-block pixels grouped by rock_layers tier:")
for t in sorted(total_by_tier):
    n = rock_by_tier[t]; tot = total_by_tier[t]
    print(f"  tier {t}: {n:4d} rock / {tot:4d} px ({100*n/tot:4.1f}%)  blocks={dict(tier_block[t].most_common(8))}")
