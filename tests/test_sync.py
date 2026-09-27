"""Offline end-to-end test: sync() against a fake LeetCode client in a temp repo."""

import os
import shutil
import subprocess
import time
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import sync

# Day boundaries are local time, so pin the timezone.
os.environ["TZ"] = "UTC"
time.tzset()
DAY1 = 1_767_225_600  # 2026-01-01 00:00 UTC
DAY2 = DAY1 + 86_400
DAY3 = DAY2 + 86_400

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

    def submission_codes(self, submission_ids):
        return {i: self.code[i] for i in submission_ids}

    def questions(self, slugs):
        return {slug: QUESTIONS[slug] for slug in slugs}

    def question_counts(self):
        return {"All": 20, "Easy": 10, "Medium": 5, "Hard": 5}


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

    def git_status(self):
        return subprocess.run(["git", "status", "--porcelain"], cwd=self.repo, check=True,
                              capture_output=True, text=True).stdout

    def git(self, *args, cwd=None):
        subprocess.run(["git", *args], cwd=cwd or self.repo, check=True, capture_output=True)

    def test_one_commit_per_day(self):
        client = FakeLeetCode(
            [sub(5, "lru-cache", DAY3 + 50),
             sub(9, "lru-cache", DAY2 + 500, status="Wrong Answer"),
             sub(4, "two-sum", DAY2 + 100),                 # same code as #1: skipped
             sub(3, "lru-cache", DAY1 + 10_000),
             sub(2, "two-sum", DAY1 + 7_200, lang="cpp"),
             sub(1, "two-sum", DAY1 + 3_600)],
            {"1": "class Solution: pass", "2": "class Solution {};", "3": "class LRUCache: pass",
             "4": "class Solution: pass", "5": "class LRUCache: better"},
        )
        self.assertEqual(sync.sync(self.repo, client, log=lambda *_: None), 2)

        # Day 2 only had an identical resubmission, so it gets no commit.
        self.assertEqual(self.git_log(), [[str(DAY1 + 10_000), "2026-01-01: 0001, 0146"],
                                          [str(DAY3 + 50), "2026-01-03: 0146"]])
        folder = self.repo / "solutions" / "0001-two-sum"
        self.assertEqual((folder / "solution.py").read_text(), "class Solution: pass\n")
        self.assertTrue((folder / "solution.cpp").exists())
        self.assertIn("## Notes", (folder / "README.md").read_text())
        self.assertEqual((self.repo / "solutions/0146-lru-cache/solution.py").read_text(),
                         "class LRUCache: better\n")
        readme = (self.repo / "README.md").read_text()
        self.assertIn("| Easy | `█░░░░░░░░░` 10% | 1 / 10 |", readme)
        self.assertIn("| Medium | `██░░░░░░░░` 20% | 1 / 5 |", readme)
        self.assertIn("| Hard | `░░░░░░░░░░` 0% | 0 / 5 |", readme)
        self.assertIn("| **Total** | `█░░░░░░░░░` 10% | 2 / 20 |", readme)
        self.assertLess(readme.index("| 1 |"), readme.index("| 146 |"))
        self.assertEqual(self.git_status(), "")

        # A later run on the same day folds into that day's (unpushed) commit.
        client.submissions.insert(0, sub(6, "two-sum", DAY3 + 500))
        client.code["6"] = "class Solution: better"
        self.assertEqual(sync.sync(self.repo, client, log=lambda *_: None), 1)
        self.assertEqual(self.git_log()[1:], [[str(DAY3 + 500), "2026-01-03: 0001, 0146"]])

        # Notes survive, and re-running with nothing new changes nothing.
        (folder / "README.md").write_text("my notes\n")
        self.git("commit", "-qam", "notes")
        self.assertEqual(sync.sync(self.repo, client, log=lambda *_: None), 0)
        self.assertEqual((folder / "README.md").read_text(), "my notes\n")

    def test_pushed_day_gets_a_new_commit_instead_of_amending(self):
        remote = Path(self.tmp.name + "-remote.git")
        self.git("init", "-q", "--bare", str(remote), cwd=self.tmp.name + "/..")
        self.addCleanup(shutil.rmtree, remote)
        client = FakeLeetCode([sub(1, "two-sum", DAY1 + 100)], {"1": "a"})
        sync.sync(self.repo, client, log=lambda *_: None)
        self.git("remote", "add", "origin", str(remote))
        self.git("push", "-q", "-u", "origin", "HEAD")

        client.submissions.insert(0, sub(2, "lru-cache", DAY1 + 200))
        client.code["2"] = "b"
        sync.sync(self.repo, client, log=lambda *_: None)
        self.assertEqual([m for _, m in self.git_log()], ["2026-01-01: 0001", "2026-01-01: 0146"])

    def test_classic_lists_table(self):
        lists = Path(self.tmp.name + "-lists")
        lists.mkdir()
        self.addCleanup(shutil.rmtree, lists)
        (lists / "a.txt").write_text("# Tiny 3\n# comment\n\ntwo-sum\nlru-cache\nthree-sum\n")
        (lists / "b.txt").write_text("# Other 1\nthree-sum\n")
        client = FakeLeetCode([sub(1, "two-sum", DAY1), sub(2, "lru-cache", DAY1 + 1)],
                              {"1": "a", "2": "b"})
        with mock.patch.object(sync, "LISTS_DIR", lists):
            sync.sync(self.repo, client, log=lambda *_: None)
        readme = (self.repo / "README.md").read_text()
        self.assertIn("| Tiny 3 | `███████░░░` 67% | 2 / 3 |", readme)
        self.assertIn("| Other 1 | `░░░░░░░░░░` 0% | 0 / 1 |", readme)
        self.assertLess(readme.index("Tiny 3"), readme.index("### Solved problems"))

    def test_index_preserves_surrounding_readme_text(self):
        (self.repo / "README.md").write_text(
            f"# Intro\n\n{sync.INDEX_START}\nold\n{sync.INDEX_END}\n\n## Footer\n")
        client = FakeLeetCode([sub(1, "two-sum", 1_700_000_000)], {"1": "x"})
        sync.sync(self.repo, client, log=lambda *_: None)
        readme = (self.repo / "README.md").read_text()
        self.assertTrue(readme.startswith("# Intro"))
        self.assertTrue(readme.endswith("## Footer\n"))
        self.assertNotIn("old", readme)


class ProgressBarTest(unittest.TestCase):
    def test_rounds_to_nearest_ten_percent(self):
        self.assertEqual(sync.progress_bar(0, 75), "░░░░░░░░░░")
        self.assertEqual(sync.progress_bar(24, 75), "███░░░░░░░")    # 32%
        self.assertEqual(sync.progress_bar(26, 75), "███░░░░░░░")    # 34.7%
        self.assertEqual(sync.progress_bar(27, 75), "████░░░░░░")    # 36%
        self.assertEqual(sync.progress_bar(75, 75), "██████████")
        self.assertEqual(sync.progress_bar(1, 0), "░░░░░░░░░░")

    def test_bundled_lists_have_expected_sizes(self):
        sizes = {name: len(slugs) for name, slugs in sync.load_lists()}
        self.assertEqual(sizes, {"Blind 75": 75, "NeetCode 150": 150, "Top Interview 150": 150})


if __name__ == "__main__":
    unittest.main()
