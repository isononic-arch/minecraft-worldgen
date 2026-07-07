#!/usr/bin/env python
"""s107_finalize.py — after both render phases pushed box-direct to Bloomhost:

  1. Extract the 10 contested island-overlap regions from the island collect
     tarballs, chunk-merge them (install_islands read/write_region; bigger
     island written LAST so it wins contested chunks — fast_install semantics),
     upload merged regions.
  2. Upload the preserved S106 metadata (level.dat with LevelName rewritten,
     datapacks/vandir_height.zip, playerdata, stats, advancements, data/,
     paper-world.yml, uid.dat — NOT session.lock, NOT entities).
  3. server.properties: backup remote as server.properties.pre_s107.bak, set
     level-name=VandirWorld_S107.
  4. Audit: remote region listing vs the S106 inventory
     (cloud_bake/_bloom_s106_regions.json — footprint is identical by design),
     zero-byte check, contested-region presence.
  5. Cleanup: remove .go/.push_lock; delete VandirWorld_S106_nether/_the_end.

Writes s107_status/finalize_done.json on success. Idempotent — safe to re-run.
"""
from __future__ import annotations
import json, posixpath, re, stat as statmod, sys, tarfile, time
from datetime import datetime
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from islands.install_islands import read_region, write_region  # noqa: E402

STATUS = ROOT / "s107_status"
STAGE = Path(r"D:\VandirWorld_S107_staging")
CREDS = json.loads(Path(r"C:\Users\nicho\.bloom_creds.json").read_text())
DEST = "VandirWorld_S107"
CONTESTED = ["r.95.103.mca", "r.96.103.mca", "r.97.122.mca", "r.97.123.mca",
             "r.97.124.mca", "r.60.101.mca", "r.60.102.mca", "r.60.103.mca",
             "r.60.104.mca", "r.100.114.mca"]
COLLECT_DIRS = [ROOT / "islands" / "_collect_v18", ROOT / "islands" / "_collect_v18r"]
# render-derived EXACT expected set: (mainland grid - skip-list) U (island
# ownership) = 10,572. NOT _bloom_s106_regions.json (mainland-only 9203) nor the
# S106 remote snapshot (10,583 incl. ~11 runtime-gen spawn-frontier regions).
INVENTORY = ROOT / "cloud_bake" / "_s107_expected_regions.json"


def log(msg):
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    STATUS.mkdir(exist_ok=True)
    with open(STATUS / "finalize.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def connect():
    t = paramiko.Transport((CREDS["host"], CREDS["port"]),
                           default_window_size=64 * 1024 * 1024)
    t.connect(username=CREDS["user"], password=CREDS["pw"])
    return t, paramiko.SFTPClient.from_transport(t)


def sftp_exists(s, p):
    try:
        s.stat(p)
        return True
    except IOError:
        return False


def extract_contested():
    """collect tarballs -> staging contested/<island>/r.X.Z.mca + manifests."""
    outd = STAGE / "contested_src"
    outd.mkdir(parents=True, exist_ok=True)
    found = 0
    for cdir in COLLECT_DIRS:
        if not cdir.exists():
            continue
        for tgz in cdir.glob("*.tgz"):
            with tarfile.open(tgz, "r:gz") as tf:
                for m in tf.getmembers():
                    parts = Path(m.name).parts
                    if not m.isfile():
                        continue
                    if "contested" in parts or Path(m.name).name == "manifest.json":
                        rel = Path(*[p for p in parts if p not in (".",)])
                        dst = outd / rel
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        dst.write_bytes(tf.extractfile(m).read())
                        if str(rel).endswith(".mca"):
                            found += 1
    log(f"extracted {found} contested region copies -> {outd}")
    return outd


def island_area(outd: Path, isl: str) -> int:
    for base in (outd / "manifests" / isl / "manifest.json",
                 ROOT / "islands" / "masks_islands" / isl / "manifest.json"):
        if base.exists():
            wh = json.loads(base.read_text()).get("world_hw", [0, 0])
            return wh[0] * wh[1]
    return 0


def merge_and_upload(outd: Path):
    cdir = outd / "contested"
    merged_dir = STAGE / "contested_merged"
    merged_dir.mkdir(parents=True, exist_ok=True)
    by_region: dict[str, list] = {}
    if cdir.exists():
        for isl_dir in cdir.iterdir():
            for mca in isl_dir.glob("r.*.mca"):
                by_region.setdefault(mca.name, []).append(
                    (island_area(outd, isl_dir.name), isl_dir.name, mca))
    missing = [r for r in CONTESTED if r not in by_region]
    if missing:
        log(f"!! WARNING: no source copies for contested {missing} — "
            f"those regions will be ABSENT from the world")
    ups = []
    for rname, lst in sorted(by_region.items()):
        merged: dict = {}
        for area, isl, mca in sorted(lst):          # smaller first, bigger wins
            ch = read_region(mca)
            merged.update(ch)
            log(f"  {rname}: {isl} (area {area}) contributes {len(ch)} chunks")
        dst = merged_dir / rname
        write_region(dst, merged)
        ups.append(dst)
        log(f"  {rname}: merged {len(merged)} chunks -> {dst.stat().st_size:,}B")
    t, s = connect()
    for p in ups:
        s.put(str(p), f"{DEST}/region/{p.name}")
        if s.stat(f"{DEST}/region/{p.name}").st_size != p.stat().st_size:
            log(f"!! upload size mismatch {p.name}")
            t.close()
            return False
    t.close()
    log(f"uploaded {len(ups)} merged contested regions")
    return True


def rewrite_levelname(level_dat: Path) -> Path:
    out = STAGE / "level.dat.s107"
    try:
        import nbtlib
        f = nbtlib.load(str(level_dat))
        data = f["Data"] if "Data" in f else f[""]["Data"]
        data["LevelName"] = nbtlib.String("VandirWorld_S107")
        f.save(str(out))
        log("level.dat LevelName -> VandirWorld_S107")
        return out
    except Exception as e:
        log(f"!! nbtlib LevelName edit failed ({e}) — uploading unmodified "
            f"level.dat (folder name is what Paper uses; cosmetic only)")
        return level_dat


def upload_metadata():
    meta = STAGE / "meta"
    lvl = rewrite_levelname(meta / "level.dat")
    t, s = connect()

    def ensure_dir(rp):
        parts = rp.split("/")
        for i in range(1, len(parts) + 1):
            d = "/".join(parts[:i])
            if not sftp_exists(s, d):
                s.mkdir(d)

    n = 0
    for p in meta.rglob("*"):
        if p.is_dir():
            continue
        rel = p.relative_to(meta).as_posix()
        if rel == "session.lock":
            continue
        src = lvl if rel == "level.dat" else p
        rp = f"{DEST}/{rel}"
        ensure_dir(posixpath.dirname(rp))
        s.put(str(src), rp)
        n += 1
    t.close()
    log(f"uploaded {n} metadata files")
    return True


def flip_server_properties():
    t, s = connect()
    with s.open("server.properties", "r") as f:
        cur = f.read().decode()
    with s.open("server.properties.pre_s107.bak", "w") as f:
        f.write(cur)
    new = re.sub(r"(?m)^level-name=.*$", f"level-name={DEST}", cur)
    if f"level-name={DEST}" not in new:
        log("!! level-name line not found — appending")
        new = cur.rstrip("\n") + f"\nlevel-name={DEST}\n"
    with s.open("server.properties", "w") as f:
        f.write(new)
    t.close()
    log(f"server.properties: level-name={DEST} (backup: server.properties.pre_s107.bak)")
    return True


def audit():
    t, s = connect()
    remote = {e.filename: e.st_size for e in s.listdir_attr(f"{DEST}/region")
              if e.filename.endswith(".mca")}
    t.close()
    want = set(json.loads(INVENTORY.read_text()))
    have = set(remote)
    zero = sorted(n for n, sz in remote.items() if sz == 0)
    missing, extra = sorted(want - have), sorted(have - want)
    log(f"AUDIT: {len(have)} regions remote; expected {len(want)}; "
        f"missing {len(missing)}; extra {len(extra)}; zero-byte {len(zero)}")
    for x in missing[:10]:
        log(f"  MISSING {x}")
    for x in extra[:10]:
        log(f"  EXTRA   {x}")
    for x in zero[:10]:
        log(f"  ZEROBYTE {x}")
    con_missing = [r for r in CONTESTED if r not in have]
    if con_missing:
        log(f"  !! contested absent: {con_missing}")
    ok = not missing and not zero
    log(f"AUDIT {'PASS' if ok else 'FAIL'}")
    return ok, {"regions": len(have), "missing": len(missing),
                "extra": len(extra), "zero": len(zero)}


def rm_rf(s, rdir):
    files, dirs = [], []
    def walk(d):
        for e in s.listdir_attr(d):
            p = posixpath.join(d, e.filename)
            if statmod.S_ISDIR(e.st_mode):
                walk(p)
                dirs.append(p)
            else:
                files.append(p)
    walk(rdir)
    for p in files:
        try:
            s.remove(p)
        except IOError:
            pass
    for d in sorted(dirs, key=len, reverse=True):
        try:
            s.rmdir(d)
        except IOError:
            pass
    s.rmdir(rdir)


def cleanup():
    t, s = connect()
    for p in (f"{DEST}/.go", f"{DEST}/.push_lock/owner"):
        try:
            s.remove(p)
        except IOError:
            pass
    try:
        s.rmdir(f"{DEST}/.push_lock")
    except IOError:
        pass
    for w in ("VandirWorld_S106_nether", "VandirWorld_S106_the_end"):
        if sftp_exists(s, w):
            try:
                rm_rf(s, w)
                log(f"deleted {w}")
            except Exception as e:
                log(f"!! delete {w} failed: {e} (non-fatal)")
    t.close()


def main():
    log("=== finalize start ===")
    if not (STAGE / "metadata_saved.flag").exists():
        log("CRITICAL: metadata flag missing — abort")
        return 1
    outd = extract_contested()
    if not merge_and_upload(outd):
        return 1
    upload_metadata()
    flip_server_properties()
    cleanup()
    ok, stats = audit()
    (STATUS / "finalize_done.json").write_text(json.dumps(
        {"when": str(datetime.now()), "audit_pass": ok, **stats}))
    log(f"=== finalize done (audit_pass={ok}) ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
