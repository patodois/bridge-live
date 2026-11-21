"""Reports and the comparison with the previous run, in the manner of bridge-verify.

Pass/fail and the discrete protocol counts (`exact`) are compared exactly. Counts that depend on timing (`rates`:
cameras, poses, frames per session) are compared with a tolerance, because a live game never delivers the same number
twice: a drop of more than `rate_drop_percent` is an alert, and each pair's own floors are checks in the run itself.
Wall time and peak RAM are compared with the maximum of the last three full runs plus `regression_percent`, ignoring a
rise under `min_secs_rise` seconds or `min_rss_rise_mb` MB.
"""

import glob
import json
import os
from pathlib import Path

from common import ROOT


def load(path):
    with open(path) as f:
        return json.load(f)


def full_reports():
    """[(path, report)] of the full runs in reports/, oldest first."""
    out = []
    for p in sorted(glob.glob(str(ROOT / "reports" / "*.json"))):
        try:
            r = load(p)
        except Exception:
            continue
        if r.get("full"):
            out.append((p, r))
    return out


def _fmt(v):
    s = json.dumps(v, sort_keys=True)
    return s if len(s) < 90 else s[:87] + "..."


# Rates judged only by an absolute floor in their own pair, never by a drop against the previous run: with two Minecraft windows competing for
# the machine the camera counts vary between runs of identical code (906 to 2,249).
NO_DROP_ALERT = {"mc-mc/": ("host.cameras_sent", "guest.cameras_received", "host.cameras_per_s", "guest.cameras_per_s")}


def _no_drop_alert(pid, key):
    return any(pid.startswith(prefix) and key in keys for prefix, keys in NO_DROP_ALERT.items())


def compare(old, new, cfg, history):
    """The alerts `new` raises against `old`; `history` is the list of earlier full reports (up to the last three) for time and RAM."""
    lim = cfg["limits"]
    alerts = []
    ov, nv = old.get("versions", {}), new.get("versions", {})
    changed = []
    for name, nrev in nv.get("repos", {}).items():
        orev = ov.get("repos", {}).get(name)
        if orev and (orev.get("rev") != nrev.get("rev") or orev.get("dirty") != nrev.get("dirty")):
            changed.append("%s %s -> %s" % (name, orev.get("rev"), nrev.get("rev")))
    for k in ("hytale_plugin_jar_sha256", "bridge_guest_binary", "live_peer_binary", "guest_compose_binary", "hytale_server_jar"):
        if ov.get(k) != nv.get(k):
            changed.append("%s changed" % k)
    new["inputs_changed"] = changed
    tag = " [inputs moved: %s]" % ", ".join(changed) if changed else ""
    for pid, np_ in new["pairs"].items():
        op = old["pairs"].get(pid)
        if np_["status"] in ("skipped", "not_available"):
            continue
        if np_["status"] != "pass":
            alerts.append("FAIL %s: %s%s" % (pid, np_["status"] + (" - " + np_["reason"] if np_.get("reason") else ""), tag))
        if not op or op["status"] in ("skipped", "not_available"):
            continue
        if op["status"] == "pass" and np_["status"] != "pass":
            alerts.append("REGRESSION %s: passed before, now %s" % (pid, np_["status"]))
        if np_["status"] != "pass":
            continue
        for k in sorted(set(op["exact"]) | set(np_["exact"])):
            a, b = op["exact"].get(k, "<absent>"), np_["exact"].get(k, "<absent>")
            if a != b:
                alerts.append("COUNT %s: %s was %s, now %s%s" % (pid, k, _fmt(a), _fmt(b), tag))
        for k in sorted(set(op["rates"]) & set(np_["rates"])):
            a, b = op["rates"][k], np_["rates"][k]
            # Small counts (a few collision snapshots) and "lower is better" or noise numbers are not judged by a percentage.
            if a >= 20 and b < a * (1 - lim["rate_drop_percent"] / 100.0) and not _no_drop_alert(pid, k) and not k.endswith(("frames_missed", "max_frame_age_ms", "latency_mean_ms", "latency_p95_ms", "error_lines", "skipped", "max_moved_m", "foreign_after_bye")):
                alerts.append("RATE %s: %s fell from %s to %s (more than %d%%)%s" % (pid, k, a, b, lim["rate_drop_percent"], tag))
        hist = [h["pairs"][pid] for h in history if pid in h["pairs"] and h["pairs"][pid]["status"] == "pass"][-3:] or [op]
        for key, floor, unit in (("wall_s", lim["min_secs_rise"], "s"), ("peak_rss_mb", lim["min_rss_rise_mb"], "MB")):
            ref = max(h.get(key, 0) for h in hist)
            cur = np_.get(key, 0)
            if ref and cur > ref * (1 + lim["regression_percent"] / 100.0) and cur - ref >= floor:
                alerts.append("%s %s: %.1f %s is more than %d%% over the maximum of the last %d run(s) (%.1f %s)%s" % (
                    "TIME" if key == "wall_s" else "RAM", pid, cur, unit, lim["regression_percent"], len(hist), ref, unit, tag))
    for pid, op in old["pairs"].items():
        if pid not in new["pairs"] and new.get("full") and op["status"] == "pass":
            alerts.append("MISSING %s: in the previous run, not in this one" % pid)
    return alerts


def summary_text(rep, alerts=None, base=None):
    L = []
    s = rep["summary"]
    L.append("bridge-live %s (%s) on %s" % (rep["stamp"], "full run" if rep["full"] else "PARTIAL run - not a baseline", rep["host"].get("machine", "?")))
    if rep.get("not_a_baseline_because"):
        L.append("not a baseline: " + rep["not_a_baseline_because"])
    L.append("pairs: %d pass, %d fail, %d skipped, %d aborted, %d error, %d not available" % (
        s.get("pass", 0), s.get("fail", 0), s.get("skipped", 0), s.get("aborted", 0), s.get("error", 0), s.get("not_available", 0)))
    if base:
        L.append("compared with: %s" % base)
    else:
        L.append("compared with: nothing (this is the baseline)" if rep["full"] else "compared with: not compared (partial run)")
    if rep.get("inputs_changed"):
        L.append("INPUTS MOVED since the previous run: " + "; ".join(rep["inputs_changed"]))
    if rep.get("skip_reason"):
        L.append("SKIPPED: " + rep["skip_reason"])
    L.append("")
    if alerts:
        L.append("ALERTS (%d)" % len(alerts))
        L += ["  " + a for a in alerts]
    else:
        L.append("ALERTS: none")
    L.append("")
    L.append("%-52s %-8s %7s %8s %8s  %s" % ("pair", "status", "wall s", "RAM MB", "game MB", "failed checks / notes"))
    for pid, p in rep["pairs"].items():
        bad = [c["name"] for c in p.get("checks", []) if not c["ok"] and c["gate"]]
        note = "; ".join(bad[:3]) + (" (+%d)" % (len(bad) - 3) if len(bad) > 3 else "")
        if not bad and p.get("reason"):
            note = p["reason"]
        L.append("%-52s %-8s %7.1f %8.0f %8.0f  %s" % (pid, p["status"].upper(), p.get("wall_s", 0), p.get("peak_rss_mb", 0), p.get("game_rss_mb", 0), note[:140]))
    jf = [(pid, k, v) for pid, p in rep["pairs"].items() for k, v in sorted(p.get("jfr", {}).items())]
    if jf:
        L.append("")
        L.append("JFR (full summaries in logs/%s/<pair>/<client>.jfr-summary.json):" % rep["stamp"])
        for pid, k, v in jf:
            if not v.get("recorded"):
                L.append("  %s %s: none - %s" % (pid, k, v.get("reason", "")[:140]))
                continue
            h = v["headline"]
            L.append("  %s %s: %.1f cpu-s over %.0f s, top %s; %.0f MB allocated; %d GC pauses, %.0f ms total, longest %.1f ms; mod hot spot %s" % (
                pid, k, h["cpu_seconds_total"], h["span_s"], ", ".join("%s %.1f" % t for t in h["cpu_top_threads"][:2]), h["allocated_mb"],
                h["gc_collections"], h["gc_pause_total_ms"], h["gc_pause_longest_ms"],
                "%s (%d samples)" % tuple(h["mod_top_innermost"][0]) if h["mod_top_innermost"] else "none sampled"))
    L.append("")
    if rep.get("profiles") is not None:
        pr = rep["profiles"]
        L.append("real launcher profiles (Minecraft, Prism, Hytale) during the run: %s" % (
            "untouched" if not (pr["added"] or pr["removed"] or pr["changed"]) else "CHANGED %d/%d/%d (added/removed/changed)" % (pr["added"], pr["removed"], pr["changed"])))
    else:
        L.append("real launcher profiles: not listed (./live --check-profiles lists them before and after a run)")
    L.append("")
    shots = [(pid, sh) for pid, p in rep["pairs"].items() for sh in p.get("shots", []) if sh.get("path")]
    L.append("screenshots: %d (local only, gitignored)" % len(shots))
    nc = [(pid, c) for pid, p in rep["pairs"].items() for c in p.get("checks", []) if not c["gate"] and not c["ok"]]
    if nc:
        L.append("non-gating checks that did not pass:")
        L += ["  %s: %s - %s" % (pid, c["name"], c["detail"][:160]) for pid, c in nc]
    lo = rep.get("leftovers", {})
    L.append("leftover processes started by this run: %s" % (lo.get("mine") or "none"))
    if lo.get("others"):
        L.append("other java/gradle/hytale processes present at the end that this run did not start: %s" % lo["others"])
    return "\n".join(L) + "\n"
