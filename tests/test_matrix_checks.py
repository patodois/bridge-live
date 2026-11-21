"""The judgement of a pair, with fake clients: what makes a check pass or fail. No game, no peer.

Run:  python3 -m unittest discover -s tests
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "orchestrator"))

import support  # noqa: E402
import matrix  # noqa: E402
import mc as mcmod  # noqa: E402


def setUpModule():
    support.start()


def tearDownModule():
    support.stop()


class FakeCtx:
    def __init__(self, logdir="/nonexistent/logs"):
        self.checks, self.exact, self.rates, self.evidence, self.procs = [], {}, {}, [], []
        self.logdir = Path(logdir)

    def check(self, name, ok, detail="", gate=True):
        self.checks.append({"name": name, "ok": bool(ok), "detail": str(detail), "gate": gate})
        return ok

    def evid(self, text, limit=300):
        self.evidence.append(str(text))

    def result(self, part):
        found = [c for c in self.checks if part in c["name"]]
        assert len(found) == 1, (part, [c["name"] for c in self.checks])
        return found[0]["ok"]


def client(status_lines=(), log_lines=(), run_dir="/nonexistent/run", checkout_run_dir=None):
    """An McClient with only what the judgement reads: its parsed status lines and the lines its log hook saw."""
    m = object.__new__(mcmod.McClient)
    m.proc = None
    m.statuses = []
    m.connected, m.spawned, m.bye, m.lost, m.closed, m.errors = [], [], [], [], [], []
    m.focus_events, m.pos_lines, m.unauthorized_notices, m.status_lines = [], [], [], []
    m.run_dir = run_dir
    m.checkout_run_dir = checkout_run_dir or run_dir
    for raw in status_lines:
        st = mcmod.parse_status(raw)
        st["t"] = float(len(m.statuses))
        m.statuses.append(st)
    for line in log_lines:
        end = mcmod.session_end_kind(line)
        if end:
            (m.bye if end == "bye" else m.lost).append(line)
        elif "link closed (" in line:
            m.closed.append(line)
    return m


CONNECTED = ("host on 127.0.0.1:47901; connected to Live Peer (mc) (session 0a0b0c0d); sent 500 cameras, 3 collision snapshots (800 triangles); "
             "received 400 poses, 300 frames; 1 guest entities; datagrams in/out 900/700, malformed 0, stale 0, foreign 0; frames drawn 290, reprojected 280; "
             "pixel file 320x240 with depth")
CONNECTED_NO_COUNTERS = ("host on 127.0.0.1:47901; connected to Live Peer (mc) (session 0a0b0c0d); sent 500 cameras, 3 collision snapshots (800 triangles); "
                         "received 400 poses, 300 frames; 1 guest entities; frames drawn 290, reprojected 280; pixel file 320x240 with depth")


class ByeTests(unittest.TestCase):
    """BL-07: "the session ended with a bye" passes only on the peer's bye."""

    def test_the_mod_lines_are_classified(self):
        self.assertEqual(mcmod.session_end_kind("[minecraft-link]: link lost: peer said bye: done; 729 cameras"), "bye")
        self.assertEqual(mcmod.session_end_kind("[minecraft-link]: link lost: timed out; 729 cameras"), "lost")
        self.assertEqual(mcmod.session_end_kind("LOST the guest: connection reset"), "lost")
        self.assertIsNone(mcmod.session_end_kind("[minecraft-link]: link closed (729 cameras, 3 collision snapshots, 0 poses sent)"))

    def judge(self, log_lines, peer_ends=True):
        ctx = FakeCtx()
        matrix._mc_host_numbers(ctx, client([CONNECTED], log_lines), frames=False, peer_ends=peer_ends)
        return ctx

    def test_a_peer_bye_passes(self):
        self.assertTrue(self.judge(["link lost: peer said bye: done; 729 cameras"]).result("peer ended the session"))

    def test_a_timeout_is_not_a_bye(self):
        self.assertFalse(self.judge(["link lost: timed out after 5000 ms"]).result("peer ended the session"))

    def test_the_clients_own_link_stop_is_not_the_peers_bye(self):
        self.assertFalse(self.judge(["link closed (729 cameras, 3 collision snapshots, 0 poses sent)"]).result("peer ended the session"))

    def test_when_this_client_ends_the_session_its_own_close_counts(self):
        ctx = self.judge(["link closed (1062 cameras, 4 collision snapshots, 0 poses sent)"], peer_ends=False)
        self.assertTrue(ctx.result("closed the link itself"))
        self.assertFalse(self.judge([], peer_ends=False).result("closed the link itself"))


class AnomalyCounterTests(unittest.TestCase):
    """BL-11: "no malformed and no foreign datagrams" needs a reading of both counters."""

    def test_zero_counters_pass(self):
        self.assertTrue(matrix.no_anomalies(client([CONNECTED]))[0])

    def test_counters_never_read_fail(self):
        ok, detail = matrix.no_anomalies(client([CONNECTED_NO_COUNTERS]))
        self.assertFalse(ok)
        self.assertIn("no connected status line carried the counters", detail)
        self.assertFalse(matrix.no_anomalies(client([]))[0])

    def test_a_malformed_datagram_fails(self):
        self.assertFalse(matrix.no_anomalies(client([CONNECTED.replace("malformed 0", "malformed 2")]))[0])

    def test_the_host_check_uses_it(self):
        ctx = FakeCtx()
        matrix._mc_host_numbers(ctx, client([CONNECTED_NO_COUNTERS], ["link lost: peer said bye: done"]), frames=False)
        self.assertFalse(ctx.result("no malformed and no foreign"))


class TokenScanTests(unittest.TestCase):
    """BL-08: the token scan covers the checkout's run/logs that Forge and NeoForge write with a scratch instance."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bl-token-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_a_token_in_the_checkouts_debug_log_is_found(self):
        token = "ab" * 32
        instance, checkout_run = self.tmp / "scratch" / "instance", self.tmp / "checkout" / "neoforge" / "run"
        (instance / "logs").mkdir(parents=True)
        (instance / "logs" / "latest.log").write_text("nothing here\n")
        (checkout_run / "logs").mkdir(parents=True)
        (checkout_run / "logs" / "debug.log").write_text("DEBUG token=%s\n" % token)
        (self.tmp / "pairlogs").mkdir()
        ctx = FakeCtx(self.tmp / "pairlogs")
        m = client(run_dir=str(instance), checkout_run_dir=str(checkout_run))
        self.assertIn(checkout_run / "logs", matrix.token_scan_dirs(ctx, [m]))
        watch = matrix.TokenWatch()
        watch.add(token)
        watch.scan(ctx, [m])
        self.assertFalse(ctx.result("no token appears"))
        self.assertIn("debug.log", ctx.checks[-1]["detail"])

    def test_a_clean_pair_passes(self):
        (self.tmp / "pairlogs").mkdir()
        ctx = FakeCtx(self.tmp / "pairlogs")
        watch = matrix.TokenWatch()
        watch.add("cd" * 32)
        watch.scan(ctx, [client(run_dir=str(self.tmp / "none"))])
        self.assertTrue(ctx.result("no token appears"))


if __name__ == "__main__":
    unittest.main()
