"""Minecraft dev clients driven by minecraft-link's AutoTest, one `McClient` per game window.

A client is one `./gradlew <runClient task> --no-daemon -I <init script> -Pautotest=<script> -Pshots=<dir>` in the worktree of its
Minecraft version, started through `Ctx.start_game` (scratch home, no writes to real launcher profiles, see gameenv.py).
Where config.json says `"instance": "scratch"` the game directory is a scratch folder too; otherwise it is the checkout's
own gitignored dev `run/` folder. Before it starts, the sound is switched off in the game directory's options.txt (master
and every category, and the 1.7.10 volumes) and read back; after it ends the game's own rewrite of the file is read back too.
The scripts below only use AutoTest commands; markers (`log START_PEER <port>`, `log SHOT <name>` ...) are lines the
orchestrator reacts to.
"""

import json
import os
import re
import time
from pathlib import Path

import common
import gameenv
import jfr as jfrmod
from common import expand

TPS = 20

# ---------------------------------------------------------------------------------------------- status parsing

def session_end_kind(line):
    """How a mod log line ends a session: 'bye' when the peer said bye (`link lost: peer said bye: <reason>`), 'lost' for any other
    loss (a timeout, a reset connection: `link lost: <reason>`, `LOST`), None otherwise. A loss is never a bye."""
    if "peer said bye" in line:
        return "bye"
    if "LOST" in line or "link lost" in line.lower():
        return "lost"
    return None



def parse_status(text):
    """Numbers out of a `/link status` (or 'link started') line; missing parts are simply absent."""
    d = {"raw": text}
    m = re.match(r"(host|guest) on 127\.0\.0\.1:(\d+)", text)
    if m:
        d["role"], d["port"] = m.group(1), int(m.group(2))
    m = re.search(r"connected to (.+?) \(session ([0-9a-f]+)\)", text)
    d["connected"] = bool(m)
    if m:
        d["peer"], d["session"] = m.group(1), m.group(2)
    m = re.search(r"input focus with the (host|guest)", text)
    if m:
        d["focus"] = m.group(1)
    m = re.search(r"sent (\d+) cameras, (\d+) collision snapshots \((\d+) triangles[^)]*\); received (\d+) poses, (\d+) frames; (\d+) guest entities", text)
    if m:
        d.update(cameras_sent=int(m.group(1)), collision_snapshots=int(m.group(2)), collision_triangles=int(m.group(3)),
                 poses_received=int(m.group(4)), frames_received=int(m.group(5)), guest_entities=int(m.group(6)))
    m = re.search(r"sent (\d+) poses; received (\d+) cameras", text)
    if m:
        d.update(poses_sent=int(m.group(1)), cameras_received=int(m.group(2)))
    m = re.search(r"token auth on", text)
    d["token_auth"] = bool(m)
    m = re.search(r"; (\d+) refused as unauthorized", text)
    d["refused_unauthorized"] = int(m.group(1)) if m else 0
    m = re.search(r"datagrams in/out (\d+)/(\d+), malformed (\d+), stale (\d+), foreign (\d+)", text)
    if m:
        d.update(dg_in=int(m.group(1)), dg_out=int(m.group(2)), malformed=int(m.group(3)), stale=int(m.group(4)), foreign=int(m.group(5)))
    m = re.search(r"frames drawn (\d+), reprojected (\d+)", text)
    if m:
        d.update(frames_drawn=int(m.group(1)), reprojected=int(m.group(2)))
    m = re.search(r"pixel file (\d+)x(\d+) with depth", text)
    if m:
        d["pixel_file"] = "%sx%s depth" % (m.group(1), m.group(2))
    m = re.search(r"camera depth_range rewritten to (-?\d+)|camera depth_range (-?\d+)", text)
    if m:
        d["camera_depth_range"] = int(m.group(1) or m.group(2))
    return d


# ---------------------------------------------------------------------------------------------- options.txt

def _sound_keys(spec):
    return ["soundCategory_" + c for c in spec.get("sound", [])] + list(spec.get("sound_keys", []))


_SOUNDLIKE = re.compile(r"^(soundCategory_[A-Za-z_]+|soundVolume|musicVolume):(.*)$")


def write_options(run_dir, spec):
    """Sound off (master and every category), no pause on lost focus, the version's extra keys (`options` in config.json: the
    accessibility onboarding of a first start, the tutorial hints, as the checkout's own prepareRunOptions task writes them
    into its run folder); other lines are kept. Atomic."""
    extra = spec.get("options", {})
    path = Path(run_dir) / "options.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    keep = []
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if _SOUNDLIKE.match(line) or line.startswith("pauseOnLostFocus:") or line.startswith("fullscreen:") or line.split(":", 1)[0] in extra:
                continue
            keep.append(line)
    keep += ["%s:0.0" % k for k in _sound_keys(spec)]
    keep += ["pauseOnLostFocus:false", "fullscreen:false"]
    keep += ["%s:%s" % kv for kv in extra.items()]
    tmp = path.with_name("options.txt.bridge-live-tmp")
    tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def verify_sound_off(run_dir, spec):
    """(ok, detail): every expected key is present and 0.0, and no sound-like key is non-zero."""
    path = Path(run_dir) / "options.txt"
    if not path.exists():
        return False, "options.txt missing"
    seen = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _SOUNDLIKE.match(line)
        if m:
            seen[line.split(":", 1)[0]] = m.group(2).strip()
    want = _sound_keys(spec)
    missing = [k for k in want if k not in seen]
    loud = {k: v for k, v in seen.items() if not _zero(v)}
    if missing or loud:
        return False, "missing %s; non-zero %s" % (missing, loud)
    return True, "%d volume keys, all 0.0" % len(seen)


def _zero(v):
    try:
        return float(v) == 0.0
    except ValueError:
        return False


# ---------------------------------------------------------------------------------------------- the mod's config (token)

def set_token_config(run_dir, endpoint_file):
    """Switch the link token (contract section 6.2) of the mod in this game directory: `config/minecraft-link.json` gets
    `token_auth: true` and `endpoint_file` = the given absolute path, or, with `endpoint_file` None, `token_auth: false` and an empty path
    (only when the file already exists: a plain run changes nothing in a game directory that has no config). Other keys are kept. Atomic.
    The token itself is never in this file: the mod does not hold it, link-java does."""
    path = Path(run_dir) / "config" / "minecraft-link.json"
    cfg = {}
    if path.exists():
        # A file this tool cannot read is somebody's settings: refuse (the pair ends as an error) rather than replace it with the token keys.
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ValueError("%s cannot be read as JSON (%s); not overwriting it" % (path, e))
        if not isinstance(cfg, dict):
            raise ValueError("%s holds no JSON object; not overwriting it" % path)
    elif endpoint_file is None:
        return None
    cfg["token_auth"] = endpoint_file is not None
    cfg["endpoint_file"] = str(endpoint_file) if endpoint_file is not None else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".bridge-live-tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------------------------- scripts

def _tp(family, gy, x, z):
    sel = "@s" if family == "modern" else "@p"
    return "cmd /tp %s %s %s %s" % (sel, x, gy, z)


def _hud(family, on):
    if family == "modern":
        return "hud on" if on else "hud off"
    return "gui 1" if on else "gui 0"


def _wall(version, family, gy, place):
    """Stone wall two blocks in front of the player's line of sight (hides the right half of a cube 4 m ahead)."""
    block = "minecraft:stone" if place else "minecraft:air"
    if version == "1.7.10":
        cmds = []
        for x in (0, 1, 2):
            for y in (gy, gy + 1):
                cmds.append("cmd /setblock %d %d 5 %s" % (x, y, "stone" if place else "air"))
        return cmds
    return ["cmd /fill 0 %d 5 2 %d 5 %s" % (gy, gy + 1, block)]


def host_script(version, family, gy, port, name, wall=True):
    """MC is the host; a peer (started when `START_PEER` shows up) is the guest and ends the session itself."""
    s = ["# bridge-live: %s (host side)" % name, "create_world", "wait_world", "cmd /gamemode creative", _tp(family, gy, "0.5" if family == "legacy" else "0.0", "7.5"),
         "wait 40", "look 180 0", "wait 5"]
    if wall:
        s += _wall(version, family, gy, True) + ["wait 20"]
    s += ["link host %d" % port, "log START_PEER %d" % port,
          "wait 100",                      # the peer is up and sending
          "link focus guest", "wait 30",
          _hud(family, False), "look 180 0", "wait 5", "log SHOT %s" % name, "wait 40",
          "link status", _hud(family, True), "wait 20",
          "link focus host", "wait 40",
          "link status",                   # a second look, still inside the session (the peer ends it 13 s after it connects)
          "wait 120",                      # the peer ends its session (despawn, bye) in this stretch
          "log END_SESSION", "link status", "link stop", "wait 60", "quit"]
    return "\n".join(s) + "\n"


def guest_script(version, family, gy, port, name):
    """MC is the guest; a host peer is started at `START_PEER` and answers the focus request; MC says bye itself."""
    s = ["# bridge-live: %s (guest side)" % name, "create_world", "wait_world", "cmd /gamemode creative", _tp(family, gy, "0.5" if family == "legacy" else "0.0", "0.5"),
         "wait 40", "look 0 0", "wait 5",
         "link guest %d" % port, "log START_PEER %d" % port,
         "wait 80",                         # connected; the host holds the input focus
         "pos", "key forward down", "wait 30",
         "pos",
         "link focus guest",                # the host grants it for a short while: the player walks, then stops again
         "wait 100", "pos", "key forward up",
         _hud(family, False), "log SHOT %s" % name, "wait 40",
         "link status", _hud(family, True), "wait 20",
         "link stop", "wait 80", "log END_SESSION", "quit"]
    return "\n".join(s) + "\n"


def host_refused_script(version, family, gy, port, name):
    """MC is the host with a token; a guest peer (started at `START_PEER`) shows a wrong one and must be refused. MC reports it twice."""
    s = ["# bridge-live: %s (host side, refused guest)" % name, "create_world", "wait_world", "cmd /gamemode creative", _tp(family, gy, "0.5" if family == "legacy" else "0.0", "7.5"),
         "wait 40", "look 180 0", "wait 5",
         "link host %d" % port, "log START_PEER %d" % port,
         "wait 160",                        # the peer is refused (it ends by itself), the host keeps listening
         "link status", "wait 20", "link status",
         "log END_SESSION", "link stop", "wait 60", "quit"]
    return "\n".join(s) + "\n"


def guest_refused_script(version, family, gy, port, name):
    """MC is the guest with a wrong token; a host peer with a token is started at `START_PEER` and refuses it."""
    s = ["# bridge-live: %s (guest side, refused)" % name, "create_world", "wait_world", "cmd /gamemode creative", _tp(family, gy, "0.5" if family == "legacy" else "0.0", "0.5"),
         "wait 40", "look 0 0", "wait 5",
         "link guest %d" % port, "log START_PEER %d" % port,
         "wait 160",                        # the host peer is up and has refused it (the guest keeps trying, nothing connects)
         "link status", "wait 20", "link status",
         "link stop", "wait 80", "log END_SESSION", "quit"]
    return "\n".join(s) + "\n"


def pair_host_script(version, family, gy, port, name, hold_status=14):
    """MC host of an MC-MC pair: waits for the other client (no peer to start), reporting every 5 s."""
    s = ["# bridge-live: %s (host side)" % name, "create_world", "wait_world", "cmd /gamemode creative", _tp(family, gy, "0.0", "7.5"),
         "wait 40", "look 180 0", "wait 5", "link host %d" % port, "log HOSTING %d" % port]
    for _ in range(hold_status):
        s += ["wait 100", "link status"]
    s += ["log END_SESSION", "link stop", "wait 60", "quit"]
    return "\n".join(s) + "\n"


def pair_guest_script(version, family, gy, port, name):
    """MC guest of an MC-MC pair: joins when its world is up, walks when granted, leaves with `link stop`."""
    s = ["# bridge-live: %s (guest side)" % name, "create_world", "wait_world", "cmd /gamemode creative", _tp(family, gy, "0.0", "0.5"),
         "wait 40", "look 0 0", "wait 5", "link guest %d" % port, "log GUEST_UP %d" % port,
         "wait 100",                        # connected (the host may still be starting: the guest retries)
         "link status", "wait 100", "link focus guest", "wait 60", "link focus host", "wait 60",
         "log SHOT %s" % name, "wait 40", "link status", "wait 100", "link status",
         "link stop", "wait 80", "log END_SESSION", "quit"]
    return "\n".join(s) + "\n"


# ---------------------------------------------------------------------------------------------- OpenAL (second sound guard)

def read_openal_log(path):
    """What OpenAL Soft logged (ALSOFT_LOGFILE, level 3) about the backend it opened: {'seen': bool, 'backend': name or None, 'null': bool, 'lines': [...]}.
    The game is started with ALSOFT_DRIVERS=null, so the only backend that may be initialised is `null`; anything else is a failure."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"seen": False, "backend": None, "null": False, "other": [], "lines": []}
    lines = [l.strip()[:200] for l in text.splitlines() if re.search(r"backend|Opened|device", l, re.I)]
    inited = re.findall(r'Initialized backend "([^"]+)"', text)
    other = [b for b in inited if b != "null"]
    return {"seen": bool(inited), "backend": inited[-1] if inited else None, "null": bool(inited) and not other, "other": other[:3], "lines": lines[:6]}


# ---------------------------------------------------------------------------------------------- the client

class McClient:
    def __init__(self, ctx, cfg, version, loader, script_text, label, endpoint_file=None):
        self.ctx = ctx
        self.cfg = cfg
        self.version = version
        self.loader = loader
        self.spec = cfg["mc"][version]
        self.lspec = self.spec["loaders"][loader]
        self.dir = expand(self.spec["dir"])
        self.scratch = ctx.new_scratch(label, minecraft_assets=True)
        self.checkout_run_dir = os.path.join(self.dir, self.lspec["run_dir"])
        # The game directory: a scratch instance where the build lets us move it, else the checkout's own dev run folder.
        self.scratch_instance = self.lspec.get("instance") == "scratch"
        self.run_dir = str(self.scratch.instance) if self.scratch_instance else self.checkout_run_dir
        self.label = label
        self.script_text = script_text
        self.endpoint_file = endpoint_file   # the token mode: the endpoint file this client's config names (None = no token)
        self.unauthorized_notices = []       # the mod's "refused ... as unauthorized" lines
        self.script_path = str(ctx.logdir / ("%s.autotest.txt" % label))
        self.proc = None
        self.statuses = []     # parsed `link status` results, in order (also the 'link started' line)
        self.status_lines = []
        self.connected = []    # (peer game, role, protocol)
        self.spawned = []      # 'guest spawned entity 1 (Provider asset)'
        self.bye = []          # the peer said bye ('link lost: peer said bye: ...')
        self.lost = []         # the session was lost another way (timeout, reset)
        self.closed = []       # this client closed the link itself ('link closed (...)')
        self.errors = []
        self.focus_events = []
        self.pos_lines = []
        self.options_pre = None
        self.options_post = None
        self.saves_before = set()
        self.crash_before = set()
        self.gradle_ok = None
        self.exit_code = None
        self.jfr_plan = jfrmod.plan(cfg, version, cfg.get("run", {}).get("jfr", False))
        self.jfr_path = None   # set by launch() when JFR records
        self.jfr_result = None
        self.openal = None
        self.init_lines = []

    # --------------------------------------------------------------- before
    def prepare(self):
        write_options(self.run_dir, self.spec)
        set_token_config(self.run_dir, self.endpoint_file)
        self.options_pre = verify_sound_off(self.run_dir, self.spec)
        saves = Path(self.run_dir) / "saves"
        self.saves_before = set(os.listdir(saves)) if saves.exists() else set()
        crash = Path(self.run_dir) / "crash-reports"
        self.crash_before = set(os.listdir(crash)) if crash.exists() else set()
        Path(self.script_path).write_text(self.script_text)
        self._fin = None
        self.ctx.finalizers.append(self.finish)  # also runs when the pair is interrupted, times out or raises

    def launch(self):
        env = {"JAVA_HOME": expand(self.spec.get("java_home", self.cfg["java_home"]))}
        shots = str(self.ctx.logdir / ("%s-ingame" % self.label))
        jvm = []
        if self.jfr_plan["active"]:
            self.jfr_path = jfrmod.recording_path(self.scratch, self.label)
            jvm = jfrmod.jvm_args(self.cfg, self.jfr_path)
        init = gameenv.write_init_script(self.scratch, jvm_args=jvm, user_home=self.spec.get("user_home", True),
                                         instance=str(self.scratch.instance) if self.scratch_instance else None)
        argv = ["./gradlew", self.lspec["task"], "--no-daemon", "--offline", "-I", init, "-Pautotest=" + self.script_path, "-Pshots=" + shots]
        # With a scratch instance nothing may be written into the checkout's own run folder (its dev worlds, logs, crash reports): the
        # sandbox makes that a fact. The one exception is options.txt: the checkout's prepareRunOptions task writes its sound-off lines
        # there before every runClient, that is its job (and the project's own rule that test clients are silent).
        deny, allow = [], []
        if self.scratch_instance:
            deny = [self.checkout_run_dir]
            # The Forge and NeoForge run tasks keep the checkout's run/ as the process directory (Gradle ignores the redirect there) and log4j opens
            # the relative logs/latest.log and logs/debug.log in it: those log folders may be written, worlds and everything else may not.
            allow = [self.checkout_run_dir, os.path.join(self.checkout_run_dir, "options.txt"), os.path.join(self.checkout_run_dir, "logs") + "/"]
        self.proc = self.ctx.start_game("mc-" + self.label, argv, self.scratch, cwd=self.dir, env=env, gui=True, deny_write=deny, allow_write=allow)
        self.ctx.line_hooks.append(self._hook)
        return self.proc

    # --------------------------------------------------------------- log lines
    def _hook(self, proc, line):
        if proc is not self.proc:
            return
        if "bridge-live-init:" in line:
            self.init_lines.append(line.split("bridge-live-init:", 1)[1].strip()[:300])
            return
        if "(minecraft-link" not in line and "[minecraft-link" not in line and "[link]" not in line:
            if "BUILD SUCCESSFUL" in line:
                self.gradle_ok = True
            elif "BUILD FAILED" in line:
                self.gradle_ok = False
            elif re.search(r"/ERROR\]|Exception in thread|Crash report saved|#@!@#", line):
                self.errors.append(line[:300])
            return
        if "[CHAT]" in line:
            return
        m = re.search(r"autotest: link status -> (.*)$", line)
        if m:
            st = parse_status(m.group(1))
            st["t"] = round(time.time() - self.ctx.t0, 1)
            self.statuses.append(st)
            self.status_lines.append(m.group(1))
            return
        m = re.search(r"connected to (.+?) \((guest|host), protocol v(\d+)\)", line)
        if m:
            self.connected.append((m.group(1), m.group(2), int(m.group(3))))
            return
        if re.search(r"refused (?:a guest )?as unauthorized|refused this guest as unauthorized", line):
            self.unauthorized_notices.append(line[-220:])
            return
        m = re.search(r"guest spawned entity (\d+) \((.*)\)", line)
        if m:
            self.spawned.append((int(m.group(1)), m.group(2)))
            return
        end = session_end_kind(line)
        if end:
            (self.bye if end == "bye" else self.lost).append(line[-160:])
            return
        m = re.search(r"link closed \((.*)\)", line)
        if m:
            self.closed.append(m.group(1))
            return
        m = re.search(r"input focus is now with the (host|guest)", line)
        if m:
            self.focus_events.append(m.group(1))
            return
        m = re.search(r"autotest: player (?:at|feet at) ([-\d.eE]+) ([-\d.eE]+) ([-\d.eE]+)", line)
        if m:
            self.pos_lines.append((float(m.group(1)), float(m.group(2)), float(m.group(3))))
            return
        if re.search(r"/ERROR\]|ERROR\]", line):
            self.errors.append(line[:300])

    # --------------------------------------------------------------- after
    def finish(self):
        """Read the options back, count (checkout run folder: delete) the worlds this run created, look for crash reports,
        read OpenAL's log, summarise the JFR recording. Idempotent. Runs before the scratch folder is removed."""
        if self._fin is not None:
            return self._fin
        out = {}
        self.options_post = verify_sound_off(self.run_dir, self.spec)
        saves = Path(self.run_dir) / "saves"
        removed = []
        if saves.exists() and self.version in ("26.3", "1.20.1"):
            for name in sorted(set(os.listdir(saves)) - self.saves_before):
                if not name.startswith("linktest-"):
                    continue
                if self.scratch_instance:
                    removed.append(name)  # goes with the scratch folder; nothing outside it was ever written
                    continue
                try:
                    common.safe_delete(str(saves / name), [str(saves)])
                    removed.append(name)
                except Exception as e:
                    self.ctx.note("could not remove the test world %s: %s" % (name, e))
        out["worlds_removed"] = removed
        crash = Path(self.run_dir) / "crash-reports"
        new_crash = sorted(set(os.listdir(crash)) - self.crash_before) if crash.exists() else []
        out["new_crash_reports"] = new_crash
        if self.scratch_instance and new_crash:
            keep = self.ctx.logdir / ("%s-crash-reports" % self.label)
            keep.mkdir(exist_ok=True)
            for name in new_crash:  # the scratch folder is about to go: keep the reports with the logs
                try:
                    (keep / name).write_bytes((crash / name).read_bytes())
                except OSError as e:
                    self.ctx.note("could not keep crash report %s: %s" % (name, e))
        self.openal = read_openal_log(self.scratch.openal_log)
        self._finish_jfr()
        self._fin = out
        return out

    def _finish_jfr(self):
        """Summarise the recording (when one was asked for), keep the summary next to the logs, drop the raw file."""
        plan = self.jfr_plan
        if not plan["active"]:
            self.jfr_result = {"recorded": False, "reason": plan["reason"]}
            if self.cfg.get("run", {}).get("jfr"):
                self.ctx.jfr[self.label] = self.jfr_result
            return
        if self.jfr_path is None:   # the launch failed before the recording was set up
            self.jfr_result = {"recorded": False, "reason": "the client was not launched"}
            self.ctx.jfr[self.label] = self.jfr_result
            return
        out_json = self.ctx.logdir / ("%s.jfr-summary.json" % self.label)
        summary, detail = jfrmod.summarise(self.cfg, self.jfr_path, str(out_json))
        try:
            jfrmod.remove_raw(self.jfr_path, self.scratch)
        except Exception as e:
            self.ctx.note("could not remove the raw JFR file: %s" % e)
        if summary is None:
            self.jfr_result = {"recorded": False, "reason": detail}
        else:
            self.jfr_result = {"recorded": True, "summary": os.path.relpath(str(out_json), str(common.ROOT)), "headline": jfrmod.headline(summary)}
        self.ctx.jfr[self.label] = self.jfr_result

    # --------------------------------------------------------------- numbers
    def peak(self, key):
        """Highest value of a counter over every status line, including the one after the bye (final totals)."""
        vals = [s[key] for s in self.statuses if key in s]
        return max(vals) if vals else None

    def peak_live(self, key):
        """Highest value over the status lines printed while a session was connected. The anomaly counters are judged on these:
        a heartbeat still in flight when the session ended is legitimately 'foreign' to a link that is no longer in one."""
        vals = [s[key] for s in self.statuses if key in s and s.get("connected")]
        return max(vals) if vals else None

    def rate_live(self, key):
        """Per second, wall clock, over the connected status lines: (last - first) of a counter divided by the time between them.
        None when fewer than two connected statuses carry the counter or no time passed between them."""
        c = [s for s in self.statuses if key in s and s.get("connected")]
        if len(c) < 2 or c[-1]["t"] <= c[0]["t"]:
            return None
        return (c[-1][key] - c[0][key]) / (c[-1]["t"] - c[0]["t"])

    def evidence_statuses(self):
        """The first and the last status line of a connected session, and the very last line (usually after the bye)."""
        conn = [s["raw"] for s in self.statuses if s.get("connected")]
        out = []
        for x in (conn[:1] + conn[-1:] + [self.statuses[-1]["raw"]] if self.statuses else []):
            if x not in out:
                out.append(x)
        return out

    def last_connected_status(self):
        c = [s for s in self.statuses if s.get("connected")]
        return c[-1] if c else None
