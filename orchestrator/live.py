#!/usr/bin/env python3
"""bridge-live: the live interop matrix of the link with the real games (test layer 4). See README.md.

  ./live                        every pair, one at a time; exit 0 = every pair ran and passed and nothing to alert, 1 = failures or alerts,
                                2 = the tool itself failed, 3 = nothing failed but not every pair ran (skipped on a locked screen,
                                not available here) or no pair passed
  ./live --only mc-26.3-fabric  only the pairs whose id contains one of the comma-separated texts (a partial run, never a baseline)
  ./live --list                 the pairs
  ./live --token                the token matrix (contract section 6.2, since revision 6; live_peer is built on revision 8) instead of the
                                plain one (a partial run, never a baseline)
  ./live --skip-gui             only the pairs that open no window
  ./live --jfr                  record Java Flight Recorder in the Minecraft clients that support it and write a summary per client
                                (a partial run, never a baseline: profiling changes the numbers)
  ./live --minecraft-assets DIR seed bridge-live's vanilla asset cache (work/cache) from DIR, once, when the cache does not exist yet
                                (also BRIDGE_LIVE_MINECRAFT_ASSETS=DIR); without it no launcher folder is read and the cache starts empty
  ./live --check-profiles       list the real launcher folders (path, size, mtime) before and after the run and alert on a change
                                (off by default: it reads the player's folders)
  ./live doctor                 what is available on this machine, screen state, RAM, disk
  ./live summary [report.json]  print a summary again
  ./live compare OLD NEW        the alerts NEW raises against OLD
  ./live clean                  delete this tool's work/ output, including the asset cache (not the built tools)
"""

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common
import gameenv
import harness
import matrix
import peers as pr
import report
from common import ROOT, expand


def sh(argv, **kw):
    return subprocess.run(argv, capture_output=True, text=True, **kw)


def sha256(path, limit=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()[:16]


def stat_sig(path):
    try:
        st = os.stat(path)
        return {"size": st.st_size, "mtime": int(st.st_mtime)}
    except OSError:
        return None


# --------------------------------------------------------------------------------------------- build steps

def ensure_tools(cfg):
    """The scripted peer (cargo, rewrite-bridge as peer/Cargo.lock pins it) and the screen-state helper (swiftc).

    The peer is built with `--locked` only: a lock that does not fit the manifest is an error, never a reason to resolve other versions."""
    peer = ROOT / "peer"
    r = sh(["cargo", "build", "--release", "--locked"], cwd=str(peer))
    if r.returncode != 0:
        raise RuntimeError("cannot build live_peer:\n" + r.stderr[-1500:])
    helper = ROOT / "work" / "bin" / "screenstate"
    src = ROOT / "tools" / "screenstate.swift"
    if not helper.exists() or helper.stat().st_mtime < src.stat().st_mtime:
        helper.parent.mkdir(parents=True, exist_ok=True)
        r = sh(["swiftc", "-O", str(src), "-o", str(helper)])
        if r.returncode != 0:
            raise RuntimeError("cannot build screenstate:\n" + r.stderr[-1500:])


def availability(cfg, pair):
    """None if the pair can run here, else why not."""
    t = cfg["tools"]
    for need in pair["needs"]:
        if need == "bridge-guest" and not os.path.exists(expand(t["bridge_guest"])):
            return "bridge-guest binary not found at " + expand(t["bridge_guest"])
        if need == "guest_compose" and not os.path.exists(expand(t["guest_compose_install"])):
            return "guest_compose not installed at " + expand(t["guest_compose_install"])
        if need == "hytale":
            hp = pr.hytale_paths(cfg)
            for k in ("java", "jar", "assets", "plugin"):
                if not os.path.exists(hp[k]):
                    return "Hytale %s not found at %s" % (k, hp[k])
    if pair["id"].startswith("mc-") and not pair["id"].startswith("mc-mc"):
        ver, loader = pair["args"]
        d = expand(cfg["mc"][ver]["dir"])
        if not os.path.isfile(os.path.join(d, "gradlew")):
            return "no checkout with gradlew at " + d
    if pair["id"].startswith("mc-mc"):
        for ver, _l in pair["args"]:
            if not os.path.isfile(os.path.join(expand(cfg["mc"][ver]["dir"]), "gradlew")):
                return "no checkout for Minecraft " + ver
    if not os.path.isfile(os.path.join(expand(cfg["java_home"]), "bin", "java")):
        return "no JDK at " + cfg["java_home"]
    if pair["id"].startswith("mc-") and not pair["id"].startswith("mc-mc"):
        j = cfg["mc"][pair["args"][0]].get("java_home")
        if j and not os.path.isfile(os.path.join(expand(j), "bin", "java")):
            return "no JDK at " + j
    return None


def java_version(java_home):
    """The first line `java -version` prints (to stderr), or "" when that JDK cannot be run."""
    try:
        r = sh([os.path.join(expand(java_home), "bin", "java"), "-version"], timeout=20)
    except Exception:
        return ""
    lines = (r.stderr or r.stdout).strip().splitlines()
    return lines[0] if lines else ""


def host_info(cfg):
    def s(argv):
        try:
            return sh(argv, timeout=10).stdout.strip()
        except Exception:
            return ""
    return {
        "machine": s(["sysctl", "-n", "machdep.cpu.brand_string"]) or platform.machine(),
        "ram_gb": round(int(s(["sysctl", "-n", "hw.memsize"]) or 0) / 2**30, 1),
        "os": "macOS " + s(["sw_vers", "-productVersion"]),
        "python": platform.python_version(),
        "rustc": s(["rustc", "--version"]),
        "jdk": java_version(cfg["java_home"]),
        "free_ram_percent_start": common.mem_free_percent(),
        "free_disk_gb_start": round(common.disk_free_gb(), 1),
    }


def locked_package(lock_path, name):
    """{'version', 'source'} of package `name` in a Cargo.lock, or None (read as text: Python 3.9 has no TOML reader)."""
    try:
        with open(lock_path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    for block in text.split("[[package]]"):
        fields = {}
        for line in block.splitlines():
            k, sep, val = line.partition("=")
            if sep:
                fields.setdefault(k.strip(), val.strip().strip('"'))
        if fields.get("name") == name:
            return {"version": fields.get("version"), "source": fields.get("source")}
    return None


def versions(cfg):
    v = {"repos": {os.path.basename(expand(r)): common.git_rev(r) for r in cfg["repos"]}}
    t = cfg["tools"]
    # What live_peer is built from is what peer/Cargo.lock pins (the build is --locked); guest_compose is installed by hand (README),
    # so only the pin config.json declares for it can be reported, next to the binary's size and mtime.
    v["crates"] = {"rewrite-bridge (live_peer, peer/Cargo.lock)": locked_package(ROOT / "peer" / "Cargo.lock", "rewrite-bridge"),
                   "rewrite-bridge-bevy (guest_compose, declared in config.json)": t.get("guest_compose_pin")}
    bg = expand(t["bridge_guest"])
    v["bridge_guest_binary"] = stat_sig(bg)
    gc = expand(t["guest_compose_install"])
    v["guest_compose_binary"] = stat_sig(gc)
    v["live_peer_binary"] = {"sha256": sha256(str(pr.PEER_BIN))} if pr.PEER_BIN.exists() else None
    hp = pr.hytale_paths(cfg)
    v["hytale_plugin_jar_sha256"] = sha256(hp["plugin"]) if os.path.exists(hp["plugin"]) else None
    v["hytale_server_jar"] = stat_sig(hp["jar"])
    return v


WATCHED = ("java", "guest_compose", "bridge-guest", "live_peer")


def watched_pids():
    """Processes that could be strays of this tool: by the executable's name (ps `comm`), not by words in a command line."""
    out = {}
    for line in sh(["ps", "-axo", "pid=,comm="]).stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and os.path.basename(parts[1]) in WATCHED:
            out[int(parts[0])] = parts[1][-100:]
    return out


# --------------------------------------------------------------------------------------------- one pair

def run_pair(cfg, pair, logroot, shotroot, roots_seen):
    pid = pair["id"]
    safe = pid.replace("/", "__").replace("+", "_")
    ctx = harness.Ctx(cfg, pid, os.path.join(logroot, safe), os.path.join(shotroot, safe))
    rec = {"id": pid, "status": "pass", "reason": "", "gui": pair["gui"]}
    t0 = time.time()
    rec["host_load"] = {"load1_start": round(os.getloadavg()[0], 2), "free_ram_percent_start": common.mem_free_percent()}
    why = availability(cfg, pair)
    if why:
        rec.update(status="not_available", reason=why)
        return rec, ctx
    prob = common.resource_problem(cfg)
    if prob:
        rec.update(status="aborted", reason=prob)
        return rec, ctx
    if pair["gui"]:
        why = common.screen_unavailable()
        if why:
            rec.update(status="skipped", reason="%s: this pair opens a game window, so it was not started" % why)
            return rec, ctx
    try:
        pair["fn"](ctx, cfg, *pair["args"])
    except harness.Abort as e:
        rec.update(status="aborted", reason=str(e))
    except Exception as e:
        rec.update(status="error", reason="harness error: %s" % e)
        rec["traceback"] = traceback.format_exc()[-1800:]
    finally:
        killed, left = ctx.finish()  # also when interrupted: nothing this pair started is left behind
        roots_seen.update(p.pid for p in ctx.procs)
    if killed:
        ctx.note("processes that had not ended on their own and were killed by PID: %s" % killed)
    ctx.check("no process of this pair is left", not left, "left: %s" % left)
    rec["wall_s"] = round(time.time() - t0, 1)
    rec["host_load"]["load1_end"] = round(os.getloadavg()[0], 2)
    rec["peak_rss_mb"] = round(ctx.peak_rss_kb / 1024.0, 0)
    rec["game_rss_mb"] = round(ctx.peak_game_kb / 1024.0, 0)
    gate_bad = [c for c in ctx.checks if c["gate"] and not c["ok"]]
    if rec["status"] == "pass" and gate_bad:
        rec["status"] = "fail"
        rec["reason"] = "%d check(s) failed" % len(gate_bad)
    if rec["status"] in ("fail", "error", "aborted") and pair["gui"]:
        # A pair that ran and failed stays failed. Only a pair that was not started because of the screen is `skipped` (above);
        # the screen state at the end is recorded so a reader can judge a failure that came with a lock.
        why = common.screen_unavailable()
        if why:
            ctx.note("when this pair ended: %s" % why)
    return rec, ctx


def fill(rec, ctx, logroot):
    if ctx.jfr:
        rec["jfr"] = ctx.jfr
    if ctx.sandbox is not None:
        rec["sandbox"] = {"on": ctx.sandbox[0], "detail": ctx.sandbox[1]}
    rec["checks"] = ctx.checks
    rec["exact"] = ctx.exact
    rec["rates"] = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in ctx.rates.items()}
    rec["shots"] = ctx.shots
    rec["evidence"] = ctx.evidence
    rec["notes"] = ctx.notes
    rec["markers"] = ctx.markers[-12:]
    rec["logs"] = os.path.relpath(str(ctx.logdir), str(ROOT))
    rec.setdefault("wall_s", 0)
    rec.setdefault("peak_rss_mb", 0)
    rec.setdefault("game_rss_mb", 0)
    return rec


# --------------------------------------------------------------------------------------------- run

def prune_logs(cfg):
    """Keep the logs of the last three full runs (they are what the reports point at) and of the newest partial runs."""
    base = ROOT / "logs"
    if not base.exists():
        return
    protected = {os.path.basename(path)[:-5] for path, _r in report.full_reports()[-3:]}
    runs = sorted(d for d in os.listdir(base) if (base / d).is_dir() and d[:2] == "20" and d not in protected)
    keep = max(0, int(cfg["limits"]["keep_log_runs"]))
    for d in runs[:len(runs) - keep]:   # (runs[:-0] would be empty: keep_log_runs 0 must delete them all)
        try:
            common.safe_delete(str(base / d), [str(base)])
        except Exception as e:
            print("could not prune logs/%s: %s" % (d, e))


def _terminate(signum, frame):
    raise KeyboardInterrupt()


def cmd_run(args):
    import signal
    signal.signal(signal.SIGTERM, _terminate)
    cfg = common.load_config()
    os.makedirs(ROOT / "work", exist_ok=True)
    lock = ROOT / "work" / "run.lock"
    held = acquire_run_lock(lock)
    if isinstance(held, int):
        print("another run is in progress (pid %d)" % held)
        return 2
    try:
        return _run(args, cfg)
    finally:
        # Closing the file releases the lock. The file itself stays: removing it would let a run that
        # opened it just before start over a new, unlocked file next to ours.
        held.close()


def acquire_run_lock(path):
    """Takes the system-wide lock of a matrix run; it lasts as long as the returned file stays open
    (or the process lives, so a run that dies cannot leave a stale lock).

    Returns the open file on success, or the pid written by the holder (0 when unreadable) when another
    run holds it. Scratch folders may only be cleared after this succeeded.
    """
    import fcntl
    f = open(path, "a+")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            f.seek(0)
            other = int(f.read().strip() or 0)
        except (ValueError, OSError):
            other = 0
        f.close()
        return other
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    return f


def _run(args, cfg):
    cfg["run"] = {"jfr": bool(args.jfr), "minecraft_assets": getattr(args, "minecraft_assets", None)}
    seed = gameenv.assets_seed(cfg)
    if seed and not os.path.isdir(seed):
        print("--minecraft-assets / %s: not a folder: %s" % (gameenv.ASSETS_SEED_ENV, seed))
        return 2
    ensure_tools(cfg)
    gameenv.clear_scratch_root()   # scratch folders of a run that died
    check_profiles = bool(getattr(args, "check_profiles", False))
    profiles_before = gameenv.profile_manifest(cfg) if check_profiles else None
    # Keep the Mac and its display awake while the matrix runs, so an idle display does not lock halfway through
    # (a power assertion that ends with this process: no setting is changed). A screen that is already locked stays locked
    # and its pairs are skipped.
    try:
        subprocess.Popen(["caffeinate", "-d", "-i", "-w", str(os.getpid())], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass
    all_pairs = matrix.all_pairs(cfg)
    pairs = all_pairs
    if args.token:
        pairs = matrix.token_pairs(cfg)    # the token matrix instead of the plain one (a partial run, never a baseline)
    if args.only:
        keys = [k.strip() for k in args.only.split(",") if k.strip()]
        pairs = [p for p in pairs if any(k in p["id"] for k in keys)]
    if args.skip_gui:
        pairs = [p for p in pairs if not p["gui"]]
    full = len(pairs) == len(all_pairs) and not args.jfr and not args.token
    if not pairs:
        print("no pair matches")
        return 2
    stamp = common.utc_stamp()
    logroot = str(ROOT / "logs" / stamp)
    shotroot = str(ROOT / "logs" / stamp / "shots")
    os.makedirs(logroot, exist_ok=True)
    rep = {"schema": 1, "stamp": stamp, "full": full, "started": common.now_iso(), "args": sys.argv[1:], "host": host_info(cfg), "versions": versions(cfg), "pairs": {}}
    print("bridge-live %s: %d pair(s)%s" % (stamp, len(pairs), "" if full else " (partial run)"))
    before = watched_pids()
    roots_seen = set()
    skipped_note = None
    part_path = os.path.join(logroot, "report.partial.json")
    try:
        for i, pair in enumerate(pairs, 1):
            print("[%d/%d] %s ..." % (i, len(pairs), pair["id"]), flush=True)
            rec, ctx = run_pair(cfg, pair, logroot, shotroot, roots_seen)
            fill(rec, ctx, logroot)
            rep["pairs"][pair["id"]] = rec
            extra = ""
            if rec["status"] != "pass":
                bad = [c["name"] for c in rec["checks"] if c["gate"] and not c["ok"]]
                extra = " - " + (rec["reason"] if not bad else "; ".join(bad[:3]))
            print("      %s in %.0f s, peak %.0f MB (game %.0f MB)%s" % (rec["status"].upper(), rec["wall_s"], rec["peak_rss_mb"], rec["game_rss_mb"], extra), flush=True)
            with open(part_path, "w") as f:
                json.dump(common.scrub(rep), f, indent=1, sort_keys=True)
            time.sleep(2.0)
    except KeyboardInterrupt:
        # An interrupted run is when a half-finished launch is most likely: compare the real profiles anyway (when that was asked for).
        if profiles_before is not None:
            _rec, alert = profiles_report(profiles_before, gameenv.profile_manifest(cfg))
            print(alert or "interrupted: real launcher profiles (Minecraft, Prism, Hytale) untouched so far")
        raise
    rep["finished"] = common.now_iso()
    # A run in which some pair could not run (a locked screen, a missing tool, a resource limit) has holes: it is reported,
    # but it is not a baseline for the next run.
    holes = [p["id"] for p in rep["pairs"].values() if p["status"] in ("skipped", "not_available", "aborted")]
    if args.jfr:
        rep["not_a_baseline_because"] = "Java Flight Recorder was on (profiling changes the timings and the RAM)"
    if full and holes:
        full = False
        rep["full"] = False
        rep["not_a_baseline_because"] = "%d pair(s) did not run: %s" % (len(holes), ", ".join(holes[:4]) + (" ..." if len(holes) > 4 else ""))
    counts = {}
    for p in rep["pairs"].values():
        counts[p["status"]] = counts.get(p["status"], 0) + 1
    rep["summary"] = counts
    skipped = [p for p in rep["pairs"].values() if p["status"] == "skipped"]
    if skipped:
        rep["skip_reason"] = "%d pair(s) skipped: %s" % (len(skipped), skipped[0]["reason"])
    # leftovers
    time.sleep(1.5)
    now = watched_pids()
    table = common.ps_table()
    mine = sorted(r[0] for r in table if r[2] in roots_seen)
    others = {pid: cmd for pid, cmd in now.items() if pid not in before and pid not in mine}
    rep["leftovers"] = {"mine": mine, "others": others}
    # compare
    hist = report.full_reports()
    alerts = []
    base = None
    if full and hist:
        base_path, old = hist[-1]
        base = os.path.basename(base_path)
        alerts = report.compare(old, rep, cfg, [h for _, h in hist][-3:])
    rep["profiles"] = None
    if profiles_before is not None:
        rep["profiles"], alert = profiles_report(profiles_before, gameenv.profile_manifest(cfg))
        if alert:
            alerts.append(alert)
    if mine:
        alerts.append("LEFTOVER processes started by this run are still alive: %s" % mine)
    rep["alerts"] = alerts
    text = report.summary_text(rep, alerts, base)
    if full:
        out_json = ROOT / "reports" / (stamp + ".json")
        out_txt = ROOT / "reports" / (stamp + ".txt")
    else:
        out_json = ROOT / "logs" / stamp / "report.json"
        out_txt = ROOT / "logs" / stamp / "report.txt"
    text = write_report(rep, text, out_json, out_txt)
    try:
        os.unlink(part_path)
    except OSError:
        pass
    prune_logs(cfg)
    print()
    print(text)
    print("report: %s" % os.path.relpath(str(out_json), str(ROOT)))
    code = exit_code(counts, alerts)
    if code == 3:
        print("not every pair ran and passed: %s" % ", ".join("%d %s" % (counts[k], k) for k in sorted(counts) if k != "pass") if set(counts) - {"pass"}
              else "no pair passed")
    return code


def exit_code(counts, alerts):
    """The run's exit status from the pairs' statuses and the alerts: 1 for a failure (fail, error, aborted) or an alert; 3 when nothing
    failed but not every pair ran (skipped, not_available) or no pair passed at all; 0 only when every pair passed."""
    if counts.get("fail", 0) + counts.get("error", 0) + counts.get("aborted", 0) or alerts:
        return 1
    if counts.get("skipped", 0) + counts.get("not_available", 0) or not counts.get("pass", 0):
        return 3
    if set(counts) - {"pass"}:
        return 1   # a status this function does not know: never report it as a pass
    return 0


def write_report(rep, text, out_json, out_txt):
    """Write the run's report and its summary with every path relative to this repository and no user's home folder in it
    (common.scrub). Returns the summary as written."""
    os.makedirs(str(Path(out_json).parent), exist_ok=True)
    with open(out_json, "w") as f:
        json.dump(common.scrub(rep), f, indent=1, sort_keys=True)
    text = common.scrub_text(text)
    Path(out_txt).write_text(text)
    return text


def home_relative(path):
    """A path under the account's home folder as `~/...`, so no report carries the home folder's name."""
    home = gameenv.real_home()
    p = str(path)
    if p == home or p.startswith(home + os.sep):
        return "~" + p[len(home):]
    return p


def profiles_report(before, after):
    """(the report's `profiles` record, an alert or None) for two manifests of the real profiles."""
    added, removed, changed = gameenv.manifest_diff(before, after)
    paths = added + removed + changed
    rec = {"added": len(added), "removed": len(removed), "changed": len(changed), "examples": [home_relative(p) for p in paths[:8]]}
    alert = None
    if paths:
        alert = ("REAL PROFILES CHANGED during the run (%d added, %d removed, %d changed; is a real launcher or game open?): %s"
                 % (len(added), len(removed), len(changed), ", ".join(home_relative(p) for p in paths[:4])))
    return rec, alert


# --------------------------------------------------------------------------------------------- other commands

def cmd_list(args):
    cfg = common.load_config()
    for p in (matrix.token_pairs(cfg) if args.token else matrix.all_pairs(cfg)):
        why = availability(cfg, p)
        print("%-52s %s%s" % (p["id"], "gui" if p["gui"] else "   ", "   NOT AVAILABLE: " + why if why else ""))
    return 0


def cmd_doctor(args):
    cfg = common.load_config()
    st = common.screen_state()
    print("screen: locked=%s display_asleep=%s" % (st.get("locked"), st.get("display_asleep")))
    free = common.mem_free_percent()
    print("free RAM %s (limit %d%%), free disk %.1f GB (limit %d GB)" % ("unknown" if free is None else "%d%%" % free, cfg["limits"]["min_free_ram_percent"], common.disk_free_gb(), cfg["limits"]["min_free_disk_gb"]))
    bad = 0
    for p in matrix.all_pairs(cfg):
        why = availability(cfg, p)
        if why:
            bad += 1
            print("not available: %s: %s" % (p["id"], why))
    print("%d pairs, %d not available" % (len(matrix.all_pairs(cfg)), bad))
    print("watched processes now: %s" % (watched_pids() or "none"))
    return 0


def cmd_summary(args):
    cfg = common.load_config()
    path = args.paths[0] if args.paths else (report.full_reports() or [(None, None)])[-1][0]
    if not path:
        print("no report")
        return 2
    rep = report.load(path)
    print(report.summary_text(rep, rep.get("alerts", []), None))
    return 0


def cmd_compare(args):
    cfg = common.load_config()
    old, new = report.load(args.paths[0]), report.load(args.paths[1])
    alerts = report.compare(old, new, cfg, [old])
    print("\n".join(alerts) if alerts else "no alerts")
    return 1 if alerts else 0


def cmd_clean(args):
    cfg = common.load_config()
    base = ROOT / "work"
    os.makedirs(base, exist_ok=True)
    # The folders belong to the run that holds the lock: never delete them from under it
    held = acquire_run_lock(base / "run.lock")
    if isinstance(held, int):
        print("a run is in progress (pid %d); not cleaning" % held)
        return 2
    try:
        for name in ("probe", "hytale-server", "target-bevy", "scratch", "cache"):
            p = base / name
            if p.exists():
                common.safe_delete(str(p), [str(base)])
                print("deleted", p)
    finally:
        held.close()
    return 0


def main():
    ap = argparse.ArgumentParser(description="bridge-live: live interop matrix")
    ap.add_argument("cmd", nargs="?", default="run", choices=["run", "doctor", "summary", "compare", "clean"])
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--only")
    ap.add_argument("--skip-gui", action="store_true")
    ap.add_argument("--jfr", action="store_true", help="record JFR in the Minecraft clients that support it and write tools/JfrSummary.java's summary")
    ap.add_argument("--token", action="store_true", help="the token matrix (contract section 6.2, since revision 6) instead of the plain one: the Minecraft pairs with the scripted Rust peer and "
                    "Minecraft with Minecraft, host and guest sharing an endpoint file, plus the wrong-token pairs that expect `bye unauthorized` (a partial run, never a baseline)")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--minecraft-assets", metavar="DIR", help="seed the vanilla asset cache (work/cache/minecraft-assets) from DIR, read once when the cache "
                    "does not exist yet; without it (or %s) no launcher folder is read and the cache starts empty" % gameenv.ASSETS_SEED_ENV)
    ap.add_argument("--check-profiles", action="store_true", help="list the real launcher folders (path, size, mtime) before and after the run "
                    "and alert on any change (off by default: it reads the player's folders)")
    args = ap.parse_args()
    try:
        if args.list:
            return cmd_list(args)
        return {"run": cmd_run, "doctor": cmd_doctor, "summary": cmd_summary, "compare": cmd_compare, "clean": cmd_clean}[args.cmd](args)
    except KeyboardInterrupt:
        print("interrupted")
        return 2
    except Exception:
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
