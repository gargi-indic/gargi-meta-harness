import os
import re
import subprocess
from pathlib import Path

SECRET_NAMES = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "JULES_API_KEY",
    "ANTHROPIC_API_KEY",
    "CODEX_API_KEY",
    "OPENAI_API_KEY",
)


def parse_env_file(text: str) -> dict[str, str]:
    result = {}
    identifier_pattern = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # Remove 'export ' prefix if present
        if line.startswith("export "):
            line = line[len("export ") :].strip()

        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if not identifier_pattern.match(key):
            continue

        # Check for quotes
        has_quotes = len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'")

        if not has_quotes:
            # Strip inline comments for unquoted values
            if "#" in value:
                value = value.split("#", 1)[0].strip()
        else:
            value = value[1:-1]

        result[key] = value

    return result


def load_env_files(paths: list[Path]) -> list[str]:
    loaded_keys = []
    for path in paths:
        path = path.expanduser()
        if not path.is_file():
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue

        env_vars = parse_env_file(text)
        for key, value in env_vars.items():
            if not value:
                continue
            if key not in os.environ or not os.environ[key]:
                os.environ[key] = value
                loaded_keys.append(key)

    return loaded_keys


def github_token() -> str:
    if os.environ.get("GH_TOKEN"):
        return os.environ["GH_TOKEN"]
    if os.environ.get("GITHUB_TOKEN"):
        return os.environ["GITHUB_TOKEN"]

    try:
        result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True)
        token = result.stdout.strip()
        if not token:
            raise RuntimeError("no GitHub token: set GH_TOKEN or run `gh auth login`")
        return token
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        raise RuntimeError("no GitHub token: set GH_TOKEN or run `gh auth login`") from e


def redact(text: str, extra: list[str] | None = None) -> str:
    secrets_to_redact = []

    # Gather secret values from environment
    for name in SECRET_NAMES:
        if name in os.environ:
            val = os.environ[name]
            if len(val) >= 8:
                secrets_to_redact.append(val)

    if extra:
        for val in extra:
            if len(val) >= 8:
                secrets_to_redact.append(val)

    # Sort by length descending to replace longer secrets first (e.g. if one secret is a prefix of another)
    secrets_to_redact.sort(key=len, reverse=True)

    for secret in secrets_to_redact:
        text = text.replace(secret, "[redacted]")

    return text
