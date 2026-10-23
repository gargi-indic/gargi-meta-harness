content = open("harness/engine/dispatcher.py").read()

content = content.replace("if r != repo: continue", "if r != repo:\n                        continue")

with open("harness/engine/dispatcher.py", "w") as f:
    f.write(content)
