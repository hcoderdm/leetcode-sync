#!/usr/bin/env python3
"""
Sync accepted LeetCode submissions into a separate solutions git repo.

Each accepted submission becomes its own commit, dated to when it was submitted.
The solution file always holds the latest attempt, and `git log` holds every attempt,
and GitHub's contribution graph reflects the days problems were actually solved.

Auth: set LEETCODE_SESSION and LEETCODE_CSRF_TOKEN (env vars or a .env file next
to this script). Both come from the browser's leetcode.com cookies.

Target repo: --repo, else LEETCODE_SOLUTIONS_REPO, else ../leetcode-solutions
(a sibling of this tool's folder).
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

from errors import ApiError, AuthError, MissingCredentialsError, NotAGitRepoError, SyncError, ToolRepoError

GRAPHQL_URL = "https://leetcode.com/graphql/"
QUERY_DIR = Path(__file__).resolve().parent / "queries"
QUERIES = {path.stem: path.read_text() for path in QUERY_DIR.glob("*.graphql")}
STATE_FILE = ".sync_state.json"
SOLUTIONS_DIR = "solutions"
INDEX_START = "<!-- leetcode-sync:start -->"
INDEX_END = "<!-- leetcode-sync:end -->"
REQUEST_DELAY = 1.0  # LeetCode rate-limits aggressively, so requests are spaced out
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


def parse_args() -> argparse.Namespace:
    tool_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", type=Path, default=os.environ.get("LEETCODE_SOLUTIONS_REPO", tool_dir.parent / "leetcode-solutions"), help="solutions git repo to write into (default: ../leetcode-solutions)")
    parser.add_argument("--push", action="store_true", help="git push after committing")
    parser.add_argument("--dry-run", action="store_true", help="list what would be synced")
    return parser.parse_args()


"""
Classes.
"""

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
        # python.org's macOS build ships without CA certs, so the fallback is the system bundle.
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
            raise ApiError(errors=payload.get("errors"))
        return payload["data"]

    def username(self):
        status = self.query(QUERIES["user_status"])["userStatus"]
        if not status["isSignedIn"]:
            raise AuthError()
        return status["username"]

    def accepted_submissions(self, since):
        """Accepted submissions newer than `since` (unix seconds), newest first."""
        found, offset = [], 0
        while True:
            page = self.query(QUERIES["submission_list"], {"offset": offset, "limit": PAGE_SIZE})["submissionList"]
            for sub in page["submissions"]:
                if int(sub["timestamp"]) <= since:
                    return found
                if sub["statusDisplay"] == "Accepted":
                    found.append(sub)
            if not page["hasNext"]:
                return found
            offset += PAGE_SIZE

    def submission_code(self, submission_id):
        return self.query(QUERIES["submission_details"], {"id": int(submission_id)})["submissionDetails"]

    def question(self, slug):
        try:
            return self.query(QUERIES["question"], {"slug": slug})["question"]
        except (urllib.error.HTTPError, ApiError):
            return None  # metadata is unavailable for premium-locked problems


"""
Helpers.
"""

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
        "<!-- Approach, complexity, gotchas. This file is never overwritten. -->\n"
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
    text = path.read_text() if path.exists() else (
        "# LeetCode Solutions\n\n"
        "My accepted LeetCode solutions, synced automatically by leetcode-sync. Each commit\n"
        "is one accepted submission, dated to when it was submitted, so `git log` on any\n"
        "solution file shows every attempt in order. Notes live in each problem's README.\n\n"
    )
    index = render_index(state)
    if INDEX_START in text and INDEX_END in text:
        head, rest = text.split(INDEX_START, 1)
        text = head + index + rest.split(INDEX_END, 1)[1]
    else:
        text = text.rstrip("\n") + "\n\n" + index + "\n"
    path.write_text(text)


"""
Primary.
"""

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
    tool_dir = Path(__file__).resolve().parent
    load_env(tool_dir / ".env")
    args = parse_args()

    try:
        repo = Path(args.repo).expanduser().resolve()
        if repo == tool_dir:
            raise ToolRepoError()
        if not (repo / ".git").exists():
            raise NotAGitRepoError(repo=repo)
        session = os.environ.get("LEETCODE_SESSION")
        csrf = os.environ.get("LEETCODE_CSRF_TOKEN")
        if not session or not csrf:
            raise MissingCredentialsError()
        print(f"Syncing into {repo}")
        sync(repo, LeetCode(session, csrf), push=args.push, dry_run=args.dry_run)
    except SyncError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
