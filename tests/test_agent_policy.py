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
    assert metadata["scheduler"] == {
        "type": "recoverable_open_set",
        "version": 2,
        "active_width": 16,
        "parents_per_step": 4,
        "fallback_action_caps": [20, 10, 5, 1],
        "effective_fallback_action_caps": [1],
        "ordering": {
            "exploitation": [
                "deletion_count_desc",
                "fba_growth_desc",
                "essential_deleted_asc",
                "conditional_essential_deleted_asc",
                "ambiguous_deleted_asc",
                "unknown_deleted_asc",
                "broken_modules_asc",
            ],
            "diversity": "alternating_jaccard_distance_slots",
            "scheduling": "fewest_completed_attempts_first",
        },
        "viability": {
            "fba_feasible": True,
            "growth_rate": ">0",
            "resource_allocation_feasible_at_growth_floor": True,
        },
        "ranking_evidence_only": [
            "essentiality",
            "module_retention",
            "unknown_evidence",
        ],
    }
    assert "key" not in str(metadata).lower()
    with pytest.raises(ValueError, match="fixed OpenRouter model"):
        AgentSearchConfig(model="openrouter/free")
    with pytest.raises(ValueError, match="must not exceed 20"):
        AgentSearchConfig(model="openai/gpt-5.6-sol", bundle_size=21)


def test_agent_mapping_and_schedule_are_scoped_to_candidate_universe() -> None:
    registry, _essentiality, _modules = _evidence()
    candidates = {"b0001", "b0003"}
    schedule = _CandidateSchedule(registry, 7, candidates)
    blind = _BlindGeneMap(registry, 7, candidates)
    metadata = AgentSearchConfig(
        model="openai/gpt-5.6-sol",
        seed=7,
    ).metadata(registry, candidate_genes=candidates)

    assert set(schedule.genes) == candidates
    assert {blind.canonical("g0001"), blind.canonical("g0002")} == candidates
    with pytest.raises(ValueError, match="unknown blinded"):
        blind.canonical("g0003")
    assert metadata["candidate_order_sha256"] == schedule.fingerprint
    assert metadata["blind_map_sha256"] == blind.fingerprint


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
        tools = _AgentGeneTools(
            registry=registry,
            essentiality=essentiality,
            modules=modules,
            schedule=schedule,
            state=current,
            mode="closed-book",
            blind=blind,
            max_genes_per_action=config.bundle_size,
        )
        return tools

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
        tools = _AgentGeneTools(
            registry=registry,
            essentiality=essentiality,
            modules=modules,
            schedule=schedule,
            state=current,
            mode="tool-rich",
            blind=None,
            max_genes_per_action=config.bundle_size,
        )
        return tools

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


def test_candidate_preview_can_fill_every_configured_bundle() -> None:
    registry, essentiality, modules = _evidence()
    config = AgentSearchConfig(
        model="openai/gpt-5.6-sol",
        mode="closed-book",
        seed=11,
        bundle_size=20,
        max_actions=4,
    )
    blind = _BlindGeneMap(registry, config.seed)
    schedule = _CandidateSchedule(registry, config.seed)
    state = GenomeState(frozenset())
    requested_counts: list[int] = []

    class RecordingTools(_AgentGeneTools):
        def list_candidates(self, page: int = 0, count: int = 24) -> dict[str, object]:
            requested_counts.append(count)
            return super().list_candidates(page, count)

    def toolkit(current: GenomeState) -> _AgentGeneTools:
        return RecordingTools(
            registry=registry,
            essentiality=essentiality,
            modules=modules,
            schedule=schedule,
            state=current,
            mode="closed-book",
            blind=blind,
            max_genes_per_action=config.bundle_size,
        )

    _format_explorer_prompt(
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

    assert requested_counts == [100]


def test_prompt_treats_action_size_as_a_ceiling_and_rotates_preview() -> None:
    registry, essentiality, modules = _evidence()
    config = AgentSearchConfig(
        model="openai/gpt-5.6-sol",
        mode="closed-book",
        seed=11,
        bundle_size=20,
        max_actions=2,
    )
    blind = _BlindGeneMap(registry, config.seed)
    schedule = _CandidateSchedule(registry, config.seed)
    requested_pages: list[int] = []

    class RecordingTools(_AgentGeneTools):
        def list_candidates(self, page: int = 0, count: int = 24) -> dict[str, object]:
            requested_pages.append(page)
            return super().list_candidates(page, count)

    def toolkit(current: GenomeState) -> _AgentGeneTools:
        return RecordingTools(
            registry=registry,
            essentiality=essentiality,
            modules=modules,
            schedule=schedule,
            state=current,
            mode="closed-book",
            blind=blind,
            max_genes_per_action=config.bundle_size,
        )

    prompt = _format_explorer_prompt(
        ExplorerContext(
            goal="minimize",
            state_id="state",
            state=GenomeState(frozenset()),
            lineage=[],
            guidance="CANDIDATE_PREVIEW_PAGE: 2",
        ),
        toolkit,
        config,
    )

    assert requested_pages == [2]
    assert "maximum is a ceiling, not a target" in prompt
    assert "one-gene action is valid" in prompt
    assert "full-size" not in prompt


def test_closed_book_tools_reject_unexposed_ids_and_unbounded_analysis() -> None:
    registry, essentiality, modules = _evidence()
    blind = _BlindGeneMap(registry, 17)
    tools = _AgentGeneTools(
        registry=registry,
        essentiality=essentiality,
        modules=modules,
        schedule=_CandidateSchedule(registry, 17),
        state=GenomeState(frozenset()),
        mode="closed-book",
        blind=blind,
        max_genes_per_action=20,
    )
    preview = tools.list_candidates(count=1)
    candidates = preview["candidates"]
    assert isinstance(candidates, list) and candidates
    first = candidates[0]
    assert isinstance(first, dict)
    exposed = str(first["gene_id"])
    guessed = next(
        blind.public(gene)
        for gene in registry.search_universe
        if blind.public(gene) != exposed
    )

    with pytest.raises(ValueError, match="not exposed") as inspect_error:
        tools.inspect_gene(guessed)
    with pytest.raises(ValueError, match="not exposed") as analyze_error:
        tools.analyze_bundle([guessed])
    with pytest.raises(ValueError, match="1 to 20"):
        tools.analyze_bundle([])
    with pytest.raises(ValueError, match="1 to 20"):
        tools.analyze_bundle([exposed] * 21)

    errors = f"{inspect_error.value} {analyze_error.value}"
    assert guessed in errors
    assert all(gene not in errors for gene in registry.search_universe)


@pytest.mark.asyncio
async def test_bound_explorer_reuses_exact_prompt_toolkit() -> None:
    registry, essentiality, modules = _evidence()
    config = AgentSearchConfig(
        model="openai/gpt-5.6-sol",
        mode="tool-rich",
        seed=19,
        bundle_size=20,
        max_actions=2,
    )
    schedule = _CandidateSchedule(registry, config.seed)
    created: list[_AgentGeneTools] = []

    def toolkit(state: GenomeState) -> _AgentGeneTools:
        tools = _AgentGeneTools(
            registry=registry,
            essentiality=essentiality,
            modules=modules,
            schedule=schedule,
            state=state,
            mode="tool-rich",
            blind=None,
            max_genes_per_action=20,
        )
        created.append(tools)
        return tools

    first_gene = schedule.genes[0]

    class PromptingExplorer:
        model = "fixture"

        async def explore(
            self, context: ExplorerContext[GenomeState]
        ) -> ExplorerResult[DeleteGenes]:
            self.format_prompt(context)
            return ExplorerResult(actions=[DeleteGenes(genes=(first_gene,))])

        def format_prompt(self, context: ExplorerContext[GenomeState]) -> str:
            return _format_explorer_prompt(context, toolkit, config)

    explorer = _BoundExplorer(
        PromptingExplorer(),
        toolkit,
        lambda action: action,
        max_actions=2,
        max_genes_per_action=20,
    )
    context = ExplorerContext(
        goal="minimize",
        state_id="state",
        state=GenomeState(frozenset()),
        lineage=[],
        guidance=None,
    )

    explorer.format_prompt(context)
    result = await explorer.explore(context)

    assert result.actions == [DeleteGenes(genes=(first_gene,))]
    assert len(created) == 1


@pytest.mark.asyncio
async def test_closed_book_action_rejects_valid_but_unexposed_blind_id() -> None:
    registry, essentiality, modules = _evidence()
    blind = _BlindGeneMap(registry, 23)
    schedule = _CandidateSchedule(registry, 23)
    exposed_gene, guessed_gene = schedule.genes[:2]
    guessed_public = blind.public(guessed_gene)
    action_type = _bounded_action_type("closed-book", 20)

    def toolkit(state: GenomeState) -> _AgentGeneTools:
        tools = _AgentGeneTools(
            registry=registry,
            essentiality=essentiality,
            modules=modules,
            schedule=schedule,
            state=state,
            mode="closed-book",
            blind=blind,
            max_genes_per_action=20,
        )
        preview = tools.list_candidates(count=1)
        assert str(preview["candidates"]) != ""
        return tools

    class GuessingExplorer:
        model = "fixture"

        async def explore(
            self, context: ExplorerContext[GenomeState]
        ) -> ExplorerResult[object]:
            return ExplorerResult(actions=[action_type(genes=(guessed_public,))])

        def format_prompt(self, context: ExplorerContext[GenomeState]) -> str:
            return "fixture"

    explorer = _BoundExplorer(
        GuessingExplorer(),
        toolkit,
        lambda action: DeleteGenes(
            genes=tuple(blind.canonical(gene) for gene in action.genes)  # type: ignore[attr-defined]
        ),
        max_actions=2,
        max_genes_per_action=20,
    )

    result = await explorer.explore(
        ExplorerContext(
            goal="minimize",
            state_id="state",
            state=GenomeState(frozenset()),
            lineage=[],
            guidance=None,
        )
    )

    assert exposed_gene != guessed_gene
    assert result.actions == []
    assert result.note == "adapter rejected 1 invalid action(s)"
    assert guessed_public not in result.note
    assert guessed_gene not in result.note


@pytest.mark.asyncio
async def test_agent_action_over_bundle_limit_is_recorded_as_rejected() -> None:
    registry, essentiality, modules = _evidence()
    schedule = _CandidateSchedule(registry, 0)
    state = GenomeState(frozenset())

    def toolkit(current: GenomeState) -> _AgentGeneTools:
        tools = _AgentGeneTools(
            registry=registry,
            essentiality=essentiality,
            modules=modules,
            schedule=schedule,
            state=current,
            mode="tool-rich",
            blind=None,
            max_genes_per_action=1,
        )
        tools.list_candidates(count=100)
        return tools

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


@pytest.mark.asyncio
async def test_agent_rejects_prior_and_same_response_duplicate_actions() -> None:
    registry, essentiality, modules = _evidence()
    schedule = _CandidateSchedule(registry, 0)
    state = GenomeState(frozenset())

    def toolkit(current: GenomeState) -> _AgentGeneTools:
        tools = _AgentGeneTools(
            registry=registry,
            essentiality=essentiality,
            modules=modules,
            schedule=schedule,
            state=current,
            mode="tool-rich",
            blind=None,
            max_genes_per_action=20,
        )
        tools.list_candidates(count=100)
        return tools

    class RepeatingExplorer:
        model = "fixture"

        async def explore(
            self, context: ExplorerContext[GenomeState]
        ) -> ExplorerResult[DeleteGenes]:
            return ExplorerResult(
                actions=[
                    DeleteGenes(genes=("b0001",)),
                    DeleteGenes(genes=("b0002",)),
                    DeleteGenes(genes=("b0002",)),
                ]
            )

        def format_prompt(self, context: ExplorerContext[GenomeState]) -> str:
            return "fixture"

    explorer = _BoundExplorer(
        RepeatingExplorer(),
        toolkit,
        lambda action: action,
        attempted_actions=lambda _state_id: frozenset({("b0001",)}),
        max_actions=3,
        max_genes_per_action=20,
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

    assert result.actions == [DeleteGenes(genes=("b0002",))]
    assert result.note == "adapter rejected 2 invalid action(s)"


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
