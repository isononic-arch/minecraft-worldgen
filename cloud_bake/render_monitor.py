#!/usr/bin/env python
"""render_monitor.py — live watchdog for multi-box cloud renders (mainland +
islands). Catches errors, flags them, and applies BOUNDED auto-fixes while the
render runs; leaves an audit trail of every action. Complements (does not
replace) box_guard.py — the guard is the billing backstop, the monitor is the
run babysitter.

WHAT IT DOES per poll tick, per box:
  1. Probe over ssh (one round-trip): done-flag, log size, log tail.
  2. Classify: DONE | RUN | STALL (log frozen > stall_secs) | FAIL (crash
     signature in tail) | UNREACHABLE (ssh failed — transient-tolerant).
  3. Act (all actions bounded + logged):
     - DONE      -> collect (tar+scp, `collect_retries` bounded), VERIFY the
                    tarball (readable, >= min_regions .mca entries, region
                    names within the box's expected set if provided), then
                    delete the box. Verify-fail counts as a collect retry.
     - FAIL      -> save full run.log locally; if the tail matches a
                    `transient_patterns` entry AND the box hasn't been
                    restarted yet -> restart the tmux job once; else reap
                    (save log, delete box, record work unit in refire list).
     - STALL     -> same as FAIL without the restart (a stall after restart
                    reaps).
     - UNREACHABLE -> tolerated `unreach_grace` consecutive ticks, then reap.
     - TTL       -> box age > ttl_min: force-collect whatever exists, reap.
  4. Global: wall-clock cap; on exit (any path) a safety sweep deletes every
     server whose name matches the runspec prefix; final splice-guard checks.

SPLICE GUARD (after all boxes resolve):
  - islands: union of collected region names vs islands/region_ownership_s101.json
    (missing/extra per island); assert none collide with mainland LAND (the
    manifest's mainland_collisions must stay empty).
  - mainland: collected region names must NOT include any region in
    cloud_bake/mainland_skip_regions_s101.txt (those are island-owned).

RUNSPEC (JSON, written by the dispatch script — see cloud_bake/runspec.example.json):
{
  "run_name": "v15",                      // safety-sweep prefix = box name prefix
  "kind": "islands",                      // islands | mainland
  "ttl_min": 120, "wall_cap_min": 240,
  "stall_secs": 900, "poll_secs": 45,
  "collect_dir": "islands/_collect_v15",
  "boxes": [
    {"name": "v15-b1", "id": 12345, "ip": "1.2.3.4",
     "flag": "/root/all_done", "log": "/root/run.log",
     "collect": "cd /root/minecraft-worldgen && tar czf /tmp/out.tgz islands/out",
     "remote_tar": "/tmp/out.tgz",
     "job_restart": "cd /root/minecraft-worldgen && tmux new-session -d -s isl '...'",
     "work_units": ["17_288", "13_130"],  // for the refire list
     "expected_regions": ["r.8.21.mca", ...],   // optional, from ownership manifest
     "min_regions": 10}
  ]
}

Usage:
  py cloud_bake/render_monitor.py <runspec.json> [--dry-run]
  py cloud_bake/render_monitor.py <runspec.json> --status   # one tick, no actions

State: <collect_dir>/monitor_state.json (resume-safe: re-running skips
already-collected boxes). Log: <collect_dir>/monitor.log.
"""
from __future__ import annotations
import json, re, subprocess, sys, time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKEN_FILE = Path(r"C:\Users\nicho\.hetzner_token")
API = "https://api.hetzner.cloud/v1"

SSHO = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30"]

CRASH_PATTERNS = [r"Traceback \(most recent call last\)", r"FileNotFoundError",
                  r"MemoryError", r"\bKilled\b", r"No space left", r"\bOOM\b",
                  r"PUSH_GATE_TIMEOUT", r"PUSH_FATAL"]
# crash signatures worth ONE tmux restart before reaping (box-local flakes)
TRANSIENT_PATTERNS = [r"Connection reset", r"BrokenPipeError", r"Errno 104"]

DEFAULTS = dict(stall_secs=900, poll_secs=45, collect_retries=3,
                unreach_grace=4, ttl_min=180, wall_cap_min=300, min_regions=1)


class Mon:
    def __init__(self, spec_path: Path, dry: bool):
        self.spec = json.loads(spec_path.read_text())
        for k, v in DEFAULTS.items():
            self.spec.setdefault(k, v)
        self.dry = dry
        self.cdir = ROOT / self.spec["collect_dir"]
        self.cdir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.cdir / "monitor_state.json"
        self.state = (json.loads(self.state_path.read_text())
                      if self.state_path.exists() else {"boxes": {}, "refire": []})
        self.t0 = time.time()

    # ---------- plumbing ----------
    def log(self, msg: str):
        line = f"[{datetime.now():%H:%M:%S}] {msg}"
        print(line, flush=True)
        with open(self.cdir / "monitor.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def save_state(self):
        self.state_path.write_text(json.dumps(self.state, indent=1))

    def bstate(self, name: str) -> dict:
        return self.state["boxes"].setdefault(name, dict(
            status="RUN", collected=False, deleted=False, restarts=0,
            collect_fails=0, unreach=0, last_log_size=-1, last_change=time.time()))

    def ssh(self, ip: str, cmd: str, timeout=60) -> tuple[int, str]:
        r = subprocess.run(["ssh", *SSHO, f"root@{ip}", cmd],
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout

    def api(self, path: str, method="GET"):
        import urllib.request, urllib.error
        req = urllib.request.Request(API + path, method=method, headers={
            "Authorization": f"Bearer {TOKEN_FILE.read_text().strip()}"})
        try:
            import contextlib
            with contextlib.closing(urllib.request.urlopen(req, timeout=30)) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            return {"error": e.code}

    def delete_box(self, box: dict, why: str):
        st = self.bstate(box["name"])
        if st["deleted"]:
            return
        self.log(f"  ACTION delete {box['name']} (id={box.get('id','?')}) — {why}")
        if not self.dry and box.get("id"):
            self.api(f"/servers/{box['id']}", method="DELETE")
        st["deleted"] = True

    # ---------- probe + classify ----------
    def probe(self, box: dict) -> str:
        st = self.bstate(box["name"])
        flag, logp = box.get("flag", "/root/all_done"), box.get("log", "/root/run.log")
        try:
            rc, out = self.ssh(box["ip"],
                f'test -f {flag} && echo __DONE__; '
                f'echo __SIZE__$(wc -c < {logp} 2>/dev/null || echo 0); '
                f'tail -c 4000 {logp} 2>/dev/null')
        except subprocess.TimeoutExpired:
            rc, out = 255, ""
        if rc != 0:
            st["unreach"] += 1
            return "UNREACHABLE"
        st["unreach"] = 0
        if "__DONE__" in out:
            return "DONE"
        for pat in CRASH_PATTERNS:
            if re.search(pat, out):
                st["crash_tail"] = out[-1500:]
                return "FAIL"
        m = re.search(r"__SIZE__(\d+)", out)
        size = int(m.group(1)) if m else 0
        if size != st["last_log_size"]:
            st["last_log_size"] = size
            st["last_change"] = time.time()
        elif time.time() - st["last_change"] > self.spec["stall_secs"]:
            return "STALL"
        return "RUN"

    # ---------- actions ----------
    def save_log(self, box: dict, suffix: str):
        try:
            rc, out = self.ssh(box["ip"], f"cat {box.get('log','/root/run.log')}", timeout=120)
            (self.cdir / f"{box['name']}.{suffix}.log").write_text(out, encoding="utf-8")
            self.log(f"  saved {box['name']} run.log ({len(out):,}B) as .{suffix}.log")
        except Exception as e:
            self.log(f"  !! could not save log for {box['name']}: {e}")

    def _manifest_from_tgz(self, tgz: Path) -> dict | None:
        """Extract push_manifest.json from a collect tarball (S107 push flow)."""
        import tarfile
        try:
            with tarfile.open(tgz, "r:gz") as tf:
                for m in tf.getmembers():
                    if Path(m.name).name == "push_manifest.json":
                        return json.loads(tf.extractfile(m).read().decode())
        except Exception:
            return None
        return None

    def verify_tarball(self, box: dict, tgz: Path) -> str | None:
        """Return None if OK, else the reason string."""
        if not tgz.exists() or tgz.stat().st_size < 1024:
            return "tarball missing/empty"
        if box.get("verify") == "push_manifest":
            # S107 box-direct-to-Bloomhost flow: the tarball carries the push
            # manifest (+ contested regions), not the world. Verify the PUSH.
            man = self._manifest_from_tgz(tgz)
            if man is None:
                return "push_manifest.json not found in tarball"
            if not man.get("remote_verified"):
                return "manifest says remote_verified=false"
            if man.get("failed"):
                return f"manifest lists {len(man['failed'])} failed pushes"
            need = int(box.get("min_regions", self.spec["min_regions"]))
            pushed = set(man.get("pushed", []))
            if len(pushed) < need:
                return f"only {len(pushed)} pushed (< {need})"
            exp = box.get("expected_regions")
            if exp:
                missing = sorted(set(exp) - pushed - set(man.get("excluded", [])))
                if missing:
                    return f"missing {len(missing)} expected regions e.g. {missing[:4]}"
            return None
        r = subprocess.run(["tar", "tzf", str(tgz)], capture_output=True, text=True)
        if r.returncode != 0:
            return f"tar unreadable: {r.stderr[:120]}"
        mcas = [Path(l).name for l in r.stdout.splitlines() if l.endswith(".mca")]
        need = int(box.get("min_regions", self.spec["min_regions"]))
        if len(mcas) < need:
            return f"only {len(mcas)} .mca entries (< {need})"
        exp = box.get("expected_regions")
        if exp:
            missing = sorted(set(exp) - set(mcas))
            if missing:
                return f"missing {len(missing)} expected regions e.g. {missing[:4]}"
        return None

    def collect(self, box: dict) -> bool:
        st = self.bstate(box["name"])
        tgz = self.cdir / f"{box['name']}.tgz"
        remote = box.get("remote_tar", "/tmp/out.tgz")
        self.log(f"  ACTION collect {box['name']} (attempt {st['collect_fails'] + 1})")
        if self.dry:
            return True
        try:
            rc, _ = self.ssh(box["ip"], box.get(
                "collect", "cd /root/minecraft-worldgen && tar czf /tmp/out.tgz islands/out"),
                timeout=900)
            r = subprocess.run(["scp", *SSHO, "-q", f"root@{box['ip']}:{remote}", str(tgz)],
                               capture_output=True, text=True, timeout=1800)
            bad = self.verify_tarball(box, tgz) if r.returncode == 0 else f"scp rc={r.returncode}"
        except subprocess.TimeoutExpired:
            bad = "collect/scp timeout"
        if bad is None:
            st["collected"] = True
            self.log(f"  collected {box['name']} OK ({tgz.stat().st_size/1e6:.0f} MB)")
            return True
        st["collect_fails"] += 1
        self.log(f"  !! collect {box['name']} FAILED: {bad} "
                 f"({st['collect_fails']}/{self.spec['collect_retries']})")
        return False

    def reap(self, box: dict, why: str, save_log_suffix: str | None = None,
             try_collect_partial: bool = False):
        st = self.bstate(box["name"])
        if save_log_suffix:
            self.save_log(box, save_log_suffix)
        if try_collect_partial and not st["collected"]:
            self.log(f"  attempting partial collect of {box['name']} before delete")
            self.collect(box)
        self.delete_box(box, why)
        if not st["collected"] and box.get("work_units"):
            for wu in box["work_units"]:
                if wu not in self.state["refire"]:
                    self.state["refire"].append(wu)
            self.log(f"  REFIRE queued: {box['work_units']} "
                     f"(see {self.cdir / 'monitor_state.json'})")
        st["status"] = f"REAPED({why})"

    def restart_job(self, box: dict) -> bool:
        st = self.bstate(box["name"])
        cmd = box.get("job_restart")
        if not cmd or st["restarts"] >= 1:
            return False
        st["restarts"] += 1
        self.log(f"  ACTION restart job on {box['name']} (1-shot; transient crash)")
        if not self.dry:
            try:
                self.ssh(box["ip"], f"tmux kill-server 2>/dev/null; rm -f {box.get('flag','/root/all_done')}; {cmd}")
            except Exception as e:
                self.log(f"  !! restart failed: {e}")
                return False
        st["last_change"] = time.time()
        return True

    # ---------- splice guard ----------
    def splice_guard(self):
        self.log("=== splice guard ===")
        own_p = ROOT / "islands" / "region_ownership_s101.json"
        skip_p = ROOT / "cloud_bake" / "mainland_skip_regions_s101.txt"
        collected = set()
        for tgz in self.cdir.glob("*.tgz"):
            man = self._manifest_from_tgz(tgz)
            if man is not None:
                # S107 push flow: pushed + excluded (contested, merged at home)
                collected |= set(man.get("pushed", [])) | set(man.get("excluded", []))
            else:
                r = subprocess.run(["tar", "tzf", str(tgz)], capture_output=True, text=True)
                collected |= {Path(l).name for l in r.stdout.splitlines() if l.endswith(".mca")}
        self.log(f"collected {len(collected)} distinct region files (pushed+excluded for push-flow boxes)")
        if self.spec["kind"] == "islands" and own_p.exists():
            own = json.loads(own_p.read_text())
            # mainland_collisions == island-owned regions inside the mainland grid ==
            # exactly the mainland skip list (S101-verified 0 mainland LAND in them).
            # The invariant to guard: manifest and skip-list FILE must agree, else one
            # is stale and the mainland render will skip the wrong set.
            if skip_p.exists():
                mc = {(x, z) for x, z in own.get("mainland_collisions", [])}
                sk = {tuple(map(int, l.split())) for l in skip_p.read_text().splitlines() if l.strip()}
                if mc != sk:
                    self.log(f"!! FLAG ownership manifest vs mainland skip-list DISAGREE "
                             f"(manifest {len(mc)}, file {len(sk)}) — regenerate "
                             f"islands/_gen_region_ownership_s101.py before the mainland render")
            want = {f"r.{x}.{z}.mca" for regs in own["islands"].values() for x, z in regs}
            missing, extra = sorted(want - collected), sorted(collected - want)
            self.log(f"vs ownership manifest: missing {len(missing)} extra {len(extra)}")
            for m in missing[:10]:
                self.log(f"  MISSING {m}")
            for e in extra[:10]:
                self.log(f"  EXTRA   {e} (not in manifest — investigate before install)")
        if self.spec["kind"] == "mainland" and skip_p.exists():
            skip = {f"r.{l.split()[0]}.{l.split()[1]}.mca"
                    for l in skip_p.read_text().splitlines() if l.strip()}
            bad = sorted(collected & skip)
            if bad:
                self.log(f"!! FLAG {len(bad)} collected mainland regions are ISLAND-OWNED "
                         f"(skip-list breach — do NOT install these): {bad[:6]}")
            else:
                self.log("skip-list respected: 0 island-owned regions in mainland output")

    # ---------- main loop ----------
    def run(self, status_only=False):
        boxes = self.spec["boxes"]
        self.log(f"=== monitor start: {self.spec['run_name']} kind={self.spec['kind']} "
                 f"{len(boxes)} boxes ttl={self.spec['ttl_min']}m "
                 f"cap={self.spec['wall_cap_min']}m dry={self.dry} ===")
        while True:
            open_boxes = 0
            for box in boxes:
                st = self.bstate(box["name"])
                if st["collected"] and st["deleted"]:
                    continue
                if st["status"].startswith("REAPED"):
                    continue
                open_boxes += 1
                cls = self.probe(box)
                age_min = (time.time() - self.t0) / 60
                self.log(f"{box['name']}: {cls} (log {st['last_log_size']:,}B, "
                         f"restarts {st['restarts']}, elapsed {age_min:.0f}m)")
                if status_only:
                    continue
                if cls == "DONE":
                    if self.collect(box):
                        if self.spec.get("keep_alive"):
                            st["deleted"] = True   # resolved for the loop; box stays up
                            self.log(f"  {box['name']} kept ALIVE per spec "
                                     f"(straggler re-render workflow; box_guard TTL is the net)")
                        else:
                            self.delete_box(box, "collected")
                    elif st["collect_fails"] >= self.spec["collect_retries"]:
                        self.reap(box, "collect-retries-exhausted", save_log_suffix="COLLECTFAIL")
                elif cls == "FAIL":
                    tail = st.get("crash_tail", "")
                    if any(re.search(p, tail) for p in TRANSIENT_PATTERNS) and self.restart_job(box):
                        pass
                    else:
                        self.reap(box, "crash", save_log_suffix="FAIL", try_collect_partial=True)
                elif cls == "STALL":
                    self.reap(box, "stall", save_log_suffix="STALL", try_collect_partial=True)
                elif cls == "UNREACHABLE":
                    if st["unreach"] >= self.spec["unreach_grace"]:
                        self.reap(box, "unreachable", try_collect_partial=False)
                if age_min > self.spec["ttl_min"]:
                    self.reap(box, "ttl", save_log_suffix="TTL", try_collect_partial=True)
            self.save_state()
            if status_only or open_boxes == 0:
                break
            if (time.time() - self.t0) / 60 > self.spec["wall_cap_min"]:
                self.log("!! wall cap — reaping all remaining")
                for box in boxes:
                    st = self.bstate(box["name"])
                    if not st["deleted"]:
                        self.reap(box, "wall-cap", save_log_suffix="WALLCAP",
                                  try_collect_partial=True)
                self.save_state()
                break
            time.sleep(self.spec["poll_secs"])
        # safety sweep by prefix, then splice guard
        if not status_only:
            pref = self.spec["run_name"]
            if self.spec.get("keep_alive"):
                self.log(f"keep_alive: skipping safety sweep — {pref}* boxes remain up "
                         f"(box_guard TTL labels are the only net; delete manually when satisfied)")
            else:
                d = self.api("/servers")
                for s in d.get("servers", []):
                    if s["name"].startswith(pref):
                        self.log(f"safety-sweep: deleting leftover {s['name']} (id={s['id']})")
                        if not self.dry:
                            self.api(f"/servers/{s['id']}", method="DELETE")
            self.splice_guard()
            if self.state["refire"]:
                self.log(f"=== REFIRE NEEDED: {self.state['refire']} ===")
            self.log("=== monitor done ===")


def main():
    args = [a for a in sys.argv[1:]]
    if not args:
        print(__doc__)
        return 2
    spec = Path(args[0])
    if not spec.exists():
        print(f"runspec not found: {spec}")
        return 2
    m = Mon(spec, dry="--dry-run" in args)
    m.run(status_only="--status" in args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
