from harness.github.client import GitHub

HARNESS_LABEL_PREFIX = "harness:"

HARNESS_LABELS = {
    "harness:ready": ("0e8a16", "Ready to be picked up by the orchestrator"),
    "harness:running": ("fbca04", "Being worked on by an agent"),
    "harness:needs-you": ("d93f0b", "Waiting for human input"),
    "harness:review": ("fbca04", "Waiting for PR review"),
    "harness:blocked": ("b60205", "Blocked by another issue"),
    "harness:done": ("0e8a16", "Successfully completed"),
    "harness:failed": ("d93f0b", "Failed and needs attention"),
    "harness:plan-approved": ("0e8a16", "The campaign plan is approved (added by the repo owner)"),
    "harness:planned": ("1d76db", "Created by the harness from an approved plan"),
}


async def ensure_harness_labels(gh: GitHub, repo: str) -> None:
    for name, (color, description) in HARNESS_LABELS.items():
        await gh.ensure_label(repo, name, color=color, description=description)
