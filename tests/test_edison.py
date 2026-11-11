import asyncio
import sqlite3
from contextlib import closing

import httpx
import pytest
from yggdrisil.agents import ToolInputError

from yggdrisil_ecoli.agent_policy import AgentSearchConfig
from yggdrisil_ecoli.tools.edison import EdisonLiterature


async def answer(path, namespace, query, offset=0, count=16000):
    tool = EdisonLiterature(path, namespace)
    job = await tool.submit_edison(query)
    return {
        **await tool.check_edison(job["task_id"], offset, count),
        "submitted": job["submitted"],
    }


async def test_query_budget_cache_paging_restart_and_run_isolation(
    tmp_path, edison_service
):
    path = tmp_path / "edison.sqlite"
    first = await answer(path, "run-a", "MG1655 reductions")
    assert first["formatted_answer"].endswith("doi:10.example/test")
    assert first["submitted"]
    counts = len(edison_service["submit"]), len(edison_service["poll"])
    restarted = EdisonLiterature(path, "run-a")
    page = await answer(path, "run-a", "MG1655 reductions", offset=6, count=8)
    assert page["formatted_answer"] == first["formatted_answer"][6:14]
    assert page["cached"] and page["next_offset"] == 14
    assert not page["submitted"]
    assert counts == (len(edison_service["submit"]), len(edison_service["poll"]))
    with pytest.raises(ToolInputError, match="limit.*1") as error:
        await restarted.submit_edison("Another question")
    assert "MG1655 reductions" in str(error.value)
    assert first["task_id"] in str(error.value)
    other = await answer(path, "run-b", "MG1655 reductions")
    assert other["task_id"] != first["task_id"]
    assert len(edison_service["submit"]) == 2


@pytest.mark.parametrize("status", ["fail", "cancelled", "truncated"])
async def test_remote_failures_are_explicit_and_do_not_resubmit(
    tmp_path, edison_service, status
):
    edison_service["status"] = status
    for _ in range(2):
        with pytest.raises(RuntimeError, match=f"Edison job {status}"):
            await answer(tmp_path / "cache.sqlite", "run", "Question")
    assert len(edison_service["submit"]) == 1


async def test_lost_submission_response_does_not_launch_another_job(
    tmp_path, edison_service
):
    edison_service["lost"] = True
    tool = EdisonLiterature(tmp_path / "cache.sqlite", "run")
    with pytest.raises(httpx.ConnectError, match="response lost"):
        await answer(tool.path, "run", "Question")
    result = await answer(tool.path, "run", "Question")
    assert result["task_id"] == edison_service["submit"][0]["id"]
    assert len(edison_service["submit"]) == 1


async def test_concurrent_questions_cannot_exceed_persistent_budget(
    tmp_path, edison_service
):
    path = tmp_path / "cache.sqlite"
    results = await asyncio.gather(
        EdisonLiterature(path, "run").submit_edison("Question A"),
        EdisonLiterature(path, "run").submit_edison("Question B"),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ToolInputError) for result in results) == 1
    assert len(edison_service["submit"]) == 1
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT COUNT(*) FROM edison_queries").fetchone()[0] == 1


def test_edison_is_research_only():
    with pytest.raises(ValueError, match="Edison requires research mode"):
        AgentSearchConfig(model="vendor/model", edison=True)


async def test_missing_credential_does_not_reserve_a_paid_job(tmp_path, monkeypatch):
    monkeypatch.delenv("EDISON_PLATFORM_API_KEY", raising=False)
    tool = EdisonLiterature(tmp_path / "cache.sqlite", "run")
    with pytest.raises(RuntimeError, match="Set EDISON_PLATFORM_API_KEY"):
        await answer(tool.path, "run", "Question")
    with closing(sqlite3.connect(tool.path)) as db:
        assert db.execute("SELECT COUNT(*) FROM edison_queries").fetchone()[0] == 0


async def test_signed_compressed_results_and_session_refresh(tmp_path, edison_service):
    edison_service["blob"] = True
    edison_service["refresh"] = True
    result = await answer(tmp_path / "cache.sqlite", "run", "Question")
    assert "doi:10.example/test" in result["formatted_answer"]
    assert result["total_cost"] == 0.25
    assert edison_service["logins"] == 3
    assert all(client.is_closed for client in edison_service["clients"])


async def test_submit_returns_without_polling_and_checks_are_throttled(
    tmp_path, edison_service
):
    tool = EdisonLiterature(tmp_path / "cache.sqlite", "run")
    job = await tool.submit_edison("Focused question")
    assert job["status"] == "queued" and job["submitted"]
    assert not edison_service["poll"]
    assert tool.snapshots()[0]["task_id"] == job["task_id"]
    edison_service["status"] = "in progress"
    for _ in range(5):
        result = await tool.check_edison(job["task_id"])
        assert result["status"] == "in progress"
    assert len(edison_service["poll"]) == 1
    edison_service["status"] = "success"
    restarted = EdisonLiterature(tool.path, "run")
    result = await restarted.check_edison(job["task_id"])
    assert result["formatted_answer"]
    assert result["cached"]
    assert len(edison_service["submit"]) == 1
    with pytest.raises(ToolInputError, match="Unknown Edison job"):
        await EdisonLiterature(tool.path, "other-run").check_edison(job["task_id"])


async def test_coordinator_persists_remote_failure_before_raising(
    genes, tmp_path, edison_service, monkeypatch
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-placeholder")
    from yggdrisil import RunLimits, Runner, SQLiteStateGraph

    from yggdrisil_ecoli.agent_policy import make_agent_policy
    from yggdrisil_ecoli.problem import EcoliProblem
    from yggdrisil_ecoli.scorers.modules import ModuleEvaluator

    tool = EdisonLiterature(tmp_path / "cache.sqlite", "run")
    job = await tool.submit_edison("Focused question")
    edison_service["status"] = "fail"
    with SQLiteStateGraph(tmp_path / "graph.sqlite") as graph:
        policy = make_agent_policy(
            genes=genes,
            modules=ModuleEvaluator(
                registry=genes,
                entries={},
                wt_complete_module_ids=(),
                parser_semantics_version="fixture",
            ),
            config=AgentSearchConfig(
                model="vendor/model",
                mode="research",
                orchestration="manager",
                edison=True,
                edison_cache_path=tool.path,
                session_path=tmp_path / "agents.sqlite",
            ),
            evaluator_ids={name: name for name in ("fba", "resource_allocation")},
            graph=graph.readonly(),
            run_id="run",
        )
        with pytest.raises(RuntimeError, match="Edison job fail"):
            await Runner(
                EcoliProblem(genes), policy, graph, RunLimits(max_steps=1), run_id="run"
            ).run()
        decision = graph.decisions("run")[-1]
        assert decision.role == "navigator"
        assert decision.metadata["attempt_status"] == "failed"
        assert job["task_id"] in decision.output["error"]
    assert len(edison_service["submit"]) == 1
