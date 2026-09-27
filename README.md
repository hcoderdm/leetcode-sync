# leetcode-sync

Syncs my accepted LeetCode submissions into a separate
[leetcode-solutions](../leetcode-solutions) repo, with one commit per day I solved
something, dated to that day, for example `20260101: 0001, 0015, 0146`. See
[DESIGN.md](DESIGN.md) for how it works.

## Usage

Requires Python 3.9 or newer and nothing else. The macOS system `python3` works.

1. Log in to leetcode.com. In DevTools, go to Application → Cookies and copy
   `LEETCODE_SESSION` and `csrftoken`.
2. Run `cp .env.example .env` and fill in both values (`.env` is gitignored).
   By default solutions go to `../leetcode-solutions`. It must already be a git repo
   (`git init ../leetcode-solutions`); set `LEETCODE_SOLUTIONS_REPO` in `.env` to use another path.
3. Preview what would sync, then run it:
   ```bash
   python3 sync.py --test
   python3 sync.py --push
   ```

| Flag | Effect |
|---|---|
| `--test` | List the submissions that would be committed, without writing anything |
| `--push` | Run `git push` after committing |
| `--repo PATH` | Solutions repo to write into (default: `LEETCODE_SOLUTIONS_REPO`, else `../leetcode-solutions`) |

When the session cookie expires, which happens every few weeks, the script exits
with a message telling you to paste in fresh values.

For backfilled commits to show up on your GitHub contribution graph, your git
`user.email` must be attached to your GitHub account.

Add notes to any problem's `solutions/<problem>/README.md` in the solutions repo. Sync never overwrites those files.

### Run daily (macOS)

Save the following as `~/Library/LaunchAgents/com.leetcode-sync.plist`, adjusting the path:

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

### Tests

```bash
python3 test_sync.py
```
