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
import http.client
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from errors import ApiError, AuthError, HttpError, MissingCredentialsError, NotAGitRepoError, SyncError, ToolRepoError

GRAPHQL_HOST = "leetcode.com"
GRAPHQL_PATH = "/graphql/"
QUERY_DIR = Path(__file__).resolve().parent / "queries"
QUERIES = {path.stem: path.read_text() for path in QUERY_DIR.glob("*.graphql")}
STATE_FILE = ".sync_state.json"
SOLUTIONS_DIR = "solutions"
INDEX_START = "<!-- leetcode-sync:start -->"
INDEX_END = "<!-- leetcode-sync:end -->"
MIN_DELAY = 0.25  # seconds between requests; doubles on each 429, decays back on success
MAX_DELAY = 8.0
CONNECT_TIMEOUT = 3  # per address, so an unroutable IPv6 address fails fast
READ_TIMEOUT = 30
PAGE_SIZE = 20
BATCH_SIZE = 20  # lookups aliased into one GraphQL request

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
    parser.add_argument("--test", action="store_true", help="list what would be synced")
    return parser.parse_args()


"""
Classes.
"""

class Connection(http.client.HTTPSConnection):
    """Keep-alive HTTPS connection that tries IPv4 addresses first.

    Some networks advertise IPv6 but can't route it. urllib would wait out the full
    timeout on each IPv6 address, on every request, before falling back to IPv4.
    """

    def connect(self):
        infos = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
        infos.sort(key=lambda info: info[0] != socket.AF_INET)
        error = None
        for family, type_, proto, _, address in infos:
            sock = socket.socket(family, type_, proto)
            sock.settimeout(CONNECT_TIMEOUT)
            try:
                sock.connect(address)
            except OSError as e:
                sock.close()
                error = e
                continue
            sock.settimeout(self.timeout)
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
            return
        raise error or OSError(f"Could not connect to {self.host}")


def batch_query(template, count):
    """Repeat a one-variable query `count` times under aliases r0, r1, ... in one request."""
    source = "\n".join(line for line in template.splitlines() if not line.lstrip().startswith("#"))
    header, _, body = source.partition("{")
    body = body.rsplit("}", 1)[0].strip()
    name, type_ = re.search(r"\$(\w+)\s*:\s*([\w!\[\]]+)", header).groups()
    params = ", ".join(f"$v{i}: {type_}" for i in range(count))
    fields = "\n".join(f"r{i}: " + re.sub(rf"\${name}\b", f"$v{i}", body) for i in range(count))
    return f"query Batch({params}) {{\n{fields}\n}}"


def retry_after(response):
    try:
        return min(float(response.getheader("Retry-After")), 60.0)
    except (TypeError, ValueError):
        return None


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
        # python.org's macOS build ships without CA certs, so the fallback is the system bundle.
        ssl_context = ssl.create_default_context()
        if not ssl_context.get_ca_certs() and Path("/etc/ssl/cert.pem").exists():
            ssl_context.load_verify_locations("/etc/ssl/cert.pem")
        self.conn = Connection(GRAPHQL_HOST, timeout=READ_TIMEOUT, context=ssl_context)
        self.delay = MIN_DELAY
        self._last_request = 0.0

    def query(self, query, variables=None, retries=5):
        body = json.dumps({"query": query, "variables": variables or {}})
        for attempt in range(retries + 1):
            wait = self.delay - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            try:
                self.conn.request("POST", GRAPHQL_PATH, body, self.headers)
                response = self.conn.getresponse()
                raw = response.read()
            except ssl.SSLCertVerificationError:
                raise
            except (OSError, http.client.HTTPException):
                self.conn.close()  # stale keep-alive or a network blip, so reconnect
                if attempt == retries:
                    raise
                time.sleep(2 ** attempt)
                continue
            if response.status == 429 or response.status >= 500:
                if response.status == 429:
                    self.delay = min(self.delay * 2, MAX_DELAY)
                if attempt == retries:
                    raise HttpError(status=response.status)
                time.sleep(retry_after(response) or 2 ** attempt)
                continue
            if response.status != 200:
                raise HttpError(status=response.status)
            self.delay = max(MIN_DELAY, self.delay * 0.9)
            payload = json.loads(raw)
            if payload.get("data") is None:
                raise ApiError(errors=payload.get("errors"))
            return payload["data"]

    def batched(self, name, values):
        """Run one-variable query `name` for each value, BATCH_SIZE per request.

        Returns {value: result}; a result is None if that lookup failed. If LeetCode
        rejects a whole batch, falls back to one request per value.
        """
        results = {}
        for start in range(0, len(values), BATCH_SIZE):
            chunk = values[start:start + BATCH_SIZE]
            try:
                data = self.query(batch_query(QUERIES[name], len(chunk)),
                                  {f"v{i}": value for i, value in enumerate(chunk)})
                results.update({value: data.get(f"r{i}") for i, value in enumerate(chunk)})
            except (ApiError, HttpError):
                for value in chunk:
                    try:
                        data = self.query(batch_query(QUERIES[name], 1), {"v0": value})
                        results[value] = data.get("r0")
                    except (ApiError, HttpError):
                        results[value] = None
        return results

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

    def submission_codes(self, submission_ids):
        """{submission id: code}. Raises if any submission's code can't be fetched."""
        details = self.batched("submission_details", [int(i) for i in submission_ids])
        missing = [i for i, d in details.items() if not d]
        if missing:
            raise ApiError(errors=f"no code returned for submission(s) {missing}")
        return {str(i): d["code"] for i, d in details.items()}

    def questions(self, slugs):
        """{slug: metadata}; metadata is None when unavailable (e.g. Premium-locked problems)."""
        return self.batched("question", list(slugs))


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


def commit(repo, message, paths, timestamp=None, amend=False):
    git(repo, "add", "--", *paths)
    if not amend and not git(repo, "diff", "--cached", "--name-only", "--", *paths).strip():
        return False
    options, env = ["-q", "-m", message], None
    if amend:
        options.append("--amend")
    if timestamp is not None:
        # --date because --amend would otherwise keep the original author date.
        date = f"{int(timestamp)} {local_time(timestamp).strftime('%z')}"
        options.append(f"--date={date}")
        env = {"GIT_COMMITTER_DATE": date}
    git(repo, "commit", *options, "--", *paths, env=env)
    return True


def unpushed_head_message(repo):
    """HEAD's subject line, or None if there are no commits or HEAD is already pushed."""
    try:
        subject = git(repo, "log", "-1", "--format=%s").strip()
    except subprocess.CalledProcessError:
        return None
    if git(repo, "branch", "-r", "--contains", "HEAD").strip():
        return None
    return subject


def local_time(timestamp):
    return datetime.fromtimestamp(int(timestamp), timezone.utc).astimezone()


def fmt_date(timestamp):
    return local_time(timestamp).strftime("%Y-%m-%d")


def day_key(timestamp):
    """Local calendar day, e.g. 20260101. Days are local so commits land on the right graph square."""
    return local_time(timestamp).strftime("%Y%m%d")


def problem_label(problem):
    number = problem["number"]
    return number.zfill(4) if number.isdigit() else number.replace(" ", "-")


def folder_name(problem):
    return f"{problem_label(problem)}-{problem['slug']}"


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

def sync(repo, client, push=False, test=False, log=print):
    state = load_state(repo)
    log(f"Signed in as {client.username()}")
    subs = client.accepted_submissions(since=state["last_timestamp"])
    subs.sort(key=lambda s: int(s["timestamp"]))
    log(f"{len(subs)} new accepted submission(s)")

    # Fetch metadata and code up front, in batches, instead of one request per submission.
    new_slugs = sorted({sub["titleSlug"] for sub in subs} - state["problems"].keys())
    questions = client.questions(new_slugs) if new_slugs else {}
    codes = client.submission_codes([sub["id"] for sub in subs]) if subs and not test else {}

    days = {}  # day key -> that day's submissions, oldest first
    for sub in subs:
        days.setdefault(day_key(sub["timestamp"]), []).append(sub)

    # One commit per day: "20260101: 0001, 0015, 0146", holding that day's solutions,
    # the regenerated index, and the state file.
    committed = 0
    for day, day_subs in days.items():
        labels, folders = set(), set()
        for sub in day_subs:
            slug, lang, ts = sub["titleSlug"], sub["lang"], int(sub["timestamp"])
            problem = state["problems"].get(slug)
            if problem is None:
                q = questions.get(slug) or {}
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
            if test:
                labels.add(problem_label(problem))
                continue

            folder = Path(SOLUTIONS_DIR) / folder_name(problem)
            solution = folder / f"solution.{EXTENSIONS.get(lang, 'txt')}"
            code = codes[sub["id"]]
            code = code if code.endswith("\n") else code + "\n"
            state["last_timestamp"] = ts
            if (repo / solution).exists() and (repo / solution).read_text() == code:
                log(f"  skipped (identical code): {problem['title']} · {lang}")
                continue
            (repo / folder).mkdir(parents=True, exist_ok=True)
            (repo / solution).write_text(code)
            readme = repo / folder / "README.md"
            if not readme.exists():
                readme.write_text(problem_readme(problem))

            problem["languages"][lang] = sub["id"]
            problem["last_solved"] = ts
            state["problems"][slug] = problem
            labels.add(problem_label(problem))
            folders.add(str(folder))

        if not labels:
            continue
        if test:
            log(f"[test] {day}: {', '.join(sorted(labels))}")
            continue

        # A later run on the same day folds into that day's commit, unless it was already pushed.
        head = unpushed_head_message(repo)
        amend = head is not None and head.startswith(f"{day}: ")
        if amend:
            labels |= set(head.split(": ", 1)[1].split(", "))
        message = f"{day}: {', '.join(sorted(labels))}"
        update_readme(repo, state)
        save_state(repo, state)
        commit(repo, message, [*sorted(folders), "README.md", STATE_FILE],
               timestamp=day_subs[-1]["timestamp"], amend=amend)
        committed += 1
        log(message + (" (amended)" if amend else ""))

    if test:
        return 0
    save_state(repo, state)  # skipped resubmissions advance the timestamp; committed with the next day
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
        # Validate solutions repo path.
        repo = Path(args.repo).expanduser().resolve()
        if repo == tool_dir:
            raise ToolRepoError()
        
        # Validate solutions repo is a git repo.
        if not (repo / ".git").exists():
            raise NotAGitRepoError(repo=repo)
        
        # Validate LeetCode session and CSRF token.
        session = os.environ.get("LEETCODE_SESSION")
        csrf = os.environ.get("LEETCODE_CSRF_TOKEN")
        if not session or not csrf:
            raise MissingCredentialsError()
        
        # Sync solutions.
        print(f"Syncing into {repo}")
        sync(repo, LeetCode(session, csrf), push=args.push, test=args.test)
    
    # Handle errors and exit.
    except SyncError as e:
        sys.exit(str(e))

if __name__ == "__main__":
    main()
