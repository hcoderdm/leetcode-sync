# LeetCode Solutions

My accepted LeetCode solutions, synced automatically by [`sync.py`](sync.py).
Each commit is one accepted submission, dated to when I submitted it, so `git log`
on any solution file shows every attempt in order.

<!-- leetcode-sync:start -->
<!-- leetcode-sync:end -->

## How the sync works

`sync.py` (Python 3, standard library only) asks LeetCode's GraphQL API for any
accepted submissions made since the last run, then processes them oldest first:

- writes the code to `solutions/<number>-<slug>/solution.<ext>`, with one file per language
- creates a `README.md` for the problem (difficulty, topics, link, and a Notes
  section) the first time you solve it. Sync never overwrites this file, so notes are safe.
- commits with `GIT_AUTHOR_DATE` set to the submission time, which puts backfilled
  history on the correct days of your GitHub contribution graph
- skips resubmissions whose code is identical to the current file
- regenerates the index table above (only the text between the markers changes)

Progress is stored in `.sync_state.json`, so each run fetches only new submissions.

### Setup

1. Log in to leetcode.com. In DevTools, go to Application → Cookies and copy
   `LEETCODE_SESSION` and `csrftoken`.
2. Put them in `.env` in this folder (the file is gitignored):
   ```
   LEETCODE_SESSION=...
   LEETCODE_CSRF_TOKEN=...
   ```
3. Preview the sync first, then run it for real:
   ```bash
   python3 sync.py --dry-run
   python3 sync.py --push
   ```

The session cookie expires every few weeks. When it does, the script exits with a
message telling you to paste in fresh values.

For the backfilled commits to count on your contribution graph, your git
`user.email` must be an email address attached to your GitHub account.

### Running it on a schedule (macOS)

Save the following as `~/Library/LaunchAgents/com.leetcode-sync.plist`, adjusting the paths:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.leetcode-sync</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/env</string><string>python3</string>
    <string>/Users/YOU/Projects/leetcode-sync/sync.py</string><string>--push</string>
  </array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>23</integer><key>Minute</key><integer>30</integer></dict>
  <key>StandardOutPath</key><string>/tmp/leetcode-sync.log</string>
  <key>StandardErrorPath</key><string>/tmp/leetcode-sync.log</string>
</dict></plist>
```

Then load it:

```bash
launchctl load ~/Library/LaunchAgents/com.leetcode-sync.plist
```

The job runs on your own machine rather than in GitHub Actions. That keeps your
LeetCode cookie off GitHub and avoids LeetCode blocking cloud IP addresses.

### Tests

```bash
python3 test_sync.py
```
