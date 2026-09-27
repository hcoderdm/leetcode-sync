# Design

## Flow

Each run of `sync.py`:

1. Checks that the session cookie is valid (`userStatus`) and exits with a clear message if it isn't.
2. Pages through `submissionList`, newest first, stopping at the last synced
   timestamp. It keeps only accepted submissions.
3. Fetches, in batches, the code for every new submission (`submissionDetails`)
   and the metadata for every problem it hasn't seen before (`question`: number,
   title, difficulty, topics).
4. Groups the new submissions by local calendar day, then goes through them oldest
   first. For each day it writes the solution files, regenerates the index in the
   top-level README, and makes one commit containing the day's solutions, the
   index, and the state file.
5. Pushes, if `--push` was given.

All of these calls go to LeetCode's undocumented GraphQL endpoint at
`https://leetcode.com/graphql/`, authenticated with the browser cookies.

## Layout

The tool and the solutions live in separate repos. This repo holds only the
tool and `.env`. The solutions repo (default `../leetcode-solutions`, or
`LEETCODE_SOLUTIONS_REPO`, or `--repo`) holds everything sync writes, and the
script refuses to write into its own folder.

The GraphQL queries live in `queries/*.graphql`, one named operation per file,
and are loaded into `QUERIES` keyed by filename. When LeetCode changes its API,
you can paste a file straight into a GraphQL client to debug it, and the git
diff shows exactly which fields changed.

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

- **One commit per day, backdated.** The message is the day and the problems
  solved that day, for example `20260101: 0001, 0015, 0146`. Days use local
  time, and the author and committer dates are set to the day's last submission
  with the local timezone offset, so each commit lands on the right square of
  the contribution graph.
- **A later run on the same day folds into that day's commit.** If HEAD is
  already that day's commit and hasn't been pushed, the run amends it and merges
  the problem lists. Once the commit is pushed, the run makes a second commit for
  the day rather than force-pushing.
- **The state file and index are committed with each day.** If a run dies
  partway, the repo and the state still agree, so the next run resumes cleanly.
- **Identical resubmissions are skipped.** If the code matches the current file,
  it isn't counted, and a day with only such resubmissions gets no commit.
- **Only text inside the markers is regenerated.** Handwritten parts of the
  README survive, and so does every problem README.
- **Runs locally, not in GitHub Actions.** This keeps the LeetCode cookie off
  GitHub and avoids LeetCode blocking cloud IP addresses.
- **Standard library only.** HTTP goes through `http.client`. python.org's macOS
  build ships without CA certificates, so the client falls back to `/etc/ssl/cert.pem`.
- **One keep-alive connection, IPv4 first.** On networks that advertise IPv6
  but can't route it, `urllib` waited out the full timeout on every IPv6 address
  for every request (about 60 seconds each). The client opens one connection for
  the whole run, tries IPv4 addresses first, and gives each address 3 seconds to
  connect. A request now takes about 0.15 seconds.
- **Batched lookups.** Code and metadata lookups are aliased 20 to a GraphQL
  request (`r0: question(titleSlug: $v0) ... r19: ...`), built from the
  single-lookup `.graphql` file by `batch_query`. A lookup that fails inside a
  batch comes back as `None` without failing the others. If LeetCode rejects a
  whole batch, the client falls back to one request per lookup.
- **Adaptive pacing.** Requests start 0.25 seconds apart. Each 429 doubles the
  gap (up to 8 seconds) and waits as long as `Retry-After` asks, and each success
  shrinks the gap back by 10%. Connection errors, 429s and 5xx responses are
  retried up to 5 times with exponential backoff.
- **Degrades on locked problems.** If the `question` lookup fails, for example on
  a Premium problem, the submission is still synced, with the number `0` and
  difficulty `Unknown`.

## Limitations

- The API is unofficial and could change without notice. The queries mirror the
  ones used by the maintained [joshcai/leetcode-sync](https://github.com/joshcai/leetcode-sync) Action.
- Refreshing the cookie is manual.
- Only the latest code per language is kept as a file. Git history keeps one
  version per day, so earlier attempts from the same day aren't preserved.

## Testing

`test_sync.py` runs the whole sync against a fake client in a temporary git
repo. It checks commit order and dates, skipped duplicates, preserved notes and
README text, and that a second run changes nothing. It also checks one commit per
day with the right message and date, that a day with only identical
resubmissions gets no commit, that a same-day rerun amends the unpushed commit,
and that a pushed day gets a new commit instead. Smaller tests check the batched query text, and use a
scripted fake connection to check 429 handling (`Retry-After` is honored and the
delay adapts) and that other HTTP errors fail immediately.
