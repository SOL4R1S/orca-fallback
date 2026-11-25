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

import base64
import ctypes
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.parse import urlparse

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


class _IsolatedOwnerRpcClient:
    """Loopback-only E2EE WebSocket client for creating/deleting disposable test bots."""

    def __init__(self, endpoint: str, expected_port: int, server_pk_b64: str, device_token: str):
        parsed = urlparse(endpoint)
        if parsed.scheme != "ws" or parsed.hostname != "127.0.0.1" or parsed.port != expected_port:
            raise RuntimeError(f"Safety gate failed: non-loopback pairing endpoint {endpoint!r}")
        lib = ctypes.CDLL("libsodium.so.23")
        if lib.sodium_init() < 0:
            raise RuntimeError("sodium_init failed")
        self._lib = lib
        self._nonce_len = lib.crypto_box_noncebytes()
        self._mac_len = lib.crypto_box_macbytes()

        pk = ctypes.create_string_buffer(lib.crypto_box_publickeybytes())
        sk = ctypes.create_string_buffer(lib.crypto_box_secretkeybytes())
        if lib.crypto_box_keypair(pk, sk) != 0:
            raise RuntimeError("crypto_box_keypair failed")
        shared = ctypes.create_string_buffer(lib.crypto_box_beforenmbytes())
        server_pk = base64.b64decode(server_pk_b64)
        if lib.crypto_box_beforenm(shared, server_pk, sk.raw) != 0:
            raise RuntimeError("crypto_box_beforenm failed")
        self._shared = shared.raw

        self._sock = socket.create_connection(("127.0.0.1", expected_port), timeout=15)
        self._buf = b""
        ws_key = base64.b64encode(os.urandom(16)).decode("ascii")
        self._sock.sendall(
            (
                f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{expected_port}\r\n"
                f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {ws_key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode("ascii")
        )
        while b"\r\n\r\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket EOF during HTTP upgrade")
            self._buf += chunk
        hdr, self._buf = self._buf.split(b"\r\n\r\n", 1)
        if b"101 " not in hdr.splitlines()[0]:
            raise RuntimeError("WebSocket HTTP upgrade rejected")

        # Legacy e2ee_hello without caller.orca-cli.v1 capability so caller is owner paired-device
        self._send_text(json.dumps({"type": "e2ee_hello", "publicKeyB64": base64.b64encode(pk.raw).decode("ascii")}))
        hello_reply = json.loads(self._recv_text())
        if hello_reply.get("type") != "e2ee_ready":
            raise RuntimeError(f"Unexpected E2EE hello reply: {hello_reply}")

        self._send_text(self._seal(json.dumps({"type": "e2ee_auth", "deviceToken": device_token})))
        auth_reply = json.loads(self._open(self._recv_text()))
        if auth_reply.get("type") != "e2ee_authenticated":
            raise RuntimeError(f"E2EE auth failed: {auth_reply}")

    def _seal(self, plaintext: str) -> str:
        msg = plaintext.encode("utf-8")
        nonce = os.urandom(self._nonce_len)
        ct = ctypes.create_string_buffer(len(msg) + self._mac_len)
        if self._lib.crypto_box_easy_afternm(ct, msg, ctypes.c_ulonglong(len(msg)), nonce, self._shared) != 0:
            raise RuntimeError("crypto_box_easy_afternm failed")
        return base64.b64encode(nonce + ct.raw).decode("ascii")

    def _open(self, frame_b64: str) -> str:
        raw = base64.b64decode(frame_b64)
        nonce, ct = raw[: self._nonce_len], raw[self._nonce_len :]
        pt = ctypes.create_string_buffer(len(ct) - self._mac_len)
        if self._lib.crypto_box_open_easy_afternm(pt, ct, ctypes.c_ulonglong(len(ct)), nonce, self._shared) != 0:
            raise RuntimeError("crypto_box_open_easy_afternm failed")
        return pt.raw.decode("utf-8")

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise RuntimeError("WebSocket EOF")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        mask = os.urandom(4)
        header = bytearray([0x81])
        ln = len(payload)
        if ln < 126:
            header.append(0x80 | ln)
        elif ln < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", ln))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", ln))
        header.extend(mask)
        self._sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _recv_text(self) -> str:
        while True:
            b1, b2 = self._read_exact(2)
            opcode, masked, ln = b1 & 0x0F, (b2 & 0x80) != 0, b2 & 0x7F
            if ln == 126:
                ln = struct.unpack("!H", self._read_exact(2))[0]
            elif ln == 127:
                ln = struct.unpack("!Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else b""
            data = self._read_exact(ln)
            if masked:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x8:
                raise RuntimeError("WebSocket closed")
            if opcode == 0x9:
                self._sock.sendall(bytes([0x8A, 0x80]) + b"\x00\x00\x00\x00")
                continue
            if opcode == 0xA:
                continue
            return data.decode("utf-8")

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        req_id = f"e2e-{int(time.time() * 1000)}-{method}"
        req: dict[str, Any] = {"id": req_id, "method": method}
        if params is not None:
            req["params"] = params
        self._send_text(self._seal(json.dumps(req)))
        while True:
            msg = json.loads(self._open(self._recv_text()))
            if msg.get("id") == req_id:
                if msg.get("error"):
                    raise RuntimeError(f"RPC error {method}: {msg['error']}")
                return msg.get("result") or {}

    def close(self) -> None:
        try:
            self._sock.close()
        except Exception:
            pass


def _read_loopback_pairing_offer(serve_proc: subprocess.Popen[str], expected_port: int, timeout_seconds: int = 15) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if serve_proc.stdout is None:
            break
        line = serve_proc.stdout.readline().strip()
        if not line:
            if serve_proc.poll() is not None:
                break
            continue
        if "orca_server_ready" in line:
            ready = json.loads(line)
            pairing = ready.get("pairing") or {}
            url = str(pairing.get("url") or "")
            if not pairing.get("available") or "code=" not in url:
                raise RuntimeError("Loopback pairing offer unavailable from isolated serve")
            code = url.split("code=", 1)[1]
            pad = "=" * ((4 - len(code) % 4) % 4)
            offer = json.loads(base64.urlsafe_b64decode(code + pad).decode("utf-8"))
            endpoint = str(offer.get("endpoint") or "")
            parsed = urlparse(endpoint)
            if parsed.hostname != "127.0.0.1" or parsed.port != expected_port:
                raise RuntimeError(f"Safety gate failed: pairing endpoint {endpoint!r} is not loopback :{expected_port}")
            return offer
    raise RuntimeError("Isolated orca-ide serve did not emit orca_server_ready")


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
                            "key": "codex:gpt-6.2-sol",
                            "agent": "codex",
                            "model": "gpt-6.2-sol",
                            "provider": "openai",
                            "available": True,
                            "pricing": {"cost4": {"input": 2.5, "output": 10.0}},
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
                                        {"key": "codex:gpt-6.2-sol", "effort": "high"},
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
                        {"model_key": "codex:gpt-6.2-sol", "benchmark": "artificial_analysis.intelligence_index", "score": 68},
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
            ["orca-ide", "serve", "--port", str(port), "--bind", "127.0.0.1", "--json"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        created_auto_ids: list[str] = []
        created_bot_id: str | None = None
        rpc_client: _IsolatedOwnerRpcClient | None = None
        try:
            pairing_offer = _read_loopback_pairing_offer(serve_proc, port)
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

            # 6. Disposable test bot creation via loopback owner RPC & full cross-provider bot-chat recovery E2E
            rpc_client = _IsolatedOwnerRpcClient(
                pairing_offer["endpoint"],
                port,
                pairing_offer["publicKeyB64"],
                pairing_offer["deviceToken"],
            )
            bot_obj = rpc_client.rpc(
                "bots.create",
                {
                    "name": "E2E Disposable Bot",
                    "agent": "codex",
                    "permissionMode": "ask",
                    "takingOver": True,
                },
            )["bot"]
            created_bot_id = bot_obj["id"]
            bot_folder = bot_obj["folder"]
            assert bot_obj.get("slack") is None and bot_obj.get("linear") is None and bot_obj.get("github") is None, bot_obj

            bot_dir = ud / "bots" / bot_folder
            soul_marker = "# E2E Disposable Bot SOUL\nPreserve SOUL_MARKER_2026 across recovery.\n"
            memory_marker = "# E2E Disposable Bot MEMORY\nPreserve MEMORY_MARKER_2026 across recovery.\n"
            items_marker = json.dumps([{"id": "101", "title": "Preserve ITEMS_MARKER_2026"}])
            (bot_dir / "SOUL.md").write_text(soul_marker, encoding="utf-8")
            (bot_dir / "MEMORY.md").write_text(memory_marker, encoding="utf-8")
            (bot_dir / "items.json").write_text(items_marker, encoding="utf-8")

            # Verify ORCA_USER_DATA_PATH setup-path isolation resolves ud/bots automatically and handles bot display names
            prev_ud_env = os.environ.get("ORCA_USER_DATA_PATH")
            os.environ["ORCA_USER_DATA_PATH"] = str(ud)
            try:
                bot_adapter = recovery.OrcaRecoveryAdapter(orca=orca)
            finally:
                if prev_ud_env is None:
                    os.environ.pop("ORCA_USER_DATA_PATH", None)
                else:
                    os.environ["ORCA_USER_DATA_PATH"] = prev_ud_env
            assert bot_adapter.bots_dir == ud.resolve() / "bots", bot_adapter.bots_dir
            assets_by_name = bot_adapter.read_bot_assets("E2E Disposable Bot")
            assets_by_id = bot_adapter.read_bot_assets(created_bot_id)
            assert assets_by_name["resolved_bot"] == created_bot_id == assets_by_id["resolved_bot"], assets_by_name
            assert assets_by_name["soul_present"] and assets_by_name["memory_present"] and assets_by_name["items_present"]

            # Trigger a harmless failing bot chat on an invalid model in a disabled one-time automation
            by_fail_iso = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            fail_bot_auto = recovery._unwrap_automation(
                orca.call(
                    "automations",
                    "create",
                    "--name",
                    "E2E Bot Failing Chat",
                    "--bot",
                    created_bot_id,
                    "--runs-in",
                    "new",
                    "--once",
                    "--disabled",
                    "--by",
                    by_fail_iso,
                    "--agent",
                    "codex",
                    "--model",
                    "nonexistent-e2e-model-xyz",
                    "--prompt",
                    "Reply with exact text E2E_BOT_RECOVERED_OK and nothing else.",
                )
            )
            fail_bot_auto_id = fail_bot_auto["id"]
            created_auto_ids.append(fail_bot_auto_id)
            orca.call("automations", "run", fail_bot_auto_id)
            term_bot_fail = _wait_for_automation_terminal_run(orca, fail_bot_auto_id, timeout_seconds=60)
            bad_chat_id = term_bot_fail["run"]["chatSessionId"]
            assert bad_chat_id, term_bot_fail

            fail_show = bot_adapter.agent_show(bad_chat_id)
            fail_read = bot_adapter.agent_read(bad_chat_id, limit=20)
            assert isinstance(fail_show.get("places"), list), fail_show
            assert any(isinstance(e, dict) and e.get("kind") == "error" for e in fail_read.get("entries", [])), fail_read

            sup_bot = recovery.RecoverySupervisor(adapter=bot_adapter, state_dir=state_dir)
            bot_rec = sup_bot.recover(
                kind="bot_chat",
                role="implementation",
                lane="premium",
                bot=created_bot_id,
                chat_id=bad_chat_id,
                failed_provider="codex:gpt-6.2-sol",
                idempotency_key="e2e-bot-chat-recovery-1",
                execute=True,
            )
            assert bot_rec["status"] == "waiting_for_run", bot_rec
            assert bot_rec["actual_provider"] == "antigravity:gemini-4-hydrogen-high", bot_rec
            bot_rec_auto_id = bot_rec["recovery_automation_id"]
            bot_rec_run_id = bot_rec["recovery_run_id"]
            assert bot_rec_auto_id and bot_rec_run_id, bot_rec
            created_auto_ids.append(bot_rec_auto_id)

            # Verify canonical deduplication via display-name alias + distinct caller idempotency key
            autos_count_before_dup = len(orca.call("automations", "list").get("automations", []))
            bot_dup = sup_bot.recover(
                kind="bot_chat",
                role="implementation",
                lane="premium",
                bot="E2E Disposable Bot",
                chat_id=bad_chat_id,
                failed_provider="codex:gpt-6.2-sol",
                idempotency_key="e2e-bot-chat-recovery-alias",
                execute=True,
            )
            autos_count_after_dup = len(orca.call("automations", "list").get("automations", []))
            assert bot_dup.get("deduplicated") is True and bot_dup["recovery_id"] == bot_rec["recovery_id"], bot_dup
            assert autos_count_before_dup == autos_count_after_dup

            # Verify replacement bot automation properties: disabled, --once, --runs-in new, antigravity model
            bot_rec_auto_show = bot_adapter.automations_show(bot_rec_auto_id)
            assert bot_rec_auto_show["enabled"] is False, bot_rec_auto_show
            assert isinstance(bot_rec_auto_show.get("once"), dict) and bot_rec_auto_show["once"].get("by"), bot_rec_auto_show
            bot_cfg = bot_rec_auto_show.get("bot") or {}
            assert bot_cfg.get("runsIn") == "new", bot_rec_auto_show
            assert bot_cfg.get("botId") == created_bot_id, bot_rec_auto_show
            agent_setup = bot_cfg.get("agentSetup") or {}
            assert agent_setup.get("agent") == "antigravity", bot_rec_auto_show
            assert (agent_setup.get("launch") or {}).get("model") == "gemini-4-hydrogen-high", bot_rec_auto_show
            assert bot_rec_auto_show.get("agentId") == "antigravity", bot_rec_auto_show

            # Wait for recovery run settlement & reconcile to terminal recovered
            term_bot_rec = _wait_for_automation_terminal_run(orca, bot_rec_auto_id, timeout_seconds=60)
            bot_runs_all = bot_adapter.automations_runs(bot_rec_auto_id).get("runs", [])
            assert len(bot_runs_all) == 1, bot_runs_all
            assert bot_runs_all[0]["id"] == bot_rec_run_id, bot_runs_all
            assert bot_runs_all[0]["status"] == "completed", bot_runs_all[0]
            new_chat_id = bot_runs_all[0].get("chatSessionId")
            assert new_chat_id and new_chat_id != bad_chat_id, bot_runs_all[0]

            bot_reconciled = sup_bot.reconcile(bot_rec["recovery_id"], execute=True)
            assert bot_reconciled["status"] == "recovered", bot_reconciled

            # Verify new bot chat exists separately, and SOUL/MEMORY/Items preserved untouched
            chats_after = orca.call("bot", "chats", "--bot", created_bot_id, "--all").get("chats", [])
            chat_ids_after = [c["id"] for c in chats_after]
            assert bad_chat_id in chat_ids_after and new_chat_id in chat_ids_after, chat_ids_after
            assert (bot_dir / "SOUL.md").read_text(encoding="utf-8") == soul_marker
            assert (bot_dir / "MEMORY.md").read_text(encoding="utf-8") == memory_marker
            assert (bot_dir / "items.json").read_text(encoding="utf-8") == items_marker

            report["scenarios"]["disposable_bot_chat_recovery"] = {
                "ok": True,
                "bot_id": created_bot_id,
                "bad_chat_id": bad_chat_id,
                "bad_chat_status": term_bot_fail["run"]["status"],
                "bad_chat_show_places_count": len(fail_show.get("places", [])),
                "bad_chat_error_entry": recovery.run.latest_turn_failure_text(fail_read),
                "recovery_provider": bot_reconciled["actual_provider"],
                "recovery_automation_id": bot_rec_auto_id,
                "recovery_run_id": bot_rec_run_id,
                "recovery_run_count": len(bot_runs_all),
                "new_chat_id": new_chat_id,
                "runs_in": bot_cfg.get("runsIn"),
                "reconciled_status": bot_reconciled["status"],
                "deduplicated_canonical_alias": True,
                "assets_preserved": {
                    "soul_present": bot_reconciled["verification_evidence"]["bot_assets"]["soul_present"],
                    "memory_present": bot_reconciled["verification_evidence"]["bot_assets"]["memory_present"],
                    "items_present": bot_reconciled["verification_evidence"]["bot_assets"]["items_present"],
                },
                "external_connectors": {"slack": None, "linear": None, "github": None},
            }

            # Cleanup automations and disposable bot inside isolated runtime before exit
            for aid in created_auto_ids:
                try:
                    orca.call("automations", "remove", aid)
                except Exception:
                    pass
            rpc_client.rpc("bots.delete", {"botId": created_bot_id})
            deleted_bot_id = created_bot_id
            created_bot_id = None
            assert orca.call("bot", "list").get("bots", []) == []
            assert orca.call("automations", "list").get("automations", []) == []
            report["cleanup"]["removed_automations"] = created_auto_ids
            report["cleanup"]["deleted_bot_id"] = deleted_bot_id
        finally:
            if rpc_client is not None:
                if created_bot_id is not None:
                    try:
                        rpc_client.rpc("bots.delete", {"botId": created_bot_id})
                    except Exception:
                        pass
                rpc_client.close()
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
            # Also terminate any detached orcad daemon / terminal children scoped to this temp root
            root_str = str(root)
            my_pid = os.getpid()
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                pid = int(entry.name)
                if pid == my_pid:
                    continue
                try:
                    cmdline = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "ignore")
                except OSError:
                    continue
                if root_str in cmdline:
                    try:
                        os.kill(pid, 15)
                    except OSError:
                        pass
            time.sleep(0.5)
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                pid = int(entry.name)
                if pid == my_pid:
                    continue
                try:
                    cmdline = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "ignore")
                except OSError:
                    continue
                if root_str in cmdline:
                    try:
                        os.kill(pid, 9)
                    except OSError:
                        pass
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
