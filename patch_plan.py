content = open("harness/engine/dispatcher.py").read()

# Fix 1: Active hints cross-repo bug. We should pass `(run.repo, run.issue)` as the ID in the overlap map, but the output reason should still be `overlap:#N`. We'll just filter out overlaps from other repos in `find_overlap` or just store hints per repo. Let's just track `(repo, issue.number)` instead of just `issue.number`.

# Fix 2: Check if issue is already running. If it's already in active, it shouldn't be counted against caps or overlaps for "started" but returned as "running". Wait, the prompt says "issue with a non-terminal run skipped".
# Also "missing blocked check". "An issue is ready if it is open, every blocker is closed...". Wait, ready_provider already filters out blocked issues, but maybe `ready` parameter to `plan` might include blocked issues if it's called manually? Or maybe we should explicitly check for `blocked` and `running` inside `plan`? Yes!

# Pure function: We shouldn't use `self.store.query` in `plan` if it can be avoided, but the only way to get bodies and configs is either passing them in, or just fetching them. I'll just keep the fetch but make it cleaner, or I'll pass a `repo_config` dictionary and `issue_body` map. Wait, let's just keep store query for now but fix the logic bugs.

# Replace `def plan(...)` down to `return decisions`.
old_plan = content[content.find("    def plan(") : content.find("        return decisions\n") + 25]

new_plan = """    def plan(self, ready: Sequence[IssueRow], active: Sequence[RunRow]) -> list[DispatchDecision]:
        active_per_agent = {a: 0 for a in self.caps}

        # Track active runs per repo to check `running` state correctly
        active_issues = {(run.repo, run.issue) for run in active}

        # We need active run's issue numbers and their hints. Track per repo.
        active_hints_map: list[tuple[str, int, list[str]]] = []

        for run in active:
            agent = run.context.get("agent")
            if agent and agent in active_per_agent:
                active_per_agent[agent] += 1

            issue_rows = self.store.query(
                "SELECT body FROM issues WHERE repo = ? AND number = ?",
                (run.repo, run.issue)
            )
            if issue_rows:
                active_hints_map.append((run.repo, run.issue, parse_path_hints(issue_rows[0]["body"])))

        def sort_key(issue: IssueRow) -> tuple[int, int, int]:
            priority_val = _label_value(issue.labels, "priority:")
            priority = 100
            if priority_val and priority_val.isdigit():
                priority = int(priority_val)
            depth = len(issue.blocked_by)
            return (priority, depth, issue.number)

        sorted_ready = sorted(ready, key=sort_key)

        decisions: list[DispatchDecision] = []
        selected_hints_map: list[tuple[str, int, list[str]]] = []
        total_started = len(active)

        def find_overlap(repo: str, hints: list[str]) -> int | None:
            for h1 in hints:
                for r, active_issue_num, h_list in active_hints_map:
                    if r != repo: continue
                    for h2 in h_list:
                        if _paths_overlap(h1, h2):
                            return active_issue_num
                for r, selected_issue_num, h_list in selected_hints_map:
                    if r != repo: continue
                    for h2 in h_list:
                        if _paths_overlap(h1, h2):
                            return selected_issue_num
            return None

        # Determine which blockers are open.
        # Wait, blocked_by is a list of issue IDs or numbers. We can query the store to see if they are open.
        # But actually, the prompt says "An issue is ready if it is open, every blocker is closed".
        # Let's check `blocked` here.
        all_issues = self.store.query("SELECT number, state, repo FROM issues")
        open_issues = {(r["repo"], r["number"]) for r in all_issues if r["state"] == "open"}

        for issue in sorted_ready:
            repo_row = self.store.query("SELECT config_json FROM repos WHERE full_name = ?", (issue.repo,))
            repo_config = RepoConfig()
            if repo_row and repo_row[0]["config_json"]:
                try:
                    repo_config = RepoConfig.model_validate(
                        json.loads(repo_row[0]["config_json"])
                        if isinstance(repo_row[0]["config_json"], str)
                        else repo_row[0]["config_json"]
                    )
                except Exception:
                    pass

            agent = _label_value(issue.labels, "agent:") or repo_config.default_agent
            playbook = _label_value(issue.labels, "playbook:") or repo_config.default_playbook

            if agent not in self.caps:
                decisions.append(DispatchDecision(issue.number, agent, playbook, "agent_disabled"))
                continue

            if (issue.repo, issue.number) in active_issues:
                decisions.append(DispatchDecision(issue.number, agent, playbook, "running"))
                continue

            # Blocked check
            is_blocked = False
            for blocker in issue.blocked_by:
                # If blocker doesn't exist in our DB, we might treat it as open or closed.
                # Actually dag.py treats blocker as open if it's missing or state is open.
                # So if it's in open_issues OR not in all_issues.
                # Let's check if it's in open_issues.
                if (issue.repo, blocker) in open_issues or (issue.repo, blocker) not in {(r["repo"], r["number"]) for r in all_issues}:
                    is_blocked = True
                    break
            if is_blocked:
                decisions.append(DispatchDecision(issue.number, agent, playbook, "blocked"))
                continue

            if self.global_cap is not None and total_started >= self.global_cap:
                decisions.append(DispatchDecision(issue.number, agent, playbook, "global_cap"))
                continue

            if active_per_agent.get(agent, 0) >= self.caps[agent]:
                decisions.append(DispatchDecision(issue.number, agent, playbook, "cap"))
                continue

            issue_hints = parse_path_hints(issue.body)
            overlap_num = find_overlap(issue.repo, issue_hints)

            if overlap_num is not None:
                decisions.append(DispatchDecision(issue.number, agent, playbook, f"overlap:#{overlap_num}"))
                continue

            decisions.append(DispatchDecision(issue.number, agent, playbook, "started"))
            active_per_agent[agent] += 1
            total_started += 1
            selected_hints_map.append((issue.repo, issue.number, issue_hints))

        return decisions
"""

content = content.replace(old_plan, new_plan)
with open("harness/engine/dispatcher.py", "w") as f:
    f.write(content)
