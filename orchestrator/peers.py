"""The non-Minecraft sides of the pairs: the scripted Rust peer, bridge-guest, link-java's LinkDemo, the Bevy host and
the Hytale server with its plugin. Each starter returns a `Peer` whose `parse()` turns the output into numbers."""

import json
import os
import re
import shutil
import time
from pathlib import Path

import common
from common import ROOT, expand
from harness import ANSI

PEER_BIN = ROOT / "peer" / "target" / "release" / "live_peer"


class Peer:
    def __init__(self, ctx, proc, kind):
        self.ctx = ctx
        self.proc = proc
        self.kind = kind

    def text(self):
        return ANSI.sub("", self.proc.text())

    def alive(self):
        return self.proc.poll() is None


# ---------------------------------------------------------------------------------------------- live_peer

def start_live_peer(ctx, role, port, name, label, extra=()):
    argv = [str(PEER_BIN), role, "--port", str(port), "--label", label] + [str(x) for x in extra]
    return Peer(ctx, ctx.start_tool(name, argv), "live_peer")


def parse_live_peer(peer):
    """The RBL-INTEROP-RESULT json of a finished live_peer, or None."""
    for line in reversed(peer.proc.text().splitlines()):
        if line.startswith("RBL-INTEROP-RESULT "):
            try:
                return json.loads(line[len("RBL-INTEROP-RESULT "):])
            except json.JSONDecodeError:
                return None
    return None


# ---------------------------------------------------------------------------------------------- bridge-guest

def start_bridge_guest(ctx, cfg, port, name, seconds):
    exe = expand(cfg["tools"]["bridge_guest"])
    argv = [exe] + list(cfg["bridge_guest_args"]) + ["--port", str(port), "--seconds", str(seconds)]
    return Peer(ctx, ctx.start_tool(name, argv), "bridge-guest")


def parse_bridge_guest(peer):
    t = peer.text()
    d = {"exit": peer.proc.p.returncode}
    m = re.search(r"summary: (\d+) cameras, (\d+) skipped, (\d+) frames in ([0-9.]+)s = ([0-9.]+) fps; latency ms .*?mean ([0-9.]+)\s+p50 ([0-9.]+)\s+p95 ([0-9.]+)\s+max ([0-9.]+)", t)
    if m:
        d.update(cameras=int(m.group(1)), skipped=int(m.group(2)), frames=int(m.group(3)), seconds=float(m.group(4)), fps=float(m.group(5)),
                 latency_mean_ms=float(m.group(6)), latency_p95_ms=float(m.group(8)))
    m = re.search(r"connected: host \"(.*?)\", protocol v(\d+)", t)
    if m:
        d["host_game"], d["protocol"] = m.group(1), int(m.group(2))
    d["placed"] = bool(re.search(r"model placed", t))
    d["lost"] = re.findall(r"LOST the host: (.*)", t)
    d["said_bye"] = bool(re.search(r"shutting down", t))
    return d


# ---------------------------------------------------------------------------------------------- link-java LinkDemo

def start_linkdemo(ctx, cfg, role, port, name, seconds, frames=True):
    cp = os.path.join(expand(cfg["tools"]["link_java"]), "build", "classes", "java", "main")
    java = os.path.join(expand(cfg["java_home"]), "bin", "java")
    argv = [java, "-cp", cp, "dev.patodois.rewritelink.LinkDemo", role, "--port", str(port), "--seconds", str(seconds)]
    if frames and role == "guest":
        argv.append("--frames")
    return Peer(ctx, ctx.start_tool(name, argv), "linkdemo")


def parse_linkdemo(peer):
    t = peer.text()
    d = {"exit": peer.proc.p.returncode}
    cams = poses = frames = 0
    for m in re.finditer(r"got (\d+) cameras; sent (\d+) poses, (\d+) frames", t):
        cams += int(m.group(1))
        poses += int(m.group(2))
        frames += int(m.group(3))
    d.update(cameras=cams, poses_sent=poses, frames_sent=frames)
    m = re.search(r"connected: host \"(.*?)\", protocol v(\d+)", t)
    if m:
        d["host_game"], d["protocol"] = m.group(1), int(m.group(2))
    d["pixel_file"] = bool(re.search(r"pixel file .*depth: true", t))
    d["collision_queries"] = len(re.findall(r"collision query", t))
    d["done"] = "done" in t.splitlines()[-1] if t.splitlines() else False
    d["lost"] = re.findall(r"LOST the host: (.*)", t)
    return d


# ---------------------------------------------------------------------------------------------- Bevy host (guest_compose)

def start_guest_compose(ctx, cfg, port, name, seconds):
    exe = expand(cfg["tools"]["guest_compose_install"])
    argv = [exe, "--external", "--port", str(port), "--size", "960x540", "--seconds", str(seconds)]
    return Peer(ctx, ctx.start_tool(name, argv, gui=True), "guest_compose")


def parse_guest_compose(peer):
    t = peer.text()
    d = {"exit": peer.proc.p.returncode}
    secs = re.findall(r"(\d+) fps; guest connected; (\d+) cameras sent, (\d+) frames shown \((\d+) missed\) this second; newest frame (\d+) ms old; camera seq \d+ in the ring: (true|false)", t)
    d["seconds_with_guest"] = len(secs)
    d["cameras_sent"] = sum(int(x[1]) for x in secs)
    d["frames_shown"] = sum(int(x[2]) for x in secs)
    d["frames_missed"] = sum(int(x[3]) for x in secs)
    d["ring_hits"] = sum(1 for x in secs if x[5] == "true")
    d["max_frame_age_ms"] = max([int(x[4]) for x in secs] or [0])
    d["guest_gone"] = re.findall(r"guest gone: (.*)", t)
    d["guest_connected"] = bool(re.search(r"guest connected", t))
    return d


# ---------------------------------------------------------------------------------------------- Hytale server

def hytale_paths(cfg):
    h = cfg["hytale"]
    pkg = expand(h["package"])
    return {
        "java": os.path.join(pkg, "jre", "latest", "Contents", "Home", "bin", "java"),
        "jar": os.path.join(pkg, "game", "latest", "Server", "HytaleServer.jar"),
        "assets": os.path.join(pkg, "game", "latest", "Assets.zip"),
        "plugin": expand(h["plugin_jar"]),
        "dir": expand(h["server_dir"]),
    }


def start_hytale(ctx, cfg, role, port, name):
    """The throwaway headless server of hytale-link's README (offline auth, 127.0.0.1 only), in this repository's own work dir."""
    hp = hytale_paths(cfg)
    sdir = Path(hp["dir"])
    (sdir / "mods").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(hp["plugin"], sdir / "mods" / "HytaleLink.jar")
    props = ctx.logdir / ("%s.link.properties" % name)
    lines = ["role=%s" % role, "port=%d" % port, "publish=auto", "follow.entity=1", "fallback.camera=spawn", "test.disable_chunk_unloading=true"]
    props.write_text("\n".join(lines) + "\n")
    scratch = ctx.new_scratch(name)
    argv = [hp["java"], "-Xmx" + cfg["hytale"]["xmx"], "-Duser.home=" + str(scratch.home), "-Djava.io.tmpdir=" + str(scratch.tmp),
            "-Dhytalelink.config=" + str(props), "-jar", hp["jar"],
            "--assets", hp["assets"], "--auth-mode", "offline", "--bind", cfg["hytale"]["bind"],
            "--universe", str(sdir / "universe"), "--disable-sentry", "--disable-file-watcher"]
    # The one launch that needs the installed Hytale: its Java runtime, server jar and Assets.zip are READ from the package folder
    # (declared read-only here; the sandbox denies writes there). Everything it writes goes to work/hytale-server (cwd, --universe) or the scratch folder.
    return Peer(ctx, ctx.start_game(name, argv, scratch, cwd=str(sdir), stdin_pipe=True, read_only=[expand(cfg["hytale"]["package"])]), "hytale")


def hytale_ready(peer):
    return "Enabled plugin dev.patodois:HytaleLink" in peer.text()


def stop_hytale(ctx, peer, timeout=40):
    peer.proc.send_line("stop")
    return ctx.wait_exit(peer.proc, timeout)


def parse_hytale(peer):
    t = peer.text()
    d = {"exit": peer.proc.p.returncode}
    m = re.search(r'LINK CONNECTED: peer "(.*?)" \((host|guest)\), protocol v(\d+), session (0x[0-9a-f]+)', t)
    if m:
        d.update(peer_game=m.group(1), peer_role=m.group(2), protocol=int(m.group(3)))
    d["connected"] = bool(m)
    d["peer_spawned"] = re.findall(r"peer spawned entity (\d+) \((.*?)\)", t)
    # The plugin prints cumulative counters every 5 s; how many such lines a run gets depends on its length, so the numbers
    # that are compared between runs are per second, from the first and the last status line of the session.
    import datetime
    sts = []
    for m in re.finditer(r"\[(\d{4})/(\d\d)/(\d\d) (\d\d):(\d\d):(\d\d)\s+INFO\].*?status: cameras sent (\d+), poses sent (\d+), poses applied (\d+), entities spawned (\d+)", t):
        ts = datetime.datetime(*[int(x) for x in m.groups()[:6]]).timestamp()
        sts.append((ts,) + tuple(int(x) for x in m.groups()[6:]))
    if sts:
        d["status_lines"] = len(sts)
        d["cameras_sent"] = max(x[1] for x in sts)
        d["poses_sent"] = max(x[2] for x in sts)
        d["poses_applied"] = max(x[3] for x in sts)
        d["entities_spawned"] = max(x[4] for x in sts)
        dt = sts[-1][0] - sts[0][0]
        if len(sts) >= 2 and dt >= 4:
            d["cameras_per_s"] = round((sts[-1][1] - sts[0][1]) / dt, 1)
            d["poses_sent_per_s"] = round((sts[-1][2] - sts[0][2]) / dt, 1)
            d["poses_applied_per_s"] = round((sts[-1][3] - sts[0][3]) / dt, 1)
    d["lost"] = re.findall(r"LINK LOST: (.*)", t)
    d["closed"] = "link closed" in t
    d["shutdown_completed"] = "Shutdown completed" in t
    d["errors"] = [l[:200] for l in t.splitlines() if re.search(r"\bERROR\b|SEVERE|Exception", l) and "HytaleLink" in l][:5]
    d["warnings_plugin"] = [l[:200] for l in t.splitlines() if "WARN" in l and "HytaleLink" in l][:5]
    return d
