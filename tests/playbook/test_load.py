from harness.playbook.load import builtin_playbooks


def test_builtin_playbooks_returns_plan_build_review():
    playbooks = builtin_playbooks()
    assert "plan-build-review" in playbooks
    pb = playbooks["plan-build-review"]
    assert pb.id == "plan-build-review"
    assert pb.apiVersion == "harness/v1"
