"""User-facing failures. main() prints these and exits."""


class SyncError(Exception):
    pass


class AuthError(SyncError):
    def __init__(self, **kwargs):
        super().__init__("LeetCode rejected the session cookie. It has probably expired: copy fresh LEETCODE_SESSION and csrftoken values from your browser.".format(**kwargs))


class ApiError(SyncError):
    def __init__(self, **kwargs):
        super().__init__("LeetCode API error: {errors}".format(**kwargs))


class ToolRepoError(SyncError):
    def __init__(self, **kwargs):
        super().__init__("Refusing to write solutions into the leetcode-sync tool repo; pass --repo.".format(**kwargs))


class NotAGitRepoError(SyncError):
    def __init__(self, **kwargs):
        super().__init__("{repo} is not a git repo. Create it first: git init {repo}".format(**kwargs))


class MissingCredentialsError(SyncError):
    def __init__(self, **kwargs):
        super().__init__("Set LEETCODE_SESSION and LEETCODE_CSRF_TOKEN (see README.md).".format(**kwargs))
