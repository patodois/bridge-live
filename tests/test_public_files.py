"""What the repository tracks must be fit to publish: no run report, no user's home folder, no machine-local file.

Reads `git ls-files` (skipped outside a Git checkout). The public-tree gate (tools/check_public_tree.py) covers game files,
decompiled code and secrets; this covers what is particular to a harness that runs on the author's machine.

Run:  python3 -m unittest discover -s tests
"""

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Made-up user names that tests use as fixtures (a path to scrub, a secret that must not leak).
FIXTURE_USERS = {"someone", "x", "zz-secret-user", "alice", "bob", "carol", "dave", "runner"}
HOME_RE = re.compile(r"(?:/Users/|/home/|[A-Za-z]:\\\\Users\\\\)([A-Za-z0-9._-]+)")


def tracked():
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return [p for p in out.stdout.decode("utf-8", "replace").split("\0") if p]


class PublicFilesTests(unittest.TestCase):
    def setUp(self):
        self.files = tracked()
        if self.files is None:
            self.skipTest("not a Git checkout")

    def test_no_run_output_is_tracked(self):
        bad = [p for p in self.files if p.split("/")[0] in ("reports", "logs", "work") or p == "config.local.json"]
        self.assertEqual(bad, [], "run reports, logs, work/ and config.local.json stay local (.gitignore)")

    def test_no_tracked_file_names_a_real_home_folder(self):
        hits = []
        for p in self.files:
            path = ROOT / p
            if not path.is_file():
                continue
            text = path.read_bytes().decode("utf-8", "replace")
            for m in HOME_RE.finditer(text):
                if m.group(1) not in FIXTURE_USERS:
                    hits.append("%s: %s" % (p, m.group(0)))
        self.assertEqual(hits, [], "a user's home folder in a tracked file (use ~ or a fixture name)")

    def test_licenses_and_agent_guide_are_present(self):
        for name in ("LICENSE-MIT", "LICENSE-APACHE", "AGENTS.md", "README.md", "tools/check_public_tree.py"):
            self.assertIn(name, self.files)


class WorkflowTests(unittest.TestCase):
    """CI runs the unit tests and the public-tree gate, with actions pinned to full commits."""

    def setUp(self):
        self.ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()

    def test_ci_runs_the_tests_and_the_gate(self):
        for line in ("- run: python3 -m unittest discover -s tests",
                     "- run: python3 -m unittest discover -s tools -p 'test_*.py'",
                     "python3 tools/check_public_tree.py --range"):
            self.assertIn(line, self.ci)
        self.assertNotIn("continue-on-error", self.ci)
        self.assertNotRegex(self.ci, r"(?m)^\s+if:")

    def test_actions_are_pinned_to_full_commits(self):
        uses = re.findall(r"(?m)^\s*(?:- )?uses: (\S+)(.*)$", self.ci)
        self.assertTrue(uses)
        for action, rest in uses:
            self.assertRegex(action, r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
            self.assertRegex(rest, r"^ +# +\S+")

    def test_the_workflow_only_reads_the_repository(self):
        self.assertRegex(self.ci, r"(?m)^permissions:\n  contents: read$")
        self.assertNotIn("write", self.ci.replace("contents: read", ""))


if __name__ == "__main__":
    unittest.main()
