"""Smaller behaviours of the harness, with fakes (review findings BL-13 to BL-20). No game, no peer, no build.

Run:  python3 -m unittest discover -s tests
"""

import argparse
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "orchestrator"))

import support  # noqa: E402
import common  # noqa: E402
import harness  # noqa: E402
import live  # noqa: E402
import matrix  # noqa: E402
import mc as mcmod  # noqa: E402
import peers  # noqa: E402


def setUpModule():
    support.start()


def tearDownModule():
    support.stop()


class TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bl-low-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))


class RamGuardTests(unittest.TestCase):
    """BL-13: free RAM that cannot be measured is unknown, not 100 %."""

    def test_a_failed_reading_is_none(self):
        with mock.patch.object(common.subprocess, "run", side_effect=OSError("no sysctl")):
            self.assertIsNone(common.mem_free_percent())

    def test_unknown_ram_does_not_gate_and_does_not_crash_the_watchdog(self):
        cfg = {"limits": {"min_free_disk_gb": 0, "min_free_ram_percent": 20}}
        with mock.patch.object(common, "mem_free_percent", return_value=None):
            self.assertIsNone(common.resource_problem(cfg, samples=1))
            ctx = object.__new__(harness.Ctx)
            ctx.cfg, ctx._low = cfg, 0
            ctx._watchdog()
            self.assertEqual(ctx._low, 0)

    def test_low_ram_still_gates(self):
        cfg = {"limits": {"min_free_disk_gb": 0, "min_free_ram_percent": 20}}
        with mock.patch.object(common, "mem_free_percent", return_value=5):
            self.assertIn("below", common.resource_problem(cfg, samples=1))


class JavaHomeTests(unittest.TestCase):
    """BL-14: a `~` in java_home is expanded wherever a JDK is started."""

    def test_linkdemo_gets_an_expanded_java(self):
        started = []
        ctx = mock.Mock()
        ctx.start_tool.side_effect = lambda name, argv, **kw: started.append(argv) or mock.Mock()
        peers.start_linkdemo(ctx, {"java_home": "~/jdk", "tools": {"link_java": "~/link-java"}}, "guest", 47901, "linkdemo", 13)
        self.assertEqual(started[0][0], os.path.join(os.path.expanduser("~/jdk"), "bin", "java"))
        self.assertNotIn("~", started[0][0])

    def test_a_missing_jdk_gives_an_empty_version_not_a_crash(self):
        self.assertEqual(live.java_version("/nonexistent/jdk"), "")


class TokenConfigTests(TmpCase):
    """BL-15: an unreadable config/minecraft-link.json is never replaced."""

    def test_a_broken_config_is_refused_and_kept(self):
        cfgdir = self.tmp / "config"
        cfgdir.mkdir()
        (cfgdir / "minecraft-link.json").write_text("{ not json, the developer's own settings")
        with self.assertRaises(ValueError):
            mcmod.set_token_config(str(self.tmp), str(self.tmp / "endpoint.json"))
        self.assertEqual((cfgdir / "minecraft-link.json").read_text(), "{ not json, the developer's own settings")

    def test_other_keys_are_kept(self):
        cfgdir = self.tmp / "config"
        cfgdir.mkdir()
        (cfgdir / "minecraft-link.json").write_text('{"port": 47800}')
        mcmod.set_token_config(str(self.tmp), str(self.tmp / "endpoint.json"))
        data = json.loads((cfgdir / "minecraft-link.json").read_text())
        self.assertEqual(data["port"], 47800)
        self.assertTrue(data["token_auth"])


class KillTests(unittest.TestCase):
    """BL-16: a pid that is not ours (PermissionError) does not stop the others from being ended."""

    def test_kill_continues_after_a_permission_error(self):
        sent = []

        def kill(pid, sig):
            if pid == 111:
                raise PermissionError("not ours")
            sent.append((pid, sig))
        with mock.patch.object(common.os, "kill", kill), mock.patch.object(common, "pid_alive", return_value=False):
            common.kill_pids([111, 222], grace=0.0)
        self.assertEqual(sent, [(222, common.signal.SIGTERM)])


class InterruptedRunTests(TmpCase):
    """BL-18: an interrupted run still compares the real profiles when --check-profiles asked for it."""

    def test_the_manifest_is_compared_on_interrupt(self):
        args = argparse.Namespace(jfr=False, token=False, only=None, skip_gui=False, minecraft_assets=None, check_profiles=True)
        manifests = mock.Mock(side_effect=[{"/x/a": (1, 1)}, {"/x/a": (1, 2)}])
        out = io.StringIO()
        pair = {"id": "fake/pair", "gui": False, "fn": None, "args": (), "needs": []}
        with mock.patch.object(live, "ROOT", self.tmp), mock.patch.object(live, "ensure_tools"), \
                mock.patch.object(live.gameenv, "clear_scratch_root"), mock.patch.object(live.gameenv, "profile_manifest", manifests), \
                mock.patch.object(live.subprocess, "Popen"), mock.patch.object(live.matrix, "all_pairs", return_value=[pair]), \
                mock.patch.object(live, "host_info", return_value={}), mock.patch.object(live, "versions", return_value={}), \
                mock.patch.object(live, "watched_pids", return_value={}), mock.patch.object(live, "run_pair", side_effect=KeyboardInterrupt), \
                mock.patch("sys.stdout", out):
            with self.assertRaises(KeyboardInterrupt):
                live._run(args, {"limits": {}})
        self.assertEqual(manifests.call_count, 2)
        self.assertIn("REAL PROFILES CHANGED", out.getvalue())


class EndpointFileTests(unittest.TestCase):
    """BL-19: an endpoint file with more fields than the two the contract names still passes (unknown fields are ignored)."""

    def ctx(self):
        c = mock.Mock()
        c.checks = []
        c.check.side_effect = lambda name, ok, detail="", gate=True: c.checks.append(bool(ok))
        return c

    def test_extra_fields_pass_and_are_evidence(self):
        c = self.ctx()
        matrix._endpoint_checks(c, "mc host", {"mode": "600", "port": 47901, "token_ok": True, "keys": ["control_port", "pid", "token"]}, 47901)
        self.assertEqual(c.checks, [True])
        self.assertTrue(c.evid.called)

    def test_a_missing_token_fails(self):
        c = self.ctx()
        matrix._endpoint_checks(c, "mc host", {"mode": "600", "port": 47901, "token_ok": False, "keys": ["control_port"]}, 47901)
        self.assertEqual(c.checks, [False])


class MarkerTests(TmpCase):
    """BL-20: only a game's AutoTest output drives the pair; a tool printing the marker text does not."""

    def test_markers_from_a_tool_are_ignored(self):
        ctx = harness.Ctx({"limits": {"min_free_disk_gb": 0, "min_free_ram_percent": 0}}, "p", self.tmp / "logs" / "run" / "p", self.tmp / "shots")
        seen = []
        ctx.handlers["SHOT"] = lambda arg, proc: seen.append((arg, proc.name))
        tool, game = mock.Mock(game=False), mock.Mock(game=True)
        tool.name, game.name = "live-peer", "mc-26.3"
        ctx.events.put((tool, "[live-guest 1.00s] autotest: SHOT fake"))
        ctx.events.put((game, "[Render thread/INFO] (minecraft-link) autotest: SHOT real"))
        with mock.patch.object(common, "ps_table", return_value=[]):
            ctx.pump(0.05)
        self.assertEqual(seen, [("real", "mc-26.3")])


class PruneTests(TmpCase):
    """BL-20: keep_log_runs 0 deletes every partial run's logs (runs[:-0] deleted none)."""

    def test_keep_zero(self):
        for d in ("20260101T000000Z", "20260102T000000Z"):
            (self.tmp / "logs" / d).mkdir(parents=True)
        with mock.patch.object(live, "ROOT", self.tmp), mock.patch.object(live.report, "full_reports", return_value=[]):
            live.prune_logs({"limits": {"keep_log_runs": 0}})
        self.assertEqual(os.listdir(self.tmp / "logs"), [])

    def test_keep_one(self):
        for d in ("20260101T000000Z", "20260102T000000Z"):
            (self.tmp / "logs" / d).mkdir(parents=True)
        with mock.patch.object(live, "ROOT", self.tmp), mock.patch.object(live.report, "full_reports", return_value=[]):
            live.prune_logs({"limits": {"keep_log_runs": 1}})
        self.assertEqual(os.listdir(self.tmp / "logs"), ["20260102T000000Z"])


class JfrFinishTests(unittest.TestCase):
    """BL-20: a client whose launch failed under --jfr reports that, instead of an AttributeError in its finalizer."""

    def test_no_recording_path(self):
        m = object.__new__(mcmod.McClient)
        m.jfr_plan = {"active": True, "reason": ""}
        m.jfr_path = None
        m.cfg = {"run": {"jfr": True}}
        m.label = "26.3-fabric"
        m.ctx = mock.Mock(jfr={})
        m._finish_jfr()
        self.assertEqual(m.jfr_result, {"recorded": False, "reason": "the client was not launched"})


if __name__ == "__main__":
    unittest.main()
