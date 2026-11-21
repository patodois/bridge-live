"""Two `./live` runs must not be inside the matrix at the same time.

The matrix uses fixed scratch folders, ports and a test server, and clears the scratch root at its start as the leftover of a run
that died. This test drives the real `cmd_run` with `_run` replaced by a probe: no game, no process, no scratch folder is touched.

Run:  python3 -m unittest discover -s tests
"""

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "orchestrator"))

import support  # noqa: E402
import live  # noqa: E402


def setUpModule():
    support.start()


def tearDownModule():
    support.stop()


class RunLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patches = [
            mock.patch.object(live, "ROOT", Path(self.tmp.name)),
            mock.patch.object(live.common, "load_config", return_value={}),
            mock.patch("signal.signal"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_a_second_start_while_a_run_is_inside_is_refused(self):
        inside, release = threading.Event(), threading.Event()
        entered = []

        def fake_run(args, cfg):
            entered.append(threading.current_thread().name)
            inside.set()
            release.wait(10)
            return 0

        results = {}
        with mock.patch.object(live, "_run", fake_run):
            first = threading.Thread(target=lambda: results.update(first=live.cmd_run(None)), name="first")
            first.start()
            self.assertTrue(inside.wait(10))
            # The first run is in the matrix: the second start must not get in, whatever the timing
            results["second"] = live.cmd_run(None)
            release.set()
            first.join(10)
        self.assertEqual(entered, ["first"])
        self.assertEqual(results, {"first": 0, "second": 2})

    def test_the_refused_run_does_not_free_the_lock_of_the_run_inside(self):
        inside, release = threading.Event(), threading.Event()

        def fake_run(args, cfg):
            inside.set()
            release.wait(10)
            return 0

        entered_third = []
        with mock.patch.object(live, "_run", fake_run):
            first = threading.Thread(target=lambda: live.cmd_run(None))
            first.start()
            self.assertTrue(inside.wait(10))
            self.assertEqual(live.cmd_run(None), 2)
            # What the finally of the refused start did must not let a third one in
            with mock.patch.object(live, "_run", lambda a, c: entered_third.append(1) or 0):
                self.assertEqual(live.cmd_run(None), 2)
            release.set()
            first.join(10)
        self.assertEqual(entered_third, [])

    def test_two_starts_at_the_same_instant_do_not_both_enter(self):
        # Holds both callers at the file-exists check, if the code makes one, until each has looked: the
        # interleaving that a check-then-write lock cannot survive
        gate = threading.Barrier(2)
        real_exists = Path.exists

        def exists(path):
            if path.name == "run.lock":
                try:
                    gate.wait(2)
                except threading.BrokenBarrierError:
                    pass
            return real_exists(path)

        lock = threading.Lock()
        now, most, entered = [0], [0], []

        def fake_run(args, cfg):
            with lock:
                now[0] += 1
                most[0] = max(most[0], now[0])
                entered.append(1)
            threading.Event().wait(0.5)
            with lock:
                now[0] -= 1
            return 0

        results = []
        with mock.patch.object(Path, "exists", exists), mock.patch.object(live, "_run", fake_run):
            threads = [threading.Thread(target=lambda: results.append(live.cmd_run(None))) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        self.assertEqual(most[0], 1, "two runs were inside the matrix together")
        self.assertEqual(sorted(results), [0, 2])

    def test_clean_does_not_delete_the_folders_of_a_run_in_progress(self):
        base = Path(self.tmp.name) / "work"
        scratch = base / "scratch"
        scratch.mkdir(parents=True)
        (scratch / "in-use").write_text("x")
        held = live.acquire_run_lock(base / "run.lock")
        self.assertNotIsInstance(held, int)
        self.assertEqual(live.cmd_clean(None), 2)
        self.assertTrue((scratch / "in-use").exists(), "clean deleted the scratch of a running matrix")
        held.close()
        # With no run, clean works, and takes the lock only while it deletes
        self.assertEqual(live.cmd_clean(None), 0)
        self.assertFalse(scratch.exists())
        self.assertNotIsInstance(live.acquire_run_lock(base / "run.lock"), int)

    def test_the_lock_is_free_again_after_a_run_ends(self):
        with mock.patch.object(live, "_run", lambda a, c: 0):
            self.assertEqual(live.cmd_run(None), 0)
            self.assertEqual(live.cmd_run(None), 0)

    def test_a_lock_file_left_by_a_dead_run_does_not_block(self):
        (Path(self.tmp.name) / "work").mkdir()
        (Path(self.tmp.name) / "work" / "run.lock").write_text("999999")
        with mock.patch.object(live, "_run", lambda a, c: 0):
            self.assertEqual(live.cmd_run(None), 0)


if __name__ == "__main__":
    unittest.main()
