import importlib.resources
from pathlib import Path

import yaml
from pydantic import ValidationError

from harness.playbook.models import Playbook


class PlaybookError(Exception):
    """Raised when a playbook fails to load or parse."""

    pass


def load_playbook(text: str) -> Playbook:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise PlaybookError(f"YAML parse error: {e}") from e

    try:
        return Playbook.model_validate(data)
    except ValidationError as e:
        raise PlaybookError(f"Playbook validation error: {e}") from e


def load_file(path: str | Path) -> Playbook:
    path_obj = Path(path)
    try:
        text = path_obj.read_text(encoding="utf-8")
    except OSError as e:
        raise PlaybookError(f"Error reading {path_obj}: {e}") from e
    return load_playbook(text)


def builtin_playbooks() -> dict[str, Playbook]:
    playbooks = {}
    try:
        pkg = importlib.resources.files("harness.playbooks")
        for p in pkg.iterdir():
            if p.name.endswith(".yaml") or p.name.endswith(".yml"):
                try:
                    text = p.read_text(encoding="utf-8")
                    pb = load_playbook(text)
                    playbooks[pb.id] = pb
                except PlaybookError as e:
                    raise PlaybookError(f"{p.name}: {e}") from e
    except FileNotFoundError:
        pass
    return playbooks
