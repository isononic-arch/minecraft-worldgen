#!/usr/bin/env python
"""make_mainland_runspec.py — build a render_monitor runspec for the mainland
50k render (render_50k_final.sh conventions: flag /root/done, log /root/job.log,
output/r.*.mca, rows assigned round-robin z % NBOXES).

Run AFTER render_50k_final.sh has created+dispatched its boxes (any time while
they're rendering — the monitor is resume-safe). Discovers the live vandir-50k-*
boxes from the Hetzner API, computes each box's expected region set from its
z-rows MINUS the current mainland skip-list, and writes runspec_mainland.json.

  py cloud_bake/make_mainland_runspec.py [--nboxes 8] [--ttl 330] [--keep-alive]

--keep-alive: monitor collects but does NOT delete boxes (render_50k_final's
straggler-re-render workflow). Deletion then falls to box_guard TTL / manual.
NOTE: the skip-list is read at BUILD time — regenerate ownership (V15 finalize
step 2) BEFORE dispatching mainland, and build this runspec after that.
"""
import argparse, json, sys, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GRID = 97
TOKEN = Path(r"C:\Users\nicho\.hetzner_token").read_text().strip()


def servers():
    req = urllib.request.Request(
        "https://api.hetzner.cloud/v1/servers?per_page=50",
        headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["servers"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nboxes", type=int, default=8)
    ap.add_argument("--ttl", type=int, default=330)
    ap.add_argument("--keep-alive", action="store_true")
    a = ap.parse_args()

    skip_p = ROOT / "cloud_bake" / "mainland_skip_regions_s101.txt"
    skip = {tuple(map(int, l.split()))
            for l in skip_p.read_text().splitlines() if l.strip()}

    live = {s["name"]: s for s in servers() if s["name"].startswith("vandir-50k-")}
    if not live:
        print("no vandir-50k-* servers running — dispatch render_50k_final.sh first")
        return 1

    boxes = []
    for i in range(a.nboxes):
        name = f"vandir-50k-{i}"
        s = live.get(name)
        if not s:
            print(f"!! {name} not found among live servers — skipping")
            continue
        rows = [z for z in range(GRID) if z % a.nboxes == i]
        exp = [f"r.{x}.{z}.mca" for z in rows for x in range(GRID)
               if (x, z) not in skip]
        boxes.append({
            "name": name, "id": s["id"],
            "ip": s["public_net"]["ipv4"]["ip"],
            "flag": "/root/done", "log": "/root/job.log",
            "collect": "cd /root/minecraft-worldgen && tar czf /tmp/out.tgz output",
            "remote_tar": "/tmp/out.tgz",
            "work_units": [f"row_{z}" for z in rows],
            "expected_regions": exp,
            "min_regions": int(0.98 * len(exp)),
        })
    spec = {
        "run_name": "vandir-50k", "kind": "mainland",
        "ttl_min": a.ttl, "wall_cap_min": a.ttl + 30,
        "stall_secs": 1800, "poll_secs": 60,
        "collect_retries": 3, "unreach_grace": 5,
        "collect_dir": "D:/render_50k_final/_collect",
        "keep_alive": bool(a.keep_alive),
        "boxes": boxes,
    }
    out = ROOT / "cloud_bake" / "runspec_mainland.json"
    out.write_text(json.dumps(spec, indent=1))
    tot = sum(len(b["expected_regions"]) for b in boxes)
    print(f"runspec written: {out}")
    print(f"  {len(boxes)} boxes, {tot} expected regions total "
          f"(9409 - {len(skip)} skip-listed = {GRID*GRID - len(skip)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
