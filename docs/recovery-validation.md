# Recovery validation status

This recovery supervisor is experimental. All validation was performed in an
isolated worktree and disposable loopback Orca runtimes; it has not been
deployed against the live Atlas bot or any production automation.

## Review history

- The initial Kimi review identified eight release blockers involving
  accepted-vs-completed runs, schedule resume policy, inference probes, real bot
  response shapes, duplicate firing, and provider history.
- An independent GPT-6.2 Sol review of the full inline code bundle confirmed the
  direction and added eighteen concrete findings around canonical identity,
  transitional states, launch crash windows, scheduler races, exact-provider
  resume, redaction, global state, and safe record lookup.
- Self-review and isolated live Orca 1.4.274 testing identified and fixed four
  additional real-schema issues: unwrapping `{"automation": ...}` envelopes on
  `automations show`/`edit`/`create`, recognizing `"dispatched"` as an active
  run status, reconciling Orca's terminal `"completed"` status with
  `outputSnapshot.content` error checks, extracting `rrule`/`runContext.path`/
  `extraAgentArgs` model identity, and clearing stale `block_reason` after
  ambiguous-launch reconciliation.

## Verified

- **Unit & regression suite**: `python3 -m unittest discover -s tests -v` (23
  deterministic standard-library tests covering all Kimi/Sol findings and real
  Orca schema envelopes).
- **Compile & diff checks**: `python3 -m compileall -q .`, `python3 -m py_compile ...`,
  `git diff --check`, and CLI `--help` checks.
- **Isolated disposable live E2E (`python3 scripts/e2e_isolated.py`)** against a
  loopback `orca-ide serve --bind 127.0.0.1 --no-pairing` runtime with an empty
  temporary `ORCA_USER_DATA_PATH`:
  - Verified empty initial state (`automations: []`, `bots: []`, `agents: []`).
  - Real bounded inference probes: `codex:gpt-6-luna` (`codex exec -s read-only`)
    and `antigravity:gemini-4-hydrogen-high` (`agy --sandbox --print=...`) both
    returned exact `OK`; nonexistent model failed closed (`ok=false`).
  - Real `dispatch.py` -> `run.py` supervised worker execution (`worker-start` ->
    `worker-show` transitional `ready/input_accepted` to `succeeded/settled/reclaimable`).
  - Real automation failure (`Provider error 404: Model not exist` in
    `outputSnapshot.content`) -> dry-run (`enabled=true` preserved) -> live
    pause & worker recovery (`enabled=false` kept disabled) -> canonical
    deduplication.
  - Real automation `run` (`runId` correlation) -> `dispatched` -> `completed` ->
    `reconcile` -> `recovered`, opt-in `auto_resume_automation=true` re-enable,
    and operator-edit race refusal (`rrule`/`updatedAt` changed -> kept disabled).
  - Real `agent show` (`places[].state == "done"`) and `agent read` (`state == "done"`).
  - Delayed resume with real `codex:gpt-6-luna` `probe_health` (`exact_ok=true`),
    healthy automation blocking (`blocked`), and simulated ambiguous launch
    blocking followed by `reconcile` recovery.

## Unsupported / unverified live gates

- **Mutating bot-chat E2E (`bot chats` + `automations create --bot ... --runs-in new --once`)**:
  `orca-ide` 1.4.274 has no headless CLI command to create/register a disposable
  test bot (`orca-ide bot create --offer` requires owner UI card acceptance, and
  `bot list` returns `[]` in an isolated `ORCA_USER_DATA_PATH` runtime). Because
  mutating the production Atlas bot is forbidden, mutating live bot-chat recovery
  was not executed end-to-end; bot-chat verification relies on read-only schema
  evidence (`agent show/read`, `bot chats`) plus unit tests.
- **Background scheduler/poller**: No permanent daemon or timer is installed;
  `reconcile` and `resume` remain explicit invocations.

Because mutating live bot-chat E2E is unsupported headlessly without an owner UI,
PR #1 remains in **draft** status until live bot-chat E2E can be verified in an
owner-approved disposable bot environment.
