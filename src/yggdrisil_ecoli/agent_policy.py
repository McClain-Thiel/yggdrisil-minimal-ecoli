"""Bounded model search with explicit blinded or annotated biological evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from functools import partial
from importlib.metadata import version
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)
from yggdrisil import NavigatorExplorerPolicy, ReadOnlyStateGraph
from yggdrisil.agents import (
    ExplorerContext,
    ExplorerResult,
    ToolInputError,
)
from yggdrisil.agents.adk import UsageLimits
from yggdrisil.exceptions import UnknownStateError
from yggdrisil.types import EvaluationRecord

from yggdrisil_ecoli.actions import DeleteGenes
from yggdrisil_ecoli.open_set import (
    SCHEDULER_VERSION,
    OpenSetConfig,
    RecoverableOpenSetSelector,
)
from yggdrisil_ecoli.policies import viability_eligibility
from yggdrisil_ecoli.scorers.base import ScalarMetric
from yggdrisil_ecoli.scorers.modules import ModuleEvaluator
from yggdrisil_ecoli.state import GenomeState
from yggdrisil_ecoli.tools.genes import GeneTools


class AgentSearchConfig(BaseModel):
    """Model, experimental arm, and per-invocation budgets."""

    model_config = ConfigDict(frozen=True)
    model: str
    mode: Literal["closed-book", "tool-rich", "research"] = "closed-book"
    orchestration: Literal["recoverable", "manager"] = "recoverable"
    edison: bool = False
    require_initial_research: bool = False
    edison_cache_path: Path = Path("runs/edison.sqlite")
    session_path: Path = Path("runs/agents.sqlite")
    seed: int = 0
    send_model_seed: bool = True
    reasoning_effort: (
        Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None
    ) = None
    temperature: float | None = Field(default=0.0, ge=0, le=2)
    provider_route: str | None = None
    provider_fallback_allowed: bool = True
    provider_max_price: dict[str, float] | None = None
    bundle_size: int = Field(default=1, gt=0, le=100)
    max_actions: int = Field(default=2, gt=0)
    open_set: OpenSetConfig = OpenSetConfig()
    max_model_requests: int = Field(default=8, gt=0)
    max_tool_calls: int = Field(default=16, gt=0)
    max_gene_inspections: int = Field(default=200, gt=0)
    max_output_tokens: int = Field(default=800, gt=0)
    max_manager_output_tokens: int = Field(default=8192, gt=0)
    max_cost_per_call_usd: Decimal = Field(default=Decimal("0.02"), gt=0)

    @model_validator(mode="after")
    def compatible_mode(self) -> AgentSearchConfig:
        if self.orchestration == "manager" and self.mode == "closed-book":
            raise ValueError("manager orchestration requires real gene identities")
        if self.mode == "research" and self.orchestration != "manager":
            raise ValueError("research mode requires manager orchestration")
        if self.edison and self.mode != "research":
            raise ValueError("Edison requires research mode")
        if self.require_initial_research and self.mode != "research":
            raise ValueError("Initial research requires research mode")
        return self

    @field_validator("model")
    @classmethod
    def fixed_model(cls, model: str) -> str:
        model = model.removeprefix("openrouter:")
        if "/" not in model or model in {"openrouter/auto", "openrouter/free"}:
            raise ValueError("model must be a fixed OpenRouter model id: vendor/model")
        return model

    @property
    def tool_names(self) -> tuple[str, ...]:
        tools: tuple[str, ...] = ("analyze_deletion_bundle",)
        if self.mode != "closed-book":
            tools += ("inspect_genes", "inspect_kegg_module")
            if self.orchestration != "manager":
                tools += ("list_deletion_candidates",)
        if self.orchestration == "manager":
            tools += ("resolve_gene_ids", "get_deleted_genes", "deletion_delta")
        return tools

    @property
    def candidate_preview_count(self) -> int:
        return min(
            100,
            self.max_gene_inspections,
            max(8, self.bundle_size * self.max_actions * 2),
        )

    @property
    def settings(self) -> dict[str, Any]:
        settings: dict[str, Any] = {
            "max_tokens": self.max_output_tokens,
            "extra_body": {
                "provider": {
                    "require_parameters": True,
                    "data_collection": "deny",
                },
                "usage": {"include": True},
            },
        }
        if self.send_model_seed:
            settings["seed"] = self.seed
        if self.provider_route is not None:
            settings["extra_body"]["provider"].update(
                {
                    "only": [self.provider_route],
                    "order": [self.provider_route],
                    "allow_fallbacks": self.provider_fallback_allowed,
                }
            )
        if self.provider_max_price is not None:
            settings["extra_body"]["provider"]["max_price"] = self.provider_max_price
        if self.reasoning_effort is not None:
            settings["extra_body"]["reasoning"] = {"effort": self.reasoning_effort}
        if self.temperature is not None:
            settings["temperature"] = self.temperature
        return settings

    def metadata(self, genes: pd.DataFrame) -> dict[str, object]:
        aliases = _aliases(genes, self.seed) if self.mode == "closed-book" else None
        return {
            **self.model_dump(mode="json"),
            "provider": "openrouter",
            "prompt_version": 22,
            "scheduler_version": SCHEDULER_VERSION,
            "agent_runtime": "google-adk",
            "google_adk": version("google-adk"),
            "settings": self.settings,
            "tools": list(self.tool_names),
            "candidate_order_sha256": hashlib.sha256(
                "\n".join(_gene_order(genes, self.seed)).encode()
            ).hexdigest(),
            "blind_map_version": 1 if aliases is not None else None,
            "blind_map_sha256": hashlib.sha256(
                json.dumps(aliases, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            if aliases is not None
            else None,
        }

    def usage_limits(self) -> UsageLimits:
        return UsageLimits(
            cost_limit=self.max_cost_per_call_usd,
            request_limit=self.max_model_requests,
            tool_calls_limit=self.max_tool_calls,
        )


def _gene_order(
    genes: pd.DataFrame, seed: int, namespace: str = "candidate"
) -> list[str]:
    return sorted(
        genes.index,
        key=lambda gene: hashlib.sha256(f"{namespace}:{seed}:{gene}".encode()).digest(),
    )


def _aliases(genes: pd.DataFrame, seed: int) -> dict[str, str]:
    return {
        gene: f"g{index:04d}"
        for index, gene in enumerate(_gene_order(genes, seed, "blind"), start=1)
    }


def _action_type(tools: GeneTools, bundle_size: int) -> type[DeleteGenes]:
    """Let Pydantic validate and translate model IDs before they reach the search."""

    class ProposedDeletion(DeleteGenes):
        genes: tuple[str, ...] = Field(min_length=1, max_length=bundle_size)

        @field_validator("genes")
        @classmethod
        def validate_genes(cls, genes: tuple[str, ...]) -> tuple[str, ...]:
            if len(set(genes)) != len(genes):
                raise ValueError("deletion action contains duplicate genes")
            action = DeleteGenes(genes=tuple(tools.canonical(gene) for gene in genes))
            if tools.deleted_genes.intersection(action.genes):
                raise ValueError("action includes an already deleted gene")
            return tuple(sorted(genes, key=tools.canonical))

    return ProposedDeletion


@dataclass
class _Explorer:
    evidence: GeneTools
    config: AgentSearchConfig
    evaluator_ids: Mapping[str, str]
    limits: UsageLimits
    selector: RecoverableOpenSetSelector | None = None
    graph: ReadOnlyStateGraph[GenomeState, DeleteGenes] | None = None
    run_id: str | None = None
    last_trace: list[dict[str, Any]] = field(default_factory=list, init=False)

    @property
    def model(self) -> str:
        return f"openrouter:{self.config.model}"

    def format_prompt(
        self, context: ExplorerContext[GenomeState], tools: GeneTools | None = None
    ) -> str:
        tools = tools or replace(
            self.evidence,
            deleted_genes=context.state.deleted_genes,
            max_gene_inspections=self.config.max_gene_inspections,
        )
        managed = self.config.orchestration == "manager"
        guidance = (
            {}
            if managed
            else (json.loads(context.guidance) if context.guidance else {})
        )
        deleted = sorted(tools.public(gene) for gene in context.state.deleted_genes)
        return json.dumps(
            {
                "goal": context.goal,
                "evidence_mode": self.config.mode,
                "state_id": context.state_id,
                "deleted_gene_count": len(deleted),
                "deleted_gene_ids_first_64": deleted[:64],
                "evaluations": _active_metrics(context.evaluations, self.evaluator_ids),
                "candidate_preview": tools.list_deletion_candidates(
                    page=guidance.get("candidate_preview_page", 0),
                    count=self.config.candidate_preview_count,
                ),
                "recovery": guidance,
                "max_actions": self.config.max_actions,
                "max_genes_per_action": self.config.bundle_size,
                "max_gene_inspections": self.config.max_gene_inspections,
                **(
                    {
                        "manager_guidance": context.guidance,
                    }
                    if managed
                    else {}
                ),
            },
            sort_keys=True,
        )

    async def explore(
        self, context: ExplorerContext[GenomeState]
    ) -> ExplorerResult[DeleteGenes]:
        from yggdrisil.agents.adk import make_explorer

        from yggdrisil_ecoli.research import memory_tools

        tools = replace(
            self.evidence,
            deleted_genes=context.state.deleted_genes,
            exposed_ids=None if self.config.orchestration == "manager" else set(),
            max_bundle_size=self.config.bundle_size,
            max_gene_inspections=self.config.max_gene_inspections,
        )
        explorer = make_explorer(
            self.model,
            _action_type(tools, self.config.bundle_size),
            tools=[getattr(tools, name) for name in self.config.tool_names]
            + (
                memory_tools(self.evidence, self.graph)
                if self.graph is not None and self.run_id is not None
                else []
            ),
            instructions=(
                "Propose direct deletion actions for the current MG1655 state. "
                "Use the manager's checkpoint, guidance and cited shared evidence. "
                "Any validated canonical MG1655 b-number still present is allowed. "
                "Resolve literature identifiers explicitly; never guess ambiguous mappings. "
                "Only protein-coding genes in the prepared registry are actionable; "
                "skip unmatched identifiers, including noncoding loci in published intervals. "
                "Use compare_saved_gene_set only for saved documents with metadata.gene_ids "
                "and reference NC_000913.3; never invent a target key. The initial dossier "
                "is prose, not a structured deletion set. Resolve identifiers mentioned in "
                "it in batches of at most 100. Resolve returns remaining_gene_ids; "
                "inspect only unambiguously resolved genes still present. "
                "The preview and inspect_genes share max_gene_inspections "
                "per turn. Already-deleted and already-inspected genes return only a status. "
                "The preview is your general shortlist. Use it plus one focused literature-derived shortlist; avoid exhaustive "
                "catalogues. Analyze each proposed bundle once, then submit actions immediately. "
                "If no defensible proposal remains, return no actions and explain why in note. "
                "All deletion IDs refer to MG1655; the experiment goal specifies the starting genotype. "
                "Report findings, uncertainties and suggested plan changes in note. "
                "The research specialist owns shared documents. Evaluation happens after you "
                "propose an action; do not assert biological viability from literature. "
                "When evaluated bundles succeed, try larger coherent bundles; split failed "
                "combinations into smaller bundles. The size limit is a ceiling, not a target. "
                "Respect max_actions and max_genes_per_action."
                if self.config.orchestration == "manager"
                else (
                    "Propose direct deletion actions using only candidate IDs and evidence from "
                    "this invocation. Only use genes exposed in the preview or candidate-list "
                    "tool. Do not use web or literature knowledge. Essentiality, modules and "
                    "unknown annotations rank risk; viability requires positive feasible FBA "
                    "and resource-allocation feasibility at the fixed 0.1/h growth floor. "
                    "The action-size maximum and fallback ceiling are not targets: choose "
                    "each size independently from 1 to max_genes_per_action. Do not repeat "
                    "previous sibling actions; learn from lethal siblings. Shortlist from the "
                    "preview, then use at most one analyze_deletion_bundle call on the final "
                    "bundle. Respect max_actions and max_genes_per_action in the prompt."
                )
            ),
            prompt=partial(self.format_prompt, tools=tools),
            usage_limits=self.limits,
            model_settings=self.config.settings,
            session_path=self.config.session_path if self.run_id else None,
            user_id=self.run_id or "baseline",
            session_id=f"explorer-{context.state_id}",
        )
        if self.config.orchestration == "manager":
            instruction = explorer.agent.instruction
            assert isinstance(instruction, str)
            explorer.agent.instruction = instruction + "\nPersistent plan: {user:plan?}"
        self.last_trace = []
        try:
            result = await explorer.explore(context)
        finally:
            self.last_trace = explorer.last_trace
        seen = (
            set(self.selector.attempted_actions(context.state_id))
            if self.selector is not None
            else set()
        )
        if self.graph is not None and self.run_id is not None:
            seen.update(
                event.action.genes
                for event in self.graph.proposal_events(
                    run_id=self.run_id, state_id=context.state_id
                )
                if event.parent_id == context.state_id
            )
        actions = []
        for proposed in result.actions[: self.config.max_actions]:
            action = DeleteGenes(
                genes=tuple(tools.canonical(gene) for gene in proposed.genes)
            )
            if action.genes not in seen:
                actions.append(DeleteGenes(genes=action.genes))
                seen.add(action.genes)
        rejected = min(len(result.actions), self.config.max_actions) - len(actions)
        note = result.note
        if rejected:
            note = (
                f"{note + '; ' if note else ''}omitted {rejected} duplicate action(s)"
            )
        return ExplorerResult(
            actions=actions,
            note=note,
            trace=result.trace,
        )


def make_agent_policy(
    *,
    genes: pd.DataFrame,
    modules: ModuleEvaluator,
    config: AgentSearchConfig,
    evaluator_ids: Mapping[str, str],
    graph: ReadOnlyStateGraph[GenomeState, DeleteGenes] | None = None,
    run_id: str | None = None,
    goal: str | None = None,
) -> NavigatorExplorerPolicy[GenomeState, DeleteGenes]:
    """Pair bounded model exploration with deterministic, recoverable scheduling."""
    from dotenv import load_dotenv

    load_dotenv(Path.cwd() / ".env", override=False)
    load_dotenv(Path.home() / ".env", override=False)
    limits = config.usage_limits()
    evidence = GeneTools(
        genes,
        modules,
        aliases=_aliases(genes, config.seed) if config.mode == "closed-book" else None,
        order=_gene_order(genes, config.seed),
    )
    if config.orchestration == "manager":
        if graph is None or run_id is None:
            raise ValueError("manager orchestration requires graph and run_id")
        return _make_managed_policy(
            evidence, config, evaluator_ids, limits, graph, run_id, goal
        )
    selector = RecoverableOpenSetSelector(
        evaluator_ids=evaluator_ids,
        max_action_size=config.bundle_size,
        config=config.open_set,
        seed=config.seed,
        candidate_count=len(genes),
        candidate_page_size=config.candidate_preview_count,
        public_gene_id=evidence.public,
    )
    return NavigatorExplorerPolicy(
        None,
        _Explorer(
            evidence, config, evaluator_ids, limits, selector=selector, run_id=run_id
        ),
        max_requests=config.open_set.parents_per_step,
        request_selector=selector,
        tolerate_explorer_failures=True,
        goal=goal
        or "Minimize the MG1655 protein-coding genome for aerobic M9 glucose at 37 C while retaining predicted viability.",
    )


def _make_managed_policy(
    evidence: GeneTools,
    config: AgentSearchConfig,
    evaluator_ids: Mapping[str, str],
    limits: UsageLimits,
    graph: ReadOnlyStateGraph[GenomeState, DeleteGenes],
    run_id: str,
    goal: str | None,
) -> NavigatorExplorerPolicy[GenomeState, DeleteGenes]:
    from google.adk import Agent
    from yggdrisil.agents.adk import (
        AdkInvocation,
        AdkNavigator,
        make_model,
        retryable_tool,
    )

    from yggdrisil_ecoli.research import make_research_agent, memory_tools
    from yggdrisil_ecoli.tools.edison import EdisonLiterature

    eligible = viability_eligibility(evaluator_ids)
    edison = (
        EdisonLiterature(config.edison_cache_path, run_id) if config.edison else None
    )

    def get_state(state_id: str) -> dict[str, object]:
        """Read a stored genome and its evaluations; copy an existing ID exactly."""
        try:
            node = graph.get_state(state_id)
        except UnknownStateError as exc:
            raise ToolInputError(f"Unknown state_id {state_id!r}") from exc
        return {
            "state_id": state_id,
            "deleted_gene_ids": sorted(node.state.deleted_genes),
            "evaluations": _active_metrics(graph.evaluations(state_id), evaluator_ids),
        }

    class Request(BaseModel):
        state_id: str
        guidance: str | None = None

        @field_validator("state_id")
        @classmethod
        def viable_parent(cls, state_id: str) -> str:
            get_state(state_id)
            if not eligible(graph.get_state(state_id), graph.evaluations(state_id)):
                raise ValueError(
                    "Select a parent with positive feasible FBA growth AND feasible RBA"
                )
            return state_id

    class Plan(BaseModel):
        plan: str = Field(
            min_length=1,
            description="Persistent plan, evidence, open questions and next steps",
        )
        requests: list[Request] = Field(max_length=config.open_set.parents_per_step)

        @computed_field
        def continue_on_empty(self) -> bool:
            # An empty worker result must reach the manager before the search stops.
            return bool(self.requests)

    async def before_navigator(callback_context: Any) -> None:
        if edison:
            await edison.poll_pending()
            jobs = edison.snapshots()
            callback_context.state["user:edison_jobs"] = jobs
            if any(job.get("formatted_answer") for job in jobs):
                callback_context.state["user:evidence_retrieved"] = True

    def after_navigator(callback_context: Any) -> None:
        output = callback_context.state.get("user:plan")
        if not output:
            raise ValueError("Navigator did not save its plan")
        if config.require_initial_research and not callback_context.state.get(
            "user:literature_search_count", 0
        ):
            raise ValueError(
                "Adaptive research requires a successful Europe PMC search before "
                "exploration; the dossier alone does not satisfy this requirement"
            )

    def instructions(context: Any) -> str:
        return (
            "Minimize the MG1655-derived starting genotype specified in the experiment goal. Choose existing viable states and "
            "delegate direct deletion proposals to the explorer. Learn from actual evaluated outcomes. "
            f"Select at most {config.open_set.parents_per_step} parents per turn; each explorer proposes at most "
            f"{config.max_actions} actions deleting at most {config.bundle_size} genes. "
            "Keep a persistent plan describing evidence, uncertainty, open questions and next steps. "
            "Published reductions may guide targets; never replace the experiment's starting genotype. Extend beyond published targets. "
            "The initial dossier is prose, not a structured deletion set. Use compare_saved_gene_set "
            "only for listed documents with metadata.gene_ids and reference NC_000913.3. "
            "Never invent document keys. Read the dossier for guidance and let the explorer "
            "resolve its candidate identifiers and compute deletion deltas. "
            "Your job is to choose a parent and strategy; delegate detailed gene screening to the explorer. "
            + (
                "Read the initial dossier when present. Use research_literature to search Europe PMC "
                "for larger deletion targets, gene interactions, and explanations of failed bundles. "
                "Research is allowed while exploration is progressing; do not wait until you are stuck. "
                "Revisit the literature when choosing a new deletion block or revising a failed strategy. "
                "At most one research call per turn. The research agent saves findings directly in shared "
                "state; the explorer can read them immediately. "
                if config.mode == "research"
                else "External retrieval is disabled. "
            )
            + (
                "Before returning your first exploration requests, call research_literature with a "
                "focused request for a fresh Europe PMC search relevant to the starting genotype. "
                "Reading the frozen dossier does not count. "
                if config.require_initial_research
                and not context.state.get("user:literature_search_count", 0)
                else ""
            )
            + (
                "Edison is a separate slow service. Continue exploration while Edison is pending; "
                "never wait or spend turns polling. "
                if edison
                else "Live Edison is disabled; ordinary Europe PMC research remains available. "
                if config.mode == "research"
                else ""
            )
            + "Return a complete plan and exploration requests before the request budget runs out.\n"
            + "PERSISTENT_PLAN: "
            + json.dumps(context.state.get("user:plan", {}))
            + "\nSHARED_MEMORY_KEYS: "
            + json.dumps(sorted(context.state.get("user:documents", {})))
            + "\nINITIAL_DOSSIER: "
            + json.dumps(context.state.get("user:initial_dossier"))
            + "\nEDISON_JOBS: "
            + json.dumps(context.state.get("user:edison_jobs", []))
        )

    tools: list[Callable[..., Any]] = [
        get_state,
        *memory_tools(evidence, graph),
    ]
    manager = Agent(
        name="navigator",
        model=make_model(
            f"openrouter:{config.model}",
            {**config.settings, "max_tokens": config.max_manager_output_tokens},
        ),
        instruction=instructions,
        include_contents="none",
        output_schema=Plan,
        output_key="user:plan",
        tools=[retryable_tool(t) for t in tools],
        sub_agents=[make_research_agent(evidence, config, graph, edison)]
        if config.mode == "research"
        else [],
        before_agent_callback=before_navigator,
        after_agent_callback=after_navigator,
    )
    navigator = AdkNavigator(
        AdkInvocation(
            manager,
            session_path=config.session_path,
            user_id=run_id,
            usage_limits=config.usage_limits(),
        )
    )
    return NavigatorExplorerPolicy(
        navigator,
        _Explorer(evidence, config, evaluator_ids, limits, graph=graph, run_id=run_id),
        goal=goal
        or "Minimize MG1655 from its full reference genome for aerobic M9 glucose at 37 C; extend beyond published reductions.",
        max_requests=config.open_set.parents_per_step,
    )


def _active_metrics(
    records: Sequence[EvaluationRecord], evaluator_ids: Mapping[str, str]
) -> dict[str, dict[str, ScalarMetric]]:
    by_id = {record.evaluator_id: record.metrics for record in records}
    try:
        return {name: by_id[identity] for name, identity in evaluator_ids.items()}
    except KeyError as exc:
        raise ValueError(f"state lacks active evaluation: {exc.args[0]}") from exc
