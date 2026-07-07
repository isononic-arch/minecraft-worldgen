#!/usr/bin/env python
"""s107_preserve_meta.py — download VandirWorld_S106 metadata (everything EXCEPT
region/ and entities/) + server.properties from Bloomhost BEFORE the S106 delete.
Writes to D:/VandirWorld_S107_staging/meta/ and drops metadata_saved.flag that
the gate daemon REQUIRES before it will delete S106.

entities/ is deliberately skipped: entity MCAs reference S106 chunks; carrying
them over a re-rendered world risks orphaned entities. Server regenerates.
"""
import json, posixpath, stat, sys, time
from pathlib import Path
import paramiko

CREDS = json.loads(Path(r"C:\Users\nicho\.bloom_creds.json").read_text())
STAGE = Path(r"D:\VandirWorld_S107_staging")
META = STAGE / "meta"
SRC = "VandirWorld_S106"
SKIP_DIRS = {"region", "entities"}


def connect():
    t = paramiko.Transport((CREDS["host"], CREDS["port"]))
    t.connect(username=CREDS["user"], password=CREDS["pw"])
    t.default_window_size = 64 * 1024 * 1024
    return t, paramiko.SFTPClient.from_transport(t)


def walk(sftp, rdir, rel=""):
    for e in sftp.listdir_attr(rdir):
        rpath = posixpath.join(rdir, e.filename)
        relpath = posixpath.join(rel, e.filename) if rel else e.filename
        if stat.S_ISDIR(e.st_mode):
            if rel == "" and e.filename in SKIP_DIRS:
                print(f"  SKIP dir {relpath}/")
                continue
            yield from walk(sftp, rpath, relpath)
        else:
            yield rpath, relpath, e.st_size


def main():
    META.mkdir(parents=True, exist_ok=True)
    t, sftp = connect()
    n = tot = 0
    for rpath, relpath, size in walk(sftp, SRC):
        lp = META / relpath.replace("/", "\\")
        lp.parent.mkdir(parents=True, exist_ok=True)
        sftp.get(rpath, str(lp))
        got = lp.stat().st_size
        if got != size:
            print(f"!! SIZE MISMATCH {relpath}: remote {size} local {got}")
            sys.exit(2)
        n += 1
        tot += size
        print(f"  {relpath} ({size:,}B)")
    # server.properties snapshot (root level)
    sftp.get("server.properties", str(STAGE / "server.properties.s106_snapshot"))
    t.close()
    (STAGE / "metadata_saved.flag").write_text(
        json.dumps({"files": n, "bytes": tot, "when": time.strftime("%Y-%m-%d %H:%M:%S")}))
    print(f"DONE: {n} files, {tot:,} bytes -> {META}")


if __name__ == "__main__":
    main()
