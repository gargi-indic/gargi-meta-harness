from pathlib import Path

ci_path = Path(".github/workflows/ci.yml")
content = ci_path.read_text()

lines = content.split("\n")
new_lines = []
for line in lines:
    new_lines.append(line)
    if "uv sync --all-extras --dev" in line:
        new_lines.append("    - name: Check playbook schema drift")
        new_lines.append("      run: uv run python scripts/export_playbook_schema.py --check")

web_job = """
  web:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: web
    steps:
      - uses: actions/checkout@v7.0.1
      - uses: actions/setup-node@v7.1.0
        with:
          node-version: "22"
          cache: npm
          cache-dependency-path: web/package-lock.json
      - run: npm ci
      - run: npm run gen:types && git diff --exit-code src/generated
      - run: npm run lint
      - run: npm run typecheck
      - run: npm test
      - run: npm run build
"""

new_content = "\n".join(new_lines) + web_job
ci_path.write_text(new_content)
