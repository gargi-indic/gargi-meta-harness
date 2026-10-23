content = open("harness/daemon.py").read()

if "dispatch_every: float | None = None," not in content:
    content = content.replace(
        "idle: float = 1.0,", "idle: float = 1.0,\n    dispatch_every: float | None = None,"
    )

with open("harness/daemon.py", "w") as f:
    f.write(content)
