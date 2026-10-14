from typing import Any

from harness.playbook.models import Playbook


def playbook_json_schema() -> dict[str, Any]:
    return Playbook.model_json_schema()
