#!/usr/bin/env python3
"""Minimal Python stdlib MCP stdio server exposing shared dispatch and status tools."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

import dispatch
import recovery
import run


SERVER_INFO = {"name": "orca-fallback-dispatch", "version": "0.1.0"}
PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "orca_fallback_dispatch",
        "description": (
            "Dispatch a delegated task through orca-fallback with explicit role and lane "
            "failover, cooldown tracking, and durable recovery registration on lane exhaustion."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "worktree": {"type": "string", "description": "Absolute path to the target git worktree."},
                "title": {"type": "string", "description": "Concise task title."},
                "role": {
                    "type": "string",
                    "enum": ["orchestrator", "implementation", "review", "documentation", "research"],
                },
                "lane": {"type": "string", "enum": ["premium", "economy"]},
                "prompt": {"type": "string", "description": "Full task specification (or use spec_file)."},
                "spec_file": {"type": "string", "description": "Optional path to task spec file."},
                "caller_kind": {"type": "string", "enum": ["bot", "automation", "worker", "manual"]},
                "bot": {"type": "string"},
                "chat_id": {"type": "string"},
                "automation_id": {"type": "string"},
                "item_id": {"type": "string"},
                "skip_providers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Explicit provider keys to exclude from this dispatch and any delayed resume.",
                },
                "unattended": {
                    "type": "boolean",
                    "description": "Opt this dispatch record in/out of unattended supervisor recovery (default true).",
                },
                "dry_run": {"type": "boolean"},
            },
            "required": ["worktree", "title", "role", "lane"],
        },
    },
    {
        "name": "orca_fallback_status",
        "description": (
            "Inspect next eligible provider for a role/lane (including cooldowns) or inspect "
            "a persisted recovery record by recovery_id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "lane": {"type": "string"},
                "recovery_id": {"type": "string"},
            },
        },
    },
]


def _handle_tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "orca_fallback_dispatch":
        result = dispatch.dispatch_task(
            worktree=Path(arguments["worktree"]),
            title=str(arguments["title"]),
            role=str(arguments["role"]),
            lane=str(arguments["lane"]),
            spec_file=Path(arguments["spec_file"]) if arguments.get("spec_file") else None,
            prompt=arguments.get("prompt"),
            caller_kind=str(arguments.get("caller_kind") or "bot"),
            bot=arguments.get("bot"),
            chat_id=arguments.get("chat_id"),
            automation_id=arguments.get("automation_id"),
            item_id=arguments.get("item_id"),
            skip_providers=set(str(x) for x in arguments["skip_providers"]) if arguments.get("skip_providers") else None,
            unattended=bool(arguments.get("unattended", True)),
            dry_run=bool(arguments.get("dry_run", False)),
        )
        return {
            "content": [{"type": "text", "text": json.dumps(run.redact_secrets(result), indent=2, ensure_ascii=False)}],
            "isError": False,
        }
    if name == "orca_fallback_status":
        if arguments.get("recovery_id"):
            rec = recovery.RecoverySupervisor().load_record(str(arguments["recovery_id"]))
            payload: Any = rec if rec is not None else {"error": "recovery_record_not_found"}
        elif arguments.get("role") and arguments.get("lane"):
            payload = dispatch.select_next_task_provider(
                str(arguments["role"]),
                str(arguments["lane"]),
                cooldown_path=run.STATE_DIR / "cooldowns.json",
            )
        else:
            raise ValueError("Provide either recovery_id or both role and lane")
        return {
            "content": [{"type": "text", "text": json.dumps(run.redact_secrets(payload), indent=2, ensure_ascii=False)}],
            "isError": False,
        }
    raise ValueError(f"Unknown tool: {name}")


def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    """Process a single MCP JSON-RPC 2.0 request and return a response dict (or None for notifications)."""
    method = message.get("method")
    req_id = message.get("id")
    params = message.get("params") or {}

    if req_id is None:
        return None

    try:
        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            tool_name = str(params.get("name") or "")
            tool_args = params.get("arguments") or {}
            if not isinstance(tool_args, dict):
                raise ValueError("Tool arguments must be an object")
            result = _handle_tool_call(tool_name, tool_args)
        else:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
        return {"jsonrpc": "2.0", "id": req_id, "result": result}
    except Exception as error:
        if method == "tools/call":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": run.redact_text(str(error))}],
                    "isError": True,
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32603, "message": run.redact_text(str(error))},
        }


def _read_message(stream: Any) -> tuple[dict[str, Any] | None, str]:
    """Read either Content-Length framed or newline-delimited JSON-RPC message."""
    while True:
        line = stream.readline()
        if not line:
            return None, "ndjson"
        if isinstance(line, bytes):
            line = line.decode("utf-8")
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith("content-length:"):
            length = int(stripped.split(":", 1)[1].strip())
            while True:
                hdr = stream.readline()
                if isinstance(hdr, bytes):
                    hdr = hdr.decode("utf-8")
                if not hdr or not hdr.strip():
                    break
            body = stream.read(length)
            if isinstance(body, bytes):
                body = body.decode("utf-8")
            return json.loads(body), "lsp"
        return json.loads(stripped), "ndjson"


def _write_message(stream: Any, payload: dict[str, Any], framing: str) -> None:
    encoded = json.dumps(payload, ensure_ascii=False)
    if framing == "lsp":
        raw = encoded.encode("utf-8")
        stream.write(f"Content-Length: {len(raw)}\r\n\r\n{encoded}")
    else:
        stream.write(encoded + "\n")
    stream.flush()


def main() -> int:
    if "--self-test" in sys.argv:
        init_resp = handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        list_resp = handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        print(json.dumps({"initialize": init_resp, "tools_list": list_resp}, indent=2))
        return 0

    while True:
        try:
            msg, framing = _read_message(sys.stdin)
        except json.JSONDecodeError:
            continue
        if msg is None:
            break
        resp = handle_request(msg)
        if resp is not None:
            _write_message(sys.stdout, resp, framing)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
