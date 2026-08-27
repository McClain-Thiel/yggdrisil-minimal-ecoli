from pathlib import Path

import pytest
from pydantic import ValidationError
from yggdrisil.agents import ExplorerContext, ExplorerResult

from yggdrisil_ecoli.actions import DeleteGenes
from yggdrisil_ecoli.agent_policy import (
    AgentPolicyError,
    AgentSearchConfig,
    _AgentGeneTools,
    _BlindGeneMap,
    _bounded_action_type,
    _BoundExplorer,
    _CandidateSchedule,
    _format_explorer_prompt,
    _UsageLimitedAgent,
)
from yggdrisil_ecoli.data.essentiality import (
    EssentialityDataset,
    EssentialityRecord,
)
from yggdrisil_ecoli.data.gff import parse_ncbi_gff
from yggdrisil_ecoli.data.registry import GeneRegistry
from yggdrisil_ecoli.scorers.modules import ModuleEvaluator
from yggdrisil_ecoli.state import GenomeState

FIXTURES = Path(__file__).parent / "fixtures"


def test_agent_config_requires_fixed_model_and_has_secret_free_metadata() -> None:
    registry, _essentiality, _modules = _evidence()
    config = AgentSearchConfig(model="openai/gpt-4o-mini-2024-07-18", seed=7)

    metadata = config.metadata(registry)

    assert metadata["model"] == "openai/gpt-4o-mini-2024-07-18"
    assert metadata["mode"] == "closed-book"
    assert metadata["blind_map_sha256"]
    assert "key" not in str(metadata).lower()
    with pytest.raises(ValueError, match="fixed OpenRouter model"):
        AgentSearchConfig(model="openrouter/free")


def test_agent_output_schema_enforces_bundle_size() -> None:
    action_type = _bounded_action_type("closed-book", 1)

    with pytest.raises(ValidationError, match="at most 1 item"):
        action_type(genes=("g0001", "g0002"))


@pytest.mark.asyncio
async def test_provider_failure_is_translated_at_boundary() -> None:
    class FailingAgent:
        async def run(self, prompt: str, *, usage_limits: object) -> object:
            raise ValueError("provider failed")

    with pytest.raises(AgentPolicyError, match="ValueError: provider failed"):
        await _UsageLimitedAgent(FailingAgent(), object()).run("prompt")


def test_closed_book_prompt_and_tools_hide_canonical_annotations() -> None:
    registry, essentiality, modules = _evidence()
    config = AgentSearchConfig(
        model="openai/gpt-4o-mini-2024-07-18",
        mode="closed-book",
        seed=11,
    )
    blind = _BlindGeneMap(registry, config.seed)
    schedule = _CandidateSchedule(registry, config.seed)
    state = GenomeState(frozenset({"b0001"}))

    def toolkit(current: GenomeState) -> _AgentGeneTools:
        return _AgentGeneTools(
            registry=registry,
            essentiality=essentiality,
            modules=modules,
            schedule=schedule,
            state=current,
            mode="closed-book",
            blind=blind,
        )

    prompt = _format_explorer_prompt(
        ExplorerContext(
            goal="minimize",
            state_id="state",
            state=state,
            lineage=[],
            guidance=None,
        ),
        toolkit,
        config,
    )
    evidence = toolkit(state).list_candidates()

    for leaked in ("b0001", "b0002", "b0003", "thrL", "thrA", "thrB"):
        assert leaked not in prompt
        assert leaked not in str(evidence)
    assert blind.public("b0001") in prompt
    assert "g" in str(evidence)


def test_tool_rich_prompt_exposes_canonical_candidate_annotations() -> None:
    registry, essentiality, modules = _evidence()
    config = AgentSearchConfig(
        model="openai/gpt-4o-mini-2024-07-18",
        mode="tool-rich",
        seed=11,
    )
    schedule = _CandidateSchedule(registry, config.seed)
    state = GenomeState(frozenset())

    def toolkit(current: GenomeState) -> _AgentGeneTools:
        return _AgentGeneTools(
            registry=registry,
            essentiality=essentiality,
            modules=modules,
            schedule=schedule,
            state=current,
            mode="tool-rich",
            blind=None,
        )

    prompt = _format_explorer_prompt(
        ExplorerContext(
            goal="minimize",
            state_id="state",
            state=state,
            lineage=[],
            guidance=None,
        ),
        toolkit,
        config,
    )

    assert any(gene in prompt for gene in ("b0001", "b0002", "b0003"))
    assert any(symbol in prompt for symbol in ("thrL", "thrA", "thrB"))


@pytest.mark.asyncio
async def test_agent_action_over_bundle_limit_is_recorded_as_rejected() -> None:
    registry, essentiality, modules = _evidence()
    schedule = _CandidateSchedule(registry, 0)
    state = GenomeState(frozenset())

    def toolkit(current: GenomeState) -> _AgentGeneTools:
        return _AgentGeneTools(
            registry=registry,
            essentiality=essentiality,
            modules=modules,
            schedule=schedule,
            state=current,
            mode="tool-rich",
            blind=None,
        )

    class TooManyGenes:
        model = "fixture"

        async def explore(
            self, context: ExplorerContext[GenomeState]
        ) -> ExplorerResult[DeleteGenes]:
            return ExplorerResult(actions=[DeleteGenes(genes=("b0001", "b0002"))])

        def format_prompt(self, context: ExplorerContext[GenomeState]) -> str:
            return "fixture"

    explorer = _BoundExplorer(
        TooManyGenes(),
        toolkit,
        lambda action: action,
        max_actions=1,
        max_genes_per_action=1,
    )

    result = await explorer.explore(
        ExplorerContext(
            goal="minimize",
            state_id="state",
            state=state,
            lineage=[],
            guidance=None,
        )
    )

    assert result.actions == []
    assert result.note == "adapter rejected 1 invalid action(s)"


def _evidence() -> tuple[GeneRegistry, EssentialityDataset, ModuleEvaluator]:
    registry = parse_ncbi_gff(FIXTURES / "mg1655_excerpt.gff3").registry
    essentiality = EssentialityDataset(
        EssentialityRecord(
            b_number=gene,
            classification="unknown",
            coverage="unknown",
            lb_call_raw=None,
            lb_ecipkm=None,
            m9_call_raw=None,
            m9_ecipkm=None,
        )
        for gene in sorted(registry.search_universe)
    )
    modules = ModuleEvaluator(
        registry=registry,
        entries={},
        wt_complete_module_ids=(),
        parser_semantics_version="fixture",
    )
    return registry, essentiality, modules
