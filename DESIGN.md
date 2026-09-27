# Design

## Flow

Each run of `sync.py`:

1. Checks that the session cookie is valid (`userStatus`) and exits with a clear message if it isn't.
2. Pages through `submissionList`, newest first, stopping at the last synced
   timestamp. It keeps only accepted submissions.
3. Processes the new submissions oldest first. For each one it fetches the code
   (`submissionDetails`) and, the first time a problem appears, its metadata
   (`question`: number, title, difficulty, topics). Then it writes the files and
   makes one commit.
4. Regenerates the index in the top-level README and commits it along with the state file.
5. Pushes, if `--push` was given.

All of these calls go to LeetCode's undocumented GraphQL endpoint at
`https://leetcode.com/graphql/`, authenticated with the browser cookies.

## Layout

The tool and the solutions live in separate repos. This repo holds only the
tool and `.env`. The solutions repo (default `../leetcode-solutions`, or
`LEETCODE_SOLUTIONS_REPO`, or `--repo`) holds everything sync writes, and the
script refuses to write into its own folder.

```
leetcode-solutions/
  solutions/0001-two-sum/
    solution.py      latest accepted code, one file per language
    solution.cpp
    README.md        created once with metadata and a Notes section, never overwritten
  .sync_state.json   last synced timestamp and per-problem metadata
  README.md          index table between the leetcode-sync markers
```

`.sync_state.json` does two jobs. Its timestamp makes runs incremental, and its
per-problem metadata is what the index is built from, so building the index
needs no API calls.

## Decisions

- **One commit per submission, backdated.** The commit's `GIT_AUTHOR_DATE` and
  `GIT_COMMITTER_DATE` are set to the submission time. The file holds the latest
  attempt, git history holds all of them, and the contribution graph shows the
  days problems were actually solved.
- **The state file is committed with each solution.** If a run dies partway, the
  repo and the state still agree, so the next run resumes cleanly.
- **Identical resubmissions are skipped.** If the code matches the current file,
  no commit is made, so history only shows real changes.
- **Only text inside the markers is regenerated.** Handwritten parts of the
  README survive, and so does every problem README.
- **Runs locally, not in GitHub Actions.** This keeps the LeetCode cookie off
  GitHub and avoids LeetCode blocking cloud IP addresses.
- **Standard library only.** HTTP goes through `urllib`. python.org's macOS
  build ships without CA certificates, so the client falls back to `/etc/ssl/cert.pem`.
- **Gentle on the API.** Requests are spaced 1 second apart, and rate-limit (429)
  or 5xx responses are retried with exponential backoff (1, 3, 9, 27 seconds).
- **Degrades on locked problems.** If the `question` lookup fails, for example on
  a Premium problem, the submission is still synced, with the number `0` and
  difficulty `Unknown`.

## Limitations

- The API is unofficial and could change without notice. The queries mirror the
  ones used by the maintained [joshcai/leetcode-sync](https://github.com/joshcai/leetcode-sync) Action.
- Refreshing the cookie is manual.
- Only the latest code per language is kept as a file. Earlier attempts exist only in git history.

## Testing

`test_sync.py` runs the whole sync against a fake client in a temporary git
repo. It checks commit order and dates, skipped duplicates, preserved notes and
README text, that a second run changes nothing, and that a new attempt shows up
as an `Update` commit.
