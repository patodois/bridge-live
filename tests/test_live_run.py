"""The run itself, with fakes: no game, no peer, no build. What a run reads, how a pair's status is settled, the exit code.

Run:  python3 -m unittest discover -s tests
"""

import argparse
import os
import re
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
import gameenv  # noqa: E402
import live  # noqa: E402
import report  # noqa: E402


def setUpModule():
    support.start()


def tearDownModule():
    support.stop()


def run_args(**kw):
    a = dict(jfr=False, token=False, only=None, skip_gui=False, minecraft_assets=None, check_profiles=False)
    a.update(kw)
    return argparse.Namespace(**a)


class RunReadsNoProfileByDefaultTests(unittest.TestCase):
    """`_run` up to the point where it would start pairs: which real folders it looks at."""

    def run_until_pairs(self, args):
        manifest = mock.Mock(return_value={})
        with mock.patch.object(live, "ensure_tools"), mock.patch.object(live.gameenv, "clear_scratch_root"), \
                mock.patch.object(live.gameenv, "profile_manifest", manifest), mock.patch.object(live.subprocess, "Popen"), \
                mock.patch.object(live.matrix, "all_pairs", return_value=[]), mock.patch.object(live.matrix, "token_pairs", return_value=[]):
            rc = live._run(args, {"limits": {}})
        self.assertEqual(rc, 2)    # "no pair matches": the run stops before any pair
        return manifest

    def test_the_profile_manifest_is_not_taken_by_default(self):
        self.assertEqual(self.run_until_pairs(run_args()).call_count, 0)

    def test_the_profile_manifest_is_taken_when_asked_for(self):
        self.assertEqual(self.run_until_pairs(run_args(check_profiles=True)).call_count, 1)

    def test_a_named_asset_folder_that_does_not_exist_stops_the_run_before_anything(self):
        with mock.patch.object(live, "ensure_tools", side_effect=AssertionError("built before the check")):
            self.assertEqual(live._run(run_args(minecraft_assets="/nonexistent/assets"), {"limits": {}}), 2)

    def test_the_summary_says_when_profiles_were_not_listed(self):
        rep = {"stamp": "x", "full": False, "host": {}, "summary": {}, "pairs": {}, "profiles": None}
        self.assertIn("not listed", report.summary_text(rep, []))


class ExitCodeTests(unittest.TestCase):
    """The exit status the README promises (BL-02): 0 only when every pair ran and passed."""

    def test_every_pair_passed(self):
        self.assertEqual(live.exit_code({"pass": 50}, []), 0)

    def test_a_failure_or_an_alert_is_1(self):
        for counts in ({"pass": 3, "fail": 1}, {"pass": 3, "error": 1}, {"pass": 3, "aborted": 1}, {"fail": 1, "not_available": 4}):
            self.assertEqual(live.exit_code(counts, []), 1, counts)
        self.assertEqual(live.exit_code({"pass": 3}, ["RATE x fell"]), 1)

    def test_pairs_that_did_not_run_are_never_a_pass(self):
        self.assertEqual(live.exit_code({"not_available": 50}, []), 3)
        self.assertEqual(live.exit_code({"pass": 49, "not_available": 1}, []), 3)
        self.assertEqual(live.exit_code({"pass": 49, "skipped": 1}, []), 3)
        self.assertEqual(live.exit_code({"skipped": 50}, []), 3)

    def test_no_pair_passed_is_not_0(self):
        self.assertNotEqual(live.exit_code({}, []), 0)

    def test_an_unknown_status_is_not_a_pass(self):
        self.assertNotEqual(live.exit_code({"pass": 3, "weird": 1}, []), 0)


class PairStatusTests(unittest.TestCase):
    """run_pair with a fake scenario: how the screen state settles a pair's status (BL-03)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bl-pair-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.cfg = {"limits": {"min_free_disk_gb": 0, "min_free_ram_percent": 0}}
        self.ran = []

    def run_pair(self, fn, screen, gui=True):
        pair = {"id": "fake/pair", "gui": gui, "fn": fn, "args": (), "needs": []}
        with mock.patch.object(live, "availability", return_value=None), mock.patch.object(live.common, "resource_problem", return_value=None), \
                mock.patch.object(live.common, "screen_unavailable", side_effect=screen):
            rec, ctx = live.run_pair(self.cfg, pair, str(self.tmp / "logs"), str(self.tmp / "shots"), set())
        return rec, ctx

    def failing(self, ctx, cfg):
        self.ran.append(1)
        ctx.check("protocol: peer connected", False, "never connected")

    def raising(self, ctx, cfg):
        self.ran.append(1)
        raise ValueError("boom")

    def test_a_failed_pair_stays_failed_when_the_screen_locks_afterwards(self):
        rec, ctx = self.run_pair(self.failing, [None, "the screen is locked"])
        self.assertEqual(rec["status"], "fail")
        self.assertTrue(any("the screen is locked" in n for n in ctx.notes), ctx.notes)

    def test_an_error_stays_an_error_when_the_screen_state_cannot_be_read(self):
        rec, _ctx = self.run_pair(self.raising, [None, "the screen state could not be read (no helper)"])
        self.assertEqual(rec["status"], "error")

    def test_a_pair_not_started_because_of_the_screen_is_skipped(self):
        rec, _ctx = self.run_pair(self.failing, ["the screen is locked"])
        self.assertEqual(rec["status"], "skipped")
        self.assertEqual(self.ran, [])

    def test_a_passing_pair_passes(self):
        rec, _ctx = self.run_pair(lambda ctx, cfg: ctx.check("ok", True), [None, None])
        self.assertEqual(rec["status"], "pass")


class PeerBuildTests(unittest.TestCase):
    """live_peer is built only as peer/Cargo.lock pins it (BL-05, BL-06)."""

    def test_a_failed_locked_build_is_an_error_and_never_retried_unlocked(self):
        calls = []

        def sh(argv, **kw):
            calls.append(list(argv))
            return mock.Mock(returncode=101, stderr="error: the lock file needs to be updated but --locked was passed", stdout="")
        with mock.patch.object(live, "sh", sh):
            with self.assertRaises(RuntimeError):
                live.ensure_tools({})
        cargo = [c for c in calls if c[0] == "cargo"]
        self.assertEqual(len(cargo), 1, cargo)
        self.assertIn("--locked", cargo[0])

    def test_the_report_takes_the_rewrite_bridge_pin_from_the_lock(self):
        got = live.locked_package(ROOT / "peer" / "Cargo.lock", "rewrite-bridge")
        self.assertIsNotNone(got)
        manifest = (ROOT / "peer" / "Cargo.toml").read_text()
        rev = re.search(r'rewrite-bridge = \{[^}]*rev = "([0-9a-f]{40})"', manifest).group(1)
        self.assertEqual(got["source"], "git+https://github.com/patodois/rewrite-bridge?rev=%s#%s" % (rev, rev),
                         "peer/Cargo.toml and peer/Cargo.lock must pin the same rewrite-bridge commit")
        self.assertEqual(got["version"], "0.4.0")

    def test_versions_reports_what_the_lock_says(self):
        cfg = {"repos": [], "tools": {"bridge_guest": "/nonexistent/bg", "guest_compose_install": "/nonexistent/gc", "guest_compose_pin": "pin"},
               "hytale": {"package": "/nonexistent/pkg", "plugin_jar": "/nonexistent/p.jar", "server_dir": "work/hytale-server"}}
        with mock.patch.object(live, "locked_package", return_value={"version": "9.9.9", "source": "s"}):
            v = live.versions(cfg)
        self.assertEqual(list(v["crates"].values()), [{"version": "9.9.9", "source": "s"}, "pin"])

    def test_locked_package_of_a_missing_lock_or_package_is_none(self):
        self.assertIsNone(live.locked_package("/nonexistent/Cargo.lock", "x"))
        self.assertIsNone(live.locked_package(ROOT / "peer" / "Cargo.lock", "no-such-crate"))


class ReportPathTests(unittest.TestCase):
    """Reports name files relative to this repository and never a user's home folder (BL-01, BL-12)."""

    HOME = "/Users/alice"

    def test_paths_under_the_repository_become_relative(self):
        root = str(live.ROOT)
        self.assertEqual(live.common.scrub_text(root + "/logs/20260101T000000Z/shots/a.png", homes=[self.HOME]), "logs/20260101T000000Z/shots/a.png")
        self.assertEqual(live.common.scrub_text("cwd " + root, homes=[self.HOME]), "cwd .")
        # a sibling folder that merely shares the prefix is not the repository
        self.assertEqual(live.common.scrub_text("/srv/bl-other/x and /srv/bl", root="/srv/bl", homes=[]), "/srv/bl-other/x and .")

    def test_home_folders_become_a_tilde(self):
        st = live.common.scrub_text
        self.assertEqual(st("at /Users/alice/.gradle/caches/x.jar line 3", root="/nowhere", homes=[self.HOME]), "at ~/.gradle/caches/x.jar line 3")
        self.assertEqual(st("/Users/bob/thing and /home/carol/x and C:\\Users\\dave\\y", root="/nowhere", homes=[]), "~/thing and ~/x and ~\\y")
        self.assertEqual(st("/Users/alice", root="/nowhere", homes=[self.HOME]), "~")

    def test_the_written_report_and_summary_hold_no_home_path(self):
        tmp = Path(tempfile.mkdtemp(prefix="bl-report-"))
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        home = str(support.fake_home())
        rep = {"pairs": {"p": {"shots": [{"path": str(live.ROOT / "logs" / "s" / "shots" / "x.png")}],
                               "evidence": ["mc ERROR: at %s/.gradle/caches/modules/a.jar" % home],
                               "notes": ["could not remove %s/Library/x" % home]}},
               "leftovers": {"others": {123: "/Users/someone/jdk/bin/java"}}}
        with mock.patch.object(live.common, "account_homes", return_value={home}):
            text = live.write_report(rep, "report of %s/bridge-live" % home, tmp / "r.json", tmp / "r.txt")
        written = (tmp / "r.json").read_text() + (tmp / "r.txt").read_text()
        self.assertNotIn(home, written)
        self.assertNotIn("/Users/", written)
        self.assertIn('"logs/s/shots/x.png"', written)
        self.assertIn("~/.gradle/caches/modules/a.jar", written)
        self.assertEqual(text, (tmp / "r.txt").read_text())

    def test_profile_examples_are_home_relative(self):
        home = str(support.fake_home())
        rec, alert = live.profiles_report({}, {home + "/Library/Application Support/x/f": (1, 1)})
        self.assertEqual(rec["examples"], ["~/Library/Application Support/x/f"])
        self.assertNotIn(home, alert)


if __name__ == "__main__":
    unittest.main()
