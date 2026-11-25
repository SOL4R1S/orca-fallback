import argparse
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import catalog
import dispatch
import recovery
import run


class FakeOrca:
    """In-memory fake Orca CLI matching realistic bot chats, agent show/read, and automations."""

    def __init__(self):
        self.calls: list[tuple[str, ...]] = []
        self.worker_shows: dict[str, dict | list[dict]] = {}
        self.worker_reads: dict[str, dict] = {}
        self.worker_start_queue: list[dict] = []
        self.chats_by_bot: dict[str, list[dict]] = {}
        self.agent_shows: dict[str, dict] = {}
        self.agent_reads: dict[str, dict] = {}
        self.automations: dict[str, dict] = {}
        self.automation_runs: dict[str, list[dict]] = {}
        self.created_automations: list[dict] = []
        self.run_receipts: dict[str, dict] = {}
        self.run_errors: dict[str, Exception] = {}
        self.run_triggers: list[tuple[str, str]] = []
        self.edit_hooks: list = []
        self.probe_results: list[dict] = []
        self._seq = 0

    def call(self, *args: str, cwd: Path | None = None, timeout: int = 90) -> dict:
        self.calls.append(args)
        cmd = args[:2]
        if cmd == ("terminal", "create"):
            self._seq += 1
            return {"terminal": {"handle": f"term_{self._seq}"}}
        if cmd == ("terminal", "close"):
            return {"closed": True}
        if cmd == ("orchestration", "worker-start"):
            self._seq += 1
            if self.worker_start_queue:
                return self.worker_start_queue.pop(0)
            return {"taskId": f"task_{self._seq}", "dispatchId": f"disp_{self._seq}"}
        if cmd == ("orchestration", "worker-show"):
            dispatch_id = args[args.index("--dispatch") + 1]
            entry = self.worker_shows[dispatch_id]
            if isinstance(entry, list):
                return entry.pop(0) if len(entry) > 1 else entry[0]
            return entry
        if cmd == ("orchestration", "worker-read"):
            dispatch_id = args[args.index("--dispatch") + 1]
            return self.worker_reads[dispatch_id]
        if cmd == ("bot", "chats"):
            bot = args[args.index("--bot") + 1]
            return {"chats": self.chats_by_bot.get(bot, [])}
        if cmd == ("agent", "show"):
            session_arg = args[2]
            session_id = session_arg.removeprefix("orca_session_id:")
            if session_arg not in self.agent_shows and session_id not in self.agent_shows:
                raise RuntimeError(f"Unknown agent session {session_arg}")
            return dict(self.agent_shows.get(session_arg) or self.agent_shows[session_id])
        if cmd == ("agent", "read"):
            session_arg = args[2]
            session_id = session_arg.removeprefix("orca_session_id:")
            if session_arg not in self.agent_reads and session_id not in self.agent_reads:
                raise RuntimeError(f"Transcript unavailable for {session_arg}")
            return dict(self.agent_reads.get(session_arg) or self.agent_reads[session_id])
        if cmd == ("automations", "list"):
            return {"automations": list(self.automations.values())}
        if cmd == ("automations", "show"):
            auto_id = args[2]
            return dict(self.automations[auto_id])
        if cmd == ("automations", "runs"):
            auto_id = args[args.index("--id") + 1]
            return {"runs": list(self.automation_runs.get(auto_id, []))}
        if cmd == ("automations", "edit"):
            auto_id = args[2]
            auto = self.automations[auto_id]
            if "--disabled" in args:
                auto["enabled"] = False
            elif "--enabled" in args:
                auto["enabled"] = True
            for hook in list(self.edit_hooks):
                hook(auto_id, args, self)
            return {"id": auto_id, "enabled": auto["enabled"]}
        if cmd == ("automations", "create"):
            self._seq += 1
            new_id = f"auto_recovery_{self._seq}"
            enabled = "--enabled" in args or "--disabled" not in args
            name = args[args.index("--name") + 1] if "--name" in args else new_id
            record = {"id": new_id, "name": name, "enabled": enabled, "args": list(args)}
            self.created_automations.append(record)
            self.automations[new_id] = record
            if enabled and "--by" in args:
                self.run_triggers.append(("scheduled_by", new_id))
            return record
        if cmd == ("automations", "run"):
            auto_id = args[2]
            self.run_triggers.append(("manual", auto_id))
            if auto_id in self.run_errors:
                raise self.run_errors[auto_id]
            return self.run_receipts.get(
                auto_id,
                {"id": auto_id, "runId": f"run_{auto_id}", "status": "accepted"},
            )
        raise AssertionError(f"Unexpected Orca command: {args}")


class FakeAdapter(recovery.OrcaRecoveryAdapter):
    def __init__(self, fake_orca: FakeOrca, bots_dir: Path):
        super().__init__(orca=fake_orca, bots_dir=bots_dir)
        self.fake_orca = fake_orca
        self.probes: list[str] = []

    def probe_health(self, provider: dict, timeout_seconds: int = 30) -> dict:
        self.probes.append(provider["name"])
        if self.fake_orca.probe_results:
            return self.fake_orca.probe_results.pop(0)
        return {
            "ok": True,
            "probe_type": "inference",
            "agent": provider["agent"],
            "provider": provider["name"],
            "model": provider["model"],
            "exact_ok": True,
        }


class RecoveryAndDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_dir = self.root / "state"
        self.worktree = self.root / "repo"
        self.worktree.mkdir()
        self.spec_file = self.root / "task.md"
        self.spec_file.write_text("Complete the bounded recovery check.\n", encoding="utf-8")
        self.bots_dir = self.root / "bots"
        atlas_dir = self.bots_dir / "atlas-orchestrator"
        atlas_dir.mkdir(parents=True)
        (atlas_dir / "bot.json").write_text(
            json.dumps(
                {
                    "id": "atlas-orchestrator",
                    "name": "Atlas · Orchestrator",
                    "api_key": "test-secret-value",
                }
            ),
            encoding="utf-8",
        )
        (atlas_dir / "SOUL.md").write_text("# Atlas\n", encoding="utf-8")
        (atlas_dir / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
        (atlas_dir / "items").mkdir()

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
                            "key": "codex:kimi-code/k3.1-pro",
                            "agent": "codex",
                            "model": "kimi-code/k3.1-pro",
                            "provider": "kimi-code",
                            "available": True,
                            "pricing": {"cost4": {"input": 1.5, "output": 6.0}},
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
                                        {"key": "codex:kimi-code/k3.1-pro", "effort": "high"},
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
                        {"model_key": "codex:kimi-code/k3.1-pro", "benchmark": "artificial_analysis.intelligence_index", "score": 55},
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

        self.fake_orca = FakeOrca()
        self.adapter = FakeAdapter(self.fake_orca, self.bots_dir)
        self.supervisor = recovery.RecoverySupervisor(
            adapter=self.adapter,
            state_dir=self.state_dir,
        )

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tmp.cleanup()

    def _seed_realistic_bot_chat(
        self,
        *,
        bot_id: str = "atlas-orchestrator",
        chat_id: str = "chat_1",
        show_state: str = "done",
        read_state: str = "done",
        assistant_error: str = "insufficient_quota on gpt-6.2-sol",
        last_asked: str = "Fix the broken build",
        run_id: str | None = None,
        closed: bool = False,
        item_id: str | None = "7",
    ) -> None:
        self.fake_orca.chats_by_bot[bot_id] = [
            {
                "id": chat_id,
                "title": "Fix build",
                "lastActivityAt": "2026-11-25T07:30:00Z",
                "lastAsked": last_asked,
                "lastSaid": "Attempted request",
                "home": False,
                "closed": closed,
                **({"lastRunId": run_id} if run_id else {}),
                **({"itemId": item_id} if item_id is not None else {}),
            }
        ]
        self.fake_orca.agent_shows[chat_id] = {
            "id": f"orca_session_id:{chat_id}",
            "state": show_state,
            **({"runId": run_id} if run_id else {}),
        }
        self.fake_orca.agent_reads[chat_id] = {
            "id": f"orca_session_id:{chat_id}",
            "state": read_state,
            **({"runId": run_id} if run_id else {}),
            "messages": [
                {"role": "user", "text": last_asked},
                {"role": "assistant", "text": assistant_error},
            ],
        }

    # --- Finding 1 & 17: worker settlement, transitional polling, coordinator retention ---
    def test_worker_transitional_polling_and_uncertain_settlement_retains_coordinator(self):
        self.fake_orca.worker_start_queue.append({"taskId": "t1", "dispatchId": "d1"})
        # First poll says failed but still settling; second poll is settled and failed -> fallback succeeds
        self.fake_orca.worker_shows["d1"] = [
            {"state": "failed", "stage": "settling", "liveness": "alive"},
            {"state": "failed", "stage": "settled", "liveness": "exited"},
        ]
        self.fake_orca.worker_reads["d1"] = {
            "transcript": {"messages": [{"role": "assistant", "text": "rate_limit exceeded"}]}
        }
        self.fake_orca.worker_start_queue.append({"taskId": "t1", "dispatchId": "d2"})
        self.fake_orca.worker_shows["d2"] = {
            "state": "succeeded",
            "stage": "settled",
            "liveness": "exited",
        }
        with mock.patch.dict(run.SETTINGS, {"poll_seconds": 0, "worker_timeout_seconds": 5}):
            res = run.run_job(
                argparse.Namespace(
                    worktree=self.worktree,
                    spec_file=self.spec_file,
                    title="Test",
                    role="implementation",
                    lane="premium",
                ),
                orca=self.fake_orca,
                state_dir=self.state_dir,
            )
        self.assertEqual(res["status"], "succeeded")
        self.assertEqual(res["selected_provider"], "antigravity:gemini-4-hydrogen-high")

        # Succeeded without settlement -> blocked and coordinator NOT closed (retained)
        self.fake_orca.calls.clear()
        self.fake_orca.worker_start_queue.append({"taskId": "t2", "dispatchId": "d3"})
        self.fake_orca.worker_shows["d3"] = {
            "state": "succeeded",
            "stage": "settling",
            "liveness": "alive",
        }
        with mock.patch.dict(run.SETTINGS, {"poll_seconds": 0, "worker_timeout_seconds": 0}):
            with self.assertRaises(TimeoutError):
                run.run_job(
                    argparse.Namespace(
                        worktree=self.worktree,
                        spec_file=self.spec_file,
                        title="Uncertain",
                        role="implementation",
                        lane="premium",
                    ),
                    orca=self.fake_orca,
                    state_dir=self.state_dir,
                )
        self.assertFalse(any(c[:2] == ("terminal", "close") for c in self.fake_orca.calls))

        # Settled without failed/succeeded outcome -> blocked and coordinator retained
        self.fake_orca.calls.clear()
        self.fake_orca.worker_start_queue.append({"taskId": "t3", "dispatchId": "d4"})
        self.fake_orca.worker_shows["d4"] = {
            "state": "unknown",
            "stage": "settled",
            "liveness": "exited",
        }
        with self.assertRaises(RuntimeError):
            run.run_job(
                argparse.Namespace(
                    worktree=self.worktree,
                    spec_file=self.spec_file,
                    title="BadOutcome",
                    role="implementation",
                    lane="premium",
                ),
                orca=self.fake_orca,
                state_dir=self.state_dir,
            )
        self.assertFalse(any(c[:2] == ("terminal", "close") for c in self.fake_orca.calls))

    # --- Finding 2: stale older-turn text, structured task error, human wait precedence ---
    def test_stale_older_turn_and_structured_task_error_and_human_wait_precedence(self):
        # Older turn had rate limit in tool output; current turn succeeded ("Done")
        self.fake_orca.chats_by_bot["atlas-orchestrator"] = [
            {
                "id": "chat_stale",
                "title": "Stale",
                "lastActivityAt": "2026-11-25T07:30:00Z",
                "lastAsked": "Run tests",
                "lastSaid": "rate limit in earlier note",
                "home": False,
                "closed": False,
            }
        ]
        self.fake_orca.agent_shows["chat_stale"] = {"id": "orca_session_id:chat_stale", "state": "done"}
        self.fake_orca.agent_reads["chat_stale"] = {
            "id": "orca_session_id:chat_stale",
            "state": "done",
            "messages": [
                {"role": "user", "text": "Old turn"},
                {"role": "tool", "text": "rate limit warning from old turn"},
                {"role": "user", "text": "Run tests"},
                {"role": "assistant", "text": "All tests passed. Done."},
            ],
        }
        res_stale = self.supervisor.recover(
            kind="bot_chat",
            role="implementation",
            lane="premium",
            bot="atlas-orchestrator",
            chat_id="chat_stale",
            failed_provider="codex:gpt-6.2-sol",
            execute=True,
        )
        self.assertEqual(res_stale["status"], "rejected_non_provider")

        # Human wait in agent_read overrides provider text in agent_show/messages
        self.fake_orca.agent_shows["chat_wait_read"] = {"id": "orca_session_id:chat_wait_read", "state": "done"}
        self.fake_orca.agent_reads["chat_wait_read"] = {
            "id": "orca_session_id:chat_wait_read",
            "state": "waiting",
            "agentWait": {"source": "prompt-text"},
            "messages": [{"role": "assistant", "text": "rate_limit then waiting for human"}],
        }
        self.fake_orca.chats_by_bot["atlas-orchestrator"].append(
            {
                "id": "chat_wait_read",
                "title": "Wait",
                "lastActivityAt": "2026-11-25T07:31:00Z",
                "lastAsked": "Deploy",
                "home": False,
                "closed": False,
            }
        )
        res_wait = self.supervisor.recover(
            kind="bot_chat",
            role="implementation",
            lane="premium",
            bot="atlas-orchestrator",
            chat_id="chat_wait_read",
            failed_provider="codex:gpt-6.2-sol",
            execute=True,
        )
        self.assertEqual(res_wait["status"], "rejected_non_provider")
        self.assertEqual(res_wait["verification_evidence"]["classification"]["classification"], "human_input_wait")

        # Worker with errorCode=test_failed takes precedence over text mentioning rate limit
        self.fake_orca.worker_shows["disp_code"] = {
            "state": "failed",
            "stage": "settled",
            "liveness": "exited",
            "errorCode": "test_failed",
        }
        self.fake_orca.worker_reads["disp_code"] = {
            "transcript": {"messages": [{"role": "assistant", "text": "rate limit test failed"}]}
        }
        res_code = self.supervisor.recover(
            kind="worker",
            role="implementation",
            lane="premium",
            dispatch_id="disp_code",
            worktree=self.worktree,
            failed_provider="codex:gpt-6.2-sol",
            execute=True,
        )
        self.assertEqual(res_code["status"], "rejected_non_provider")

    # --- Finding 3: canonical identity aliases across keys, names, run_id, failed_provider ---
    def test_canonical_identity_deduplicates_aliases_names_and_implicit_vs_explicit_run_id(self):
        self._seed_realistic_bot_chat(chat_id="chat_alias", run_id="run_bot_9")
        first = self.supervisor.recover(
            kind="bot_chat",
            role="implementation",
            lane="premium",
            bot="Atlas · Orchestrator",
            chat_id="chat_alias",
            failed_provider="codex:gpt-6.2-sol",
            idempotency_key="caller-key-A",
            execute=True,
        )
        self.assertEqual(first["status"], "waiting_for_run")

        # Different caller key, canonical slug name, explicit matching run_id -> MUST deduplicate!
        second = self.supervisor.recover(
            kind="bot_chat",
            role="implementation",
            lane="premium",
            bot="atlas-orchestrator",
            chat_id="chat_alias",
            run_id="run_bot_9",
            failed_provider="codex:gpt-6.2-sol",
            idempotency_key="caller-key-B",
            execute=True,
        )
        self.assertTrue(second["deduplicated"])
        self.assertEqual(second["recovery_id"], first["recovery_id"])
        self.assertEqual(len(self.fake_orca.created_automations), 1)

        # Automation explicit vs implicit run_id + different caller keys
        self.fake_orca.automations["auto_alias"] = {
            "id": "auto_alias",
            "enabled": True,
            "bot": "atlas-orchestrator",
            "prompt": "Sync",
        }
        self.fake_orca.automation_runs["auto_alias"] = [
            {"id": "run_auto_1", "status": "failed", "error": "rate_limit"}
        ]
        a1 = self.supervisor.recover(
            kind="automation",
            role="implementation",
            lane="premium",
            automation_id="auto_alias",
            failed_provider="codex:gpt-6.2-sol",
            idempotency_key="auto-k1",
            execute=True,
        )
        self.fake_orca.automations["auto_alias"]["enabled"] = False
        a2 = self.supervisor.recover(
            kind="automation",
            role="implementation",
            lane="premium",
            automation_id="auto_alias",
            run_id="run_auto_1",
            failed_provider="codex:gpt-6.2-sol",
            idempotency_key="auto-k2",
            execute=True,
        )
        self.assertTrue(a2["deduplicated"])
        self.assertEqual(a2["recovery_id"], a1["recovery_id"])

        # Worker canonical key ignores failed_provider difference
        self.fake_orca.worker_shows["disp_canon"] = {
            "state": "failed",
            "stage": "settled",
            "liveness": "exited",
            "modelKey": "codex:gpt-6.2-sol",
        }
        self.fake_orca.worker_reads["disp_canon"] = {
            "transcript": {"messages": [{"role": "assistant", "text": "usage limit"}]}
        }
        self.fake_orca.worker_start_queue.append({"taskId": "tc", "dispatchId": "dc2"})
        self.fake_orca.worker_shows["dc2"] = {"state": "succeeded", "stage": "settled", "liveness": "exited"}
        w1 = self.supervisor.recover(
            kind="worker",
            role="implementation",
            lane="premium",
            dispatch_id="disp_canon",
            worktree=self.worktree,
            prompt="Finish",
            idempotency_key="wk-1",
            execute=True,
        )
        self.assertEqual(w1["status"], "recovered")
        self.assertEqual(w1["actual_provider"], "antigravity:gemini-4-hydrogen-high")
        w2 = self.supervisor.recover(
            kind="worker",
            role="implementation",
            lane="premium",
            dispatch_id="disp_canon",
            worktree=self.worktree,
            prompt="Finish",
            failed_provider="codex:gpt-6.2-sol",
            idempotency_key="wk-2",
            execute=True,
        )
        self.assertTrue(w2["deduplicated"])
        self.assertEqual(w2["recovery_id"], w1["recovery_id"])

    # --- Finding 4: bind to verified role/lane/provider, reject conflicts ---
    def test_verified_role_lane_and_provider_binding_rejects_mismatch(self):
        self.fake_orca.worker_shows["disp_bound"] = {
            "state": "failed",
            "stage": "settled",
            "liveness": "exited",
            "role": "implementation",
            "lane": "economy",
            "modelKey": "codex:gpt-6-luna",
        }
        self.fake_orca.worker_reads["disp_bound"] = {
            "transcript": {"messages": [{"role": "assistant", "text": "rate limit"}]}
        }
        # Conflicting lane -> blocked
        conflict = self.supervisor.recover(
            kind="worker",
            role="implementation",
            lane="premium",
            dispatch_id="disp_bound",
            worktree=self.worktree,
            prompt="Run",
            execute=True,
        )
        self.assertEqual(conflict["status"], "blocked")
        self.assertEqual(conflict["block_reason"], "conflicting_role_or_lane")

        # Matching lane WITHOUT caller failed_provider -> automatically skips verified failed model
        self.fake_orca.worker_start_queue.append({"taskId": "tb", "dispatchId": "db2"})
        self.fake_orca.worker_shows["db2"] = {"state": "succeeded", "stage": "settled", "liveness": "exited"}
        bound_ok = self.supervisor.recover(
            kind="worker",
            role="implementation",
            lane="economy",
            dispatch_id="disp_bound",
            worktree=self.worktree,
            prompt="Run",
            execute=True,
        )
        self.assertEqual(bound_ok["status"], "recovered")
        self.assertEqual(bound_ok["actual_provider"], "codex:command-code/z-ai/glm-5.3-flash")

    # --- Finding 5: observation-only states allow reinspection after settlement ---
    def test_observation_only_blocked_allows_reinspection_once_settled(self):
        self._seed_realistic_bot_chat(chat_id="chat_re", show_state="working", read_state="working")
        blocked = self.supervisor.recover(
            kind="bot_chat",
            role="implementation",
            lane="premium",
            bot="atlas-orchestrator",
            chat_id="chat_re",
            failed_provider="codex:gpt-6.2-sol",
            execute=True,
        )
        self.assertEqual(blocked["status"], "blocked")

        # Now chat settles with quota failure -> reinspection MUST proceed!
        self._seed_realistic_bot_chat(chat_id="chat_re", show_state="done", read_state="done")
        res = self.supervisor.recover(
            kind="bot_chat",
            role="implementation",
            lane="premium",
            bot="atlas-orchestrator",
            chat_id="chat_re",
            failed_provider="codex:gpt-6.2-sol",
            execute=True,
        )
        self.assertEqual(res["status"], "waiting_for_run")

    # --- Finding 6: dry-run resume rejection & original target recheck ---
    def test_resume_rejects_dry_run_plan_and_rechecks_original_target(self):
        self._seed_realistic_bot_chat(
            chat_id="chat_dry_res",
            assistant_error="usage limit",
        )
        dry_plan = self.supervisor.recover(
            kind="bot_chat",
            role="implementation",
            lane="economy",
            bot="atlas-orchestrator",
            chat_id="chat_dry_res",
            failed_provider="codex:command-code/z-ai/glm-5.3-flash",
            prior_attempted=["codex:gpt-6-luna"],
            execute=False,
        )
        self.assertEqual(dry_plan["status"], "dry_run_exhausted_delayed_resume")
        with self.assertRaises(ValueError):
            self.supervisor.resume_after_probe(dry_plan["recovery_id"], execute=True, now_ts=time.time() + 3600)

        # Live exhausted plan, but original chat becomes working before resume -> blocks!
        run.update_cooldown(self.state_dir / "cooldowns.json", "codex:command-code/z-ai/glm-5.3-flash", 60)
        live_plan = self.supervisor.recover(
            kind="bot_chat",
            role="implementation",
            lane="economy",
            bot="atlas-orchestrator",
            chat_id="chat_dry_res",
            failed_provider="codex:gpt-6-luna",
            execute=True,
        )
        self.assertEqual(live_plan["status"], "exhausted_delayed_resume")
        self._seed_realistic_bot_chat(chat_id="chat_dry_res", show_state="working", read_state="working")
        blocked = self.supervisor.resume_after_probe(
            live_plan["recovery_id"], execute=True, now_ts=time.time() + 3600
        )
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(self.adapter.probes, [])

    # --- Findings 7 & 8: launch-intent crash window + strict run correlation ---
    def test_launch_intent_crash_window_and_strict_run_correlation(self):
        self._seed_realistic_bot_chat(chat_id="chat_crash")
        # Simulate automations_run raising timeout AFTER remote automation was created and accepted run
        def fail_on_run(*args, **kwargs):
            auto_id = args[2] if len(args) > 2 else ""
            if args[:2] == ("automations", "run"):
                self.fake_orca.automation_runs[auto_id] = [{"id": "remote_run_99", "status": "succeeded"}]
                raise RuntimeError("timed out waiting for Orca reply")
            return FakeOrca.call(self.fake_orca, *args, **kwargs)

        with mock.patch.object(self.fake_orca, "call", side_effect=fail_on_run):
            crashed = self.supervisor.recover(
                kind="bot_chat",
                role="implementation",
                lane="premium",
                bot="atlas-orchestrator",
                chat_id="chat_crash",
                failed_provider="codex:gpt-6.2-sol",
                execute=True,
            )
        self.assertEqual(crashed["status"], "blocked")
        self.assertEqual(crashed["block_reason"], "ambiguous_launch_reconcile_required")
        self.assertEqual(crashed["launch_intent"]["step"], "launch_uncertain")

        # Retrying recover MUST deduplicate instead of creating a second automation
        retried = self.supervisor.recover(
            kind="bot_chat",
            role="implementation",
            lane="premium",
            bot="atlas-orchestrator",
            chat_id="chat_crash",
            failed_provider="codex:gpt-6.2-sol",
            execute=True,
        )
        self.assertTrue(retried["deduplicated"])
        self.assertEqual(len(self.fake_orca.created_automations), 1)

        # Reconcile discovers the single run and completes recovery
        reconciled = self.supervisor.reconcile(crashed["recovery_id"], execute=True)
        self.assertEqual(reconciled["status"], "recovered")

        # Finding 8: generic `id == automation_id` on run receipt must NOT become recovery_run_id,
        # `completed` / unknown status must stay unresolved, multiple runs without runId block!
        self._seed_realistic_bot_chat(chat_id="chat_corr")
        self.fake_orca.run_receipts["auto_recovery_2"] = {"id": "auto_recovery_2", "status": "accepted"}
        accepted = self.supervisor.recover(
            kind="bot_chat",
            role="implementation",
            lane="premium",
            bot="atlas-orchestrator",
            chat_id="chat_corr",
            failed_provider="codex:gpt-6.2-sol",
            execute=True,
        )
        self.assertIsNone(accepted.get("recovery_run_id"))
        self.fake_orca.automation_runs[accepted["recovery_automation_id"]] = [
            {"id": "r_other", "status": "completed"}
        ]
        # `completed` is NOT treated as exact `succeeded` -> stays waiting_for_run
        still = self.supervisor.reconcile(accepted["recovery_id"], execute=True)
        self.assertEqual(still["status"], "waiting_for_run")

        # Multiple runs without recovery_run_id -> blocked
        self.fake_orca.automation_runs[accepted["recovery_automation_id"]] = [
            {"id": "r1", "status": "succeeded"},
            {"id": "r2", "status": "failed"},
        ]
        # clear recovery_run_id to simulate uncorrelated multi-run ambiguity
        rec_path = self.state_dir / "recoveries" / f"{accepted['recovery_id']}.json"
        rec_data = json.loads(rec_path.read_text())
        rec_data.pop("recovery_run_id", None)
        run.write_state(rec_path, rec_data)
        run.write_state(
            self.state_dir / "recoveries" / "by_key" / f"{recovery._key_hash(accepted['idempotency_key'])}.json",
            rec_data,
        )
        amb = self.supervisor.reconcile(accepted["recovery_id"], execute=True)
        self.assertEqual(amb["status"], "blocked")
        self.assertEqual(amb["block_reason"], "ambiguous_multiple_runs_without_run_id")

    # --- Finding 9: cron tick during pause & operator edit before reconcile ---
    def test_cron_tick_during_pause_and_operator_edit_before_reconcile(self):
        # 1) Missing enabled bool blocks
        self.fake_orca.automations["auto_no_bool"] = {"id": "auto_no_bool"}
        res_nobool = self.supervisor.recover(
            kind="automation",
            role="implementation",
            lane="premium",
            automation_id="auto_no_bool",
            failed_provider="codex:gpt-6.2-sol",
            execute=True,
        )
        self.assertEqual(res_nobool["status"], "blocked")
        self.assertEqual(res_nobool["block_reason"], "authoritative_enabled_state_missing")

        # 2) Cron tick starts active run between read and edit -> blocked after disable, zero replacement!
        self.fake_orca.automations["auto_race"] = {
            "id": "auto_race",
            "enabled": True,
            "bot": "atlas-orchestrator",
            "prompt": "Cron job",
        }
        self.fake_orca.automation_runs["auto_race"] = [
            {"id": "run_old", "status": "failed", "error": "rate_limit"}
        ]

        def inject_tick(auto_id, args, orca):
            if auto_id == "auto_race" and "--disabled" in args:
                orca.automation_runs["auto_race"].insert(0, {"id": "run_tick", "status": "running"})

        self.fake_orca.edit_hooks.append(inject_tick)
        raced = self.supervisor.recover(
            kind="automation",
            role="implementation",
            lane="premium",
            automation_id="auto_race",
            run_id="run_old",
            failed_provider="codex:gpt-6.2-sol",
            execute=True,
        )
        self.fake_orca.edit_hooks.clear()
        self.assertEqual(raced["status"], "blocked")
        self.assertEqual(raced["block_reason"], "active_run_started_during_pause")
        self.assertEqual(self.fake_orca.created_automations, [])

        # 3) Operator modifies automation (updatedAt changed) before reconcile -> do NOT re-enable!
        self.fake_orca.automations["auto_op"] = {
            "id": "auto_op",
            "enabled": True,
            "updatedAt": "t1",
            "trigger": "0 * * * *",
            "bot": "atlas-orchestrator",
            "prompt": "Cron job",
        }
        self.fake_orca.automation_runs["auto_op"] = [
            {"id": "run_op_1", "status": "failed", "error": "rate_limit"}
        ]
        sup_auto = recovery.RecoverySupervisor(
            adapter=self.adapter,
            state_dir=self.state_dir,
            settings={"auto_resume_automation": True},
        )
        acc = sup_auto.recover(
            kind="automation",
            role="implementation",
            lane="premium",
            automation_id="auto_op",
            run_id="run_op_1",
            failed_provider="codex:gpt-6.2-sol",
            execute=True,
        )
        self.assertEqual(acc["status"], "waiting_for_run")
        # Operator edits schedule independently
        self.fake_orca.automations["auto_op"]["updatedAt"] = "t2_operator"
        self.fake_orca.automation_runs[acc["recovery_automation_id"]] = [
            {"id": acc["recovery_run_id"], "status": "succeeded"}
        ]
        rec = sup_auto.reconcile(acc["recovery_id"], execute=True)
        self.assertEqual(rec["status"], "recovered")
        self.assertFalse(self.fake_orca.automations["auto_op"]["enabled"])

    # --- Findings 10, 12, 13, 14: exact probed resume, spec_file/lastAsked preserved, post-runner exhaustion, isolated cwd ---
    def test_exact_probed_provider_resume_preserves_spec_and_post_runner_exhaustion(self):
        # 1) Worker recovery runs model 2, which ALSO hits rate limit -> run_job returns lane_exhausted,
        # and supervisor creates delayed_resume_plan with unused model 3 (kimi) & preserves spec_file!
        self.fake_orca.worker_shows["disp_ex"] = {
            "state": "failed",
            "stage": "settled",
            "liveness": "exited",
            "modelKey": "codex:gpt-6.2-sol",
        }
        self.fake_orca.worker_reads["disp_ex"] = {
            "transcript": {"messages": [{"role": "assistant", "text": "rate_limit"}]}
        }
        # Put kimi on pre-existing cooldown so run_job tries gemini, fails with rate_limit, then hits lane_exhausted
        run.update_cooldown(self.state_dir / "cooldowns.json", "codex:kimi-code/k3.1-pro", 120)
        self.fake_orca.worker_start_queue.append({"taskId": "tex", "dispatchId": "dex_2"})
        self.fake_orca.worker_shows["dex_2"] = {
            "state": "failed",
            "stage": "settled",
            "liveness": "exited",
        }
        self.fake_orca.worker_reads["dex_2"] = {
            "transcript": {"messages": [{"role": "assistant", "text": "usage limit"}]}
        }
        res = self.supervisor.recover(
            kind="worker",
            role="implementation",
            lane="premium",
            dispatch_id="disp_ex",
            worktree=self.worktree,
            spec_file=self.spec_file,
            execute=True,
        )
        self.assertEqual(res["status"], "exhausted_delayed_resume")
        self.assertEqual(res["delayed_resume_plan"]["spec_file"], str(self.spec_file.resolve()))

        # Resume after cooldown: probes ONLY unused kimi, passes, and dispatches ONLY kimi (only_provider)!
        self.fake_orca.worker_start_queue.append({"taskId": "tex", "dispatchId": "dex_3"})
        self.fake_orca.worker_shows["dex_3"] = {
            "state": "succeeded",
            "stage": "settled",
            "liveness": "exited",
        }
        resumed = self.supervisor.resume_after_probe(
            res["recovery_id"], execute=True, now_ts=time.time() + 1800
        )
        self.assertEqual(resumed["status"], "recovered")
        self.assertEqual(self.adapter.probes, ["codex:kimi-code/k3.1-pro"])
        self.assertEqual(resumed["actual_provider"], "codex:kimi-code/k3.1-pro")
        # Verify spec_file was preserved in worker-start
        last_start = [c for c in self.fake_orca.calls if c[:2] == ("orchestration", "worker-start")][-1]
        self.assertIn(str(self.spec_file.resolve()), last_start)

        # 2) Missing task request on bot_chat fails closed; lastAsked is carried when prompt omitted
        self._seed_realistic_bot_chat(chat_id="chat_no_ask", last_asked="")
        self.fake_orca.chats_by_bot["atlas-orchestrator"][0].pop("itemId", None)
        self.fake_orca.agent_reads["chat_no_ask"]["messages"] = [
            {"role": "assistant", "text": "rate_limit"}
        ]
        no_task = self.supervisor.recover(
            kind="bot_chat",
            role="implementation",
            lane="premium",
            bot="atlas-orchestrator",
            chat_id="chat_no_ask",
            failed_provider="codex:gpt-6.2-sol",
            execute=True,
        )
        self.assertEqual(no_task["status"], "blocked")
        self.assertEqual(no_task["block_reason"], "missing_recoverable_task_request")

        # 3) probe_health runs in isolated temp cwd and validates exact OK
        real_adapter = recovery.OrcaRecoveryAdapter(orca=self.fake_orca, bots_dir=self.bots_dir)
        observed_cwds: list[Path] = []

        def fake_codex(cmd, **kwargs):
            observed_cwds.append(Path(kwargs["cwd"]))
            Path(cmd[cmd.index("-o") + 1]).write_text("OK\n", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with mock.patch("recovery.subprocess.run", side_effect=fake_codex):
            p = real_adapter.probe_health({"name": "codex:gpt-6-luna", "agent": "codex", "model": "gpt-6-luna"})
        self.assertTrue(p["ok"])
        self.assertEqual(p["probe_type"], "inference")
        self.assertNotEqual(observed_cwds[0], Path.cwd())

        def fake_agy(cmd, **kwargs):
            self.assertTrue(cmd[-1].startswith("--print="))
            return subprocess.CompletedProcess(cmd, 0, stdout="OK\n", stderr="")

        with mock.patch("recovery.subprocess.run", side_effect=fake_agy):
            p = real_adapter.probe_health(
                {"name": "antigravity:gemini-4-hydrogen-high", "agent": "antigravity", "model": "gemini-4-hydrogen-high"}
            )
        self.assertTrue(p["ok"])

    # --- Finding 11: redaction before truncation, nested credential dicts, stderr ---
    def test_redaction_before_truncation_and_nested_credentials(self):
        # Secret crossing the 2000-char boundary: prefix "Bearer " starts just before -2000 slice
        long_tail = "x" * 1995
        boundary_secret = f"Authorization: Bearer secret-boundary-token-987654 {long_tail}"
        self.fake_orca.worker_shows["disp_red"] = {
            "state": "failed",
            "stage": "settled",
            "liveness": "exited",
            "credentials": {"nested_secret": "raw-nested-secret-xyz"},
            "cookie": ["session=raw-cookie-abc"],
        }
        self.fake_orca.worker_reads["disp_red"] = {
            "transcript": {"messages": [{"role": "assistant", "text": boundary_secret}]}
        }
        with self.assertRaises(Exception) if False else mock.patch.dict(run.SETTINGS, {"poll_seconds": 0}):
            res = self.supervisor.recover(
                kind="worker",
                role="implementation",
                lane="premium",
                dispatch_id="disp_red",
                worktree=self.worktree,
                failed_provider="codex:gpt-6.2-sol",
                execute=True,
            )
        dumped = json.dumps(res)
        self.assertNotIn("secret-boundary-token-987654", dumped)
        self.assertNotIn("raw-nested-secret-xyz", dumped)
        self.assertNotIn("raw-cookie-abc", dumped)

        # Direct redact_secrets checks nested dicts
        sanitized = run.redact_secrets({"credentials": {"k": "v"}, "cookie": ["a"], "ok": "yes"})
        self.assertEqual(sanitized["credentials"], "[REDACTED]")
        self.assertEqual(sanitized["cookie"], "[REDACTED]")
        self.assertEqual(sanitized["ok"], "yes")

    # --- Findings 15 & 16: global STATE_DIR isolation & post-run actual/returned_to_preferred ---
    def test_dispatch_state_dir_isolation_and_post_run_provider_metrics(self):
        dir_a = self.root / "dir_a"
        sentinel = self.state_dir
        # Preferred provider fails -> fallback completes -> returned_to_preferred MUST be False!
        self.fake_orca.worker_start_queue.extend(
            [
                {"taskId": "ta1", "dispatchId": "da1"},
                {"taskId": "ta1", "dispatchId": "da2"},
            ]
        )
        self.fake_orca.worker_shows["da1"] = {"state": "failed", "stage": "settled", "liveness": "exited"}
        self.fake_orca.worker_reads["da1"] = {
            "transcript": {"messages": [{"role": "assistant", "text": "rate_limit"}]}
        }
        self.fake_orca.worker_shows["da2"] = {"state": "succeeded", "stage": "settled", "liveness": "exited"}

        res = dispatch.dispatch_task(
            worktree=self.worktree,
            spec_file=self.spec_file,
            title="Fallback check",
            role="implementation",
            lane="economy",
            orca=self.fake_orca,
            state_dir=dir_a,
        )
        self.assertEqual(run.STATE_DIR, sentinel)
        self.assertTrue((dir_a / "dispatches" / f"{res['handoff_id']}.json").is_file())
        self.assertFalse(res["returned_to_preferred"])
        self.assertEqual(res["actual_provider"], "codex:command-code/z-ai/glm-5.3-flash")

        # All providers in cooldown -> skipped -> actual_provider MUST be None!
        run.update_cooldown(dir_a / "cooldowns.json", "codex:gpt-6-luna", 300)
        run.update_cooldown(dir_a / "cooldowns.json", "codex:command-code/z-ai/glm-5.3-flash", 300)
        skipped = dispatch.dispatch_task(
            worktree=self.worktree,
            spec_file=self.spec_file,
            title="All skipped",
            role="implementation",
            lane="economy",
            orca=self.fake_orca,
            state_dir=dir_a,
        )
        self.assertEqual(skipped["status"], "lane_exhausted")
        self.assertIsNone(skipped["actual_provider"])
        self.assertFalse(skipped["returned_to_preferred"])

    # --- Finding 18: safe load_record rejects path traversal ---
    def test_actual_agent_show_places_state(self):
        self.assertEqual(
            recovery._extract_agent_show_states({"places": [{"state": "done"}]}),
            ["done"],
        )

    def test_load_record_rejects_path_traversal(self):
        outside = self.root / "outside.json"
        outside.write_text(json.dumps({"recovery_id": "outside", "idempotency_key": "k"}), encoding="utf-8")
        self.assertIsNone(self.supervisor.load_record("../../outside"))
        self.assertIsNone(self.supervisor.load_record(str(outside)))
        self.assertIsNone(self.supervisor.load_record("../outside.json"))


if __name__ == "__main__":
    unittest.main()
