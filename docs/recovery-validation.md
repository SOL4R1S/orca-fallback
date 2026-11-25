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
- Self-review and isolated live Orca 1.4.274 testing identified and fixed
  real-schema and setup-path issues across worker, automation, and bot-chat
  paths:
  - unwrapping `{"automation": ...}` envelopes on `automations show`/`edit`/`create`;
  - recognizing `"dispatching"` and `"dispatched"` as active run states and
    `"dispatch_failed"` as a terminal failed run state;
  - reconciling Orca's terminal `"completed"` status with
    `outputSnapshot.content` error checks and clearing stale `block_reason`;
  - extracting nested `bot.botId`, `bot.resultsTo`, `bot.agentSetup.launch.model`,
    `rrule`, `runContext.path`, and `extraAgentArgs`;
  - handling real `agent show` (`places: []` when settled) and `agent read`
    (`entries` with terminal `kind: "error"`, plus `couldn't finish this
    response` provider failure matching in `run.py`);
  - resolving isolated `<ORCA_USER_DATA_PATH>/bots` automatically when
    `ORCA_USER_DATA_PATH` is set.

## Verified

- **Unit & regression suite**: `python3 -m unittest discover -s tests -v` (24
  deterministic standard-library tests covering all Kimi/Sol findings, real
  Orca schema envelopes, `entries`-based bot-chat errors, and `ORCA_USER_DATA_PATH`
  bots-dir resolution).
- **Compile & diff checks**: `python3 -m compileall -q .`, `python3 -m py_compile ...`,
  `git diff --check`, and CLI `--help` checks.
- **Isolated disposable live E2E (`python3 scripts/e2e_isolated.py`)** against a
  loopback `orca-ide serve --bind 127.0.0.1` runtime with an empty temporary
  `ORCA_USER_DATA_PATH` (and `ORCA_PAIRING_CODE`/`ORCA_ENVIRONMENT` unset):
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
  - Real `agent show` (`places[].state == "done"` on terminal workers and
    `places: []` on settled bot chats) and `agent read` (`state == "done"` /
    `entries[].kind == "error"`).
  - Delayed resume with real `codex:gpt-6-luna` `probe_health` (`exact_ok=true`),
    healthy automation blocking (`blocked`), and simulated ambiguous launch
    blocking followed by `reconcile` recovery.
  - **Mutating disposable bot-chat recovery E2E**: registered an isolated test
    bot via owner-scoped loopback E2EE RPC (`bots.create` with `takingOver=true`
    and zero external connectors), populated `SOUL.md`, `MEMORY.md`, and
    `items.json`, triggered a real Codex model failure (`dispatch_failed` with
    `Codex couldn't finish this response. Open Details for more information.`),
    recovered cross-provider to `antigravity:gemini-4-hydrogen-high` via a
    disabled one-time `--runs-in new --once` bot automation triggered once,
    verified canonical display-name deduplication, correlated `runId`, reconciled
    to `recovered` on a **new** chat ID with preserved `SOUL.md`/`MEMORY.md`/
    `items.json`, and deleted all test automations and the disposable bot
    before stopping the isolated server.

## Operational scope notes

- **Background scheduler/poller**: No permanent daemon or timer is installed;
  `reconcile` and `resume` remain explicit invocations.
- **Production deployment**: All validation ran strictly against isolated
  temporary runtimes (`ORCA_USER_DATA_PATH` under `/tmp`, loopback `127.0.0.1`);
  production Atlas bots and automations were never modified.
