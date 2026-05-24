"""Normalize schematic_index.json paths to be relative + cross-platform.

One-time fix: paths in the index were stored as absolute Windows paths like
`C:\\Users\\nicho\\minecraft-worldgen\\Vegetation\\foo.schem` which fail to
resolve on Linux (cloud render boxes). After this script, paths are stored as
`Vegetation/foo.schem` — relative from project root, works on any OS.

Run from project root:
    py tools/fix_schematic_index_paths.py
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path


def main() -> None:
    index_path = Path("schematic_index.json")
    if not index_path.is_file():
        raise SystemExit(f"missing: {index_path}")

    with index_path.open() as f:
        idx = json.load(f)

    win_prefix_bs = "C:\\Users\\nicho\\minecraft-worldgen\\"
    win_prefix_fs = "C:/Users/nicho/minecraft-worldgen/"

    total = fixed = missing = 0
    for biome_key, entries in idx.items():
        if not isinstance(entries, list):
            continue
        for e in entries:
            if not isinstance(e, dict):
                continue
            total += 1
            p = e.get("path", "")
            if p.startswith(win_prefix_bs):
                p = p[len(win_prefix_bs):]
                fixed += 1
            elif p.startswith(win_prefix_fs):
                p = p[len(win_prefix_fs):]
                fixed += 1
            # Normalize to forward slashes (works on both platforms)
            p = p.replace("\\", "/")
            e["path"] = p
            if not os.path.exists(p):
                missing += 1

    print(f"Total entries: {total}")
    print(f"Paths normalized (Windows-absolute -> relative): {fixed}")
    print(f"Files missing on disk: {missing}")

    fd, tmp = tempfile.mkstemp(suffix=".json", dir=".")
    os.close(fd)
    with open(tmp, "w") as f:
        json.dump(idx, f, indent=2)
    shutil.move(tmp, str(index_path))
    print(f"Wrote normalized {index_path}")

    # Verify
    with index_path.open() as f:
        idx2 = json.load(f)
    sample = idx2["birch"][0]["path"]
    print(f"Sample after: {sample}")
    print(f"Sample exists on disk: {os.path.exists(sample)}")


if __name__ == "__main__":
    main()
