"""Bounded model search with explicit blinded or annotated biological evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from functools import partial
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from yggdrisil import MemoryUpdate, NavigatorExplorerPolicy, ReadOnlyStateGraph
from yggdrisil.agents import (
    ExplorerContext,
    ExplorerResult,
    NavigationPlan,
    ToolInputError,
    bind_memory_tools,
)
from yggdrisil.exceptions import UnknownStateError
from yggdrisil.types import EvaluationRecord

from yggdrisil_ecoli.actions import DeleteGenes
from yggdrisil_ecoli.constants import REFERENCE_ACCESSION
from yggdrisil_ecoli.open_set import (
    SCHEDULER_VERSION,
    OpenSetConfig,
    RecoverableOpenSetSelector,
)
from yggdrisil_ecoli.scorers.base import ScalarMetric
from yggdrisil_ecoli.scorers.modules import ModuleEvaluator
from yggdrisil_ecoli.state import GenomeState
from yggdrisil_ecoli.tools.genes import GeneTools

if TYPE_CHECKING:
    from pydantic_ai import RunContext, UsageLimits


class AgentSearchConfig(BaseModel):
    """Model, experimental arm, and per-invocation budgets."""

    model_config = ConfigDict(frozen=True)
    model: str
    mode: Literal["closed-book", "tool-rich", "research"] = "closed-book"
    orchestration: Literal["recoverable", "manager"] = "recoverable"
    seed: int = 0
    temperature: float | None = Field(default=0.0, ge=0, le=2)
    bundle_size: int = Field(default=1, gt=0, le=20)
    max_actions: int = Field(default=2, gt=0)
    open_set: OpenSetConfig = OpenSetConfig()
    max_model_requests: int = Field(default=6, gt=0)
    max_tool_calls: int = Field(default=16, gt=0)
    max_output_tokens: int = Field(default=800, gt=0)
    max_manager_output_tokens: int = Field(default=8192, gt=0)
    max_cost_per_call_usd: Decimal = Field(default=Decimal("0.02"), gt=0)

    @model_validator(mode="after")
    def compatible_mode(self) -> AgentSearchConfig:
        if self.orchestration == "manager" and self.mode == "closed-book":
            raise ValueError("manager orchestration requires real gene identities")
        if self.mode == "research" and self.orchestration != "manager":
            raise ValueError("research mode requires manager orchestration")
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
            tools += (
                "list_deletion_candidates",
                "inspect_gene_evidence",
                "inspect_kegg_module",
            )
        if self.orchestration == "manager":
            tools += ("resolve_gene_ids", "get_deleted_genes", "deletion_delta")
        return tools

    @property
    def candidate_preview_count(self) -> int:
        return min(50, max(8, self.bundle_size * self.max_actions * 2))

    @property
    def settings(self) -> dict[str, Any]:
        settings: dict[str, Any] = {
            "max_tokens": self.max_output_tokens,
            "seed": self.seed,
            "openrouter_provider": {
                "require_parameters": True,
                "data_collection": "deny",
            },
            "openrouter_usage": {"include": True},
        }
        if self.temperature is not None:
            settings["temperature"] = self.temperature
        return settings

    def metadata(self, genes: pd.DataFrame) -> dict[str, object]:
        aliases = _aliases(genes, self.seed) if self.mode == "closed-book" else None
        return {
            **self.model_dump(mode="json"),
            "provider": "openrouter",
            "prompt_version": 7,
            "scheduler_version": SCHEDULER_VERSION,
            "pydantic_ai": version("pydantic-ai"),
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

    def usage_limits(self, *, manager: bool = False) -> UsageLimits:
        from pydantic_ai import UsageLimits

        tokens = self.max_manager_output_tokens if manager else self.max_output_tokens
        return UsageLimits(
            cost_limit=self.max_cost_per_call_usd,
            request_limit=self.max_model_requests,
            tool_calls_limit=self.max_tool_calls,
            output_tokens_limit=self.max_model_requests * tokens,
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
            return action.genes

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

    @property
    def model(self) -> str:
        return f"openrouter:{self.config.model}"

    def format_prompt(
        self, context: ExplorerContext[GenomeState], tools: GeneTools | None = None
    ) -> str:
        tools = tools or replace(
            self.evidence, deleted_genes=context.state.deleted_genes
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
                **(
                    {
                        "manager_guidance": context.guidance,
                        "manager_checkpoint": context.checkpoint,
                        "shared_memory": [asdict(entry) for entry in context.memory],
                        "shared_memory_keys": context.memory_keys,
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
        from yggdrisil.agents.pydantic_ai import make_explorer

        tools = replace(
            self.evidence,
            deleted_genes=context.state.deleted_genes,
            exposed_ids=None if self.config.orchestration == "manager" else set(),
            max_bundle_size=self.config.bundle_size,
        )
        explorer = make_explorer(
            self.model,
            _action_type(tools, self.config.bundle_size),
            tools=[getattr(tools, name) for name in self.config.tool_names]
            + (
                bind_memory_tools(self.graph, self.run_id)
                + _gene_set_tools(self.graph, self.run_id, self.evidence)
                if self.graph is not None and self.run_id is not None
                else []
            ),
            instructions=(
                "Propose direct deletion actions for the current MG1655 state. "
                "Use the manager's checkpoint, guidance and cited shared evidence. "
                "Any validated canonical MG1655 b-number still present is allowed. "
                "Resolve literature identifiers explicitly; never guess ambiguous mappings. "
                "Use compare_saved_gene_set to walk toward a cited deletion set, then "
                "continue investigating additional deletions. Only use saved gene sets "
                "whose keys appear in shared_memory_keys; never invent a target key. "
                "The root is always MG1655. "
                "Report findings, uncertainties and suggested plan changes in note. "
                "Return shared memory updates only for reusable findings, with their "
                "expected revision and source metadata. Evaluation happens after you "
                "propose an action; do not assert biological viability from literature. "
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
            output_mode="prompted"
            if self.config.orchestration == "manager"
            else "tool",
        )
        graph, run_id = self.graph, self.run_id
        if graph is not None and run_id is not None:
            from pydantic_ai import ModelRetry

            def require_memory_revisions(output: Any) -> Any:
                errors = _memory_update_errors(graph, run_id, output.memory_updates)
                if errors:
                    raise ModelRetry("\n".join(errors))
                return output

            explorer.agent.output_validator(require_memory_revisions)
        result = await explorer.explore(context)
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
        for action in result.actions[: self.config.max_actions]:
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
            memory_updates=result.memory_updates,
        )


def make_agent_policy(
    *,
    genes: pd.DataFrame,
    modules: ModuleEvaluator,
    config: AgentSearchConfig,
    evaluator_ids: Mapping[str, str],
    graph: ReadOnlyStateGraph[GenomeState, DeleteGenes] | None = None,
    run_id: str | None = None,
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
            evidence, config, evaluator_ids, limits, graph, run_id
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
        _Explorer(evidence, config, evaluator_ids, limits, selector=selector),
        max_requests=config.open_set.parents_per_step,
        request_selector=selector,
        tolerate_explorer_failures=True,
        goal="Minimize the MG1655 protein-coding genome for aerobic M9 glucose at 37 C while retaining predicted viability.",
    )


def _memory_update_errors(
    graph: ReadOnlyStateGraph[GenomeState, DeleteGenes],
    namespace: str,
    updates: Sequence[MemoryUpdate],
) -> list[str]:
    revisions = {entry.key: entry.revision for entry in graph.memories(namespace)}
    errors = []
    for update in updates:
        revision = revisions.get(update.key, 0)
        if update.expected_revision != revision:
            errors.append(
                f"Shared document {update.key!r} needs expected_revision={revision}; "
                "revision 0 creates a new document."
            )
        revisions[update.key] = revision + 1
    return errors


def _gene_set_tools(
    graph: ReadOnlyStateGraph[GenomeState, DeleteGenes],
    run_id: str,
    evidence: GeneTools,
) -> list[Any]:
    def compare_saved_gene_set(
        key: str,
        state_id: str,
        offset: int = 0,
        count: int = 50,
    ) -> dict[str, object]:
        """Read a cited target set's remaining delta against an existing graph state."""
        try:
            entry = graph.memory(run_id, key)
        except KeyError as exc:
            keys = [entry.key for entry in graph.memories(run_id)]
            raise ToolInputError(
                f"Shared document {key!r} does not exist. Available keys: {keys}. "
                "Use an existing cited target, or propose from local gene evidence."
            ) from exc
        if entry.metadata.get("reference") != REFERENCE_ACCESSION:
            raise ToolInputError(
                "This document is not a validated MG1655 deletion target: "
                "saved gene set must specify reference NC_000913.3"
            )
        genes = entry.metadata.get("gene_ids")
        if not isinstance(genes, list) or not all(
            isinstance(gene, str) for gene in genes
        ):
            raise ToolInputError("saved gene set metadata must contain a gene_ids list")
        if not entry.metadata.get("source_url"):
            raise ToolInputError("saved gene set must cite its source_url")
        tools = replace(
            evidence, deleted_genes=graph.get_state(state_id).state.deleted_genes
        )
        return tools.deletion_delta(genes, offset, count)

    return [compare_saved_gene_set]


def _make_managed_policy(
    evidence: GeneTools,
    config: AgentSearchConfig,
    evaluator_ids: Mapping[str, str],
    limits: UsageLimits,
    graph: ReadOnlyStateGraph[GenomeState, DeleteGenes],
    run_id: str,
) -> NavigatorExplorerPolicy[GenomeState, DeleteGenes]:
    from pydantic_ai import ModelRetry
    from yggdrisil.agents.pydantic_ai import (
        DEFAULT_MANAGER_INSTRUCTIONS,
        make_navigator,
    )

    def get_state(state_id: str) -> dict[str, object]:
        """Read an existing state; copy its ID exactly from the supplied context."""
        try:
            node = graph.get_state(state_id)
        except UnknownStateError as exc:
            recent = [node.state_id for node in graph.states(limit=10, newest=True)]
            raise ToolInputError(
                f"Unknown state_id {state_id!r}. Recent existing state IDs: {recent}"
            ) from exc
        return {
            "state_id": node.state_id,
            "deleted_gene_ids": sorted(node.state.deleted_genes),
            "created_step": node.created_step,
            "evaluations": _active_metrics(
                graph.evaluations(node.state_id), evaluator_ids
            ),
        }

    tools = (
        [get_state]
        + bind_memory_tools(graph, run_id)
        + _gene_set_tools(graph, run_id, evidence)
        + [evidence.resolve_gene_ids, evidence.inspect_gene_evidence]
    )
    if config.mode == "research":
        from yggdrisil_ecoli.tools.literature import read_literature, search_literature

        tools += [search_literature, read_literature]
    manager = make_navigator(
        f"openrouter:{config.model}",
        tools=tools,
        usage_limits=config.usage_limits(manager=True),
        output_mode="prompted",
        model_settings={
            **config.settings,
            "max_tokens": config.max_manager_output_tokens,
        },
        instructions=DEFAULT_MANAGER_INSTRUCTIONS
        + (
            "\nMinimize MG1655, starting from its full reference genome. "
            "Choose existing states that pass the recorded FBA and resource-allocation "
            "checks. You own the persistent checkpoint; use any useful structure for "
            "your plan, evidence, open questions and progress, and return it every turn. "
            "Separate observed facts from hypotheses. Delegate bounded deletion "
            "proposals to worker 'explorer'. "
            "Learn from evaluated outcomes rather than assuming proposals succeeded. "
            "Use recent evaluations and worker reports already in your context; "
            "fetch a stored state only when needed information is absent. "
            "Published reduced genomes may inform a target, but never replace the root. "
            "To save a deletion target, return a memory update with metadata containing "
            "reference='NC_000913.3', canonical gene_ids and source_url. "
            "Further reductions beyond a retrieved target remain part of the task. "
            "The coordinator commits your shared memory updates before dispatching "
            "selected workers, so they may request keys you are saving in this plan. "
            + (
                "Begin with one focused literature search for published MG1655 genome "
                "reductions, and save useful sources in shared memory. Reuse retrieved "
                "evidence; search again only to resolve a concrete open question. "
                "Read at most one full-text page per turn. If research needs another "
                "turn, save progress, return no requests, and set continue_on_empty=true."
                if config.mode == "research"
                else "Use local annotations and shared records; external retrieval is disabled."
            )
            + f" Each invocation allows {config.max_model_requests} model requests "
            f"and {config.max_tool_calls} tool calls; reserve a request for your final plan."
        ),
    )

    retrieval_seen = any(
        event.get("role") == "tool_return" and event.get("tool") == "search_literature"
        for decision in graph.decisions()
        if decision.run_id == run_id
        for event in decision.tool_calls
    )

    def require_plan(context: RunContext[Any], plan: NavigationPlan) -> NavigationPlan:
        nonlocal retrieval_seen
        retrieval_seen = retrieval_seen or any(
            getattr(part, "tool_name", None) == "search_literature"
            and part.part_kind == "tool-return"
            for message in context.messages
            for part in message.parts
        )
        errors = _memory_update_errors(graph, run_id, plan.memory_updates)
        if not plan.checkpoint:
            errors.append(
                "Return a nonempty checkpoint with your current plan and next step. "
                "Do not discard the persistent manager plan between turns."
            )
        memory_keys = {entry.key for entry in graph.memories(run_id)}
        if config.mode == "research" and not retrieval_seen:
            errors.append(
                "Call search_literature to retrieve actual evidence before finishing "
                "this research plan; do not substitute remembered or invented sources."
            )
        if config.mode == "research" and not memory_keys and not plan.memory_updates:
            errors.append(
                "Save retrieved evidence in memory_updates. Return no worker requests "
                "and continue_on_empty=true to commit the evidence first."
            )
        for request in plan.requests:
            try:
                get_state(request.state_id)
            except ToolInputError as exc:
                errors.append(str(exc))
            missing = (
                set(request.memory_keys)
                - memory_keys
                - {update.key for update in plan.memory_updates}
            )
            if missing:
                errors.append(
                    f"Unknown worker memory keys: {sorted(missing)}. "
                    "Use an existing key or include the document in memory_updates."
                )
        if errors:
            raise ModelRetry(
                "\n".join(errors)
                + "\nReturn the complete corrected plan, including your nonempty "
                "checkpoint, requests, memory_updates and continue_on_empty."
            )
        return plan

    manager.agent.output_validator(require_plan)
    return NavigatorExplorerPolicy(
        manager,
        _Explorer(evidence, config, evaluator_ids, limits, graph=graph, run_id=run_id),
        goal=(
            "Minimize MG1655 for aerobic M9 glucose at 37 C; extend beyond published reductions."
            + (
                " First search the literature for published MG1655 reductions, save cited "
                "evidence in shared memory, and keep a nonempty checkpoint with the next step. "
                "Then delegate deletions from the full MG1655 root."
                if config.mode == "research"
                else ""
            )
        ),
        max_requests=config.open_set.parents_per_step,
        memory_enabled=True,
        commit_memory_before_exploration=True,
    )


def _active_metrics(
    records: Sequence[EvaluationRecord], evaluator_ids: Mapping[str, str]
) -> dict[str, dict[str, ScalarMetric]]:
    by_id = {record.evaluator_id: record.metrics for record in records}
    try:
        return {name: by_id[identity] for name, identity in evaluator_ids.items()}
    except KeyError as exc:
        raise ValueError(f"state lacks active evaluation: {exc.args[0]}") from exc
