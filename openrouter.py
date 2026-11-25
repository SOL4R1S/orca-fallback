#!/usr/bin/env python3
"""Collect OpenRouter prices and benchmarks with conservative model matching."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from urllib.request import urlopen

from catalog import (
    CATALOG_PATH, ROOT, SETTINGS, atomic_json, load_json, now,
)


BENCHMARKS_PATH = ROOT / "benchmarks.json"
ALIASES_PATH = ROOT / "model_aliases.json"


def candidate_openrouter_ids(model: dict) -> list[str]:
    key = model["key"]
    aliases = load_json(ALIASES_PATH) if ALIASES_PATH.exists() else {}
    if key in aliases:
        return [aliases[key]]
    model_id = model["model"]
    provider = model["provider"]
    if model["agent"] == "antigravity":
        base = model_id
        for suffix in ("-xhigh", "-high", "-medium", "-low"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        return [f"google/{base}"]
    if provider == "openai":
        return [f"openai/{model_id.removesuffix('--fast')}"]
    if provider == "kimi-code":
        return []
    if "/" not in model_id:
        return []
    _, upstream = model_id.split("/", 1)
    candidates = []
    if provider == "command-code":
        candidates.append(upstream)
        if "/" not in upstream:
            candidates.append(f"openai/{upstream}")
    elif provider.startswith("alibaba-"):
        if upstream.startswith("deepseek-"):
            candidates.append(f"deepseek/{upstream}")
        elif upstream.startswith("qwen"):
            candidates.append(f"qwen/{upstream}")
        elif upstream.startswith("glm-"):
            candidates.append(f"z-ai/{upstream}")
    return candidates


def match_models(catalog_models: list[dict], openrouter_models: list[dict]) -> tuple[dict, dict]:
    available = {
        item["id"].casefold(): item
        for item in openrouter_models
        if not item["id"].startswith("~")
        and not item["id"].endswith(":batch")
        and not item.get("alias_target")
    }
    matches = {}
    unmatched = {}
    for model in catalog_models:
        candidates = candidate_openrouter_ids(model)
        match = next((available[value.casefold()] for value in candidates if value.casefold() in available), None)
        if match:
            matches[model["key"]] = match
        else:
            unmatched[model["key"]] = candidates
    return matches, unmatched


def collect(quiet: bool = False) -> dict:
    url = SETTINGS["sources"].get("openrouter_url", "https://openrouter.ai/api/v1/models")
    with urlopen(url, timeout=45) as response:
        raw = response.read()
    payload = json.loads(raw)
    catalog = load_json(CATALOG_PATH)
    matches, unmatched = match_models(catalog["models"], payload.get("data", []))
    entries = []
    for model in catalog["models"]:
        model.pop("openrouter", None)
        match = matches.get(model["key"])
        if not match:
            continue
        page_url = f"https://openrouter.ai/{match['id']}"
        pricing = match.get("pricing", {})
        model["openrouter"] = {
            "id": match["id"],
            "canonical_slug": match.get("canonical_slug"),
            "source_url": page_url,
            "context_length": match.get("context_length"),
            "pricing_per_million": {
                "input": round(float(pricing["prompt"]) * 1_000_000, 8) if pricing.get("prompt") else None,
                "output": round(float(pricing["completion"]) * 1_000_000, 8) if pricing.get("completion") else None,
            },
        }
        benchmarks = match.get("benchmarks") or {}
        for name, score in (benchmarks.get("artificial_analysis") or {}).items():
            if isinstance(score, (int, float)):
                entries.append(
                    {
                        "model_key": model["key"],
                        "benchmark": f"artificial_analysis.{name}",
                        "score": score,
                        "source_url": page_url,
                        "source_model_id": match["id"],
                        "verified_at": now(),
                    }
                )
        for row in benchmarks.get("design_arena") or []:
            if not all(row.get(field) for field in ("arena", "category")):
                continue
            if isinstance(row.get("elo"), (int, float)):
                entries.append(
                    {
                        "model_key": model["key"],
                        "benchmark": f"design_arena.{row['arena']}.{row['category']}.elo",
                        "score": row["elo"],
                        "source_url": page_url,
                        "source_model_id": match["id"],
                        "verified_at": now(),
                    }
                )
    atomic_json(CATALOG_PATH, catalog)
    benchmark_data = {
        "version": 1,
        "source": "OpenRouter public models API",
        "source_url": url,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "refreshed_at": now(),
        "entries": entries,
    }
    atomic_json(BENCHMARKS_PATH, benchmark_data)
    cache = Path.home() / ".cache" / "orca-fallback"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "openrouter-models.json").write_bytes(raw)
    summary = {
        "openrouter_models": len(payload.get("data", [])),
        "matched_local_models": len(matches),
        "benchmark_entries": len(entries),
        "unmatched_local_models": len(unmatched),
    }
    if not quiet:
        print(json.dumps(summary, indent=2))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["collect"])
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    collect(args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
