"""Java Flight Recorder for the Minecraft dev clients (`./live --jfr`): switch it on in the game JVM, summarise the recording.

Only the game's own JVM records (the JVM arguments travel in the Gradle init script of gameenv.py, so Gradle's JVM is
not recorded). The raw `.jfr` is private (it holds the JVM's command line, environment variables and system properties,
and with them user paths): it lives in the launch's scratch folder, is read once by `tools/JfrSummary.java` (JDK only,
exports none of that) and removed with the scratch folder. The summary goes next to the pair's logs:
`logs/<stamp>/<pair>/<client>.jfr-summary.json`.

Which builds can record is a fact about their JVM, written in config.json (`mc.<version>.jfr`): the modern builds run
on Java 25 (26.3) and Java 17 (1.20.1), where JFR is built in and `jdk.ThreadCPULoad#period` can be set on the command
line. The 1.12.2 and 1.7.10 builds run on Java 8, whose JFR is a backport with another option syntax and no per-event
settings on the command line: they are skipped with the reason in the report, never failed.
"""

import json
import os
import subprocess

from common import ROOT, expand, safe_delete

SUMMARISER = ROOT / "tools" / "JfrSummary.java"

DEFAULTS = {
    "packages": ["dev.patodois.minecraftlink", "dev.patodois.rewritelink"],
    "settings": "profile",
    "period": "1s",
    "top": 15,
    "timeout_s": 180,
}


def options(cfg):
    o = dict(DEFAULTS)
    o.update(cfg.get("jfr", {}))
    return o


def plan(cfg, version, requested):
    """{'active': bool, 'reason': str}: whether this Minecraft version records JFR in this run, and why not."""
    if not requested:
        return {"active": False, "reason": "not requested (./live --jfr)"}
    spec = (cfg["mc"][version].get("jfr") or {})
    if not spec.get("supported"):
        return {"active": False, "reason": spec.get("reason") or "no JFR support is recorded for Minecraft %s in config.json" % version}
    return {"active": True, "reason": ""}


def recording_path(scratch, label):
    path = os.path.join(str(scratch.jfr), "%s.jfr" % label)
    if "," in path or " " in path:
        raise ValueError("a JFR file name with a comma or a space cannot be passed to -XX:StartFlightRecording: %r" % path)
    return path


def jvm_args(cfg, path):
    """Arguments for the game JVM. dumponexit writes the file when the game quits; the two one-second periods give the
    per-thread CPU and allocation counters enough readings for a run of a minute (the defaults are 10 s and once per chunk)."""
    o = options(cfg)
    spec = ",".join([
        "filename=" + path,
        "settings=" + o["settings"],
        "dumponexit=true",
        "jdk.ThreadCPULoad#period=" + o["period"],
        "jdk.ThreadAllocationStatistics#period=" + o["period"],
    ])
    return ["-XX:StartFlightRecording:" + spec]


def summary_argv(cfg, recording):
    o = options(cfg)
    java = os.path.join(expand(cfg["java_home"]), "bin", "java")
    argv = [java, "-Xmx1g", str(SUMMARISER)]
    for p in o["packages"]:
        argv += ["--package", p]
    argv += ["--top", str(o["top"]), recording]
    return argv


def summarise(cfg, recording, out_json):
    """Run JfrSummary.java over `recording`; write its JSON to `out_json`. Returns (summary dict or None, detail)."""
    if not os.path.isfile(recording) or os.path.getsize(recording) == 0:
        return None, "no recording was written (the game may have been killed before it quit, or the JVM refused the option)"
    try:
        r = subprocess.run(summary_argv(cfg, recording), capture_output=True, text=True, timeout=options(cfg)["timeout_s"])
    except subprocess.TimeoutExpired:
        return None, "the summariser took longer than %d s" % options(cfg)["timeout_s"]
    if r.returncode != 0:
        return None, "the summariser failed (exit %d): %s" % (r.returncode, r.stderr.strip()[-300:])
    try:
        summary = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        return None, "the summariser printed no JSON: %s" % e
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1)
        f.write("\n")
    return summary, "ok"


def headline(summary):
    """The few numbers worth carrying into the run report (the full summary stays in logs/)."""
    cpu, alloc, gc = summary["cpu"], summary["allocation"], summary["gc"]
    ex = summary["mod"]["execution"]
    return {
        "jvm": (summary.get("jvm") or {}).get("version", "?")[:60],
        "span_s": summary["event_span_seconds"],
        "cpu_seconds_total": cpu["total_cpu_seconds"],
        "cpu_top_threads": [(t["thread"], t["cpu_seconds"]) for t in cpu["top_threads"][:3]],
        "allocated_mb": round(alloc["total_bytes_observed"] / 1048576.0, 1),
        "gc_collections": gc["collections"],
        "gc_pause_total_ms": gc["pause_total_ms"],
        "gc_pause_longest_ms": gc["pause_longest_ms"],
        "mod_samples": ex["samples_with_a_mod_frame"],
        "mod_top_innermost": [(m["method"], m["samples"]) for m in ex["top_innermost_mod_frame"][:3]],
    }


def remove_raw(path, scratch):
    """The raw recording goes as soon as it is summarised."""
    if os.path.exists(path):
        safe_delete(path, [str(scratch.jfr)])
