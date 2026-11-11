"""Offline checks that the frozen baseline selection cannot drift."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

FOLDER = Path(__file__).resolve().parent.parent
ROOT = FOLDER.parent.parent
SPEC = importlib.util.spec_from_file_location('baseline_validation_plan', FOLDER / 'source/prepare-plan.py')
PLAN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLAN)


def inputs():
    parent = ROOT / 'runs/deletion-baselines-20261106'
    return (PLAN.read(parent / 'final/nominations.json'),
            PLAN.read(parent / 'final/catalog.json'),
            PLAN.read(parent / 'inputs/starting-genotype.json'),
            PLAN.read(ROOT / 'benchmarks/vecoli_20gen_remote.json')['genotypes'][:2])


def test_all_ten_search_seeds_and_exact_controls_are_preserved():
    genotypes = PLAN.freeze_genotypes(*inputs())
    assert len(genotypes) == 12
    assert len({g['state_id'] for g in genotypes}) == 12
    assert [g['variant_index'] for g in genotypes] == list(range(12))
    assert [g['deletion_count'] for g in genotypes[:2]] == [0, 1030]
    assert sum(len(g.get('source_run_ids', [])) for g in genotypes) == 10


def test_missing_nominee_rejected():
    nominations, catalog, start, controls = inputs()
    nominations['nominations'].pop()
    catalog['nominations'] = copy.deepcopy(nominations['nominations'])
    with pytest.raises(ValueError, match='all ten'):
        PLAN.freeze_genotypes(nominations, catalog, start, controls)


def test_modified_gene_set_rejected_even_if_count_is_updated():
    nominations, catalog, start, controls = inputs()
    nomination = nominations['nominations'][0]
    nomination['deleted_gene_ids'].pop()
    nomination['genes_deleted'] -= 1
    catalog['nominations'] = copy.deepcopy(nominations['nominations'])
    with pytest.raises(ValueError, match='identity'):
        PLAN.freeze_genotypes(nominations, catalog, start, controls)


def test_control_substitution_rejected():
    nominations, catalog, start, controls = inputs()
    controls[1]['deletion_set_sha256'] = '0' * 64
    with pytest.raises(ValueError, match='Control genotype'):
        PLAN.freeze_genotypes(nominations, catalog, start, controls)


def test_unverified_graph_not_accepted():
    nominations, catalog, start, controls = inputs()
    catalog['jobs'][0]['checkpoint']['wandb_download_verified'] = False
    with pytest.raises(ValueError, match='verified per-run'):
        PLAN.freeze_genotypes(nominations, catalog, start, controls)


def test_plan_cannot_authorize_spend_or_expand_science():
    protocol = json.loads((FOLDER / 'protocol.json').read_bytes())
    assert protocol['authorization']['paid_launch'] is False
    assert protocol['authorization']['model_calls_allowed'] is False
    assert protocol['authorization']['approved_budget_usd'] is None
    assert protocol['execution']['platform'] == 'remote_linux_only'
    assert protocol['execution']['max_parallel_cells_total'] == 9
    assert protocol['maximum_full_cell_simulations'] == 12 * 3 * 20
    assert protocol['additional_calibration_cells'] == 2
    assert protocol['execution']['science_hours'] < protocol['execution']['max_worker_hours'] == 24
    assert protocol['solver_patch_sha256'] == '3a4a50fdc7b4d16277b7ce8d3cb76e2e2fd69132937a4d57cbb232b01a06592a'


def test_frozen_metadata_and_source_hashes_match():
    manifest = PLAN.read(FOLDER / 'manifest.json')
    for name, digest in manifest['files'].items():
        assert PLAN.sha((FOLDER / name).read_bytes()) == digest
    for name, digest in PLAN.read(FOLDER / 'source-manifest.json')['files'].items():
        assert PLAN.sha((FOLDER / 'source' / name).read_bytes()) == digest
