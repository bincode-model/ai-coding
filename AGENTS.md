<!-- software-delivery-harness:start -->
## Software Delivery Harness

- Apply `$software-delivery-harness` to software changes in this repository.
- Before product edits, maintain `.harness/spec/requirements.json`, test cases,
  and criterion-level traceability. Never silently reduce scope.
- Separate implementation, testing, independent runtime acceptance, security
  review, and release judgment. A worker cannot make its own final verdict.
- Use only `PASS`, `FAIL`, `BLOCKED`, and `NOT_RUN`; missing machine evidence is
  never `PASS`.
- Run `python3 scripts/harness_verify.py --strict --json` before release or a
  completion claim. Delivery is complete only when `release_allowed` is true.
- Subagents must end with labeled `TASK_STATUS`, `FILES`, `EVIDENCE`, `FAILURES`,
  `BLOCKERS`, and `NOT_RUN` lines.
- Before every subagent spawn, the primary agent must automatically run
  `python3 scripts/harness_authorize_subagent.py` with the exact role, requirement
  or test IDs, and narrowest allowed or reviewed paths. Every write or evidence
  role must name every intended changed path as a concrete `--required-file`;
  evidence-role outputs must finish as non-empty regular files.
  Do not delegate this routine authorization or validation step back to the user.
- Run exactly one matching specialist per pending authorization through
  `python3 scripts/harness_run_subagent.py`. This trusted launcher atomically
  claims the authorization, holds the exclusive repository lease, disables
  nested agents, applies the role sandbox, and must verify the correlated stop
  receipt. Never call native `Agent`, `spawn_agent`, or
  `collaboration.spawn_agent` tools in a Harness repository: project
  `features.multi_agent` is disabled because desktop-specialized paths can bypass
  project Hooks. Native `PreToolUse`, `SubagentStart`, and `SubagentStop` Hooks
  remain defense in depth only.
<!-- software-delivery-harness:end -->
