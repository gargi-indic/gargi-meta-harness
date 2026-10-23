from harness.scope import match_protected


def test_match_protected():
    patterns = [".harness/", "docs/PLAN.md", "*.lock", "tests/fake_*"]

    assert match_protected(".harness/config.yaml", patterns) is True
    assert match_protected(".harness/inner/file.txt", patterns) is True
    assert match_protected("docs/PLAN.md", patterns) is True
    assert (
        match_protected("docs/PLAN.md/extra", patterns) is True
    )  # "docs/PLAN.md/" prefix from exact match fallback
    assert match_protected("uv.lock", patterns) is True
    assert match_protected("tests/fake_adapter.py", patterns) is True

    assert match_protected("my.harness.txt", patterns) is False
    assert match_protected("docs/PLAN.md.tmp", patterns) is False
    assert match_protected("test/fake.py", patterns) is False
    assert match_protected("lock", patterns) is False


def test_match_protected_case_sensitive():
    assert match_protected("README.md", ["readme.md"]) is False
    assert match_protected("README.md", ["*md"]) is True
    assert match_protected("README.MD", ["*md"]) is False


def test_match_protected_empty_patterns():
    assert match_protected("file.txt", ["", "file.txt"]) is True
    assert match_protected("file.txt", [""]) is False


def test_match_protected_negative_prefix():
    assert match_protected("my.harness.txt", [".harness/"]) is False


def test_match_protected_glob():
    assert match_protected("test.lock", ["*.lock"]) is True
    assert match_protected("nested/path/file.lock", ["*.lock"]) is True
    assert match_protected("test.lock.tmp", ["*.lock"]) is False
