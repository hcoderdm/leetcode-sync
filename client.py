"""Minimal client for LeetCode's (undocumented) GraphQL API."""

import http.client
import json
import re
import socket
import ssl
import time
from pathlib import Path

from errors import ApiError, AuthError, HttpError

GRAPHQL_HOST = "leetcode.com"
GRAPHQL_PATH = "/graphql/"
QUERY_DIR = Path(__file__).resolve().parent / "queries"
QUERIES = {path.stem: path.read_text() for path in QUERY_DIR.glob("*.graphql")}
MIN_DELAY = 0.25  # seconds between requests; doubles on each 429, decays back on success
MAX_DELAY = 8.0
CONNECT_TIMEOUT = 3  # per address, so an unroutable IPv6 address fails fast
READ_TIMEOUT = 30
PAGE_SIZE = 20
BATCH_SIZE = 20  # lookups aliased into one GraphQL request


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

    def question_counts(self):
        """Problems on LeetCode per difficulty, e.g. {"All": 4064, "Easy": 967, ...}."""
        rows = self.query(QUERIES["question_counts"])["allQuestionsCount"]
        return {row["difficulty"]: row["count"] for row in rows}
