from pathlib import Path

import pytest
from pydantic import ValidationError
from yggdrisil import EvaluationResult, SQLiteStateGraph

from yggdrisil_ecoli.analysis import score_rediscovery, summarize_run
from yggdrisil_ecoli.state import GenomeState


def test_summary_uses_active_evidence_and_preserves_trace_audits(
    tmp_path: Path,
) -> None:
    path = tmp_path / "run.sqlite"
    metrics = {
        "essentiality": {"n_essential_deleted": 0},
        "fba": {"feasible": True, "growth_rate": 1.0},
        "genome_size": {"genes_deleted": 0},
        "module_retention": {"n_complete": 2},
    }
    with SQLiteStateGraph(path) as graph:
        graph.save_run(
            "run",
            step=1,
            status="completed",
            metadata={
                "evaluators": {name: name for name in metrics},
                "policy": "agent",
            },
        )
        for name, genes in (("root", []), ("child", ["b0001"])):
            graph.add_state(name, GenomeState(frozenset(genes)))
            for evaluator, values in metrics.items():
                graph.add_evaluation(
                    name,
                    evaluator_id=evaluator,
                    evaluator=evaluator,
                    version="1",
                    config_hash="active",
                    result=EvaluationResult(values),
                )
            # A newer incompatible cached record must not disqualify the child.
            graph.add_evaluation(
                name,
                evaluator_id="stale-fba",
                evaluator="fba",
                version="1",
                config_hash="other",
                result=EvaluationResult({"feasible": False}),
            )
        graph.add_decision(
            "decision",
            run_id="run",
            policy="agent",
            role="explorer",
            model="test",
            selected_state_ids=["root"],
            input_context="Inspect b0001",
            output=None,
            metadata={},
            created_step=1,
            tool_calls=[
                {"role": "tool_call", "tool": "analyze_deletion_bundle"},
                {"role": "tool_return", "tool": "analyze_deletion_bundle"},
                {
                    "role": "usage",
                    "input_tokens": 10,
                    "requests": 1,
                    "cost_usd": "0.01",
                },
                {
                    "role": "usage",
                    "input_tokens": 5,
                    "requests": True,
                    "cost_usd": "0.02",
                },
            ],
        )
    summary = summarize_run(path)
    assert summary["deepest_viable_candidate"]["deleted_gene_ids"] == ["b0001"]
    assert summary["scientific_tool_calls"] == {"analyze_deletion_bundle": 1}
    assert summary["canonical_ids_in_model_io"] == ["decision"]
    assert summary["model_usage"] == {
        "input_tokens": 15,
        "requests": 1,
        "cost_usd": "0.03",
    }


@pytest.mark.parametrize(
    "strain",
    [
        {"deleted_gene_ids": [123], "deletion_intervals": []},
        {"deleted_gene_ids": [], "deletion_intervals": [None]},
        {"deleted_gene_ids": [], "deletion_intervals": [{"gene_ids": "b0001"}]},
    ],
)
def test_rediscovery_rejects_malformed_truth_instead_of_coercing(strain: dict) -> None:
    with pytest.raises(ValidationError):
        score_rediscovery({"b0001"}, {"strains": {"MDS42": strain}})
