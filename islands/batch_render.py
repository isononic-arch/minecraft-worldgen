"""batch_render.py — render a curated list of islands sequentially (fast mode,
2 threads, memory-safe for the 7.4GB box), top-down each as it completes.
Designed to run unattended overnight. Logs to islands/out/batch.log via redirect.
"""
import sys, json, time, subprocess
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
ISL = ROOT / "islands"
PY = sys.executable

# curated showcase, small/fast -> big, max climate variety
SHOWCASE = ["grand_turk", "ouvea", "fogo_island", "efate", "kostati",
            "new_vincentia", "madre_de_dios"]


def _safe(n):
    import re
    return re.sub(r"[^a-z0-9]+", "_", n.lower()).strip("_")


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", default=",".join(SHOWCASE))
    ap.add_argument("--threads", type=int, default=2)
    # S95-fix: render WITH schematics by default (mainland parity). --fast skips
    # them for the memory-tight 7.4GB box (the old hardcoded fast=True is why the
    # showcase had no trees).
    ap.add_argument("--fast", action="store_true", help="skip schematics (RAM-saver)")
    a = ap.parse_args()
    keys = [k.strip() for k in a.list.split(",") if k.strip()]
    layout = json.loads((ISL / "layout.json").read_text())
    from islands.render_drive import render_island
    t0 = time.time()
    for k in keys:
        entry = next((i for i in layout["islands"] if k in _safe(i["name"]) or k in i["dem_path"]), None)
        if entry is None:
            print(f"[batch] SKIP {k}: not in layout", flush=True); continue
        mdir = ISL / "masks_islands" / _safe(entry["name"])
        if not (mdir / "height.tif").exists():
            print(f"[batch] SKIP {k}: not baked", flush=True); continue
        print(f"\n[batch] ===== {k} ===== ({(time.time()-t0)/60:.0f}m elapsed)", flush=True)
        try:
            render_island(entry, threads=a.threads, fast=a.fast)
        except Exception as e:
            print(f"[batch] {k} render FAILED: {e}", flush=True); continue
        # top-down it
        try:
            subprocess.run([PY, str(ISL / "topdown_mca.py"), "--name", k, "--maxpx", "1200"],
                           cwd=str(ROOT), timeout=600)
        except Exception as e:
            print(f"[batch] {k} topdown failed: {e}", flush=True)
    print(f"\n[batch] ALL DONE in {(time.time()-t0)/60:.0f}m", flush=True)


if __name__ == "__main__":
    main()
