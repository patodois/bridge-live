"""tools/JfrSummary.java against a real recording made by the JDK of config.json.

The test compiles a small program (named threads, allocation, System.gc(), a "mod" package with a hot method), records it with
the very arguments `./live --jfr` gives a game (orchestrator/jfr.py), summarises it, and checks the numbers against `jfr print`
and that nothing private came out: the program is started with a secret in its command line, a system property and the
environment, and with a user path in a thread name.

Skipped when the JDK of config.json (or its javac/jfr) is missing.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "orchestrator"))

import support  # noqa: E402
import common  # noqa: E402
import jfr as jfrmod  # noqa: E402


def setUpModule():
    support.start()


def tearDownModule():
    support.stop()

CFG = json.loads((ROOT / "config.json").read_text())
JAVA_HOME = common.expand(CFG["java_home"])
JAVAC, JAVA, JFR = (os.path.join(JAVA_HOME, "bin", x) for x in ("javac", "java", "jfr"))

SECRET_ARG = "zz-cmdline-secret-7731"
SECRET_PROP = "/Users/zz-secret-user/private/dir"
SECRET_ENV = "zz-env-secret-4410"

PROGRAM = {
    "probe/mod/Hot.java": """
package probe.mod;
public final class Hot {
    static volatile long sink;
    public static void spin(long millis) {
        long end = System.nanoTime() + millis * 1_000_000L;
        while (System.nanoTime() < end) {
            for (int i = 0; i < 20_000; i++) sink += Math.sqrt(i) > 3 ? i : -i;
            Cold.grow();
        }
    }
}
""",
    "probe/mod/Cold.java": """
package probe.mod;
import java.util.ArrayList;
import java.util.List;
final class Cold {
    static final List<byte[]> keep = new ArrayList<>();
    static void grow() {
        keep.add(new byte[4096]);
        if (keep.size() > 4000) keep.clear();
    }
}
""",
    "probe/Main.java": """
package probe;
public final class Main {
    public static void main(String[] args) throws Exception {
        Thread busy = new Thread(() -> probe.mod.Hot.spin(3500), "burn-worker");
        // a thread named after a path must not leak the path; it works for 1.5 s, so it is in the summary's top lists
        Thread odd = new Thread(() -> {
            long end = System.nanoTime() + 1_500_000_000L;
            java.util.List<byte[]> keep = new java.util.ArrayList<>();
            while (System.nanoTime() < end) { keep.add(new byte[2048]); if (keep.size() > 2000) keep.clear(); }
        }, "reader of /Users/zz-secret-user/private/dir/file");
        busy.start();
        odd.start();
        Thread.sleep(1200);
        System.gc();
        Thread.sleep(1200);
        System.gc();
        busy.join();
        odd.join();
        System.out.println("done " + args.length + " " + System.getProperty("secret.prop") + " " + System.getenv("ZZ_SECRET_ENV"));
    }
}
""",
}


def tool(path):
    return os.path.isfile(path)


@unittest.skipUnless(tool(JAVAC) and tool(JAVA) and tool(JFR), "needs the JDK of config.json (javac, java, jfr)")
class JfrSummaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="bl-jfr-")).resolve()
        src = cls.tmp / "src"
        for rel, text in PROGRAM.items():
            f = src / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
        classes = cls.tmp / "classes"
        r = subprocess.run([JAVAC, "--release", "17", "-d", str(classes)] + [str(src / rel) for rel in PROGRAM], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        cls.recording = str(cls.tmp / "probe.jfr")
        env = dict(os.environ, ZZ_SECRET_ENV=SECRET_ENV)
        argv = [JAVA] + jfrmod.jvm_args(CFG, cls.recording) + ["-Dsecret.prop=" + SECRET_PROP, "-cp", str(classes), "probe.Main", SECRET_ARG]
        r = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=120)
        assert r.returncode == 0 and os.path.getsize(cls.recording) > 0, r.stdout + r.stderr
        cls.out_json = str(cls.tmp / "summary.json")
        cfg = json.loads(json.dumps(CFG))
        cfg["jfr"] = dict(CFG.get("jfr", {}), packages=["probe.mod"], top=10)
        cls.summary, detail = jfrmod.summarise(cfg, cls.recording, cls.out_json)
        assert cls.summary is not None, detail
        cls.text = Path(cls.out_json).read_text()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def jfr_print(self, event):
        return subprocess.run([JFR, "print", "--events", event, self.recording], capture_output=True, text=True).stdout

    def test_nothing_private_leaves_the_recording(self):
        raw = subprocess.run([JFR, "print", "--events", "jdk.JVMInformation,jdk.InitialEnvironmentVariable,jdk.InitialSystemProperty", self.recording],
                             capture_output=True, text=True).stdout
        # the recording itself does hold the secrets (that is why it stays private); the summary must not
        self.assertIn(SECRET_ARG, raw)
        self.assertIn(SECRET_ENV, raw)
        for secret in (SECRET_ARG, SECRET_ENV, "zz-secret-user", "/Users/", str(self.tmp)):
            self.assertNotIn(secret, self.text, secret)
        self.assertNotIn(os.path.expanduser("~"), self.text)
        self.assertNotIn("javaArguments", self.text)

    def test_a_path_in_a_thread_name_is_scrubbed(self):
        names = [t["thread"] for t in self.summary["cpu"]["top_threads"] + self.summary["allocation"]["top_threads"]]
        self.assertIn("reader of ~/private/dir/file", names)          # the thread is listed, under its scrubbed name
        self.assertEqual([n for n in names if "zz-secret" in n or "/Users/" in n], [])

    def test_the_busy_thread_has_cpu_time_and_allocation(self):
        cpu = {t["thread"]: t for t in self.summary["cpu"]["top_threads"]}
        self.assertIn("burn-worker", cpu)
        self.assertGreater(cpu["burn-worker"]["cpu_seconds"], 1.5)       # it spun for 3.5 s on one core
        self.assertLess(cpu["burn-worker"]["cpu_seconds"], 5.0)
        self.assertGreater(cpu["burn-worker"]["peak_cores"], 0.5)
        self.assertEqual(self.summary["cpu"]["top_threads"][0]["thread"], "burn-worker")
        alloc = {t["thread"]: t for t in self.summary["allocation"]["top_threads"]}
        self.assertIn("burn-worker", alloc)
        self.assertGreater(alloc["burn-worker"]["bytes"], 50_000_000)    # 4 KB per loop for 3.5 s
        self.assertGreater(alloc["burn-worker"]["bytes_per_second"], 10_000_000)

    def test_hardware_threads_match_the_recording(self):
        info = self.jfr_print("jdk.CPUInformation")
        hw = int(re.search(r"hwThreads = (\d+)", info).group(1))
        self.assertEqual(self.summary["cpu"]["hw_threads"], hw)

    def test_gc_numbers_match_jfr_print(self):
        printed = self.jfr_print("jdk.GarbageCollection")
        count = len(re.findall(r"^jdk\.GarbageCollection \{", printed, re.M))
        self.assertGreaterEqual(count, 2)    # two System.gc()
        self.assertEqual(self.summary["gc"]["collections"], count)
        self.assertGreater(self.summary["gc"]["pause_total_ms"], 0)
        self.assertGreaterEqual(self.summary["gc"]["pause_total_ms"], self.summary["gc"]["pause_longest_ms"])
        self.assertTrue(self.summary["gc"]["heap_used_peak_bytes"])

    def test_the_mod_package_hot_method_is_found(self):
        ex = self.summary["mod"]["execution"]
        self.assertGreater(ex["samples_with_a_mod_frame"], 20)
        self.assertIn(ex["top_innermost_mod_frame"][0]["method"], ("probe.mod.Hot.spin", "probe.mod.Cold.grow"))   # grow() is called from spin()
        self.assertEqual(ex["top_inclusive"][0]["method"], "probe.mod.Hot.spin")
        self.assertIn("probe.mod.Hot.spin", [m["method"] for m in ex["top_inclusive"]])
        # only methods of the package are listed
        for key in ("top_self", "top_innermost_mod_frame", "top_inclusive"):
            for m in ex[key]:
                self.assertTrue(m["method"].startswith("probe.mod."), m)
        alloc = self.summary["mod"]["allocation_samples"]
        self.assertTrue(any(m["method"] == "probe.mod.Cold.grow" for m in alloc["top_innermost_mod_frame"]))

    def test_no_package_given_means_no_mod_rows(self):
        out = self.tmp / "nomod.json"
        cfg = json.loads(json.dumps(CFG))
        cfg["jfr"] = dict(CFG.get("jfr", {}), packages=[])
        s, _ = jfrmod.summarise(cfg, self.recording, str(out))
        self.assertEqual(s["mod"]["execution"]["samples_with_a_mod_frame"], 0)
        self.assertEqual(s["mod"]["execution"]["top_inclusive"], [])

    def test_shape_and_headline(self):
        for k in ("schema", "jvm", "events_read", "event_span_seconds", "cpu", "allocation", "gc", "mod", "notes"):
            self.assertIn(k, self.summary)
        h = jfrmod.headline(self.summary)
        self.assertEqual(h["cpu_top_threads"][0][0], "burn-worker")
        self.assertEqual(h["gc_collections"], self.summary["gc"]["collections"])
        json.dumps(h)

    def test_a_missing_or_empty_recording_is_reported_not_raised(self):
        s, detail = jfrmod.summarise(CFG, str(self.tmp / "nope.jfr"), str(self.tmp / "x.json"))
        self.assertIsNone(s)
        self.assertIn("no recording", detail)
        empty = self.tmp / "empty.jfr"
        empty.write_bytes(b"")
        s, detail = jfrmod.summarise(CFG, str(empty), str(self.tmp / "x.json"))
        self.assertIsNone(s)

    def test_a_corrupt_recording_is_reported(self):
        bad = self.tmp / "bad.jfr"
        bad.write_bytes(b"not a flight recording" * 10)
        s, detail = jfrmod.summarise(CFG, str(bad), str(self.tmp / "x.json"))
        self.assertIsNone(s)
        self.assertIn("failed", detail)


if __name__ == "__main__":
    unittest.main()
