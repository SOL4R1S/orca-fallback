#!/usr/bin/env python3
"""Gated isolated live E2E harness for orca-fallback dispatch and recovery.

Safety gates:
- Requires an explicit fresh temporary ORCA_USER_DATA_PATH inside the system temp dir.
- Unsets ORCA_PAIRING_CODE and ORCA_ENVIRONMENT so paired/remote hosts are unreachable.
- Starts a disposable `orca-ide serve` bound exclusively to 127.0.0.1:<ephemeral_port>.
- Asserts empty initial automations/bots/agents before any mutation.
- Never touches production Atlas, production ~/.orca state, or production worktrees.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import catalog
import dispatch
import recovery
import run


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _assert_isolated_env(env: dict[str, str], user_data_path: Path) -> None:
    tmp_root = Path(tempfile.gettempdir()).resolve()
    resolved_ud = user_data_path.resolve()
    if tmp_root not in resolved_ud.parents:
        raise RuntimeError(f"Safety gate failed: ORCA_USER_DATA_PATH must live under {tmp_root}: {resolved_ud}")
    if env.get("ORCA_PAIRING_CODE") or env.get("ORCA_ENVIRONMENT"):
        raise RuntimeError("Safety gate failed: ORCA_PAIRING_CODE and ORCA_ENVIRONMENT must be unset")
    prod_orca = (Path.home() / ".config" / "Orca").resolve()
    if resolved_ud == prod_orca:
        raise RuntimeError("Safety gate failed: refusing to target production Orca data directory")


class IsolatedOrca(run.Orca):
    """Orca CLI wrapper pinned to the disposable isolated environment and repo cwd."""

    def __init__(self, executable: str, env: dict[str, str], default_cwd: Path):
        super().__init__(executable)
        self.env = env
        self.default_cwd = default_cwd

    def call(self, *args: str, cwd: Path | None = None, timeout: int = 90) -> dict:
        completed = subprocess.run(
            [self.executable, *args, "--json"],
            cwd=cwd or self.default_cwd,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            sanitized_err = run.redact_text(completed.stderr)[-1000:]
            raise RuntimeError(f"Orca returned invalid JSON: {sanitized_err}") from error
        if completed.returncode or not result.get("ok", False):
            err_payload = run.redact_secrets(result.get("error", result))
            raise RuntimeError(json.dumps(err_payload, ensure_ascii=False))
        return result.get("result", result)


def _wait_for_runtime(env: dict[str, str], timeout_seconds: int = 20) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        proc = subprocess.run(
            ["orca-ide", "status", "--json"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            payload = json.loads(proc.stdout)
            runtime = payload.get("result", {}).get("runtime", {})
            if runtime.get("reachable") and runtime.get("state") == "ready":
                return payload["result"]
        time.sleep(0.3)
    raise RuntimeError("Isolated orca-ide serve runtime did not become ready in time")


def _wait_for_automation_terminal_run(orca: IsolatedOrca, automation_id: str, timeout_seconds: int = 45) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    seen_statuses: list[str] = []
    while time.monotonic() < deadline:
        runs = orca.call("automations", "runs", "--id", automation_id, "--limit", "5").get("runs", [])
        if runs:
            latest = runs[0]
            st = str(latest.get("status") or "").lower()
            if not seen_statuses or seen_statuses[-1] != st:
                seen_statuses.append(st)
            if st not in recovery.ACTIVE_RUN_STATES:
                return {"run": latest, "seen_statuses": seen_statuses}
        time.sleep(1.5)
    raise TimeoutError(f"Automation {automation_id} did not settle within {timeout_seconds}s (seen={seen_statuses})")


def run_isolated_e2e() -> dict[str, Any]:
    port = _free_loopback_port()
    report: dict[str, Any] = {
        "loopback_port": port,
        "scenarios": {},
        "cleanup": {},
    }

    with tempfile.TemporaryDirectory(prefix="orca-recovery-e2e-") as td:
        root = Path(td).resolve()
        ud = root / "user-data"
        ud.mkdir()
        state_dir = root / "fallback-state"
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "e2e@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "E2E Runner"], cwd=repo, check=True)
        (repo / "README.md").write_text("Isolated E2E repository.\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)

        env = os.environ.copy()
        env["ORCA_USER_DATA_PATH"] = str(ud)
        env["ORCA_SERVE_USER_DATA_PATH"] = str(ud)
        env["ORCA_INSTANCE_ID"] = f"e2e-{port}"
        env["ORCA_FALLBACK_STATE_DIR"] = str(state_dir)
        env.pop("ORCA_PAIRING_CODE", None)
        env.pop("ORCA_ENVIRONMENT", None)
        _assert_isolated_env(env, ud)

        # Write disposable catalog/selection/benchmarks in temp dir (monkeypatched in-process)
        cat_path = root / "catalog.json"
        sel_path = root / "selection.json"
        bench_path = root / "benchmarks.json"
        cat_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "models": [
                        {
                            "key": "codex:nonexistent-e2e-model-xyz",
                            "agent": "codex",
                            "model": "nonexistent-e2e-model-xyz",
                            "provider": "openai",
                            "available": True,
                            "pricing": {"cost4": {"input": 0.1, "output": 0.4}},
                        },
                        {
                            "key": "codex:gpt-6-luna",
                            "agent": "codex",
                            "model": "gpt-6-luna",
                            "provider": "openai",
                            "available": True,
                            "pricing": {"cost4": {"input": 0.2, "output": 0.6}},
                        },
                        {
                            "key": "antigravity:gemini-4-hydrogen-high",
                            "agent": "antigravity",
                            "model": "gemini-4-hydrogen-high",
                            "provider": "antigravity",
                            "available": True,
                            "pricing": {"cost4": {"input": 2.0, "output": 8.0}},
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        sel_path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "roles": {
                        "implementation": {
                            "default_lane": "economy",
                            "allow_lane_escalation": False,
                            "lanes": {
                                "economy": {
                                    "budget": {"max_comparison_price_usd": 1, "min_intelligence_index": 20},
                                    "models": [
                                        {"key": "codex:nonexistent-e2e-model-xyz"},
                                        {"key": "codex:gpt-6-luna", "effort": "low"},
                                    ],
                                },
                                "premium": {
                                    "budget": {"max_comparison_price_usd": 8, "min_intelligence_index": 40},
                                    "models": [
                                        {"key": "antigravity:gemini-4-hydrogen-high"},
                                    ],
                                },
                            },
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        bench_path.write_text(
            json.dumps(
                {
                    "entries": [
                        {"model_key": "codex:nonexistent-e2e-model-xyz", "benchmark": "artificial_analysis.intelligence_index", "score": 25},
                        {"model_key": "codex:gpt-6-luna", "benchmark": "artificial_analysis.intelligence_index", "score": 30},
                        {"model_key": "antigravity:gemini-4-hydrogen-high", "benchmark": "artificial_analysis.intelligence_index", "score": 62},
                    ]
                }
            ),
            encoding="utf-8",
        )

        orig_cat, orig_sel, orig_root = catalog.CATALOG_PATH, catalog.SELECTION_PATH, catalog.ROOT
        orig_poll = run.SETTINGS.get("poll_seconds", 5)
        orig_timeout = run.SETTINGS.get("worker_timeout_seconds", 7200)
        catalog.CATALOG_PATH = cat_path
        catalog.SELECTION_PATH = sel_path
        catalog.ROOT = root
        run.SETTINGS["poll_seconds"] = 2
        run.SETTINGS["worker_timeout_seconds"] = 45

        serve_proc = subprocess.Popen(
            ["orca-ide", "serve", "--port", str(port), "--bind", "127.0.0.1", "--no-pairing", "--json"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        created_auto_ids: list[str] = []
        try:
            status_res = _wait_for_runtime(env)
            report["runtime_id"] = status_res["runtime"]["runtimeId"]
            orca = IsolatedOrca("orca-ide", env, repo)

            # Verify runtime is strictly isolated and empty
            init_autos = orca.call("automations", "list").get("automations", [])
            init_bots = orca.call("bot", "list").get("bots", [])
            init_agents = orca.call("agent", "list").get("agents", [])
            assert init_autos == [] and init_bots == [] and init_agents == [], (
                f"Expected empty isolated runtime; got autos={len(init_autos)} bots={len(init_bots)} agents={len(init_agents)}"
            )
            report["scenarios"]["isolation_verified"] = {
                "ok": True,
                "initial_automations": 0,
                "initial_bots": 0,
                "initial_agents": 0,
            }

            # 1. Real bounded inference probes (positive + fail-closed negative)
            adapter = recovery.OrcaRecoveryAdapter(orca=orca, bots_dir=root / "bots")
            codex_probe = adapter.probe_health(
                {"name": "codex:gpt-6-luna", "agent": "codex", "model": "gpt-6-luna"}, timeout_seconds=30
            )
            agy_probe = adapter.probe_health(
                {
                    "name": "antigravity:gemini-4-hydrogen-high",
                    "agent": "antigravity",
                    "model": "gemini-4-hydrogen-high",
                },
                timeout_seconds=30,
            )
            bad_probe = adapter.probe_health(
                {
                    "name": "codex:nonexistent-e2e-model-xyz",
                    "agent": "codex",
                    "model": "nonexistent-e2e-model-xyz",
                },
                timeout_seconds=20,
            )
            assert codex_probe["ok"] and codex_probe["exact_ok"], codex_probe
            assert agy_probe["ok"] and agy_probe["exact_ok"], agy_probe
            assert not bad_probe["ok"], bad_probe
            report["scenarios"]["inference_probes"] = {
                "codex_gpt6_luna": codex_probe,
                "agy_hydrogen": agy_probe,
                "bad_model_fail_closed": bad_probe,
            }

            # 2. Register isolated repo & exercise shared dispatch.py -> run.py worker lifecycle
            orca.call("repo", "add", "--path", str(repo))
            spec_ok = root / "spec_ok.md"
            spec_ok.write_text(
                "Read README.md and then immediately run the orchestration worker-done / send command from your prompt with summary OK, and exit.",
                encoding="utf-8",
            )
            # Clear nonexistent model via cooldown first so dispatch runs gpt-6-luna directly
            run.update_cooldown(state_dir / "cooldowns.json", "codex:nonexistent-e2e-model-xyz", 120)
            disp_res = dispatch.dispatch_task(
                worktree=repo,
                spec_file=spec_ok,
                title="E2E Shared Dispatch Smoke",
                role="implementation",
                lane="economy",
                caller_kind="automation",
                orca=orca,
                state_dir=state_dir,
            )
            assert disp_res["status"] == "succeeded", disp_res
            assert disp_res["actual_provider"] == "codex:gpt-6-luna", disp_res
            report["scenarios"]["shared_dispatch_worker"] = {
                "ok": True,
                "status": disp_res["status"],
                "actual_provider": disp_res["actual_provider"],
                "task_id": disp_res["original_ids"]["task_id"],
                "dispatch_id": disp_res["original_ids"]["dispatch_id"],
            }

            # 3. Exercise real automation lifecycle: failing run -> dry-run -> pause & worker recovery (keep disabled)
            # Reset cooldown so failing model is tried/recorded properly
            run.update_cooldown(state_dir / "cooldowns.json", "codex:nonexistent-e2e-model-xyz", 0)
            bad_auto = recovery._unwrap_automation(
                orca.call(
                    "automations",
                    "create",
                    "--name",
                    "E2E Failing Schedule",
                    "--trigger",
                    "daily",
                    "--prompt",
                    "Read README.md and report worker_done OK.",
                    "--provider",
                    "codex",
                    "--extra-agent-args=--model nonexistent-e2e-model-xyz",
                    "--workspace",
                    f"path:{repo}",
                    "--enabled",
                )
            )
            bad_auto_id = bad_auto["id"]
            created_auto_ids.append(bad_auto_id)
            run_resp = orca.call("automations", "run", bad_auto_id)
            term = _wait_for_automation_terminal_run(orca, bad_auto_id)
            bad_run_id = term["run"]["id"]
            assert term["run"]["status"] == "completed" and recovery._run_output_has_provider_failure(term["run"]), term

            sup_default = recovery.RecoverySupervisor(
                adapter=adapter,
                state_dir=state_dir,
                settings={"auto_resume_automation": False},
            )
            dry_rec = sup_default.recover(
                kind="automation",
                role="implementation",
                lane="economy",
                automation_id=bad_auto_id,
                run_id=bad_run_id,
                spec_file=spec_ok,
                execute=False,
            )
            assert dry_rec["status"] == "dry_run_planned", dry_rec
            assert adapter.automations_show(bad_auto_id)["enabled"] is True

            live_rec = sup_default.recover(
                kind="automation",
                role="implementation",
                lane="economy",
                automation_id=bad_auto_id,
                run_id=bad_run_id,
                spec_file=spec_ok,
                execute=True,
            )
            assert live_rec["status"] == "recovered", live_rec
            assert live_rec["actual_provider"] == "codex:gpt-6-luna", live_rec
            assert adapter.automations_show(bad_auto_id)["enabled"] is False

            # Deduplication check against canonical identity
            dup_rec = sup_default.recover(
                kind="automation",
                role="implementation",
                lane="economy",
                automation_id=bad_auto_id,
                idempotency_key="e2e-alias-check",
                spec_file=spec_ok,
                execute=True,
            )
            assert dup_rec["deduplicated"] is True and dup_rec["recovery_id"] == live_rec["recovery_id"]
            report["scenarios"]["automation_pause_and_keep_disabled"] = {
                "ok": True,
                "original_automation_id": bad_auto_id,
                "seen_run_statuses": term["seen_statuses"],
                "recovery_id": live_rec["recovery_id"],
                "actual_provider": live_rec["actual_provider"],
                "original_kept_disabled": True,
                "deduplicated": True,
            }

            # 4. Exercise real automation async accept -> waiting_for_run -> reconcile -> opt-in auto-resume & operator race & agent show/read
            good_auto = recovery._unwrap_automation(
                orca.call(
                    "automations",
                    "create",
                    "--name",
                    "E2E Recovery Target",
                    "--trigger",
                    "daily",
                    "--prompt",
                    "Reply with exact word OK and exit.",
                    "--provider",
                    "codex",
                    "--extra-agent-args=--model gpt-6-luna",
                    "--workspace",
                    f"path:{repo}",
                    "--disabled",
                )
            )
            good_auto_id = good_auto["id"]
            created_auto_ids.append(good_auto_id)

            # Re-enable bad_auto_id to simulate a fresh failure under opt-in auto_resume_automation=True
            orca.call("automations", "edit", bad_auto_id, "--enabled")
            sup_optin = recovery.RecoverySupervisor(
                adapter=adapter,
                state_dir=state_dir,
                settings={"auto_resume_automation": True},
            )
            rec_record = {
                "recovery_id": "e2e000000001",
                "idempotency_key": "canonical:e2e:reconcile",
                "mode": "execute",
                "target_kind": "automation",
                "status": "in_progress",
                "role": "implementation",
                "lane": "economy",
                "actual_provider": "codex:gpt-6-luna",
                "original_enabled": True,
                "pause_owned": False,
                "original_ids": {"automation_id": bad_auto_id},
                "verification_evidence": {},
                "mutations": [],
                "external_action_receipts": [],
                "history": [],
            }
            paused_ok = sup_optin._pause_and_verify_automation(rec_record, bad_auto_id)
            assert paused_ok
            accepted = orca.call("automations", "run", good_auto_id)
            accepted_run_id = accepted["runId"]
            rec_record["recovery_automation_id"] = good_auto_id
            rec_record["recovery_run_id"] = accepted_run_id
            rec_record["status"] = "waiting_for_run"
            sup_optin._persist(rec_record, claim_idempotency=True)

            term_good = _wait_for_automation_terminal_run(orca, good_auto_id)
            reconciled = sup_optin.reconcile("e2e000000001", execute=True)
            assert reconciled["status"] == "recovered", reconciled
            assert adapter.automations_show(bad_auto_id)["enabled"] is True

            # Verify operator-edit race: if operator changes schedule while paused, reconcile does NOT re-enable
            paused_race_rec = {
                "recovery_id": "e2e000000002",
                "idempotency_key": "canonical:e2e:operator_race",
                "mode": "execute",
                "target_kind": "automation",
                "status": "in_progress",
                "role": "implementation",
                "lane": "economy",
                "actual_provider": "codex:gpt-6-luna",
                "original_enabled": True,
                "pause_owned": False,
                "original_ids": {"automation_id": bad_auto_id},
                "verification_evidence": {},
                "mutations": [],
                "external_action_receipts": [],
                "history": [],
            }
            assert sup_optin._pause_and_verify_automation(paused_race_rec, bad_auto_id)
            # Operator independently changes schedule time -> updates updatedAt & rrule
            orca.call("automations", "edit", bad_auto_id, "--trigger", "daily", "--time", "11:30")
            paused_race_rec["recovery_automation_id"] = good_auto_id
            paused_race_rec["recovery_run_id"] = accepted_run_id
            paused_race_rec["status"] = "waiting_for_run"
            sup_optin._persist(paused_race_rec, claim_idempotency=True)
            race_reconciled = sup_optin.reconcile("e2e000000002", execute=True)
            assert race_reconciled["status"] == "recovered", race_reconciled
            assert adapter.automations_show(bad_auto_id)["enabled"] is False

            # Verify real agent show/read schemas on the finished automation's terminalSessionId
            agents = orca.call("agent", "list").get("agents", [])
            done_agent = next((a for a in agents if a.get("state") == "done"), None)
            agent_show_res = adapter.agent_show(done_agent["orcaSessionId"]) if done_agent else {}
            agent_read_res = adapter.agent_read(done_agent["orcaSessionId"], limit=5) if done_agent else {}
            assert recovery._extract_agent_show_states(agent_show_res) == ["done"], agent_show_res
            assert agent_read_res.get("state") == "done", agent_read_res

            report["scenarios"]["reconcile_and_optin_resume_and_agent_schemas"] = {
                "ok": True,
                "accepted_run_id": accepted_run_id,
                "seen_run_statuses": term_good["seen_statuses"],
                "reconciled_status": reconciled["status"],
                "optin_resumed_original": True,
                "operator_edit_race_kept_disabled": True,
                "agent_show_states": recovery._extract_agent_show_states(agent_show_res),
                "agent_read_state": agent_read_res.get("state"),
            }

            # 5. Task-failure rejection, delayed resume with real probe, and ambiguous launch blocking
            task_fail_auto = recovery._unwrap_automation(
                orca.call(
                    "automations",
                    "create",
                    "--name",
                    "E2E Task Done Check",
                    "--trigger",
                    "daily",
                    "--prompt",
                    "Reply with exact word OK and exit.",
                    "--provider",
                    "codex",
                    "--extra-agent-args=--model gpt-6-luna",
                    "--workspace",
                    f"path:{repo}",
                    "--enabled",
                )
            )
            task_fail_id = task_fail_auto["id"]
            created_auto_ids.append(task_fail_id)
            orca.call("automations", "run", task_fail_id)
            _wait_for_automation_terminal_run(orca, task_fail_id)
            # A healthy completed run without provider error must NOT trigger recovery (settled=False -> blocked)
            non_fail_res = sup_default.recover(
                kind="automation",
                role="implementation",
                lane="economy",
                automation_id=task_fail_id,
                execute=True,
            )
            assert non_fail_res["status"] == "blocked" and adapter.automations_show(task_fail_id)["enabled"] is True

            # Delayed resume using real codex:gpt-6-luna inference probe & no lane escalation
            bad_auto_2 = recovery._unwrap_automation(
                orca.call(
                    "automations",
                    "create",
                    "--name",
                    "E2E Delayed Failing Schedule",
                    "--trigger",
                    "daily",
                    "--prompt",
                    "Read README.md and report worker_done OK.",
                    "--provider",
                    "codex",
                    "--extra-agent-args=--model nonexistent-e2e-model-xyz",
                    "--workspace",
                    f"path:{repo}",
                    "--enabled",
                )
            )
            bad_auto_2_id = bad_auto_2["id"]
            created_auto_ids.append(bad_auto_2_id)
            orca.call("automations", "run", bad_auto_2_id)
            term_bad_2 = _wait_for_automation_terminal_run(orca, bad_auto_2_id)
            bad_run_2_id = term_bad_2["run"]["id"]

            run.update_cooldown(state_dir / "cooldowns.json", "codex:gpt-6-luna", 60)
            delayed_rec = sup_default.recover(
                kind="automation",
                role="implementation",
                lane="economy",
                automation_id=bad_auto_2_id,
                run_id=bad_run_2_id,
                idempotency_key="e2e-delayed-real-probe",
                spec_file=spec_ok,
                execute=True,
            )
            assert delayed_rec["status"] == "exhausted_delayed_resume", delayed_rec
            assert delayed_rec["escalation_required"] is True
            # Advance past cooldown and run resume_after_probe -> real codex:gpt-6-luna OK probe -> recovered
            resumed_delayed = sup_default.resume_after_probe(
                delayed_rec["recovery_id"],
                execute=True,
                now_ts=time.time() + 1800,
            )
            assert resumed_delayed["status"] == "recovered", resumed_delayed
            assert resumed_delayed["actual_provider"] == "codex:gpt-6-luna"
            assert adapter.automations_show(bad_auto_2_id)["enabled"] is False

            # Ambiguous launch blocking on automations_run timeout
            amb_rec = {
                "recovery_id": "e2e000000003",
                "idempotency_key": "canonical:e2e:ambiguous",
                "mode": "execute",
                "target_kind": "bot_chat",
                "status": "in_progress",
                "role": "implementation",
                "lane": "economy",
                "actual_provider": "codex:gpt-6-luna",
                "attempted_providers": [],
                "original_ids": {"bot": "e2e-bot", "chat_id": "c1"},
                "verification_evidence": {},
                "mutations": [],
                "external_action_receipts": [],
                "history": [],
            }
            orig_create = adapter.create_one_time_bot_automation
            orig_run = adapter.automations_run
            adapter.create_one_time_bot_automation = lambda **kw: {"id": good_auto_id, "enabled": False}  # type: ignore[method-assign]
            adapter.automations_run = lambda _aid: (_ for _ in ()).throw(TimeoutError("simulated accept timeout"))  # type: ignore[method-assign]
            try:
                amb_out = sup_default._launch_disabled_bot_recovery_run(
                    record=amb_rec,
                    name="E2E Ambiguous",
                    bot="e2e-bot",
                    provider={"name": "codex:gpt-6-luna", "agent": "codex", "model": "gpt-6-luna"},
                    prompt="OK",
                    by_iso="2026-11-25T18:00:00Z",
                    item_id=None,
                    results_to=None,
                )
            finally:
                adapter.create_one_time_bot_automation = orig_create  # type: ignore[method-assign]
                adapter.automations_run = orig_run  # type: ignore[method-assign]
            assert amb_out["status"] == "blocked"
            assert amb_out["block_reason"] == "ambiguous_launch_reconcile_required"
            amb_reconciled = sup_default.reconcile("e2e000000003", execute=True)
            assert amb_reconciled["status"] == "recovered"
            assert "block_reason" not in amb_reconciled

            report["scenarios"]["delayed_probe_and_ambiguous_launch"] = {
                "ok": True,
                "healthy_auto_blocked": True,
                "delayed_resume_status": resumed_delayed["status"],
                "delayed_resume_provider": resumed_delayed["actual_provider"],
                "ambiguous_launch_blocked_then_reconciled": True,
            }

            # 6. Headless bot-creation limitation check (honest gate check)
            bots_after = orca.call("bot", "list").get("bots", [])
            report["scenarios"]["headless_bot_creation_gate"] = {
                "supported_without_owner_ui": False,
                "bots_in_isolated_runtime": len(bots_after),
                "note": "orca-ide bot create requires owner UI card acceptance (--offer); mutating bot-chat E2E cannot run headlessly without touching production bots.",
            }

            # Cleanup automations inside isolated runtime before exit
            for aid in created_auto_ids:
                try:
                    orca.call("automations", "remove", aid)
                except Exception:
                    pass
            report["cleanup"]["removed_automations"] = created_auto_ids
        finally:
            catalog.CATALOG_PATH = orig_cat
            catalog.SELECTION_PATH = orig_sel
            catalog.ROOT = orig_root
            run.SETTINGS["poll_seconds"] = orig_poll
            run.SETTINGS["worker_timeout_seconds"] = orig_timeout
            serve_proc.terminate()
            try:
                serve_proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                serve_proc.kill()
                serve_proc.communicate()
            report["cleanup"]["serve_stopped"] = True
            report["cleanup"]["temp_dir_removed_on_exit"] = str(root)

    return report


def main() -> int:
    try:
        result = run_isolated_e2e()
    except Exception as error:
        print(run.redact_text(str(error)), file=sys.stderr)
        return 1
    print(json.dumps(run.redact_secrets(result), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
