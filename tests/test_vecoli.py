from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from scipy import sparse
from yggdrisil.serialize import dumps

from yggdrisil_ecoli.data.registry import GeneRecord, GeneRegistry
from yggdrisil_ecoli.state import GenomeState, genome_state_key
from yggdrisil_ecoli.vecoli import (
    Finalist,
    build_workflow_config,
    install_vecoli_adapter,
    map_finalists,
    select_diverse_finalists,
    select_finalists,
)
from yggdrisil_ecoli.vecoli_results import summarize_vecoli_lineages

FBA_ID = "fba-active"
RESOURCE_ID = "resource-active"


def _finalist(name: str, genes: set[str], growth: float = 0.8) -> Finalist:
    return Finalist(name, frozenset(genes), growth, FBA_ID, RESOURCE_ID)


def test_diverse_selection_starts_deepest_then_spreads() -> None:
    candidates = (
        _finalist("deep", {"b0001", "b0002", "b0003", "b0004"}),
        _finalist("near", {"b0001", "b0002", "b0003"}),
        _finalist("branch-a", {"b0001", "b0002", "b0005"}),
        _finalist("branch-b", {"b0003", "b0004", "b0006"}),
    )

    selected = select_diverse_finalists(candidates, count=3, deletion_band=0.5)

    assert [item.state_id for item in selected] == ["deep", "branch-a", "branch-b"]


def test_graph_selection_uses_only_active_viability_evidence(tmp_path: Path) -> None:
    graph = tmp_path / "run.sqlite"
    connection = sqlite3.connect(graph)
    connection.executescript(
        """
        CREATE TABLE runs (
            run_id TEXT, status TEXT, metadata_json TEXT, created_at TEXT
        );
        CREATE TABLE states (state_id TEXT, state_json TEXT);
        CREATE TABLE evaluations (
            state_id TEXT, evaluator_id TEXT, metrics_json TEXT
        );
        CREATE TABLE proposal_events (
            run_id TEXT, parent_id TEXT, child_id TEXT
        );
        """
    )
    connection.execute(
        "INSERT INTO runs VALUES (?, ?, ?, ?)",
        (
            "run-1",
            "completed",
            dumps(
                {
                    "evaluators": {
                        "fba": FBA_ID,
                        "resource_allocation": RESOURCE_ID,
                    }
                }
            ),
            "2026-08-28T00:00:00Z",
        ),
    )
    states = [
        GenomeState(frozenset({"b0001"})),
        GenomeState(frozenset({"b0001", "b0002"})),
        GenomeState(frozenset({"b0003", "b0004"})),
    ]
    for state in states:
        state_id = genome_state_key(state)
        connection.execute("INSERT INTO states VALUES (?, ?)", (state_id, dumps(state)))
        connection.execute(
            "INSERT INTO evaluations VALUES (?, ?, ?)",
            (state_id, FBA_ID, dumps({"feasible": True, "growth_rate": 0.8})),
        )
        connection.execute(
            "INSERT INTO evaluations VALUES (?, ?, ?)",
            (
                state_id,
                RESOURCE_ID,
                dumps({"feasible_at_growth_floor": state is not states[1]}),
            ),
        )
        connection.execute(
            "INSERT INTO evaluations VALUES (?, ?, ?)",
            (
                state_id,
                "resource-stale",
                dumps({"feasible_at_growth_floor": True}),
            ),
        )
    connection.execute(
        "INSERT INTO proposal_events VALUES (?, ?, ?)",
        ("run-1", genome_state_key(states[0]), genome_state_key(states[2])),
    )
    outside_run = GenomeState(frozenset({"b0001", "b0002", "b0003"}))
    outside_id = genome_state_key(outside_run)
    connection.execute(
        "INSERT INTO states VALUES (?, ?)", (outside_id, dumps(outside_run))
    )
    connection.execute(
        "INSERT INTO evaluations VALUES (?, ?, ?)",
        (outside_id, FBA_ID, dumps({"feasible": True, "growth_rate": 0.9})),
    )
    connection.execute(
        "INSERT INTO evaluations VALUES (?, ?, ?)",
        (outside_id, RESOURCE_ID, dumps({"feasible_at_growth_floor": True})),
    )
    connection.commit()
    connection.close()

    provenance, selected = select_finalists(graph, count=2, deletion_band=0.5)

    assert {item.deleted_genes for item in selected} == {
        frozenset({"b0001"}),
        frozenset({"b0003", "b0004"}),
    }
    assert provenance["validation_inputs_loaded"] == []
    assert provenance["active_evaluator_ids"] == {
        "fba": FBA_ID,
        "resource_allocation": RESOURCE_ID,
    }


def test_mapping_and_workflow_keep_exact_variant_order(tmp_path: Path) -> None:
    registry = GeneRegistry(
        [
            _record("b0001", "EG1"),
            _record("b0002", "EG2"),
            _record("b0003", "EG3"),
        ]
    )
    finalists = (
        _finalist("one", {"b0002", "b0001"}),
        _finalist("two", {"b0003"}),
    )

    variants = map_finalists(finalists, registry)
    config = build_workflow_config(
        variants,
        output_root=tmp_path,
        experiment_id="experiment",
        lineage_seed=101,
        generations=20,
        sim_data_path=None,
    )

    assert variants[0].gene_mapping == (("b0001", "EG1"), ("b0002", "EG2"))
    assert config["variants"] == {
        "yggdrisil_multi_gene_knockout": {
            "gene_ids": {"value": [["EG1", "EG2"], ["EG3"]]}
        }
    }
    assert config["parca_options"]["operons"] is False  # type: ignore[index]
    assert config["single_daughters"] is True
    assert config["fail_at_max_duration"] is True
    assert "MDS42" not in repr(config)
    assert "MS56" not in repr(config)


def test_mapping_rejects_missing_or_ambiguous_ids() -> None:
    finalist = (_finalist("candidate", {"b0001", "b0002"}),)
    missing = GeneRegistry([_record("b0001", "EG1"), _record("b0002", None)])
    with pytest.raises(ValueError, match="lack EcoCyc"):
        map_finalists(finalist, missing)

    ambiguous = GeneRegistry([_record("b0001", "EG1"), _record("b0002", "EG1")])
    with pytest.raises(ValueError, match="ambiguous"):
        map_finalists(finalist, ambiguous)


def test_installed_adapter_zeroes_every_requested_rna(tmp_path: Path) -> None:
    checkout = tmp_path / "vecoli"
    (checkout / "ecoli" / "variants").mkdir(parents=True)
    module_path = install_vecoli_adapter(checkout)
    spec = importlib.util.spec_from_file_location("adapter", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sim_data = _fake_sim_data()

    result = module.apply_variant(sim_data, {"gene_ids": ["EG1", "EG3"]})

    assert result.yggdrisil_knockout_gene_ids == ("EG1", "EG3")
    assert result.yggdrisil_knockout_rna_indices == (0, 2)
    for values in sim_data.process.transcription.rna_synth_prob.values():
        assert values[[0, 2]].tolist() == [0.0, 0.0]
    assert sim_data.process.transcription.exp_ppgpp[[0, 2]].tolist() == [0.0, 0.0]
    assert sim_data.process.transcription.attenuation_basal_prob_adjustments[
        [0, 2]
    ].tolist() == [0.0, 0.0]
    assert sim_data.process.transcription_regulation.delta_prob["deltaV"][
        [0, 2]
    ].tolist() == [0.0, 0.0]


def test_installed_adapter_rejects_operon_coupling(tmp_path: Path) -> None:
    checkout = tmp_path / "vecoli"
    (checkout / "ecoli" / "variants").mkdir(parents=True)
    module_path = install_vecoli_adapter(checkout)
    spec = importlib.util.spec_from_file_location("adapter", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sim_data = _fake_sim_data()
    sim_data.process.transcription.cistron_tu_mapping_matrix[1, 0] = 1

    with pytest.raises(ValueError, match="operon partners"):
        module.apply_variant(sim_data, {"gene_ids": ["EG1"]})


def test_installed_adapter_reads_sparse_cistron_rows(tmp_path: Path) -> None:
    checkout = tmp_path / "vecoli"
    (checkout / "ecoli" / "variants").mkdir(parents=True)
    module_path = install_vecoli_adapter(checkout)
    spec = importlib.util.spec_from_file_location("adapter", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sim_data = _fake_sim_data()
    sim_data.process.transcription.cistron_tu_mapping_matrix = sparse.csr_matrix(
        np.eye(3, dtype=int)
    )

    result = module.apply_variant(sim_data, {"gene_ids": ["EG3"]})

    assert result.yggdrisil_knockout_rna_indices == (2,)


def test_lineage_summary_distinguishes_division_from_nondivision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = tmp_path / "graph.sqlite"
    graph.write_text("frozen graph\n")
    selection_source = tmp_path / "selection.py"
    selection_source.write_text("# frozen selection\n")
    registry = tmp_path / "registry.parquet"
    registry.write_text("frozen registry\n")
    checkout = tmp_path / "vecoli"
    checkout.mkdir()
    adapter = checkout / "adapter.py"
    adapter.write_text("# frozen adapter\n")
    vecoli_provenance = {
        "git_commit": "commit",
        "uv_lock_sha256": "lock",
        "nextflow_version": "version",
    }
    monkeypatch.setattr(
        "yggdrisil_ecoli.vecoli_results.validate_vecoli_checkout",
        lambda path: {**vecoli_provenance, "origin": "origin"},
    )
    config = tmp_path / "workflow.json"
    config.write_text("{}\n")
    output_root = tmp_path / "output"
    experiment = output_root / "experiment"
    workdirs = experiment / "nextflow" / "nextflow_workdirs"
    _fake_task(workdirs / "aa" / "one", variant=1, generation=1, exit_code=0)
    _fake_task(
        workdirs / "bb" / "two",
        variant=1,
        generation=2,
        exit_code=1,
        error="TimeLimitError: cell reached max duration",
    )
    daughter_dir = (
        experiment
        / "daughter_states"
        / "variant=1"
        / "seed=101"
        / "generation=1"
        / "agent_id=0"
    )
    daughter_dir.mkdir(parents=True)
    (daughter_dir / "daughter_state_0.json").write_text("{}\n")
    (daughter_dir / "daughter_state_1.json").write_text("{}\n")
    history = (
        experiment
        / "history"
        / "experiment_id=experiment"
        / "variant=1"
        / "lineage_seed=101"
        / "generation=1"
        / "agent_id=0"
    )
    history.mkdir(parents=True)
    pq.write_table(
        pa.table(
            {
                "global_time": [2200.0],
                "listeners__mass__cell_mass": [2300.0],
                "listeners__mass__dry_mass": [700.0],
                "listeners__mass__dry_mass_fold_change": [1.8],
            }
        ),
        history / "2200.pq",
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "application": {
                    "selection_source_path": str(selection_source),
                    "selection_source_sha256": hashlib.sha256(
                        selection_source.read_bytes()
                    ).hexdigest(),
                },
                "selection": {
                    "graph_path": str(graph),
                    "graph_files": {
                        graph.name: hashlib.sha256(graph.read_bytes()).hexdigest()
                    },
                },
                "registry": {
                    "path": str(registry),
                    "sha256": hashlib.sha256(registry.read_bytes()).hexdigest(),
                },
                "vecoli": {
                    "checkout": str(checkout),
                    "adapter_path": str(adapter),
                    "adapter_sha256": hashlib.sha256(adapter.read_bytes()).hexdigest(),
                    **vecoli_provenance,
                },
                "workflow": {
                    "config_path": str(config),
                    "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
                    "output_root": str(output_root),
                    "experiment_id": "experiment",
                },
                "lineage": {"seed": 101, "max_generations": 2},
                "finalists": [
                    {
                        "variant_index": 1,
                        "state_id": "state",
                        "deletion_count": 3,
                        "deletion_set_sha256": "hash",
                    }
                ],
            }
        )
    )

    result = summarize_vecoli_lineages(manifest, tmp_path / "result.json")

    finalist = result["finalists"][0]  # type: ignore[index]
    assert finalist["generations_completed"] == 1
    assert finalist["terminal_reason"] == "nondivision_max_duration"
    assert finalist["generations"][0]["final_dry_mass_fold_change"] == 1.8
    assert result["all_biologically_failed"] is True

    graph.write_text("changed graph\n")
    with pytest.raises(ValueError, match="source graph hash"):
        summarize_vecoli_lineages(manifest, tmp_path / "changed-result.json")


def _record(b_number: str, ecocyc_id: str | None) -> GeneRecord:
    return GeneRecord(
        b_number=b_number,
        symbol=None,
        name=None,
        description=None,
        start=1,
        end=2,
        strand="+",
        ncbi_gene_id=None,
        ecocyc_id=ecocyc_id,
    )


def _fake_sim_data() -> SimpleNamespace:
    cistrons = np.array(
        [("c1", "EG1"), ("c2", "EG2"), ("c3", "EG3")],
        dtype=[("id", "U2"), ("gene_id", "U3")],
    )
    rnas = np.array([("c1[c]",), ("c2[c]",), ("c3[c]",)], dtype=[("id", "U5")])
    transcription = SimpleNamespace(
        cistron_data=SimpleNamespace(struct_array=cistrons),
        rna_data=SimpleNamespace(struct_array=rnas),
        cistron_tu_mapping_matrix=np.eye(3, dtype=int),
        rna_synth_prob={"basal": np.array([0.2, 0.3, 0.5])},
        rna_expression={"basal": np.array([0.2, 0.3, 0.5])},
        exp_free=np.array([0.2, 0.3, 0.5]),
        exp_ppgpp=np.array([0.2, 0.3, 0.5]),
        attenuated_rna_indices=np.array([0, 1, 2]),
        attenuation_basal_prob_adjustments=np.array([0.2, 0.3, 0.5]),
    )
    regulation = SimpleNamespace(
        basal_prob=np.array([0.2, 0.3, 0.5]),
        delta_prob={
            "deltaI": np.array([0, 1, 2]),
            "deltaV": np.array([0.2, 0.3, 0.5]),
        },
    )

    def adjust(indices: list[int], factors: list[float]) -> None:
        for index, factor in zip(indices, factors):
            for values in transcription.rna_synth_prob.values():
                values[index] *= factor
            for values in transcription.rna_expression.values():
                values[index] *= factor
            transcription.exp_free[index] *= factor
            transcription.exp_ppgpp[index] *= factor
            attenuation_mask = transcription.attenuated_rna_indices == index
            transcription.attenuation_basal_prob_adjustments[attenuation_mask] *= factor
            regulation.basal_prob[index] *= factor
            regulation.delta_prob["deltaV"][
                regulation.delta_prob["deltaI"] == index
            ] *= factor

    return SimpleNamespace(
        process=SimpleNamespace(
            transcription=transcription,
            transcription_regulation=regulation,
        ),
        adjust_final_expression=adjust,
    )


def _fake_task(
    path: Path,
    *,
    variant: int,
    generation: int,
    exit_code: int,
    error: str = "",
) -> None:
    path.mkdir(parents=True)
    (path / ".command.sh").write_text(
        "python ecoli_master_sim.py "
        f"--variant {variant} --initial_state_file generation={generation - 1}/x "
        f"--daughter_outdir generation={generation}/x\n"
    )
    (path / ".exitcode").write_text(str(exit_code))
    (path / ".command.err").write_text(error)
    (path / ".command.trace").write_text("nextflow.trace/v2\nrealtime=1234\n")
    if exit_code == 0:
        (path / "division_time.sh").write_text("export division_time=2200.0")
