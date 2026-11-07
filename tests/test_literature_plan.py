from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from scripts.plan_literature_ablation import (
    build_jobs,
    file_sha256,
    frozen_dossier,
    prepare,
)


@pytest.fixture
def spec():
    path = Path(__file__).parents[1] / "benchmarks/literature_ablation.json"
    return json.loads(path.read_text())


def test_information_arms_share_settings_but_isolate_memory(spec):
    jobs = build_jobs(spec, "same-start")
    assert len(jobs) == 9
    assert len({job["agent_config"]["session_path"] for job in jobs}) == 9
    assert len({job["graph_path"] for job in jobs}) == 9
    for index, seed in enumerate(spec["search_seeds"]):
        block = [job for job in jobs if job["seed"] == seed]
        assert block[0]["arm"] == spec["arms"][index]
        by_arm = {job["arm"]: job for job in block}
        plain = by_arm["no_retrieval"]
        fixed = by_arm["frozen_dossier"]
        live = by_arm["adaptive_literature"]
        assert plain["dossier"] is None
        assert fixed["dossier"] == live["dossier"] == "inputs/dossier.json"
        assert [job["live_literature"] for job in (plain, fixed, live)] == [
            False,
            False,
            True,
        ]
        for job in (plain, fixed, live):
            config = job["agent_config"]
            assert config["mode"] != "closed-book"
            assert config["edison"] is False
            assert job["limits"]["max_states"] == 51
            common = {
                key: value
                for key, value in config.items()
                if key not in {
                    "mode", "seed", "session_path", "edison_cache_path",
                    "require_initial_research",
                }
            }
            assert common == spec["agent"]
    jobs[0]["agent_config"]["open_set"]["active_width"] = 1
    assert jobs[1]["agent_config"]["open_set"]["active_width"] == 12


def test_long_search_requires_fresh_research_only_in_adaptive_arm(spec):
    from yggdrisil_ecoli.agent_policy import AgentSearchConfig

    spec["agent"]["bundle_size"] = 100
    spec["search"].update(unique_new_states=500, require_initial_research=True)
    for job in build_jobs(spec, "same-start"):
        config = AgentSearchConfig.model_validate(job["agent_config"])
        assert config.bundle_size == 100
        assert config.require_initial_research == job["live_literature"]
        assert job["limits"]["max_states"] == 501


@pytest.mark.parametrize(
    "change", ["duplicate_seed", "live_edison", "blind_mode", "shared_memory"]
)
def test_invalid_arm_comparisons_are_rejected(spec, change):
    if change == "duplicate_seed":
        spec["search_seeds"] = [17, 17]
    elif change == "live_edison":
        spec["agent"]["edison"] = True
    elif change == "blind_mode":
        spec["agent"]["mode"] = "closed-book"
    else:
        spec["agent"]["session_path"] = "shared.sqlite"
    with pytest.raises(ValueError):
        build_jobs(spec, "root")


@pytest.fixture
def frozen_inputs(tmp_path, spec):
    answer = "Frozen evidence with a source citation."
    cache = tmp_path / "edison.sqlite"
    with sqlite3.connect(cache) as db:
        db.execute(
            "CREATE TABLE edison_queries(namespace, task_id, query, status, result_json)"
        )
        db.execute(
            "INSERT INTO edison_queries VALUES (?, ?, ?, ?, ?)",
            (
                "prior-run",
                "task",
                "Question",
                "success",
                json.dumps({"formatted_answer": answer}),
            ),
        )
    spec["dossier"] = {
        "cache_path": cache.name,
        "namespace": "prior-run",
        "task_id": "task",
        "answer_sha256": hashlib.sha256(answer.encode()).hexdigest(),
    }
    gene_file = tmp_path / "genes.parquet"
    gene_file.write_bytes(b"registry artifact; the planner verifies its checksum only")
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "reference": {
                    "accession": "NC_000913.3",
                    "genes_sha256": file_sha256(gene_file),
                },
                "strains": {
                    "MS56": {
                        "deleted_gene_ids": ["b0001"],
                        "representation": "test projection",
                        "published_ids_outside_search_universe": [],
                    }
                },
            }
        )
    )
    spec["starting_genotype"]["catalog"] = {
        "path": catalog.name,
        "sha256": file_sha256(catalog),
    }
    spec["starting_genotype"]["expected_deletion_count"] = 1
    spec["inputs"] = {
        "genes": {"path": gene_file.name, "sha256": file_sha256(gene_file)}
    }
    config = tmp_path / "plan.json"
    config.write_text(json.dumps(spec))
    return config


def test_prepare_freezes_only_scientific_inputs_and_refuses_overwrite(
    frozen_inputs, tmp_path
):
    output = tmp_path / "bundle"
    manifest = prepare(frozen_inputs, output, tmp_path)
    assert manifest["launch_ready"] is False
    assert manifest["maximum_new_states"] == 450
    assert manifest["maximum_validation_lineages"] == 33
    assert manifest["maximum_validation_cell_cycles"] == 396
    dossier = json.loads((output / "inputs/dossier.json").read_text())
    assert dossier["reported_original_cost"] is None
    for name, checksum in manifest["bundle_sha256"].items():
        assert file_sha256(output / name) == checksum
    with pytest.raises(FileExistsError):
        prepare(frozen_inputs, output, tmp_path)


def test_changed_dossier_and_incomplete_report_fail_loudly(frozen_inputs, tmp_path):
    spec = json.loads(frozen_inputs.read_text())
    spec["dossier"]["answer_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="checksum"):
        frozen_dossier(tmp_path, spec["dossier"])
    with sqlite3.connect(tmp_path / "edison.sqlite") as db:
        db.execute("UPDATE edison_queries SET status='in progress'")
    with pytest.raises(ValueError, match="successful"):
        frozen_dossier(tmp_path, spec["dossier"])


def test_changed_input_fails_before_creating_bundle(frozen_inputs, tmp_path):
    (tmp_path / "catalog.json").write_text("changed")
    output = tmp_path / "bundle"
    with pytest.raises(ValueError, match="checksum"):
        prepare(frozen_inputs, output, tmp_path)
    assert not output.exists()


def test_valid_new_registry_hash_cannot_silently_reuse_an_old_catalog(
    frozen_inputs, tmp_path
):
    spec = json.loads(frozen_inputs.read_text())
    registry = tmp_path / "genes.parquet"
    registry.write_bytes(b"a different registry")
    spec["inputs"]["genes"]["sha256"] = file_sha256(registry)
    frozen_inputs.write_text(json.dumps(spec))
    with pytest.raises(ValueError, match="registry/reference differ"):
        prepare(frozen_inputs, tmp_path / "bundle", tmp_path)
