"""Shared helpers of bridge-live: config, resource guards, process handling, guarded deletion, screen state.

Everything the harness starts is a `Proc`: a child in its own session whose output is teed to a log file and to an
in-memory list, and whose whole tree can be listed (for the RAM sample) and ended by PID (never by name or pattern).
Nothing here ever runs `rm`: deletions go through `safe_delete`, which only accepts an absolute path strictly inside
an allowed root, without `..` or glob characters and not through a symlink.
"""

import datetime
import json
import os
import pwd
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def expand(p, base=None):
    """~ expanded; a relative path is relative to the repository (or `base`)."""
    p = os.path.expanduser(str(p))
    if not os.path.isabs(p):
        p = os.path.join(str(base or ROOT), p)
    return os.path.normpath(p)


def _merge(base, over):
    """`over` laid over `base`: objects merge key by key, everything else (lists included) is replaced."""
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = _merge(base[k], v) if k in base else v
        return out
    return over


def load_config():
    """config.json, with config.local.json (git-ignored, optional) laid over it: a machine that tests other checkouts than the ones in
    config.json (a worktree per branch, say) keeps its own paths there and the committed file stays as it is."""
    with open(ROOT / "config.json") as f:
        cfg = json.load(f)
    local = ROOT / "config.local.json"
    if local.exists():
        with open(local) as f:
            cfg = _merge(cfg, json.load(f))
    return cfg


# A user's home folder in a path, as tools/JfrSummary.java recognises it: macOS, Linux and Windows forms.
USER_PATH = re.compile(r"(/Users/[^/\s\"']+|/home/[^/\s\"']+|[A-Za-z]:\\Users\\[^\\\s\"']+)")


def scrub_text(text, root=None, homes=None):
    """`text` with this repository's own path made relative (`logs/...`, or `.` for the folder itself) and every user's home folder
    replaced by `~`, so no report or summary names the account. `homes` defaults to the account's home (password database and $HOME)."""
    if homes is None:
        homes = account_homes()
    s = str(text)
    r = str(root or ROOT)
    s = re.sub(re.escape(r) + r"/", "", s)
    s = re.sub(re.escape(r) + r"(?![^/\s\"'\],:;)])", ".", s)
    for h in sorted((h.rstrip("/") for h in homes if h and len(h.rstrip("/")) > 1), key=len, reverse=True):
        s = re.sub(re.escape(h) + r"(?![^/\s\"'\],:;)])", "~", s)
    return USER_PATH.sub("~", s)


def account_homes():
    """The account's home folder as the password database and $HOME name it."""
    return {pwd.getpwuid(os.getuid()).pw_dir, os.environ.get("HOME", "")}


def scrub(obj, root=None, homes=None):
    """A copy of a report structure (dicts, lists, strings) with `scrub_text` applied to every string, keys included."""
    if homes is None:
        homes = account_homes()
    if isinstance(obj, str):
        return scrub_text(obj, root, homes)
    if isinstance(obj, dict):
        return {scrub(k, root, homes) if isinstance(k, str) else k: scrub(v, root, homes) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [scrub(v, root, homes) for v in obj]
    return obj


def utc_stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------------------------- resources

def mem_free_percent():
    """Free memory in percent, the number `memory_pressure` prints (kern.memorystatus_level), or None when it cannot be read
    (unknown is never reported as plenty: the guard does not gate on it and the report shows null)."""
    try:
        out = subprocess.run(["sysctl", "-n", "kern.memorystatus_level"], capture_output=True, text=True, timeout=5).stdout
        return int(out.strip())
    except Exception:
        return None


def disk_free_gb(path=None):
    return shutil.disk_usage(path or os.path.expanduser("~")).free / 1e9


def resource_problem(cfg, samples=3, gap=2.0):
    """None when RAM and disk are fine (or free RAM cannot be measured), else a sentence. RAM must be below the limit for `samples`
    samples in a row."""
    lim = cfg["limits"]
    d = disk_free_gb()
    if d < lim["min_free_disk_gb"]:
        return "free disk %.1f GB is below the %d GB limit" % (d, lim["min_free_disk_gb"])
    low = 0
    last = 100
    for i in range(samples):
        last = mem_free_percent()
        if last is None or last >= lim["min_free_ram_percent"]:
            return None
        low += 1
        if i + 1 < samples:
            time.sleep(gap)
    return "free RAM %d%% stayed below the %d%% limit for %d samples" % (last, lim["min_free_ram_percent"], low)


# --------------------------------------------------------------------------------------------- processes

def ps_table():
    """[(pid, ppid, pgid, rss_kb, command)] for every process."""
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,pgid=,rss=,command="], capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3]), parts[4]))
        except ValueError:
            continue
    return rows


def descendants(table, roots):
    """The roots that are alive plus everything below them, as a set of pids."""
    kids = {}
    for pid, ppid, _pg, _rss, _cmd in table:
        kids.setdefault(ppid, []).append(pid)
    alive = {r[0] for r in table}
    seen = set()
    stack = [r for r in roots if r in alive]
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        stack.extend(kids.get(p, []))
    return seen


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def kill_pids(pids, grace=4.0):
    """TERM then KILL, by PID, only the pids given (the ones this run started)."""
    pids = [p for p in pids if p != os.getpid()]
    for p in pids:
        try:
            os.kill(p, signal.SIGTERM)
        except OSError:   # gone, or (a recycled pid) not ours: the others are still ended
            pass
    end = time.time() + grace
    while time.time() < end and any(pid_alive(p) for p in pids):
        time.sleep(0.2)
    for p in pids:
        if pid_alive(p):
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass


class Proc:
    """A child process: own session, output teed to a file and a list, tree listing, kill by PID."""

    def __init__(self, name, argv, cwd, env, logpath, events=None, stdin_pipe=False):
        self.name = name
        self.argv = list(argv)
        self.logpath = str(logpath)
        self.lines = []
        self.events = events
        self.started = time.time()
        self.t_exit = None
        self.all_pids = set()  # every pid ever seen in this proc's tree
        Path(self.logpath).parent.mkdir(parents=True, exist_ok=True)
        self._log = open(self.logpath, "w", encoding="utf-8", errors="replace")
        self.p = subprocess.Popen(
            self.argv, cwd=cwd, env=env, stdin=subprocess.PIPE if stdin_pipe else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
        )
        self.pid = self.p.pid
        self.all_pids.add(self.pid)
        self._t = threading.Thread(target=self._pump, daemon=True)
        self._t.start()

    def _pump(self):
        cap = 60 * 1024 * 1024
        written = 0
        for raw in iter(self.p.stdout.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            t = time.time() - self.started
            self.lines.append((t, line))
            if written < cap:
                self._log.write("%8.2f %s\n" % (t, line))
                written += len(line) + 10
                if written >= cap:
                    self._log.write("... log capped at %d MB\n" % (cap // 1024 // 1024))
                self._log.flush()
            if self.events is not None:
                self.events.put((self, line))
        self.p.stdout.close()

    def poll(self):
        rc = self.p.poll()
        if rc is not None and self.t_exit is None:
            self.t_exit = time.time()
        return rc

    def send_line(self, text):
        try:
            self.p.stdin.write((text + "\n").encode())
            self.p.stdin.flush()
            return True
        except Exception:
            return False

    def tree(self, table=None):
        table = table or ps_table()
        pids = descendants(table, [self.pid])
        # Members of the process group we created (start_new_session makes our pid the group id) count too: a daemonised
        # child that kept the group stays in it. A child that moved to another group and was reparented is not found here.
        pids |= {r[0] for r in table if r[2] == self.pid}
        self.all_pids |= pids
        return pids

    def wait(self, timeout):
        try:
            self.p.wait(timeout)
        except subprocess.TimeoutExpired:
            return None
        self.poll()
        self._t.join(timeout=3)
        return self.p.returncode

    def terminate(self, grace=4.0):
        pids = sorted(self.tree())
        if pids:
            kill_pids(pids, grace)
        self.poll()
        self._t.join(timeout=3)
        try:
            self._log.close()
        except Exception:
            pass

    def close(self):
        try:
            self._t.join(timeout=3)
            self._log.close()
        except Exception:
            pass

    def text(self):
        return "\n".join(l for _, l in self.lines)


# --------------------------------------------------------------------------------------------- deletion

def safe_delete(path, roots):
    """Delete a directory tree. Refuses anything but an absolute path strictly inside one of `roots`."""
    p = str(path)
    if not os.path.isabs(p) or ".." in p.split(os.sep) or any(c in p for c in "*?[]{}") or p != os.path.normpath(p):
        raise ValueError("refusing to delete %r" % p)
    if os.path.islink(p):
        raise ValueError("refusing to delete a symlink: %r" % p)
    real = os.path.realpath(p)
    ok = False
    for r in roots:
        rr = os.path.realpath(str(r))
        if real != rr and real.startswith(rr + os.sep):
            ok = True
    if not ok:
        raise ValueError("%r is not strictly inside an allowed root" % p)
    if os.path.isdir(real):
        shutil.rmtree(real)
    elif os.path.exists(real):
        os.unlink(real)


# --------------------------------------------------------------------------------------------- screen

def helper_path():
    return str(ROOT / "work" / "bin" / "screenstate")


def screen_state(pids=None):
    """{'locked': bool, 'display_asleep': bool, 'windows': [...]} from the screenstate helper (read-only)."""
    argv = [helper_path()]
    if pids:
        argv += ["--windows"] + [str(p) for p in sorted(pids)]
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=10).stdout
        return json.loads(out.strip().splitlines()[-1])
    except Exception as e:
        return {"locked": None, "display_asleep": None, "error": str(e), "windows": []}


def screen_unavailable():
    """None when windows can be shown and captured, else the reason."""
    s = screen_state()
    if s.get("locked"):
        return "the screen is locked"
    if s.get("display_asleep"):
        return "the display is asleep"
    if s.get("locked") is None:
        return "the screen state could not be read (%s)" % s.get("error", "?")
    return None


def png_stats(path):
    """(width, height, mean_luma, stdev_luma, distinct_colours_sampled) of an 8-bit RGB/RGBA PNG, stdlib only."""
    import struct
    import zlib
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos = 8
    idat = b""
    w = h = depth = ctype = interlace = None
    while pos < len(data):
        ln, typ = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if typ == b"IHDR":
            w, h, depth, ctype, _c, _f, interlace = struct.unpack(">IIBBBBB", body)
        elif typ == b"IDAT":
            idat += body
        elif typ == b"IEND":
            break
    if depth != 8 or ctype not in (2, 6) or interlace:
        raise ValueError("unsupported PNG (depth %s colour type %s interlace %s)" % (depth, ctype, interlace))
    bpp = 3 if ctype == 2 else 4
    raw = zlib.decompress(idat)
    stride = w * bpp
    prev = bytearray(stride)
    step = max(1, h // 120)
    n = 0
    s = s2 = 0.0
    colours = set()
    i = 0
    for y in range(h):
        ft = raw[i]
        cur = bytearray(raw[i + 1:i + 1 + stride])
        i += 1 + stride
        if ft == 1:
            for x in range(bpp, stride):
                cur[x] = (cur[x] + cur[x - bpp]) & 255
        elif ft == 2:
            for x in range(stride):
                cur[x] = (cur[x] + prev[x]) & 255
        elif ft == 3:
            for x in range(stride):
                a = cur[x - bpp] if x >= bpp else 0
                cur[x] = (cur[x] + ((a + prev[x]) >> 1)) & 255
        elif ft == 4:
            for x in range(stride):
                a = cur[x - bpp] if x >= bpp else 0
                b = prev[x]
                c = prev[x - bpp] if x >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                cur[x] = (cur[x] + pr) & 255
        prev = cur
        if y % step == 0:
            for x in range(0, w, max(1, w // 160)):
                r, g, b = cur[x * bpp], cur[x * bpp + 1], cur[x * bpp + 2]
                l = 0.299 * r + 0.587 * g + 0.114 * b
                n += 1
                s += l
                s2 += l * l
                colours.add((r >> 3, g >> 3, b >> 3))
    mean = s / n
    return w, h, mean, max(0.0, s2 / n - mean * mean) ** 0.5, len(colours)


def git_rev(repo):
    try:
        r = subprocess.run(["git", "-C", expand(repo), "rev-parse", "--short=12", "HEAD"], capture_output=True, text=True, timeout=10)
        dirty = subprocess.run(["git", "-C", expand(repo), "status", "--porcelain"], capture_output=True, text=True, timeout=20).stdout.strip()
        br = subprocess.run(["git", "-C", expand(repo), "branch", "--show-current"], capture_output=True, text=True, timeout=10).stdout.strip()
        return {"rev": r.stdout.strip(), "branch": br, "dirty": bool(dirty)}
    except Exception as e:
        return {"error": str(e)}
