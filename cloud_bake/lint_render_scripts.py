#!/usr/bin/env python
"""lint_render_scripts.py — static checks for cloud render/dispatch scripts,
targeting the failure classes we have actually hit (S96/S101c/S104):

  FLAG      completion-flag name mismatches: a poll loop testing a /root/<flag>
            that nothing (script or referenced box driver .py) ever writes.
  QUOTE     shell-quoting in remote commands: unquoted heredocs feeding
            `ssh bash -s` (everything expands LOCALLY), $vars inside
            double-quoted ssh command strings, tmux-in-heredoc double expansion.
  ORPHAN    orchestrator lifecycle: infinite poll loops without a time cap,
            failure branches that retry forever without a bounded counter,
            local background jobs without a wait.
  NET       safety-net presence: scripts that create servers must have a final
            unconditional delete-by-pattern sweep, a hard time cap, and set -u;
            suggest box_guard arm/set-ttl at dispatch.

Usage:
  py cloud_bake/lint_render_scripts.py <script.sh> [more.sh ...]
  py cloud_bake/lint_render_scripts.py --all     # islands/*.sh + cloud_bake/*.sh

Exit 1 if any HIGH finding, else 0. Heuristic — findings are review prompts,
not verdicts; suppress a line with a trailing `# lint-ok` comment.
"""
from __future__ import annotations
import re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

HIGH, WARN, INFO = "HIGH", "WARN", "INFO"


class Finding:
    def __init__(self, path, line, sev, cat, msg):
        self.path, self.line, self.sev, self.cat, self.msg = path, line, sev, cat, msg

    def __str__(self):
        return f"  {self.sev:4} {self.cat:6} {self.path.name}:{self.line}  {self.msg}"


def _referenced_drivers(text: str) -> list[Path]:
    """Box-side drivers this script launches (islands/_box_*.py, *.sh)."""
    out = []
    for m in re.finditer(r"(?:islands|tools|cloud_bake)/[\w.]+\.(?:py|sh)", text):
        p = ROOT / m.group(0)
        if p.exists():
            out.append(p)
    return list(dict.fromkeys(out))


def _flags_written(text: str) -> set[str]:
    w = set()
    # touch /root/x ;  > /root/x ;  >> /root/x ;  tee /root/x
    for m in re.finditer(r"(?:touch|tee)\s+(/root/[\w.]+)", text):
        w.add(m.group(1))
    for m in re.finditer(r">>?\s*(/root/[\w.]+)", text):
        w.add(m.group(1))
    # python: Path("/root/x").write_text / open("/root/x","w")
    for m in re.finditer(r"""Path\(\s*["'](/root/[\w.]+)["']\s*\)\.write_""", text):
        w.add(m.group(1))
    for m in re.finditer(r"""open\(\s*["'](/root/[\w.]+)["']\s*,\s*["'][wa]""", text):
        w.add(m.group(1))
    return w


def _flags_tested(text: str):
    """[(flag, lineno)] for test -f / [ -f ] / cat probes of /root files."""
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        for m in re.finditer(r"(?:test\s+-f|\[\s+-f)\s+(/root/[\w.]+)", line):
            out.append((m.group(1), i))
    return out


# /root files that are logs/data, not completion flags — probing them is fine
_NON_FLAG = re.compile(r"\.(log|txt|tgz|json|mca)$|/(run\.log|job\.log|dems)")


def check_flags(path: Path, text: str) -> list[Finding]:
    written = _flags_written(text)
    for drv in _referenced_drivers(text):
        written |= _flags_written(drv.read_text(encoding="utf-8", errors="replace"))
    fs = []
    for flag, line in _flags_tested(text):
        if _NON_FLAG.search(flag):
            continue
        if flag not in written:
            fs.append(Finding(path, line, HIGH, "FLAG",
                f"polls {flag} but neither this script nor its referenced drivers write it "
                f"(written flags found: {sorted(written) or 'none'}) — S101c-class mismatch"))
    return fs


def check_quoting(path: Path, lines: list[str]) -> list[Finding]:
    fs = []
    heredoc = None  # (delim, quoted, start_line, feeds_ssh)
    for i, line in enumerate(lines, 1):
        if "# lint-ok" in line:
            continue
        if heredoc:
            delim, quoted, start, feeds_ssh = heredoc
            if line.strip() == delim:
                heredoc = None
                continue
            if not quoted and feeds_ssh:
                # unquoted heredoc into ssh: $... expands on the LOCAL machine
                if re.search(r"\$\((?!\()", line) or re.search(r"\$\{?[A-Za-z_]", line):
                    if not re.search(r"\\\$", line):  # escaped = author knew
                        fs.append(Finding(path, i, WARN, "QUOTE",
                            f"inside unquoted heredoc (<<{delim} at line {start}) feeding ssh: "
                            f"`{line.strip()[:70]}` expands LOCALLY — confirm that's intended "
                            f"(remote-side vars need \\$ or a quoted delimiter)"))
            if feeds_ssh and "tmux new-session" in line and '"' in line:
                fs.append(Finding(path, i, WARN, "QUOTE",
                    "tmux command string inside a heredoc — triple expansion (local shell, "
                    "remote shell, tmux) — verify quoting of every $ in the tmux payload"))
            continue
        m = re.search(r"<<-?\s*(['\"]?)(\w+)\1", line)
        if m:
            heredoc = (m.group(2), bool(m.group(1)), i, bool(re.search(r"\bssh\b", line)))
            continue
        # double-quoted ssh command containing command substitution
        sm = re.search(r'ssh\s.*?"([^"]*)"\s*$', line)
        if sm and re.search(r"\$\(", sm.group(1)) and not re.search(r"\\\$\(", sm.group(1)):
            fs.append(Finding(path, i, INFO, "QUOTE",
                f"$(...) inside double-quoted ssh command runs LOCALLY: "
                f"`{sm.group(1)[:70]}` — confirm intent"))
    return fs


def check_orchestrator(path: Path, lines: list[str], text: str) -> list[Finding]:
    fs = []
    has_cap = bool(re.search(
        r"date \+%s.*-gt|\bSECONDS\b.*-gt|-gt\s+\d{4,}"
        r"|-gt\s+\"?\$\{?(DEADLINE|HARD_?CAP|TIME_?CAP|BUDGET)", text))
    for i, line in enumerate(lines, 1):
        if "# lint-ok" in line:
            continue
        if re.match(r"\s*while\s+(:|true)\s*;?\s*do?", line):
            if not has_cap:
                fs.append(Finding(path, i, HIGH, "ORPHAN",
                    "infinite poll loop with NO elapsed-time cap anywhere in the script — "
                    "a false-negative probe idles boxes forever (S104)"))
        if re.search(r"retry next pass|retry", line, re.I) and "RETRY" not in text.upper().replace("RETRY NEXT", ""):
            pass  # counter detection below
    # unbounded retry: a 'retry' failure branch with no retry counter variable anywhere
    if re.search(r"retry next pass", text, re.I) and not re.search(r"RETRIES?\[|retry_count|RETRY_MAX|n_retry", text):
        i = next(i for i, l in enumerate(lines, 1) if re.search(r"retry next pass", l, re.I))
        fs.append(Finding(path, i, HIGH, "ORPHAN",
            "collect-failure branch retries with no bounded counter — one persistently "
            "failing scp keeps the box alive until the hard cap (S104 false-negative); "
            "add a per-box retry counter that force-collects the log + deletes after N tries"))
    # local background fan-out without wait
    if re.search(r"^\s*\w+.*&\s*(#.*)?$", text, re.M) and not re.search(r"^\s*wait\b", text, re.M):
        i = next((i for i, l in enumerate(lines, 1) if re.search(r"&\s*(#.*)?$", l) and "nohup" not in l), 0)
        if i:
            fs.append(Finding(path, i, WARN, "ORPHAN",
                "backgrounded local job(s) but no `wait` in script — orphaned orchestrator "
                "risk (box_dispatch_hang was the inverse: a wait that never returns; either "
                "way, account for every child)"))
    return fs


def check_safety_net(path: Path, lines: list[str], text: str) -> list[Finding]:
    fs = []
    creates = re.search(r"-X POST\b.*?/servers", text)
    if not creates:
        return fs
    line_no = next(i for i, l in enumerate(lines, 1) if re.search(r"-X POST\b.*?/servers", l))
    deletes = [i for i, l in enumerate(lines, 1) if re.search(r"-X DELETE\b.*?/servers", l)]
    if not deletes:
        if re.search(r"box_guard\.py.*(selfdestruct|set-ttl|arm)|ttl_min", text):
            fs.append(Finding(path, line_no, WARN, "NET",
                "no in-script delete — cleanup delegated to box_guard/render_monitor "
                "(ttl labels/selfdestruct found). Verify the monitor actually gets "
                "launched after dispatch; the guard sweeper is the only net until then"))
        else:
            fs.append(Finding(path, line_no, HIGH, "NET",
                "creates servers but has NO delete call at all — guaranteed leak"))
    else:
        for di in deletes:
            if re.search(r"\bnohup\b", lines[di - 1]):
                fs.append(Finding(path, di, WARN, "NET",
                    "auto-killer is a LOCAL nohup process — dies with a reboot/sleep of this "
                    "PC while boxes stay up billing; box_guard's scheduled task survives that"))
        # final unconditional sweep by name pattern (delete inside the poll loop
        # doesn't count — it's skipped on early exit)
        tail = "\n".join(lines[int(len(lines) * 0.7):])
        if not re.search(r"startswith|grep .*-b|safety", tail, re.I):
            fs.append(Finding(path, line_no, WARN, "NET",
                "no recognizable end-of-script safety sweep (delete-by-name-pattern after "
                "the poll loop) — early exit / crash of the poll loop leaks boxes"))
    if "box_guard" not in text:
        fs.append(Finding(path, line_no, INFO, "NET",
            "dispatch does not arm box_guard — add: `\"$PY\" cloud_bake/box_guard.py arm` + "
            "`set-ttl --all <expected+30>` right after box creation"))
    if not re.search(r"^\s*set\s+-u", text, re.M):
        fs.append(Finding(path, 1, WARN, "NET", "no `set -u` — typo'd vars expand empty"))
    if re.search(r"^\s*hcloud\s", text, re.M):
        fs.append(Finding(path, 1, WARN, "NET",
            "uses hcloud CLI — not installed here; use curl REST (project convention)"))
    return fs


def lint(path: Path) -> list[Finding]:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    fs = []
    fs += check_flags(path, text)
    fs += check_quoting(path, lines)
    fs += check_orchestrator(path, lines, text)
    fs += check_safety_net(path, lines, text)
    return fs


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    if args == ["--all"]:
        paths = sorted((ROOT / "islands").glob("*.sh")) + sorted((ROOT / "cloud_bake").glob("*.sh"))
    else:
        paths = [Path(a) for a in args]
    total = {HIGH: 0, WARN: 0, INFO: 0}
    for p in paths:
        if not p.exists():
            print(f"!! missing: {p}")
            continue
        fs = lint(p)
        if fs:
            print(f"{p}:")
            for f in sorted(fs, key=lambda f: (f.sev != HIGH, f.sev != WARN, f.line)):
                print(f)
                total[f.sev] += 1
    print(f"\n{total[HIGH]} HIGH, {total[WARN]} WARN, {total[INFO]} INFO across {len(paths)} script(s)")
    return 1 if total[HIGH] else 0


if __name__ == "__main__":
    sys.exit(main())
