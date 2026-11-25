#!/usr/bin/env python3
"""Discover available models and manage user-selected role fallback chains."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
from typing import Any
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("ORCA_FALLBACK_CONFIG", ROOT / "settings.json"))
if not CONFIG_PATH.exists():
    CONFIG_PATH = ROOT / "settings.example.json"
SETTINGS = json.loads(CONFIG_PATH.read_text())


def configured_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


CATALOG_PATH = configured_path(SETTINGS["catalog"])
SELECTION_PATH = configured_path(SETTINGS["selection"])
EXAMPLE_SELECTION_PATH = ROOT / "selection.example.json"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, data: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(temporary, path)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def load_selection() -> dict:
    path = SELECTION_PATH if SELECTION_PATH.exists() else EXAMPLE_SELECTION_PATH
    return load_json(path)


def discover_ocx(url: str) -> list[dict]:
    with urlopen(url, timeout=15) as response:
        payload = json.load(response)
    models = []
    models_by_key = {}
    live_pairs = set()
    live_ids = set()
    for item in payload.get("data", []):
        capabilities = item.get("capabilities", {})
        efforts = item.get("reasoning_efforts", [])
        model_id = item["id"]
        provider = item.get("owned_by", "ocx")
        if provider == "system" and "/" in model_id:
            provider = model_id.split("/", 1)[0]
        live_ids.add(model_id)
        if model_id.startswith(provider + "/"):
            live_pairs.add((provider, model_id[len(provider) + 1 :]))
        else:
            live_pairs.add((provider, model_id))
        entry = {
                "key": f"codex:{model_id}",
                "agent": "codex",
                "model": model_id,
                "provider": provider,
                "label": model_id,
                "source": "ocx",
                "available": True,
                "input_modalities": capabilities.get("input_modalities", ["text"]),
                "efforts": [entry["value"] for entry in efforts if "value" in entry],
            }
        models.append(entry)
        models_by_key[entry["key"]] = entry
    executable = SETTINGS["sources"].get("ocx_command")
    if executable:
        saved = subprocess.run(
            [executable, "models", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        saved_payload = json.loads(saved.stdout)
        for item in saved_payload.get("models", []):
            pair = (item["provider"], item["model"])
            provider, model_name = pair
            model_id = f"{provider}/{model_name}"
            target = models_by_key.get(f"codex:{model_id}")
            if target is None and provider == "openai":
                target = models_by_key.get(f"codex:{model_name}")
            if target is not None:
                target["context_window"] = item.get("contextWindow")
                if item.get("inputModalities"):
                    target["input_modalities"] = item["inputModalities"]
                if item.get("reasoningEfforts"):
                    target["efforts"] = item["reasoningEfforts"]
                if item.get("price"):
                    target["pricing"] = item["price"]
                continue
            if pair in live_pairs or model_id in live_ids:
                continue
            discovery = subprocess.run(
                [executable, "provider", "test", provider, "--json"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            try:
                health = json.loads(discovery.stdout)
            except json.JSONDecodeError:
                health = {"ok": False, "error": "provider health returned invalid JSON"}
            models.append(
                {
                    "key": f"codex:{model_id}",
                    "agent": "codex",
                    "model": model_id,
                    "provider": provider,
                    "label": model_id,
                    "source": "ocx",
                    "available": False,
                    "status": "configured_unavailable",
                    "error": health.get("error", "not present in live model catalog"),
                    "input_modalities": item.get("inputModalities") or ["text"],
                    "efforts": item.get("reasoningEfforts") or [],
                    "context_window": item.get("contextWindow"),
                    **({"pricing": item["price"]} if item.get("price") else {}),
                }
            )
    return models


def discover_agy(executable: str) -> list[dict]:
    completed = subprocess.run(
        [executable, "models"],
        capture_output=True,
        text=True,
        timeout=45,
        check=True,
    )
    models = []
    for line in completed.stdout.splitlines():
        if "\t" not in line:
            continue
        model_id, label = line.split("\t", 1)
        models.append(
            {
                "key": f"antigravity:{model_id.strip()}",
                "agent": "antigravity",
                "model": model_id.strip(),
                "provider": "antigravity",
                "label": label.strip(),
                "source": "agy",
                "available": True,
                "input_modalities": ["text", "image"],
                "efforts": [],
            }
        )
    return models


def unavailable_selections(catalog: dict, selection: dict) -> dict:
    available = {model["key"] for model in catalog["models"]}
    result = {}
    for role, config in selection.get("roles", {}).items():
        entries = config if isinstance(config, list) else [
            entry for lane in config.get("lanes", {}).values() for entry in lane["models"]
        ]
        missing = [entry["key"] for entry in entries if entry["key"] not in available]
        if missing:
            result[role] = missing
    return result


def role_lane(role: str, lane: str | None = None) -> tuple[str, dict]:
    config = load_selection()["roles"][role]
    if isinstance(config, list):
        return "legacy", {"models": config, "budget": {}, "allow_lane_escalation": False}
    lane_name = lane or config.get("default_lane", "default")
    try:
        selected = config["lanes"][lane_name]
    except KeyError as error:
        raise ValueError(f"Unknown lane {lane_name} for {role}") from error
    return lane_name, {
        **selected,
        "allow_lane_escalation": config.get("allow_lane_escalation", False),
    }


def comparison_price(model: dict) -> float | None:
    costs = model.get("pricing", {}).get("cost4", {})
    input_price, output_price = costs.get("input"), costs.get("output")
    if isinstance(input_price, (int, float)) and isinstance(output_price, (int, float)):
        return 0.75 * input_price + 0.25 * output_price
    costs = model.get("openrouter", {}).get("pricing_per_million", {})
    input_price, output_price = costs.get("input"), costs.get("output")
    if isinstance(input_price, (int, float)) and isinstance(output_price, (int, float)):
        return 0.75 * input_price + 0.25 * output_price
    return None


def policy_exclusions(model: dict, budget: dict, intelligence: float | None) -> list[str]:
    reasons = []
    maximum = budget.get("max_comparison_price_usd")
    if maximum is not None:
        price = comparison_price(model)
        if price is None:
            reasons.append("price_unknown")
        elif price > maximum:
            reasons.append(f"price_above_{maximum}")
    minimum = budget.get("min_intelligence_index")
    if minimum is not None:
        if intelligence is None:
            reasons.append("quality_unknown")
        elif intelligence < minimum:
            reasons.append(f"quality_below_{minimum}")
    return reasons


def refresh(quiet: bool = False) -> dict:
    sources = SETTINGS["sources"]
    previous = load_json(CATALOG_PATH) if CATALOG_PATH.exists() else {"models": []}
    models = []
    source_status = {}
    for name, discover, argument in (
        ("ocx", discover_ocx, sources["ocx_url"]),
        ("agy", discover_agy, sources["agy_command"]),
    ):
        try:
            discovered = discover(argument)
            if not discovered:
                raise RuntimeError("source returned no models")
            models.extend(discovered)
            source_status[name] = {"ok": True, "count": len(discovered)}
        except Exception as error:
            retained = [m for m in previous["models"] if m["source"] == name]
            models.extend(retained)
            source_status[name] = {
                "ok": False,
                "error": str(error),
                "stale_count": len(retained),
            }
    if not models:
        raise RuntimeError("No model source returned a usable catalog")
    old_keys = {model["key"] for model in previous["models"]}
    new_keys = {model["key"] for model in models}
    catalog = {
        "version": 1,
        "refreshed_at": now(),
        "sources": source_status,
        "models": sorted(models, key=lambda model: model["key"]),
    }
    atomic_json(CATALOG_PATH, catalog)
    selection = load_selection()
    summary = {
        "model_count": len(models),
        "unavailable_count": sum(not model.get("available", True) for model in models),
        "added": sorted(new_keys - old_keys),
        "removed": sorted(old_keys - new_keys),
        "sources": source_status,
        "unavailable_selections": unavailable_selections(catalog, selection),
    }
    if not quiet:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    return summary


def resolve_policy(role: str, lane: str | None = None) -> tuple[list[dict], dict]:
    catalog = load_json(CATALOG_PATH)
    models = {model["key"]: model for model in catalog["models"]}
    lane_name, lane_config = role_lane(role, lane)
    entries = lane_config["models"]
    benchmarks_path = ROOT / "benchmarks.json"
    benchmarks = load_json(benchmarks_path) if benchmarks_path.exists() else {"entries": []}
    intelligence = {
        entry["model_key"]: entry["score"]
        for entry in benchmarks["entries"]
        if entry.get("benchmark") == "artificial_analysis.intelligence_index"
    }
    providers = []
    skipped = {}
    for entry in entries:
        key = entry["key"]
        if key not in models:
            raise ValueError(
                f"Selected model {key} is unavailable for {role}. "
                "Refresh the catalog and choose a replacement; no automatic substitution was made."
            )
        model = models[key]
        reasons = policy_exclusions(model, lane_config.get("budget", {}), intelligence.get(key))
        if reasons:
            skipped[key] = reasons
            continue
        providers.append(
            {
                "name": key,
                "agent": model["agent"],
                "model": model["model"],
                "lane": lane_name,
                **({"effort": entry["effort"]} if entry.get("effort") else {}),
            }
        )
    report = {
        "role": role,
        "lane": lane_name,
        "budget": lane_config.get("budget", {}),
        "allow_lane_escalation": lane_config["allow_lane_escalation"],
        "policy_skipped": skipped,
    }
    return providers, report


def resolve_providers(role: str, lane: str | None = None) -> list[dict]:
    providers, _ = resolve_policy(role, lane)
    if not providers:
        raise ValueError(f"Role {role} lane {lane or 'default'} has no eligible models")
    return providers


def selected_entries(role: str, lane: str | None = None) -> list[dict]:
    _, config = role_lane(role, lane)
    return config["models"]


def set_selected_entries(selection: dict, role: str, lane: str | None, entries: list[dict]) -> None:
    config = selection["roles"][role]
    if isinstance(config, list):
        selection["roles"][role] = entries
    else:
        lane_name = lane or config["default_lane"]
        selection["roles"][role]["lanes"][lane_name]["models"] = entries


def list_models(role: str | None, lane: str | None = None) -> None:
    catalog = load_json(CATALOG_PATH)
    models = catalog["models"]
    selected = {entry["key"] for entry in selected_entries(role, lane)} if role else set()
    for index, model in enumerate(models, 1):
        marker = "*" if model["key"] in selected else " "
        health = "" if model.get("available", True) else "  [unavailable]"
        print(f"{index:3} {marker} {model['key']}  [{model['label']}]{health}")
    if role:
        chain = selected_entries(role, lane)
        print(f"\nSelected fallback order ({lane or role_lane(role)[0]}):")
        for index, entry in enumerate(chain, 1):
            effort = f" effort={entry['effort']}" if entry.get("effort") else ""
            print(f"  {index}. {entry['key']}{effort}")


def select(role: str, keys: list[str], lane: str | None = None) -> None:
    catalog = load_json(CATALOG_PATH)
    selection = load_selection()
    available = {model["key"]: model for model in catalog["models"]}
    entries = []
    seen = set()
    for value in keys:
        key, separator, effort = value.partition("=")
        if key not in available:
            raise ValueError(f"Unknown model key: {key}")
        if key in seen:
            raise ValueError(f"Duplicate model in fallback chain: {key}")
        supported_efforts = available[key].get("efforts", [])
        if separator and effort not in supported_efforts:
            raise ValueError(f"Unsupported effort {effort!r} for {key}")
        seen.add(key)
        entries.append({"key": key, **({"effort": effort} if separator else {})})
    if not entries:
        raise ValueError("Choose at least one model")
    set_selected_entries(selection, role, lane, entries)
    atomic_json(SELECTION_PATH, selection)
    print(json.dumps({role: entries}, indent=2, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    refresh_parser = commands.add_parser("refresh")
    refresh_parser.add_argument("--quiet", action="store_true")
    list_parser = commands.add_parser("list")
    list_parser.add_argument("--role")
    list_parser.add_argument("--lane")
    select_parser = commands.add_parser("select")
    select_parser.add_argument("--role", required=True)
    select_parser.add_argument("--lane")
    select_parser.add_argument("--models", nargs="+", required=True)
    args = parser.parse_args()
    try:
        if args.command == "refresh":
            refresh(args.quiet)
        elif args.command == "list":
            list_models(args.role, args.lane)
        else:
            select(args.role, args.models, args.lane)
    except Exception as error:
        parser.exit(1, f"error: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
