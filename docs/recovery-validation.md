# Recovery validation status

This recovery supervisor is experimental. It is implemented by AGY/Gemini in an
isolated worktree and has not been deployed against the live Atlas bot or any
production automation.

## Review history

- The initial Kimi review identified eight release blockers involving
  accepted-vs-completed runs, schedule resume policy, inference probes, real bot
  response shapes, duplicate firing, and provider history.
- An independent GPT-6.2 Sol review of the full inline code bundle confirmed the
  direction and added eighteen concrete findings around canonical identity,
  transitional states, launch crash windows, scheduler races, exact-provider
  resume, redaction, global state, and safe record lookup.
- AGY applied the consolidated review. The final pass also fixed two issues found
  by live read-only validation: AGY print argument ordering and Orca's `places[]`
  agent-state representation.

## Verified

- 22 standard-library unit tests pass, along with compile and diff checks.
- Real Codex/Luna and AGY/Hydrogen bounded inference probes both return exact
  `OK` responses.
- A read-only dry run against the real Atlas chat resolves the display name to
  its bot ID, observes `agent show/read` as done, and rejects the current
  non-provider-failure turn without mutations.
- A real worker smoke read the requested README content. The worker did not emit
  `worker_done`, so the runner correctly refused to assume success. The owned
  smoke worker and coordinator were explicitly stopped and cleaned up.

## Still requires disposable live E2E

- Actual automation create/run/runs response schemas and exact run correlation.
- A disabled one-time recovery automation fires exactly once and reconciles to
  terminal success or failure.
- Scheduler ticks and independent operator edits during pause and resume.
- No daemon or timer invokes reconcile/resume automatically yet.

The implementation is substantial and should remain behind dry-run/explicit
execution until those checks pass. Mainline deployment is intentionally not part
of this branch.
