#!/usr/bin/env python3
"""Run one Orca worker with provider-only fallback and a durable audit trail."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any
from uuid import uuid4

from catalog import SETTINGS, resolve_policy


ROOT = Path(__file__).resolve().parent
STATE_DIR = Path(
    os.environ.get(
        "ORCA_FALLBACK_STATE_DIR", Path.home() / ".local" / "state" / "orca-fallback"
    )
)
PROVIDER_ERRORS = (
    "usage limit",
    "usage_limit",
    "rate limit",
    "rate_limit",
    "resource_exhausted",
    "insufficient_quota",
    "model returned an invalid object",
    "empty glm result",
    "empty response",
    "authentication timed out",
    "invalid_api_key",
    "authentication_error",
    "provider rejected the configured credentials",
    "provider timed out",
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_performance(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def update_cooldown(path: Path, model_key: str, seconds: int) -> None:
    lock_path = path.with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = json.loads(path.read_text()) if path.exists() else {}
        if seconds:
            data[model_key] = time.time() + seconds
        else:
            data.pop(model_key, None)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2))
        os.replace(temporary, path)


def cooldown_remaining(path: Path, model_key: str) -> int:
    if not path.exists():
        return 0
    data = json.loads(path.read_text())
    return max(0, int(data.get(model_key, 0) - time.time()))


class Orca:
    def __init__(self, executable: str):
        self.executable = executable

    def call(self, *args: str, cwd: Path | None = None, timeout: int = 90) -> dict:
        completed = subprocess.run(
            [self.executable, *args, "--json"],
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"Orca returned invalid JSON: {completed.stderr[-1000:]}"
            ) from error
        if completed.returncode or not result.get("ok", False):
            raise RuntimeError(json.dumps(result.get("error", result), ensure_ascii=False))
        return result.get("result", result)


def write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    os.replace(temporary, path)


def response_text(read_result: dict) -> str:
    transcript = read_result.get("transcript", {})
    messages = transcript.get("messages", [])
    texts = []
    for message in messages:
        if message.get("role") not in ("assistant", "tool"):
            continue
        for block in message.get("blocks", []):
            for key in ("text", "output"):
                value = block.get(key)
                if isinstance(value, str):
                    texts.append(value)
    return "\n".join(texts[-20:])


def is_provider_failure(text: str) -> bool:
    lowered = text.lower()
    return any(pattern in lowered for pattern in PROVIDER_ERRORS)


def worker_is_settled(show: dict) -> bool:
    return show.get("stage") == "settled" and (
        show.get("liveness") in ("exited", "dead")
        or show.get("terminalState") in ("released", "reclaimable")
    )


def start_worker(
    orca: Orca,
    *,
    worktree: Path,
    coordinator: str,
    title: str,
    spec_file: Path,
    provider: dict,
    task_id: str | None,
    retry_of: str | None,
) -> dict:
    args = [
        "orchestration",
        "worker-start",
        "--from",
        coordinator,
        "--worktree",
        "current",
        "--agent",
        provider["agent"],
        "--model",
        provider["model"],
        "--task-title",
        title,
    ]
    if provider.get("effort"):
        args.extend(["--effort", provider["effort"]])
    if task_id and retry_of:
        args.extend(["--task", task_id, "--retry-of", retry_of])
    else:
        args.extend(["--spec-file", str(spec_file)])
    return orca.call(*args, cwd=worktree, timeout=180)


def wait_for_worker(orca: Orca, dispatch: str, poll: int, timeout: int) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        show = orca.call("orchestration", "worker-show", "--dispatch", dispatch)
        state = show.get("state")
        if state in ("succeeded", "failed") or show.get("stage") == "settled":
            return show
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "Worker is still running or its state is uncertain; no fallback was started."
            )
        time.sleep(poll)


def run_job(args: argparse.Namespace) -> dict:
    config = SETTINGS
    providers, policy = resolve_policy(args.role, args.lane)
    if not providers:
        raise ValueError("No models satisfy this role and lane policy")
    worktree = args.worktree.resolve()
    spec_file = args.spec_file.resolve()
    if not worktree.is_dir() or not spec_file.is_file():
        raise ValueError("Worktree and spec file must exist")
    if not spec_file.read_text().strip():
        raise ValueError("The task specification is empty")

    job_id = uuid4().hex[:12]
    state_path = STATE_DIR / f"{job_id}.json"
    performance_path = STATE_DIR / "performance.jsonl"
    cooldown_path = STATE_DIR / "cooldowns.json"
    state: dict[str, Any] = {
        "job_id": job_id,
        "title": args.title,
        "worktree": str(worktree),
        "spec_file": str(spec_file),
        "role": args.role,
        "lane": policy["lane"],
        "policy": policy,
        "started_at": now(),
        "attempts": [],
        "status": "starting",
    }
    write_state(state_path, state)
    orca = Orca(config["orca_cli"])
    terminal = orca.call(
        "terminal",
        "create",
        "--worktree",
        "current",
        "--title",
        f"Fallback coordinator {job_id}",
        "--command",
        "/bin/bash",
        cwd=worktree,
    )["terminal"]
    coordinator = terminal["handle"]
    state["coordinator"] = coordinator
    state["state_file"] = str(state_path)
    write_state(state_path, state)

    task_id = None
    previous_dispatch = None
    try:
        for provider in providers:
            remaining = cooldown_remaining(cooldown_path, provider["name"])
            if remaining:
                state["attempts"].append(
                    {
                        "provider": provider["name"],
                        "model": provider["model"],
                        "status": "cooldown_skipped",
                        "remaining_seconds": remaining,
                    }
                )
                write_state(state_path, state)
                continue
            attempt = {
                "provider": provider["name"],
                "agent": provider["agent"],
                "model": provider["model"],
                "started_at": now(),
            }
            state["attempts"].append(attempt)
            attempt_started = time.monotonic()
            state["status"] = "running"
            write_state(state_path, state)
            started = start_worker(
                orca,
                worktree=worktree,
                coordinator=coordinator,
                title=args.title,
                spec_file=spec_file,
                provider=provider,
                task_id=task_id,
                retry_of=previous_dispatch,
            )
            task_id = started["taskId"]
            dispatch = started["dispatchId"]
            previous_dispatch = dispatch
            attempt.update({"task_id": task_id, "dispatch_id": dispatch})
            write_state(state_path, state)

            show = wait_for_worker(
                orca,
                dispatch,
                config["poll_seconds"],
                config["worker_timeout_seconds"],
            )
            attempt["worker_state"] = show.get("state")
            if show.get("state") == "succeeded":
                attempt["latency_seconds"] = round(time.monotonic() - attempt_started, 3)
                attempt["finished_at"] = now()
                state.update(
                    status="succeeded",
                    selected_provider=provider["name"],
                    selected_model=provider["model"],
                    finished_at=now(),
                )
                write_state(state_path, state)
                append_performance(
                    performance_path,
                    {
                        "recorded_at": now(),
                        "job_id": job_id,
                        "role": args.role,
                        "model_key": provider["name"],
                        "outcome": "succeeded",
                        "latency_seconds": attempt["latency_seconds"],
                    },
                )
                update_cooldown(cooldown_path, provider["name"], 0)
                return state

            if not worker_is_settled(show):
                raise RuntimeError(
                    "Worker failed but may still be active; no fallback was started."
                )
            read_result = orca.call(
                "orchestration",
                "worker-read",
                "--dispatch",
                dispatch,
                "--source",
                "auto",
                "--limit",
                "80",
            )
            error_text = response_text(read_result)
            attempt["provider_failure"] = is_provider_failure(error_text)
            attempt["error_excerpt"] = error_text[-2000:]
            attempt["latency_seconds"] = round(time.monotonic() - attempt_started, 3)
            attempt["finished_at"] = now()
            write_state(state_path, state)
            append_performance(
                performance_path,
                {
                    "recorded_at": now(),
                    "job_id": job_id,
                    "role": args.role,
                    "model_key": provider["name"],
                    "outcome": "provider_failed" if attempt["provider_failure"] else "task_failed",
                    "latency_seconds": attempt["latency_seconds"],
                },
            )
            if not attempt["provider_failure"]:
                state.update(status="failed_non_provider", finished_at=now())
                write_state(state_path, state)
                return state
            update_cooldown(
                cooldown_path,
                provider["name"],
                config.get("provider_cooldown_seconds", 900),
            )

        state.update(
            status="lane_exhausted",
            escalation_required=not policy["allow_lane_escalation"],
            finished_at=now(),
        )
        write_state(state_path, state)
        return state
    except Exception as error:
        state.update(status="blocked", error=str(error), finished_at=now())
        write_state(state_path, state)
        raise
    finally:
        if state.get("status") != "running":
            try:
                orca.call("terminal", "close", "--terminal", coordinator)
            except Exception:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument("--spec-file", type=Path, required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--role", default="implementation")
    parser.add_argument("--lane")
    args = parser.parse_args()
    try:
        result = run_job(args)
    except Exception as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
