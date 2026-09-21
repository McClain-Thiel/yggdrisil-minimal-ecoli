"""Summarize search graphs and compare candidates with held-out deletion sets."""

from __future__ import annotations

import json
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, StrictStr, TypeAdapter
from yggdrisil import SQLiteStateGraph
from yggdrisil.types import EvaluationRecord

from yggdrisil_ecoli.actions import DeleteGenes
from yggdrisil_ecoli.scorers.base import passes_growth_gates
from yggdrisil_ecoli.state import GenomeState

_CANONICAL_ID = re.compile(r"\bb\d{4}\b")
_SCIENTIFIC_TOOLS = {
    "list_deletion_candidates",
    "inspect_gene_evidence",
    "analyze_deletion_bundle",
    "inspect_kegg_module",
}
_USAGE_FIELDS = {
    "requests",
    "tool_calls",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
}


class _Interval(BaseModel):
    gene_ids: list[StrictStr]


class _Strain(BaseModel):
    deleted_gene_ids: list[StrictStr]
    deletion_intervals: list[_Interval]


class _Validation(BaseModel):
    strains: dict[StrictStr, _Strain]


def summarize_run(
    path: Path, validation: dict[str, Any] | None = None
) -> dict[str, object]:
    """Report independent evidence, model usage, and the largest eligible deletion set."""
    with SQLiteStateGraph[GenomeState, DeleteGenes](path) as graph:
        run = graph.latest_run()
        if run is None:
            raise ValueError(f"graph has no runs: {path}")
        identities = TypeAdapter(dict[str, str]).validate_python(
            run.metadata.get("evaluators"), strict=True
        )
        required = {
            "essentiality",
            "fba",
            "genome_size",
            "module_retention",
            "resource_allocation",
        }
        if required - identities.keys():
            raise ValueError("run metadata lacks required evaluator identities")
        viable = []
        for node in graph.states():
            evidence: dict[str, EvaluationRecord] = {}
            for name, identity in identities.items():
                record = graph.get_evaluation(node.state_id, identity)
                if record is None:
                    raise ValueError(f"state lacks active evaluation: {name}")
                evidence[name] = record
            if passes_growth_gates(evidence):
                viable.append((node, evidence))
        best = max(
            viable,
            default=None,
            key=lambda item: (
                len(item[0].state.deleted_genes),
                item[1]["fba"].metrics["growth_rate"],
                item[0].state_id,
            ),
        )
        candidate = None
        if best is not None:
            node, evidence = best
            candidate = {
                "state_id": node.state_id,
                "genes_deleted": len(node.state.deleted_genes),
                "deleted_gene_ids": sorted(node.state.deleted_genes),
                "growth_rate": evidence["fba"].metrics["growth_rate"],
                "evaluations": {
                    name: record.metrics for name, record in evidence.items()
                },
                "coverage": {
                    name: record.metadata.get("coverage", {})
                    for name, record in evidence.items()
                },
            }
        decisions = graph.decisions(run.run_id)
        events = [event for decision in decisions for event in decision.tool_calls]
        usage_events = [event for event in events if event.get("role") == "usage"]
        usage: Counter[str] = Counter()
        for event in usage_events:
            usage.update(
                {
                    key: value
                    for key, value in event.items()
                    if key in _USAGE_FIELDS and type(value) is int
                }
            )
        cost = sum(
            (Decimal(event.get("cost_usd") or "0") for event in usage_events),
            Decimal("0"),
        )
        result: dict[str, object] = {
            "graph": str(path.resolve()),
            "run_id": run.run_id,
            "status": run.status,
            "stop_reason": run.metadata.get("stop_reason"),
            "states": len(graph),
            "edges": graph.edge_count(),
            "policy": run.metadata.get("policy"),
            "agent": run.metadata.get("agent"),
            "decision_counts": dict(Counter(decision.role for decision in decisions)),
            "scientific_tool_calls": dict(
                Counter(
                    event["tool"]
                    for event in events
                    if event.get("role") == "tool_call"
                    and isinstance(event.get("tool"), str)
                    and event.get("tool") in _SCIENTIFIC_TOOLS
                )
            ),
            "model_usage": {**usage, "cost_usd": str(cost)},
            "canonical_ids_in_model_io": [
                decision.decision_id
                for decision in decisions
                if _CANONICAL_ID.search(
                    json.dumps(
                        [decision.input_context, decision.tool_calls], default=str
                    )
                )
            ],
            "deepest_viable_candidate": candidate,
        }
        if best is not None and validation is not None:
            result["rediscovery"] = score_rediscovery(
                set(best[0].state.deleted_genes), validation
            )
        return result


def score_rediscovery(
    deleted_gene_ids: set[str], validation: dict[str, Any]
) -> dict[str, object]:
    """Compare a candidate with truth sets that were not loaded during search."""
    strains = _Validation.model_validate(validation, strict=True).strains
    scores: dict[str, object] = {}
    for name, strain in sorted(strains.items()):
        truth = set(strain.deleted_gene_ids)
        overlap = deleted_gene_ids & truth
        intervals = [
            set(interval.gene_ids)
            for interval in strain.deletion_intervals
            if interval.gene_ids
        ]
        hits = sum(bool(deleted_gene_ids & genes) for genes in intervals)
        scores[name] = {
            "published_deleted_genes": len(truth),
            "candidate_genes": len(deleted_gene_ids),
            "overlap_genes": len(overlap),
            "overlap_gene_ids": sorted(overlap),
            "published_deletion_gene_precision": _ratio(
                len(overlap), len(deleted_gene_ids)
            ),
            "published_deletion_gene_recall": _ratio(len(overlap), len(truth)),
            "published_deletion_gene_jaccard": _ratio(
                len(overlap), len(deleted_gene_ids | truth)
            ),
            "published_intervals_with_search_genes": len(intervals),
            "published_intervals_hit": hits,
            "published_deletion_interval_recall": _ratio(hits, len(intervals))
            if intervals
            else None,
        }
    return scores


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0
