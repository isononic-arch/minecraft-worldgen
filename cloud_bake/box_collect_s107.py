#!/usr/bin/env python
"""box_collect_s107.py — runs ON a render box as the monitor's collect command.
The world already went box-direct to Bloomhost (box_push.py); this stages the
SMALL audit artifacts + the contested island-overlap regions (which box_push
deliberately did NOT push — home chunk-merges + uploads them in finalize).
Output: /tmp/out.tgz

Python (not bash) ON PURPOSE: box-side shell scripts fetched via git-reset or the
code tarball arrive with CRLF line endings from the Windows working copy, which
real Linux bash mis-parses; Python's universal-newline source handling is immune.
"""
import glob, os, shutil, subprocess, sys, tarfile
from pathlib import Path

STAGE = Path("/root/collect_stage")
MW = Path("/root/minecraft-worldgen")
CONTESTED = ["r.95.103.mca", "r.96.103.mca", "r.97.122.mca", "r.97.123.mca",
             "r.97.124.mca", "r.60.101.mca", "r.60.102.mca", "r.60.103.mca",
             "r.60.104.mca", "r.100.114.mca"]


def main():
    if STAGE.exists():
        shutil.rmtree(STAGE, ignore_errors=True)
    STAGE.mkdir(parents=True)
    # small audit artifacts
    for f in ("/root/push_manifest.json", "/root/run.log", "/root/job.log",
              "/root/health.txt", "/root/md5.txt"):
        if os.path.exists(f):
            shutil.copy2(f, STAGE / os.path.basename(f))
    # contested island-vs-island regions (per-island copies -> home merges)
    for r in CONTESTED:
        for f in glob.glob(str(MW / "islands" / "out" / "*" / r)):
            isl = Path(f).parent.name
            d = STAGE / "contested" / isl
            d.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, d / r)
    # island bake manifests (tiny; merge bookkeeping)
    for m in glob.glob(str(MW / "islands" / "masks_islands" / "*" / "manifest.json")):
        isl = Path(m).parent.name
        d = STAGE / "manifests" / isl
        d.mkdir(parents=True, exist_ok=True)
        shutil.copy2(m, d / "manifest.json")
    with tarfile.open("/tmp/out.tgz", "w:gz") as tf:
        tf.add(str(STAGE), arcname=".")
    n = sum(1 for _ in STAGE.rglob("*") if _.is_file())
    print(f"staged: {n} files -> /tmp/out.tgz", flush=True)


if __name__ == "__main__":
    main()
