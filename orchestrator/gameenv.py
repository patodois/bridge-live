"""The one place that decides what a launched game may see and touch.

Every game bridge-live starts (a Minecraft dev client through Gradle, the headless Hytale server) is started by
`harness.Ctx.start_game`, which calls into this module. What it guarantees:

* **Scratch home.** HOME, the XDG folders and TMPDIR of the game point at a fresh folder under `work/scratch/`, and the
  game JVM gets `-Duser.home` and `-Djava.io.tmpdir` of the same folder (a JVM on macOS ignores $HOME, so the
  property has to be set; it is set by a Gradle init script, which touches the forked game JVM only, never Gradle's
  own JVM and its caches). A game that falls back to "the player's folder" lands in the scratch folder.
* **Scratch instance.** Where the build lets it (`instance` in config.json), the game's working directory is a scratch
  `instance/` too, so options.txt, saves, logs and crash reports of a run never touch the checkout's `run/` folder.
* **No sound.** options.txt volumes (mc.py) *and* OpenAL Soft's `null` backend (`ALSOFT_DRIVERS=null`), so even a
  wrong options.txt cannot reach the speakers. OpenAL's own log is kept and read back as a check.
* **Real profiles are not writable.** The launch is wrapped in `sandbox-exec` with a profile that denies every write
  under the player's real launcher folders (see `real_profile_dirs`). The mechanism is tested before it is relied on
  (`sandbox_status`); if it cannot be used, the game is not started (the pair ends as an error).
* **Real profiles are not read.** Nothing here lists or reads a real launcher folder by default. The only reads are
  opt-in: the vanilla asset cache is seeded from a folder the caller names (`assets_seed`), and the before/after
  manifest (`profile_manifest`) runs only with `./live --check-profiles`. Building the deny list resolves the path of
  each protected folder (`os.path.realpath`), which looks at the folder's own entry, never inside it.
* **Real profiles are not named.** `assert_clear_of_profiles` refuses a launch whose working directory, argv or
  environment names a real profile, except the paths the caller declares read-only (the Hytale server reads its
  Java runtime, jar and Assets.zip from the installed package; nothing is ever written there).

Nothing in here runs `rm`: scratch folders are removed by `common.safe_delete` inside the `work/scratch` root.
"""

import os
import pwd
import re
import shutil
import subprocess
from pathlib import Path

from common import ROOT, safe_delete

SCRATCH_ROOT = ROOT / "work" / "scratch"
CACHE_ROOT = ROOT / "work" / "cache"          # survives runs (`./live clean` deletes it): game assets the Forge builds download
ASSETS_CACHE = CACHE_ROOT / "minecraft-assets"
# The one way to seed the asset cache from an existing folder, besides `./live --minecraft-assets DIR`.
ASSETS_SEED_ENV = "BRIDGE_LIVE_MINECRAFT_ASSETS"

# Names of the launcher folders under ~/Library/Application Support that hold a player's profiles, instances, saves
# and sign-in data. The list is the "never write here" list; config.json may add more (`protected_dirs`).
_APP_SUPPORT_PROFILES = ("minecraft", "PrismLauncher", "Prism Launcher", "PolyMC", "MultiMC", "ATLauncher", "Hytale")
_HOME_PROFILES = (".minecraft",)


def real_home():
    """The account's home folder from the password database, never from $HOME (which this module overrides for games)."""
    return pwd.getpwuid(os.getuid()).pw_dir


def real_profile_dirs(cfg=None):
    home = real_home()
    dirs = [os.path.join(home, d) for d in _HOME_PROFILES]
    dirs += [os.path.join(home, "Library", "Application Support", d) for d in _APP_SUPPORT_PROFILES]
    for extra in (cfg or {}).get("protected_dirs", []):
        dirs.append(os.path.normpath(os.path.expanduser(extra)))
    out = []
    for d in dirs:
        for form in (d, os.path.realpath(d)):
            if form not in out:
                out.append(form)
    return out


# ---------------------------------------------------------------------------------------------- scratch folders

class Scratch:
    """A game's scratch folders: `home`, the XDG folders, `tmp`, an `instance` for the game directory, `jfr` for recordings."""

    SUBDIRS = ("home", "xdg-data", "xdg-config", "xdg-cache", "xdg-state", "tmp", "instance", "jfr")

    def __init__(self, root):
        self.root = Path(root)
        if not self.root.is_absolute() or not str(self.root).startswith(str(SCRATCH_ROOT) + os.sep):
            raise ValueError("a scratch folder must be inside %s, not %s" % (SCRATCH_ROOT, root))
        for d in self.SUBDIRS:
            (self.root / d).mkdir(parents=True, exist_ok=True)
        self.home = self.root / "home"
        self.tmp = self.root / "tmp"
        self.instance = self.root / "instance"
        self.jfr = self.root / "jfr"

    def link_minecraft_assets(self, seed=None):
        """Some Forge builds (ForgeGradle 7 / Mavenizer on 26.3) keep the vanilla assets in `$HOME/Library/Application Support/minecraft/assets`
        and download what is missing there. With HOME in the scratch folder that path is `home/Library/...`: make it a link to bridge-live's own
        persistent cache (`work/cache/minecraft-assets`, see `seed_minecraft_assets`; `seed` is the folder the caller named, or None)."""
        seed_minecraft_assets(seed)
        d = self.home / "Library" / "Application Support" / "minecraft"
        d.mkdir(parents=True, exist_ok=True)
        link = d / "assets"
        if not link.is_symlink():
            link.symlink_to(ASSETS_CACHE)

    @property
    def openal_log(self):
        return self.root / "openal.log"

    @property
    def init_script(self):
        return self.root / "init.gradle"

    def remove(self):
        if self.root.exists():
            safe_delete(str(self.root), [str(SCRATCH_ROOT)])
        for parent in (self.root.parent, self.root.parent.parent):   # <pair> and <run> folders, once they are empty
            if parent != SCRATCH_ROOT and str(parent).startswith(str(SCRATCH_ROOT) + os.sep):
                try:
                    os.rmdir(parent)
                except OSError:
                    pass


_SEEDED = []


def assets_seed(cfg=None, environ=None):
    """The folder the asset cache may be seeded from, or None. Only a folder the caller names explicitly counts:
    `./live --minecraft-assets DIR` (cfg["run"]["minecraft_assets"]) or the BRIDGE_LIVE_MINECRAFT_ASSETS environment variable.
    There is no default: without one of them no launcher folder is looked at and the cache starts empty."""
    env = os.environ if environ is None else environ
    given = ((cfg or {}).get("run") or {}).get("minecraft_assets") or env.get(ASSETS_SEED_ENV) or None
    return os.path.abspath(os.path.expanduser(str(given))) if given else None


def seed_minecraft_assets(seed=None):
    """Make sure work/cache/minecraft-assets exists. When it does not, it starts empty, or, when the caller named a folder (`seed`, see
    `assets_seed`), as a copy of that folder (copied with `cp -c`, an APFS clone that costs no disk), so a run does not download thousands of
    files again. `seed` is only read, once, when the cache is created; an existing cache is used as it is. No folder is ever read by default."""
    if _SEEDED:
        return
    if not ASSETS_CACHE.exists():
        if seed is not None and not os.path.isdir(seed):
            raise ValueError("the folder named to seed the Minecraft asset cache is not a folder: %s" % seed)
        CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_ROOT / "minecraft-assets.partial"
        if tmp.exists():
            safe_delete(str(tmp), [str(CACHE_ROOT)])
        if seed is not None:
            r = subprocess.run(["/bin/cp", "-cR", str(seed), str(tmp)], capture_output=True, text=True)
            if r.returncode != 0:
                if tmp.exists():
                    safe_delete(str(tmp), [str(CACHE_ROOT)])
                shutil.copytree(seed, tmp, symlinks=True)
        else:
            tmp.mkdir()
        os.replace(tmp, ASSETS_CACHE)
    _SEEDED.append(True)


def clear_scratch_root():
    """At the start of a run: scratch folders a crashed earlier run left behind (one run at a time, the lock is held)."""
    if SCRATCH_ROOT.exists():
        for name in sorted(os.listdir(SCRATCH_ROOT)):
            safe_delete(str(SCRATCH_ROOT / name), [str(SCRATCH_ROOT)])


# ---------------------------------------------------------------------------------------------- the environment

def game_env(scratch, base=None, **extra):
    """The environment of a game process: scratch home, XDG and temp folders, Gradle's real cache folder kept, no sound.

    `base` is the environment to start from (default os.environ); the result is a new dict. Gradle (the Minecraft dev
    clients start through it) needs its own cache offline, so GRADLE_USER_HOME is pinned to the real one *before* HOME is
    replaced; that cache holds downloaded libraries, not a game profile.
    """
    env = dict(os.environ if base is None else base)
    env.update({k: str(v) for k, v in extra.items()})
    env.setdefault("GRADLE_USER_HOME", os.path.join(real_home(), ".gradle"))
    env.update({
        "HOME": str(scratch.home),
        "XDG_DATA_HOME": str(scratch.root / "xdg-data"),
        "XDG_CONFIG_HOME": str(scratch.root / "xdg-config"),
        "XDG_CACHE_HOME": str(scratch.root / "xdg-cache"),
        "XDG_STATE_HOME": str(scratch.root / "xdg-state"),
        "TMPDIR": str(scratch.tmp),
        "BRIDGE_LIVE_SCRATCH": str(scratch.root),
        # No sound, second layer: OpenAL Soft opens its "null" backend, so nothing can reach an output device whatever options.txt says.
        # Its log says which backend was opened (read back by mc.py).
        "ALSOFT_DRIVERS": "null",
        "ALSOFT_LOGLEVEL": "3",
        "ALSOFT_LOGFILE": str(scratch.openal_log),
        "BRIDGE_LIVE_NO_AUDIO": "1",
    })
    return env


def init_script_text(scratch, jvm_args=(), instance=None, user_home=True):
    """A Gradle init script for the game's `runClient` JavaExec task.

    Everything is done in `doFirst`, i.e. after the build script (and Loom, ForgeGradle, ModDevGradle) finished configuring
    the task, so nothing can overwrite it. It only touches the forked game JVM: scratch user.home and temp dir, the
    environment, the extra JVM arguments (JFR), and, with `instance`, the working directory plus any --gameDir argument.
    """
    def q(s):
        return "'" + str(s).replace("\\", "\\\\").replace("'", "\\'") + "'"

    jvm = list(jvm_args)
    if user_home:
        jvm += ["-Duser.home=" + str(scratch.home), "-Djava.io.tmpdir=" + str(scratch.tmp)]
    lines = [
        "// bridge-live: generated per launch. Applies to the forked game JVM of runClient only.",
        "allprojects {",
        "    tasks.withType(JavaExec).configureEach { t ->",
        "        if (t.name != 'runClient') return",
        "        t.doFirst {",
        "            t.jvmArgs(%s)" % ", ".join(q(a) for a in jvm) if jvm else "            // no extra JVM arguments",
    ]
    for key in ("HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "TMPDIR",
                "ALSOFT_DRIVERS", "ALSOFT_LOGLEVEL", "ALSOFT_LOGFILE"):
        lines.append("            t.environment(%s, System.getenv(%s))" % (q(key), q(key)))
    if instance:
        lines += [
            "            def inst = new File(%s)" % q(instance),
            "            inst.mkdirs()",
            "            t.workingDir = inst",   # not honoured by every run task (Loom's keeps its own), hence --gameDir as well
            "            def a = new ArrayList(t.args ?: [])",
            "            int at = a.findIndexOf { it.toString() == '--gameDir' }",
            "            if (at >= 0 && at + 1 < a.size()) a[at + 1] = inst.absolutePath",
            "            else { a.add('--gameDir'); a.add(inst.absolutePath) }",
            "            t.args = a",
        ]
    lines += [
        "            println 'bridge-live-init: workingDir=' + t.workingDir + ' java=' + t.executable",
        "            println 'bridge-live-init: args=' + t.args + ' providers=' + t.argumentProviders.collect { it.asArguments().toList() } + ' jvm=' + t.allJvmArgs",
        "        }",
        "    }",
        "}",
    ]
    return "\n".join(lines) + "\n"


def write_init_script(scratch, **kw):
    scratch.init_script.write_text(init_script_text(scratch, **kw), encoding="utf-8")
    return str(scratch.init_script)


# ---------------------------------------------------------------------------------------------- real profiles

def _mentions(token, profile_dirs, read_only):
    """The real profile dir named in `token` outside every read-only path, or None.

    Case-insensitive (the default macOS file system is), and a read-only path covers itself and what is below it, never a sibling
    that merely shares its prefix (`.../package-other`)."""
    low = token.casefold()
    ros = [ro.rstrip("/").casefold() for ro in read_only]
    for prof in profile_dirs:
        p = prof.casefold()
        start = 0
        while True:
            i = low.find(p, start)
            if i < 0:
                break
            tail = low[i:]
            if not any(tail == ro or tail.startswith(ro + "/") for ro in ros):
                return prof
            start = i + len(p)
    return None


def assert_clear_of_profiles(argv, cwd, env, read_only=(), cfg=None):
    """Raise ValueError when the working directory, an argument or an environment value names a real profile folder,
    except inside the declared read-only paths (installed game files that are only read)."""
    profs = real_profile_dirs(cfg)
    ro = []
    for p in read_only:
        for form in (os.path.normpath(str(p)), os.path.realpath(str(p))):
            if form not in ro:
                ro.append(form)
    where = [("cwd", str(cwd))] if cwd else []
    where += [("argument %d" % i, str(a)) for i, a in enumerate(argv)]
    where += [("environment %s" % k, str(v)) for k, v in (env or {}).items()]
    for label, token in where:
        hit = _mentions(token, profs, ro)
        if hit:
            raise ValueError("refusing to start a game: %s names the real profile folder %s" % (label, hit))


def _sb(path):
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def sandbox_profile(deny_write, allow_write=()):
    """Deny every write below the `deny_write` folders; then (the last matching rule wins) allow the exact files and folders in
    `allow_write`, the few the project's own build must still write (a path ending in "/" allows the whole folder)."""
    deny = "".join('(subpath "%s")' % _sb(p) for p in sorted({str(p) for p in deny_write}))
    if not deny:
        return "(version 1)(allow default)"
    allow = "".join(('(subpath "%s")' % _sb(str(p).rstrip("/")) if str(p).endswith("/") else '(literal "%s")' % _sb(p)) for p in sorted({str(p) for p in allow_write}))
    return "(version 1)(allow default)(deny file-write* %s)%s" % (deny, "(allow file-write* %s)" % allow if allow else "")


_SANDBOX_STATUS = {}


def sandbox_status(scratch):
    """(ok, detail): sandbox-exec exists and really denies a write. Tested against a folder inside the scratch folder, so
    a broken sandbox can never write anywhere that matters. Cached for the process."""
    if "v" in _SANDBOX_STATUS:
        return _SANDBOX_STATUS["v"]
    exe = "/usr/bin/sandbox-exec"
    if not os.path.exists(exe):
        res = (False, "sandbox-exec not found")
    else:
        denied = scratch.root / "sandbox-probe" / "denied"
        allowed = scratch.root / "sandbox-probe" / "allowed"
        denied.mkdir(parents=True, exist_ok=True)
        allowed.mkdir(parents=True, exist_ok=True)
        prof = sandbox_profile([os.path.realpath(denied)])
        try:
            r1 = subprocess.run([exe, "-p", prof, "/usr/bin/touch", str(denied / "x")], capture_output=True, text=True, timeout=20)
            r2 = subprocess.run([exe, "-p", prof, "/usr/bin/touch", str(allowed / "x")], capture_output=True, text=True, timeout=20)
            if r1.returncode != 0 and not (denied / "x").exists() and r2.returncode == 0 and (allowed / "x").exists():
                res = (True, "a write into a denied folder fails, a write elsewhere works")
            else:
                res = (False, "self-test failed: denied rc=%s created=%s, allowed rc=%s" % (r1.returncode, (denied / "x").exists(), r2.returncode))
        except Exception as e:
            res = (False, "self-test could not run: %s" % e)
    _SANDBOX_STATUS["v"] = res
    return res


def sandboxed_argv(argv, deny_write, allow_write=()):
    return ["/usr/bin/sandbox-exec", "-p", sandbox_profile(deny_write, allow_write)] + list(argv)


# ---------------------------------------------------------------------------------------------- did anything change?

def profile_manifest(cfg=None):
    """{path: (size, mtime_ns)} of every file under the real profile folders. Read-only (os.scandir), but it lists the player's folders,
    so it only runs when asked for (`./live --check-profiles`)."""
    out = {}
    for base in real_profile_dirs(cfg):
        if not os.path.isdir(base) or os.path.islink(base):
            continue
        stack = [base]
        while stack:
            d = stack.pop()
            try:
                with os.scandir(d) as it:
                    for e in it:
                        try:
                            if e.is_symlink():
                                continue
                            st = e.stat(follow_symlinks=False)
                            if e.is_dir(follow_symlinks=False):
                                stack.append(e.path)
                                out[e.path + "/"] = (0, st.st_mtime_ns)
                            else:
                                out[e.path] = (st.st_size, st.st_mtime_ns)
                        except OSError:
                            pass
            except OSError:
                pass
    return out


def manifest_diff(before, after):
    """(added, removed, changed) path lists."""
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(p for p in set(before) & set(after) if before[p] != after[p] and not p.endswith("/"))
    return added, removed, changed
