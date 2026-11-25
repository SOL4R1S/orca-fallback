import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import catalog
import dispatch
import mcp_dispatch
import recovery
import run
import wiring
from tests.test_recovery import FakeAdapter, FakeOrca


class ExtendedFakeOrca(FakeOrca):
    """Extends FakeOrca with bot tools and automation prompt editing for wiring tests."""

    def __init__(self):
        super().__init__()
        self.bot_tools_by_bot: dict[str, list[dict]] = {}

    def call(self, *args: str, cwd: Path | None = None, timeout: int = 90) -> dict:
        cmd2 = args[:2]
        cmd3 = args[:3]
        if cmd3 == ("bot", "tools", "add"):
            self.calls.append(args)
            source = args[3]
            name = args[args.index("--name") + 1] if "--name" in args else "tool"
            bot = args[args.index("--bot") + 1]
            tools = self.bot_tools_by_bot.setdefault(bot, [])
            entry = {"name": name, "detail": source, "remote": False}
            tools.append(entry)
            return {"tool": entry}
        if cmd3 == ("bot", "tools", "rm"):
            self.calls.append(args)
            name = args[3]
            bot = args[args.index("--bot") + 1]
            tools = self.bot_tools_by_bot.get(bot, [])
            self.bot_tools_by_bot[bot] = [t for t in tools if t.get("name") != name]
            return {"removed": name}
        if cmd2 == ("bot", "tools"):
            self.calls.append(args)
            bot = args[args.index("--bot") + 1]
            return {"agent": "codex", "own": list(self.bot_tools_by_bot.get(bot, [])), "inherited": [], "problems": {}}
        if cmd2 == ("automations", "edit"):
            auto_id = args[2]
            res = super().call(*args, cwd=cwd, timeout=timeout)
            if "--prompt" in args:
                self.automations[auto_id]["prompt"] = args[args.index("--prompt") + 1]
            return res
        return super().call(*args, cwd=cwd, timeout=timeout)


class WiringAndSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_dir = self.root / "state"
        self.worktree = self.root / "repo"
        self.worktree.mkdir()
        self.spec_file = self.root / "task.md"
        self.spec_file.write_text("Verify bounded dispatch recovery and wiring.\n", encoding="utf-8")

        self.bots_dir = self.root / "bots"
        atlas_dir = self.bots_dir / "atlas-orchestrator"
        atlas_dir.mkdir(parents=True)
        (atlas_dir / "bot.json").write_text(
            json.dumps({"id": "atlas-orchestrator", "name": "Atlas · Orchestrator"}),
            encoding="utf-8",
        )
        (atlas_dir / "SOUL.md").write_text("# Atlas\nKeep role files short.\n", encoding="utf-8")
        (atlas_dir / "AGENTS.md").write_text("# Orca Managed\n", encoding="utf-8")

        self.catalog_path = self.root / "catalog.json"
        self.selection_path = self.root / "selection.json"
        self.benchmarks_path = self.root / "benchmarks.json"
        self.catalog_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "models": [
                        {
                            "key": "codex:gpt-6.2-sol",
                            "agent": "codex",
                            "model": "gpt-6.2-sol",
                            "provider": "openai",
                            "available": True,
                            "pricing": {"cost4": {"input": 2.0, "output": 8.0}},
                        },
                        {
                            "key": "antigravity:gemini-4-hydrogen-high",
                            "agent": "antigravity",
                            "model": "gemini-4-hydrogen-high",
                            "provider": "antigravity",
                            "available": True,
                            "pricing": {"cost4": {"input": 2.0, "output": 8.0}},
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
                            "key": "codex:command-code/z-ai/glm-5.3-flash",
                            "agent": "codex",
                            "model": "command-code/z-ai/glm-5.3-flash",
                            "provider": "command-code",
                            "available": True,
                            "pricing": {"cost4": {"input": 0.1, "output": 0.4}},
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.selection_path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "roles": {
                        "implementation": {
                            "default_lane": "premium",
                            "allow_lane_escalation": False,
                            "lanes": {
                                "premium": {
                                    "budget": {"max_comparison_price_usd": 8, "min_intelligence_index": 40},
                                    "models": [
                                        {"key": "codex:gpt-6.2-sol", "effort": "high"},
                                        {"key": "antigravity:gemini-4-hydrogen-high"},
                                    ],
                                },
                                "economy": {
                                    "budget": {"max_comparison_price_usd": 1, "min_intelligence_index": 20},
                                    "models": [
                                        {"key": "codex:gpt-6-luna", "effort": "low"},
                                        {"key": "codex:command-code/z-ai/glm-5.3-flash", "effort": "low"},
                                    ],
                                },
                            },
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        self.benchmarks_path.write_text(
            json.dumps(
                {
                    "entries": [
                        {"model_key": "codex:gpt-6.2-sol", "benchmark": "artificial_analysis.intelligence_index", "score": 65},
                        {"model_key": "antigravity:gemini-4-hydrogen-high", "benchmark": "artificial_analysis.intelligence_index", "score": 62},
                        {"model_key": "codex:gpt-6-luna", "benchmark": "artificial_analysis.intelligence_index", "score": 30},
                        {"model_key": "codex:command-code/z-ai/glm-5.3-flash", "benchmark": "artificial_analysis.intelligence_index", "score": 25},
                    ]
                }
            ),
            encoding="utf-8",
        )

        self.patchers = [
            mock.patch.object(catalog, "CATALOG_PATH", self.catalog_path),
            mock.patch.object(catalog, "SELECTION_PATH", self.selection_path),
            mock.patch.object(catalog, "ROOT", self.root),
            mock.patch.object(run, "STATE_DIR", self.state_dir),
        ]
        for patcher in self.patchers:
            patcher.start()

        self.fake_orca = ExtendedFakeOrca()
        self.adapter = FakeAdapter(self.fake_orca, self.bots_dir)
        self.supervisor = recovery.RecoverySupervisor(adapter=self.adapter, state_dir=self.state_dir)

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tmp.cleanup()

    def test_dispatch_all_cooldown_skipped_creates_durable_recovery_and_honors_health_after_cooldown(self):
        # Put all economy models in cooldown (preferred luna=60s, fallback glm=120s)
        run.update_cooldown(self.state_dir / "cooldowns.json", "codex:gpt-6-luna", 60)
        run.update_cooldown(self.state_dir / "cooldowns.json", "codex:command-code/z-ai/glm-5.3-flash", 120)

        disp = dispatch.dispatch_task(
            worktree=self.worktree,
            spec_file=self.spec_file,
            title="Cooldown skipped dispatch",
            role="implementation",
            lane="economy",
            caller_kind="bot",
            bot="atlas-orchestrator",
            orca=self.fake_orca,
            state_dir=self.state_dir,
        )
        self.assertEqual(disp["status"], "lane_exhausted")
        self.assertIsNone(disp["actual_provider"])
        self.assertTrue(disp.get("recovery_id"))
        self.assertEqual(disp.get("recovery_status"), "exhausted_delayed_resume")
        self.assertFalse(any(c[:2] == ("orchestration", "worker-start") for c in self.fake_orca.calls))

        rec = self.supervisor.load_record(disp["recovery_id"])
        self.assertEqual(rec["target_kind"], "dispatch")
        self.assertEqual(rec["attempted_providers"], [])
        self.assertEqual(rec["delayed_resume_plan"]["title"], "Cooldown skipped dispatch")

        # Too early: supervisor skips without probing
        early = self.supervisor.supervise(execute=True, now_ts=time.time() + 10)
        self.assertEqual(early["resumed"], [])
        self.assertEqual(self.adapter.probes, [])

        # At +70s, luna is off cooldown, glm still in cooldown (~50s left).
        # Probe luna FAILS -> luna re-cooled, eligible_at advances to glm reset (~120s), not +900s!
        self.fake_orca.probe_results.append(
            {"ok": False, "probe_type": "inference", "agent": "codex", "model": "gpt-6-luna", "exact_ok": False}
        )
        tick1 = self.supervisor.supervise(execute=True, now_ts=time.time() + 70)
        self.assertEqual(len(tick1["resumed"]), 1)
        self.assertEqual(tick1["resumed"][0]["status"], "probe_failed")
        self.assertEqual(self.adapter.probes, ["codex:gpt-6-luna"])

        # At +130s, glm is off cooldown -> probe PASSES -> dispatches ONLY glm -> recovered!
        self.fake_orca.worker_start_queue.append({"taskId": "t_res", "dispatchId": "d_res"})
        self.fake_orca.worker_shows["d_res"] = {"state": "succeeded", "stage": "settled", "liveness": "exited"}
        tick2 = self.supervisor.supervise(execute=True, now_ts=time.time() + 130)
        self.assertEqual(len(tick2["resumed"]), 1)
        self.assertEqual(tick2["resumed"][0]["status"], "recovered")
        self.assertEqual(tick2["resumed"][0]["actual_provider"], "codex:command-code/z-ai/glm-5.3-flash")
        self.assertEqual(self.adapter.probes, ["codex:gpt-6-luna", "codex:command-code/z-ai/glm-5.3-flash"])

        # Subsequent supervisor ticks do nothing (no duplicate replay)
        tick3 = self.supervisor.supervise(execute=True, now_ts=time.time() + 200)
        self.assertEqual(tick3["registered_dispatches"], [])
        self.assertEqual(tick3["resumed"], [])

        log = (self.state_dir / "operations.txt").read_text(encoding="utf-8")
        for expected in (
            "dispatch_started", "dispatch_finished", "lane_exhausted", "probe_failed", "recovered",
            disp["handoff_id"], disp["recovery_id"],
        ):
            self.assertIn(expected, log)
        self.assertNotIn(self.spec_file.read_text(encoding="utf-8").strip(), log)
        self.assertNotIn("Cooldown skipped dispatch", log)

    def test_operations_log_rotates_and_redacts(self):
        with mock.patch.object(run, "OPERATIONS_LOG_MAX_BYTES", 140):
            run.log_operation(self.state_dir, "first", api_key="synthetic-secret")
            run.log_operation(self.state_dir, "second", status="recovered")
            run.log_operation(self.state_dir, "third", status="succeeded")
        current = self.state_dir / "operations.txt"
        previous = self.state_dir / "operations.previous.txt"
        self.assertIn("third", current.read_text(encoding="utf-8"))
        old_log = previous.read_text(encoding="utf-8")
        self.assertIn("first", old_log)
        self.assertIn("[REDACTED]", old_log)
        self.assertNotIn("synthetic-secret", old_log)
        self.assertEqual(current.stat().st_mode & 0o777, 0o600)

    def test_dispatch_all_attempted_failed_and_task_failure_never_escalate_or_replay(self):
        # 1a) All models hit rate_limit/usage_limit BEFORE work -> delayed resume permits ONE post-cooldown attempt -> succeeds
        self.fake_orca.worker_start_queue.extend(
            [{"taskId": "t1", "dispatchId": "d1"}, {"taskId": "t1", "dispatchId": "d2"}]
        )
        self.fake_orca.worker_shows["d1"] = {"state": "failed", "stage": "settled", "liveness": "exited"}
        self.fake_orca.worker_reads["d1"] = {"transcript": {"messages": [{"role": "assistant", "text": "rate_limit"}]}}
        self.fake_orca.worker_shows["d2"] = {"state": "failed", "stage": "settled", "liveness": "exited"}
        self.fake_orca.worker_reads["d2"] = {"transcript": {"messages": [{"role": "assistant", "text": "usage_limit"}]}}

        with mock.patch.dict(run.SETTINGS, {"poll_seconds": 0}):
            disp = dispatch.dispatch_task(
                worktree=self.worktree,
                spec_file=self.spec_file,
                title="All rate limited pre-work",
                role="implementation",
                lane="economy",
                orca=self.fake_orca,
                state_dir=self.state_dir,
            )
        self.assertEqual(disp["status"], "lane_exhausted")
        self.assertEqual(disp["recovery_status"], "exhausted_delayed_resume")
        rec = self.supervisor.load_record(disp["recovery_id"])
        self.assertTrue(rec["all_pre_work_safe"])
        self.assertEqual(rec["post_cooldown_attempts"], 0)

        # Too early: skipped
        early = self.supervisor.supervise(execute=True, now_ts=time.time() + 10)
        self.assertEqual(early["resumed"], [])

        # After cooldown: exact healthy probe in same lane -> 1 post-cooldown attempt -> recovered
        self.fake_orca.worker_start_queue.append({"taskId": "t1_post", "dispatchId": "d1_post"})
        self.fake_orca.worker_shows["d1_post"] = {"state": "succeeded", "stage": "settled", "liveness": "exited"}
        resumed = self.supervisor.supervise(execute=True, now_ts=time.time() + 1800)
        self.assertEqual(len(resumed["resumed"]), 1)
        self.assertEqual(resumed["resumed"][0]["status"], "recovered")
        self.assertEqual(resumed["resumed"][0]["actual_provider"], "codex:gpt-6-luna")

        # 1b) Second post-cooldown attempt is NEVER allowed: if that 1 attempt fails -> explicit blocked state
        run.update_cooldown(self.state_dir / "cooldowns.json", "codex:gpt-6-luna", 0)
        run.update_cooldown(self.state_dir / "cooldowns.json", "codex:command-code/z-ai/glm-5.3-flash", 0)
        self.fake_orca.worker_start_queue.extend(
            [
                {"taskId": "t1b", "dispatchId": "d1b"},
                {"taskId": "t1b", "dispatchId": "d2b"},
                {"taskId": "t1b_post", "dispatchId": "d3b_post"},
            ]
        )
        for did in ("d1b", "d2b", "d3b_post"):
            self.fake_orca.worker_shows[did] = {"state": "failed", "stage": "settled", "liveness": "exited"}
            self.fake_orca.worker_reads[did] = {"transcript": {"messages": [{"role": "assistant", "text": "rate_limit"}]}}

        with mock.patch.dict(run.SETTINGS, {"poll_seconds": 0}):
            disp_b = dispatch.dispatch_task(
                worktree=self.worktree,
                spec_file=self.spec_file,
                title="Single post-cooldown bound",
                role="implementation",
                lane="economy",
                orca=self.fake_orca,
                state_dir=self.state_dir,
            )
            post_fail = self.supervisor.supervise(execute=True, now_ts=time.time() + 1800)
        self.assertEqual(post_fail["resumed"][0]["status"], "blocked")
        rec_b = self.supervisor.load_record(disp_b["recovery_id"])
        self.assertEqual(rec_b["block_reason"], "post_cooldown_attempt_exhausted_user_decision_required")
        self.assertTrue(rec_b["user_decision_required"])
        self.assertTrue(rec_b["escalation_required"])
        # No further retries
        again = self.supervisor.supervise(execute=True, now_ts=time.time() + 7200)
        self.assertEqual(again["resumed"], [])

        # 1c) Explicit skip_providers exclusion is preserved across resume (luna excluded, glm retried once)
        run.update_cooldown(self.state_dir / "cooldowns.json", "codex:gpt-6-luna", 0)
        run.update_cooldown(self.state_dir / "cooldowns.json", "codex:command-code/z-ai/glm-5.3-flash", 0)
        self.adapter.probes.clear()
        self.fake_orca.worker_start_queue.append({"taskId": "t_ex", "dispatchId": "d_ex"})
        self.fake_orca.worker_shows["d_ex"] = {"state": "failed", "stage": "settled", "liveness": "exited"}
        self.fake_orca.worker_reads["d_ex"] = {"transcript": {"messages": [{"role": "assistant", "text": "rate_limit"}]}}
        with mock.patch.dict(run.SETTINGS, {"poll_seconds": 0}):
            disp_ex = dispatch.dispatch_task(
                worktree=self.worktree,
                spec_file=self.spec_file,
                title="Preserve skip_providers",
                role="implementation",
                lane="economy",
                skip_providers={"codex:gpt-6-luna"},
                orca=self.fake_orca,
                state_dir=self.state_dir,
            )
        self.assertEqual(disp_ex["recovery_status"], "exhausted_delayed_resume")
        self.fake_orca.worker_start_queue.append({"taskId": "t_ex2", "dispatchId": "d_ex2"})
        self.fake_orca.worker_shows["d_ex2"] = {"state": "succeeded", "stage": "settled", "liveness": "exited"}
        res_ex = self.supervisor.supervise(execute=True, now_ts=time.time() + 1800)
        self.assertEqual(res_ex["resumed"][0]["status"], "recovered")
        self.assertEqual(res_ex["resumed"][0]["actual_provider"], "codex:command-code/z-ai/glm-5.3-flash")
        self.assertEqual(self.adapter.probes, ["codex:command-code/z-ai/glm-5.3-flash"])

        # 1d) Side-effecting job (tool activity observed before rate_limit) -> NEVER auto-retried, immediately blocked
        run.update_cooldown(self.state_dir / "cooldowns.json", "codex:gpt-6-luna", 0)
        run.update_cooldown(self.state_dir / "cooldowns.json", "codex:command-code/z-ai/glm-5.3-flash", 0)
        self.fake_orca.worker_start_queue.extend(
            [{"taskId": "tse", "dispatchId": "dse1"}, {"taskId": "tse", "dispatchId": "dse2"}]
        )
        self.fake_orca.worker_shows["dse1"] = {"state": "failed", "stage": "settled", "liveness": "exited"}
        self.fake_orca.worker_reads["dse1"] = {
            "transcript": {
                "messages": [
                    {"role": "user", "text": "Edit file"},
                    {"role": "tool", "text": "wrote src/app.py"},
                    {"role": "assistant", "text": "rate_limit"},
                ]
            }
        }
        self.fake_orca.worker_shows["dse2"] = {"state": "failed", "stage": "settled", "liveness": "exited"}
        self.fake_orca.worker_reads["dse2"] = {"transcript": {"messages": [{"role": "assistant", "text": "rate_limit"}]}}
        with mock.patch.dict(run.SETTINGS, {"poll_seconds": 0}):
            disp_se = dispatch.dispatch_task(
                worktree=self.worktree,
                spec_file=self.spec_file,
                title="Side effecting job",
                role="implementation",
                lane="economy",
                orca=self.fake_orca,
                state_dir=self.state_dir,
            )
        self.assertEqual(disp_se["recovery_status"], "blocked")
        rec_se = self.supervisor.load_record(disp_se["recovery_id"])
        self.assertEqual(rec_se["block_reason"], "prior_side_effects_not_proven_absent_user_decision_required")
        self.assertTrue(rec_se["user_decision_required"])

        # 1e) Credentials / model-not-found failures -> NO auto-retries, immediately blocked
        run.update_cooldown(self.state_dir / "cooldowns.json", "codex:gpt-6-luna", 0)
        run.update_cooldown(self.state_dir / "cooldowns.json", "codex:command-code/z-ai/glm-5.3-flash", 0)
        self.fake_orca.worker_start_queue.extend(
            [{"taskId": "tcred", "dispatchId": "dc1"}, {"taskId": "tcred", "dispatchId": "dc2"}]
        )
        self.fake_orca.worker_shows["dc1"] = {"state": "failed", "stage": "settled", "liveness": "exited"}
        self.fake_orca.worker_reads["dc1"] = {"transcript": {"messages": [{"role": "assistant", "text": "invalid_api_key"}]}}
        self.fake_orca.worker_shows["dc2"] = {"state": "failed", "stage": "settled", "liveness": "exited"}
        self.fake_orca.worker_reads["dc2"] = {"transcript": {"messages": [{"role": "assistant", "text": "model_not_found"}]}}
        with mock.patch.dict(run.SETTINGS, {"poll_seconds": 0}):
            disp_cred = dispatch.dispatch_task(
                worktree=self.worktree,
                spec_file=self.spec_file,
                title="Non-retryable credentials/model",
                role="implementation",
                lane="economy",
                orca=self.fake_orca,
                state_dir=self.state_dir,
            )
        self.assertEqual(disp_cred["recovery_status"], "blocked")
        rec_cred = self.supervisor.load_record(disp_cred["recovery_id"])
        self.assertEqual(rec_cred["block_reason"], "non_retryable_provider_failure_user_decision_required")
        self.assertTrue(rec_cred["user_decision_required"])

        # 2) Task/code failure -> NO recovery record, NO replay by supervise
        run.update_cooldown(self.state_dir / "cooldowns.json", "codex:gpt-6-luna", 0)
        run.update_cooldown(self.state_dir / "cooldowns.json", "codex:command-code/z-ai/glm-5.3-flash", 0)
        self.fake_orca.worker_start_queue.append({"taskId": "t2", "dispatchId": "d3"})
        self.fake_orca.worker_shows["d3"] = {
            "state": "failed",
            "stage": "settled",
            "liveness": "exited",
            "errorCode": "test_failed",
        }
        self.fake_orca.worker_reads["d3"] = {
            "transcript": {"messages": [{"role": "assistant", "text": "AssertionError: 1 != 2"}]}
        }
        with mock.patch.dict(run.SETTINGS, {"poll_seconds": 0}):
            task_fail = dispatch.dispatch_task(
                worktree=self.worktree,
                spec_file=self.spec_file,
                title="Test failure",
                role="implementation",
                lane="economy",
                orca=self.fake_orca,
                state_dir=self.state_dir,
            )
        self.assertEqual(task_fail["status"], "failed_non_provider")
        self.assertNotIn("recovery_id", task_fail)

        sup_out = self.supervisor.supervise(execute=True, now_ts=time.time() + 10800)
        self.assertEqual(sup_out["registered_dispatches"], [])
        self.assertEqual(sup_out["resumed"], [])

    def test_bounded_probe_retries_and_unsafe_legacy_skip_and_ambiguous_reconcile(self):
        # Bounded probe attempts: max_probe_attempts=2 -> stops in blocked after 2 failed probes
        run.update_cooldown(self.state_dir / "cooldowns.json", "codex:gpt-6-luna", 30)
        run.update_cooldown(self.state_dir / "cooldowns.json", "codex:command-code/z-ai/glm-5.3-flash", 30)
        with mock.patch.dict(run.SETTINGS, {"recovery": {"max_probe_attempts": 2, "max_resume_attempts": 3}}):
            disp = dispatch.dispatch_task(
                worktree=self.worktree,
                spec_file=self.spec_file,
                title="Bounded probe",
                role="implementation",
                lane="economy",
                orca=self.fake_orca,
                state_dir=self.state_dir,
            )
            rid = disp["recovery_id"]
            self.fake_orca.probe_results.extend(
                [
                    {"ok": False, "probe_type": "inference", "agent": "codex", "model": "gpt-6-luna", "exact_ok": False},
                    {"ok": False, "probe_type": "inference", "agent": "codex", "model": "command-code/z-ai/glm-5.3-flash", "exact_ok": False},
                ]
            )
            p1 = self.supervisor.supervise(execute=True, now_ts=time.time() + 60)
            self.assertEqual(p1["resumed"][0]["status"], "probe_failed")
            p2 = self.supervisor.supervise(execute=True, now_ts=time.time() + 120)
            self.assertEqual(p2["resumed"][0]["status"], "blocked")
            rec = self.supervisor.load_record(rid)
            self.assertEqual(rec["block_reason"], "max_probe_attempts_exceeded")
            self.assertTrue(rec["user_decision_required"])

        # Seed unsafe/legacy & full-shape historical records without schema marker -> skipped cleanly
        (self.state_dir / "recoveries" / "not-hex-id.json").write_text("{}", encoding="utf-8")
        (self.state_dir / "recoveries" / "aaaaaaaaaaaa.json").write_text("{corrupt", encoding="utf-8")
        run.write_state(
            self.state_dir / "recoveries" / "bbbbbbbbbbbb.json",
            {
                "recovery_id": "bbbbbbbbbbbb",
                "supervision_schema_version": run.SUPERVISION_SCHEMA_VERSION,
                "unattended_supervision": True,
                "idempotency_key": "k:dry",
                "mode": "dry_run",
                "status": "exhausted_delayed_resume",
            },
        )
        run.write_state(
            self.state_dir / "recoveries" / "cccccccccccc.json",
            {
                "recovery_id": "cccccccccccc",
                "supervision_schema_version": run.SUPERVISION_SCHEMA_VERSION,
                "unattended_supervision": True,
                "idempotency_key": "k:worker_uncertain",
                "mode": "execute",
                "status": "blocked",
                "block_reason": "ambiguous_launch_reconcile_required",
                "launch_intent": {"action": "run.run_job", "step": "worker_uncertain"},
            },
        )
        # Full-shape historical recovery & dispatch records lacking schema marker -> MUST be skipped!
        self.fake_orca.automations["auto_hist"] = {"id": "auto_hist", "name": "Historical", "enabled": False}
        self.fake_orca.automation_runs["auto_hist"] = [{"id": "run_hist", "status": "succeeded"}]
        run.write_state(
            self.state_dir / "recoveries" / "eeeeeeeeeeee.json",
            {
                "recovery_id": "eeeeeeeeeeee",
                "idempotency_key": "canonical:hist:waiting",
                "mode": "execute",
                "target_kind": "bot_chat",
                "status": "waiting_for_run",
                "role": "implementation",
                "lane": "economy",
                "recovery_automation_id": "auto_hist",
                "recovery_run_id": "run_hist",
                "original_ids": {"bot": "atlas-orchestrator", "chat_id": "c_hist"},
            },
        )
        run.write_state(
            self.state_dir / "dispatches" / "legacy01.json",
            {
                "handoff_id": "legacy01",
                "mode": "execute",
                "status": "lane_exhausted",
                "role": "implementation",
                "lane": "economy",
                "worktree": str(self.worktree),
                "spec_file": str(self.spec_file),
                "title": "Historical Dispatch",
                "verification_evidence": {
                    "attempts": [{"provider": "codex:gpt-6-luna", "status": "cooldown_skipped"}]
                },
            },
        )
        # Opted-out new dispatch (unattended=False) -> MUST also be skipped!
        run.update_cooldown(self.state_dir / "cooldowns.json", "codex:gpt-6-luna", 300)
        run.update_cooldown(self.state_dir / "cooldowns.json", "codex:command-code/z-ai/glm-5.3-flash", 300)
        opt_out_disp = dispatch.dispatch_task(
            worktree=self.worktree,
            spec_file=self.spec_file,
            title="Opted out dispatch",
            role="implementation",
            lane="economy",
            unattended=False,
            orca=self.fake_orca,
            state_dir=self.state_dir,
        )
        self.assertFalse(opt_out_disp["unattended_supervision"])

        # Ambiguous automation launch with remote run -> safely reconciled by supervise!
        self.fake_orca.automations["auto_amb"] = {"id": "auto_amb", "name": "Recover amb", "enabled": False}
        self.fake_orca.automation_runs["auto_amb"] = [{"id": "run_amb", "status": "succeeded"}]
        amb_rec = {
            "recovery_id": "dddddddddddd",
            "supervision_schema_version": run.SUPERVISION_SCHEMA_VERSION,
            "unattended_supervision": True,
            "idempotency_key": "canonical:amb:1",
            "mode": "execute",
            "target_kind": "bot_chat",
            "status": "blocked",
            "block_reason": "ambiguous_launch_reconcile_required",
            "role": "implementation",
            "lane": "economy",
            "recovery_automation_id": "auto_amb",
            "launch_intent": {"automation_name": "Recover amb", "step": "launch_uncertain"},
            "original_ids": {"bot": "atlas-orchestrator", "chat_id": "c1"},
            "verification_evidence": {},
            "mutations": [],
            "external_action_receipts": [],
            "history": [],
        }
        self.supervisor._persist(amb_rec, claim_idempotency=True)

        # Waiting recovery (opted-in) -> auto-reconciled by supervise once run succeeds!
        self.fake_orca.chats_by_bot["atlas-orchestrator"] = [
            {"id": "c_wait", "lastAsked": "Verify waiting reconcile", "lastActivityAt": "2026-11-25T08:00:00Z"}
        ]
        self.fake_orca.agent_shows["c_wait"] = {"id": "orca_session_id:c_wait", "state": "done"}
        self.fake_orca.agent_reads["c_wait"] = {
            "id": "orca_session_id:c_wait",
            "state": "done",
            "messages": [{"role": "user", "text": "Verify"}, {"role": "assistant", "text": "rate_limit"}],
        }
        run.update_cooldown(self.state_dir / "cooldowns.json", "codex:gpt-6-luna", 0)
        run.update_cooldown(self.state_dir / "cooldowns.json", "codex:command-code/z-ai/glm-5.3-flash", 0)
        wait_rec = self.supervisor.recover(
            kind="bot_chat",
            role="implementation",
            lane="economy",
            bot="atlas-orchestrator",
            chat_id="c_wait",
            failed_provider="codex:gpt-6-luna",
            execute=True,
        )
        self.assertEqual(wait_rec["status"], "waiting_for_run")
        self.fake_orca.automation_runs[wait_rec["recovery_automation_id"]] = [
            {"id": wait_rec["recovery_run_id"], "status": "running"}
        ]
        sup_still_waiting = self.supervisor.supervise(execute=True, now_ts=time.time() + 180)
        self.assertIn({"recovery_id": "dddddddddddd", "status": "recovered"}, sup_still_waiting["reconciled"])
        self.assertIn({"recovery_id": wait_rec["recovery_id"], "status": "waiting_for_run"}, sup_still_waiting["reconciled"])
        self.assertNotIn("eeeeeeeeeeee", [r["recovery_id"] for r in sup_still_waiting["reconciled"]])
        self.assertEqual(sup_still_waiting["registered_dispatches"], [])
        self.assertEqual(sup_still_waiting["resumed"], [])
        self.assertEqual(self.supervisor.load_record("cccccccccccc")["status"], "blocked")

        # Once waiting recovery finishes -> next supervise auto-reconciles it to recovered!
        self.fake_orca.automation_runs[wait_rec["recovery_automation_id"]] = [
            {"id": wait_rec["recovery_run_id"], "status": "completed", "outputSnapshot": {"content": "OK"}}
        ]
        sup_done = self.supervisor.supervise(execute=True, now_ts=time.time() + 190)
        self.assertEqual(sup_done["reconciled"], [{"recovery_id": wait_rec["recovery_id"], "status": "recovered"}])

    def test_wiring_bot_idempotent_and_reversible(self):
        mgr = wiring.WiringManager(
            adapter=self.adapter,
            state_dir=self.state_dir,
            repo_root=Path(__file__).resolve().parent.parent,
            systemd_dir=self.root / "systemd",
        )
        orig_soul = (self.bots_dir / "atlas-orchestrator" / "SOUL.md").read_text(encoding="utf-8")
        orig_agents = (self.bots_dir / "atlas-orchestrator" / "AGENTS.md").read_text(encoding="utf-8")

        dry = mgr.install_bot("Atlas · Orchestrator", default_role="implementation", default_lane="economy", execute=False)
        self.assertEqual(dry["status"], "dry_run_planned")
        self.assertEqual((self.bots_dir / "atlas-orchestrator" / "SOUL.md").read_text(encoding="utf-8"), orig_soul)
        self.assertEqual(self.fake_orca.bot_tools_by_bot, {})

        inst = mgr.install_bot("Atlas · Orchestrator", default_role="implementation", default_lane="economy", execute=True)
        self.assertEqual(inst["status"], "installed")
        self.assertEqual(len(self.fake_orca.bot_tools_by_bot["atlas-orchestrator"]), 1)
        soul_after = (self.bots_dir / "atlas-orchestrator" / "SOUL.md").read_text(encoding="utf-8")
        self.assertIn(wiring.BOT_MARKER_BEGIN, soul_after)
        self.assertIn("Keep role files short.", soul_after)
        self.assertEqual((self.bots_dir / "atlas-orchestrator" / "AGENTS.md").read_text(encoding="utf-8"), orig_agents)

        # Idempotent re-run
        calls_before = len(self.fake_orca.calls)
        again = mgr.install_bot("atlas-orchestrator", default_role="implementation", default_lane="economy", execute=True)
        self.assertEqual(again["status"], "unchanged")
        self.assertFalse(any(c[:3] == ("bot", "tools", "add") for c in self.fake_orca.calls[calls_before:]))

        # Reversible uninstall
        un = mgr.uninstall_bot("atlas-orchestrator", execute=True)
        self.assertEqual(un["status"], "uninstalled")
        self.assertEqual((self.bots_dir / "atlas-orchestrator" / "SOUL.md").read_text(encoding="utf-8"), orig_soul)
        self.assertEqual(self.fake_orca.bot_tools_by_bot["atlas-orchestrator"], [])

    def test_wiring_automation_eligibility_idempotency_and_supervise_enrolled_only(self):
        # Eligible automation
        self.fake_orca.automations["auto_ok"] = {
            "id": "auto_ok",
            "name": "Nightly Repo Check",
            "enabled": True,
            "rrule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
            "workspaceMode": "existing",
            "runContext": {"path": str(self.worktree)},
            "prompt": "Check repository status.",
            "agentId": "codex",
            "extraAgentArgs": "--model gpt-6-luna",
        }
        # Ineligible: one-time, new-per-run, unenrolled failing automation
        self.fake_orca.automations["auto_once"] = {
            "id": "auto_once",
            "name": "One-time",
            "enabled": True,
            "once": {"by": "2026-12-01T00:00:00Z"},
            "runContext": {"path": str(self.worktree)},
            "prompt": "Run once",
        }
        self.fake_orca.automations["auto_new_wt"] = {
            "id": "auto_new_wt",
            "name": "New Per Run",
            "enabled": True,
            "rrule": "FREQ=DAILY;BYHOUR=8;BYMINUTE=0",
            "workspaceMode": "new-per-run",
            "runContext": {"path": str(self.worktree)},
            "prompt": "New worktree",
        }
        self.fake_orca.automations["auto_unenrolled"] = {
            "id": "auto_unenrolled",
            "name": "Unenrolled Schedule",
            "enabled": True,
            "rrule": "FREQ=DAILY;BYHOUR=7;BYMINUTE=0",
            "runContext": {"path": str(self.worktree)},
            "prompt": "Legacy job",
            "agentId": "codex",
            "extraAgentArgs": "--model gpt-6-luna",
        }
        self.fake_orca.automation_runs["auto_unenrolled"] = [
            {"id": "run_un_1", "status": "failed", "error": "rate_limit"}
        ]

        mgr = wiring.WiringManager(
            adapter=self.adapter,
            state_dir=self.state_dir,
            repo_root=Path(__file__).resolve().parent.parent,
            systemd_dir=self.root / "systemd",
        )
        status = mgr.status()
        by_id = {a["id"]: a for a in status["automations"]}
        self.assertTrue(by_id["auto_ok"]["eligible"])
        self.assertEqual(by_id["auto_once"]["skip_reason"], "one_time_automation")
        self.assertEqual(by_id["auto_new_wt"]["skip_reason"], "unsupported_workspace_mode")

        # Ineligible skipped
        skipped = mgr.install_automation("auto_once", role="implementation", lane="economy", execute=True)
        self.assertEqual(skipped["status"], "skipped_ineligible")

        # Pre-enrollment failing runs on auto_ok MUST NOT be replayed after enrollment!
        self.fake_orca.automation_runs["auto_ok"] = [
            {
                "id": "run_pre_enroll_baseline",
                "status": "failed",
                "error": "rate_limit",
                "startedAt": "2020-01-01T00:00:00Z",
            }
        ]

        # Dry run preserves prompt & explains native boundary
        dry = mgr.install_automation("auto_ok", role="implementation", lane="economy", execute=False)
        self.assertEqual(dry["status"], "dry_run_planned")
        self.assertIn("install-scheduled-dispatch", dry["boundary_note"])
        self.assertEqual(self.fake_orca.automations["auto_ok"]["prompt"], "Check repository status.")

        # Execute + idempotent re-run
        inst = mgr.install_automation("auto_ok", role="implementation", lane="economy", execute=True)
        self.assertEqual(inst["status"], "installed")
        self.assertEqual(inst["baseline_run_count"], 1)
        self.assertIn(wiring.AUTO_MARKER_BEGIN, self.fake_orca.automations["auto_ok"]["prompt"])
        again = mgr.install_automation("auto_ok", role="implementation", lane="economy", execute=True)
        self.assertEqual(again["status"], "unchanged")

        # Supervise skips pre-enrollment baseline run AND older-timestamp run not in baseline
        s_pre1 = self.supervisor.supervise(execute=True)
        self.assertEqual(s_pre1["enrolled_automations"], [])
        self.assertTrue(self.fake_orca.automations["auto_ok"]["enabled"])

        self.fake_orca.automation_runs["auto_ok"] = [
            {
                "id": "run_pre_enroll_old_ts",
                "status": "failed",
                "error": "rate_limit",
                "startedAt": "2020-01-02T00:00:00Z",
            }
        ]
        s_pre2 = self.supervisor.supervise(execute=True)
        self.assertEqual(s_pre2["enrolled_automations"], [])
        self.assertTrue(self.fake_orca.automations["auto_ok"]["enabled"])
        self.assertTrue(any(s.get("reason") == "pre_enrollment_run_timestamp" for s in s_pre2["skipped"]))

        # Enrolled automation has a task failure -> supervisor rejects & caches, never recovers or disables
        self.fake_orca.automation_runs["auto_ok"] = [
            {"id": "run_task_err", "status": "failed", "errorCode": "test_failed", "error": "pytest failed"}
        ]
        s1 = self.supervisor.supervise(execute=True)
        self.assertEqual(s1["enrolled_automations"], [])
        self.assertTrue(self.fake_orca.automations["auto_ok"]["enabled"])
        self.assertTrue(self.fake_orca.automations["auto_unenrolled"]["enabled"])

        # Enrolled automation hits provider error -> supervisor recovers -> unenrolled still untouched!
        self.fake_orca.automation_runs["auto_ok"] = [
            {"id": "run_prov_err", "status": "failed", "error": "rate_limit exceeded"}
        ]
        self.fake_orca.worker_start_queue.append({"taskId": "ta", "dispatchId": "da"})
        self.fake_orca.worker_shows["da"] = {"state": "succeeded", "stage": "settled", "liveness": "exited"}
        with mock.patch.dict(run.SETTINGS, {"poll_seconds": 0}):
            s2 = self.supervisor.supervise(execute=True)
        self.assertEqual(len(s2["enrolled_automations"]), 1)
        self.assertEqual(s2["enrolled_automations"][0]["status"], "recovered")
        self.assertFalse(self.fake_orca.automations["auto_ok"]["enabled"])
        self.assertTrue(self.fake_orca.automations["auto_unenrolled"]["enabled"])

        # Uninstall restores exact original prompt
        un = mgr.uninstall_automation("auto_ok", execute=True)
        self.assertEqual(un["status"], "uninstalled")
        self.assertEqual(self.fake_orca.automations["auto_ok"]["prompt"], "Check repository status.")

    def test_mcp_dispatch_and_systemd_unit_install(self):
        # MCP initialize & tools/list & status & dry-run dispatch
        init = mcp_dispatch.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "orca-fallback-dispatch")
        tools = mcp_dispatch.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        names = [t["name"] for t in tools["result"]["tools"]]
        self.assertEqual(names, ["orca_fallback_dispatch", "orca_fallback_status"])

        dry_call = mcp_dispatch.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "orca_fallback_dispatch",
                    "arguments": {
                        "worktree": str(self.worktree),
                        "title": "MCP Smoke",
                        "role": "implementation",
                        "lane": "economy",
                        "prompt": "Run smoke check",
                        "dry_run": True,
                    },
                },
            }
        )
        self.assertFalse(dry_call["result"]["isError"])
        payload = json.loads(dry_call["result"]["content"][0]["text"])
        self.assertEqual(payload["status"], "planned_dry_run")

        # Systemd install + uninstall in isolated temp dir
        sys_dir = self.root / "systemd-user"
        mgr = wiring.WiringManager(
            adapter=self.adapter,
            state_dir=self.state_dir,
            repo_root=Path(__file__).resolve().parent.parent,
            systemd_dir=sys_dir,
        )
        dry_sys = mgr.install_systemd(execute=False)
        self.assertEqual(dry_sys["status"], "dry_run_planned")
        self.assertFalse(sys_dir.exists())

        inst_sys = mgr.install_systemd(execute=True)
        self.assertEqual(inst_sys["status"], "installed")
        svc = sys_dir / "orca-recovery-supervisor.service"
        tmr = sys_dir / "orca-recovery-supervisor.timer"
        self.assertTrue(svc.is_file() and tmr.is_file())
        svc_text = svc.read_text(encoding="utf-8")
        self.assertIn("supervise --execute", svc_text)
        self.assertIn(
            "Environment=PATH=%h/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            svc_text,
        )
        tmr_text = tmr.read_text(encoding="utf-8")
        self.assertIn("OnUnitInactiveSec=1min", tmr_text)
        self.assertNotIn("OnUnitActiveSec=1min", tmr_text)
        verify_sup = subprocess.run(
            ["systemd-analyze", "--user", "verify", str(svc), str(tmr)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(verify_sup.returncode, 0, verify_sup.stderr)

        un_sys = mgr.uninstall_systemd(execute=True)
        self.assertEqual(un_sys["status"], "uninstalled")
        self.assertFalse(svc.exists() or tmr.exists())

        # Opt-in systemd deterministic scheduled dispatch for local worktrees
        special_wt = self.root / "wt%h$var"
        special_wt.mkdir()
        dry_sd = mgr.install_scheduled_dispatch(
            name="nightly-check",
            worktree=special_wt,
            role="implementation",
            lane="economy",
            spec_file=self.spec_file,
            title="Nightly Repo $HOME Check",
            on_calendar="daily",
            execute=False,
        )
        self.assertEqual(dry_sd["status"], "dry_run_planned")
        inst_sd = mgr.install_scheduled_dispatch(
            name="nightly-check",
            worktree=special_wt,
            role="implementation",
            lane="economy",
            spec_file=self.spec_file,
            title="Nightly Repo $HOME Check",
            on_calendar="daily",
            execute=True,
        )
        self.assertEqual(inst_sd["status"], "installed")
        sd_svc = sys_dir / "orca-dispatch-nightly-check.service"
        sd_tmr = sys_dir / "orca-dispatch-nightly-check.timer"
        self.assertTrue(sd_svc.is_file() and sd_tmr.is_file())
        sd_svc_text = sd_svc.read_text(encoding="utf-8")
        self.assertIn("dispatch.py", sd_svc_text)
        self.assertIn(
            "Environment=PATH=%h/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            sd_svc_text,
        )
        self.assertIn(f"WorkingDirectory={str(special_wt).replace('%', '%%')}", sd_svc_text)
        self.assertIn("Nightly Repo $$HOME Check", sd_svc_text)
        self.assertIn("OnCalendar=daily", sd_tmr.read_text(encoding="utf-8"))
        verify_sd = subprocess.run(
            ["systemd-analyze", "--user", "verify", str(sd_svc), str(sd_tmr)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(verify_sd.returncode, 0, verify_sd.stderr)
        un_sd = mgr.uninstall_scheduled_dispatch(name="nightly-check", execute=True)
        self.assertEqual(un_sd["status"], "uninstalled")
        self.assertFalse(sd_svc.exists() or sd_tmr.exists())

    def test_git_worktree_mutation_during_attempt_blocks_post_cooldown_retry(self):
        git_wt = self.root / "git_repo"
        git_wt.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=git_wt, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=git_wt, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=git_wt, check=True)
        (git_wt / "README.md").write_text("clean\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=git_wt, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=git_wt, check=True)

        self.fake_orca.worker_start_queue.extend(
            [{"taskId": "tg1", "dispatchId": "dg1"}, {"taskId": "tg1", "dispatchId": "dg2"}]
        )
        self.fake_orca.worker_shows["dg1"] = {"state": "failed", "stage": "settled", "liveness": "exited"}
        self.fake_orca.worker_reads["dg1"] = {"transcript": {"messages": [{"role": "assistant", "text": "rate_limit"}]}}
        self.fake_orca.worker_shows["dg2"] = {"state": "failed", "stage": "settled", "liveness": "exited"}
        self.fake_orca.worker_reads["dg2"] = {"transcript": {"messages": [{"role": "assistant", "text": "rate_limit"}]}}

        orig_call = self.fake_orca.call

        def mutate_on_dg1(*args, **kwargs):
            res = orig_call(*args, **kwargs)
            if args[:2] == ("orchestration", "worker-show") and "dg1" in args:
                (git_wt / "partial.txt").write_text("side effect\n", encoding="utf-8")
            return res

        with mock.patch.object(self.fake_orca, "call", side_effect=mutate_on_dg1), mock.patch.dict(
            run.SETTINGS, {"poll_seconds": 0}
        ):
            disp = dispatch.dispatch_task(
                worktree=git_wt,
                spec_file=self.spec_file,
                title="Git mutation side effect",
                role="implementation",
                lane="economy",
                orca=self.fake_orca,
                state_dir=self.state_dir,
            )
        self.assertEqual(disp["recovery_status"], "blocked")
        rec = self.supervisor.load_record(disp["recovery_id"])
        self.assertEqual(rec["block_reason"], "prior_side_effects_not_proven_absent_user_decision_required")
        self.assertFalse(rec["all_pre_work_safe"])


if __name__ == "__main__":
    unittest.main()
