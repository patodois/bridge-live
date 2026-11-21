"""One pair's run context: processes, markers from the games' logs, RAM samples, screenshots, checks."""

import os
import queue
import re
import subprocess
import time
from pathlib import Path

import common
import gameenv
from common import Proc, ROOT

MARKER = re.compile(r"autotest: (START_PEER|SHOT|HOSTING|GUEST_UP|END_SESSION)\b ?(.*)$")
ANSI = re.compile(r"\x1b\[[0-9;]*m")


class Abort(Exception):
    """Raised by pump() when the resource limits are broken: the pair is recorded as aborted."""


class Ctx:
    def __init__(self, cfg, pair_id, logdir, shotdir):
        self.cfg = cfg
        self.id = pair_id
        self.logdir = Path(logdir)
        self.logdir.mkdir(parents=True, exist_ok=True)
        self.shotdir = Path(shotdir)
        self.shotdir.mkdir(parents=True, exist_ok=True)
        self.events = queue.Queue()
        self.procs = []
        self.checks = []
        self.exact = {}
        self.rates = {}
        self.notes = []
        self.evidence = []
        self.shots = []
        self.markers = []
        self.handlers = {}  # marker name -> fn(arg, proc)
        self.line_hooks = []  # fn(proc, line)
        self.t0 = time.time()
        self.peak_rss_kb = 0
        self.peak_game_kb = 0
        self._last_sample = 0.0
        self._last_res = 0.0
        self._low = 0
        self.gui_pids = set()  # trees whose windows are the games' windows
        self.all_started_pids = set()
        self.finalizers = []
        self.scratches = []   # gameenv.Scratch of every game this pair started; removed in finish()
        self.jfr = {}         # client label -> what the JFR summary found (or why there is none)
        self.sandbox = None   # (ok, detail) of the write-deny sandbox, once a game was started

    # ------------------------------------------------------------------ checks and records
    def check(self, name, ok, detail="", gate=True):
        self.checks.append({"name": name, "ok": bool(ok), "detail": str(detail)[:600], "gate": gate})
        return ok

    def note(self, text):
        self.notes.append(str(text)[:400])

    def evid(self, text, limit=300):
        if len(self.evidence) < 80:
            self.evidence.append(str(text)[:limit])

    # ------------------------------------------------------------------ processes
    def new_scratch(self, label, minecraft_assets=False):
        """Scratch folders for one game of this pair: work/scratch/<run>/<pair>/<label>/ (see gameenv). `minecraft_assets`: link the
        vanilla assets folder of the scratch home to bridge-live's persistent asset cache (seeded only from a folder the caller named,
        see gameenv.assets_seed)."""
        root = gameenv.SCRATCH_ROOT / self.logdir.parent.name / self.logdir.name / re.sub(r"[^A-Za-z0-9._-]+", "_", label)
        sc = gameenv.Scratch(root)
        self.scratches.append(sc)
        if minecraft_assets:
            sc.link_minecraft_assets(gameenv.assets_seed(self.cfg))
        return sc

    def start_game(self, name, argv, scratch, cwd=None, env=None, stdin_pipe=False, gui=False, read_only=(), deny_write=(), allow_write=()):
        """THE way a game is started (a Minecraft dev client, the Hytale server). Nothing else may start one: tests/test_game_home.py
        fails for code that does.

        The game gets the scratch environment of gameenv.game_env (home, XDG and temp folders under work/scratch, no sound),
        is refused when its working directory, arguments or environment name a real launcher profile (except `read_only` paths:
        installed game files that are only read), and runs under a sandbox that denies every write below the real profile
        folders (and `deny_write`, minus the exact paths in `allow_write`). `env` is added on top, `scratch` is the game's Scratch from `new_scratch`.
        Without a working sandbox the game is not started: the pair ends as an error (it fails closed)."""
        full_env = gameenv.game_env(scratch, **(env or {}))
        gameenv.assert_clear_of_profiles(argv, cwd, full_env, read_only=read_only, cfg=self.cfg)
        ok, detail = gameenv.sandbox_status(scratch)
        self.sandbox = (ok, detail)
        if not ok:
            self.note("no write-deny sandbox for %s (%s): the game was not started" % (name, detail))
            raise RuntimeError("refusing to start %s without the write-deny sandbox: %s" % (name, detail))
        launch = gameenv.sandboxed_argv(argv, list(gameenv.real_profile_dirs(self.cfg)) + [os.path.realpath(str(d)) for d in deny_write], [os.path.realpath(str(d)) + ("/" if str(d).endswith("/") else "") for d in allow_write])
        p = self._spawn(name, launch, cwd, full_env, stdin_pipe, gui)
        p.game = True
        return p

    def start_tool(self, name, argv, cwd=None, env=None, stdin_pipe=False, gui=False):
        """A helper program that is not a game (the scripted peers, bridge-guest, LinkDemo, the Bevy host). It must not launch a
        game or read a launcher profile that is not named in config.json; tests/test_game_home.py checks the callers."""
        p = self._spawn(name, argv, cwd, dict(os.environ, **(env or {})), stdin_pipe, gui)
        p.game = False
        return p

    def _spawn(self, name, argv, cwd, env, stdin_pipe, gui):
        p = Proc(name, argv, cwd or str(ROOT), env, self.logdir / (name + ".log"), events=self.events, stdin_pipe=stdin_pipe)
        p.gui = gui
        self.procs.append(p)
        self.all_started_pids.add(p.pid)
        return p

    def pump(self, timeout=0.25):
        """Handle queued log lines (markers, hooks), sample RAM once a second, watch the limits."""
        end = time.time() + timeout
        while True:
            left = end - time.time()
            try:
                proc, line = self.events.get(timeout=max(0.0, left)) if left > 0 else self.events.get_nowait()
            except queue.Empty:
                break
            for hook in self.line_hooks:
                hook(proc, line)
            # Markers come from the AutoTest script of a Minecraft client; a peer or tool that prints the same text cannot drive the pair.
            m = MARKER.search(line) if getattr(proc, "game", False) else None
            if m:
                name, arg = m.group(1), m.group(2).strip()
                self.markers.append((round(time.time() - self.t0, 2), name, arg, proc.name))
                h = self.handlers.get(name)
                if h:
                    h(arg, proc)
        now = time.time()
        if now - self._last_sample >= 1.0:
            self._last_sample = now
            self._sample()
        if now - self._last_res >= 2.0:
            self._last_res = now
            self._watchdog()

    def _sample(self):
        table = common.ps_table()
        rss = {r[0]: r[3] for r in table}
        total = 0
        biggest = 0
        for p in self.procs:
            for pid in p.tree(table):
                self.all_started_pids.add(pid)
                total += rss.get(pid, 0)
                biggest = max(biggest, rss.get(pid, 0))
        self.peak_rss_kb = max(self.peak_rss_kb, total)
        self.peak_game_kb = max(self.peak_game_kb, biggest)

    def _watchdog(self):
        lim = self.cfg["limits"]
        if common.disk_free_gb() < lim["min_free_disk_gb"]:
            raise Abort("free disk %.1f GB fell below %d GB" % (common.disk_free_gb(), lim["min_free_disk_gb"]))
        free = common.mem_free_percent()
        if free is not None and free < lim["min_free_ram_percent"]:
            self._low += 1
            if self._low >= 3:
                raise Abort("free RAM %d%% stayed below %d%% for 3 samples" % (free, lim["min_free_ram_percent"]))
        else:
            self._low = 0

    def wait_for(self, pred, timeout, tick=0.25):
        end = time.time() + timeout
        while time.time() < end:
            self.pump(tick)
            if pred():
                return True
        return pred()

    def wait_exit(self, proc, timeout):
        end = time.time() + timeout
        while time.time() < end:
            self.pump(0.25)
            if proc.poll() is not None:
                proc.wait(2)
                self.pump(0.1)
                return proc.p.returncode
        return None

    # ------------------------------------------------------------------ screenshots
    def screenshot(self, label, procs=None):
        """screencapture of the largest window of the given (or all gui) processes. Returns the record."""
        rec = {"label": label, "ok": False, "path": None, "detail": ""}
        pids = set()
        for p in (procs or [x for x in self.procs if getattr(x, "gui", False)]):
            pids |= p.tree()
        why = common.screen_unavailable()
        if why:
            rec["detail"] = "not captured: " + why
            self.shots.append(rec)
            return rec
        st = common.screen_state(pids)
        wins = [w for w in st.get("windows", []) if w.get("layer") == 0 and w.get("w", 0) > 200 and w.get("h", 0) > 150]
        if not wins:
            rec["detail"] = "no on-screen window of the game found (%d candidate pids)" % len(pids)
            self.shots.append(rec)
            return rec
        w = max(wins, key=lambda x: x["w"] * x["h"])
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", label)
        path = str(self.shotdir / (safe + ".png"))
        r = subprocess.run(["screencapture", "-x", "-o", "-l%d" % w["id"], path], capture_output=True, text=True, timeout=30)
        if r.returncode != 0 or not os.path.exists(path):
            rec["detail"] = "screencapture failed: rc=%s %s" % (r.returncode, r.stderr.strip()[:200])
            self.shots.append(rec)
            return rec
        rec["path"] = os.path.relpath(path, str(ROOT))   # reports name files relative to this repository
        rec["window"] = {"title": w.get("title", ""), "w": w["w"], "h": w["h"], "owner": w.get("owner", "")}
        try:
            pw, ph, mean, sd, ncol = common.png_stats(path)
            rec.update({"pixels": [pw, ph], "mean_luma": round(mean, 1), "stdev_luma": round(sd, 1), "colours": ncol})
            if sd < 6 or ncol < 20:
                rec["detail"] = "the capture looks blank (stdev %.1f, %d colours): screen-recording permission missing, or the window had not drawn" % (sd, ncol)
            else:
                rec["ok"] = True
        except Exception as e:
            rec["detail"] = "cannot analyse the PNG: %s" % e
        self.shots.append(rec)
        return rec

    # ------------------------------------------------------------------ end
    def finish(self, grace=5.0):
        """End everything this pair started, by PID; returns the pids that had to be killed."""
        killed = []
        for p in self.procs:
            if p.poll() is None:
                killed.append(p.name)
            p.terminate(grace)
        for fn in self.finalizers:
            try:
                fn()
            except Exception as e:
                self.note("finalizer failed: %s" % e)
        for sc in self.scratches:
            try:
                sc.remove()
            except Exception as e:
                self.note("could not remove the scratch folder %s: %s" % (sc.root.name, e))
        self.scratches = []
        # What is left in the sessions this pair created? (pgid == the root pid of a Proc started with its own session;
        # matching on that, never on a remembered pid alone, keeps a recycled pid from being killed by mistake.)
        time.sleep(0.3)
        roots = {p.pid for p in self.procs}
        left = sorted(r[0] for r in common.ps_table() if r[2] in roots)
        if left:
            common.kill_pids(left, 3.0)
        roots = {p.pid for p in self.procs}
        left2 = sorted(r[0] for r in common.ps_table() if r[2] in roots)
        for p in self.procs:
            p.close()
        return killed, left2
