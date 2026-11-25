#!/usr/bin/env python3
"""Run one Orca worker with provider-only fallback and a durable audit trail."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
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
    "service unavailable",
    "overloaded_error",
)
HUMAN_WAIT_STATES = (
    "waiting",
    "waiting_for_input",
    "waiting_for_human",
    "awaiting_approval",
    "needs_input",
    "paused_for_input",
)
HUMAN_WAIT_PATTERNS = (
    "waiting for human",
    "human approval required",
    "human input required",
    "approval required",
    "waiting for user approval",
    "waiting on a prompt only a human can answer",
)
TASK_ERROR_CODES = frozenset(
    {
        "test_failed",
        "lint_failed",
        "build_failed",
        "task_failed",
        "non_zero_exit",
        "assertion_error",
    }
)
SECRET_KEY_NAMES = {
    "api_key",
    "apikey",
    "secret",
    "client_secret",
    "access_token",
    "refresh_token",
    "auth_token",
    "bot_token",
    "bearer_token",
    "password",
    "passwd",
    "authorization",
    "pairing_code",
    "private_key",
    "cookie",
    "credentials",
}
SECRET_VALUE_PATTERNS = (
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._\-+/=]{8,}", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(Basic\s+)[A-Za-z0-9+/=]{8,}", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"\bsk-(?:ant-|proj-|or-)?[A-Za-z0-9_\-]{12,}\b"), "[REDACTED]"),
    (re.compile(r"\b(?:ghp|gho|ghs|ghu|github_pat)_[A-Za-z0-9_]{16,}\b"), "[REDACTED]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b"), "[REDACTED]"),
    (re.compile(r"\bAIza[0-9A-Za-z\-_]{20,}\b"), "[REDACTED]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED]"),
    (re.compile(r"\blin_api_[A-Za-z0-9]{16,}\b"), "[REDACTED]"),
    (re.compile(r"orca://pair\?[^\s\"']+"), "orca://pair?[REDACTED]"),
    (
        re.compile(
            r"((?:api[_-]?key|token|secret|password|passwd|authorization|pairing[_-]?code|client[_-]?secret|access[_-]?token|refresh[_-]?token)\s*[:=]\s*['\"]?)([^\s'\",;}{]+)",
            re.IGNORECASE,
        ),
        r"\1[REDACTED]",
    ),
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_performance(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(redact_secrets(record), ensure_ascii=False) + "\n")


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


def cooldown_remaining(path: Path, model_key: str, *, at_ts: float | None = None) -> int:
    if not path.exists():
        return 0
    data = json.loads(path.read_text())
    now_ts = at_ts if at_ts is not None else time.time()
    return max(0, int(data.get(model_key, 0) - now_ts))


def cooldown_reset_at(path: Path, model_key: str, *, at_ts: float | None = None) -> str | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    expires = data.get(model_key)
    now_ts = at_ts if at_ts is not None else time.time()
    if not isinstance(expires, (int, float)) or expires <= now_ts:
        return None
    return datetime.fromtimestamp(expires, tz=timezone.utc).isoformat()


def redact_text(value: str) -> str:
    redacted = value
    for pattern, replacement in SECRET_VALUE_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def redact_secrets(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            key_lower = str(key).lower().replace("-", "_")
            if key_lower in SECRET_KEY_NAMES and item not in (None, "", [], {}):
                sanitized[key] = "[REDACTED]"
            else:
                sanitized[key] = redact_secrets(item)
        return sanitized
    return value


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
            sanitized_err = redact_text(completed.stderr)[-1000:]
            raise RuntimeError(f"Orca returned invalid JSON: {sanitized_err}") from error
        if completed.returncode or not result.get("ok", False):
            err_payload = redact_secrets(result.get("error", result))
            raise RuntimeError(json.dumps(err_payload, ensure_ascii=False))
        return result.get("result", result)


def write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    sanitized = redact_secrets(state)
    temporary.write_text(json.dumps(sanitized, indent=2, ensure_ascii=False))
    os.replace(temporary, path)


def _message_texts(message: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    for field in ("text", "content", "output", "error"):
        val = message.get(field)
        if isinstance(val, str) and val:
            texts.append(val)
    for block in message.get("blocks", []):
        if isinstance(block, dict):
            for key in ("text", "output"):
                value = block.get(key)
                if isinstance(value, str) and value:
                    texts.append(value)
    return texts


def response_text(read_result: dict) -> str:
    """Return assistant/tool text from transcript (preserving legacy helper contract)."""
    transcript = read_result.get("transcript", {})
    messages = transcript.get("messages", [])
    texts = []
    for message in messages:
        if message.get("role") not in ("assistant", "tool"):
            continue
        texts.extend(_message_texts(message))
    return "\n".join(texts[-20:])


def latest_turn_failure_text(read_result: dict) -> str:
    """Extract only the latest turn's assistant/error failure text, ignoring stale earlier turns."""
    transcript = read_result.get("transcript", {})
    messages = transcript.get("messages")
    if not isinstance(messages, list):
        messages = read_result.get("messages")
    if not isinstance(messages, list) or not messages:
        err = read_result.get("error")
        return err if isinstance(err, str) else ""

    last_user_idx = -1
    for idx, msg in enumerate(messages):
        if isinstance(msg, dict) and str(msg.get("role", "")).lower() == "user":
            last_user_idx = idx
    turn_messages = [m for m in messages[last_user_idx + 1 :] if isinstance(m, dict)]
    if not turn_messages:
        return ""

    assistant_msgs = [
        m for m in turn_messages if str(m.get("role", "")).lower() in ("assistant", "bot", "error")
    ]
    if assistant_msgs:
        return "\n".join(_message_texts(assistant_msgs[-1]))

    tool_msgs = [m for m in turn_messages if str(m.get("role", "")).lower() == "tool"]
    if tool_msgs:
        return "\n".join(_message_texts(tool_msgs[-1]))
    return ""


def is_provider_failure(text: str) -> bool:
    lowered = text.lower()
    return any(pattern in lowered for pattern in PROVIDER_ERRORS)


def is_human_input_wait(status_record: dict[str, Any] | None, text: str = "") -> bool:
    record = status_record or {}
    observation = record.get("observation")
    if isinstance(observation, dict) and observation.get("agentWait"):
        return True
    if record.get("agentWait") or record.get("waitingForHuman") or record.get("pendingApproval"):
        return True
    state = str(record.get("state") or record.get("status") or "").lower()
    if state in HUMAN_WAIT_STATES:
        return True
    lowered = text.lower()
    return any(pattern in lowered for pattern in HUMAN_WAIT_PATTERNS)


def classify_worker_failure(status_record: dict[str, Any] | None, text: str = "") -> dict[str, Any]:
    record = status_record or {}
    if is_human_input_wait(record, text):
        return {
            "classification": "human_input_wait",
            "eligible_for_fallback": False,
            "reason": "Target is waiting for human approval or input.",
        }
    error_code = str(record.get("errorCode") or record.get("failureType") or "").lower()
    if error_code in TASK_ERROR_CODES:
        return {
            "classification": "task_failure",
            "eligible_for_fallback": False,
            "reason": f"Explicit non-provider errorCode={error_code}.",
        }
    if str(record.get("providerError") or "").lower() in ("true", "1") or record.get("providerError") is True:
        return {
            "classification": "provider_failure",
            "eligible_for_fallback": True,
            "reason": "Structured providerError flag on target record.",
        }
    parts = [text]
    for field in ("error", "lastError", "failureReason", "message"):
        val = record.get(field)
        if isinstance(val, str) and val:
            parts.append(val)
    if is_provider_failure("\n".join(parts)):
        return {
            "classification": "provider_failure",
            "eligible_for_fallback": True,
            "reason": "Matched provider availability/quota/auth failure pattern.",
        }
    return {
        "classification": "task_failure",
        "eligible_for_fallback": False,
        "reason": "No provider availability error matched; treating as task/test/lint failure.",
    }


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
        if is_human_input_wait(show):
            return show
        if state in ("succeeded", "failed") and worker_is_settled(show):
            return show
        if show.get("stage") == "settled" and worker_is_settled(show) and state not in (
            None,
            "",
            "running",
            "starting",
            "queued",
            "pending",
        ):
            return show
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "Worker is still running or its state is uncertain; no fallback was started."
            )
        time.sleep(poll)


def run_job(
    args: argparse.Namespace,
    *,
    orca: Orca | None = None,
    skip_providers: set[str] | None = None,
    only_provider: str | None = None,
    dispatch_meta: dict[str, Any] | None = None,
    state_dir: Path | None = None,
) -> dict:
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

    effective_state_dir = state_dir or STATE_DIR
    job_id = uuid4().hex[:12]
    state_path = effective_state_dir / f"{job_id}.json"
    performance_path = effective_state_dir / "performance.jsonl"
    cooldown_path = effective_state_dir / "cooldowns.json"
    state: dict[str, Any] = {
        "job_id": job_id,
        "title": args.title,
        "worktree": str(worktree),
        "spec_file": str(spec_file),
        "role": args.role,
        "lane": policy["lane"],
        "policy": policy,
        "preferred_provider": providers[0]["name"],
        "started_at": now(),
        "attempts": [],
        "status": "starting",
    }
    if dispatch_meta:
        state["dispatch_meta"] = dispatch_meta
    write_state(state_path, state)
    orca = orca or Orca(config["orca_cli"])
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
    attempted = set(skip_providers or ())
    worker_settled = False
    try:
        candidate_providers = (
            [p for p in providers if p["name"] == only_provider] if only_provider else providers
        )
        if only_provider and not candidate_providers:
            raise ValueError(f"Requested provider {only_provider!r} is not eligible in {args.role}/{policy['lane']}")

        for provider in candidate_providers:
            if provider["name"] in attempted:
                state["attempts"].append(
                    {
                        "provider": provider["name"],
                        "model": provider["model"],
                        "status": "already_attempted_skipped",
                    }
                )
                write_state(state_path, state)
                continue
            remaining = cooldown_remaining(cooldown_path, provider["name"])
            if remaining:
                state["attempts"].append(
                    {
                        "provider": provider["name"],
                        "model": provider["model"],
                        "status": "cooldown_skipped",
                        "remaining_seconds": remaining,
                        "reset_at": cooldown_reset_at(cooldown_path, provider["name"]),
                    }
                )
                write_state(state_path, state)
                continue
            attempted.add(provider["name"])
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
            if is_human_input_wait(show):
                attempt["human_input_wait"] = True
                attempt["latency_seconds"] = round(time.monotonic() - attempt_started, 3)
                attempt["finished_at"] = now()
                state.update(
                    status="waiting_for_human",
                    selected_provider=provider["name"],
                    selected_model=provider["model"],
                    coordinator_retained=True,
                    finished_at=now(),
                )
                write_state(state_path, state)
                return redact_secrets(state)
            if show.get("state") == "succeeded":
                if not worker_is_settled(show):
                    raise RuntimeError(
                        "Worker reported succeeded without verified settlement; state is uncertain."
                    )
                worker_settled = True
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
                return redact_secrets(state)

            if show.get("state") != "failed" or not worker_is_settled(show):
                raise RuntimeError(
                    "Worker settled without a confirmed failed outcome or may still be active; no fallback was started."
                )
            worker_settled = True
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
            error_text = latest_turn_failure_text(read_result)
            classification = classify_worker_failure(show, error_text)
            attempt["human_input_wait"] = classification["classification"] == "human_input_wait"
            attempt["provider_failure"] = classification["eligible_for_fallback"]
            attempt["classification"] = classification
            attempt["error_excerpt"] = redact_text(error_text)[-2000:]
            attempt["latency_seconds"] = round(time.monotonic() - attempt_started, 3)
            attempt["finished_at"] = now()
            write_state(state_path, state)
            if attempt["human_input_wait"]:
                state.update(
                    status="waiting_for_human",
                    selected_provider=provider["name"],
                    selected_model=provider["model"],
                    coordinator_retained=True,
                    finished_at=now(),
                )
                write_state(state_path, state)
                return redact_secrets(state)
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
                return redact_secrets(state)
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
        return redact_secrets(state)
    except Exception as error:
        state.update(
            status="blocked",
            error=redact_text(str(error)),
            coordinator_retained=not worker_settled,
            finished_at=now(),
        )
        write_state(state_path, state)
        raise
    finally:
        if worker_settled and state.get("status") in (
            "succeeded",
            "failed_non_provider",
            "lane_exhausted",
        ):
            try:
                orca.call("terminal", "close", "--terminal", coordinator)
            except Exception as close_error:
                state["coordinator_close_error"] = redact_text(str(close_error))
                write_state(state_path, state)


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
        print(redact_text(str(error)), file=sys.stderr)
        return 2
    print(json.dumps(redact_secrets(result), indent=2, ensure_ascii=False))
    return 0 if result["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
