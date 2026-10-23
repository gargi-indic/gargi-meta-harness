"""Protected-path guard for local agents (pure, synchronous, stdlib only).

``PathGuard`` decides whether a tool call may proceed. File-write tools are checked by target path;
``Bash`` commands are parsed (``shlex`` with a crude fallback) and scanned for writes to protected or
out-of-workspace paths. Read-only access to protected paths is allowed. Anything that cannot be parsed
but mentions a protected path is denied (fail closed). Pattern matching is delegated to
:func:`harness.scope.match_protected`.
"""

import os
import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.scope import match_protected

DEFAULT_PROTECTED: tuple[str, ...] = (".harness/", ".claude/", ".orchestrator/", ".github/workflows/")

_FILE_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
_PATH_KEYS = ("file_path", "notebook_path", "path")
_SAFE_DEVICES = {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"}
_PUNCT = ";<>|&()\n"
_MAX_DEPTH = 3
_WRAPPERS = {"sudo", "env", "command", "nohup", "time", "nice", "exec", "builtin"}
_ALL_ARGS_WRITE = {
    "rm",
    "rmdir",
    "touch",
    "mkdir",
    "chmod",
    "chown",
    "chgrp",
    "truncate",
    "unlink",
    "shred",
    "tee",
}
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
_INTERPRETERS = {"python": "-c", "python3": "-c", "node": "-e", "nodejs": "-e", "ruby": "-e", "perl": "-e"}
_GIT_PATH_SUBCOMMANDS = {"checkout", "restore", "rm", "mv", "clean"}
_WRITE_HINT = re.compile(
    r"""open\([^)]*['"][wax+][bt+]*['"]|writeFile|appendFile|write_text|write_bytes|\.write\(|"""
    r"""createWriteStream|unlink|remove|rename|rmtree|copy|move|mkdir|truncate|symlink|chmod|writeSync"""
)
_REDIRECT_DUP = re.compile(r"^(\d+|-)$")
_HEREDOC = re.compile(r"""(?<!<)<<-?\s*(['"]?)([A-Za-z0-9_]+)\1""")
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


@dataclass(frozen=True)
class GuardDecision:
    allow: bool
    reason: str | None = None


_ALLOW = GuardDecision(True)


def _deny(reason: str) -> GuardDecision:
    return GuardDecision(False, reason)


def _is_op(tok: str) -> bool:
    return bool(tok) and all(c in _PUNCT for c in tok)


def _strip_heredocs(command: str) -> str:
    """Drop heredoc bodies (data, not commands); the ``cat > file <<EOF`` line itself is kept."""
    out: list[str] = []
    pending: list[str] = []
    for line in command.split("\n"):
        if pending:
            if line.strip() == pending[0]:
                pending.pop(0)
            continue
        out.append(line)
        pending.extend(m.group(2) for m in _HEREDOC.finditer(line))
    return "\n".join(out)


def _extract_substitutions(command: str) -> tuple[str, list[str]]:
    """Pull ``$(...)``, backtick and ``<(...)``/``>(...)`` bodies out; return (outer, inners)."""
    outer: list[str] = []
    inners: list[str] = []
    i, n = 0, len(command)
    single = double = False
    while i < n:
        c = command[i]
        if c == "\\" and not single and i + 1 < n:
            outer.append(command[i : i + 2])
            i += 2
            continue
        if c == "'" and not double:
            single = not single
        elif c == '"' and not single:
            double = not double
        elif not single and (
            command.startswith("$(", i) or (not double and command[i : i + 2] in ("<(", ">("))
        ):
            depth, j = 1, i + 2
            while j < n and depth:
                depth += (command[j] == "(") - (command[j] == ")")
                j += 1
            inners.append(command[i + 2 : j - 1 if depth == 0 else j])
            outer.append("X")
            i = j
            continue
        elif c == "`" and not single:
            j = command.find("`", i + 1)
            j = n if j == -1 else j
            inners.append(command[i + 1 : j])
            outer.append("X")
            i = j + 1
            continue
        outer.append(c)
        i += 1
    return "".join(outer), inners


def _tokenize(command: str) -> list[str]:
    lex = shlex.shlex(command, posix=True, punctuation_chars=_PUNCT)
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    lex.commenters = ""
    return list(lex)


def _fallback_tokenize(command: str) -> list[str]:
    return [
        t for t in re.split(r"[\s;|&()<>]+", command.replace("\\", "").replace('"', "").replace("'", "")) if t
    ]


def _split_args(args: list[str]) -> list[str]:
    """Non-option arguments (everything after ``--`` counts as an argument)."""
    out: list[str] = []
    dashdash = False
    for a in args:
        if dashdash:
            out.append(a)
        elif a == "--":
            dashdash = True
        elif not a.startswith("-") or a == "-":
            out.append(a)
    return out


class PathGuard:
    def __init__(self, workspace: Path, protected: Sequence[str]) -> None:
        self.workspace = Path(os.path.realpath(workspace))
        self.protected = tuple(protected)

    # -- paths -------------------------------------------------------------------------------------
    def _check_path(self, raw: str | Path) -> GuardDecision:
        text = os.path.expanduser(str(raw))
        if text in _SAFE_DEVICES:
            return _ALLOW
        resolved = Path(os.path.realpath(self.workspace / text))
        try:
            rel = resolved.relative_to(self.workspace).as_posix()
        except ValueError:
            return _deny(
                f"writes outside the workspace ({raw}) are not allowed; keep changes inside the repo"
            )
        if rel == ".":
            return _ALLOW
        if match_protected(rel, self.protected) or match_protected(rel + "/", self.protected):
            return _deny(f"writes to protected path {rel} are not allowed; ask in the PR instead")
        return _ALLOW

    def _check_paths(self, paths: Sequence[str]) -> GuardDecision:
        for p in paths:
            d = self._check_path(p)
            if not d.allow:
                return d
        return _ALLOW

    def _mentions_protected(self, text: str) -> str | None:
        for entry in self.protected:
            literal = re.split(r"[*?\[]", entry.rstrip("/"))[0]
            if literal and literal in text:
                return entry
        return None

    def check_file_write(self, path: str | Path) -> GuardDecision:
        return self._check_path(path)

    # -- bash --------------------------------------------------------------------------------------
    def check_bash(self, command: str) -> GuardDecision:
        return self._check_command(command, 0)

    def _check_command(self, command: str, depth: int) -> GuardDecision:
        if depth > _MAX_DEPTH:
            hit = self._mentions_protected(command)
            return (
                _deny(f"command nesting too deep to inspect and mentions {hit}; simplify it")
                if hit
                else _ALLOW
            )
        outer, inners = _extract_substitutions(_strip_heredocs(command))
        for inner in inners:
            d = self._check_command(inner, depth + 1)
            if not d.allow:
                return d
        try:
            tokens = _tokenize(outer)
        except ValueError:
            hit = self._mentions_protected(outer)
            if hit:
                return _deny(f"unparsable command mentions protected path {hit}; rewrite it plainly")
            tokens = _fallback_tokenize(outer)
        segment: list[str] = []
        segments: list[list[str]] = []
        redirects: list[str] = []
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if _is_op(tok):
                if ">" in tok:
                    nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
                    if nxt and not _is_op(nxt) and not (tok.endswith("&") and _REDIRECT_DUP.match(nxt)):
                        redirects.append(nxt)
                        i += 1
                elif not tok.startswith("<"):
                    segments.append(segment)
                    segment = []
            else:
                segment.append(tok)
            i += 1
        segments.append(segment)
        d = self._check_paths(redirects)
        if not d.allow:
            return d
        for words in segments:
            d = self._check_words(words, depth)
            if not d.allow:
                return d
        return _ALLOW

    def _check_words(self, words: list[str], depth: int) -> GuardDecision:
        while words and (_ASSIGN.match(words[0]) or os.path.basename(words[0]) in _WRAPPERS):
            words = words[1:]
        if not words:
            return _ALLOW
        cmd, args = os.path.basename(words[0]), words[1:]
        if cmd in _ALL_ARGS_WRITE:
            return self._check_paths(_split_args(args))
        if cmd == "mv" or cmd == "ln":
            return self._check_paths(_split_args(args))
        if cmd in ("cp", "install", "rsync"):
            targets = [a for a in _split_args(args)][-1:]
            for k, a in enumerate(args):
                if a in ("-t", "--target-directory") and k + 1 < len(args):
                    targets.append(args[k + 1])
                elif a.startswith("--target-directory="):
                    targets.append(a.split("=", 1)[1])
            return self._check_paths(targets)
        if cmd == "dd":
            return self._check_paths([a[3:] for a in args if a.startswith("of=")])
        if cmd == "sed":
            return self._check_sed(args)
        if cmd == "find":
            if any(a in ("-delete", "-exec", "-execdir", "-fprint", "-fprintf", "-fls") for a in args):
                roots: list[str] = []
                for a in args:
                    if a.startswith("-") or a in ("(", "!"):
                        break
                    roots.append(a)
                return self._check_paths(roots or ["."])
            return _ALLOW
        if cmd == "git":
            return self._check_git(args)
        if cmd in _SHELLS:
            for k, a in enumerate(args):
                if a.startswith("-") and not a.startswith("--") and "c" in a and k + 1 < len(args):
                    return self._check_command(args[k + 1], depth + 1)
            return _ALLOW
        if cmd == "eval":
            return self._check_command(" ".join(args), depth + 1)
        if re.fullmatch(r"(python|node|nodejs|ruby|perl)[0-9.]*", cmd):
            flag = _INTERPRETERS.get(re.sub(r"[0-9.]+$", "", cmd), "-c")
            for k, a in enumerate(args):
                if a == flag and k + 1 < len(args):
                    return self._check_snippet(args[k + 1])
        return _ALLOW

    def _check_sed(self, args: list[str]) -> GuardDecision:
        in_place = any(
            a == "--in-place"
            or a.startswith("--in-place=")
            or (re.match(r"^-[a-zA-Z]*i", a) and not a.startswith("--"))
            for a in args
        )
        if not in_place:
            return _ALLOW
        has_script_opt = any(a in ("-e", "-f", "--expression", "--file") for a in args)
        files = _split_args(args)
        if not has_script_opt:
            files = files[1:]
        return self._check_paths(files)

    def _check_git(self, args: list[str]) -> GuardDecision:
        rest = list(args)
        while rest and rest[0].startswith("-"):
            rest = rest[2:] if rest[0] in ("-C", "-c") else rest[1:]
        if not rest or rest[0] not in _GIT_PATH_SUBCOMMANDS:
            return _ALLOW
        for p in _split_args(rest[1:]):
            d = self._check_path(p)
            if not d.allow and "protected" in (d.reason or ""):
                return d
        return _ALLOW

    def _check_snippet(self, snippet: str) -> GuardDecision:
        if not _WRITE_HINT.search(snippet):
            return _ALLOW
        for _, lit in re.findall(r"""(['"])(.*?)\1""", snippet):
            if not lit or re.search(r"\s", lit):
                continue
            d = self._check_path(lit)
            if not d.allow and ("protected" in (d.reason or "") or lit.startswith("/")):
                return d
        return _ALLOW

    # -- dispatcher --------------------------------------------------------------------------------
    def check_tool(self, tool_name: str, tool_input: Mapping[str, Any]) -> GuardDecision:
        if tool_name == "Bash":
            command = tool_input.get("command")
            if not isinstance(command, str):
                return _deny("Bash call without a command string is not allowed")
            return self.check_bash(command)
        if tool_name in _FILE_WRITE_TOOLS:
            for key in _PATH_KEYS:
                value = tool_input.get(key)
                if isinstance(value, str | Path) and str(value):
                    return self.check_file_write(value)
            return _deny(f"{tool_name} call without a target path is not allowed")
        return _ALLOW
