# Reuse evaluation

## What was reused

- LiteLLM and Portkey inspired explicit provider failure classification,
  cooldowns, and ordered fallback policies.
- OpenRouter's public model API supplies versioned model IDs, prices,
  Artificial Analysis indexes, and Design Arena results.
- The recommender uses deterministic role weights. AI is optional for explaining
  evidence and cannot change scores or selections.

## Why the Orca adapter remains

An API gateway does not own Orca's worker lifecycle, terminals, worktrees, or
task mailbox. The local runner therefore retains Orca-specific checks: a failed
worker must be settled before another provider can resume its task, and an
uncertain live worker prevents fallback.

## Initial OCI validation

- 120 locally available model routes matched OpenRouter entries.
- 1,443 normalized public benchmark entries were collected.
- Model variants and alternate providers are grouped in recommendation output.
- Nine unit tests cover score direction, provider/task failure distinction,
  conservative model matching, cooldown clearing, and cost/quality lane limits.
- A real Orca/Luna supervised worker completed a read-only smoke task through
  the Python runner without repository edits.

## Current limits

- Public benchmark coverage is uneven and does not prove performance on a
  private repository.
- OpenRouter prices are not ChatGPT, Google, Command Code, or Alibaba plan bills.
- Reliability and latency remain unknown until real local jobs produce evidence.
- Atlas's own failed bot chat is not restarted by this worker runner.
- Routing quality has not been evaluated against a counterfactual framework such
  as RouterArena. Real alternative runs would be required for that comparison.
