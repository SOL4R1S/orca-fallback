#!/usr/bin/env python3
"""Idempotent, narrowly scoped, reversible wiring for bots, automations, and systemd units."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

from catalog import SETTINGS, resolve_policy
import recovery
import run


BOT_MARKER_BEGIN = "<!-- BEGIN ORCA-FALLBACK DISPATCH -->"
BOT_MARKER_END = "<!-- END ORCA-FALLBACK DISPATCH -->"
AUTO_MARKER_BEGIN = "<!-- BEGIN ORCA-FALLBACK AUTOMATION DISPATCH -->"
AUTO_MARKER_END = "<!-- END ORCA-FALLBACK AUTOMATION DISPATCH -->"
MCP_TOOL_NAME = "orca-fallback-dispatch"
SCHEDULED_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")

_BOT_BLOCK_RE = re.compile(
    rf"\n?{re.escape(BOT_MARKER_BEGIN)}.*?{re.escape(BOT_MARKER_END)}\n?",
    re.DOTALL,
)
_AUTO_BLOCK_RE = re.compile(
    rf"\n?{re.escape(AUTO_MARKER_BEGIN)}.*?{re.escape(AUTO_MARKER_END)}\n?",
    re.DOTALL,
)


def _upsert_marked_block(existing: str, block: str, pattern: re.Pattern[str]) -> str:
    cleaned = pattern.sub("\n", existing).strip()
    if not cleaned:
        return block.strip() + "\n"
    return cleaned + "\n\n" + block.strip() + "\n"


def _strip_marked_block(existing: str, pattern: re.Pattern[str]) -> str:
    stripped = pattern.sub("\n", existing).strip()
    return (stripped + "\n") if stripped else ""


def render_bot_guidance_block(*, default_role: str, default_lane: str) -> str:
    return "\n".join(
        [
            BOT_MARKER_BEGIN,
            "## Shared Dispatch (orca-fallback)",
            f"- Delegate tasks via `orca_fallback_dispatch` (or `dispatch.py`) with explicit `--role` and `--lane` (default `{default_role}` / `{default_lane}`).",
            "- Never launch raw `orchestration worker-start` directly for delegated tasks and never escalate lanes automatically.",
            "- Native bot-chat turns are invoked by Orca itself; this guidance applies to delegated tasks.",
            BOT_MARKER_END,
        ]
    )


def render_automation_guidance_block(
    *,
    automation_id: str,
    role: str,
    lane: str,
    worktree: str,
    repo_root: Path,
) -> str:
    dispatch_script = repo_root.resolve() / "dispatch.py"
    return "\n".join(
        [
            AUTO_MARKER_BEGIN,
            f"For delegated work in this automation, invoke `python3 {dispatch_script} --worktree {worktree} --role {role} --lane {lane} --caller-kind automation --automation-id {automation_id} --title <title> --spec-file <path>` (or the `orca_fallback_dispatch` tool) instead of direct `orchestration worker-start`.",
            "Do not escalate lanes automatically.",
            AUTO_MARKER_END,
        ]
    )


def assess_automation_eligibility(auto: dict[str, Any]) -> tuple[bool, str | None, dict[str, Any]]:
    """Determine whether an Orca automation is safe and eligible for scoped dispatch wiring."""
    if not isinstance(auto, dict) or not auto.get("id"):
        return False, "missing_automation_id", {}
    if not isinstance(auto.get("enabled"), bool):
        return False, "legacy_missing_enabled", {}
    name = str(auto.get("name") or "")
    if name.startswith("Recover ") or name.startswith("Resumed "):
        return False, "recovery_automation", {}
    if auto.get("once"):
        return False, "one_time_automation", {}
    if not (auto.get("rrule") or auto.get("trigger")):
        return False, "missing_schedule", {}
    ws_mode = auto.get("workspaceMode")
    if ws_mode not in (None, "existing"):
        return False, "unsupported_workspace_mode", {}
    if auto.get("reuseSession"):
        return False, "reuse_session_enabled", {}
    prompt = auto.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return False, "missing_prompt", {}
    worktree = recovery._automation_worktree(auto)
    if not worktree or not Path(worktree).is_dir():
        return False, "missing_or_invalid_worktree", {}
    return True, None, {
        "worktree": str(Path(worktree).resolve()),
        "bot_id": recovery._automation_bot_id(auto),
    }


def _extract_tool_entries(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("tools", "botTools", "own", "ownTools", "servers"):
        val = payload.get(key)
        if isinstance(val, list):
            return [item for item in val if isinstance(item, dict)]
    return []


class WiringManager:
    """Manages idempotent, scoped, reversible wiring across bots, automations, and systemd units."""

    def __init__(
        self,
        *,
        adapter: recovery.OrcaRecoveryAdapter | None = None,
        state_dir: Path | None = None,
        repo_root: Path | None = None,
        systemd_dir: Path | None = None,
        python_executable: str | None = None,
    ):
        self.adapter = adapter or recovery.OrcaRecoveryAdapter()
        self._explicit_state_dir = state_dir
        self.repo_root = (repo_root or Path(__file__).resolve().parent).resolve()
        self.systemd_dir = (systemd_dir or (Path.home() / ".config" / "systemd" / "user")).expanduser()
        self.python_executable = python_executable or sys.executable or "/usr/bin/python3"

    @property
    def state_dir(self) -> Path:
        return self._explicit_state_dir or run.STATE_DIR

    @property
    def wiring_dir(self) -> Path:
        return self.state_dir / "wiring"

    @property
    def enrollments_path(self) -> Path:
        return self.wiring_dir / "enrollments.json"

    def _load_enrollments(self) -> dict[str, Any]:
        if not self.enrollments_path.is_file():
            return {"version": 1, "bots": {}, "automations": {}, "scheduled_dispatches": {}}
        try:
            data = json.loads(self.enrollments_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "bots": {}, "automations": {}, "scheduled_dispatches": {}}
        if not isinstance(data, dict):
            return {"version": 1, "bots": {}, "automations": {}, "scheduled_dispatches": {}}
        data.setdefault("version", 1)
        data.setdefault("bots", {})
        data.setdefault("automations", {})
        data.setdefault("scheduled_dispatches", {})
        return data

    def _save_enrollments(self, data: dict[str, Any]) -> None:
        run.write_state(self.enrollments_path, data)

    def _mcp_command(self) -> str:
        return f"{self.python_executable} {self.repo_root / 'mcp_dispatch.py'}"

    def install_bot(
        self,
        bot: str,
        *,
        default_role: str = "implementation",
        default_lane: str = "economy",
        register_mcp: bool = True,
        execute: bool = False,
    ) -> dict[str, Any]:
        resolve_policy(default_role, default_lane)
        assets = self.adapter.read_bot_assets(bot)
        if not assets.get("exists"):
            raise ValueError(f"Bot folder does not exist for {bot!r}: {assets.get('bot_dir')}")
        resolved_bot = assets["resolved_bot"]
        bot_dir = Path(assets["bot_dir"])
        soul_path = bot_dir / "SOUL.md"
        existing_soul = soul_path.read_text(encoding="utf-8") if soul_path.is_file() else ""
        guidance = render_bot_guidance_block(default_role=default_role, default_lane=default_lane)
        desired_soul = _upsert_marked_block(existing_soul, guidance, _BOT_BLOCK_RE)
        soul_changed = existing_soul != desired_soul

        mcp_cmd = self._mcp_command()
        mcp_action = "skipped"
        if register_mcp:
            tools_payload = self.adapter.bot_tools(resolved_bot)
            existing_tools = _extract_tool_entries(tools_payload)
            matched = next(
                (t for t in existing_tools if t.get("name") == MCP_TOOL_NAME),
                None,
            )
            if matched is None:
                mcp_action = "add"
            else:
                cmd_val = matched.get("detail") or matched.get("command") or matched.get("source") or ""
                mcp_action = "unchanged" if cmd_val == mcp_cmd else "replace"

        result: dict[str, Any] = {
            "mode": "execute" if execute else "dry_run",
            "target": "bot",
            "bot": resolved_bot,
            "bot_dir": str(bot_dir),
            "default_role": default_role,
            "default_lane": default_lane,
            "soul_changed": soul_changed,
            "mcp_tool": MCP_TOOL_NAME,
            "mcp_command": mcp_cmd,
            "mcp_action": mcp_action,
            "boundary_note": (
                "Native Orca bot-chat turns are launched internally by Orca and cannot be externally intercepted; "
                "this installs a bot-owned MCP delegation tool plus concise SOUL.md guidance."
            ),
        }
        if not execute:
            result["status"] = "dry_run_planned"
            return run.redact_secrets(result)

        snap_path = self.wiring_dir / "bots" / f"{resolved_bot}.json"
        if not snap_path.is_file():
            run.write_state(
                snap_path,
                {
                    "bot": resolved_bot,
                    "soul_existed": soul_path.is_file(),
                    "original_soul": existing_soul,
                    "installed_at": run.now(),
                },
            )
        if soul_changed:
            soul_path.write_text(desired_soul, encoding="utf-8")

        if register_mcp:
            if mcp_action == "replace":
                self.adapter.bot_tools_rm(resolved_bot, MCP_TOOL_NAME)
                self.adapter.bot_tools_add(resolved_bot, mcp_cmd, name=MCP_TOOL_NAME)
            elif mcp_action == "add":
                self.adapter.bot_tools_add(resolved_bot, mcp_cmd, name=MCP_TOOL_NAME)

        enrollments = self._load_enrollments()
        enrollments["bots"][resolved_bot] = {
            "default_role": default_role,
            "default_lane": default_lane,
            "mcp_tool": MCP_TOOL_NAME if register_mcp else None,
            "updated_at": run.now(),
        }
        self._save_enrollments(enrollments)
        result["status"] = "installed" if (soul_changed or mcp_action in ("add", "replace")) else "unchanged"
        return run.redact_secrets(result)

    def uninstall_bot(
        self,
        bot: str,
        *,
        unregister_mcp: bool = True,
        execute: bool = False,
    ) -> dict[str, Any]:
        assets = self.adapter.read_bot_assets(bot)
        resolved_bot = assets.get("resolved_bot") or bot
        bot_dir = Path(assets["bot_dir"])
        soul_path = bot_dir / "SOUL.md"
        existing_soul = soul_path.read_text(encoding="utf-8") if soul_path.is_file() else ""
        stripped_soul = _strip_marked_block(existing_soul, _BOT_BLOCK_RE)
        soul_changed = bool(assets.get("exists") and existing_soul != stripped_soul)

        mcp_present = False
        if unregister_mcp and assets.get("exists"):
            try:
                tools_payload = self.adapter.bot_tools(resolved_bot)
                mcp_present = any(t.get("name") == MCP_TOOL_NAME for t in _extract_tool_entries(tools_payload))
            except Exception:
                mcp_present = False

        result: dict[str, Any] = {
            "mode": "execute" if execute else "dry_run",
            "target": "bot",
            "bot": resolved_bot,
            "soul_changed": soul_changed,
            "mcp_remove": mcp_present,
        }
        if not execute:
            result["status"] = "dry_run_planned"
            return run.redact_secrets(result)

        snap_path = self.wiring_dir / "bots" / f"{resolved_bot}.json"
        if soul_changed:
            if snap_path.is_file():
                snap = json.loads(snap_path.read_text(encoding="utf-8"))
                orig_soul = snap.get("original_soul", "")
                if not snap.get("soul_existed") and not stripped_soul:
                    soul_path.unlink(missing_ok=True)
                elif stripped_soul.strip() == orig_soul.strip():
                    soul_path.write_text(orig_soul, encoding="utf-8")
                else:
                    soul_path.write_text(stripped_soul, encoding="utf-8")
            else:
                soul_path.write_text(stripped_soul, encoding="utf-8")
        if mcp_present:
            self.adapter.bot_tools_rm(resolved_bot, MCP_TOOL_NAME)
        snap_path.unlink(missing_ok=True)

        enrollments = self._load_enrollments()
        enrollments["bots"].pop(resolved_bot, None)
        self._save_enrollments(enrollments)
        result["status"] = "uninstalled"
        return run.redact_secrets(result)

    def install_automation(
        self,
        automation_id: str,
        *,
        role: str,
        lane: str,
        execute: bool = False,
    ) -> dict[str, Any]:
        if not role or not lane:
            raise ValueError("Both role and lane must be specified explicitly")
        resolve_policy(role, lane)
        auto = self.adapter.automations_show(automation_id)
        eligible, skip_reason, details = assess_automation_eligibility(auto)
        if not eligible:
            return run.redact_secrets(
                {
                    "mode": "execute" if execute else "dry_run",
                    "target": "automation",
                    "automation_id": automation_id,
                    "status": "skipped_ineligible",
                    "reason": skip_reason,
                }
            )

        current_prompt = auto["prompt"]
        block = render_automation_guidance_block(
            automation_id=automation_id,
            role=role,
            lane=lane,
            worktree=details["worktree"],
            repo_root=self.repo_root,
        )
        desired_prompt = _upsert_marked_block(current_prompt, block, _AUTO_BLOCK_RE)
        prompt_changed = current_prompt != desired_prompt

        result: dict[str, Any] = {
            "mode": "execute" if execute else "dry_run",
            "target": "automation",
            "automation_id": automation_id,
            "role": role,
            "lane": lane,
            "worktree": details["worktree"],
            "prompt_changed": prompt_changed,
            "boundary_note": (
                "Orca automations always launch an LLM agent (--provider or --bot) with --prompt and have no "
                "supported deterministic command-only entry point (and headless orca-serve has no plugin hook service). "
                "This adds concise, reversible prompt guidance for delegated work; use install-scheduled-dispatch "
                "for deterministic systemd scheduled dispatch on local worktrees."
            ),
        }
        if not execute:
            result["status"] = "dry_run_planned" if prompt_changed else "dry_run_unchanged"
            return run.redact_secrets(result)

        snap_path = self.wiring_dir / "automations" / f"{automation_id}.json"
        if not snap_path.is_file():
            run.write_state(
                snap_path,
                {
                    "automation_id": automation_id,
                    "name": auto.get("name"),
                    "original_prompt": current_prompt,
                    "original_enabled": auto.get("enabled"),
                    "worktree": details["worktree"],
                    "installed_at": run.now(),
                },
            )
        if prompt_changed:
            self.adapter.orca.call("automations", "edit", automation_id, "--prompt", desired_prompt)

        enrollments = self._load_enrollments()
        existing_enrollment = enrollments["automations"].get(automation_id)
        if isinstance(existing_enrollment, dict) and existing_enrollment.get("enrolled_at"):
            enrolled_at = existing_enrollment["enrolled_at"]
            baseline_run_ids = list(existing_enrollment.get("baseline_run_ids") or [])
        else:
            enrolled_at = run.now()
            runs_payload = self.adapter.automations_runs(automation_id)
            runs = runs_payload.get("runs", []) if isinstance(runs_payload, dict) else []
            baseline_run_ids = [str(r["id"]) for r in runs if isinstance(r, dict) and r.get("id")]
        enrollments["automations"][automation_id] = {
            "role": role,
            "lane": lane,
            "worktree": details["worktree"],
            "enrolled_at": enrolled_at,
            "baseline_run_ids": baseline_run_ids,
            "updated_at": run.now(),
        }
        self._save_enrollments(enrollments)
        result["enrolled_at"] = enrolled_at
        result["baseline_run_count"] = len(baseline_run_ids)
        result["status"] = "installed" if prompt_changed else "unchanged"
        return run.redact_secrets(result)

    def uninstall_automation(self, automation_id: str, *, execute: bool = False) -> dict[str, Any]:
        auto = self.adapter.automations_show(automation_id)
        current_prompt = str(auto.get("prompt") or "")
        restored_prompt = _strip_marked_block(current_prompt, _AUTO_BLOCK_RE)
        snap_path = self.wiring_dir / "automations" / f"{automation_id}.json"
        if snap_path.is_file():
            try:
                snap = json.loads(snap_path.read_text(encoding="utf-8"))
                orig_prompt = snap.get("original_prompt")
                if isinstance(orig_prompt, str) and restored_prompt.strip() == orig_prompt.strip():
                    restored_prompt = orig_prompt
            except (OSError, json.JSONDecodeError):
                pass
        prompt_changed = current_prompt != restored_prompt

        result: dict[str, Any] = {
            "mode": "execute" if execute else "dry_run",
            "target": "automation",
            "automation_id": automation_id,
            "prompt_changed": prompt_changed,
        }
        if not execute:
            result["status"] = "dry_run_planned"
            return run.redact_secrets(result)

        if prompt_changed:
            self.adapter.orca.call("automations", "edit", automation_id, "--prompt", restored_prompt)
        snap_path.unlink(missing_ok=True)
        enrollments = self._load_enrollments()
        enrollments["automations"].pop(automation_id, None)
        self._save_enrollments(enrollments)
        result["status"] = "uninstalled"
        return run.redact_secrets(result)

    def _rendered_supervisor_service(self) -> str:
        template = (self.repo_root / "units" / "orca-recovery-supervisor.service").read_text(encoding="utf-8")
        if self.repo_root == (Path.home() / "orca-fallback").resolve() and self.python_executable == "/usr/bin/python3":
            return template
        return (
            template.replace(
                "WorkingDirectory=%h/orca-fallback",
                f"WorkingDirectory={str(self.repo_root).replace('%', '%%')}",
            )
            .replace(
                "ExecStart=/usr/bin/python3 %h/orca-fallback/recovery.py supervise --execute",
                f"ExecStart={self._systemd_quote(self.python_executable)} {self._systemd_quote(str(self.repo_root / 'recovery.py'))} supervise --execute",
            )
        )

    def install_systemd(
        self,
        *,
        include_catalog: bool = False,
        enable_units: bool = False,
        execute: bool = False,
    ) -> dict[str, Any]:
        unit_names = ["orca-recovery-supervisor.service", "orca-recovery-supervisor.timer"]
        if include_catalog:
            unit_names.extend(["orca-model-catalog.service", "orca-model-catalog.timer"])

        planned: list[dict[str, str]] = []
        for name in unit_names:
            src = self.repo_root / "units" / name
            if not src.is_file():
                raise FileNotFoundError(f"Missing unit template: {src}")
            planned.append({"unit": name, "dest": str(self.systemd_dir / name)})

        result: dict[str, Any] = {
            "mode": "execute" if execute else "dry_run",
            "target": "systemd",
            "systemd_dir": str(self.systemd_dir),
            "units": planned,
            "enable_units": enable_units,
        }
        if not execute:
            result["status"] = "dry_run_planned"
            return result

        self.systemd_dir.mkdir(parents=True, exist_ok=True)
        for name in unit_names:
            dest = self.systemd_dir / name
            src = self.repo_root / "units" / name
            content = self._rendered_supervisor_service() if name == "orca-recovery-supervisor.service" else src.read_text(encoding="utf-8")
            if dest.is_symlink():
                dest.unlink()
            dest.write_text(content, encoding="utf-8")

        if enable_units:
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
            timers = [u for u in unit_names if u.endswith(".timer")]
            for timer in timers:
                subprocess.run(["systemctl", "--user", "enable", "--now", timer], check=True)

        result["status"] = "installed"
        return result

    def uninstall_systemd(
        self,
        *,
        include_catalog: bool = False,
        disable_units: bool = False,
        execute: bool = False,
    ) -> dict[str, Any]:
        unit_names = ["orca-recovery-supervisor.timer", "orca-recovery-supervisor.service"]
        if include_catalog:
            unit_names.extend(["orca-model-catalog.timer", "orca-model-catalog.service"])
        existing = [name for name in unit_names if (self.systemd_dir / name).exists() or (self.systemd_dir / name).is_symlink()]
        result: dict[str, Any] = {
            "mode": "execute" if execute else "dry_run",
            "target": "systemd",
            "systemd_dir": str(self.systemd_dir),
            "units": existing,
            "disable_units": disable_units,
        }
        if not execute:
            result["status"] = "dry_run_planned"
            return result

        if disable_units:
            for name in existing:
                if name.endswith(".timer"):
                    subprocess.run(["systemctl", "--user", "disable", "--now", name], check=False)
        for name in existing:
            (self.systemd_dir / name).unlink(missing_ok=True)
        if disable_units:
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        result["status"] = "uninstalled"
        return result

    @staticmethod
    def _validate_unit_value(value: str, label: str) -> str:
        if any(ch in value for ch in ("\n", "\r", "\x00")):
            raise ValueError(f"Invalid control characters in {label}")
        return value

    @staticmethod
    def _systemd_quote(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
        return f'"{escaped}"'

    def install_scheduled_dispatch(
        self,
        *,
        name: str,
        worktree: Path | str,
        role: str,
        lane: str,
        spec_file: Path | str,
        title: str | None = None,
        on_calendar: str = "daily",
        enable_unit: bool = False,
        execute: bool = False,
    ) -> dict[str, Any]:
        if not SCHEDULED_NAME_RE.fullmatch(name):
            raise ValueError(f"Invalid scheduled dispatch name {name!r}; use [A-Za-z0-9][A-Za-z0-9_-]{{0,47}}")
        if not role or not lane:
            raise ValueError("Both role and lane must be specified explicitly")
        resolve_policy(role, lane)
        worktree_path = Path(worktree).resolve()
        if not worktree_path.is_dir():
            raise ValueError(f"Worktree does not exist: {worktree_path}")
        spec_path = Path(spec_file).resolve()
        if not spec_path.is_file() or not spec_path.read_text(encoding="utf-8").strip():
            raise ValueError(f"Spec file must exist and be non-empty: {spec_path}")
        task_title = self._validate_unit_value((title or f"Scheduled {name}").strip(), "title")
        calendar_expr = self._validate_unit_value(on_calendar.strip(), "on_calendar")
        if not calendar_expr:
            raise ValueError("on_calendar must be non-empty")

        svc_name = f"orca-dispatch-{name}.service"
        tmr_name = f"orca-dispatch-{name}.timer"
        exec_args = [
            self.python_executable,
            str(self.repo_root / "dispatch.py"),
            "--worktree",
            str(worktree_path),
            "--title",
            task_title,
            "--role",
            role,
            "--lane",
            lane,
            "--spec-file",
            str(spec_path),
            "--caller-kind",
            "automation",
        ]
        exec_start = " ".join(self._systemd_quote(arg) for arg in exec_args)
        working_dir = str(worktree_path).replace("%", "%%")
        svc_content = "\n".join(
            [
                "[Unit]",
                f"Description=Deterministic Orca fallback scheduled dispatch ({name})",
                "After=orca-serve.service",
                "",
                "[Service]",
                "Type=oneshot",
                "Environment=PATH=%h/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                f"WorkingDirectory={working_dir}",
                f"ExecStart={exec_start}",
                "",
            ]
        )
        tmr_content = "\n".join(
            [
                "[Unit]",
                f"Description=Schedule deterministic Orca fallback dispatch ({name})",
                "",
                "[Timer]",
                f"OnCalendar={calendar_expr}",
                "Persistent=true",
                f"Unit={svc_name}",
                "",
                "[Install]",
                "WantedBy=timers.target",
                "",
            ]
        )
        result: dict[str, Any] = {
            "mode": "execute" if execute else "dry_run",
            "target": "scheduled_dispatch",
            "name": name,
            "systemd_dir": str(self.systemd_dir),
            "units": [
                {"unit": svc_name, "dest": str(self.systemd_dir / svc_name)},
                {"unit": tmr_name, "dest": str(self.systemd_dir / tmr_name)},
            ],
            "worktree": str(worktree_path),
            "spec_file": str(spec_path),
            "role": role,
            "lane": lane,
            "on_calendar": calendar_expr,
            "enable_unit": enable_unit,
        }
        if not execute:
            result["status"] = "dry_run_planned"
            return run.redact_secrets(result)

        self.systemd_dir.mkdir(parents=True, exist_ok=True)
        for uname, content in ((svc_name, svc_content), (tmr_name, tmr_content)):
            dest = self.systemd_dir / uname
            if dest.is_symlink():
                dest.unlink()
            dest.write_text(content, encoding="utf-8")

        if enable_unit:
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
            subprocess.run(["systemctl", "--user", "enable", "--now", tmr_name], check=True)

        enrollments = self._load_enrollments()
        enrollments["scheduled_dispatches"][name] = {
            "role": role,
            "lane": lane,
            "worktree": str(worktree_path),
            "spec_file": str(spec_path),
            "on_calendar": calendar_expr,
            "service": svc_name,
            "timer": tmr_name,
            "updated_at": run.now(),
        }
        self._save_enrollments(enrollments)
        result["status"] = "installed"
        return run.redact_secrets(result)

    def uninstall_scheduled_dispatch(
        self,
        *,
        name: str,
        disable_unit: bool = False,
        execute: bool = False,
    ) -> dict[str, Any]:
        if not SCHEDULED_NAME_RE.fullmatch(name):
            raise ValueError(f"Invalid scheduled dispatch name {name!r}")
        tmr_name = f"orca-dispatch-{name}.timer"
        svc_name = f"orca-dispatch-{name}.service"
        unit_names = [tmr_name, svc_name]
        existing = [
            uname for uname in unit_names if (self.systemd_dir / uname).exists() or (self.systemd_dir / uname).is_symlink()
        ]
        result: dict[str, Any] = {
            "mode": "execute" if execute else "dry_run",
            "target": "scheduled_dispatch",
            "name": name,
            "systemd_dir": str(self.systemd_dir),
            "units": existing,
            "disable_unit": disable_unit,
        }
        if not execute:
            result["status"] = "dry_run_planned"
            return result

        if disable_unit and tmr_name in existing:
            subprocess.run(["systemctl", "--user", "disable", "--now", tmr_name], check=False)
        for uname in existing:
            (self.systemd_dir / uname).unlink(missing_ok=True)
        if disable_unit:
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        enrollments = self._load_enrollments()
        enrollments["scheduled_dispatches"].pop(name, None)
        self._save_enrollments(enrollments)
        result["status"] = "uninstalled"
        return result

    def status(self) -> dict[str, Any]:
        enrollments = self._load_enrollments()
        autos_payload = self.adapter.automations_list()
        autos = autos_payload if isinstance(autos_payload, list) else autos_payload.get("automations", [])
        automation_assessment = []
        for auto in autos:
            if not isinstance(auto, dict):
                continue
            aid = str(auto.get("id") or "")
            eligible, reason, _ = assess_automation_eligibility(auto)
            automation_assessment.append(
                {
                    "id": aid,
                    "name": auto.get("name"),
                    "enrolled": aid in enrollments["automations"],
                    "eligible": eligible,
                    "skip_reason": reason,
                }
            )
        units_present = {
            name: ((self.systemd_dir / name).exists() or (self.systemd_dir / name).is_symlink())
            for name in (
                "orca-recovery-supervisor.service",
                "orca-recovery-supervisor.timer",
                "orca-model-catalog.service",
                "orca-model-catalog.timer",
            )
        }
        return run.redact_secrets(
            {
                "enrollments": enrollments,
                "automations": automation_assessment,
                "systemd_units": units_present,
            }
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("status", help="Inspect current wiring enrollments and automation eligibility (read-only)")

    ib = commands.add_parser("install-bot", help="Wire bot delegation tool and short SOUL.md guidance (dry-run by default)")
    ib.add_argument("--bot", required=True)
    ib.add_argument("--role", default="implementation")
    ib.add_argument("--lane", default="economy")
    ib.add_argument("--no-mcp", action="store_true")
    ib.add_argument("--execute", action="store_true")

    ub = commands.add_parser("uninstall-bot", help="Remove bot delegation tool and SOUL.md block (dry-run by default)")
    ub.add_argument("--bot", required=True)
    ub.add_argument("--no-mcp", action="store_true")
    ub.add_argument("--execute", action="store_true")

    ia = commands.add_parser("install-automation", help="Enroll and wire an eligible scheduled automation (dry-run by default)")
    ia.add_argument("--automation-id", required=True)
    ia.add_argument("--role", required=True)
    ia.add_argument("--lane", required=True)
    ia.add_argument("--execute", action="store_true")

    ua = commands.add_parser("uninstall-automation", help="Unenroll and restore a wired automation (dry-run by default)")
    ua.add_argument("--automation-id", required=True)
    ua.add_argument("--execute", action="store_true")

    isys = commands.add_parser("install-systemd", help="Install systemd user units for recovery supervisor (dry-run by default)")
    isys.add_argument("--systemd-dir", type=Path)
    isys.add_argument("--include-catalog", action="store_true")
    isys.add_argument("--enable", action="store_true")
    isys.add_argument("--execute", action="store_true")

    usys = commands.add_parser("uninstall-systemd", help="Uninstall systemd user units (dry-run by default)")
    usys.add_argument("--systemd-dir", type=Path)
    usys.add_argument("--include-catalog", action="store_true")
    usys.add_argument("--disable", action="store_true")
    usys.add_argument("--execute", action="store_true")

    isd = commands.add_parser(
        "install-scheduled-dispatch",
        help="Install opt-in systemd scheduled deterministic dispatch units for a local worktree (dry-run by default)",
    )
    isd.add_argument("--name", required=True)
    isd.add_argument("--worktree", type=Path, required=True)
    isd.add_argument("--role", required=True)
    isd.add_argument("--lane", required=True)
    isd.add_argument("--spec-file", type=Path, required=True)
    isd.add_argument("--title")
    isd.add_argument("--on-calendar", default="daily")
    isd.add_argument("--systemd-dir", type=Path)
    isd.add_argument("--enable", action="store_true")
    isd.add_argument("--execute", action="store_true")

    usd = commands.add_parser(
        "uninstall-scheduled-dispatch",
        help="Uninstall opt-in systemd scheduled dispatch units (dry-run by default)",
    )
    usd.add_argument("--name", required=True)
    usd.add_argument("--systemd-dir", type=Path)
    usd.add_argument("--disable", action="store_true")
    usd.add_argument("--execute", action="store_true")

    args = parser.parse_args()
    systemd_dir = getattr(args, "systemd_dir", None)
    manager = WiringManager(systemd_dir=systemd_dir)
    try:
        if args.command == "status":
            result = manager.status()
        elif args.command == "install-bot":
            result = manager.install_bot(
                args.bot,
                default_role=args.role,
                default_lane=args.lane,
                register_mcp=not args.no_mcp,
                execute=args.execute,
            )
        elif args.command == "uninstall-bot":
            result = manager.uninstall_bot(
                args.bot,
                unregister_mcp=not args.no_mcp,
                execute=args.execute,
            )
        elif args.command == "install-automation":
            result = manager.install_automation(
                args.automation_id,
                role=args.role,
                lane=args.lane,
                execute=args.execute,
            )
        elif args.command == "uninstall-automation":
            result = manager.uninstall_automation(args.automation_id, execute=args.execute)
        elif args.command == "install-systemd":
            result = manager.install_systemd(
                include_catalog=args.include_catalog,
                enable_units=args.enable,
                execute=args.execute,
            )
        elif args.command == "uninstall-systemd":
            result = manager.uninstall_systemd(
                include_catalog=args.include_catalog,
                disable_units=args.disable,
                execute=args.execute,
            )
        elif args.command == "install-scheduled-dispatch":
            result = manager.install_scheduled_dispatch(
                name=args.name,
                worktree=args.worktree,
                role=args.role,
                lane=args.lane,
                spec_file=args.spec_file,
                title=args.title,
                on_calendar=args.on_calendar,
                enable_unit=args.enable,
                execute=args.execute,
            )
        else:
            result = manager.uninstall_scheduled_dispatch(
                name=args.name,
                disable_unit=args.disable,
                execute=args.execute,
            )
    except Exception as error:
        print(run.redact_text(str(error)), file=sys.stderr)
        return 2
    print(json.dumps(run.redact_secrets(result), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
