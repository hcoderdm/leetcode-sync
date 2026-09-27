#!/usr/bin/env python3
"""Sync accepted LeetCode submissions into this git repo.

Each accepted submission becomes its own commit, dated to when you submitted it.
The solution file always holds your latest attempt; `git log` holds every attempt,
and GitHub's contribution graph reflects the days you actually solved things.

Auth: set LEETCODE_SESSION and LEETCODE_CSRF_TOKEN (env vars or a .env file next
to this script). Both come from your browser's leetcode.com cookies.
"""

import argparse
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

GRAPHQL_URL = "https://leetcode.com/graphql/"
STATE_FILE = ".sync_state.json"
SOLUTIONS_DIR = "solutions"
INDEX_START = "<!-- leetcode-sync:start -->"
INDEX_END = "<!-- leetcode-sync:end -->"
REQUEST_DELAY = 1.0  # seconds between requests; LeetCode rate-limits aggressively
PAGE_SIZE = 20

EXTENSIONS = {
    "python": "py", "python3": "py", "pythondata": "py", "cpp": "cpp", "c": "c",
    "java": "java", "csharp": "cs", "javascript": "js", "typescript": "ts",
    "golang": "go", "rust": "rs", "kotlin": "kt", "swift": "swift", "ruby": "rb",
    "scala": "scala", "php": "php", "dart": "dart", "racket": "rkt",
    "erlang": "erl", "elixir": "ex", "bash": "sh", "mysql": "sql", "mssql": "sql",
    "oraclesql": "sql", "postgresql": "sql",
}
DIFFICULTY_ORDER = ["Easy", "Medium", "Hard"]


class AuthError(Exception):
    pass


class LeetCode:
    """Minimal client for LeetCode's (undocumented) GraphQL API."""

    def __init__(self, session, csrf_token):
        self.headers = {
            "Content-Type": "application/json",
            "Origin": "https://leetcode.com",
            "Referer": "https://leetcode.com/",
            "Cookie": f"csrftoken={csrf_token}; LEETCODE_SESSION={session}",
            "X-CSRFToken": csrf_token,
            "User-Agent": "Mozilla/5.0 (leetcode-sync)",
        }
        self._last_request = 0.0
        # python.org's macOS build ships without CA certs; fall back to the system bundle.
        self.ssl_context = ssl.create_default_context()
        if not self.ssl_context.get_ca_certs() and Path("/etc/ssl/cert.pem").exists():
            self.ssl_context.load_verify_locations("/etc/ssl/cert.pem")

    def query(self, query, variables=None, retries=4):
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        for attempt in range(retries + 1):
            wait = REQUEST_DELAY - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            req = urllib.request.Request(GRAPHQL_URL, data=body, headers=self.headers)
            try:
                with urllib.request.urlopen(req, timeout=30, context=self.ssl_context) as resp:
                    payload = json.load(resp)
                break
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                    time.sleep(3 ** attempt)
                    continue
                raise
            except urllib.error.URLError:
                if attempt < retries:
                    time.sleep(3 ** attempt)
                    continue
                raise
        if payload.get("data") is None:
            raise RuntimeError(f"LeetCode API error: {payload.get('errors')}")
        return payload["data"]

    def username(self):
        status = self.query("query { userStatus { isSignedIn username } }")["userStatus"]
        if not status["isSignedIn"]:
            raise AuthError(
                "LeetCode rejected the session cookie. It has probably expired: copy "
                "fresh LEETCODE_SESSION and csrftoken values from your browser."
            )
        return status["username"]

    def accepted_submissions(self, since):
        """Accepted submissions newer than `since` (unix seconds), newest first."""
        query = """query ($offset: Int!, $limit: Int!) {
          submissionList(offset: $offset, limit: $limit) {
            hasNext
            submissions { id lang timestamp statusDisplay runtime memory title titleSlug }
          }
        }"""
        found, offset = [], 0
        while True:
            page = self.query(query, {"offset": offset, "limit": PAGE_SIZE})["submissionList"]
            for sub in page["submissions"]:
                if int(sub["timestamp"]) <= since:
                    return found
                if sub["statusDisplay"] == "Accepted":
                    found.append(sub)
            if not page["hasNext"]:
                return found
            offset += PAGE_SIZE

    def submission_code(self, submission_id):
        query = """query ($id: Int!) {
          submissionDetails(submissionId: $id) { code runtimePercentile memoryPercentile }
        }"""
        return self.query(query, {"id": int(submission_id)})["submissionDetails"]

    def question(self, slug):
        query = """query ($slug: String!) {
          question(titleSlug: $slug) { questionFrontendId title difficulty topicTags { name } }
        }"""
        try:
            return self.query(query, {"slug": slug})["question"]
        except (urllib.error.HTTPError, RuntimeError):
            return None  # e.g. premium-locked problem


# ---------------------------------------------------------------- repo helpers

def load_env(path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def load_state(repo):
    path = repo / STATE_FILE
    if path.exists():
        return json.loads(path.read_text())
    return {"last_timestamp": 0, "problems": {}}


def save_state(repo, state):
    (repo / STATE_FILE).write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def git(repo, *args, env=None):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
        env={**os.environ, **(env or {})},
    ).stdout


def commit(repo, message, paths, timestamp=None):
    git(repo, "add", "--", *paths)
    if not git(repo, "diff", "--cached", "--name-only", "--", *paths).strip():
        return False
    env = None
    if timestamp is not None:
        date = f"{int(timestamp)} +0000"
        env = {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    git(repo, "commit", "-q", "-m", message, "--", *paths, env=env)
    return True


def fmt_date(timestamp):
    return datetime.fromtimestamp(int(timestamp), timezone.utc).strftime("%Y-%m-%d")


def folder_name(problem):
    number = problem["number"]
    prefix = number.zfill(4) if number.isdigit() else number.replace(" ", "-")
    return f"{prefix}-{problem['slug']}"


def problem_readme(problem):
    tags = ", ".join(problem["tags"]) or "—"
    return (
        f"# {problem['number']}. {problem['title']}\n\n"
        f"**Difficulty:** {problem['difficulty']} · **Topics:** {tags}  \n"
        f"[View on LeetCode](https://leetcode.com/problems/{problem['slug']}/)\n\n"
        "## Notes\n\n"
        "<!-- Approach, complexity, gotchas. Sync never overwrites this file. -->\n"
    )


def render_index(state):
    problems = sorted(
        state["problems"].values(),
        key=lambda p: (not p["number"].isdigit(), int(p["number"]) if p["number"].isdigit() else 0, p["number"]),
    )
    counts = {d: sum(p["difficulty"] == d for p in problems) for d in DIFFICULTY_ORDER}
    lines = [
        INDEX_START,
        f"**{len(problems)} solved** · "
        + " · ".join(f"{d}: {counts[d]}" for d in DIFFICULTY_ORDER),
        "",
        "| # | Problem | Difficulty | Topics | Solutions | Last solved |",
        "|---:|---|---|---|---|---|",
    ]
    for p in problems:
        folder = f"{SOLUTIONS_DIR}/{folder_name(p)}"
        langs = ", ".join(
            f"[{lang}]({folder}/solution.{EXTENSIONS.get(lang, 'txt')})"
            for lang in sorted(p["languages"])
        )
        lines.append(
            f"| {p['number']} | [{p['title']}]({folder}) | {p['difficulty']} "
            f"| {', '.join(p['tags'])} | {langs} | {fmt_date(p['last_solved'])} |"
        )
    lines.append(INDEX_END)
    return "\n".join(lines)


def update_readme(repo, state):
    path = repo / "README.md"
    text = path.read_text() if path.exists() else "# LeetCode Solutions\n\n"
    index = render_index(state)
    if INDEX_START in text and INDEX_END in text:
        head, rest = text.split(INDEX_START, 1)
        text = head + index + rest.split(INDEX_END, 1)[1]
    else:
        text = text.rstrip("\n") + "\n\n" + index + "\n"
    path.write_text(text)


# ------------------------------------------------------------------------ sync

def sync(repo, client, push=False, dry_run=False, log=print):
    state = load_state(repo)
    log(f"Signed in as {client.username()}")
    subs = client.accepted_submissions(since=state["last_timestamp"])
    subs.sort(key=lambda s: int(s["timestamp"]))
    log(f"{len(subs)} new accepted submission(s)")

    committed = 0
    for sub in subs:
        slug, lang, ts = sub["titleSlug"], sub["lang"], int(sub["timestamp"])
        problem = state["problems"].get(slug)
        if problem is None:
            q = client.question(slug) or {}
            problem = {
                "slug": slug,
                "number": str(q.get("questionFrontendId") or "0"),
                "title": q.get("title") or sub["title"],
                "difficulty": q.get("difficulty") or "Unknown",
                "tags": [t["name"] for t in q.get("topicTags") or []],
                "first_solved": ts,
                "last_solved": ts,
                "languages": {},
            }
        folder = Path(SOLUTIONS_DIR) / folder_name(problem)
        solution = folder / f"solution.{EXTENSIONS.get(lang, 'txt')}"
        verb = "Update" if lang in problem["languages"] else "Solve"
        message = (
            f"{verb} {problem['number']}. {problem['title']} ({problem['difficulty']}) "
            f"· {lang} · {sub['runtime']}, {sub['memory']}"
        )
        if dry_run:
            log(f"[dry-run] {fmt_date(ts)} {message}")
            continue

        code = client.submission_code(sub["id"])["code"]
        code = code if code.endswith("\n") else code + "\n"
        state["last_timestamp"] = ts
        if (repo / solution).exists() and (repo / solution).read_text() == code:
            log(f"{fmt_date(ts)} skipped (identical code): {problem['title']} · {lang}")
            continue
        (repo / folder).mkdir(parents=True, exist_ok=True)
        (repo / solution).write_text(code)
        readme = repo / folder / "README.md"
        if not readme.exists():
            readme.write_text(problem_readme(problem))

        problem["languages"][lang] = sub["id"]
        problem["last_solved"] = ts
        state["problems"][slug] = problem
        save_state(repo, state)
        commit(repo, message, [str(folder), STATE_FILE], timestamp=ts)
        committed += 1
        log(f"{fmt_date(ts)} {message}")

    if dry_run:
        return 0
    update_readme(repo, state)
    save_state(repo, state)
    if commit(repo, "Update solutions index", ["README.md", STATE_FILE]):
        committed += 1
    log(f"Made {committed} commit(s)")
    if push and committed:
        git(repo, "push")
        log("Pushed")
    return committed


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent,
                        help="git repo to write solutions into (default: this script's folder)")
    parser.add_argument("--push", action="store_true", help="git push after committing")
    parser.add_argument("--dry-run", action="store_true", help="list what would be synced")
    args = parser.parse_args()

    load_env(args.repo / ".env")
    session = os.environ.get("LEETCODE_SESSION")
    csrf = os.environ.get("LEETCODE_CSRF_TOKEN")
    if not session or not csrf:
        sys.exit("Set LEETCODE_SESSION and LEETCODE_CSRF_TOKEN (see README.md).")
    try:
        sync(args.repo, LeetCode(session, csrf), push=args.push, dry_run=args.dry_run)
    except AuthError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
