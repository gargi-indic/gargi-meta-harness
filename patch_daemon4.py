with open("harness/daemon.py") as f:
    lines = f.readlines()

new_lines = []
skip = False
for line in lines:
    if line.strip() == "if dispatch_every is not None:":
        pass
    new_lines.append(line)

content = "".join(new_lines)
with open("harness/daemon.py", "w") as f:
    f.write(content)
