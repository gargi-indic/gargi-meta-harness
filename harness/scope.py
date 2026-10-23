import fnmatch
from collections.abc import Sequence


def match_protected(path: str, patterns: Sequence[str]) -> bool:
    """Check if path matches any of the protected patterns."""
    for entry in patterns:
        if not entry:
            continue
        if any(c in entry for c in "*?["):
            if fnmatch.fnmatchcase(path, entry):
                return True
        elif entry.endswith("/"):
            if path.startswith(entry):
                return True
        elif path == entry or path.startswith(entry + "/"):
            return True
    return False
