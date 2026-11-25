# Orca Fallback

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
```

Set `ORCA_FALLBACK_CONFIG` to use a configuration outside the repository.
`settings.json`, `selection.json`, generated catalogs, benchmarks, credentials,
and runtime measurements are intentionally excluded from version control.

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

This first version handles supervised workers. It does not restart Atlas's own
bot chat with a different provider. If the runner loses contact with a live or
uncertain worker, it stops instead of risking a duplicate implementation.

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

The included `units/orca-model-catalog.timer` refreshes this inventory every
six hours. It updates only `catalog.json`, never the user's selections.

## Evidence-based recommendations

`recommend.py` ranks available models for each bot role using sourced OCX price
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

## Design references

- LiteLLM and Portkey inspired explicit retry/fallback policy and provider
  failure handling.
- OpenRouter's public model API is a planned source for additional price and
  benchmark evidence.
- Orca remains the source of truth for worker lifecycle, terminals, worktrees,
  and task state.

This project does not yet restart an Orca bot's own failed chat with another
provider, and it does not claim that public benchmark scores predict success on
a user's private workload.
