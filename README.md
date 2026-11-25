# Orca Fallback

**English** | [한국어](README.ko.md)

Provider failover and evidence-based model selection for supervised Orca workers.

This is an early public prototype. It discovers models from OpenCodex (OCX) and
Antigravity, preserves a user's role-specific fallback order, executes workers
through the Orca CLI, and ranks candidates without hiding missing evidence.

## Requirements

- Python 3.10+
- Orca 1.4.274 or a compatible version with supervised worker CLI support
- OCX and/or Antigravity (`agy`) for model discovery

## Setup

```sh
git clone https://github.com/SOL4R1S/orca-fallback.git ~/orca-fallback
cd ~/orca-fallback
cp settings.example.json settings.json
cp selection.example.json selection.json
python3 -m unittest discover -s tests -v
python3 catalog.py refresh
python3 openrouter.py collect
```

Set `ORCA_FALLBACK_CONFIG` to use a configuration outside the repository.
`settings.json`, `selection.json`, generated catalogs, benchmarks, credentials,
and runtime measurements are intentionally excluded from version control.

The bot/automation recovery supervisor is experimental; see
[its validation status](docs/recovery-validation.md) before any live execution.

## Execute a task

Runs an existing-worktree Orca task through the models selected for a role. A retry is
allowed only after a failed worker is settled and its transcript contains a
provider availability error. Code failures and test failures stop the chain.

```sh
python3 ~/orca-fallback/run.py \
  --worktree /path/to/repository \
  --title 'Example task' \
  --spec-file /path/to/task.md
```

The task file must contain the complete request and acceptance criteria.
The command prints a final JSON result. Every attempt, actual model, dispatch,
and error excerpt is also stored in `~/.local/state/orca-fallback/`.

This first version handles supervised workers and now includes a shared dispatch
entry point (`dispatch.py`) and a bounded recovery supervisor (`recovery.py`)
for workers, bot chats, and scheduled automations. If the runner or supervisor
loses contact with a live or uncertain target, it stops in a `blocked` state
instead of killing PIDs or risking duplicate execution.

## Shared dispatch and bounded recovery

### Shared dispatch (`dispatch.py`)

Bots and automations should invoke `dispatch.py` instead of launching providers
directly. It requires an explicit `--role` and `--lane`, persists a redacted
handoff/audit record under `~/.local/state/orca-fallback/dispatches/`, and
invokes `run.py`:

```sh
python3 ~/orca-fallback/dispatch.py \
  --worktree /path/to/repository \
  --title 'Example task' \
  --spec-file /path/to/task.md \
  --role implementation \
  --lane economy \
  --caller-kind bot \
  --bot atlas \
  --chat-id chat_123
```

Pass `--dry-run` to inspect provider selection and persist a preview without
launching a worker.

When a live `dispatch.py` invocation returns `lane_exhausted` (whether models
failed with provider errors or all models in the lane were skipped in cooldown),
it automatically registers a durable recovery record in
`~/.local/state/orca-fallback/recoveries/` with `supervision_schema_version: 2`,
`unattended_supervision` opt-in (`true` by default; pass `--no-unattended` to opt
out), preserved `excluded_providers` (`--skip-provider`), and `attempted_providers`
containing only actually-attempted models. Models skipped in cooldown remain
eligible for health-gated delayed resume once their cooldown expires. When every
non-excluded candidate in a lane actually hit a settled rate-limit/transient
provider failure proven to have occurred before work or side effects, at most one
post-cooldown attempt is permitted after an exact `OK` health probe in the same
lane; credential errors, `model_not_found`, and jobs with tool/worktree side
effects are never auto-retried and enter an explicit `blocked` state.

### Scoped dispatch wiring (`wiring.py` & `mcp_dispatch.py`)

`wiring.py` defaults to read-only dry-run mode (requiring `--execute` for live
mutations) and wires **delegated tasks** through supported Orca CLI surfaces:

- **Bot delegation wiring (`install-bot` / `uninstall-bot`)**: Registers the
  pure-stdlib MCP stdio server (`mcp_dispatch.py`, exposing
  `orca_fallback_dispatch` and `orca_fallback_status`) per bot via
  `orca-ide bot tools add` and adds a short, marker-delimited guidance block to
  that bot's `SOUL.md` (never touching Orca-managed `AGENTS.md`, `CLAUDE.md`, or
  `bot.json`).
- **Eligible scheduled automations (`install-automation` / `uninstall-automation`)**:
  Explicitly enrolls only recurring scheduled automations that pass safety
  checks (non-`once`, existing local worktree, non-`new-per-run`, non-`reuseSession`,
  non-recovery), records `enrolled_at` plus `baseline_run_ids` so pre-enrollment
  runs are never replayed, and wraps a reversible marker-delimited dispatch
  instruction in the automation prompt via `orca-ide automations edit`. Legacy,
  one-time, and unenrolled automations are skipped by default.
- **Deterministic local scheduled dispatch (`install-scheduled-dispatch` / `uninstall-scheduled-dispatch`)**:
  Installs an opt-in, dry-run-by-default systemd user `.service`/`.timer` pair
  (`orca-dispatch-<name>.*`) that invokes `dispatch.py` directly on a local
  worktree without going through an outer LLM prompt.

**Native Orca boundary**: Native Orca bot-chat turns and Orca scheduled
automations always launch an LLM agent (`--provider` or `--bot` with `--prompt`)
internally and cannot be externally intercepted before Orca invokes the provider
(Orca has no command-only automation mode, `--precheck` is only a schedule gate,
and headless `orca-serve` has no plugin hook service). `SOUL.md` and prompt
blocks are agent guidance for delegated tasks, not a hard interposer; use
`install-scheduled-dispatch` for deterministic local worktree schedules, and
`recovery.py` handles settled provider failures after the fact.

### Recovery supervisor (`recovery.py`) & systemd timer

`recovery.py` defaults to read-only dry-run mode. Live mutations require an
explicit `--execute` flag.

```sh
# Dry-run preview (default)
python3 ~/orca-fallback/recovery.py recover \
  --kind bot_chat --bot atlas --chat-id chat_123 \
  --role implementation --lane premium --failed-provider codex:gpt-6.2-sol

# Live execution (creates a disabled one-time recovery automation and triggers it once -> waiting_for_run)
python3 ~/orca-fallback/recovery.py recover \
  --kind bot_chat --bot atlas --chat-id chat_123 \
  --role implementation --lane premium --failed-provider codex:gpt-6.2-sol --execute

# Reconcile an accepted recovery run against terminal status before marking recovered
python3 ~/orca-fallback/recovery.py reconcile --recovery-id <id> --execute

# Run one bounded reconcile + delayed-resume supervisor pass (dry-run by default)
python3 ~/orca-fallback/recovery.py supervise --execute

# Install and enable the oneshot supervisor timer (`units/orca-recovery-supervisor.*`)
python3 ~/orca-fallback/wiring.py install-systemd --enable --execute

# Run the isolated, loopback-only live E2E harness (creates disposable temp ORCA_USER_DATA_PATH)
python3 ~/orca-fallback/scripts/e2e_isolated.py
```

Key safety guarantees:
- **Idempotency & file locking**: Every live recovery, reconciliation, and
  delayed resume acquires a per-key `flock` under
  `~/.local/state/orca-fallback/recoveries/by_key/` and returns deduplicated
  results while a run is `waiting_for_run` or completed.
- **Settled-before-recovery & no PID kills**: Workers require `worker-show`
  settled failure, and bot chats require `bot chats` plus `agent show` and
  `agent read` (`orca_session_id:<chat_id>`) proving `state=done`. Active or
  uncertain targets return `blocked` (`active_or_uncertain`) with zero
  mutations. Orca has `worker-stop` for supervised dispatches, but no general
  agent-stop or automation-cancel command, so no stop APIs are invented.
- **Provider-only fallback**: Never falls back on test failures, lint failures,
  ordinary implementation errors, or human approval/input waits
  (`observation.agentWait`). Each provider in a role/lane is attempted once,
  without looping back or escalating lanes automatically.
- **Bot chats start a NEW chat without double fire**: Cross-provider bot chat
  recovery resolves bot display names via `~/.orca/bots/*/bot.json`, preserves
  `SOUL.md`, `MEMORY.md`, and `items`, and creates a `--disabled` one-time bot
  automation (`--runs-in new --once --by ...`) triggered once by `automations run`
  (`waiting_for_run` until `reconcile` verifies terminal success).
- **Scheduled automations pause first**: Disables the original schedule before
  running recovery and keeps it disabled by default unless
  `recovery.auto_resume_automation` is `true` in `settings.json` **and** the
  recovery run is reconciled as terminal `succeeded`.
- **Next-task preferred return & delayed health-gated resume**: Active fallback
  tasks never switch mid-task; returning to the preferred provider after a quota
  reset applies to the next task. If all providers in a lane are unavailable, a
  `delayed_resume_plan` is persisted; `recovery.py resume` preserves explicit
  `excluded_providers` across resume, only considers unused eligible providers
  first (or at most one post-cooldown attempt when every non-excluded candidate
  hit a settled rate-limit/transient failure proven to have happened before work
  or side effects), never auto-retries credentials/`model_not_found`/side-effecting
  runs, and requires a bounded read-only inference probe (`codex exec -s read-only`
  or `agy --print --sandbox`) returning exact `OK` in the same lane before
  starting work.
- **Opt-in unattended supervision & timer pacing**: `recovery.py supervise` only
  acts on newly written records carrying `supervision_schema_version: 2` and
  `unattended_supervision: true` (skipping historical records of the same shape
  and `--no-unattended` records), and skips enrolled automation runs recorded at
  or before enrollment. `units/orca-recovery-supervisor.timer` uses
  `OnUnitInactiveSec=1min` so long supervisor runs do not lose the next tick.
- **Secret redaction**: API keys, Bearer tokens, Slack/GitHub/Linear tokens, and
  Orca pairing codes are redacted in persisted state and receipts.

### Operations log

Live dispatch and recovery write timestamped text to
`~/.local/state/orca-fallback/operations.txt`. Follow it with
`tail -F ~/.local/state/orca-fallback/operations.txt`. Each line records the
handoff or recovery ID, status, and relevant model or failure classification.
Prompts, task titles, and raw model output are kept out of this log. Use the ID
to open the detailed record under `dispatches/` or `recoveries/`. The log rotates
at 1 MiB and keeps one `operations.previous.txt` file. Both files follow
`ORCA_FALLBACK_STATE_DIR` when set.

## Model discovery and selection

`catalog.py` reads OCX's live `/v1/models` catalog and `agy models`. New models
registered in either system appear after a refresh. Refreshing never rewrites
the user's role selections or silently substitutes a disappeared model.
OCX-configured models whose live discovery fails remain visible as
`unavailable`, so a temporary credential or quota failure does not erase them.

```sh
python3 ~/orca-fallback/catalog.py refresh
python3 ~/orca-fallback/catalog.py list --role implementation
python3 ~/orca-fallback/catalog.py select --role implementation \
  --models codex:gpt-6.2-sol=high \
           antigravity:gemini-4-hydrogen-high \
           codex:command-code/z-ai/glm-5.3-flash=low
```

Roles are `orchestrator`, `implementation`, `review`, `documentation`, and
`research`. Selections live in `selection.json`; the generated model inventory
lives in `catalog.json`. A selected model that disappears blocks the next run
until the user chooses a replacement.

### Cost and quality lanes

Each role has `premium` and `economy` lanes with independent fallback orders,
absolute comparison-price ceilings, and minimum intelligence scores. Economy
does not escalate to premium automatically. Exhausting every eligible model
returns `lane_exhausted`; the user must explicitly rerun with `--lane premium`
to approve a more expensive attempt.

```sh
python3 run.py --worktree /path/to/repo --title 'Summarize sources' \
  --spec-file /path/to/task.md --role research --lane economy
python3 catalog.py list --role research --lane economy
python3 catalog.py select --role research --lane economy \
  --models codex:gpt-6-luna=low \
           codex:command-code/z-ai/glm-5.3-flash=low \
           codex:alibaba-token-plan-sg/deepseek-v4.1-flash-1017=low
```

Missing price or quality evidence does not bypass these limits. Legacy role
arrays remain supported for existing configurations.

The included `units/orca-model-catalog.timer` refreshes this inventory every
six hours. It updates only `catalog.json`, never the user's selections.

## OpenRouter evidence collection

`openrouter.py` reads the public `/api/v1/models` JSON directly. It collects
OpenRouter pricing, Artificial Analysis indexes, and Design Arena scores. Exact
IDs and reviewed aliases are matched conservatively; similar names and different
versions are not treated as the same model. Raw JSON is cached under
`~/.cache/orca-fallback/`, while normalized entries retain source URLs,
collection time, and a source hash.

OpenRouter prices describe OpenRouter usage. OCX subscription and token-plan
billing can differ, so both values remain separate.

## Evidence-based recommendations

`recommend.py` ranks available models for each bot role using sourced OCX or OpenRouter price
metadata, local success/latency observations, and optional public benchmark
records. It does not infer missing prices or benchmark scores. Public benchmark
entries require an exact model key, benchmark name, score, source URL, and
verification date in `benchmarks.json`. Different benchmark raw scores are
normalized separately before comparison.

```sh
python3 ~/orca-fallback/recommend.py --role implementation
python3 ~/orca-fallback/recommend.py --role research --format json
python3 ~/orca-fallback/recommend.py --role documentation --ai
```

The default is read-only. `--ai` asks the selected orchestrator model to explain
the supplied evidence without tools. `--apply` is the explicit user action that
replaces a role's chain with the top three displayed candidates. Local real-job
measurements are appended to `~/.local/state/orca-fallback/performance.jsonl`.

The recommender is deterministic. AI explanation is optional. Results group
routes for the same underlying model, show alternative providers, and weight
benchmarks according to the bot role. Local task evidence remains necessary
before treating a public leaderboard ranking as a production recommendation.

## Cooldowns

Provider failures trigger a configurable cooldown, inspired by LiteLLM and
Portkey. Subsequent jobs skip that model until the cooldown expires. Successful
calls clear it. Task and test failures never trigger provider fallback.

## Design references

- LiteLLM and Portkey inspired explicit retry/fallback policy and provider
  failure handling.
- OpenRouter's public model API supplies additional price and benchmark evidence.
- Orca remains the source of truth for worker lifecycle, terminals, worktrees,
  and task state.

Cross-provider bot chat recovery starts a **new** bot chat through a one-time
bot automation (`--runs-in new`); it never claims an existing chat can switch
providers in place. This project also does not claim that public benchmark scores
predict success on a user's private workload.

See [the reuse evaluation](docs/reuse-evaluation.md) for the initial measured
results and current limitations.
