#!/usr/bin/env python3
"""Bounded recovery supervisor for Orca workers, bot chats, and automations."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from typing import Any, Iterator
from uuid import uuid4

from catalog import SETTINGS, resolve_policy
import run


SETTLED_FAILED_RUN_STATES = frozenset({"failed", "error", "errored", "timed_out", "dispatch_failed"})
TERMINAL_FAILED_RUN_STATES = frozenset(
    {"failed", "error", "errored", "timed_out", "cancelled", "canceled", "dispatch_failed"}
)
ACTIVE_RUN_STATES = frozenset(
    {
        "running",
        "active",
        "queued",
        "pending",
        "starting",
        "started",
        "accepted",
        "dispatching",
        "dispatched",
        "in_progress",
        "streaming",
    }
)
RESUMABLE_DELAYED_STATUSES = frozenset({"exhausted_delayed_resume", "probe_failed", "waiting_for_reset"})
SAFE_BOT_META_KEYS = ("id", "slug", "name", "displayName", "bot", "handle")
RECEIPT_ALLOWLIST_KEYS = (
    "id",
    "runId",
    "automationId",
    "status",
    "state",
    "enabled",
    "job_id",
    "state_file",
    "task_id",
    "dispatch_id",
    "probe_type",
    "agent",
    "provider",
    "model",
    "returncode",
    "exact_ok",
    "ok",
    "error",
)
RECOVERY_ID_RE = re.compile(r"^[0-9a-f]{12}$")
HEALTH_PROBE_PROMPT = "Reply with exact text OK and nothing else."
EXTRA_MODEL_RE = re.compile(r"(?:--model|-m)(?:\s+|=)([^\s'\"]+)")
TUI_PROVIDER_ERROR_MARKERS = ("unexpected status", "provider error")


def _recovery_settings() -> dict[str, Any]:
    defaults = {
        "auto_resume_automation": False,
        "return_to_preferred_on_next_task": True,
        "delayed_resume_delay_seconds": SETTINGS.get("provider_cooldown_seconds", 900),
        "health_probe_timeout_seconds": 30,
        "bots_dir": "~/.orca/bots",
        "once_deadline_seconds": 300,
    }
    configured = SETTINGS.get("recovery")
    if isinstance(configured, dict):
        defaults.update(configured)
    return defaults


def _parse_iso(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _iso_from_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _key_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def _is_exact_ok(text: str) -> bool:
    return text.strip() == "OK"


def _allowlisted_receipt(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    out: dict[str, Any] = {}
    for key in RECEIPT_ALLOWLIST_KEYS:
        if key in payload and payload[key] is not None:
            val = payload[key]
            if key == "error" and isinstance(val, str):
                out[key] = run.redact_text(val)[:200]
            else:
                out[key] = run.redact_secrets(val)
    return out


def _provider_key(agent: Any, model: Any) -> str | None:
    if isinstance(agent, str) and agent.strip() and isinstance(model, str) and model.strip():
        return f"{agent.strip()}:{model.strip()}"
    return None


def _unwrap_automation(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    inner = payload.get("automation")
    return inner if isinstance(inner, dict) else payload


def _automation_worktree(auto: dict[str, Any]) -> str | None:
    for candidate in (
        auto.get("workspace"),
        auto.get("worktree"),
        (auto.get("runContext") or {}).get("path") if isinstance(auto.get("runContext"), dict) else None,
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    ws_id = auto.get("workspaceId")
    if isinstance(ws_id, str) and "::" in ws_id:
        path_part = ws_id.split("::", 1)[1].strip()
        if path_part:
            return path_part
    return None


def _automation_bot_id(auto: dict[str, Any]) -> str | None:
    bot_field = auto.get("bot")
    if isinstance(bot_field, dict):
        bid = bot_field.get("botId") or bot_field.get("id")
        if isinstance(bid, str) and bid.strip():
            return bid.strip()
    elif isinstance(bot_field, str) and bot_field.strip():
        return bot_field.strip()
    bid = auto.get("botId")
    return bid.strip() if isinstance(bid, str) and bid.strip() else None


def _automation_results_to(auto: dict[str, Any]) -> str | None:
    bot_field = auto.get("bot")
    if isinstance(bot_field, dict):
        val = bot_field.get("resultsTo")
        if isinstance(val, str) and val.strip():
            return val.strip()
    val = auto.get("resultsTo")
    return val.strip() if isinstance(val, str) and val.strip() else None


def _automation_provider_key(auto: dict[str, Any]) -> str | None:
    if isinstance(auto.get("modelKey"), str) and auto["modelKey"].strip():
        return auto["modelKey"].strip()
    bot_field = auto.get("bot") if isinstance(auto.get("bot"), dict) else {}
    agent_setup = bot_field.get("agentSetup") if isinstance(bot_field.get("agentSetup"), dict) else {}
    launch = agent_setup.get("launch") if isinstance(agent_setup.get("launch"), dict) else {}
    agent = agent_setup.get("agent") or auto.get("agent") or auto.get("provider") or auto.get("agentId")
    model = launch.get("model") or auto.get("model")
    if not model:
        extra = auto.get("extraAgentArgs") or (
            (auto.get("launchSetup") or {}).get("extraAgentArgs")
            if isinstance(auto.get("launchSetup"), dict)
            else None
        )
        if isinstance(extra, str):
            match = EXTRA_MODEL_RE.search(extra)
            if match:
                model = match.group(1)
    return _provider_key(agent, model)


def _extract_run_text(target_run: dict[str, Any]) -> str:
    parts: list[str] = []
    for field in ("error", "output", "excerpt", "summary"):
        val = target_run.get(field)
        if isinstance(val, str) and val:
            parts.append(val)
    snapshot = target_run.get("outputSnapshot")
    if isinstance(snapshot, dict) and isinstance(snapshot.get("content"), str) and snapshot["content"]:
        parts.append(snapshot["content"])
    return "\n".join(parts)


def _run_output_has_provider_failure(target_run: dict[str, Any]) -> bool:
    snapshot = target_run.get("outputSnapshot")
    content = snapshot.get("content") if isinstance(snapshot, dict) else None
    text = "\n".join(
        val for val in (target_run.get("error"), content) if isinstance(val, str) and val
    )
    if not text:
        return False
    lowered = text.lower()
    return run.is_provider_failure(text) or all(m in lowered for m in TUI_PROVIDER_ERROR_MARKERS)


def _completed_run_has_evidence(target_run: dict[str, Any]) -> bool:
    return any(
        target_run.get(field) is not None
        for field in ("outputSnapshot", "output", "terminalSessionId", "chatSessionId", "finishedAt", "completedAt")
    )


def _extract_agent_show_states(show: dict[str, Any]) -> list[str]:
    states: list[str] = []
    for key in ("state", "status"):
        val = show.get(key)
        if isinstance(val, str) and val.strip():
            states.append(val.strip().lower())
    agent_obj = show.get("agent")
    if isinstance(agent_obj, dict):
        val = agent_obj.get("state") or agent_obj.get("status")
        if isinstance(val, str) and val.strip():
            states.append(val.strip().lower())
    for list_key in ("entries", "panes", "sessions", "places"):
        items = show.get(list_key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    val = item.get("state") or item.get("status")
                    if isinstance(val, str) and val.strip():
                        states.append(val.strip().lower())
    return states


def _extract_bot_messages(read_payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("messages", "entries", "turns"):
        items = read_payload.get(key)
        if isinstance(items, list):
            return [m for m in items if isinstance(m, dict)]
    return []


def _extract_latest_bot_user_ask(read_payload: dict[str, Any]) -> str | None:
    messages = _extract_bot_messages(read_payload)
    for msg in reversed(messages):
        if str(msg.get("role") or msg.get("author") or "").lower() == "user":
            text = "\n".join(run._message_texts(msg)).strip()
            if text:
                return text
    return None


def _has_side_effect_or_claim(record: dict[str, Any]) -> bool:
    if record.get("mutations") or record.get("external_action_receipts") or record.get("launch_intent"):
        return True
    return record.get("status") in (
        "in_progress",
        "paused_original",
        "waiting_for_run",
        "recovered",
        "recovery_run_failed",
        "exhausted_delayed_resume",
        "probe_failed",
        "waiting_for_reset",
    )


def _default_bots_dir() -> Path:
    configured = _recovery_settings()["bots_dir"]
    user_data = os.environ.get("ORCA_USER_DATA_PATH", "").strip()
    if user_data and configured == "~/.orca/bots":
        ud_path = Path(user_data).expanduser().resolve()
        home = Path.home().resolve()
        default_dirs = {
            (home / ".orca").resolve(),
            (home / ".config" / "Orca").resolve(),
            (home / ".config" / "orca").resolve(),
            (home / ".config" / "orca-dev").resolve(),
        }
        if ud_path not in default_dirs:
            return ud_path / "bots"
    return Path(configured).expanduser()


class OrcaRecoveryAdapter:
    """Thin adapter around public Orca CLI commands and bounded bot metadata."""

    def __init__(self, orca: run.Orca | None = None, *, bots_dir: Path | None = None):
        self.orca = orca or run.Orca(SETTINGS["orca_cli"])
        self.bots_dir = bots_dir or _default_bots_dir()

    def worker_show(self, dispatch_id: str) -> dict[str, Any]:
        return self.orca.call("orchestration", "worker-show", "--dispatch", dispatch_id)

    def worker_read(self, dispatch_id: str) -> dict[str, Any]:
        return self.orca.call(
            "orchestration", "worker-read", "--dispatch", dispatch_id, "--source", "auto", "--limit", "80"
        )

    def bot_chats(self, bot: str) -> dict[str, Any]:
        return self.orca.call("bot", "chats", "--bot", bot, "--all")

    def agent_show(self, session_id: str) -> dict[str, Any]:
        arg = session_id if session_id.startswith("orca_session_id:") else f"orca_session_id:{session_id}"
        return self.orca.call("agent", "show", arg)

    def agent_read(self, session_id: str, limit: int = 20) -> dict[str, Any]:
        arg = session_id if session_id.startswith("orca_session_id:") else f"orca_session_id:{session_id}"
        return self.orca.call("agent", "read", arg, "--limit", str(limit))

    def automations_list(self) -> dict[str, Any]:
        return self.orca.call("automations", "list")

    def automations_show(self, automation_id: str) -> dict[str, Any]:
        return _unwrap_automation(self.orca.call("automations", "show", automation_id))

    def automations_runs(self, automation_id: str) -> dict[str, Any]:
        return self.orca.call("automations", "runs", "--id", automation_id, "--limit", "20")

    def automations_edit_enabled(self, automation_id: str, enabled: bool) -> dict[str, Any]:
        return _unwrap_automation(
            self.orca.call("automations", "edit", automation_id, "--enabled" if enabled else "--disabled")
        )

    def create_one_time_bot_automation(
        self,
        *,
        name: str,
        bot: str,
        provider: dict[str, Any],
        prompt: str,
        by_iso: str,
        item_id: str | None = None,
        results_to: str | None = None,
        enabled: bool = False,
    ) -> dict[str, Any]:
        args = [
            "automations",
            "create",
            "--name",
            name,
            "--bot",
            bot,
            "--runs-in",
            "new",
            "--once",
            "--by",
            by_iso,
            "--agent",
            provider["agent"],
            "--model",
            provider["model"],
            "--prompt",
            prompt,
            "--enabled" if enabled else "--disabled",
        ]
        if provider.get("effort"):
            args.extend(["--effort", provider["effort"]])
        if item_id is not None and str(item_id).isdigit():
            args.extend(["--item", str(item_id)])
        if results_to:
            args.extend(["--results-to", results_to])
        return _unwrap_automation(self.orca.call(*args))

    def automations_run(self, automation_id: str) -> dict[str, Any]:
        return self.orca.call("automations", "run", automation_id)

    def _resolve_bot_dir(self, bot: str) -> tuple[Path | None, dict[str, Any]]:
        if not self.bots_dir.is_dir():
            return None, {}
        target_norm = bot.strip().lower()
        candidates: list[Path] = []
        direct = self.bots_dir / bot
        if direct.is_dir():
            candidates.append(direct)
        for child in sorted(self.bots_dir.iterdir()):
            if child.is_dir() and child not in candidates:
                candidates.append(child)

        for folder in candidates:
            bot_json_path = folder / "bot.json"
            safe_meta: dict[str, Any] = {}
            if bot_json_path.is_file():
                try:
                    raw = json.loads(bot_json_path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        safe_meta = {
                            k: raw[k] for k in SAFE_BOT_META_KEYS if isinstance(raw.get(k), str) and raw[k].strip()
                        }
                except (OSError, json.JSONDecodeError):
                    safe_meta = {}
            match_values = [folder.name.lower(), *(v.strip().lower() for v in safe_meta.values())]
            if target_norm in match_values:
                return folder, safe_meta
        return None, {}

    def read_bot_assets(self, bot: str) -> dict[str, Any]:
        folder, safe_meta = self._resolve_bot_dir(bot)
        if folder is None:
            return {
                "bot_input": bot,
                "resolved_bot": bot,
                "bot_dir": str(self.bots_dir / bot),
                "exists": False,
                "soul_present": False,
                "memory_present": False,
                "items_present": False,
            }
        resolved_bot = safe_meta.get("id") or safe_meta.get("slug") or folder.name
        return {
            "bot_input": bot,
            "resolved_bot": resolved_bot,
            "bot_dir": str(folder),
            "exists": True,
            "soul_present": (folder / "SOUL.md").is_file(),
            "memory_present": (folder / "MEMORY.md").is_file(),
            "items_present": (folder / "items").exists() or (folder / "items.json").is_file(),
        }

    def probe_health(self, provider: dict[str, Any], timeout_seconds: int = 30) -> dict[str, Any]:
        agent, model = provider["agent"], provider["model"]
        if agent not in ("codex", "antigravity"):
            return {
                "ok": False,
                "probe_type": "inference",
                "agent": agent,
                "provider": provider["name"],
                "model": model,
                "exact_ok": False,
                "error": f"unsupported_probe_agent:{agent}",
            }
        try:
            with tempfile.TemporaryDirectory(prefix="orca-health-probe-") as tmpdir:
                iso_cwd = Path(tmpdir)
                if agent == "codex":
                    cmd = [
                        SETTINGS.get("sources", {}).get("codex_command", "codex"),
                        "exec",
                        "--skip-git-repo-check",
                        "--ephemeral",
                        "-s",
                        "read-only",
                        "-m",
                        model,
                        "-o",
                        str(iso_cwd / "probe.txt"),
                        "-",
                    ]
                    completed = subprocess.run(
                        cmd,
                        cwd=iso_cwd,
                        input=HEALTH_PROBE_PROMPT,
                        capture_output=True,
                        text=True,
                        timeout=timeout_seconds,
                        check=False,
                    )
                    answer = (iso_cwd / "probe.txt").read_text(encoding="utf-8") if (iso_cwd / "probe.txt").is_file() else ""
                else:
                    cmd = [
                        SETTINGS.get("sources", {}).get("agy_command", "agy"),
                        "--output-format",
                        "text",
                        "--sandbox",
                        "--disable-slash-commands",
                        "--model",
                        model,
                        "--print-timeout",
                        f"{timeout_seconds}s",
                        f"--print={HEALTH_PROBE_PROMPT}",
                    ]
                    completed = subprocess.run(
                        cmd,
                        cwd=iso_cwd,
                        capture_output=True,
                        text=True,
                        timeout=timeout_seconds,
                        check=False,
                    )
                    answer = completed.stdout
        except (subprocess.TimeoutExpired, OSError) as error:
            return {
                "ok": False,
                "probe_type": "inference",
                "agent": agent,
                "provider": provider["name"],
                "model": model,
                "exact_ok": False,
                "error": run.redact_text(str(error))[:160],
            }
        ok = completed.returncode == 0 and _is_exact_ok(answer)
        result: dict[str, Any] = {
            "ok": ok,
            "probe_type": "inference",
            "agent": agent,
            "provider": provider["name"],
            "model": model,
            "returncode": completed.returncode,
            "exact_ok": ok,
        }
        if not ok:
            hint = completed.stderr.strip() or f"unexpected_probe_output(len={len(answer.strip())})"
            result["error"] = run.redact_text(hint)[:160]
        return result


class RecoverySupervisor:
    """Durable, idempotent, same-lane recovery supervisor defaulting to dry-run."""

    def __init__(
        self,
        *,
        adapter: OrcaRecoveryAdapter | None = None,
        state_dir: Path | None = None,
        settings: dict[str, Any] | None = None,
    ):
        self.adapter = adapter or OrcaRecoveryAdapter()
        self._explicit_state_dir = state_dir
        self._override_settings = settings

    @property
    def state_dir(self) -> Path:
        return self._explicit_state_dir or run.STATE_DIR

    @property
    def recoveries_dir(self) -> Path:
        return self.state_dir / "recoveries"

    @property
    def by_key_dir(self) -> Path:
        return self.recoveries_dir / "by_key"

    @property
    def aliases_dir(self) -> Path:
        return self.recoveries_dir / "aliases"

    @property
    def cooldown_path(self) -> Path:
        return self.state_dir / "cooldowns.json"

    def _settings(self) -> dict[str, Any]:
        merged = _recovery_settings()
        if self._override_settings:
            merged.update(self._override_settings)
        return merged

    @contextmanager
    def _lock_key(self, lock_key: str) -> Iterator[None]:
        self.by_key_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.by_key_dir / f"{_key_hash(lock_key)}.lock"
        with lock_path.open("w", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    def _load_by_key(self, key: str) -> dict[str, Any] | None:
        path = self.by_key_dir / f"{_key_hash(key)}.json"
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data.get("idempotency_key") == key else None

    def _load_alias(self, caller_key: str) -> dict[str, Any] | None:
        alias_path = self.aliases_dir / f"{_key_hash(caller_key)}.json"
        if not alias_path.is_file():
            return None
        alias = json.loads(alias_path.read_text(encoding="utf-8"))
        canonical_key = alias.get("canonical_key") if isinstance(alias, dict) else None
        if isinstance(canonical_key, str):
            return self._load_by_key(canonical_key)
        return None

    def _save_alias(self, caller_key: str | None, canonical_key: str, recovery_id: str) -> None:
        if caller_key and caller_key != canonical_key:
            run.write_state(
                self.aliases_dir / f"{_key_hash(caller_key)}.json",
                {"caller_key": caller_key, "canonical_key": canonical_key, "recovery_id": recovery_id},
            )

    def load_record(self, recovery_id_or_key: str) -> dict[str, Any] | None:
        if RECOVERY_ID_RE.fullmatch(recovery_id_or_key):
            by_id = self.recoveries_dir / f"{recovery_id_or_key}.json"
            if by_id.is_file():
                data = json.loads(by_id.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("recovery_id") == recovery_id_or_key:
                    return data
            return None
        return self._load_by_key(recovery_id_or_key) or self._load_alias(recovery_id_or_key)

    def _persist(self, record: dict[str, Any], *, claim_idempotency: bool) -> dict[str, Any]:
        record["updated_at"] = run.now()
        sanitized = run.redact_secrets(record)
        run.write_state(self.recoveries_dir / f"{sanitized['recovery_id']}.json", sanitized)
        if claim_idempotency:
            run.write_state(self.by_key_dir / f"{_key_hash(sanitized['idempotency_key'])}.json", sanitized)
            self._save_alias(
                sanitized.get("caller_idempotency_key"),
                sanitized["idempotency_key"],
                sanitized["recovery_id"],
            )
        return sanitized

    def _record_event(self, record: dict[str, Any], event: str, status: str, detail: Any = None) -> None:
        record["status"] = status
        entry: dict[str, Any] = {"at": run.now(), "event": event, "status": status}
        if detail is not None:
            entry["detail"] = run.redact_secrets(detail)
        record["history"].append(entry)

    def _find_local_audit(
        self,
        *,
        dispatch_id: str | None = None,
        task_id: str | None = None,
        automation_id: str | None = None,
        chat_id: str | None = None,
    ) -> dict[str, Any] | None:
        if not self.state_dir.is_dir():
            return None
        files: list[Path] = [
            p for p in sorted(self.state_dir.glob("*.json")) if p.name != "cooldowns.json"
        ]
        dispatches_dir = self.state_dir / "dispatches"
        if dispatches_dir.is_dir():
            files.extend(sorted(dispatches_dir.glob("*.json")))
        for path in reversed(files):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            orig = data.get("original_ids") if isinstance(data.get("original_ids"), dict) else {}
            attempts = data.get("attempts") if isinstance(data.get("attempts"), list) else []
            if dispatch_id and (
                orig.get("dispatch_id") == dispatch_id
                or any(a.get("dispatch_id") == dispatch_id for a in attempts if isinstance(a, dict))
            ):
                return data
            if task_id and (
                orig.get("task_id") == task_id
                or any(a.get("task_id") == task_id for a in attempts if isinstance(a, dict))
            ):
                return data
            if automation_id and orig.get("automation_id") == automation_id:
                return data
            if chat_id and orig.get("chat_id") == chat_id:
                return data
        return None

    def _select_next_provider(
        self,
        *,
        role: str,
        lane: str,
        attempted: list[str],
        preferred_provider: str | None,
        at_ts: float | None = None,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
        providers, policy = resolve_policy(role, lane)
        if not providers:
            raise ValueError(f"No models satisfy role={role!r} lane={lane!r}")
        attempted_set = set(attempted)
        cooldowns: dict[str, Any] = {}
        next_provider = None
        for provider in providers:
            key = provider["name"]
            remaining = run.cooldown_remaining(self.cooldown_path, key, at_ts=at_ts)
            if remaining:
                cooldowns[key] = {
                    "remaining_seconds": remaining,
                    "reset_at": run.cooldown_reset_at(self.cooldown_path, key, at_ts=at_ts),
                }
            if next_provider is None and key not in attempted_set and remaining == 0:
                next_provider = provider
        if preferred_provider is None:
            preferred_provider = providers[0]["name"]
        return next_provider, providers, policy, {
            "preferred_provider": preferred_provider,
            "cooldowns": cooldowns,
        }

    def _verify_and_classify(
        self,
        *,
        kind: str,
        role: str,
        lane: str,
        bot: str | None,
        chat_id: str | None,
        automation_id: str | None,
        run_id: str | None,
        dispatch_id: str | None,
        caller_failed_provider: str | None,
        caller_prior_attempted: list[str],
    ) -> dict[str, Any]:
        if kind == "worker":
            if not dispatch_id:
                return {"settled": False, "reason": "missing_dispatch_id", "evidence": {}}
            show = self.adapter.worker_show(dispatch_id)
            audit = self._find_local_audit(dispatch_id=dispatch_id, task_id=show.get("taskId"))
            if run.is_human_input_wait(show):
                return {
                    "settled": True,
                    "classification": run.classify_worker_failure(show),
                    "evidence": {"worker_show": show},
                    "raw_record": show,
                    "canonical_key": f"canonical:worker:{dispatch_id}",
                }
            if show.get("state") != "failed" or not run.worker_is_settled(show):
                return {
                    "settled": False,
                    "reason": "active_or_uncertain",
                    "evidence": {"worker_show": show},
                    "raw_record": show,
                    "canonical_key": f"canonical:worker:{dispatch_id}",
                }
            read_result = self.adapter.worker_read(dispatch_id)
            text = run.latest_turn_failure_text(read_result)
            classification = run.classify_worker_failure(show, text)
            verified_role = show.get("role") or (audit or {}).get("role")
            verified_lane = show.get("lane") or (audit or {}).get("lane")
            verified_attempted: list[str] = []
            if audit and isinstance(audit.get("attempts"), list):
                for att in audit["attempts"]:
                    if isinstance(att, dict) and att.get("provider") and att.get("status") not in (
                        "already_attempted_skipped",
                        "cooldown_skipped",
                    ):
                        verified_attempted.append(att["provider"])
            verified_failed = (
                show.get("modelKey")
                or _provider_key(show.get("agent"), show.get("model"))
                or (verified_attempted[-1] if verified_attempted else None)
            )
            return {
                "settled": True,
                "classification": classification,
                "evidence": {"worker_show": show, "excerpt": run.redact_text(text)[-1500:]},
                "raw_record": show,
                "worktree": show.get("worktree") or (audit or {}).get("worktree"),
                "task_id": show.get("taskId"),
                "canonical_key": f"canonical:worker:{dispatch_id}",
                "verified_role": verified_role,
                "verified_lane": verified_lane,
                "verified_failed_provider": verified_failed,
                "verified_attempted": verified_attempted,
            }

        if kind == "bot_chat":
            if not bot or not chat_id:
                return {"settled": False, "reason": "missing_bot_or_chat_id", "evidence": {}}
            bot_assets = self.adapter.read_bot_assets(bot)
            canonical_bot = bot_assets.get("resolved_bot") or bot
            chats_payload = self.adapter.bot_chats(canonical_bot)
            chat = next(
                (c for c in chats_payload.get("chats", []) if str(c.get("id")) == str(chat_id)),
                None,
            )
            if chat is None:
                return {
                    "settled": False,
                    "reason": "active_or_uncertain",
                    "evidence": {"reason": "chat_not_found", "bot_assets": bot_assets},
                    "canonical_key": f"canonical:bot_chat:{canonical_bot}:{chat_id}:unknown",
                }
            try:
                agent_show = self.adapter.agent_show(chat_id)
                agent_read = self.adapter.agent_read(chat_id, limit=20)
            except Exception as error:
                return {
                    "settled": False,
                    "reason": "active_or_uncertain",
                    "evidence": {
                        "chat": chat,
                        "bot_assets": bot_assets,
                        "agent_probe_error": run.redact_text(str(error))[:300],
                    },
                    "raw_record": chat,
                    "resolved_bot": canonical_bot,
                    "canonical_key": f"canonical:bot_chat:{canonical_bot}:{chat_id}:unknown",
                }

            observed_run_id = (
                agent_read.get("runId")
                or agent_read.get("turnId")
                or agent_show.get("runId")
                or chat.get("lastRunId")
            )
            turn_identity = observed_run_id or chat.get("lastActivityAt") or "latest"
            canonical_key = f"canonical:bot_chat:{canonical_bot}:{chat_id}:{turn_identity}"

            if run_id and (not observed_run_id or str(observed_run_id) != str(run_id)):
                return {
                    "settled": False,
                    "reason": "chat_run_id_mismatch" if observed_run_id else "chat_run_id_unverified",
                    "evidence": {"chat": chat, "agent_show": agent_show, "observed_run_id": observed_run_id},
                    "raw_record": chat,
                    "resolved_bot": canonical_bot,
                    "canonical_key": canonical_key,
                }

            latest_text = run.latest_turn_failure_text(agent_read)
            if run.is_human_input_wait(agent_show) or run.is_human_input_wait(agent_read, latest_text):
                merged_wait = {
                    **agent_show,
                    **({"agentWait": True} if run.is_human_input_wait(agent_read, latest_text) else {}),
                }
                return {
                    "settled": True,
                    "classification": run.classify_worker_failure(merged_wait, latest_text),
                    "evidence": {"chat": chat, "agent_show": agent_show, "bot_assets": bot_assets},
                    "raw_record": chat,
                    "resolved_bot": canonical_bot,
                    "canonical_key": canonical_key,
                }

            show_states = _extract_agent_show_states(agent_show)
            has_authoritative_show = bool(show_states) or isinstance(agent_show.get("places"), list)
            read_state = str(agent_read.get("state") or agent_read.get("turnState") or "").strip().lower()
            entries = _extract_bot_messages(agent_read)
            latest_entry = entries[-1] if entries else None
            terminal_error_entry = (
                isinstance(latest_entry, dict)
                and str(latest_entry.get("kind") or latest_entry.get("role") or "").strip().lower() == "error"
            )
            read_settled = (read_state == "done") or (read_state == "" and terminal_error_entry)
            if not has_authoritative_show or any(state != "done" for state in show_states) or not read_settled:
                return {
                    "settled": False,
                    "reason": "active_or_uncertain",
                    "evidence": {
                        "chat": chat,
                        "agent_show": agent_show,
                        "agent_read_state": read_state or ("error_entry" if terminal_error_entry else "missing"),
                        "bot_assets": bot_assets,
                    },
                    "raw_record": chat,
                    "resolved_bot": canonical_bot,
                    "canonical_key": canonical_key,
                }

            effective_read_state = read_state or ("error_entry" if terminal_error_entry else "done")

            audit = self._find_local_audit(chat_id=chat_id)
            verified_failed = (
                agent_show.get("modelKey")
                or _provider_key(agent_show.get("agent"), agent_show.get("model"))
                or agent_read.get("modelKey")
                or _provider_key(agent_read.get("agent"), agent_read.get("model"))
            )
            return {
                "settled": True,
                "classification": run.classify_worker_failure(agent_show, latest_text),
                "evidence": {
                    "chat": chat,
                    "agent_show": agent_show,
                    "agent_read_state": effective_read_state,
                    "bot_assets": bot_assets,
                    "excerpt": run.redact_text(latest_text)[-1500:],
                },
                "raw_record": chat,
                "resolved_bot": canonical_bot,
                "worktree": chat.get("worktree") or agent_show.get("worktree") or (audit or {}).get("worktree"),
                "item_id": chat.get("itemId") or chat.get("item"),
                "task_request": chat.get("lastAsked") or _extract_latest_bot_user_ask(agent_read),
                "resolved_run_id": observed_run_id,
                "canonical_key": canonical_key,
                "verified_role": agent_show.get("role") or (audit or {}).get("role"),
                "verified_lane": agent_show.get("lane") or (audit or {}).get("lane"),
                "verified_failed_provider": verified_failed,
            }

        if kind == "automation":
            if not automation_id:
                return {"settled": False, "reason": "missing_automation_id", "evidence": {}}
            auto = self.adapter.automations_show(automation_id)
            if not isinstance(auto.get("enabled"), bool):
                return {
                    "settled": False,
                    "reason": "authoritative_enabled_state_missing",
                    "evidence": {"automation": auto},
                    "raw_record": auto,
                    "canonical_key": f"canonical:automation:{automation_id}:unknown",
                }
            runs_payload = self.adapter.automations_runs(automation_id)
            runs = runs_payload.get("runs", [])
            active_runs = [
                r for r in runs
                if str(r.get("status") or r.get("state") or "").lower() in ACTIVE_RUN_STATES
                or bool(r.get("active") or r.get("running"))
            ]
            if active_runs:
                return {
                    "settled": False,
                    "reason": "active_or_uncertain",
                    "evidence": {"automation": auto, "active_runs": active_runs},
                    "raw_record": auto,
                    "canonical_key": f"canonical:automation:{automation_id}:{run_id or 'active'}",
                }
            target_run = (
                next((r for r in runs if str(r.get("id")) == str(run_id)), None)
                if run_id
                else (runs[0] if runs else None)
            )
            if target_run is None or not target_run.get("id"):
                return {
                    "settled": False,
                    "reason": "active_or_uncertain",
                    "evidence": {"automation": auto, "reason": "target_run_missing"},
                    "raw_record": auto,
                    "canonical_key": f"canonical:automation:{automation_id}:{run_id or 'missing'}",
                }
            resolved_run_id = str(target_run["id"])
            canonical_key = f"canonical:automation:{automation_id}:{resolved_run_id}"
            run_text = _extract_run_text(target_run)
            if run.is_human_input_wait(target_run, run_text):
                return {
                    "settled": True,
                    "classification": run.classify_worker_failure(target_run, run_text),
                    "evidence": {"automation": auto, "run": target_run},
                    "raw_record": auto,
                    "target_run": target_run,
                    "resolved_run_id": resolved_run_id,
                    "canonical_key": canonical_key,
                }
            run_status = str(target_run.get("status") or target_run.get("state") or "").lower()
            completed_failed = run_status == "completed" and (
                bool(target_run.get("error")) or _run_output_has_provider_failure(target_run)
            )
            if run_status not in SETTLED_FAILED_RUN_STATES and not completed_failed:
                return {
                    "settled": False,
                    "reason": "active_or_uncertain",
                    "evidence": {"automation": auto, "run": target_run},
                    "raw_record": auto,
                    "target_run": target_run,
                    "resolved_run_id": resolved_run_id,
                    "canonical_key": canonical_key,
                }
            audit = self._find_local_audit(automation_id=automation_id)
            verified_failed = (
                target_run.get("modelKey")
                or _provider_key(target_run.get("agent"), target_run.get("model"))
                or _automation_provider_key(auto)
            )
            return {
                "settled": True,
                "classification": run.classify_worker_failure(target_run, run_text),
                "evidence": {
                    "automation": auto,
                    "run": target_run,
                    "excerpt": run.redact_text(run_text)[-1500:],
                },
                "raw_record": auto,
                "target_run": target_run,
                "resolved_run_id": resolved_run_id,
                "worktree": _automation_worktree(auto) or (audit or {}).get("worktree"),
                "item_id": auto.get("itemId") or auto.get("item"),
                "canonical_key": canonical_key,
                "verified_role": auto.get("role") or (audit or {}).get("role"),
                "verified_lane": auto.get("lane") or (audit or {}).get("lane"),
                "verified_failed_provider": verified_failed,
            }

        raise ValueError(f"Unsupported recovery target kind: {kind}")

    def _build_handoff_prompt(
        self,
        *,
        kind: str,
        original_ids: dict[str, Any],
        worktree: str | None,
        excerpt: str,
        task_request: str | None,
    ) -> str | None:
        if not task_request or not task_request.strip():
            return None
        context_lines = [
            f"Original target: {kind}",
            f"Original IDs: {json.dumps(original_ids, ensure_ascii=False)}",
        ]
        if worktree:
            context_lines.append(f"Worktree: {worktree}")
        if excerpt:
            context_lines.append(f"Prior failure excerpt (redacted): {run.redact_text(excerpt)[:600]}")
        return task_request.strip() + "\n\n" + "\n".join(context_lines)

    def _build_delayed_resume_plan(
        self,
        *,
        kind: str,
        role: str,
        lane: str,
        providers: list[dict[str, Any]],
        cooldowns: dict[str, Any],
        preferred_provider: str,
        original_ids: dict[str, Any],
        original_enabled: bool | None,
        worktree: str | None,
        spec_file: Path | None,
        handoff_prompt: str,
        results_to: str | None,
    ) -> dict[str, Any]:
        settings = self._settings()
        earliest_ts = time.time() + int(settings["delayed_resume_delay_seconds"])
        reset_timestamps = [
            _parse_iso(entry["reset_at"])
            for entry in cooldowns.values()
            if isinstance(entry.get("reset_at"), str)
        ]
        if reset_timestamps:
            earliest_ts = min(reset_timestamps)
        return {
            "status": "pending_probe",
            "requires_health_probe": True,
            "probe_passed": False,
            "eligible_at": _iso_from_ts(earliest_ts),
            "role": role,
            "lane": lane,
            "preferred_provider": preferred_provider,
            "candidate_models": [p["name"] for p in providers],
            "probe_timeout_seconds": int(settings["health_probe_timeout_seconds"]),
            "target_kind": kind,
            "original_ids": original_ids,
            "original_enabled": original_enabled,
            "worktree": worktree,
            "spec_file": str(spec_file.resolve()) if spec_file is not None else None,
            "handoff_prompt": run.redact_text(handoff_prompt),
            "results_to": results_to,
        }

    def _launch_disabled_bot_recovery_run(
        self,
        *,
        record: dict[str, Any],
        name: str,
        bot: str,
        provider: dict[str, Any],
        prompt: str,
        by_iso: str,
        item_id: str | None,
        results_to: str | None,
    ) -> dict[str, Any]:
        record["actual_provider"] = provider["name"]
        record["launch_intent"] = {
            "action": "create_disabled_one_time_bot_automation",
            "provider": provider["name"],
            "automation_name": name,
            "step": "pre_create",
            "recorded_at": run.now(),
        }
        self._persist(record, claim_idempotency=True)

        try:
            created = self.adapter.create_one_time_bot_automation(
                name=name,
                bot=bot,
                provider=provider,
                prompt=prompt,
                by_iso=by_iso,
                item_id=item_id,
                results_to=results_to,
                enabled=False,
            )
            new_auto_id = str(created.get("id") or (created.get("automation") or {}).get("id") or "")
            if not new_auto_id:
                raise RuntimeError("automations.create did not return an automation id")
            record["recovery_automation_id"] = new_auto_id
            record["launch_intent"]["step"] = "created_ready_to_run"
            record["mutations"].append(
                {
                    "at": run.now(),
                    "action": "create_disabled_one_time_bot_automation",
                    "target": new_auto_id,
                    "after": {
                        "bot": bot,
                        "runs_in": "new",
                        "once": True,
                        "enabled": False,
                        "agent": provider["agent"],
                        "model": provider["model"],
                    },
                }
            )
            record["external_action_receipts"].append(
                {"action": "automations.create", "at": run.now(), "receipt": _allowlisted_receipt(created)}
            )
            self._persist(record, claim_idempotency=True)

            run_receipt = self.adapter.automations_run(new_auto_id)
        except Exception as error:
            record["launch_intent"]["step"] = "launch_uncertain"
            record["launch_intent"]["error"] = run.redact_text(str(error))[:200]
            record["block_reason"] = "ambiguous_launch_reconcile_required"
            self._record_event(
                record,
                "launch_uncertain_blocked",
                "blocked",
                {"error": record["launch_intent"]["error"], "step": record["launch_intent"]["step"]},
            )
            return self._persist(record, claim_idempotency=True)

        recovery_run_id = self._extract_accepted_run_id(run_receipt, new_auto_id)
        if recovery_run_id:
            record["recovery_run_id"] = recovery_run_id
        record["launch_intent"]["step"] = "run_accepted"
        record["external_action_receipts"].append(
            {"action": "automations.run", "at": run.now(), "receipt": _allowlisted_receipt(run_receipt)}
        )
        if provider["name"] not in record["attempted_providers"]:
            record["attempted_providers"].append(provider["name"])
        self._record_event(
            record,
            "recovery_run_accepted",
            "waiting_for_run",
            {
                "recovery_automation_id": new_auto_id,
                "recovery_run_id": record.get("recovery_run_id"),
                "actual_provider": provider["name"],
            },
        )
        return self._persist(record, claim_idempotency=True)

    @staticmethod
    def _extract_accepted_run_id(run_receipt: dict[str, Any], automation_id: str) -> str | None:
        if not isinstance(run_receipt, dict):
            return None
        if isinstance(run_receipt.get("runId"), str) and run_receipt["runId"]:
            return run_receipt["runId"]
        run_obj = run_receipt.get("run")
        if isinstance(run_obj, dict) and isinstance(run_obj.get("id"), str) and run_obj["id"]:
            return run_obj["id"]
        generic_id = run_receipt.get("id")
        if (
            isinstance(generic_id, str)
            and generic_id
            and generic_id != automation_id
            and run_receipt.get("automationId") == automation_id
        ):
            return generic_id
        return None

    def _pause_and_verify_automation(self, record: dict[str, Any], automation_id: str) -> bool:
        disable_receipt = self.adapter.automations_edit_enabled(automation_id, False)
        record["mutations"].append(
            {
                "at": run.now(),
                "action": "disable_original_schedule",
                "target": automation_id,
                "before": {"enabled": True},
                "after": {"enabled": False},
            }
        )
        record["external_action_receipts"].append(
            {"action": "automations.edit", "at": run.now(), "receipt": _allowlisted_receipt(disable_receipt)}
        )
        post_auto = self.adapter.automations_show(automation_id)
        post_runs = self.adapter.automations_runs(automation_id).get("runs", [])
        active_post = [
            r for r in post_runs
            if str(r.get("status") or r.get("state") or "").lower() in ACTIVE_RUN_STATES
            or bool(r.get("active") or r.get("running"))
        ]
        if post_auto.get("enabled") is not False or active_post:
            record["pause_owned"] = False
            record["block_reason"] = "active_run_started_during_pause"
            self._record_event(
                record,
                "pause_race_detected_blocked",
                "blocked",
                {"enabled": post_auto.get("enabled"), "active_runs": len(active_post)},
            )
            self._persist(record, claim_idempotency=True)
            return False

        record["pause_owned"] = True
        record["paused_snapshot"] = {
            "enabled": False,
            "updatedAt": post_auto.get("updatedAt"),
            "trigger": post_auto.get("trigger"),
            "rrule": post_auto.get("rrule"),
        }
        self._record_event(record, "original_schedule_disabled", "paused_original")
        self._persist(record, claim_idempotency=True)
        return True

    def _auto_resume_if_owned(self, record: dict[str, Any], *, execute: bool) -> bool:
        settings = self._settings()
        orig_auto_id = (record.get("original_ids") or {}).get("automation_id")
        if not (
            execute
            and record.get("target_kind") == "automation"
            and orig_auto_id
            and settings["auto_resume_automation"]
            and record.get("original_enabled") is True
            and record.get("pause_owned") is True
        ):
            return False
        current = self.adapter.automations_show(orig_auto_id)
        snap = record.get("paused_snapshot") or {}
        operator_changed = (
            current.get("enabled") is not False
            or (snap.get("updatedAt") is not None and current.get("updatedAt") != snap.get("updatedAt"))
            or (snap.get("trigger") is not None and current.get("trigger") != snap.get("trigger"))
            or (snap.get("rrule") is not None and current.get("rrule") != snap.get("rrule"))
        )
        if operator_changed:
            record["pause_owned"] = False
            self._record_event(
                record,
                "auto_resume_skipped_operator_modified",
                record["status"],
                {"current_enabled": current.get("enabled")},
            )
            return False

        enable_receipt = self.adapter.automations_edit_enabled(orig_auto_id, True)
        record["pause_owned"] = False
        record["mutations"].append(
            {
                "at": run.now(),
                "action": "auto_resume_original_schedule",
                "target": orig_auto_id,
                "before": {"enabled": False},
                "after": {"enabled": True},
            }
        )
        record["external_action_receipts"].append(
            {"action": "automations.edit", "at": run.now(), "receipt": _allowlisted_receipt(enable_receipt)}
        )
        return True

    def _run_worker_recovery(
        self,
        *,
        record: dict[str, Any],
        title: str,
        worktree: str | None,
        spec_file: Path | None,
        prompt: str,
        role: str,
        lane: str,
        attempted: list[str],
        only_provider: str | None = None,
    ) -> dict[str, Any]:
        if not worktree:
            raise ValueError("worktree is required for worker/workspace recovery")
        worktree_path = Path(worktree).resolve()
        if spec_file is not None:
            resolved_spec = Path(spec_file).resolve()
        else:
            resolved_spec = self.state_dir / "specs" / f"recovery-{record['recovery_id']}.md"
            resolved_spec.parent.mkdir(parents=True, exist_ok=True)
            resolved_spec.write_text(prompt, encoding="utf-8")

        record["launch_intent"] = {
            "action": "run.run_job",
            "only_provider": only_provider,
            "step": "starting_worker",
            "recorded_at": run.now(),
        }
        self._persist(record, claim_idempotency=True)

        try:
            job_result = run.run_job(
                SimpleNamespace(
                    worktree=worktree_path,
                    spec_file=resolved_spec,
                    title=title,
                    role=role,
                    lane=lane,
                ),
                orca=self.adapter.orca,
                skip_providers=set(attempted),
                only_provider=only_provider,
                dispatch_meta={"recovery_id": record["recovery_id"]},
                state_dir=self.state_dir,
            )
        except Exception as error:
            record["launch_intent"]["step"] = "worker_uncertain"
            record["launch_intent"]["error"] = run.redact_text(str(error))[:200]
            record["block_reason"] = "ambiguous_launch_reconcile_required"
            self._record_event(record, "worker_launch_uncertain", "blocked", record["launch_intent"]["error"])
            self._persist(record, claim_idempotency=True)
            return {"status": "blocked", "attempts": [], "error": record["launch_intent"]["error"]}

        attempts = job_result.get("attempts", [])
        executed = [a for a in attempts if "dispatch_id" in a]
        for attempt in executed:
            provider_name = attempt.get("provider")
            if provider_name and provider_name not in record["attempted_providers"]:
                record["attempted_providers"].append(provider_name)
        last = executed[-1] if executed else {}
        record["actual_provider"] = job_result.get("selected_provider") or last.get("provider")
        record["launch_intent"]["step"] = "worker_finished"
        record["external_action_receipts"].append(
            {
                "action": "run.run_job",
                "at": run.now(),
                "receipt": _allowlisted_receipt(
                    {
                        "job_id": job_result.get("job_id"),
                        "state_file": job_result.get("state_file"),
                        "status": job_result.get("status"),
                        "task_id": last.get("task_id"),
                        "dispatch_id": last.get("dispatch_id"),
                    }
                ),
            }
        )
        return job_result

    def _enter_exhausted_plan(
        self,
        *,
        record: dict[str, Any],
        kind: str,
        role: str,
        lane: str,
        providers: list[dict[str, Any]],
        policy: dict[str, Any],
        preferred_provider: str,
        original_ids: dict[str, Any],
        original_enabled: bool | None,
        worktree: str | None,
        spec_file: Path | None,
        handoff_prompt: str,
        results_to: str | None,
        execute: bool,
    ) -> dict[str, Any]:
        cooldowns = {
            p["name"]: {
                "remaining_seconds": run.cooldown_remaining(self.cooldown_path, p["name"]),
                "reset_at": run.cooldown_reset_at(self.cooldown_path, p["name"]),
            }
            for p in providers
            if run.cooldown_remaining(self.cooldown_path, p["name"])
        }
        cooldowns.update(record.get("cooldown", {}))
        record["cooldown"] = cooldowns
        plan = self._build_delayed_resume_plan(
            kind=kind,
            role=role,
            lane=policy["lane"],
            providers=providers,
            cooldowns=cooldowns,
            preferred_provider=preferred_provider,
            original_ids=original_ids,
            original_enabled=original_enabled,
            worktree=worktree,
            spec_file=spec_file,
            handoff_prompt=handoff_prompt,
            results_to=results_to,
        )
        record["delayed_resume_plan"] = plan
        record["escalation_required"] = not policy["allow_lane_escalation"]
        record["planned_actions"].append(
            {"action": "delayed_health_probe_before_resume", "eligible_at": plan["eligible_at"]}
        )
        status = "exhausted_delayed_resume" if execute else "dry_run_exhausted_delayed_resume"
        self._record_event(
            record,
            "lane_exhausted_delayed_resume_planned",
            status,
            {"eligible_at": plan["eligible_at"]},
        )
        return self._persist(record, claim_idempotency=execute)

    def recover(
        self,
        *,
        kind: str,
        role: str,
        lane: str,
        idempotency_key: str | None = None,
        bot: str | None = None,
        chat_id: str | None = None,
        automation_id: str | None = None,
        run_id: str | None = None,
        dispatch_id: str | None = None,
        task_id: str | None = None,
        item_id: str | None = None,
        worktree: Path | str | None = None,
        spec_file: Path | None = None,
        prompt: str | None = None,
        preferred_provider: str | None = None,
        failed_provider: str | None = None,
        prior_attempted: list[str] | None = None,
        execute: bool = False,
    ) -> dict[str, Any]:
        if not role or not lane:
            raise ValueError("Both role and lane must be specified explicitly")
        target_lock_id = self._pre_lock_key(
            kind=kind, bot=bot, chat_id=chat_id, automation_id=automation_id, dispatch_id=dispatch_id
        )
        if execute:
            with self._lock_key(target_lock_id):
                return self._recover_impl(
                    kind=kind,
                    role=role,
                    lane=lane,
                    caller_key=idempotency_key,
                    bot=bot,
                    chat_id=chat_id,
                    automation_id=automation_id,
                    run_id=run_id,
                    dispatch_id=dispatch_id,
                    task_id=task_id,
                    item_id=item_id,
                    worktree=worktree,
                    spec_file=spec_file,
                    prompt=prompt,
                    preferred_provider=preferred_provider,
                    failed_provider=failed_provider,
                    prior_attempted=prior_attempted,
                    execute=True,
                )
        return self._recover_impl(
            kind=kind,
            role=role,
            lane=lane,
            caller_key=idempotency_key,
            bot=bot,
            chat_id=chat_id,
            automation_id=automation_id,
            run_id=run_id,
            dispatch_id=dispatch_id,
            task_id=task_id,
            item_id=item_id,
            worktree=worktree,
            spec_file=spec_file,
            prompt=prompt,
            preferred_provider=preferred_provider,
            failed_provider=failed_provider,
            prior_attempted=prior_attempted,
            execute=False,
        )

    def _pre_lock_key(
        self,
        *,
        kind: str,
        bot: str | None,
        chat_id: str | None,
        automation_id: str | None,
        dispatch_id: str | None,
    ) -> str:
        if kind == "bot_chat":
            resolved = self.adapter.read_bot_assets(bot).get("resolved_bot") if bot else "unknown"
            return f"target:bot_chat:{resolved}:{chat_id or 'unknown'}"
        if kind == "automation":
            return f"target:automation:{automation_id or 'unknown'}"
        return f"target:worker:{dispatch_id or 'unknown'}"

    def _recover_impl(
        self,
        *,
        kind: str,
        role: str,
        lane: str,
        caller_key: str | None,
        bot: str | None,
        chat_id: str | None,
        automation_id: str | None,
        run_id: str | None,
        dispatch_id: str | None,
        task_id: str | None,
        item_id: str | None,
        worktree: Path | str | None,
        spec_file: Path | None,
        prompt: str | None,
        preferred_provider: str | None,
        failed_provider: str | None,
        prior_attempted: list[str] | None,
        execute: bool,
    ) -> dict[str, Any]:
        if caller_key:
            existing_alias = self._load_by_key(caller_key) or self._load_alias(caller_key)
            if existing_alias is not None and _has_side_effect_or_claim(existing_alias):
                dup = dict(existing_alias)
                dup["deduplicated"] = True
                return run.redact_secrets(dup)

        inspection = self._verify_and_classify(
            kind=kind,
            role=role,
            lane=lane,
            bot=bot,
            chat_id=chat_id,
            automation_id=automation_id,
            run_id=run_id,
            dispatch_id=dispatch_id,
            caller_failed_provider=failed_provider,
            caller_prior_attempted=list(prior_attempted or ()),
        )
        canonical_key = inspection["canonical_key"]
        existing = self._load_by_key(canonical_key)
        if existing is not None and _has_side_effect_or_claim(existing):
            self._save_alias(caller_key, canonical_key, existing["recovery_id"])
            dup = dict(existing)
            dup["deduplicated"] = True
            return run.redact_secrets(dup)

        recovery_id = uuid4().hex[:12]
        settings = self._settings()
        raw_record = inspection.get("raw_record") or {}
        resolved_bot = inspection.get("resolved_bot") or bot or _automation_bot_id(raw_record)
        resolved_results_to = _automation_results_to(raw_record)
        if resolved_bot and kind == "automation":
            resolved_bot = self.adapter.read_bot_assets(resolved_bot).get("resolved_bot") or resolved_bot
        resolved_worktree = str(worktree) if worktree else inspection.get("worktree")
        resolved_item = item_id if item_id is not None else inspection.get("item_id")
        resolved_task = task_id or inspection.get("task_id")
        resolved_run_id = run_id or inspection.get("resolved_run_id")
        original_ids = {
            "bot": resolved_bot,
            "chat_id": chat_id,
            "automation_id": automation_id,
            "run_id": resolved_run_id,
            "dispatch_id": dispatch_id,
            "task_id": resolved_task,
            "item_id": resolved_item,
        }
        original_enabled = (
            raw_record.get("enabled") if kind == "automation" and isinstance(raw_record.get("enabled"), bool) else None
        )
        verified_attempted = list(inspection.get("verified_attempted") or ())
        verified_failed = inspection.get("verified_failed_provider")

        attempted = list(
            dict.fromkeys(
                [
                    *verified_attempted,
                    *(prior_attempted or ()),
                    *(([verified_failed]) if verified_failed else ()),
                    *(([failed_provider]) if failed_provider else ()),
                ]
            )
        )
        record: dict[str, Any] = {
            "recovery_id": recovery_id,
            "idempotency_key": canonical_key,
            "caller_idempotency_key": caller_key,
            "deduplicated": False,
            "mode": "execute" if execute else "dry_run",
            "target_kind": kind,
            "status": "inspecting",
            "role": role,
            "lane": lane,
            "preferred_provider": preferred_provider,
            "actual_provider": None,
            "attempted_providers": attempted,
            "in_place_provider_switch": False,
            "mid_task_switch_allowed": False,
            "original_enabled": original_enabled,
            "pause_owned": False,
            "original_ids": original_ids,
            "worktree": resolved_worktree,
            "spec_file": str(spec_file.resolve()) if spec_file is not None else None,
            "results_to": resolved_results_to,
            "verification_evidence": inspection.get("evidence", {}),
            "mutations": [],
            "external_action_receipts": [],
            "cooldown": {},
            "delayed_resume_plan": None,
            "planned_actions": [],
            "history": [],
        }

        if not inspection["settled"]:
            record["block_reason"] = inspection["reason"]
            self._record_event(record, "settlement_check_blocked", "blocked", {"reason": inspection["reason"]})
            return self._persist(record, claim_idempotency=False)

        # Enforce verified role/lane/failed_provider corroboration (Finding 4)
        v_role = inspection.get("verified_role")
        v_lane = inspection.get("verified_lane")
        if (v_role and v_role != role) or (v_lane and v_lane != lane):
            record["block_reason"] = "conflicting_role_or_lane"
            self._record_event(
                record,
                "role_or_lane_conflict_blocked",
                "blocked",
                {"verified_role": v_role, "verified_lane": v_lane, "caller_role": role, "caller_lane": lane},
            )
            return self._persist(record, claim_idempotency=False)

        if verified_failed and failed_provider and verified_failed != failed_provider:
            record["block_reason"] = "conflicting_failed_provider"
            self._record_event(
                record,
                "failed_provider_conflict_blocked",
                "blocked",
                {"verified": verified_failed, "caller": failed_provider},
            )
            return self._persist(record, claim_idempotency=False)

        classification = inspection["classification"]
        record["verification_evidence"]["classification"] = classification
        if not classification["eligible_for_fallback"]:
            self._record_event(record, "non_provider_failure_rejected", "rejected_non_provider", classification)
            return self._persist(record, claim_idempotency=False)

        effective_failed_provider = verified_failed or failed_provider
        if not effective_failed_provider:
            record["block_reason"] = "failed_provider_unverified"
            self._record_event(
                record,
                "failed_provider_unverified_blocked",
                "blocked",
                {"reason": "Could not verify which provider failed from Orca evidence or caller input."},
            )
            return self._persist(record, claim_idempotency=False)

        task_request = (
            prompt
            or raw_record.get("prompt")
            or inspection.get("task_request")
            or (spec_file.read_text(encoding="utf-8") if spec_file and spec_file.is_file() else None)
            or (f"Resume linked Orca item #{resolved_item}." if resolved_item is not None else None)
        )
        handoff_prompt = self._build_handoff_prompt(
            kind=kind,
            original_ids=original_ids,
            worktree=resolved_worktree,
            excerpt=inspection.get("evidence", {}).get("excerpt", ""),
            task_request=task_request,
        )
        if handoff_prompt is None:
            record["block_reason"] = "missing_recoverable_task_request"
            self._record_event(record, "task_request_missing_blocked", "blocked")
            return self._persist(record, claim_idempotency=False)

        cooldown_seconds = int(SETTINGS.get("provider_cooldown_seconds", 900))
        if execute:
            run.update_cooldown(self.cooldown_path, effective_failed_provider, cooldown_seconds)

        next_provider, providers, policy, meta = self._select_next_provider(
            role=role,
            lane=lane,
            attempted=attempted,
            preferred_provider=preferred_provider,
        )
        record["preferred_provider"] = meta["preferred_provider"]
        record["cooldown"] = dict(meta["cooldowns"])
        if effective_failed_provider not in record["cooldown"] and not execute:
            record["cooldown"][effective_failed_provider] = {
                "remaining_seconds": cooldown_seconds,
                "reset_at": _iso_from_ts(time.time() + cooldown_seconds),
                "planned": True,
            }

        if next_provider is None:
            if kind == "automation" and original_enabled is True:
                record["planned_actions"].append(
                    {"action": "automations.edit", "automation_id": automation_id, "enabled": False}
                )
                if execute:
                    self._record_event(record, "disabling_exhausted_automation", "in_progress")
                    self._persist(record, claim_idempotency=True)
                    if not self._pause_and_verify_automation(record, automation_id):
                        return record
            return self._enter_exhausted_plan(
                record=record,
                kind=kind,
                role=role,
                lane=lane,
                providers=providers,
                policy=policy,
                preferred_provider=meta["preferred_provider"],
                original_ids=original_ids,
                original_enabled=original_enabled,
                worktree=resolved_worktree,
                spec_file=spec_file,
                handoff_prompt=handoff_prompt,
                results_to=resolved_results_to,
                execute=execute,
            )

        record["actual_provider"] = next_provider["name"]
        by_iso = _iso_from_ts(time.time() + int(settings["once_deadline_seconds"]))
        if kind == "bot_chat" or (kind == "automation" and resolved_bot):
            if kind == "automation" and original_enabled is True:
                record["planned_actions"].append(
                    {"action": "automations.edit", "automation_id": automation_id, "enabled": False}
                )
            record["planned_actions"].extend(
                [
                    {
                        "action": "automations.create_disabled_one_time_bot_chat",
                        "bot": resolved_bot,
                        "runs_in": "new",
                        "once": True,
                        "enabled": False,
                        "by": by_iso,
                        "agent": next_provider["agent"],
                        "model": next_provider["model"],
                        "effort": next_provider.get("effort"),
                        "item_id": resolved_item,
                    },
                    {"action": "automations.run"},
                ]
            )
            if kind == "automation" and settings["auto_resume_automation"] and original_enabled is True:
                record["planned_actions"].append(
                    {"action": "automations.edit_after_verified_success", "automation_id": automation_id, "enabled": True}
                )
        else:
            if kind == "automation" and original_enabled is True:
                record["planned_actions"].append(
                    {"action": "automations.edit", "automation_id": automation_id, "enabled": False}
                )
            record["planned_actions"].append(
                {
                    "action": "run.run_job",
                    "worktree": resolved_worktree,
                    "role": role,
                    "lane": policy["lane"],
                    "provider": next_provider["name"],
                    "skip_providers": attempted,
                }
            )

        if not execute:
            self._record_event(
                record,
                "dry_run_planned",
                "dry_run_planned",
                {"next_provider": next_provider["name"], "planned_actions": record["planned_actions"]},
            )
            return self._persist(record, claim_idempotency=False)

        self._record_event(record, "execution_started", "in_progress")
        self._persist(record, claim_idempotency=True)

        if kind == "automation" and original_enabled is True:
            if not self._pause_and_verify_automation(record, automation_id):
                return record

        if kind == "bot_chat" or (kind == "automation" and resolved_bot):
            label = f"Recover {resolved_bot} {chat_id} ({recovery_id})" if kind == "bot_chat" else f"Recover automation {automation_id} ({recovery_id})"
            return self._launch_disabled_bot_recovery_run(
                record=record,
                name=label,
                bot=resolved_bot,
                provider=next_provider,
                prompt=handoff_prompt,
                by_iso=by_iso,
                item_id=str(resolved_item) if resolved_item is not None else None,
                results_to=resolved_results_to,
            )

        title = f"Recover automation {automation_id}" if kind == "automation" else f"Recover worker {dispatch_id}"
        job_result = self._run_worker_recovery(
            record=record,
            title=title,
            worktree=resolved_worktree,
            spec_file=spec_file,
            prompt=handoff_prompt,
            role=role,
            lane=policy["lane"],
            attempted=attempted,
        )
        return self._finalize_sync_worker_result(
            record=record,
            job_result=job_result,
            kind=kind,
            role=role,
            providers=providers,
            policy=policy,
            preferred_provider=meta["preferred_provider"],
            original_ids=original_ids,
            original_enabled=original_enabled,
            worktree=resolved_worktree,
            spec_file=spec_file,
            handoff_prompt=handoff_prompt,
            results_to=resolved_results_to,
            execute=True,
        )

    def _finalize_sync_worker_result(
        self,
        *,
        record: dict[str, Any],
        job_result: dict[str, Any],
        kind: str,
        role: str,
        providers: list[dict[str, Any]],
        policy: dict[str, Any],
        preferred_provider: str,
        original_ids: dict[str, Any],
        original_enabled: bool | None,
        worktree: str | None,
        spec_file: Path | None,
        handoff_prompt: str,
        results_to: str | None,
        execute: bool,
    ) -> dict[str, Any]:
        status = job_result.get("status", "failed")
        if status == "succeeded":
            resumed = self._auto_resume_if_owned(record, execute=execute)
            if record.get("delayed_resume_plan"):
                record["delayed_resume_plan"]["status"] = "resumed"
            self._record_event(
                record,
                "recovery_succeeded",
                "recovered",
                {"original_schedule_resumed": resumed},
            )
            return self._persist(record, claim_idempotency=True)
        if status == "lane_exhausted":
            return self._enter_exhausted_plan(
                record=record,
                kind=kind,
                role=role,
                lane=policy["lane"],
                providers=providers,
                policy=policy,
                preferred_provider=preferred_provider,
                original_ids=original_ids,
                original_enabled=original_enabled,
                worktree=worktree,
                spec_file=spec_file,
                handoff_prompt=handoff_prompt,
                results_to=results_to,
                execute=execute,
            )
        if status != "blocked":
            self._record_event(
                record,
                "worker_recovery_not_succeeded",
                status,
                {"job_id": job_result.get("job_id")},
            )
        return self._persist(record, claim_idempotency=True)

    def reconcile(self, recovery_id_or_key: str, *, execute: bool = False) -> dict[str, Any]:
        initial = self.load_record(recovery_id_or_key)
        if initial is None:
            raise ValueError(f"Unknown recovery record: {recovery_id_or_key}")
        key = initial["idempotency_key"]
        if execute:
            with self._lock_key(key):
                return self._reconcile_impl(initial["recovery_id"], key=key, execute=True)
        return self._reconcile_impl(initial["recovery_id"], key=key, execute=False)

    def _reconcile_impl(self, recovery_id: str, *, key: str, execute: bool) -> dict[str, Any]:
        record = self._load_by_key(key) or self.load_record(recovery_id)
        if record is None:
            raise ValueError(f"Unknown recovery record: {recovery_id}")
        if record.get("status") in ("recovered", "recovery_run_failed", "waiting_for_human"):
            dup = dict(record)
            dup["deduplicated"] = True
            return run.redact_secrets(dup)

        recovery_auto_id = record.get("recovery_automation_id")
        launch_intent = record.get("launch_intent") or {}
        if not recovery_auto_id and launch_intent.get("automation_name"):
            autos_payload = self.adapter.automations_list()
            autos = (
                autos_payload
                if isinstance(autos_payload, list)
                else autos_payload.get("automations", [])
            )
            matched = next(
                (a for a in autos if isinstance(a, dict) and a.get("name") == launch_intent["automation_name"]),
                None,
            )
            if matched and matched.get("id"):
                recovery_auto_id = str(matched["id"])
                record["recovery_automation_id"] = recovery_auto_id
        if not recovery_auto_id:
            if record.get("block_reason") == "ambiguous_launch_reconcile_required":
                return run.redact_secrets(record)
            raise ValueError("Recovery record does not have a recovery_automation_id to reconcile")

        runs = self.adapter.automations_runs(recovery_auto_id).get("runs", [])
        target_run_id = record.get("recovery_run_id")
        if target_run_id:
            target_run = next(
                (r for r in runs if str(r.get("id") or r.get("runId")) == str(target_run_id)),
                None,
            )
        elif len(runs) == 1:
            target_run = runs[0]
            if target_run.get("id"):
                record["recovery_run_id"] = str(target_run["id"])
        elif len(runs) > 1:
            record["block_reason"] = "ambiguous_multiple_runs_without_run_id"
            self._record_event(record, "reconcile_ambiguous_runs", "blocked", {"run_count": len(runs)})
            return self._persist(record, claim_idempotency=execute)
        else:
            target_run = None

        if target_run is None:
            if record.get("block_reason") == "ambiguous_launch_reconcile_required":
                return run.redact_secrets(record)
            self._record_event(
                record,
                "reconcile_waiting_for_run",
                "waiting_for_run",
                {"recovery_automation_id": recovery_auto_id, "reason": "run_not_yet_visible"},
            )
            return self._persist(record, claim_idempotency=execute)

        run_text = _extract_run_text(target_run)
        if run.is_human_input_wait(target_run, run_text):
            record.pop("block_reason", None)
            record["verification_evidence"]["recovery_run_classification"] = run.classify_worker_failure(
                target_run, run_text
            )
            record["external_action_receipts"].append(
                {"action": "automations.runs.reconciled", "at": run.now(), "receipt": _allowlisted_receipt(target_run)}
            )
            self._record_event(record, "reconcile_human_input_wait", "waiting_for_human")
            return self._persist(record, claim_idempotency=execute)

        run_status = str(target_run.get("status") or target_run.get("state") or "").lower()
        if run_status in ACTIVE_RUN_STATES or bool(target_run.get("active") or target_run.get("running")):
            record.pop("block_reason", None)
            self._record_event(
                record,
                "reconcile_still_running",
                "waiting_for_run",
                {"recovery_run_id": target_run.get("id"), "run_status": run_status or "unknown"},
            )
            return self._persist(record, claim_idempotency=execute)

        completed_ok = (
            run_status == "completed"
            and _completed_run_has_evidence(target_run)
            and not target_run.get("error")
            and not _run_output_has_provider_failure(target_run)
        )
        if (run_status == "succeeded" and not target_run.get("error")) or completed_ok:
            record.pop("block_reason", None)
            record["external_action_receipts"].append(
                {"action": "automations.runs.reconciled", "at": run.now(), "receipt": _allowlisted_receipt(target_run)}
            )
            if record.get("delayed_resume_plan"):
                record["delayed_resume_plan"]["status"] = "resumed"
            resumed = self._auto_resume_if_owned(record, execute=execute)
            self._record_event(
                record,
                "reconcile_succeeded",
                "recovered",
                {"recovery_run_id": target_run.get("id"), "original_schedule_resumed": resumed},
            )
            return self._persist(record, claim_idempotency=execute)

        completed_err = run_status == "completed" and (
            bool(target_run.get("error")) or _run_output_has_provider_failure(target_run)
        )
        if run_status in TERMINAL_FAILED_RUN_STATES or completed_err:
            record.pop("block_reason", None)
            classification = run.classify_worker_failure(target_run, run_text)
            record["verification_evidence"]["recovery_run_classification"] = classification
            record["external_action_receipts"].append(
                {"action": "automations.runs.reconciled", "at": run.now(), "receipt": _allowlisted_receipt(target_run)}
            )
            actual_provider = record.get("actual_provider")
            if execute and actual_provider and classification.get("eligible_for_fallback"):
                run.update_cooldown(
                    self.cooldown_path,
                    actual_provider,
                    int(SETTINGS.get("provider_cooldown_seconds", 900)),
                )
            self._record_event(
                record,
                "reconcile_failed",
                "recovery_run_failed",
                {"recovery_run_id": target_run.get("id"), "run_status": run_status, "classification": classification},
            )
            return self._persist(record, claim_idempotency=execute)

        self._record_event(
            record,
            "reconcile_unresolved_status",
            "waiting_for_run",
            {"recovery_run_id": target_run.get("id"), "run_status": run_status or "unknown"},
        )
        return self._persist(record, claim_idempotency=execute)

    def resume_after_probe(
        self,
        recovery_id_or_key: str,
        *,
        execute: bool = False,
        now_ts: float | None = None,
    ) -> dict[str, Any]:
        initial = self.load_record(recovery_id_or_key)
        if initial is None:
            raise ValueError(f"Unknown recovery record: {recovery_id_or_key}")
        key = initial["idempotency_key"]
        if execute:
            with self._lock_key(key):
                return self._resume_after_probe_impl(initial["recovery_id"], key=key, execute=True, now_ts=now_ts)
        return self._resume_after_probe_impl(initial["recovery_id"], key=key, execute=False, now_ts=now_ts)

    def _resume_after_probe_impl(
        self,
        recovery_id: str,
        *,
        key: str,
        execute: bool,
        now_ts: float | None,
    ) -> dict[str, Any]:
        record = self._load_by_key(key) or self.load_record(recovery_id)
        if record is None:
            raise ValueError(f"Unknown recovery record: {recovery_id}")
        plan = record.get("delayed_resume_plan")
        if not plan:
            raise ValueError("Recovery record does not have a delayed_resume_plan")
        if record.get("mode") != "execute":
            raise ValueError("Cannot resume a dry-run plan; run recover --execute first")
        if record.get("status") in ("waiting_for_run", "recovered", "resumed"):
            dup = dict(record)
            dup["deduplicated"] = True
            return run.redact_secrets(dup)
        if record.get("status") not in RESUMABLE_DELAYED_STATUSES:
            raise ValueError(f"Record in status={record.get('status')!r} is not eligible for delayed resume")

        current_ts = now_ts if now_ts is not None else time.time()
        eligible_ts = _parse_iso(plan["eligible_at"])
        if current_ts < eligible_ts:
            self._record_event(
                record,
                "delayed_resume_waiting_for_reset",
                "waiting_for_reset",
                {"eligible_at": plan["eligible_at"]},
            )
            return self._persist(record, claim_idempotency=execute)

        # Re-verify original target before probing/launching (Finding 6)
        orig_ids = plan["original_ids"]
        recheck = self._verify_and_classify(
            kind=plan["target_kind"],
            role=plan["role"],
            lane=plan["lane"],
            bot=orig_ids.get("bot"),
            chat_id=orig_ids.get("chat_id"),
            automation_id=orig_ids.get("automation_id"),
            run_id=orig_ids.get("run_id"),
            dispatch_id=orig_ids.get("dispatch_id"),
            caller_failed_provider=None,
            caller_prior_attempted=record.get("attempted_providers", []),
        )
        if not recheck["settled"] or not recheck["classification"]["eligible_for_fallback"]:
            record["block_reason"] = recheck.get("reason") or "original_target_changed"
            self._record_event(record, "delayed_resume_original_recheck_blocked", "blocked", {"reason": record["block_reason"]})
            return self._persist(record, claim_idempotency=execute)
        if plan["target_kind"] == "automation":
            auto_now = recheck.get("raw_record") or {}
            if auto_now.get("enabled") is not False:
                record["block_reason"] = "original_automation_not_disabled"
                self._record_event(record, "delayed_resume_automation_enabled_blocked", "blocked")
                return self._persist(record, claim_idempotency=execute)

        candidate, providers, policy, meta = self._select_next_provider(
            role=plan["role"],
            lane=plan["lane"],
            attempted=record.get("attempted_providers", []),
            preferred_provider=plan.get("preferred_provider"),
            at_ts=current_ts,
        )
        if candidate is None:
            attempted_set = set(record.get("attempted_providers", []))
            unused_in_cooldown = [p for p in providers if p["name"] not in attempted_set]
            if unused_in_cooldown and meta["cooldowns"]:
                self._record_event(
                    record,
                    "delayed_resume_waiting_for_unused_provider_cooldown",
                    "waiting_for_reset",
                    {"cooldowns": meta["cooldowns"]},
                )
                return self._persist(record, claim_idempotency=execute)

            record["block_reason"] = "no_unused_eligible_provider_user_decision_required"
            record["user_decision_required"] = True
            plan["status"] = "blocked_user_decision_required"
            self._record_event(
                record,
                "delayed_resume_blocked_no_unused_provider",
                "blocked",
                {"attempted_providers": record.get("attempted_providers", []), "reason": record["block_reason"]},
            )
            return self._persist(record, claim_idempotency=execute)

        if not execute:
            self._record_event(
                record,
                "delayed_resume_dry_run_probe_required",
                "dry_run_probe_required",
                {"candidate": candidate["name"]},
            )
            return run.redact_secrets(record)

        probe_result = self.adapter.probe_health(
            candidate, timeout_seconds=int(plan.get("probe_timeout_seconds", 30))
        )
        record["external_action_receipts"].append(
            {"action": "health_probe", "at": run.now(), "receipt": _allowlisted_receipt(probe_result)}
        )
        if not probe_result.get("ok"):
            cooldown_seconds = int(SETTINGS.get("provider_cooldown_seconds", 900))
            run.update_cooldown(self.cooldown_path, candidate["name"], cooldown_seconds)
            plan["status"] = "probe_failed"
            plan["probe_passed"] = False
            plan["eligible_at"] = _iso_from_ts(current_ts + cooldown_seconds)
            self._record_event(
                record,
                "delayed_resume_probe_failed",
                "probe_failed",
                {"candidate": candidate["name"], "next_eligible_at": plan["eligible_at"]},
            )
            return self._persist(record, claim_idempotency=True)

        run.update_cooldown(self.cooldown_path, candidate["name"], 0)
        plan["probe_passed"] = True
        plan["status"] = "probe_passed"
        record["actual_provider"] = candidate["name"]

        kind = plan["target_kind"]
        settings = self._settings()
        if (kind == "bot_chat" and orig_ids.get("bot")) or (kind == "automation" and orig_ids.get("bot")):
            name = (
                f"Resumed {orig_ids['bot']} ({record['recovery_id']})"
                if kind == "bot_chat"
                else f"Resumed automation {orig_ids['automation_id']} ({record['recovery_id']})"
            )
            return self._launch_disabled_bot_recovery_run(
                record=record,
                name=name,
                bot=orig_ids["bot"],
                provider=candidate,
                prompt=plan["handoff_prompt"],
                by_iso=_iso_from_ts(current_ts + int(settings["once_deadline_seconds"])),
                item_id=str(orig_ids["item_id"]) if orig_ids.get("item_id") is not None else None,
                results_to=plan.get("results_to"),
            )

        spec_path = Path(plan["spec_file"]) if plan.get("spec_file") else None
        title = (
            f"Resumed automation {orig_ids['automation_id']}"
            if kind == "automation"
            else f"Resumed worker {orig_ids.get('dispatch_id')}"
        )
        job_result = self._run_worker_recovery(
            record=record,
            title=title,
            worktree=plan.get("worktree"),
            spec_file=spec_path,
            prompt=plan["handoff_prompt"],
            role=plan["role"],
            lane=plan["lane"],
            attempted=record.get("attempted_providers", []),
            only_provider=candidate["name"],
        )
        return self._finalize_sync_worker_result(
            record=record,
            job_result=job_result,
            kind=kind,
            role=plan["role"],
            providers=providers,
            policy=policy,
            preferred_provider=plan["preferred_provider"],
            original_ids=orig_ids,
            original_enabled=plan.get("original_enabled"),
            worktree=plan.get("worktree"),
            spec_file=spec_path,
            handoff_prompt=plan["handoff_prompt"],
            results_to=plan.get("results_to"),
            execute=True,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    rec = commands.add_parser("recover", help="Inspect or execute bounded recovery (dry-run by default)")
    rec.add_argument("--kind", choices=("worker", "bot_chat", "automation"), required=True)
    rec.add_argument("--role", required=True)
    rec.add_argument("--lane", required=True)
    rec.add_argument("--idempotency-key")
    rec.add_argument("--bot")
    rec.add_argument("--chat-id")
    rec.add_argument("--automation-id")
    rec.add_argument("--run-id")
    rec.add_argument("--dispatch-id")
    rec.add_argument("--task-id")
    rec.add_argument("--item-id")
    rec.add_argument("--worktree", type=Path)
    rec.add_argument("--spec-file", type=Path)
    rec.add_argument("--prompt")
    rec.add_argument("--preferred-provider")
    rec.add_argument("--failed-provider")
    rec.add_argument("--prior-attempted", nargs="*", default=[])
    rec.add_argument(
        "--execute",
        action="store_true",
        help="Perform live recovery mutations (default is read-only dry-run).",
    )

    reconcile_p = commands.add_parser(
        "reconcile", help="Reconcile accepted recovery run against terminal status (dry-run by default)"
    )
    reconcile_p.add_argument("--recovery-id", required=True, help="Recovery ID or idempotency key")
    reconcile_p.add_argument("--execute", action="store_true")

    res = commands.add_parser("resume", help="Run health-gated delayed resume (dry-run by default)")
    res.add_argument("--recovery-id", required=True, help="Recovery ID or idempotency key")
    res.add_argument("--execute", action="store_true")

    insp = commands.add_parser("inspect", help="Read a persisted recovery record")
    insp.add_argument("--recovery-id", required=True, help="Recovery ID or idempotency key")

    args = parser.parse_args()
    supervisor = RecoverySupervisor()
    try:
        if args.command == "inspect":
            result = supervisor.load_record(args.recovery_id)
            if result is None:
                raise ValueError(f"Recovery record not found: {args.recovery_id}")
        elif args.command == "reconcile":
            result = supervisor.reconcile(args.recovery_id, execute=args.execute)
        elif args.command == "resume":
            result = supervisor.resume_after_probe(args.recovery_id, execute=args.execute)
        else:
            result = supervisor.recover(
                kind=args.kind,
                role=args.role,
                lane=args.lane,
                idempotency_key=args.idempotency_key,
                bot=args.bot,
                chat_id=args.chat_id,
                automation_id=args.automation_id,
                run_id=args.run_id,
                dispatch_id=args.dispatch_id,
                task_id=args.task_id,
                item_id=args.item_id,
                worktree=args.worktree,
                spec_file=args.spec_file,
                prompt=args.prompt,
                preferred_provider=args.preferred_provider,
                failed_provider=args.failed_provider,
                prior_attempted=args.prior_attempted,
                execute=args.execute,
            )
    except Exception as error:
        print(run.redact_text(str(error)), file=sys.stderr)
        return 2
    print(json.dumps(run.redact_secrets(result), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
