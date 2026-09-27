"""Offline end-to-end test: sync() against a fake LeetCode client in a temp repo."""

import subprocess
import tempfile
import unittest
from pathlib import Path

import sync

QUESTIONS = {
    "two-sum": {"questionFrontendId": "1", "title": "Two Sum", "difficulty": "Easy",
                "topicTags": [{"name": "Array"}, {"name": "Hash Table"}]},
    "lru-cache": {"questionFrontendId": "146", "title": "LRU Cache", "difficulty": "Medium",
                  "topicTags": [{"name": "Design"}]},
}


def sub(id, slug, ts, status="Accepted", lang="python3"):
    return {"id": str(id), "lang": lang, "timestamp": str(ts), "statusDisplay": status,
            "runtime": "50 ms", "memory": "17 MB", "title": QUESTIONS[slug]["title"],
            "titleSlug": slug}


class FakeLeetCode:
    def __init__(self, submissions, code):
        self.submissions = submissions  # newest first, like the real API
        self.code = code

    def username(self):
        return "tester"

    def accepted_submissions(self, since):
        return [s for s in self.submissions
                if int(s["timestamp"]) > since and s["statusDisplay"] == "Accepted"]

    def submission_code(self, submission_id):
        return {"code": self.code[submission_id]}

    def question(self, slug):
        return QUESTIONS[slug]


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        for args in (["init", "-q"], ["config", "user.email", "t@example.com"],
                     ["config", "user.name", "Tester"]):
            subprocess.run(["git", *args], cwd=self.repo, check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def git_log(self):
        out = subprocess.run(["git", "log", "--format=%at|%s", "--reverse"], cwd=self.repo,
                             check=True, capture_output=True, text=True).stdout
        return [line.split("|", 1) for line in out.splitlines()]

    def test_backfill_then_incremental(self):
        client = FakeLeetCode(
            [sub(4, "two-sum", 1_700_300_000),              # same code as #1: skipped
             sub(3, "lru-cache", 1_700_200_000),
             sub(9, "lru-cache", 1_700_150_000, status="Wrong Answer"),
             sub(2, "two-sum", 1_700_100_000, lang="cpp"),
             sub(1, "two-sum", 1_700_000_000)],
            {"1": "class Solution: pass", "2": "class Solution {};",
             "3": "class LRUCache: pass", "4": "class Solution: pass"},
        )
        sync.sync(self.repo, client, log=lambda *_: None)

        log = self.git_log()
        self.assertEqual([int(ts) for ts, _ in log[:3]], [1_700_000_000, 1_700_100_000, 1_700_200_000])
        self.assertEqual(log[0][1], "Solve 1. Two Sum (Easy) · python3 · 50 ms, 17 MB")
        self.assertEqual(log[-1][1], "Update solutions index")
        self.assertEqual(len(log), 4)

        folder = self.repo / "solutions" / "0001-two-sum"
        self.assertEqual((folder / "solution.py").read_text(), "class Solution: pass\n")
        self.assertTrue((folder / "solution.cpp").exists())
        self.assertIn("## Notes", (folder / "README.md").read_text())

        readme = (self.repo / "README.md").read_text()
        self.assertIn("**2 solved** · Easy: 1 · Medium: 1 · Hard: 0", readme)
        self.assertLess(readme.index("| 1 |"), readme.index("| 146 |"))

        # Notes survive, re-running is a no-op, and a new attempt is an "Update" commit.
        (folder / "README.md").write_text("my notes\n")
        subprocess.run(["git", "commit", "-qam", "notes"], cwd=self.repo, check=True)
        self.assertEqual(sync.sync(self.repo, client, log=lambda *_: None), 0)

        client.submissions.insert(0, sub(5, "two-sum", 1_700_400_000))
        client.code["5"] = "class Solution: better"
        sync.sync(self.repo, client, log=lambda *_: None)
        self.assertEqual(self.git_log()[-2][1], "Update 1. Two Sum (Easy) · python3 · 50 ms, 17 MB")
        self.assertEqual((folder / "README.md").read_text(), "my notes\n")

    def test_index_preserves_surrounding_readme_text(self):
        (self.repo / "README.md").write_text(
            f"# Intro\n\n{sync.INDEX_START}\nold\n{sync.INDEX_END}\n\n## Footer\n")
        client = FakeLeetCode([sub(1, "two-sum", 1_700_000_000)], {"1": "x"})
        sync.sync(self.repo, client, log=lambda *_: None)
        readme = (self.repo / "README.md").read_text()
        self.assertTrue(readme.startswith("# Intro"))
        self.assertTrue(readme.endswith("## Footer\n"))
        self.assertNotIn("old", readme)


if __name__ == "__main__":
    unittest.main()
