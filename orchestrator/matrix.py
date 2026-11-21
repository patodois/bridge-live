"""The pairs: what runs together, how the scenario is driven, and what counts as a pass.

Every scenario fills `ctx.checks` (a pair passes when every gating check is ok), `ctx.exact` (discrete protocol
counts and flags: compared exactly with the previous run) and `ctx.rates` (counts that depend on timing: judged against
floors here, and against a drop of more than `rate_drop_percent` in the comparison).
"""

import json
import os
import re
import secrets
import socket
import stat
import time
from pathlib import Path

import common
import mc as mcmod
import peers as pr
from common import expand
from harness import Abort

# --------------------------------------------------------------------------------------------- the list

MC_ORDER = [("26.3", "fabric"), ("26.3", "neoforge"), ("26.3", "forge"), ("1.20.1", "fabric"), ("1.20.1", "forge"), ("1.20.1", "neoforge"),
            ("1.12.2", "forge"), ("1.7.10", "forge")]
HYTALE_HOST_BUILDS = {("26.3", "fabric"), ("1.20.1", "fabric"), ("1.12.2", "forge"), ("1.7.10", "forge")}


def all_pairs(cfg):
    pairs = []
    for ver, loader in MC_ORDER:
        if loader not in cfg["mc"][ver]["loaders"]:
            continue
        b = "mc-%s-%s" % (ver, loader)
        for sc, fn in (("peer-guest", sc_mc_host_live), ("peer-host", sc_mc_guest_live), ("bridge-guest", sc_mc_host_bridge),
                       ("java-frames", sc_mc_host_java), ("hytale-guest", sc_mc_host_hytale)):
            pairs.append({"id": "%s/%s" % (b, sc), "gui": True, "fn": fn, "args": (ver, loader), "needs": ["bridge-guest"] if sc == "bridge-guest" else (["hytale"] if sc == "hytale-guest" else [])})
        if (ver, loader) in HYTALE_HOST_BUILDS:
            pairs.append({"id": "%s/hytale-host" % b, "gui": True, "fn": sc_mc_guest_hytale, "args": (ver, loader), "needs": ["hytale"]})
    pairs.append({"id": "mc-mc/26.3-fabric-host+1.20.1-fabric-guest", "gui": True, "fn": sc_mc_mc, "args": (("26.3", "fabric"), ("1.20.1", "fabric")), "needs": []})
    pairs.append({"id": "mc-mc/1.20.1-fabric-host+26.3-fabric-guest", "gui": True, "fn": sc_mc_mc, "args": (("1.20.1", "fabric"), ("26.3", "fabric")), "needs": []})
    pairs.append({"id": "hytale-host/peer-guest", "gui": False, "fn": sc_hytale_host_live, "args": (), "needs": ["hytale"]})
    pairs.append({"id": "hytale-guest/peer-host", "gui": False, "fn": sc_hytale_guest_live, "args": (), "needs": ["hytale"]})
    pairs.append({"id": "hytale-host/bridge-guest", "gui": False, "fn": sc_hytale_host_bridge, "args": (), "needs": ["hytale", "bridge-guest"]})
    pairs.append({"id": "bevy-host/bridge-guest", "gui": True, "fn": sc_bevy_bridge, "args": (), "needs": ["bridge-guest", "guest_compose"]})
    return pairs


# Minecraft builds that also get the wrong-token pairs: one per Minecraft version (a modern loader where there are several).
WRONG_TOKEN_BUILDS = [("26.3", "fabric"), ("1.20.1", "fabric"), ("1.12.2", "forge"), ("1.7.10", "forge")]


def token_pairs(cfg):
    """The token matrix (contract section 6.2, added in revision 6; live_peer implements revision 8), all through the same launch path as the plain one: every Minecraft build as host and as
    guest of the scripted Rust peer with a token handed over by an endpoint file; one wrong-token pair per Minecraft version in each role (the refused
    side must see `bye unauthorized`); two Minecraft clients with each other. The programs without token support (bridge-guest, link-java's LinkDemo,
    the Hytale plugin) are not in it."""
    pairs = []
    for ver, loader in MC_ORDER:
        if loader not in cfg["mc"][ver]["loaders"]:
            continue
        b = "mc-%s-%s" % (ver, loader)
        pairs.append({"id": "%s/token-peer-guest" % b, "gui": True, "fn": sc_token_mc_host, "args": (ver, loader), "needs": []})
        pairs.append({"id": "%s/token-peer-host" % b, "gui": True, "fn": sc_token_mc_guest, "args": (ver, loader), "needs": []})
    for ver, loader in WRONG_TOKEN_BUILDS:
        if loader not in cfg["mc"][ver]["loaders"]:
            continue
        b = "mc-%s-%s" % (ver, loader)
        pairs.append({"id": "%s/token-wrong-guest" % b, "gui": True, "fn": sc_token_wrong_guest, "args": (ver, loader), "needs": []})
        pairs.append({"id": "%s/token-wrong-host" % b, "gui": True, "fn": sc_token_wrong_host, "args": (ver, loader), "needs": []})
    pairs.append({"id": "mc-mc/token-26.3-fabric-host+1.20.1-fabric-guest", "gui": True, "fn": sc_token_mc_mc, "args": (("26.3", "fabric"), ("1.20.1", "fabric")), "needs": []})
    pairs.append({"id": "mc-mc/token-1.20.1-fabric-host+26.3-fabric-guest", "gui": True, "fn": sc_token_mc_mc, "args": (("1.20.1", "fabric"), ("26.3", "fabric")), "needs": []})
    return pairs


class Ports:
    def __init__(self, base=47900):
        self.n = base

    def next(self):
        while True:
            self.n += 1
            s = socket.socket()
            try:
                s.bind(("127.0.0.1", self.n))
                return self.n
            except OSError:
                continue
            finally:
                s.close()


PORTS = Ports()


# --------------------------------------------------------------------------------------------- shared checks

def _mc_common(ctx, mc, rc, label=None):
    """Checks every Minecraft client gets: build and exit, sound, crashes, errors, the screenshot of its window."""
    p = label or mc.label
    fin = mc.finish()
    ctx.check("%s: gradle build ended well (exit %s)" % (p, rc), rc == 0 and mc.gradle_ok is not False, "exit %s, BUILD SUCCESSFUL seen: %s" % (rc, mc.gradle_ok))
    ok_pre, d_pre = mc.options_pre
    ctx.check("%s: sound off before launch (master and all categories)" % p, ok_pre, d_pre)
    ok_post, d_post = mc.options_post
    ctx.check("%s: sound still off after the game rewrote options.txt" % p, ok_post, d_post)
    al = mc.openal
    if al and al["seen"]:
        ctx.check("%s: OpenAL opened its null backend (no sound device)" % p, al["null"], 'initialised backend(s): %s' % (", ".join(al["other"] or [al["backend"]])))
    else:
        ctx.check("%s: OpenAL opened its null backend (no sound device)" % p, False, "OpenAL wrote no log line (this client may not use OpenAL Soft); the options.txt volumes are the guard", gate=False)
    ctx.check("%s: no crash report" % p, not fin["new_crash_reports"], str(fin["new_crash_reports"]))
    if mc.jfr_result and mc.jfr_result.get("recorded") is False and mc.jfr_plan["active"]:
        ctx.check("%s: JFR recording summarised" % p, False, mc.jfr_result["reason"], gate=False)
    ctx.exact["%s.worlds_removed" % p] = len(fin["worlds_removed"])
    ctx.rates["%s.error_lines" % p] = len(mc.errors)
    for e in mc.errors[:3]:
        ctx.evid("%s ERROR: %s" % (p, e))
    ctx.check("%s: the autotest script ran to its end" % p, any(m[1] == "END_SESSION" and m[3] == mc.proc.name for m in ctx.markers), "markers: %s" % [m[1] for m in ctx.markers if m[3] == mc.proc.name])


def _shot_check(ctx, expected=1):
    good = [s for s in ctx.shots if s["ok"]]
    ctx.check("screenshot of the game window", len(good) >= expected, "; ".join("%s: %s" % (s["label"], s["detail"] or "ok") for s in ctx.shots) or "none taken", gate=False)


def no_anomalies(m, strict_after_bye=False):
    """(ok, detail): no malformed and no foreign datagram while connected. Both counters must have been READ from a connected status line:
    a status line format the parser no longer understands is a failure, not a pass. `strict_after_bye`: malformed counts after the bye too."""
    mal, forn, stale = m.peak_live("malformed"), m.peak_live("foreign"), m.peak_live("stale")
    ok = mal is not None and forn is not None and mal == 0 and forn == 0
    detail = "while connected: malformed %s foreign %s stale %s" % (mal, forn, stale)
    if mal is None or forn is None:
        detail += " (no connected status line carried the counters)"
    if strict_after_bye:
        ok = ok and (m.peak("malformed") or 0) == 0
        detail += "; in the last status after the bye: foreign %s" % m.peak("foreign")
    return ok, detail


def _mc_host_numbers(ctx, mc, tag="mc", frames=True, min_poses=100, min_frames=100, peer_ends=True):
    """Numbers and checks of an MC client that was the host. `peer_ends`: the other side ends the session (its bye must be on this client's
    log); otherwise this client ends it with `link stop` (its own `link closed` must be there, and the other side checks the bye arrived)."""
    ctx.check("%s: peer connected as a guest, protocol v1" % tag, any(c[1] == "guest" and c[2] == 1 for c in mc.connected), str(mc.connected))
    live = [s for s in mc.statuses if s.get("connected") and s.get("role") == "host"]
    ctx.check("%s: /link status shows a connected session" % tag, bool(live), "%d status lines, %d connected" % (len(mc.statuses), len(live)))
    for s in mc.evidence_statuses():
        ctx.evid("%s status: %s" % (tag, s), 700)
    ctx.exact["%s.connected" % tag] = sorted(set(c[0] for c in mc.connected))
    ctx.exact["%s.spawned" % tag] = sorted(set(x[1] for x in mc.spawned))
    ctx.exact["%s.guest_entities_peak" % tag] = mc.peak("guest_entities")
    for k in ("malformed", "stale", "foreign"):
        ctx.exact["%s.%s_max" % (tag, k)] = mc.peak_live(k)
    ctx.rates["%s.foreign_after_bye" % tag] = (mc.peak("foreign") or 0)
    ctx.check("%s: no malformed and no foreign datagrams while connected" % tag, *no_anomalies(mc, strict_after_bye=True))
    ctx.check("%s: one guest entity shown" % tag, mc.peak("guest_entities") == 1, "peak %s" % mc.peak("guest_entities"))
    pr_ = mc.peak("poses_received") or 0
    ctx.check("%s: poses received (>= %d)" % (tag, min_poses), pr_ >= min_poses, str(pr_))
    ctx.rates["%s.cameras_sent" % tag] = mc.peak("cameras_sent") or 0
    ctx.rates["%s.poses_received" % tag] = pr_
    ctx.rates["%s.collision_snapshots" % tag] = mc.peak("collision_snapshots") or 0
    ctx.rates["%s.datagrams_in" % tag] = mc.peak("dg_in") or 0
    ctx.rates["%s.datagrams_out" % tag] = mc.peak("dg_out") or 0
    if frames:
        fr = mc.peak("frames_received") or 0
        dr = mc.peak("frames_drawn")
        rp = mc.peak("reprojected")
        ctx.rates["%s.frames_received" % tag] = fr
        ctx.check("%s: frames with depth received (>= %d)" % (tag, min_frames), fr >= min_frames, str(fr))
        if dr is None:
            ctx.check("%s: frames drawn is reported by /link status" % tag, False, "no 'frames drawn' in the status line")
        else:
            ctx.rates["%s.frames_drawn" % tag] = dr
            ctx.rates["%s.reprojected" % tag] = rp or 0
            ctx.check("%s: frames drawn in the world (>= %d)" % (tag, min_frames // 2), dr >= min_frames // 2, "drawn %s reprojected %s" % (dr, rp))
        depth = [s.get("pixel_file", "") for s in mc.statuses if s.get("pixel_file")]
        ctx.exact["%s.pixel_file" % tag] = depth[-1] if depth else None
        ctx.check("%s: the pixel file carried a depth plane" % tag, bool(depth) and "depth" in depth[-1], str(depth[-1:] or "none"))
    if peer_ends:
        ctx.check("%s: the peer ended the session with a bye (`peer said bye` on the game's log)" % tag, bool(mc.bye), "; ".join(mc.bye[:2] + mc.lost[:2]) or "no bye, no loss seen")
    else:
        ctx.check("%s: this client closed the link itself (`link closed` on the game's log)" % tag, bool(mc.closed), "; ".join(mc.closed[:1] + mc.lost[:2]) or "not closed")
    ctx.exact["%s.closed_seen" % tag] = bool(mc.closed)
    ctx.exact["%s.bye_seen" % tag] = bool(mc.bye)


def _peer_exit(ctx, peer, name, timeout=45):
    rc = ctx.wait_exit(peer.proc, timeout)
    if rc is None:
        ctx.check("%s: ended by itself" % name, False, "still running after %d s, killed" % timeout)
        peer.proc.terminate(3)
        return None
    return rc


def _live_result(ctx, peer, name):
    rc = _peer_exit(ctx, peer, name)
    r = pr.parse_live_peer(peer)
    ctx.check("%s: result line printed" % name, r is not None, "exit %s" % rc)
    if r is None:
        return None
    ctx.check("%s: its own checks passed (exit %s)" % (name, rc), r["ok"] and rc == 0, "; ".join(r["failures"]))
    ctx.evid("%s result: %s" % (name, {k: r[k] for k in ("spawns", "despawns", "cameras", "poses", "focus", "lost_reason", "stats")}), 600)
    st = r["stats"]
    for k in ("malformed", "foreign", "stale", "unknown_entity", "spawn_ignored"):
        ctx.exact["%s.stats.%s" % (name, k)] = st[k]
    ctx.exact["%s.spawns" % name] = r["spawns"]
    ctx.exact["%s.despawns" % name] = r["despawns"]
    ctx.exact["%s.focus" % name] = r["focus"]
    ctx.exact["%s.protocol" % name] = r["protocol"]
    ctx.exact["%s.peer_game" % name] = r["peer_game"]
    ctx.rates["%s.cameras" % name] = r["cameras"]
    ctx.rates["%s.poses" % name] = r["poses"]
    for k in ("poses_sent", "frames_sent", "collisions", "depth_finite_pixels_max", "max_moved_m"):
        if r["extra"].get(k):
            ctx.rates["%s.%s" % (name, k)] = r["extra"][k]
    return r


def _mc_run(ctx, mc, timeout=420):
    """Launch, wait for the game to end by itself (the script quits it); kill it if it does not."""
    mc.prepare()
    mc.launch()
    rc = ctx.wait_exit(mc.proc, timeout)
    if rc is None:
        ctx.check("%s: finished within %d s" % (mc.label, timeout), False, "timed out; killed. last markers: %s" % ctx.markers[-3:])
        mc.proc.terminate(4)
        rc = mc.proc.p.returncode
    return rc


def _mc_spec(cfg, ver):
    s = cfg["mc"][ver]
    return s["family"], s["ground_y"]


# --------------------------------------------------------------------------------------------- token helpers

def _endpoint_dir(ctx, label):
    """A folder for this pair's endpoint files: owner-only (0700), inside a scratch folder that goes away with the pair."""
    d = ctx.new_scratch("endpoint-" + label).root / "endpoint"
    d.mkdir(mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def _write_endpoint(path, port, token):
    """An endpoint file the way CONTRACT 6.2 writes one (exclusive, 0600 from the start, renamed into place); used for the WRONG token."""
    tmp = path.with_name("." + path.name + ".tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps({"control_port": port, "token": token}) + "\n")
    os.replace(str(tmp), str(path))


def _look_at_endpoint(path):
    """({'mode': '600', 'port': N, 'token_ok': bool, 'keys': [...]}, token or None). The token's value stays in memory: it is only compared
    with the logs afterwards and never goes into a check, an evidence line or a report."""
    try:
        st = os.stat(str(path))
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    tok = data.get("token")
    ok = isinstance(tok, str) and re.fullmatch(r"[0-9a-f]{64}", tok) is not None
    return {"mode": "%o" % stat.S_IMODE(st.st_mode), "port": data.get("control_port"), "token_ok": ok, "keys": sorted(data)}, (tok if ok else None)


class TokenWatch:
    """The tokens seen during a pair (never printed), and the check that none of them reached a log."""

    def __init__(self):
        self.tokens = []

    def grab(self, path):
        info, tok = _look_at_endpoint(path)
        if tok and tok not in self.tokens:
            self.tokens.append(tok)
        return info

    def add(self, tok):
        if tok and tok not in self.tokens:
            self.tokens.append(tok)

    def scan(self, ctx, mcs, what="this pair"):
        hits, files = [], 0
        needles = [t.encode() for t in self.tokens]
        for p in ctx.procs:
            t = p.text()
            if any(tok in t for tok in self.tokens):
                hits.append("console output of %s" % p.name)
        for d in token_scan_dirs(ctx, mcs):
            if not d.is_dir():
                continue
            for root, _dn, fn in os.walk(str(d)):
                for name in fn:
                    f = os.path.join(root, name)
                    try:
                        if os.path.getsize(f) > 96 * 1024 * 1024:
                            continue
                        data = Path(f).read_bytes()
                    except OSError:
                        continue
                    files += 1
                    if any(n in data for n in needles):
                        hits.append(os.path.relpath(f, str(ROOT_DIR)))
        ctx.check("token: no token appears in any log, console output or crash report of %s" % what, bool(self.tokens) and not hits,
                  ("%d token(s) looked for in %d files" % (len(self.tokens), files)) if not hits else "found in: " + ", ".join(hits[:4]))


ROOT_DIR = Path(__file__).resolve().parent.parent


def token_scan_dirs(ctx, mcs):
    """Where a token could have been written during a pair: this tool's logs of the pair and, for every client, `logs/` and `crash-reports/`
    of its game directory AND of the checkout's run folder. With a scratch instance the Forge and NeoForge run tasks keep the checkout's
    `run/` as the process directory, and log4j writes `logs/latest.log` and `logs/debug.log` there (mc.py, `allow_write`)."""
    out = [Path(ctx.logdir)]
    for m in mcs:
        for base in (m.run_dir, m.checkout_run_dir):
            for sub in ("logs", "crash-reports"):
                d = Path(base) / sub
                if d not in out:
                    out.append(d)
    return out


def _endpoint_checks(ctx, tag, info, port, gone_path=None, host_closed=True):
    # CONTRACT 6.2: a reader ignores unknown fields "so a later revision can add some": the two keys must be there, more is evidence only.
    ctx.check("%s: the host wrote the endpoint file after binding, mode 600, with the bound port and a 64-digit token" % tag,
              bool(info) and info["mode"] == "600" and info["port"] == port and info["token_ok"] and {"control_port", "token"} <= set(info["keys"]), str(info))
    extra = sorted(set(info["keys"]) - {"control_port", "token"}) if info else []
    if extra:
        ctx.evid("%s: the endpoint file has more fields than control_port and token: %s" % (tag, extra))
    if gone_path is not None and host_closed:
        ctx.check("%s: the endpoint file is gone after the host's clean shutdown" % tag, not os.path.exists(str(gone_path)), str(gone_path.name))


# --------------------------------------------------------------------------------------------- MC host scenarios

def _sc_mc_host(ctx, cfg, ver, loader, kind, token=False):
    port = ctx.port = PORTS.next()
    fam, gy = _mc_spec(cfg, ver)
    name = "%s-%s" % (ver, loader)
    efile = _endpoint_dir(ctx, name) / "minecraft-java.json" if token else None
    watch = TokenWatch()
    mc = mcmod.McClient(ctx, cfg, ver, loader, mcmod.host_script(ver, fam, gy, port, name + "/" + kind), name, endpoint_file=efile)
    peer = {}
    seen = {}

    def start_peer(arg, proc):
        if token:
            seen["info"] = watch.grab(efile)   # the host listens now, so its file is there
        if kind == "peer-guest":
            peer["p"] = pr.start_live_peer(ctx, "guest", port, "live-peer", "mc-" + name,
                                           ["--seconds", 12, "--frames", "--expect-cameras", 200, "--expect-collision"] + (["--endpoint-file", efile] if token else []))
        elif kind == "bridge-guest":
            peer["p"] = pr.start_bridge_guest(ctx, cfg, port, "bridge-guest", 14)
        elif kind == "java-frames":
            peer["p"] = pr.start_linkdemo(ctx, cfg, "guest", port, "linkdemo", 13)

    ctx.handlers["START_PEER"] = start_peer
    ctx.handlers["SHOT"] = lambda arg, proc: ctx.screenshot(arg, [mc.proc])
    rc = _mc_run(ctx, mc)
    ctx.check("peer was started when the host listened", "p" in peer, "markers %s" % [m[1] for m in ctx.markers])
    p = peer.get("p")
    if p:
        if kind == "peer-guest":
            r = _live_result(ctx, p, "live-peer")
            if r:
                ctx.check("live-peer: saw the input focus come and go (guest:set, host:set)", r["focus"] == ["guest:set", "host:set"], str(r["focus"]))
                ctx.check("live-peer: frames were sent with depth", r["extra"]["frames_sent"] >= 100 and r["extra"]["depth_finite_pixels_max"] > 0,
                          "frames %s, finite depth pixels %s" % (r["extra"]["frames_sent"], r["extra"]["depth_finite_pixels_max"]))
                ctx.check("live-peer: the host's collision snapshot arrived", r["collision_triangles"] > 0, str(r["collision_triangles"]))
                ctx.check("live-peer: the host said bye (or this peer did) without an error", r["lost_reason"] is None, str(r["lost_reason"]))
        elif kind == "bridge-guest":
            prc = _peer_exit(ctx, p, "bridge-guest", 40)
            b = pr.parse_bridge_guest(p)
            ctx.evid("bridge-guest: %s" % {k: v for k, v in b.items()}, 500)
            ctx.check("bridge-guest: exit 0 after its --seconds", prc == 0, "exit %s" % prc)
            ctx.check("bridge-guest: connected to a Minecraft host, protocol v1", str(b.get("host_game", "")).startswith("Minecraft") and b.get("protocol") == 1, "%s v%s" % (b.get("host_game"), b.get("protocol")))
            ctx.check("bridge-guest: model placed ahead of the camera", b.get("placed"), "")
            ctx.check("bridge-guest: cameras answered with frames (>= 300 each)", b.get("cameras", 0) >= 300 and b.get("frames", 0) >= 300, "%s cameras, %s frames" % (b.get("cameras"), b.get("frames")))
            for k in ("cameras", "frames", "skipped", "fps", "latency_mean_ms", "latency_p95_ms"):
                if k in b:
                    ctx.rates["bridge-guest." + k] = b[k]
            ctx.exact["bridge-guest.protocol"] = b.get("protocol")
            ctx.exact["bridge-guest.host_game"] = b.get("host_game")
        elif kind == "java-frames":
            prc = _peer_exit(ctx, p, "linkdemo", 40)
            j = pr.parse_linkdemo(p)
            ctx.evid("link-java LinkDemo: %s" % j, 500)
            ctx.check("link-java LinkDemo guest: exit 0", prc == 0, "exit %s" % prc)
            ctx.check("link-java: connected to a Minecraft host, protocol v1", str(j.get("host_game", "")).startswith("Minecraft") and j.get("protocol") == 1, "%s v%s" % (j.get("host_game"), j.get("protocol")))
            ctx.check("link-java: announced a pixel file with a depth plane", j.get("pixel_file"), "")
            ctx.check("link-java: cameras answered with poses and frames (>= 100)", j["cameras"] >= 100 and j["poses_sent"] >= 100 and j["frames_sent"] >= 100,
                      "%s cameras, %s poses, %s frames" % (j["cameras"], j["poses_sent"], j["frames_sent"]))
            ctx.check("link-java: host's collision snapshot arrived", j["collision_queries"] > 0, str(j["collision_queries"]))
            for k in ("cameras", "poses_sent", "frames_sent", "collision_queries"):
                ctx.rates["linkdemo." + k] = j[k]
            ctx.exact["linkdemo.protocol"] = j.get("protocol")
    _mc_host_numbers(ctx, mc, frames=True)
    if token:
        _endpoint_checks(ctx, "mc host", seen.get("info"), port, efile)
        live = [s for s in mc.statuses if s.get("connected")]
        ctx.check("mc host: /link status says token auth on", bool(live) and all(s.get("token_auth") for s in live), "%d connected status lines" % len(live))
        ctx.check("mc host: nobody was refused", all(not s.get("refused_unauthorized") for s in mc.statuses) and not mc.unauthorized_notices, str(mc.unauthorized_notices[:1]))
        watch.scan(ctx, [mc])
    _mc_common(ctx, mc, rc)
    _shot_check(ctx)


def sc_token_mc_host(ctx, cfg, ver, loader):
    _sc_mc_host(ctx, cfg, ver, loader, "peer-guest", token=True)


def sc_mc_host_live(ctx, cfg, ver, loader):
    _sc_mc_host(ctx, cfg, ver, loader, "peer-guest")


def sc_mc_host_bridge(ctx, cfg, ver, loader):
    _sc_mc_host(ctx, cfg, ver, loader, "bridge-guest")


def sc_mc_host_java(ctx, cfg, ver, loader):
    _sc_mc_host(ctx, cfg, ver, loader, "java-frames")


# --------------------------------------------------------------------------------------------- MC guest vs live peer host

def sc_token_mc_guest(ctx, cfg, ver, loader):
    sc_mc_guest_live(ctx, cfg, ver, loader, token=True)


def sc_mc_guest_live(ctx, cfg, ver, loader, token=False):
    port = ctx.port = PORTS.next()
    fam, gy = _mc_spec(cfg, ver)
    name = "%s-%s" % (ver, loader)
    efile = _endpoint_dir(ctx, name) / "peer-host.json" if token else None   # the scripted host writes it, the guest's config names it
    watch = TokenWatch()
    seen = {}
    mc = mcmod.McClient(ctx, cfg, ver, loader, mcmod.guest_script(ver, fam, gy, port, name + "/peer-host"), name, endpoint_file=efile)
    peer = {}
    ctx.handlers["START_PEER"] = lambda arg, proc: peer.setdefault("p", pr.start_live_peer(
        ctx, "host", port, "live-peer", "mc-" + name, ["--seconds", 45, "--guest-leaves", "--expect-poses", 100, "--expect-spawns", 1, "--expect-moved", 2.0] + (["--endpoint-file", efile] if token else [])))

    def shot(arg, proc):
        if token and "info" not in seen:
            seen["info"] = watch.grab(efile)    # mid-session: the peer host's file is there
        ctx.screenshot(arg, [mc.proc])
    ctx.handlers["SHOT"] = shot
    rc = _mc_run(ctx, mc)
    ctx.check("peer was started when the guest began to connect", "p" in peer, "markers %s" % [m[1] for m in ctx.markers])
    if "p" in peer:
        r = _live_result(ctx, peer["p"], "live-peer")
        if r:
            ctx.check("live-peer: the guest announced exactly one entity", len(r["spawns"]) == 1, str(r["spawns"]))
            ctx.check("live-peer: the guest asked for the input focus and it was granted", "guest:request" in r["focus"], str(r["focus"]))
            ctx.check("live-peer: the guest despawned its entity and said bye", r["despawns"] == r["spawns"] and "bye" in str(r["lost_reason"]), "despawns %s, lost %s" % (r["despawns"], r["lost_reason"]))
            ctx.exact["live-peer.lost_reason"] = r["lost_reason"]
    live = [s for s in mc.statuses if s.get("connected") and s.get("role") == "guest"]
    ctx.check("mc: connected to the host, protocol v1", any(c[1] == "host" and c[2] == 1 for c in mc.connected), str(mc.connected))
    ctx.check("mc: /link status shows a connected guest session", bool(live), "%d status lines" % len(mc.statuses))
    for s in mc.evidence_statuses():
        ctx.evid("mc status: " + s, 600)
    ctx.exact["mc.connected"] = sorted(set(c[0] for c in mc.connected))
    for k in ("malformed", "stale", "foreign"):
        ctx.exact["mc.%s_max" % k] = mc.peak_live(k)
    ctx.check("mc: no malformed and no foreign datagrams while connected", *no_anomalies(mc))
    ctx.rates["mc.poses_sent"] = mc.peak("poses_sent") or 0
    ctx.rates["mc.cameras_received"] = mc.peak("cameras_received") or 0
    ctx.check("mc: cameras received and poses sent (>= 100 each)", (mc.peak("poses_sent") or 0) >= 100 and (mc.peak("cameras_received") or 0) >= 100, "%s poses, %s cameras" % (mc.peak("poses_sent"), mc.peak("cameras_received")))
    z = [p[2] for p in mc.pos_lines]
    ctx.check("mc: the player stood still while the host held the focus and walked once it was granted",
              len(z) >= 3 and abs(z[1] - z[0]) < 0.5 and z[2] - z[0] >= 2.0, "pos z at start, forward held, after the grant: %s" % [round(v, 2) for v in z])
    ctx.check("mc: the focus came and went (guest, host)", "guest" in mc.focus_events and "host" in mc.focus_events, str(mc.focus_events))
    ctx.exact["mc.focus_events"] = mc.focus_events
    ctx.check("mc: closed the link with bye", bool(mc.closed), str(mc.closed))
    ctx.exact["mc.closed_seen"] = bool(mc.closed)
    if token:
        _endpoint_checks(ctx, "live-peer host", seen.get("info"), port, efile)
        live = [s for s in mc.statuses if s.get("connected")]
        ctx.check("mc guest: /link status says token auth on", bool(live) and all(s.get("token_auth") for s in live), "%d connected status lines" % len(live))
        ctx.check("mc guest: it was not refused", all(not s.get("refused_unauthorized") for s in mc.statuses) and not mc.unauthorized_notices, str(mc.unauthorized_notices[:1]))
        watch.scan(ctx, [mc])
    _mc_common(ctx, mc, rc)
    _shot_check(ctx)


# --------------------------------------------------------------------------------------------- token: wrong token

def sc_token_wrong_guest(ctx, cfg, ver, loader):
    """MC host with a token; the scripted guest has an endpoint file with the right port and a WRONG token and must be refused with `bye unauthorized`."""
    port = ctx.port = PORTS.next()
    fam, gy = _mc_spec(cfg, ver)
    name = "%s-%s" % (ver, loader)
    d = _endpoint_dir(ctx, name)
    efile, wrongfile = d / "minecraft-java.json", d / "wrong.json"
    watch = TokenWatch()
    wrong = secrets.token_hex(32)
    watch.add(wrong)
    seen = {}
    mc = mcmod.McClient(ctx, cfg, ver, loader, mcmod.host_refused_script(ver, fam, gy, port, name + "/token-wrong-guest"), name, endpoint_file=efile)
    peer = {}

    def start_peer(arg, proc):
        seen["info"] = watch.grab(efile)
        _write_endpoint(wrongfile, port, wrong)
        peer["p"] = pr.start_live_peer(ctx, "guest", port, "live-peer", "mc-" + name, ["--seconds", 12, "--endpoint-file", wrongfile, "--expect-refused", "--connect-timeout", 60])
    ctx.handlers["START_PEER"] = start_peer
    rc = _mc_run(ctx, mc)
    ctx.check("peer was started when the host listened", "p" in peer, "markers %s" % [m[1] for m in ctx.markers])
    if "p" in peer:
        r = _live_result(ctx, peer["p"], "live-peer")
        if r:
            ctx.check("live-peer (wrong token): never connected", r["peer_game"] is None and not r["spawns"], str(r["peer_game"]))
            ctx.check("live-peer (wrong token): refused with exactly `peer said bye: unauthorized`", "handshake failed: peer said bye: unauthorized" in peer["p"].text(), "")
            ctx.check("live-peer (wrong token): the refusal was counted", r["extra"].get("refused", 0) >= 1, str(r["extra"].get("refused")))
    _endpoint_checks(ctx, "mc host", seen.get("info"), port, efile)
    _token_refused_mc(ctx, mc, "host", "refused a guest as unauthorized")
    watch.scan(ctx, [mc])
    _mc_common(ctx, mc, rc)


def sc_token_wrong_host(ctx, cfg, ver, loader):
    """MC guest whose endpoint file names the scripted host's port with a WRONG token; the host (with its own token) refuses it."""
    port = ctx.port = PORTS.next()
    fam, gy = _mc_spec(cfg, ver)
    name = "%s-%s" % (ver, loader)
    d = _endpoint_dir(ctx, name)
    hostfile, wrongfile = d / "peer-host.json", d / "wrong.json"
    watch = TokenWatch()
    wrong = secrets.token_hex(32)
    watch.add(wrong)
    _write_endpoint(wrongfile, port, wrong)
    seen = {}
    mc = mcmod.McClient(ctx, cfg, ver, loader, mcmod.guest_refused_script(ver, fam, gy, port, name + "/token-wrong-host"), name, endpoint_file=wrongfile)
    peer = {}

    def start_peer(arg, proc):
        peer["p"] = pr.start_live_peer(ctx, "host", port, "live-peer", "mc-" + name, ["--seconds", 30, "--endpoint-file", hostfile, "--expect-refused", "--connect-timeout", 90])
        # The host peer ends a moment after the first refusal and takes its file with it: catch the real token while the file is there, so the
        # log scan below looks for it too.
        end = time.time() + 5
        while time.time() < end and not seen.get("info"):
            seen["info"] = watch.grab(hostfile)
            time.sleep(0.01)
    ctx.handlers["START_PEER"] = start_peer
    rc = _mc_run(ctx, mc)
    ctx.check("peer was started when the guest began to connect", "p" in peer, "markers %s" % [m[1] for m in ctx.markers])
    if "p" in peer:
        r = _live_result(ctx, peer["p"], "live-peer")
        if r:
            ctx.check("live-peer (host): never connected", r["peer_game"] is None, str(r["peer_game"]))
            ctx.check("live-peer (host): refused the guest with exactly `unauthorized`", "handshake failed: unauthorized" in peer["p"].text(), "")
            ctx.check("live-peer (host): the refusal was counted", r["extra"].get("refused", 0) >= 1, str(r["extra"].get("refused")))
    _token_refused_mc(ctx, mc, "guest", "the host refused this guest as unauthorized")
    ctx.check("token: the host's own token was caught for the log scan", bool(seen.get("info")) and len(watch.tokens) == 2, "%d token(s) known" % len(watch.tokens), gate=False)
    watch.scan(ctx, [mc])
    _mc_common(ctx, mc, rc)


def _token_refused_mc(ctx, mc, role, notice):
    """What the Minecraft side of a wrong-token pair must show: never connected, token auth on, the refusal counted in /link status and told once."""
    ctx.check("mc %s: never connected" % role, not mc.connected and not any(s.get("connected") for s in mc.statuses), str(mc.connected))
    ctx.check("mc %s: /link status says token auth on" % role, bool(mc.statuses) and all(s.get("token_auth") for s in mc.statuses), "%d status lines" % len(mc.statuses))
    n = max([s.get("refused_unauthorized", 0) for s in mc.statuses] or [0])
    ctx.check("mc %s: /link status counts the refusal as unauthorized" % role, n >= 1, "refused %s" % n)
    ctx.exact["mc.%s.refused_seen" % role] = n >= 1
    ctx.check("mc %s: the player was told (\"%s\")" % (role, notice), any(notice in l for l in mc.unauthorized_notices), str(mc.unauthorized_notices[:1]))
    ctx.check("mc %s: told once, not for every retry" % role, len(mc.unauthorized_notices) <= 1, "%d notices" % len(mc.unauthorized_notices))
    ctx.check("mc %s: the chat/log text names no token and no `handshake failed` noise" % role, not any("handshake failed" in l for l in mc.unauthorized_notices), "")


# --------------------------------------------------------------------------------------------- Hytale

def _hytale_up(ctx, cfg, role, port, name="hytale"):
    h = pr.start_hytale(ctx, cfg, role, port, name)
    ok = ctx.wait_for(lambda: pr.hytale_ready(h) or not h.alive(), 180)
    ctx.check("hytale: server started with the plugin enabled", ok and pr.hytale_ready(h), "ready after %.0f s" % (time.time() - h.proc.started))
    return h if pr.hytale_ready(h) else None


def _hytale_down(ctx, h):
    rc = pr.stop_hytale(ctx, h, 45)
    if rc is None:
        ctx.check("hytale: server stopped by its own stop command", False, "still running after 45 s, killed")
        h.proc.terminate(5)
    return rc


def _rate_or_count(d, rate_key, count_key, floor_per_s, floor_count):
    """(ok, text): the per-second rate between the first and last status line, or the cumulative count if there was only one."""
    r = d.get(rate_key)
    if r is not None:
        return r >= floor_per_s, "%.1f/s (floor %s/s)" % (r, floor_per_s)
    c = d.get(count_key, 0)
    return c >= floor_count, "%s in total, only one status line (floor %s)" % (c, floor_count)


def _hytale_checks(ctx, h, *, peer_prefix, peer_role, min_poses_sent=False, min_applied=False, min_cameras=False, spawned_prefix=None):
    """Hytale's side from the plugin's log lines. Floors are per second (the game's own publish rate is about 19/s)."""
    d = pr.parse_hytale(h)
    for l in d.get("errors", []):
        ctx.evid("hytale error line: " + l)
    ctx.evid("hytale: %s" % {k: v for k, v in d.items() if k not in ("errors", "warnings_plugin")}, 700)
    ctx.check("hytale: link connected, protocol v1", d["connected"] and d.get("protocol") == 1, "%s" % d.get("peer_game"))
    ctx.check("hytale: peer is a %s named %s*" % (peer_role, peer_prefix), d.get("peer_role") == peer_role and str(d.get("peer_game", "")).startswith(peer_prefix), "%s (%s)" % (d.get("peer_game"), d.get("peer_role")))
    ctx.check("hytale: status lines printed", d.get("status_lines", 0) >= 1, str(d.get("status_lines")))
    for flag, rk, ck, label in ((min_poses_sent, "poses_sent_per_s", "poses_sent", "poses sent"), (min_cameras, "cameras_per_s", "cameras_sent", "cameras sent"),
                                (min_applied, "poses_applied_per_s", "poses_applied", "the peer's poses applied")):
        if flag:
            ok, txt = _rate_or_count(d, rk, ck, 10.0, 40)
            ctx.check("hytale: %s (>= 10/s)" % label, ok, txt)
    if spawned_prefix:
        ctx.check("hytale: peer spawned an entity (%s)" % spawned_prefix, any(x[1].startswith(spawned_prefix) for x in d["peer_spawned"]), str(d["peer_spawned"]))
    ctx.check("hytale: the session ended with the peer's bye", any("bye" in x for x in d["lost"]), str(d["lost"]))
    ctx.check("hytale: link closed and server shut down cleanly (exit 0)", d["closed"] and d["shutdown_completed"] and d["exit"] == 0, "exit %s closed %s shutdown %s" % (d["exit"], d["closed"], d["shutdown_completed"]))
    ctx.check("hytale: no plugin error lines", not d["errors"], str(d["errors"][:2]))
    ctx.exact["hytale.peer_game"] = d.get("peer_game")
    ctx.exact["hytale.peer_spawned"] = sorted(set(x[1] for x in d["peer_spawned"]))
    ctx.exact["hytale.entities_spawned"] = d.get("entities_spawned")
    ctx.exact["hytale.lost"] = d["lost"]
    for k in ("cameras_per_s", "poses_sent_per_s", "poses_applied_per_s"):
        if k in d:
            ctx.rates["hytale." + k] = d[k]
    return d


def sc_mc_host_hytale(ctx, cfg, ver, loader):
    """MC is the host, the Hytale server (plugin as guest, no player: fallback pose at the world spawn) connects."""
    port = ctx.port = PORTS.next()
    fam, gy = _mc_spec(cfg, ver)
    name = "%s-%s" % (ver, loader)
    h = _hytale_up(ctx, cfg, "guest", port)
    if not h:
        return
    mc = mcmod.McClient(ctx, cfg, ver, loader, mcmod.host_script(ver, fam, gy, port, name + "/hytale-guest", wall=False), name)
    ctx.handlers["SHOT"] = lambda arg, proc: ctx.screenshot(arg, [mc.proc])
    rc = _mc_run(ctx, mc)
    _hytale_down(ctx, h)
    _hytale_checks(ctx, h, peer_prefix="Minecraft", peer_role="host", min_poses_sent=True)
    _mc_host_numbers(ctx, mc, frames=False, min_poses=60, peer_ends=False)   # the Hytale guest stays; MC ends with `link stop`
    ctx.check("mc: the Hytale entity arrived as 'Hytale Human/Player'", any("Hytale" in x[1] for x in mc.spawned), str(mc.spawned))
    _mc_common(ctx, mc, rc)
    _shot_check(ctx)


def sc_mc_guest_hytale(ctx, cfg, ver, loader):
    """MC is the guest, the Hytale server (plugin as host) listens. The MC player's pose drives the followed entity."""
    port = ctx.port = PORTS.next()
    fam, gy = _mc_spec(cfg, ver)
    name = "%s-%s" % (ver, loader)
    h = _hytale_up(ctx, cfg, "host", port)
    if not h:
        return
    mc = mcmod.McClient(ctx, cfg, ver, loader, mcmod.guest_script(ver, fam, gy, port, name + "/hytale-host"), name)
    ctx.handlers["SHOT"] = lambda arg, proc: ctx.screenshot(arg, [mc.proc])
    rc = _mc_run(ctx, mc)
    _hytale_down(ctx, h)
    _hytale_checks(ctx, h, peer_prefix="Minecraft", peer_role="guest", min_cameras=True, min_applied=True, spawned_prefix="Minecraft")
    live = [s for s in mc.statuses if s.get("connected") and s.get("role") == "guest"]
    ctx.check("mc: /link status shows a connected guest session", bool(live), "%d status lines" % len(mc.statuses))
    for s in mc.evidence_statuses():
        ctx.evid("mc status: " + s, 600)
    ctx.check("mc: connected to the Hytale host, protocol v1", any(c[1] == "host" and c[2] == 1 and "Hytale" in c[0] for c in mc.connected), str(mc.connected))
    for k in ("malformed", "foreign", "stale"):
        ctx.exact["mc.%s_max" % k] = mc.peak_live(k)
    ctx.check("mc: no malformed and no foreign datagrams while connected", *no_anomalies(mc))
    ctx.rates["mc.poses_sent"] = mc.peak("poses_sent") or 0
    ctx.rates["mc.cameras_received"] = mc.peak("cameras_received") or 0
    ctx.check("mc: cameras received and poses sent (>= 100 each)", (mc.peak("poses_sent") or 0) >= 100 and (mc.peak("cameras_received") or 0) >= 100, "%s poses, %s cameras" % (mc.peak("poses_sent"), mc.peak("cameras_received")))
    ctx.check("mc: closed the link with bye", bool(mc.closed), str(mc.closed))
    _mc_common(ctx, mc, rc)
    _shot_check(ctx)


def sc_hytale_host_live(ctx, cfg):
    """Hytale server as host, the scripted peer as guest sending frames with depth."""
    port = ctx.port = PORTS.next()
    h = _hytale_up(ctx, cfg, "host", port)
    if not h:
        return
    p = pr.start_live_peer(ctx, "guest", port, "live-peer", "hytale", ["--seconds", 14, "--frames", "--expect-cameras", 100])
    r = _live_result(ctx, p, "live-peer")
    _hytale_down(ctx, h)
    if r:
        ctx.check("live-peer: frames were sent with depth", r["extra"]["frames_sent"] >= 100 and r["extra"]["depth_finite_pixels_max"] > 0, "frames %s" % r["extra"]["frames_sent"])
    _hytale_checks(ctx, h, peer_prefix="Live Peer", peer_role="guest", min_cameras=True, min_applied=True, spawned_prefix="Live Peer")


def sc_hytale_guest_live(ctx, cfg):
    """Hytale server as guest (no player: fallback pose), the scripted peer as host spawning an entity near the guest's pose."""
    port = ctx.port = PORTS.next()
    h = _hytale_up(ctx, cfg, "guest", port)
    if not h:
        return
    p = pr.start_live_peer(ctx, "host", port, "live-peer", "hytale", ["--seconds", 14, "--expect-poses", 60, "--expect-spawns", 1])
    r = _live_result(ctx, p, "live-peer")
    _hytale_down(ctx, h)
    if r:
        # The plugin sometimes announces its entity twice within 6 ms at the start of a session (1 run in about 25; see README, "Findings").
        ctx.exact["live-peer.spawns"] = sorted(set(r["spawns"]))
        ctx.rates["live-peer.spawn_events"] = len(r["spawns"])
        ctx.check("live-peer: the guest spawned 'Human/Player' (entity 1)", sorted(set(r["spawns"])) == [1], str(r["spawns"]))
        ctx.check("hytale (plugin): the entity is announced exactly once per session", r["spawns"] == [1], "spawn events seen by the host: %s; lines received by the host: %s" % (r["spawns"], r["extra"].get("lines_received")), gate=False)
        ctx.check("live-peer: the guest's entity stayed put", r["extra"]["max_moved_m"] < 1.0, "moved %.2f m" % r["extra"]["max_moved_m"])
    _hytale_checks(ctx, h, peer_prefix="Live Peer", peer_role="host", min_poses_sent=True, min_applied=True)


def sc_hytale_host_bridge(ctx, cfg):
    """Hytale server as host, bridge-guest rendering a prop with the host's camera."""
    port = ctx.port = PORTS.next()
    h = _hytale_up(ctx, cfg, "host", port)
    if not h:
        return
    p = pr.start_bridge_guest(ctx, cfg, port, "bridge-guest", 14)
    prc = _peer_exit(ctx, p, "bridge-guest", 60)
    b = pr.parse_bridge_guest(p)
    _hytale_down(ctx, h)
    ctx.evid("bridge-guest: %s" % b, 500)
    ctx.check("bridge-guest: exit 0 after its --seconds", prc == 0, "exit %s" % prc)
    ctx.check("bridge-guest: connected to the Hytale host, protocol v1", str(b.get("host_game", "")).startswith("Hytale") and b.get("protocol") == 1, "%s v%s" % (b.get("host_game"), b.get("protocol")))
    ctx.check("bridge-guest: model placed ahead of the camera", b.get("placed"), "")
    # The Hytale fallback camera comes at about 19 per second, so far fewer cameras than from a game.
    ctx.check("bridge-guest: cameras answered with frames (>= 100 each)", b.get("cameras", 0) >= 100 and b.get("frames", 0) >= 100, "%s cameras, %s frames" % (b.get("cameras"), b.get("frames")))
    for k in ("cameras", "frames", "skipped", "fps", "latency_mean_ms", "latency_p95_ms"):
        if k in b:
            ctx.rates["bridge-guest." + k] = b[k]
    ctx.exact["bridge-guest.host_game"] = b.get("host_game")
    _hytale_checks(ctx, h, peer_prefix="bridge-guest", peer_role="guest", min_cameras=True, min_applied=True)


# --------------------------------------------------------------------------------------------- Bevy host

def sc_bevy_bridge(ctx, cfg):
    port = ctx.port = PORTS.next()
    host = pr.start_guest_compose(ctx, cfg, port, "guest_compose", 24)
    time.sleep(0.5)
    ctx.wait_for(lambda: "listening" in host.text() or "waiting" in host.text() or not host.alive() or time.time() - host.proc.started > 6, 8)
    guest = pr.start_bridge_guest(ctx, cfg, port, "bridge-guest", 16)
    # a picture of the Bevy window once frames are flowing
    ctx.wait_for(lambda: "guest connected" in host.text() and guest.text().count("cameras") > 6, 25)
    ctx.pump(3.0)
    ctx.screenshot("bevy-host+bridge-guest", [host.proc])
    prc = _peer_exit(ctx, guest, "bridge-guest", 40)
    hrc = ctx.wait_exit(host.proc, 40)
    if hrc is None:
        ctx.check("guest_compose: ended by itself (--seconds)", False, "killed")
        host.proc.terminate(4)
    b = pr.parse_bridge_guest(guest)
    g = pr.parse_guest_compose(host)
    ctx.evid("bridge-guest: %s" % b, 500)
    ctx.evid("guest_compose: %s" % g, 500)
    ctx.check("bridge-guest: exit 0", prc == 0, "exit %s" % prc)
    ctx.check("guest_compose: exit 0", hrc == 0, "exit %s" % hrc)
    ctx.check("bridge-guest: connected to guest_compose, protocol v1", b.get("protocol") == 1, "%s v%s" % (b.get("host_game"), b.get("protocol")))
    ctx.check("bridge-guest: cameras answered with frames (>= 300 each)", b.get("cameras", 0) >= 300 and b.get("frames", 0) >= 300, "%s cameras, %s frames" % (b.get("cameras"), b.get("frames")))
    ctx.check("guest_compose: the guest's frames were shown (>= 300)", g["frames_shown"] >= 300, "%s shown, %s missed" % (g["frames_shown"], g["frames_missed"]))
    ctx.check("guest_compose: every frame's camera was still in the ring", g["seconds_with_guest"] > 0 and g["ring_hits"] == g["seconds_with_guest"], "%s of %s seconds" % (g["ring_hits"], g["seconds_with_guest"]))
    ctx.check("guest_compose: the guest said bye", any("bye" in x for x in g["guest_gone"]), str(g["guest_gone"]))
    for k in ("cameras", "frames", "skipped", "fps", "latency_mean_ms", "latency_p95_ms"):
        if k in b:
            ctx.rates["bridge-guest." + k] = b[k]
    ctx.rates["guest_compose.frames_shown"] = g["frames_shown"]
    ctx.rates["guest_compose.cameras_sent"] = g["cameras_sent"]
    ctx.rates["guest_compose.frames_missed"] = g["frames_missed"]
    ctx.rates["guest_compose.max_frame_age_ms"] = g["max_frame_age_ms"]
    ctx.exact["guest_compose.guest_gone"] = g["guest_gone"]
    _shot_check(ctx)


# --------------------------------------------------------------------------------------------- MC with MC

MC_MC_MIN_CAMERAS_PER_S = 5.0


def sc_token_mc_mc(ctx, cfg, host_b, guest_b):
    sc_mc_mc(ctx, cfg, host_b, guest_b, token=True)


def sc_mc_mc(ctx, cfg, host_b, guest_b, token=False):
    port = ctx.port = PORTS.next()
    (hv, hl), (gv, gl) = host_b, guest_b
    hfam, hgy = _mc_spec(cfg, hv)
    gfam, ggy = _mc_spec(cfg, gv)
    hn, gn = "%s-%s-host" % (hv, hl), "%s-%s-guest" % (gv, gl)
    # With a token both clients' configs name the same file (their scratch homes differ, so the default place would not be shared).
    efile = _endpoint_dir(ctx, "mc-mc") / "minecraft-java.json" if token else None
    watch = TokenWatch()
    seen = {}
    host = mcmod.McClient(ctx, cfg, hv, hl, mcmod.pair_host_script(hv, hfam, hgy, port, hn), hn, endpoint_file=efile)
    guest = mcmod.McClient(ctx, cfg, gv, gl, mcmod.pair_guest_script(gv, gfam, ggy, port, gn), gn, endpoint_file=efile)

    def shot(arg, proc):
        if token and "info" not in seen:
            seen["info"] = watch.grab(efile)
        ctx.screenshot("host-" + host.label, [host.proc])
        ctx.screenshot("guest-" + guest.label, [guest.proc])
    ctx.handlers["SHOT"] = shot
    host.prepare()
    guest.prepare()
    host.launch()
    guest.launch()
    hrc = ctx.wait_exit(host.proc, 480)
    grc = ctx.wait_exit(guest.proc, 120) if host.proc.poll() is not None else ctx.wait_exit(guest.proc, 480)
    for m, rc in ((host, hrc), (guest, grc)):
        if rc is None:
            ctx.check("%s: finished in time" % m.label, False, "timed out; killed")
            m.proc.terminate(4)
    hrc, grc = host.proc.p.returncode, guest.proc.p.returncode
    # host side
    ctx.check("host: connected to a guest, protocol v1", any(c[1] == "guest" and c[2] == 1 and c[0].startswith("Minecraft") for c in host.connected), str(host.connected))
    for s in host.evidence_statuses():
        ctx.evid("host status: " + s, 600)
    for s in guest.evidence_statuses():
        ctx.evid("guest status: " + s, 600)
    ctx.check("host: one guest entity shown", host.peak("guest_entities") == 1, str(host.peak("guest_entities")))
    ctx.check("host: poses received (>= 100)", (host.peak("poses_received") or 0) >= 100, str(host.peak("poses_received")))
    ctx.check("host: no malformed, no foreign datagrams while connected", *no_anomalies(host))
    ctx.check("host: the guest ended the session with a bye (`peer said bye`)", bool(host.bye), "; ".join(host.bye[:2] + host.lost[:2]) or "no bye, no loss seen")
    # guest side
    ctx.check("guest: connected to the host, protocol v1", any(c[1] == "host" and c[2] == 1 and c[0].startswith("Minecraft") for c in guest.connected), str(guest.connected))
    ctx.check("guest: cameras received and poses sent (>= 100 each)", (guest.peak("poses_sent") or 0) >= 100 and (guest.peak("cameras_received") or 0) >= 100, "%s poses, %s cameras" % (guest.peak("poses_sent"), guest.peak("cameras_received")))
    ctx.check("guest: no malformed, no foreign datagrams while connected", *no_anomalies(guest))
    ctx.check("guest: closed the link with bye", bool(guest.closed), str(guest.closed))
    ctx.exact["host.connected"] = sorted(set(c[0] for c in host.connected))
    ctx.exact["guest.connected"] = sorted(set(c[0] for c in guest.connected))
    ctx.exact["host.guest_entities_peak"] = host.peak("guest_entities")
    for k in ("malformed", "stale", "foreign"):
        ctx.exact["host.%s_max" % k] = host.peak_live(k)
        ctx.exact["guest.%s_max" % k] = guest.peak_live(k)
    ctx.exact["host.spawned"] = sorted(set(x[1] for x in host.spawned))
    # Two game windows compete for the CPU and the GPU, so the camera count of this pair is not repeatable (906 to 2,249 in runs with identical code):
    # its judge is an absolute floor on the sustained rate, not a comparison with the previous run (see report.NO_DROP_ALERT).
    for who, m, key in (("host", host, "cameras_sent"), ("guest", guest, "cameras_received")):
        r = m.rate_live(key)
        ctx.rates["%s.cameras_per_s" % who] = round(r, 1) if r is not None else 0
        ctx.check("%s: cameras %s at a sustained >= %s/s while connected" % (who, "sent" if who == "host" else "received", MC_MC_MIN_CAMERAS_PER_S), r is not None and r >= MC_MC_MIN_CAMERAS_PER_S,
                  "no rate (fewer than two connected status lines)" if r is None else "%.1f/s (floor %s/s)" % (r, MC_MC_MIN_CAMERAS_PER_S))
    ctx.rates["host.poses_received"] = host.peak("poses_received") or 0
    ctx.rates["host.cameras_sent"] = host.peak("cameras_sent") or 0
    ctx.rates["guest.poses_sent"] = guest.peak("poses_sent") or 0
    ctx.rates["guest.cameras_received"] = guest.peak("cameras_received") or 0
    if token:
        _endpoint_checks(ctx, "mc-mc host", seen.get("info"), port, efile)
        ctx.check("host and guest: /link status says token auth on", all(s.get("token_auth") for m in (host, guest) for s in m.statuses if s.get("connected")), "")
        ctx.check("host and guest: nobody was refused", not host.unauthorized_notices and not guest.unauthorized_notices, str((host.unauthorized_notices + guest.unauthorized_notices)[:1]))
        watch.scan(ctx, [host, guest])
    _mc_common(ctx, host, hrc, "host")
    _mc_common(ctx, guest, grc, "guest")
    _shot_check(ctx, expected=2)
