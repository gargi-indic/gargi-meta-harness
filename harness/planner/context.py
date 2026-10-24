"""Repository context for the decomposer: the files a repo lists as ``context_files`` and its open issue titles.

All of it is untrusted text on its way into a prompt; this module only reads it and keeps it within a budget.
"""

from dataclasses import dataclass, field

from harness.config import RepoConfig
from harness.github.client import GitHub

MAX_FILE_BYTES = 20 * 1024
MAX_TOTAL_BYTES = 100 * 1024
MAX_TITLES = 200
MAX_TITLE_CHARS = 200


@dataclass
class RepoContext:
    context_files: dict[str, str] = field(default_factory=dict)
    existing_issue_titles: list[str] = field(default_factory=list)
    # Files that were configured but not (fully) included: missing, cut short or over the total budget.
    notes: list[str] = field(default_factory=list)


_MARKER = "\n[truncated]\n"


def _cap(text: str, limit: int) -> tuple[str, bool]:
    """``text`` within ``limit`` bytes of valid UTF-8 (marker included), and whether it was cut."""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text, False
    head = raw[: max(limit - len(_MARKER), 0)].decode("utf-8", errors="ignore")
    return head + _MARKER, True


async def build_repo_context(
    gh: GitHub,
    repo: str,
    config: RepoConfig,
    ref: str | None = None,
    *,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_total_bytes: int = MAX_TOTAL_BYTES,
    max_titles: int = MAX_TITLES,
) -> RepoContext:
    """Fetch ``config.context_files`` at ``ref`` (each capped, all together capped) and the open issue titles."""
    context = RepoContext()
    remaining = max_total_bytes
    for path in dict.fromkeys(p for p in config.context_files if p.strip()):
        if remaining <= 0:
            context.notes.append(
                f"{path}: skipped, the total context budget of {max_total_bytes} bytes is used"
            )
            continue
        text = await gh.file_text(repo, path, ref)
        if text is None:
            context.notes.append(f"{path}: not found")
            continue
        kept, cut = _cap(text, min(max_file_bytes, remaining))
        if cut:
            context.notes.append(f"{path}: truncated")
        remaining -= len(kept.encode("utf-8"))
        context.context_files[path] = kept

    issues = await gh.issues(repo, state="open")
    for issue in issues:
        title = " ".join(str(issue.get("title") or "").split())[:MAX_TITLE_CHARS]
        if title:
            context.existing_issue_titles.append(title)
        if len(context.existing_issue_titles) >= max_titles:
            break
    return context
