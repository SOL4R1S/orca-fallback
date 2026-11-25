#!/usr/bin/env python3
"""Rank model candidates using sourced evidence without changing selections."""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import statistics
import subprocess
import tempfile
from typing import Any

from catalog import (
    CATALOG_PATH, ROOT, SELECTION_PATH, atomic_json, comparison_price, load_json, load_selection,
    policy_exclusions, role_lane, selected_entries, set_selected_entries,
)


STATE_DIR = Path.home() / ".local" / "state" / "orca-fallback"
PERFORMANCE_PATH = STATE_DIR / "performance.jsonl"
BENCHMARKS_PATH = ROOT / "benchmarks.json"
PROFILES = {
    "orchestrator": {"quality": 0.45, "reliability": 0.30, "cost": 0.10, "speed": 0.15},
    "implementation": {"quality": 0.45, "reliability": 0.30, "cost": 0.15, "speed": 0.10},
    "review": {"quality": 0.55, "reliability": 0.30, "cost": 0.05, "speed": 0.10},
    "documentation": {"quality": 0.20, "reliability": 0.25, "cost": 0.40, "speed": 0.15},
    "research": {"quality": 0.35, "reliability": 0.25, "cost": 0.20, "speed": 0.20},
}
ROLE_BENCHMARKS = {
    "orchestrator": {
        "artificial_analysis.agentic_index": 0.6,
        "artificial_analysis.intelligence_index": 0.4,
    },
    "implementation": {
        "artificial_analysis.coding_index": 0.5,
        "artificial_analysis.intelligence_index": 0.25,
        "design_arena.models.codecategories.elo": 0.25,
    },
    "review": {
        "artificial_analysis.engineering_index": 0.5,
        "artificial_analysis.intelligence_index": 0.5,
    },
    "documentation": {"artificial_analysis.intelligence_index": 1.0},
    "research": {
        "artificial_analysis.intelligence_index": 0.8,
        "artificial_analysis.multilingual_index": 0.2,
    },
}


def performance() -> dict:
    rows = defaultdict(list)
    if PERFORMANCE_PATH.exists():
        for line in PERFORMANCE_PATH.read_text().splitlines():
            try:
                row = json.loads(line)
                rows[(row["role"], row["model_key"])].append(row)
            except (json.JSONDecodeError, KeyError):
                continue
    result = {}
    for key, attempts in rows.items():
        recent = attempts[-20:]
        latencies = [row["latency_seconds"] for row in recent if row.get("latency_seconds") is not None]
        result[key] = {
            "attempts": len(recent),
            "success_rate": sum(row["outcome"] == "succeeded" for row in recent) / len(recent),
            "median_latency_seconds": statistics.median(latencies) if latencies else None,
        }
    return result


def price_per_million(model: dict) -> float | None:
    return comparison_price(model)


def benchmark_scores(key: str, benchmarks: dict) -> tuple[dict, list[str]]:
    scores = {}
    sources = []
    for entry in benchmarks.get("entries", []):
        if entry.get("model_key") != key:
            continue
        if not all(entry.get(field) for field in ("benchmark", "source_url", "verified_at")):
            continue
        score = entry.get("score")
        if not isinstance(score, (int, float)) or not math.isfinite(score):
            continue
        scores[entry["benchmark"]] = score
        sources.append(entry["source_url"])
    return scores, sorted(set(sources))


def normalized(values: dict[str, float], *, lower_is_better: bool = False) -> dict[str, float | None]:
    if not values:
        return {}
    if len(values) == 1:
        return {next(iter(values)): 0.5}
    low, high = min(values.values()), max(values.values())
    if math.isclose(low, high):
        return {key: 0.5 for key in values}
    return {
        key: ((high - value) if lower_is_better else (value - low)) / (high - low)
        for key, value in values.items()
    }


def rank(role: str, provider: str | None = None, limit: int = 10, lane: str | None = None) -> dict:
    if role not in PROFILES:
        raise ValueError(f"Unsupported recommendation role: {role}")
    catalog = load_json(CATALOG_PATH)
    benchmarks = load_json(BENCHMARKS_PATH) if BENCHMARKS_PATH.exists() else {"entries": []}
    lane_name, lane_config = role_lane(role, lane)
    measurements = performance()
    candidates = []
    for model in catalog["models"]:
        if not model.get("available", False):
            continue
        if provider and model["provider"] != provider:
            continue
        scores, sources = benchmark_scores(model["key"], benchmarks)
        exclusions = policy_exclusions(
            model,
            lane_config.get("budget", {}),
            scores.get("artificial_analysis.intelligence_index"),
        )
        if exclusions:
            continue
        candidates.append(
            {
                "key": model["key"],
                "provider": model["provider"],
                "agent": model["agent"],
                "model": model["model"],
                "context_window": model.get("context_window"),
                "input_modalities": model.get("input_modalities", []),
                "comparison_price_usd": price_per_million(model),
                "pricing": model.get("pricing"),
                "openrouter": model.get("openrouter"),
                "benchmark_scores": scores,
                "benchmark_sources": sources,
                "local": measurements.get((role, model["key"])),
            }
        )
    metrics = {
        "cost": normalized(
            {c["key"]: c["comparison_price_usd"] for c in candidates if c["comparison_price_usd"] is not None},
            lower_is_better=True,
        ),
        "reliability": normalized(
            {c["key"]: c["local"]["success_rate"] for c in candidates if c["local"]}
        ),
        "speed": normalized(
            {
                c["key"]: c["local"]["median_latency_seconds"]
                for c in candidates
                if c["local"] and c["local"]["median_latency_seconds"] is not None
            },
            lower_is_better=True,
        ),
    }
    benchmark_names = [
        name for name in ROLE_BENCHMARKS[role]
        if sum(name in c["benchmark_scores"] for c in candidates) >= 2
    ]
    normalized_benchmarks = {
        name: normalized(
            {c["key"]: c["benchmark_scores"][name] for c in candidates if name in c["benchmark_scores"]}
        )
        for name in benchmark_names
    }
    for candidate in candidates:
        candidate["normalized_benchmark_scores"] = {
            name: scores[candidate["key"]]
            for name, scores in normalized_benchmarks.items()
            if candidate["key"] in scores and scores[candidate["key"]] is not None
        }
    quality_values = {}
    total_quality_weight = sum(ROLE_BENCHMARKS[role][name] for name in benchmark_names)
    for candidate in candidates:
        scores = candidate["normalized_benchmark_scores"]
        if scores:
            present_weight = sum(ROLE_BENCHMARKS[role][name] for name in scores)
            weighted_score = sum(scores[name] * ROLE_BENCHMARKS[role][name] for name in scores)
            quality_values[candidate["key"]] = (
                weighted_score / present_weight, present_weight / total_quality_weight
            )
    weights = PROFILES[role]
    ranked = []
    for candidate in candidates:
        key = candidate["key"]
        contributions = {}
        unknown = []
        for metric in ("cost", "reliability", "speed"):
            score = metrics[metric].get(key)
            if score is None:
                unknown.append(metric)
            else:
                contributions[metric] = round(weights[metric] * score, 4)
        quality_score = quality_values.get(key)
        if quality_score is not None:
            score, ratio = quality_score
            candidate["quality_evidence_ratio"] = round(ratio, 2)
            contributions["quality"] = round(weights["quality"] * ratio * score, 4)
            candidate["evidence_coverage_quality"] = round(weights["quality"] * ratio, 4)
        else:
            unknown.append("quality")
        known_weight = sum(weights[metric] for metric in contributions if metric != "quality")
        known_weight += candidate.get("evidence_coverage_quality", 0)
        candidate["evidence_coverage"] = round(known_weight, 2)
        candidate["partial_score"] = round(sum(contributions.values()), 4)
        candidate["score_is_comparable"] = known_weight == 1.0
        candidate["unknown_metrics"] = unknown
        candidate["current_selection_rank"] = current_rank(role, key, lane_name)
        ranked.append(candidate)
    ranked.sort(
        key=lambda c: (
            -c["evidence_coverage"],
            -c["partial_score"],
            c["current_selection_rank"] if c["current_selection_rank"] is not None else 9999,
            c["key"],
        )
    )
    groups = {}
    for candidate in ranked:
        group = (candidate.get("openrouter") or {}).get("id") or candidate["key"]
        groups.setdefault(group, []).append(candidate)
    recommendations = []
    for routes in groups.values():
        routes.sort(
            key=lambda c: (
                -c["partial_score"],
                c["current_selection_rank"] if c["current_selection_rank"] is not None else 9999,
                c["key"],
            )
        )
        primary = routes[0]
        primary["alternative_routes"] = [candidate["key"] for candidate in routes[1:]]
        recommendations.append(primary)
    recommendations.sort(
        key=lambda c: (-c["evidence_coverage"], -c["partial_score"], c["key"])
    )
    return {
        "role": role,
        "lane": lane_name,
        "budget": lane_config.get("budget", {}),
        "candidate_count": len(candidates),
        "model_group_count": len(groups),
        "ranking_policy": "Evidence coverage first, then partial score. Missing metrics never count as good scores.",
        "price_policy": "75% input + 25% output per million tokens from OCX or OpenRouter estimates. Plan billing may differ.",
        "recommendations": recommendations[:limit],
        "selection_changed": False,
    }


def ai_explain(result: dict, role: str) -> dict:
    orchestrator = selected_entries("orchestrator")[0]
    model_key = orchestrator["key"]
    if not model_key.startswith("codex:"):
        return {"error": "AI explanation currently requires a Codex orchestrator selection"}
    model = model_key.removeprefix("codex:")
    schema = {
        "type": "object",
        "properties": {
            "recommendations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "model_key": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["model_key", "reason"],
                    "additionalProperties": False,
                },
            },
            "cautions": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["recommendations", "cautions"],
        "additionalProperties": False,
    }
    prompt = (
        "You recommend models for an Orca bot role. Use only the supplied evidence. "
        "Do not invent benchmarks, prices, or model capabilities. Missing evidence must remain a caution. "
        "Explain recommendations in concise Korean. You cannot change files or selections. "
        f"Bot role: {role}. Evidence: "
        + json.dumps(result["recommendations"], ensure_ascii=False)
    )
    with tempfile.TemporaryDirectory(prefix="orca-model-recommendation-") as directory:
        schema_path = Path(directory) / "schema.json"
        output_path = Path(directory) / "answer.json"
        schema_path.write_text(json.dumps(schema))
        completed = subprocess.run(
            [
                "codex", "exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only",
                "-m", model, "--output-schema", str(schema_path), "-o", str(output_path), "-",
            ],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=150,
            check=False,
        )
        if completed.returncode or not output_path.exists():
            return {"error": completed.stderr[-1000:] or "AI explanation returned no output"}
        answer = json.loads(output_path.read_text())
    allowed = {candidate["key"] for candidate in result["recommendations"]}
    answer["recommendations"] = [
        item for item in answer["recommendations"] if item["model_key"] in allowed
    ]
    return answer


def current_rank(role: str, key: str, lane: str | None = None) -> int | None:
    keys = [entry["key"] for entry in selected_entries(role, lane)]
    return keys.index(key) + 1 if key in keys else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=sorted(PROFILES), required=True)
    parser.add_argument("--provider")
    parser.add_argument("--lane")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--format", choices=("json", "text"), default="text")
    parser.add_argument("--apply", action="store_true", help="Apply the displayed order after explicit user review")
    parser.add_argument("--ai", action="store_true", help="Ask the orchestrator model to explain the evidence")
    args = parser.parse_args()
    result = rank(args.role, args.provider, max(1, min(args.limit, 50)), args.lane)
    if args.apply:
        selection = load_selection()
        existing = {entry["key"]: entry for entry in selected_entries(args.role, args.lane)}
        entries = [
            {
                "key": candidate["key"],
                **({"effort": existing[candidate["key"]]["effort"]} if candidate["key"] in existing and existing[candidate["key"]].get("effort") else {}),
            }
            for candidate in result["recommendations"][:3]
        ]
        set_selected_entries(selection, args.role, args.lane, entries)
        atomic_json(SELECTION_PATH, selection)
        result["selection_changed"] = True
    if args.ai:
        result["ai_explanation"] = ai_explain(result, args.role)
    if args.format == "json":
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"Role: {args.role} ({result['candidate_count']} available candidates)")
        for index, candidate in enumerate(result["recommendations"], 1):
            local = candidate["local"] or {}
            price = candidate["comparison_price_usd"]
            print(f"\n{index}. {candidate['key']}")
            print(f"   evidence={candidate['evidence_coverage']} partial_score={candidate['partial_score']}")
            print(f"   price={price if price is not None else 'unknown'} local={local or 'unknown'}")
            print(f"   benchmarks={candidate['benchmark_scores'] or 'unknown'} unknown={candidate['unknown_metrics']}")
        print("\nSelection changed:", result["selection_changed"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
