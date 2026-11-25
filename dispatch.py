#!/usr/bin/env python3
"""Shared dispatch entry point for bots and automations with explicit role/lane audit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from catalog import resolve_policy
import run


def _cooldown_snapshot(cooldown_path: Path, providers: list[dict[str, Any]]) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for provider in providers:
        key = provider["name"]
        remaining = run.cooldown_remaining(cooldown_path, key)
        if remaining:
            snapshot[key] = {
                "remaining_seconds": remaining,
                "reset_at": run.cooldown_reset_at(cooldown_path, key),
            }
    return snapshot


def select_next_task_provider(
    role: str,
    lane: str,
    *,
    cooldown_path: Path,
    preferred_provider: str | None = None,
    skip_providers: set[str] | None = None,
) -> dict[str, Any]:
    """Return the next eligible provider in the same role/lane (next-task preferred return)."""
    providers, policy = resolve_policy(role, lane)
    if not providers:
        raise ValueError(f"No models satisfy role={role!r} lane={lane!r}")
    preferred = preferred_provider or providers[0]["name"]
    skipped = set(skip_providers or ())
    cooldowns = _cooldown_snapshot(cooldown_path, providers)

    chosen = None
    for provider in providers:
        key = provider["name"]
        if key in skipped or key in cooldowns:
            continue
        chosen = provider
        break

    return {
        "role": role,
        "lane": policy["lane"],
        "preferred_provider": preferred,
        "returned_to_preferred": bool(chosen and chosen["name"] == preferred),
        "next_provider": chosen,
        "providers": providers,
        "policy": policy,
        "cooldowns": cooldowns,
    }


def dispatch_task(
    *,
    worktree: Path,
    title: str,
    role: str,
    lane: str,
    spec_file: Path | None = None,
    prompt: str | None = None,
    caller_kind: str = "bot",
    bot: str | None = None,
    chat_id: str | None = None,
    automation_id: str | None = None,
    run_id: str | None = None,
    item_id: str | None = None,
    preferred_provider: str | None = None,
    skip_providers: set[str] | None = None,
    unattended: bool = True,
    dry_run: bool = False,
    orca: run.Orca | None = None,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    """Invoke run.run_job with an explicit role and lane and persist a handoff/audit record."""
    if not role or not lane:
        raise ValueError("Both role and lane must be specified explicitly")
    worktree_path = Path(worktree).resolve()
    if not worktree_path.is_dir():
        raise ValueError(f"Worktree does not exist: {worktree_path}")

    effective_state_dir = state_dir or run.STATE_DIR
    handoff_id = uuid4().hex[:12]
    handoff_path = effective_state_dir / "dispatches" / f"{handoff_id}.json"
    cooldown_path = effective_state_dir / "cooldowns.json"

    if spec_file is not None:
        spec_path = Path(spec_file).resolve()
    elif prompt and prompt.strip():
        spec_path = effective_state_dir / "specs" / f"dispatch-{handoff_id}.md"
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        spec_path.write_text(prompt, encoding="utf-8")
    else:
        raise ValueError("Either spec_file or non-empty prompt is required")

    if not spec_path.is_file() or not spec_path.read_text(encoding="utf-8").strip():
        raise ValueError(f"Spec file must exist and be non-empty: {spec_path}")

    selection_info = select_next_task_provider(
        role,
        lane,
        cooldown_path=cooldown_path,
        preferred_provider=preferred_provider,
        skip_providers=skip_providers,
    )
    next_provider = selection_info["next_provider"]

    record: dict[str, Any] = {
        "handoff_id": handoff_id,
        "supervision_schema_version": run.SUPERVISION_SCHEMA_VERSION,
        "unattended_supervision": bool(unattended),
        "created_at": run.now(),
        "updated_at": run.now(),
        "mode": "dry_run" if dry_run else "execute",
        "status": "planned_dry_run" if dry_run else "dispatching",
        "caller_kind": caller_kind,
        "role": role,
        "lane": selection_info["lane"],
        "preferred_provider": selection_info["preferred_provider"],
        "excluded_providers": sorted(set(skip_providers or ())),
        "actual_provider": next_provider["name"] if (dry_run and next_provider) else None,
        "returned_to_preferred": selection_info["returned_to_preferred"] if dry_run else False,
        "mid_task_switch_allowed": False,
        "original_ids": {
            "bot": bot,
            "chat_id": chat_id,
            "automation_id": automation_id,
            "run_id": run_id,
            "task_id": None,
            "dispatch_id": None,
            "item_id": item_id,
        },
        "worktree": str(worktree_path),
        "spec_file": str(spec_path),
        "title": title,
        "cooldown": selection_info["cooldowns"],
        "verification_evidence": {
            "policy": selection_info["policy"],
            "candidate_order": [p["name"] for p in selection_info["providers"]],
            "preflight": {
                "planned_provider": next_provider["name"] if next_provider else None,
                "planned_returned_to_preferred": selection_info["returned_to_preferred"],
            },
        },
        "external_action_receipts": [],
        "history": [
            {
                "at": run.now(),
                "event": "handoff_recorded",
                "status": "planned_dry_run" if dry_run else "dispatching",
                "detail": {
                    "caller_kind": caller_kind,
                    "role": role,
                    "lane": selection_info["lane"],
                    "planned_returned_to_preferred": selection_info["returned_to_preferred"],
                },
            }
        ],
        "handoff_file": str(handoff_path),
    }
    run.write_state(handoff_path, record)

    if dry_run:
        return run.redact_secrets(record)

    run.log_operation(
        effective_state_dir, "dispatch_started", handoff_id=handoff_id,
        caller_kind=caller_kind, role=role, lane=record["lane"],
        preferred_provider=record["preferred_provider"],
    )
    job_args = SimpleNamespace(
        worktree=worktree_path,
        spec_file=spec_path,
        title=title,
        role=role,
        lane=lane,
    )
    dispatch_meta = {
        "handoff_id": handoff_id,
        "caller_kind": caller_kind,
        "original_ids": record["original_ids"],
    }

    try:
        result = run.run_job(
            job_args,
            orca=orca,
            skip_providers=skip_providers,
            dispatch_meta=dispatch_meta,
            state_dir=effective_state_dir,
        )
    except Exception as error:
        record["status"] = "blocked"
        record["error"] = run.redact_text(str(error))
        record["updated_at"] = run.now()
        record["history"].append(
            {
                "at": run.now(),
                "event": "worker_runner_blocked",
                "status": "blocked",
                "detail": run.redact_text(str(error)),
            }
        )
        run.write_state(handoff_path, record)
        run.log_operation(
            effective_state_dir, "dispatch_blocked", handoff_id=handoff_id,
            error_type=type(error).__name__,
        )
        raise

    attempts = result.get("attempts", [])
    executed_attempts = [a for a in attempts if "dispatch_id" in a]
    last_executed = executed_attempts[-1] if executed_attempts else None
    actual_provider = result.get("selected_provider") or (
        last_executed.get("provider")
        if (last_executed and result.get("status") == "succeeded")
        else None
    )
    record["status"] = result.get("status", "unknown")
    record["actual_provider"] = actual_provider
    record["returned_to_preferred"] = bool(
        executed_attempts
        and actual_provider == selection_info["preferred_provider"]
        and result.get("status") == "succeeded"
    )
    record["original_ids"]["task_id"] = last_executed.get("task_id") if last_executed else None
    record["original_ids"]["dispatch_id"] = (
        last_executed.get("dispatch_id") if last_executed else None
    )
    record["cooldown"] = _cooldown_snapshot(cooldown_path, selection_info["providers"])
    record["verification_evidence"]["attempts"] = attempts
    record["external_action_receipts"].append(
        {
            "action": "run.run_job",
            "at": run.now(),
            "job_id": result.get("job_id"),
            "state_file": result.get("state_file"),
            "coordinator": result.get("coordinator"),
            "task_id": record["original_ids"]["task_id"],
            "dispatch_id": record["original_ids"]["dispatch_id"],
            "status": result.get("status"),
        }
    )
    record["updated_at"] = run.now()
    record["history"].append(
        {
            "at": run.now(),
            "event": "worker_runner_completed",
            "status": record["status"],
            "detail": {
                "actual_provider": actual_provider,
                "returned_to_preferred": record["returned_to_preferred"],
                "job_id": result.get("job_id"),
            },
        }
    )
    run.write_state(handoff_path, record)
    run.log_operation(
        effective_state_dir, "dispatch_finished", handoff_id=handoff_id,
        status=record["status"], provider=actual_provider, job_id=result.get("job_id"),
        attempts=[
            {
                "provider": attempt.get("provider"),
                "status": attempt.get("status") or attempt.get("worker_state"),
                "classification": (attempt.get("classification") or {}).get("classification"),
            }
            for attempt in attempts
        ],
    )

    if record["status"] == "lane_exhausted":
        import recovery

        supervisor = recovery.RecoverySupervisor(
            adapter=recovery.OrcaRecoveryAdapter(orca=orca),
            state_dir=effective_state_dir,
        )
        rec = supervisor.register_dispatch_exhausted(record, execute=True, unattended=unattended)
        if rec is not None:
            record["recovery_id"] = rec.get("recovery_id")
            record["recovery_status"] = rec.get("status")
            record["delayed_resume_plan"] = rec.get("delayed_resume_plan")
            record["updated_at"] = run.now()
            record["history"].append(
                {
                    "at": run.now(),
                    "event": "durable_recovery_registered",
                    "status": rec.get("status"),
                    "detail": {"recovery_id": rec.get("recovery_id")},
                }
            )
            run.write_state(handoff_path, record)

    return run.redact_secrets(record)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--lane", required=True)
    parser.add_argument("--spec-file", type=Path)
    parser.add_argument("--prompt")
    parser.add_argument(
        "--caller-kind",
        choices=("bot", "automation", "worker", "manual"),
        default="bot",
    )
    parser.add_argument("--bot")
    parser.add_argument("--chat-id")
    parser.add_argument("--automation-id")
    parser.add_argument("--run-id")
    parser.add_argument("--item-id")
    parser.add_argument("--preferred-provider")
    parser.add_argument("--skip-provider", nargs="*", default=None)
    parser.add_argument("--no-unattended", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        result = dispatch_task(
            worktree=args.worktree,
            title=args.title,
            role=args.role,
            lane=args.lane,
            spec_file=args.spec_file,
            prompt=args.prompt,
            caller_kind=args.caller_kind,
            bot=args.bot,
            chat_id=args.chat_id,
            automation_id=args.automation_id,
            run_id=args.run_id,
            item_id=args.item_id,
            preferred_provider=args.preferred_provider,
            skip_providers=set(args.skip_provider) if args.skip_provider else None,
            unattended=not args.no_unattended,
            dry_run=args.dry_run,
        )
    except Exception as error:
        print(run.redact_text(str(error)), file=sys.stderr)
        return 2
    print(json.dumps(run.redact_secrets(result), indent=2, ensure_ascii=False))
    return 0 if result["status"] in ("succeeded", "planned_dry_run") else 1


if __name__ == "__main__":
    raise SystemExit(main())
