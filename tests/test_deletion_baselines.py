from __future__ import annotations

import asyncio
import hashlib
import io
import json
import sqlite3
import tarfile
import threading
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from yggdrisil import EvaluatorSuite, RunLimits, Runner, RunStatus, SQLiteStateGraph

from scripts.collect_deletion_baselines import collect
from scripts.plan_deletion_baselines import prepare, source_files, validate_protocol
from scripts.run_deletion_baseline import (
    CheckpointPolicy,
    curves,
    load_bundle,
    run_job,
    validate_approval,
)
from yggdrisil_ecoli.actions import DeleteGenes
from yggdrisil_ecoli.baseline_storage import BaselineArchive, screen, sqlite_snapshot
from yggdrisil_ecoli.data.io import atomic_json, file_sha256
from yggdrisil_ecoli.policies import BlockDeletionPolicy
from yggdrisil_ecoli.problem import EcoliProblem
from yggdrisil_ecoli.scorers.base import active_evaluator_ids, scientific_evaluation
from yggdrisil_ecoli.state import GenomeState, genome_state_key


@dataclass
class FixtureScorer:
    name: str
    version: str = "fixture"
    config: str = "offline-only"
    calls: int = 0
    fail_on_call: int | None = None

    async def evaluate(self, state):
        self.calls += 1
        if self.calls == self.fail_on_call:
            raise RuntimeError("deliberate interruption inside evaluation")
        if self.name == "fba":
            return scientific_evaluation({"feasible": True, "growth_rate": 1.0})
        return scientific_evaluation({
            "feasible_at_growth_floor": "b0001" not in state.deleted_genes
        })


def make_policy(scorers, kind="randomized_block", seed=17, sizes=(4, 2, 1)):
    return BlockDeletionPolicy(
        gene_ids=[f"b{i:04}" for i in range(1, 13)],
        evaluator_ids=active_evaluator_ids(scorers), initial_state=GenomeState(frozenset()),
        kind=kind, seed=seed, block_sizes=sizes, neighborhood_size=3,
    )


def status(graph, maximum=25):
    return RunStatus(step=0, unique_states=len(graph), edges=graph.edge_count(),
                     elapsed_s=0, limits=RunLimits(max_states=maximum), run_id="fixture")


async def evaluated_root(graph, scorers):
    state = GenomeState(frozenset())
    graph.add_state(genome_state_key(state), state)
    await EvaluatorSuite(scorers).evaluate_cached(graph, genome_state_key(state))


@pytest.mark.parametrize("kind", ["randomized_block", "greedy_block"])
@pytest.mark.asyncio
async def test_interrupt_mid_evaluation_resume_matches_uninterrupted(tmp_path, kind):
    genes = pd.DataFrame(index=[f"b{i:04}" for i in range(1, 13)])
    traces = []
    for interrupted in [False, True]:
        scorers = [FixtureScorer("fba"), FixtureScorer("resource_allocation")]
        scorers[1].fail_on_call = 4 if interrupted else None
        path = tmp_path / f"{interrupted}.sqlite"
        graph = SQLiteStateGraph[GenomeState, DeleteGenes](path)
        def runner(resume=False):
            return Runner(EcoliProblem(genes), make_policy(scorers, kind), graph,
                          RunLimits(max_states=17, max_steps=30),
                          evaluators=EvaluatorSuite(scorers), run_id="fixture", resume=resume)
        if interrupted:
            with pytest.raises(RuntimeError, match="deliberate interruption"):
                await runner().run()
            graph.close()
            graph = SQLiteStateGraph[GenomeState, DeleteGenes](path)
        result = await runner(interrupted).run()
        assert len(graph) <= 17
        assert result.stop_reason in {"max_states", "no_proposals"}
        rows = curves(graph, active_evaluator_ids(scorers), 0)
        assert all(r["evaluation_complete"] for r in rows)
        assert any(not r["growth_gates_passed"] for r in rows)
        assert len(rows) - 1 == len(graph) - 1  # Rejected states count too.
        events = graph.proposal_events(run_id="fixture")
        traces.append([(e.parent_id, e.action.genes, e.child_id) for e in events])
        assert len({e.child_id for e in events if e.child_id}) == len(graph) - 1
        for edge in graph.edges():
            parent_edges = [e for e in graph.edges() if e.child_id == edge.parent_id]
            assert all(edge.metadata["block_size"] <= p.metadata["block_size"] for p in parent_edges)
        graph.close()
    assert traces[0] == traces[1]


@pytest.mark.asyncio
async def test_shared_neighborhood_seed_change_and_budget_truncation(tmp_path):
    scorers = [FixtureScorer("fba"), FixtureScorer("resource_allocation")]
    with SQLiteStateGraph(tmp_path / "graph.sqlite") as graph:
        await evaluated_root(graph, scorers)
        random = (await make_policy(scorers).step(graph.readonly(), status(graph)))[0]
        greedy = (await make_policy(scorers, "greedy_block").step(graph.readonly(), status(graph)))[0]
        assert greedy.proposals[0].action == random.proposals[0].action
        assert len(greedy.proposals) == 3
        changed = (await make_policy(scorers, seed=29).step(graph.readonly(), status(graph)))[0]
        assert changed.proposals[0].action != random.proposals[0].action
        truncated = (await make_policy(scorers, "greedy_block").step(graph.readonly(), status(graph, 2)))[0]
        assert len(truncated.proposals) == 1
        exhausted = (await make_policy(scorers).step(graph.readonly(), status(graph, 1)))[0]
        assert not exhausted.proposals


@pytest.mark.asyncio
async def test_requires_exact_evaluator_evidence(tmp_path):
    scorers = [FixtureScorer("fba"), FixtureScorer("resource_allocation")]
    with SQLiteStateGraph(tmp_path / "graph.sqlite") as graph:
        await evaluated_root(graph, scorers)
        scorers[0].config = "changed"
        with pytest.raises(ValueError, match="lacks active evaluations"):
            await make_policy(scorers).step(graph.readonly(), status(graph))


@pytest.mark.parametrize("changes", [dict(seed=-1), dict(block_sizes=(1, 4)),
                                   dict(kind="unknown"), dict(neighborhood_size=0)])
def test_invalid_policy(changes):
    args = dict(gene_ids=["b0001"], evaluator_ids={"fba": "f", "resource_allocation": "r"},
                initial_state=GenomeState(frozenset()), kind="greedy_block", seed=17)
    with pytest.raises(ValueError):
        BlockDeletionPolicy(**(args | changes))


@pytest.fixture
def prepared(tmp_path):
    spec = json.loads(Path("benchmarks/deletion_baselines.json").read_text())
    spec["starting_genotype"].update(deletion_count=0,
        deletion_set_sha256=hashlib.sha256(b"[]").hexdigest())
    protocol, start = tmp_path / "protocol.json", tmp_path / "start.json"
    atomic_json(protocol, spec)
    atomic_json(start, {"deleted_gene_ids": []})
    framework = tmp_path / "framework"
    framework.mkdir()
    (framework / "pyproject.toml").write_text("# fixture\n")
    bundle = tmp_path / "bundle"
    prepare(protocol, start, bundle, Path.cwd(), framework)
    return bundle


def test_source_snapshot_excludes_partial_distribution_metadata(tmp_path):
    app, framework = tmp_path / "app", tmp_path / "framework"
    for base in (app, framework):
        source = base / "src" / "package"
        source.mkdir(parents=True)
        (source / "__init__.py").write_text("# source\n")
        for suffix in ("egg-info", "dist-info"):
            metadata = base / "src" / f"package.{suffix}"
            metadata.mkdir()
            (metadata / "requires.txt").write_text("dependency\n")
    files = source_files(app, framework)
    assert "application/src/package/__init__.py" in files
    assert "framework/src/package/__init__.py" in files
    assert not any(".egg-info" in name or ".dist-info" in name for name in files)


def test_bundle_deterministic_jobs_and_tamper_detection(prepared):
    spec, job = load_bundle(prepared, 0)
    assert job["seed"] == 17
    jobs = json.loads((prepared / "jobs.json").read_text())
    assert len(jobs) == 10 and len({j["run_id"] for j in jobs}) == 10
    catalog = collect(prepared)
    assert not catalog["finalist_selection_frozen"]
    assert len(catalog["jobs"]) == 10 and not catalog["nominations"]
    (prepared / "inputs/starting-genotype.json").write_text("{}")
    with pytest.raises(ValueError, match="frozen input"):
        load_bundle(prepared, 0)


def test_protocol_rejects_paid_launch_and_changed_growth_gates():
    spec = json.loads(Path("benchmarks/deletion_baselines.json").read_text())
    validate_protocol(spec)
    spec["authorization"]["paid_launch"] = True
    with pytest.raises(ValueError, match="Planning cannot"):
        validate_protocol(spec)
    spec["authorization"]["paid_launch"] = False
    spec["growth_gates"]["rba_growth_floor_per_hour"] = 0.01
    with pytest.raises(ValueError, match="Growth gates"):
        validate_protocol(spec)


def test_unapproved_and_expired_launch_refused(prepared, monkeypatch):
    spec, _ = load_bundle(prepared, 0)
    with pytest.raises(ValueError, match="approval"):
        validate_approval(prepared, spec, {})
    monkeypatch.setattr("scripts.run_deletion_baseline.time.time", lambda: 1000)
    approval = dict(experiment_id=spec["experiment_id"],
                    protocol_sha256=file_sha256(prepared / "protocol.json"),
                    bundle_manifest_sha256=file_sha256(prepared / "manifest.json"), paid_launch=True,
                    model_calls_allowed=False, user_authorization="offline fixture", total_cap_usd=25,
                    science_deadline_unix=1500, stop_deadline_unix=2000)
    validate_approval(prepared, spec, approval)
    for expiry in [999, float("inf")]:
        with pytest.raises(ValueError, match="deadline"):
            validate_approval(prepared, spec, approval | {"science_deadline_unix": expiry})


@pytest.mark.asyncio
async def test_mac_guard_before_file_reads_or_models(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.run_deletion_baseline.sys.platform", "darwin")
    with pytest.raises(RuntimeError, match="remote-only"):
        await run_job(tmp_path / "missing", 0, tmp_path, tmp_path / "missing", False)


def test_sqlite_snapshot_includes_committed_wal(tmp_path):
    source, target = tmp_path / "live.sqlite", tmp_path / "backup.sqlite"
    writer = sqlite3.connect(source)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE values_seen(value INTEGER)")
    writer.execute("INSERT INTO values_seen VALUES (42)")
    writer.commit()
    assert Path(str(source) + "-wal").stat().st_size > 0
    sqlite_snapshot(source, target)
    with sqlite3.connect(target) as read:
        assert read.execute("SELECT * FROM values_seen").fetchall() == [(42,)]
    writer.close()


def test_secret_scan_across_chunk_boundary(tmp_path):
    p = tmp_path / "data"
    p.write_bytes(b"x" * (1024 * 1024 - 3) + b"test-secret-value")
    with pytest.raises(RuntimeError, match="Credential detected"):
        screen(p, [b"test-secret-value"])
    p.write_bytes(b"allowed scientific metadata")
    screen(p, [b"test-secret-value"])


class FakeS3:
    def __init__(self, corrupt=False):
        self.data, self.corrupt = None, corrupt

    def put_object(self, **kwargs):
        assert kwargs["ServerSideEncryption"] == "AES256"
        assert kwargs["ExpectedBucketOwner"] == "owner"
        self.data = kwargs["Body"].read()

    def get_object(self, **kwargs):
        return {"Body": io.BytesIO(b"corrupt" if self.corrupt else self.data)}


@pytest.mark.parametrize("corrupt", [True, False])
def test_full_get_verification(tmp_path, corrupt):
    archive = object.__new__(BaselineArchive)
    archive.s3 = FakeS3(corrupt)
    archive.owner, archive.bucket, archive.prefix, archive.run_id = "owner", "bucket", "prefix/", "run"
    p = tmp_path / "data.json"
    p.write_text('{"scientific":true}')
    if corrupt:
        with pytest.raises(RuntimeError, match="full-GET"):
            archive.upload(p, "data")
    else:
        assert archive.upload(p, "data")["sha256"] == file_sha256(p)


def test_checkpoint_allowlist_and_wandb_corruption(prepared, tmp_path):
    folder = tmp_path / "run"
    folder.mkdir()
    (folder / "source.py").write_text("SOURCE MUST NOT BE UPLOADED")
    (folder / ".env").write_text("PRIVATE MUST NOT BE UPLOADED")
    atomic_json(folder / "progress.json", {"status": "running"})
    seen = {}

    class Artifact:
        qualified_name, state = "entity/project/run-data:v0", "COMMITTED"
        def __init__(self, *args, **kwargs):
            self.entries = {}
        def add_file(self, path, name):
            self.entries[name] = Path(path)
        def wait(self):
            return self
        def get_entry(self, name):
            return SimpleNamespace(download=lambda **_: self.entries[name])

    def log(artifact):
        seen["artifact"] = artifact
        with tarfile.open(artifact.entries["checkpoint.tar.gz"]) as tar:
            seen["names"] = tar.getnames()
        return artifact

    archive = object.__new__(BaselineArchive)
    archive.s3, archive.owner, archive.bucket = FakeS3(), "owner", "bucket"
    archive.prefix, archive.run_id, archive.secrets = "prefix/", "run", []
    archive.tracked = SimpleNamespace(log_artifact=log)
    archive.wandb = SimpleNamespace(Artifact=Artifact, Api=lambda **_: SimpleNamespace(artifact=lambda _: seen["artifact"]))
    result = archive.checkpoint(prepared, folder)
    assert result["wandb_download_verified"] and result["full_get_verified"]
    assert "progress.json" in seen["names"]
    assert not any("source/" in n or n.endswith(".py") or n == ".env" for n in seen["names"])
    original = log
    def corrupt_log(artifact):
        original(artifact)
        bad = tmp_path / "bad.json"
        bad.write_text("corrupted W&B content")
        artifact.entries["index.json"] = bad
        return artifact
    archive.tracked.log_artifact = corrupt_log
    with pytest.raises(RuntimeError, match="W&B downloaded"):
        archive.checkpoint(prepared, folder)
    assert json.loads((folder / "checkpoint.json").read_text()) == result


@pytest.mark.asyncio
async def test_cancellation_waits_for_checkpoint_before_return(tmp_path):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    class Archive:
        def checkpoint(self, *args):
            started.set()
            if not release.wait(timeout=3):
                raise RuntimeError("fixture checkpoint never released")
            finished.set()
    class Policy:
        async def step(self, *args):
            raise AssertionError("No new proposals after cancellation")
    scorers = [FixtureScorer("fba"), FixtureScorer("resource_allocation")]
    with SQLiteStateGraph(tmp_path / "graph.sqlite") as graph:
        await evaluated_root(graph, scorers)
        wrapped = CheckpointPolicy(Policy(), Archive(), SimpleNamespace(log=lambda _: None),
                                   tmp_path, tmp_path, active_evaluator_ids(scorers), 0)
        task = asyncio.create_task(wrapped.step(graph.readonly(), status(graph)))
        await asyncio.to_thread(started.wait, 2)
        assert started.is_set()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
