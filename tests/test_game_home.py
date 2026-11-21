"""A game started by bridge-live must not touch a real player's profile.

Every launch goes through `Ctx.start_game` (orchestrator/harness.py), which gives the game a scratch home, refuses a launch that
names a real launcher profile and wraps it in a sandbox that cannot write there (orchestrator/gameenv.py). These tests fail when:

* some code starts a process without going through that function (a new subprocess/Popen/os.system call outside the short list
  of helpers below, or a call to `Ctx.start`, which no longer exists),
* the code that builds a game command line (Gradle's runClient, a server jar) is started as a plain tool,
* a real profile folder is named in the code or in config.json other than the one documented read-only use (the Hytale server
  reads its Java runtime, jar and Assets.zip from the installed package),
* a launcher folder is read without the caller naming it (the asset cache is seeded only from a folder given explicitly),
* the environment, the guard or the sandbox stop doing what the README says.

Every test runs with `gameenv.real_home()` pointed at an empty temporary folder (tests/support.py).

Run:  python3 -m unittest discover -s tests
"""

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "orchestrator"))

import support  # noqa: E402
import common  # noqa: E402
import gameenv  # noqa: E402
import harness  # noqa: E402
import jfr as jfrmod  # noqa: E402

ORCH = ROOT / "orchestrator"


def setUpModule():
    support.start()


def tearDownModule():
    support.stop()


# Functions that may start a process by themselves, and what for. Everything else goes through Ctx.start_game / Ctx.start_tool.
SPAWNERS = {
    ("common.py", "mem_free_percent"): "sysctl, reads the free memory",
    ("common.py", "ps_table"): "ps, lists processes",
    ("common.py", "Proc.__init__"): "the one Popen: every game and tool of a pair is a Proc, made by Ctx._spawn",
    ("common.py", "screen_state"): "the screenstate helper, reads the window server",
    ("common.py", "git_rev"): "git, reads revisions",
    ("harness.py", "Ctx.screenshot"): "screencapture of a game window",
    ("harness.py", "Ctx._spawn"): "makes the Proc; only start_game and start_tool call it",
    ("live.py", "sh"): "helper for cargo/swiftc/sysctl/ps/java -version, no game",
    ("live.py", "_run"): "caffeinate, keeps the display awake",
    ("gameenv.py", "sandbox_status"): "self-test of sandbox-exec against a folder in the scratch dir",
    ("gameenv.py", "seed_minecraft_assets"): "cp -c of the folder the caller named (--minecraft-assets) into work/cache, once",
    ("jfr.py", "summarise"): "java tools/JfrSummary.java over a recording (a JDK program that reads one file)",
}

SPAWN_CALLS = {"Popen", "run", "call", "check_call", "check_output", "system", "execv", "execve", "execvp", "execl", "execlp",
               "spawnv", "spawnl", "posix_spawn", "fork", "forkpty", "Proc"}

# Strings that mean "this code builds a game's command line".
GAME_MARKERS = ("gradlew", "runClient", "-jar", "--gameDir", "--universe", "HytaleServer")

# Names of real launcher folders: only gameenv.py (the guard) may spell them.
PROFILE_WORDS = ("Application Support", ".minecraft", "PrismLauncher", "Prism Launcher", "MultiMC", "PolyMC", "ATLauncher")
_PROFILE_RE = re.compile("|".join(re.escape(w) + r"(?![A-Za-z0-9_])" for w in PROFILE_WORDS))   # ".minecraft" is not "dev.patodois.minecraftlink"


def names_profile(text):
    return bool(_PROFILE_RE.search(text))


def sources():
    for path in sorted(ORCH.glob("*.py")):
        yield path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def functions(tree):
    """(qualified name, node) of every function, methods as Class.method, nested functions under their parent."""
    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + child.name
                yield name, child
                yield from walk(child, name + ".")
            elif isinstance(child, ast.ClassDef):
                yield from walk(child, prefix + child.name + ".")
    yield from walk(tree, "")


def own_nodes(fn):
    """The nodes of a function without those of the functions nested in it."""
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        n = stack.pop()
        yield n
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(n))


def call_name(node):
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return None


def string_constants(fn):
    return [n.value for n in own_nodes(fn) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def spawn_offenders(name, tree, allowed=SPAWNERS):
    """Process-starting calls outside the allowed helpers."""
    out = []
    for qual, fn in functions(tree):
        for n in own_nodes(fn):
            if not (isinstance(n, ast.Call) and call_name(n) in SPAWN_CALLS):
                continue
            f = n.func
            base = f.value.id if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) else None
            # `.run(` / `.call(` also name unrelated methods; only the subprocess and os forms count.
            if call_name(n) in ("run", "call", "check_call", "check_output", "system") and base not in ("subprocess", "os"):
                continue
            if (name, qual) not in allowed:
                out.append("%s:%d in %s calls %s" % (name, n.lineno, qual, call_name(n)))
    for n in tree.body:   # module level code
        if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
            continue
        for m in ast.walk(n):
            if isinstance(m, ast.Call) and call_name(m) in SPAWN_CALLS:
                out.append("%s:%d at module level calls %s" % (name, m.lineno, call_name(m)))
    return out


def _lists_a_marker(fn):
    return any(isinstance(n, ast.List) and any(isinstance(e, ast.Constant) and isinstance(e.value, str) and any(m in e.value for m in GAME_MARKERS) for e in n.elts)
               for n in own_nodes(fn))


def game_launch_offenders(name, tree):
    """Functions that build a game command line and do not start it with start_game, or start one as a tool."""
    out = []
    for qual, fn in functions(tree):
        consts = string_constants(fn)
        calls = {call_name(n) for n in own_nodes(fn) if isinstance(n, ast.Call)}
        builds_game = any(any(m in c for m in GAME_MARKERS) for c in consts if len(c) < 60)
        if "start_tool" in calls and (builds_game or "hytale_paths" in calls):
            out.append("%s %s starts a game as a tool" % (name, qual))
        if builds_game and "start_game" not in calls and _lists_a_marker(fn):
            out.append("%s %s builds a game command line and does not use start_game" % (name, qual))
    return out


def uses_start_game(tree):
    return any("start_game" in {call_name(n) for n in own_nodes(fn) if isinstance(n, ast.Call)} for _q, fn in functions(tree))


class NoBypassTests(unittest.TestCase):
    def test_only_the_listed_helpers_start_processes(self):
        offenders = [o for path, tree in sources() for o in spawn_offenders(path.name, tree)]
        self.assertEqual(offenders, [], "start games through Ctx.start_game (and helpers through Ctx.start_tool); if this is a new "
                                        "helper that is neither, list it in SPAWNERS with the reason")

    def test_listed_helpers_still_exist(self):
        have = {(p.name, q) for p, t in sources() for q, _ in functions(t)}
        self.assertEqual(sorted(set(SPAWNERS) - have), [], "SPAWNERS lists a function that is gone: remove it")

    def test_ctx_start_is_gone(self):
        self.assertFalse(hasattr(harness.Ctx, "start"), "Ctx.start would be a launch without the scratch environment")
        calls = []
        for path, tree in sources():
            for qual, fn in functions(tree):
                for n in own_nodes(fn):
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "start":
                        recv = n.func.value
                        name = recv.id if isinstance(recv, ast.Name) else (recv.attr if isinstance(recv, ast.Attribute) else "")
                        if name in ("ctx", "self") and path.name != "common.py":
                            calls.append("%s:%d %s" % (path.name, n.lineno, qual))
        self.assertEqual(calls, [])

    def test_code_that_builds_a_game_command_starts_it_with_start_game(self):
        offenders = [o for path, tree in sources() if path.name != "gameenv.py" for o in game_launch_offenders(path.name, tree)]
        self.assertEqual(offenders, [])
        self.assertTrue(any(uses_start_game(tree) for _p, tree in sources()), "no caller of start_game found: the scan is looking at the wrong thing")

    def test_both_known_games_use_start_game(self):
        text = {p.name: p.read_text() for p, _ in sources()}
        self.assertIn("ctx.start_game(", text["peers.py"], "the Hytale server")
        self.assertIn("self.ctx.start_game(", text["mc.py"], "the Minecraft clients")


class TheScanCatchesBypassesTests(unittest.TestCase):
    """The scans above are only worth something if they fail on the code they are meant to forbid."""

    def parse(self, code):
        return ast.parse(textwrap.dedent(code))

    def test_a_direct_popen_or_subprocess_run_is_an_offender(self):
        for code in ("import subprocess\ndef go():\n    subprocess.Popen(['java'])\n",
                     "import subprocess\ndef go():\n    subprocess.run(['./gradlew', 'runClient'])\n",
                     "import os\ndef go():\n    os.system('java -jar x.jar')\n",
                     "from common import Proc\nclass K:\n    def go(self):\n        Proc('g', ['java'], None, {}, '/x')\n",
                     "import subprocess\nsubprocess.call(['java'])\n"):
            self.assertTrue(spawn_offenders("new.py", self.parse(code)), code)

    def test_a_listed_helper_and_unrelated_run_methods_are_not(self):
        self.assertEqual(spawn_offenders("jfr.py", self.parse("import subprocess\ndef summarise():\n    subprocess.run(['java'])\n")), [])
        self.assertEqual(spawn_offenders("new.py", self.parse("def go(thing):\n    thing.run()\n    thing.call()\n")), [])

    def test_a_game_started_as_a_tool_or_by_hand_is_an_offender(self):
        as_tool = "def launch(ctx):\n    ctx.start_tool('mc', ['./gradlew', ':fabric:runClient'])\n"
        by_hand = "def launch(ctx):\n    argv = ['java', '-jar', 'HytaleServer.jar']\n    return mine(argv)\n"
        hytale = "def launch(ctx, cfg):\n    hp = hytale_paths(cfg)\n    ctx.start_tool('h', [hp['java']])\n"
        for code in (as_tool, by_hand, hytale):
            self.assertTrue(game_launch_offenders("new.py", self.parse(code)), code)

    def test_start_game_and_plain_tools_are_fine(self):
        ok = "def launch(ctx, sc):\n    ctx.start_game('mc', ['./gradlew', ':fabric:runClient'], sc)\n"
        tool = "def peer(ctx):\n    ctx.start_tool('p', ['live_peer', 'guest'])\n"
        self.assertEqual(game_launch_offenders("new.py", self.parse(ok)), [])
        self.assertEqual(game_launch_offenders("new.py", self.parse(tool)), [])


class RealProfileNamesTests(unittest.TestCase):
    def test_no_module_but_gameenv_spells_a_profile_folder(self):
        offenders = []
        for path, tree in sources():
            if path.name == "gameenv.py":
                continue
            for n in ast.walk(tree):
                if isinstance(n, ast.Constant) and isinstance(n.value, str) and names_profile(n.value):
                    offenders.append("%s:%d %r" % (path.name, n.lineno, n.value[:60]))
        self.assertEqual(offenders, [])

    def test_config_names_a_profile_folder_only_for_the_read_only_hytale_package(self):
        cfg = json.loads((ROOT / "config.json").read_text())
        hits = []

        def walk(node, trail):
            if isinstance(node, dict):
                for k, v in node.items():
                    walk(v, trail + [k])
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, trail + [str(i)])
            elif isinstance(node, str) and names_profile(node):
                hits.append("/".join(trail))
        walk(cfg, [])
        self.assertEqual(hits, ["hytale/package"], "config.json may name only the installed Hytale package (read-only use)")

    def test_the_hytale_package_is_only_read_and_declared_read_only(self):
        users = []
        for path, tree in sources():
            for qual, fn in functions(tree):
                for n in own_nodes(fn):
                    if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant) and n.slice.value == "package":
                        users.append((path.name, qual))
        self.assertEqual(sorted(users), [("peers.py", "hytale_paths"), ("peers.py", "start_hytale")])
        src = (ORCH / "peers.py").read_text()
        self.assertIn('read_only=[expand(cfg["hytale"]["package"])]', src)

    def test_the_servers_own_writes_stay_in_this_repository(self):
        cfg = json.loads((ROOT / "config.json").read_text())
        self.assertFalse(os.path.isabs(cfg["hytale"]["server_dir"]), "the server directory is relative to the repository")
        self.assertTrue(common.expand(cfg["hytale"]["server_dir"]).startswith(str(ROOT) + os.sep))


class GameEnvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bl-test-"))
        self.scratch = gameenv.Scratch(gameenv.SCRATCH_ROOT / ("unittest-%d" % os.getpid()) / "a" / "game")

    def tearDown(self):
        self.scratch.remove()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scratch_home_xdg_and_tmp_are_inside_the_scratch_folder(self):
        env = gameenv.game_env(self.scratch, base={"PATH": "/usr/bin", "HOME": "/Users/someone"})
        root = str(self.scratch.root)
        for key in ("HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "TMPDIR"):
            self.assertTrue(env[key].startswith(root + os.sep), key)
            self.assertTrue(os.path.isdir(env[key]), key)
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_gradle_keeps_its_real_cache_and_nothing_else_of_the_real_home_is_used(self):
        env = gameenv.game_env(self.scratch, base={})
        self.assertEqual(env["GRADLE_USER_HOME"], os.path.join(gameenv.real_home(), ".gradle"))
        env = gameenv.game_env(self.scratch, base={"GRADLE_USER_HOME": "/elsewhere/gradle"})
        self.assertEqual(env["GRADLE_USER_HOME"], "/elsewhere/gradle")

    def test_sound_is_off_in_the_environment_too(self):
        env = gameenv.game_env(self.scratch, base={})
        self.assertEqual(env["ALSOFT_DRIVERS"], "null")
        self.assertTrue(env["ALSOFT_LOGFILE"].startswith(str(self.scratch.root)))

    def test_it_does_not_touch_the_process_environment(self):
        before = dict(os.environ)
        gameenv.game_env(self.scratch, JAVA_HOME="/x")
        self.assertEqual(dict(os.environ), before)

    def test_extra_values_are_kept(self):
        env = gameenv.game_env(self.scratch, base={}, JAVA_HOME="/jdk")
        self.assertEqual(env["JAVA_HOME"], "/jdk")

    def test_two_scratches_are_different(self):
        other = gameenv.Scratch(gameenv.SCRATCH_ROOT / ("unittest-%d" % os.getpid()) / "a" / "game2")
        try:
            self.assertNotEqual(gameenv.game_env(self.scratch, base={})["HOME"], gameenv.game_env(other, base={})["HOME"])
        finally:
            other.remove()

    def _fake_launcher_assets(self):
        """A launcher's assets folder in the (fake) real home, as a player's machine has one."""
        real_assets = Path(gameenv.real_home()) / "Library" / "Application Support" / "minecraft" / "assets"
        (real_assets / "objects" / "aa").mkdir(parents=True, exist_ok=True)
        (real_assets / "objects" / "aa" / "aabb").write_text("asset")
        self.addCleanup(lambda: shutil.rmtree(Path(gameenv.real_home()) / "Library", ignore_errors=True))
        return real_assets

    def _cache(self):
        cache = self.tmp / "cache"
        return cache, (mock.patch.object(gameenv, "CACHE_ROOT", cache), mock.patch.object(gameenv, "ASSETS_CACHE", cache / "minecraft-assets"),
                       mock.patch.object(gameenv, "_SEEDED", []))

    def test_by_default_the_asset_cache_starts_empty_and_no_launcher_folder_is_read(self):
        real_assets = self._fake_launcher_assets()
        cache, patches = self._cache()
        listed = []
        real_listdir, real_scandir = os.listdir, os.scandir
        watch = lambda p: listed.append(str(p)) if "Application Support" in str(p) and not str(p).startswith(str(self.scratch.root)) else None
        with patches[0], patches[1], patches[2], \
                mock.patch.object(gameenv.os, "listdir", lambda p=".": watch(p) or real_listdir(p)), \
                mock.patch.object(gameenv.os, "scandir", lambda p=".": watch(p) or real_scandir(p)), \
                mock.patch.object(gameenv.subprocess, "run", side_effect=AssertionError("no copy without a named folder")), \
                mock.patch.object(gameenv.shutil, "copytree", side_effect=AssertionError("no copy without a named folder")):
            self.scratch.link_minecraft_assets(gameenv.assets_seed({"run": {}}))
        link = self.scratch.home / "Library" / "Application Support" / "minecraft" / "assets"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.path.realpath(link), os.path.realpath(cache / "minecraft-assets"))
        self.assertEqual(os.listdir(cache / "minecraft-assets"), [], "the cache must start empty")
        self.assertEqual(listed, [])
        self.assertTrue((real_assets / "objects" / "aa" / "aabb").exists())

    def test_the_asset_cache_is_seeded_only_from_a_folder_the_caller_names(self):
        seed = self.tmp / "my-assets"
        (seed / "objects" / "aa").mkdir(parents=True)
        (seed / "objects" / "aa" / "aabb").write_text("asset")
        before = os.stat(seed / "objects" / "aa" / "aabb").st_mtime_ns
        cache, patches = self._cache()
        with patches[0], patches[1], patches[2]:
            self.scratch.link_minecraft_assets(gameenv.assets_seed({"run": {"minecraft_assets": str(seed)}}))
        link = self.scratch.home / "Library" / "Application Support" / "minecraft" / "assets"
        self.assertEqual((link / "objects" / "aa" / "aabb").read_text(), "asset")       # a copy of the named folder
        (link / "objects" / "new").write_text("downloaded")                             # a download lands in the cache ...
        self.assertFalse((seed / "objects" / "new").exists())                            # ... never in the named folder
        self.assertEqual(os.stat(seed / "objects" / "aa" / "aabb").st_mtime_ns, before)

    def test_assets_seed_comes_from_the_argument_or_the_environment_only(self):
        self.assertIsNone(gameenv.assets_seed(None, environ={}))
        self.assertIsNone(gameenv.assets_seed({"run": {"minecraft_assets": None}}, environ={}))
        self.assertEqual(gameenv.assets_seed(None, environ={gameenv.ASSETS_SEED_ENV: "/x/assets"}), "/x/assets")
        self.assertEqual(gameenv.assets_seed({"run": {"minecraft_assets": "/a"}}, environ={gameenv.ASSETS_SEED_ENV: "/b"}), "/a")

    def test_a_named_seed_that_is_not_a_folder_is_refused(self):
        cache, patches = self._cache()
        with patches[0], patches[1], patches[2]:
            with self.assertRaises(ValueError):
                gameenv.seed_minecraft_assets(str(self.tmp / "missing"))
        self.assertFalse((cache / "minecraft-assets").exists())

    def test_only_the_deny_list_and_gradle_cache_use_the_real_home(self):
        tree = ast.parse((ORCH / "gameenv.py").read_text(encoding="utf-8"))
        users = sorted(q for q, fn in functions(tree) if "real_home" in {call_name(n) for n in own_nodes(fn) if isinstance(n, ast.Call)})
        self.assertEqual(users, ["game_env", "real_profile_dirs"], "a new user of the real home folder in gameenv.py: does it read a launcher folder?")

    def test_a_scratch_outside_work_scratch_is_refused(self):
        with self.assertRaises(ValueError):
            gameenv.Scratch(self.tmp / "x")
        with self.assertRaises(ValueError):
            gameenv.Scratch(Path(gameenv.real_home()) / "Library" / "Application Support" / "minecraft")

    def test_scratch_removal_goes_through_safe_delete_and_leaves_the_root(self):
        marker = self.scratch.home / "f.txt"
        marker.write_text("x")
        self.scratch.remove()
        self.assertFalse(self.scratch.root.exists())
        self.assertTrue(gameenv.SCRATCH_ROOT.exists())


class ProfileGuardTests(unittest.TestCase):
    def setUp(self):
        home = gameenv.real_home()
        self.mc = os.path.join(home, "Library", "Application Support", "minecraft")
        self.prism = os.path.join(home, "Library", "Application Support", "PrismLauncher")
        self.hytale = os.path.join(home, "Library", "Application Support", "Hytale")
        self.package = os.path.join(self.hytale, "install", "release", "package")

    def test_the_list_covers_minecraft_prism_and_hytale(self):
        dirs = gameenv.real_profile_dirs()
        for d in (self.mc, self.prism, self.hytale, os.path.join(gameenv.real_home(), ".minecraft")):
            self.assertIn(d, dirs)

    def test_cwd_argument_and_environment_are_checked(self):
        with self.assertRaises(ValueError):
            gameenv.assert_clear_of_profiles(["java"], self.mc + "/saves", {})
        with self.assertRaises(ValueError):
            gameenv.assert_clear_of_profiles(["java", "-Dx=" + self.prism + "/instances/a"], None, {})
        with self.assertRaises(ValueError):
            gameenv.assert_clear_of_profiles(["java"], None, {"MINECRAFT_HOME": self.mc})
        with self.assertRaises(ValueError):
            gameenv.assert_clear_of_profiles(["java", "--gameDir", os.path.join(gameenv.real_home(), ".minecraft")], None, {})

    def test_read_only_paths_are_allowed_but_nothing_else_in_the_same_profile(self):
        gameenv.assert_clear_of_profiles(["java", "-jar", self.package + "/game/latest/Server/HytaleServer.jar"], None, {}, read_only=[self.package])
        with self.assertRaises(ValueError):
            gameenv.assert_clear_of_profiles(["java", self.hytale + "/UserData/x"], None, {}, read_only=[self.package])
        with self.assertRaises(ValueError):  # a sibling that merely shares the prefix of the read-only path
            gameenv.assert_clear_of_profiles(["java", self.package + "-other/x"], None, {}, read_only=[self.package])
        gameenv.assert_clear_of_profiles(["java", self.package], None, {}, read_only=[self.package])   # the path itself
        with self.assertRaises(ValueError):  # another spelling of the same folder on a case-insensitive file system
            gameenv.assert_clear_of_profiles(["java", self.mc.upper() + "/saves"], None, {})

    def test_clean_launches_pass(self):
        gameenv.assert_clear_of_profiles(["./gradlew", ":fabric:runClient", "-Pshots=/Users/x/bridge-live/logs"], "/Users/x/minecraft-link", {"PATH": "/usr/bin"})

    def test_extra_protected_dirs_from_config(self):
        self.assertIn("/data/mc", gameenv.real_profile_dirs({"protected_dirs": ["/data/mc"]}))


class SandboxTests(unittest.TestCase):
    @unittest.skipUnless(os.path.exists("/usr/bin/sandbox-exec"), "no sandbox-exec")
    def test_profile_denies_writes_below_a_folder_and_allows_the_listed_files(self):
        tmp = Path(tempfile.mkdtemp(prefix="bl-sb-")).resolve()
        try:
            denied, other = tmp / "denied", tmp / "other"
            denied.mkdir()
            other.mkdir()
            argv = lambda target: gameenv.sandboxed_argv(["/usr/bin/touch", str(target)], [denied], [denied / "options.txt"])
            (denied / "logs").mkdir()
            logs = lambda target: gameenv.sandboxed_argv(["/usr/bin/touch", str(target)], [denied], [str(denied / "logs") + "/"])
            self.assertEqual(subprocess.run(logs(denied / "logs" / "latest.log"), capture_output=True).returncode, 0)   # a folder allowed as a whole
            self.assertNotEqual(subprocess.run(logs(denied / "saves"), capture_output=True).returncode, 0)
            self.assertNotEqual(subprocess.run(argv(denied / "x"), capture_output=True).returncode, 0)
            self.assertFalse((denied / "x").exists())
            self.assertEqual(subprocess.run(argv(denied / "options.txt"), capture_output=True).returncode, 0)
            self.assertEqual(subprocess.run(argv(other / "y"), capture_output=True).returncode, 0)
            self.assertTrue((denied / "options.txt").exists() and (other / "y").exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_profile_text(self):
        p = gameenv.sandbox_profile(["/a b", '/c"d'], ["/a b/o.txt"])
        self.assertIn('(deny file-write* (subpath "/a b")(subpath "/c\\"d"))', p)
        self.assertTrue(p.endswith('(allow file-write* (literal "/a b/o.txt"))'))
        self.assertEqual(gameenv.sandbox_profile([]), "(version 1)(allow default)")


class StartGameTests(unittest.TestCase):
    """Ctx.start_game with Proc replaced: what a launch would be handed."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bl-ctx-"))
        cfg = json.loads((ROOT / "config.json").read_text())
        logdir = ROOT / "logs" / ("unittest-%d" % os.getpid()) / "pair"
        self.ctx = harness.Ctx(cfg, "pair", logdir, self.tmp / "shots")
        self.logroot = logdir.parent
        self.calls = []

        class FakeProc:
            def __init__(inner, name, argv, cwd, env, logpath, events=None, stdin_pipe=False):
                inner.pid = 1
                self_calls.append({"name": name, "argv": argv, "cwd": cwd, "env": env})
            def poll(inner):
                return 0
            def close(inner):
                pass
            def terminate(inner, grace=0):
                pass
            def tree(inner, table=None):
                return set()
        self_calls = self.calls
        patcher = mock.patch.object(harness, "Proc", FakeProc)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.addCleanup(lambda: shutil.rmtree(self.logroot, ignore_errors=True))
        self.addCleanup(lambda: [s.remove() for s in self.ctx.scratches])

    @unittest.skipUnless(os.path.exists("/usr/bin/sandbox-exec"), "no sandbox-exec")
    def test_a_game_gets_the_scratch_environment_and_the_sandbox(self):
        sc = self.ctx.new_scratch("client")
        self.ctx.start_game("g", ["/bin/echo", "hi"], sc, cwd=str(ROOT), env={"JAVA_HOME": "/jdk"})
        call = self.calls[0]
        self.assertTrue(call["env"]["HOME"].startswith(str(sc.root)))
        self.assertEqual(call["env"]["JAVA_HOME"], "/jdk")
        self.assertEqual(call["env"]["ALSOFT_DRIVERS"], "null")
        self.assertEqual(call["argv"][0], "/usr/bin/sandbox-exec")
        for prof in gameenv.real_profile_dirs():
            self.assertIn('(subpath "%s")' % prof, call["argv"][2])
        self.assertEqual(call["argv"][-2:], ["/bin/echo", "hi"])

    def test_without_a_working_sandbox_no_game_is_started(self):
        sc = self.ctx.new_scratch("client")
        with mock.patch.object(gameenv, "sandbox_status", return_value=(False, "sandbox-exec not found")):
            with self.assertRaises(RuntimeError):
                self.ctx.start_game("g", ["/bin/echo", "hi"], sc, cwd=str(ROOT))
        self.assertEqual(self.calls, [])
        self.assertTrue(any("no write-deny sandbox" in n for n in self.ctx.notes))

    def test_a_launch_that_names_a_real_profile_is_refused_before_anything_starts(self):
        sc = self.ctx.new_scratch("client")
        home = gameenv.real_home()
        with self.assertRaises(ValueError):
            self.ctx.start_game("g", ["java", "--gameDir", os.path.join(home, ".minecraft")], sc)
        with self.assertRaises(ValueError):
            self.ctx.start_game("g", ["java"], sc, cwd=os.path.join(home, "Library", "Application Support", "PrismLauncher"))
        self.assertEqual(self.calls, [])

    def test_a_tool_is_started_as_is(self):
        self.ctx.start_tool("t", ["/bin/echo"], env={"A": "1"})
        self.assertEqual(self.calls[0]["env"]["A"], "1")
        self.assertEqual(self.calls[0]["argv"], ["/bin/echo"])
        self.assertNotEqual(self.calls[0]["env"].get("ALSOFT_DRIVERS"), "null")

    def test_scratch_folders_of_a_pair_go_with_finish(self):
        sc = self.ctx.new_scratch("client")
        self.assertTrue(sc.root.exists())
        self.ctx.finish(grace=0.1)
        self.assertFalse(sc.root.exists())


class InitScriptTests(unittest.TestCase):
    def setUp(self):
        self.scratch = gameenv.Scratch(gameenv.SCRATCH_ROOT / ("unittest-%d" % os.getpid()) / "i" / "game")
        self.addCleanup(self.scratch.remove)

    def test_the_game_jvm_gets_a_scratch_user_home_and_the_extra_arguments(self):
        text = gameenv.init_script_text(self.scratch, jvm_args=["-XX:StartFlightRecording:filename=/x/y.jfr"])
        self.assertIn("-Duser.home=" + str(self.scratch.home), text)
        self.assertIn("-Djava.io.tmpdir=" + str(self.scratch.tmp), text)
        self.assertIn("-XX:StartFlightRecording:filename=/x/y.jfr", text)
        self.assertIn("name != 'runClient'", text)
        self.assertNotIn("--gameDir", text)

    def test_user_home_can_be_left_alone_for_a_build_that_needs_it(self):
        self.assertNotIn("-Duser.home", gameenv.init_script_text(self.scratch, user_home=False))

    def test_instance_sets_the_game_directory(self):
        text = gameenv.init_script_text(self.scratch, instance=str(self.scratch.instance))
        self.assertIn("'--gameDir'", text)
        self.assertIn(str(self.scratch.instance), text)

    def test_a_quote_in_a_path_cannot_break_out(self):
        text = gameenv.init_script_text(self.scratch, jvm_args=["-Dx=it's"])
        self.assertIn("-Dx=it\\'s", text)


class Jfr(unittest.TestCase):
    def test_a_java_8_build_is_skipped_with_its_reason(self):
        cfg = json.loads((ROOT / "config.json").read_text())
        for v in ("1.12.2", "1.7.10"):
            p = jfrmod.plan(cfg, v, True)
            self.assertFalse(p["active"], v)
            self.assertIn("Java 8", p["reason"])
        for v in ("26.3", "1.20.1"):
            self.assertTrue(jfrmod.plan(cfg, v, True)["active"], v)
        self.assertFalse(jfrmod.plan(cfg, "26.3", False)["active"])


if __name__ == "__main__":
    unittest.main()
