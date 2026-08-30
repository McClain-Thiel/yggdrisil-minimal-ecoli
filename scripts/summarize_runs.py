#!/usr/bin/env python3
"""Summarize comparable Yggdrisil E. coli run graphs as JSON."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import mean, median
from typing import Any

from yggdrisil import SQLiteStateGraph
from yggdrisil.types import EvaluationRecord, StateNode

from yggdrisil_ecoli.actions import DeleteGenes
from yggdrisil_ecoli.data.io import atomic_json
from yggdrisil_ecoli.policies import ViabilityGate
from yggdrisil_ecoli.state import GenomeState

_CANONICAL_ID = re.compile(r"\bb\d{4}\b")
_SCIENTIFIC_TOOLS = {
    "list_deletion_candidates",
    "inspect_gene_evidence",
    "analyze_deletion_bundle",
    "inspect_kegg_module",
}


def summarize_run(
    path: Path, validation: dict[str, Any] | None = None
) -> dict[str, object]:
    graph = SQLiteStateGraph[GenomeState, DeleteGenes](path)
    try:
        run = graph.latest_run()
        if run is None:
            raise ValueError(f"graph has no runs: {path}")
        evaluator_ids = _evaluator_ids(run.metadata.get("evaluators"))
        viability_gate = _viability_gate(run.metadata.get("viability_gate"))
        viable = []
        jointly_viable = []
        gate_counts: Counter[str] = Counter()
        for node in graph.states():
            evidence = _active_evidence(graph.evaluations(node.state_id), evaluator_ids)
            fba_positive = _fba_positive(evidence)
            resource_feasible = (
                evidence["resource_allocation"].metrics.get("feasible_at_growth_floor")
                is True
            )
            gate_counts.update(
                {
                    "fba_positive": fba_positive,
                    "resource_feasible": resource_feasible,
                    "jointly_viable": fba_positive and resource_feasible,
                }
            )
            if _viable(evidence, gate=viability_gate):
                viable.append((node, evidence))
            if fba_positive and resource_feasible:
                jointly_viable.append((node, evidence))
        candidate = _best_candidate(viable)
        joint_candidate = _best_candidate(jointly_viable)
        decisions = graph.decisions(run.run_id)
        proposal_events = graph.proposal_events(run_id=run.run_id)
        action_sizes = [len(event.action.genes) for event in proposal_events]
        role_counts = Counter(decision.role for decision in decisions)
        tool_counts: Counter[str] = Counter()
        usage_counts: Counter[str] = Counter()
        cost_usd = Decimal("0")
        canonical_model_io: list[str] = []
        for decision in decisions:
            model_io = json.dumps(
                {
                    "input_context": decision.input_context,
                    "tool_calls": decision.tool_calls,
                },
                default=str,
                sort_keys=True,
            )
            if _CANONICAL_ID.search(model_io):
                canonical_model_io.append(decision.decision_id)
            for event in decision.tool_calls:
                tool = event.get("tool")
                if (
                    event.get("role") == "tool_call"
                    and isinstance(tool, str)
                    and tool in _SCIENTIFIC_TOOLS
                ):
                    tool_counts[tool] += 1
                if event.get("role") == "usage":
                    for key in (
                        "requests",
                        "tool_calls",
                        "input_tokens",
                        "output_tokens",
                        "cache_read_tokens",
                        "cache_write_tokens",
                    ):
                        value = event.get(key)
                        if isinstance(value, int) and not isinstance(value, bool):
                            usage_counts[key] += value
                    cost_usd += _decimal(event.get("cost_usd"))
        agent = run.metadata.get("agent")
        result: dict[str, object] = {
            "graph": str(path.resolve()),
            "run_id": run.run_id,
            "status": run.status,
            "stop_reason": run.metadata.get("stop_reason"),
            "states": len(graph),
            "edges": graph.edge_count(),
            "policy": run.metadata.get("policy"),
            "viability_gate": viability_gate,
            "agent": agent if isinstance(agent, dict) else None,
            "decision_counts": dict(sorted(role_counts.items())),
            "state_gate_counts": {
                **dict(sorted(gate_counts.items())),
                "declared_gate_viable": len(viable),
            },
            "proposal_events": {
                "count": len(proposal_events),
                "outcomes": dict(
                    sorted(Counter(event.outcome for event in proposal_events).items())
                ),
                "action_size": (
                    {
                        "minimum": min(action_sizes),
                        "maximum": max(action_sizes),
                        "mean": mean(action_sizes),
                        "median": median(action_sizes),
                    }
                    if action_sizes
                    else None
                ),
            },
            "scientific_tool_calls": dict(sorted(tool_counts.items())),
            "model_usage": {
                **dict(sorted(usage_counts.items())),
                "cost_usd": str(cost_usd),
            },
            "canonical_ids_in_model_io": canonical_model_io,
            "deepest_viable_candidate": (
                _candidate_summary(
                    candidate[0].state_id, candidate[0].state, candidate[1]
                )
                if candidate is not None
                else None
            ),
            "deepest_jointly_viable_candidate": (
                _candidate_summary(
                    joint_candidate[0].state_id,
                    joint_candidate[0].state,
                    joint_candidate[1],
                )
                if joint_candidate is not None
                else None
            ),
        }
        if candidate is not None and validation is not None:
            result["rediscovery"] = score_rediscovery(
                set(candidate[0].state.deleted_genes), validation
            )
        return result
    finally:
        graph.close()


def _evaluator_ids(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ValueError("run metadata lacks evaluator identities")
    evaluator_ids = {
        key: value
        for key, value in raw.items()
        if isinstance(key, str) and isinstance(value, str)
    }
    required = {
        "essentiality",
        "fba",
        "genome_size",
        "module_retention",
        "resource_allocation",
    }
    if required - evaluator_ids.keys():
        raise ValueError("run metadata lacks required evaluator identities")
    return evaluator_ids


def _best_candidate(
    candidates: list[tuple[StateNode[GenomeState], dict[str, EvaluationRecord]]],
) -> tuple[StateNode[GenomeState], dict[str, EvaluationRecord]] | None:
    return max(
        candidates,
        key=lambda item: (
            len(item[0].state.deleted_genes),
            _number(item[1]["fba"].metrics.get("growth_rate")),
            item[0].state_id,
        ),
        default=None,
    )


def _active_evidence(
    records: list[EvaluationRecord], evaluator_ids: dict[str, str]
) -> dict[str, EvaluationRecord]:
    by_id = {record.evaluator_id: record for record in records}
    missing = [
        name for name, identity in evaluator_ids.items() if identity not in by_id
    ]
    if missing:
        raise ValueError(f"state lacks active evaluations: {sorted(missing)}")
    return {name: by_id[identity] for name, identity in evaluator_ids.items()}


def _viability_gate(raw: object) -> ViabilityGate:
    if raw is None:
        return "fba-rba"
    if raw not in {"fba-rba", "fba-only"}:
        raise ValueError(f"unknown run viability gate: {raw!r}")
    return raw


def _viable(
    evidence: dict[str, EvaluationRecord], *, gate: ViabilityGate = "fba-rba"
) -> bool:
    resource = evidence["resource_allocation"].metrics
    fba_positive = _fba_positive(evidence)
    return fba_positive and (
        gate == "fba-only" or resource.get("feasible_at_growth_floor") is True
    )


def _fba_positive(evidence: dict[str, EvaluationRecord]) -> bool:
    fba = evidence["fba"].metrics
    growth = fba.get("growth_rate")
    return (
        fba.get("feasible") is True
        and isinstance(growth, (int, float))
        and not isinstance(growth, bool)
        and growth > 0
    )


def _candidate_summary(
    state_id: str,
    state: GenomeState,
    evidence: dict[str, EvaluationRecord],
) -> dict[str, object]:
    essentiality = evidence["essentiality"].metrics
    fba = evidence["fba"]
    resource = evidence["resource_allocation"]
    modules = evidence["module_retention"].metrics
    fba_coverage = _metadata_dict(fba.metadata, "coverage")
    resource_coverage = _metadata_dict(resource.metadata, "coverage")
    module_coverage = _metadata_dict(evidence["module_retention"].metadata, "coverage")
    return {
        "state_id": state_id,
        "genes_deleted": len(state.deleted_genes),
        "deleted_gene_ids": sorted(state.deleted_genes),
        "growth_rate": fba.metrics.get("growth_rate"),
        "resource_feasible_at_growth_floor": resource.metrics.get(
            "feasible_at_growth_floor"
        ),
        "resource_growth_rate_floor_h": resource.metrics.get("growth_rate_floor_h"),
        "essential_deleted": essentiality.get("n_essential_deleted"),
        "conditional_essential_deleted": essentiality.get(
            "n_conditional_essential_deleted"
        ),
        "ambiguous_deleted": essentiality.get("n_ambiguous_deleted"),
        "unknown_deleted": essentiality.get("n_unknown_deleted"),
        "modules_complete": modules.get("n_complete"),
        "modules_broken": modules.get("n_broken"),
        "fba_modeled_deletions": fba_coverage.get("deleted_genes_modeled"),
        "fba_unmodeled_deletions": fba_coverage.get("deleted_genes_unmodeled"),
        "resource_modeled_deletions": resource_coverage.get("deleted_genes_modeled"),
        "resource_unmodeled_deletions": resource_coverage.get(
            "deleted_genes_unmodeled"
        ),
        "deletions_with_ko": module_coverage.get("deleted_genes_with_ko"),
        "deletions_without_ko": len(
            _list(module_coverage.get("deleted_genes_without_ko"))
        ),
    }


def _metadata_dict(metadata: dict[str, Any], key: str) -> dict[str, Any]:
    value = metadata.get(key)
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _number(value: object) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return float("-inf")


def _decimal(value: object) -> Decimal:
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return Decimal("0")


def score_rediscovery(
    deleted_gene_ids: set[str], validation: dict[str, Any]
) -> dict[str, object]:
    """Score a candidate against truth sets that were not loaded during search."""

    strains = validation.get("strains")
    if not isinstance(strains, dict):
        raise ValueError("validation artifact lacks strain truth sets")
    scores: dict[str, object] = {}
    for strain_name, raw in sorted(strains.items()):
        if not isinstance(strain_name, str) or not isinstance(raw, dict):
            raise ValueError("validation strain entries must be named objects")
        truth = _string_set(raw.get("deleted_gene_ids"), "deleted_gene_ids")
        overlap = deleted_gene_ids & truth
        union = deleted_gene_ids | truth
        intervals = raw.get("deletion_intervals")
        if not isinstance(intervals, list):
            raise ValueError(f"{strain_name}: deletion_intervals must be a list")
        interval_gene_sets = [
            _string_set(interval.get("gene_ids"), "gene_ids")
            for interval in intervals
            if isinstance(interval, dict)
        ]
        if len(interval_gene_sets) != len(intervals):
            raise ValueError(f"{strain_name}: malformed deletion interval")
        eligible_intervals = [gene_ids for gene_ids in interval_gene_sets if gene_ids]
        intervals_hit = sum(
            bool(deleted_gene_ids & gene_ids) for gene_ids in eligible_intervals
        )
        scores[strain_name] = {
            "published_deleted_genes": len(truth),
            "candidate_genes": len(deleted_gene_ids),
            "overlap_genes": len(overlap),
            "overlap_gene_ids": sorted(overlap),
            "published_deletion_gene_precision": _ratio(
                len(overlap), len(deleted_gene_ids)
            ),
            "published_deletion_gene_recall": _ratio(len(overlap), len(truth)),
            "published_deletion_gene_jaccard": _ratio(len(overlap), len(union)),
            "published_intervals_with_search_genes": len(eligible_intervals),
            "published_intervals_hit": intervals_hit,
            "published_deletion_interval_recall": (
                _ratio(intervals_hit, len(eligible_intervals))
                if eligible_intervals
                else None
            ),
        }
    return scores


def _string_set(value: object, field: str) -> set[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"validation {field} must be a list of strings")
    return set(value)


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("graphs", type=Path, nargs="+")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--validation",
        type=Path,
        help="agent-invisible reduced-genome controls loaded only for scoring",
    )
    args = parser.parse_args()
    validation = None
    if args.validation is not None:
        raw_validation = json.loads(args.validation.read_text())
        if not isinstance(raw_validation, dict):
            raise ValueError("validation artifact must be a JSON object")
        validation = raw_validation
    payload = {
        "runs": [summarize_run(path, validation=validation) for path in args.graphs]
    }
    if args.output is not None:
        atomic_json(args.output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
