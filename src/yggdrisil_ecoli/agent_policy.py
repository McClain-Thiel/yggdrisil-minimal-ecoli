"""Bounded model search with explicit blinded or annotated biological evidence."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from yggdrisil import NavigatorExplorerPolicy
from yggdrisil.agents import ExplorerContext, ExplorerResult, NavigatorContext
from yggdrisil.types import EvaluationRecord

from yggdrisil_ecoli.actions import DeleteGenes
from yggdrisil_ecoli.data.essentiality import EssentialityDataset
from yggdrisil_ecoli.data.registry import GeneRegistry
from yggdrisil_ecoli.scorers.base import ScalarMetric
from yggdrisil_ecoli.scorers.modules import ModuleEvaluator
from yggdrisil_ecoli.state import GenomeState
from yggdrisil_ecoli.tools.genes import GeneTools


class AgentSearchConfig(BaseModel):
    """Model, experimental arm, and per-invocation budgets."""

    model_config = ConfigDict(frozen=True)
    model: str
    mode: Literal["closed-book", "tool-rich"] = "closed-book"
    seed: int = 0
    bundle_size: int = Field(default=1, gt=0)
    max_actions: int = Field(default=2, gt=0)
    max_navigator_requests: int = Field(default=1, gt=0)
    max_model_requests: int = Field(default=6, gt=0)
    max_tool_calls: int = Field(default=16, gt=0)
    max_output_tokens: int = Field(default=800, gt=0)
    max_cost_per_call_usd: Decimal = Field(default=Decimal("0.02"), gt=0)

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
        if self.mode == "tool-rich":
            tools += (
                "list_deletion_candidates",
                "inspect_gene_evidence",
                "inspect_kegg_module",
            )
        return tools

    @property
    def settings(self) -> dict[str, Any]:
        return {
            "max_tokens": self.max_output_tokens,
            "temperature": 0.0,
            "seed": self.seed,
            "openrouter_provider": {
                "require_parameters": True,
                "data_collection": "deny",
            },
            "openrouter_usage": {"include": True},
        }

    def metadata(self, registry: GeneRegistry) -> dict[str, object]:
        view = _GeneView(registry, self)
        return {
            **self.model_dump(mode="json"),
            "provider": "openrouter",
            "prompt_version": 3,
            "pydantic_ai": version("pydantic-ai"),
            "settings": self.settings,
            "tools": list(self.tool_names),
            "candidate_order_sha256": hashlib.sha256(
                "\n".join(view.order).encode()
            ).hexdigest(),
            "blind_map_version": 1 if view.blinded else None,
            "blind_map_sha256": hashlib.sha256(
                json.dumps(view.aliases, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            if view.blinded
            else None,
        }


class _GeneView:
    """One deterministic candidate order and a reversible display-ID mapping."""

    def __init__(self, registry: GeneRegistry, config: AgentSearchConfig) -> None:
        def ordered(namespace: str) -> list[str]:
            return sorted(
                registry.search_universe,
                key=lambda gene: hashlib.sha256(
                    f"{namespace}:{config.seed}:{gene}".encode()
                ).digest(),
            )

        self.order = ordered("candidate")
        self.blinded = config.mode == "closed-book"
        self.aliases = {
            gene: f"g{index:04d}" if self.blinded else gene
            for index, gene in enumerate(ordered("blind"), start=1)
        }
        self.canonical_ids = {alias: gene for gene, alias in self.aliases.items()}

    def canonical(self, alias: str) -> str:
        try:
            return self.canonical_ids[alias]
        except KeyError as exc:
            raise ValueError(f"unknown candidate gene id: {alias}") from exc


@dataclass
class _GeneTools:
    evidence: GeneTools
    view: _GeneView
    state: GenomeState

    def list_deletion_candidates(
        self, page: int = 0, count: int = 24
    ) -> dict[str, object]:
        """List a reproducible page of undeleted candidates and allowed evidence."""
        if page < 0 or not 1 <= count <= 50:
            raise ValueError("page must be non-negative and count must be 1 to 50")
        available = [
            gene for gene in self.view.order if gene not in self.state.deleted_genes
        ]
        candidates = available[page * count : (page + 1) * count]
        return {
            "page": page,
            "count": len(candidates),
            "remaining_candidates": len(available),
            "candidates": [
                self.inspect_gene_evidence(self.view.aliases[gene])
                for gene in candidates
            ],
        }

    def inspect_gene_evidence(self, gene_id: str) -> dict[str, object]:
        """Inspect allowed evidence for one candidate, without unblinding its identity."""
        gene = self.view.canonical(gene_id)
        if not self.view.blinded:
            return {
                **self.evidence.get_gene_info(gene),
                "essentiality_evidence": self.evidence.essentiality.detail(gene),
            }
        record = self.evidence.registry.require(gene)
        essentiality = self.evidence.essentiality.record(gene)
        return {
            "gene_id": gene_id,
            "essentiality": essentiality.classification,
            "experimental_coverage": essentiality.coverage,
            "metabolic_model_coverage": record.in_iml1515,
            "has_ko_mapping": bool(record.ko_ids),
            "module_membership_count": len(
                self.evidence.modules.modules_for_kos(set(record.ko_ids))
            ),
            "condition_disagreement": essentiality.condition_disagreement,
            "evidence_conflict": essentiality.evidence_conflict,
        }

    def analyze_deletion_bundle(self, gene_ids: list[str]) -> dict[str, object]:
        """Check a proposed bundle and module retention including existing deletions."""
        genes = [self.view.canonical(gene) for gene in gene_ids]
        projected = self.state.deleted_genes.union(genes)
        modules = self.evidence.modules.score_deleted(projected)
        if not self.view.blinded:
            return {
                **self.evidence.analyze_gene_set(sorted(projected)),
                "proposed_gene_ids": gene_ids,
                "projected_modules_complete": modules.n_complete,
                "projected_modules_broken": modules.n_broken,
            }
        classes = Counter(
            self.evidence.essentiality.record(gene).classification for gene in genes
        )
        modeled = sum(self.evidence.registry.require(gene).in_iml1515 for gene in genes)
        return {
            "gene_ids": gene_ids,
            "essentiality_summary": {
                name: classes[name]
                for name in (
                    "essential",
                    "conditionally_essential",
                    "nonessential",
                    "ambiguous",
                    "unknown",
                )
            },
            "projected_modules_complete": modules.n_complete,
            "projected_modules_broken": modules.n_broken,
            "model_coverage": {"modeled": modeled, "unmodeled": len(genes) - modeled},
        }

    def inspect_kegg_module(
        self, module_id: str, deleted_gene_ids: list[str] | None = None
    ) -> dict[str, object]:
        """Inspect a module after adding a proposed bundle to current deletions."""
        if self.view.blinded:
            raise ValueError("module details are unavailable in closed-book mode")
        deleted = self.state.deleted_genes.union(
            self.view.canonical(gene) for gene in deleted_gene_ids or ()
        )
        return self.evidence.get_module_info(module_id, deleted_genes=sorted(deleted))


def _action_type(
    view: _GeneView, state: GenomeState, bundle_size: int
) -> type[DeleteGenes]:
    """Let Pydantic validate and translate model IDs before they reach the search."""

    class ProposedDeletion(DeleteGenes):
        genes: tuple[str, ...] = Field(min_length=1, max_length=bundle_size)

        @field_validator("genes")
        @classmethod
        def validate_genes(cls, genes: tuple[str, ...]) -> tuple[str, ...]:
            if len(set(genes)) != len(genes):
                raise ValueError("deletion action contains duplicate genes")
            action = DeleteGenes(genes=tuple(view.canonical(gene) for gene in genes))
            if state.deleted_genes.intersection(action.genes):
                raise ValueError("action includes an already deleted gene")
            return action.genes

    return ProposedDeletion


@dataclass
class _LimitedAgent:
    """Pass PydanticAI budgets through the framework's minimal agent.run interface."""

    inner: Any
    limits: Any

    async def run(self, prompt: str) -> Any:
        return await self.inner.run(prompt, usage_limits=self.limits)


@dataclass
class _Explorer:
    evidence: GeneTools
    view: _GeneView
    config: AgentSearchConfig
    evaluator_ids: Mapping[str, str]
    limits: Any

    @property
    def model(self) -> str:
        return f"openrouter:{self.config.model}"

    def format_prompt(self, context: ExplorerContext[GenomeState]) -> str:
        tools = _GeneTools(self.evidence, self.view, context.state)
        deleted = sorted(
            self.view.aliases[gene] for gene in context.state.deleted_genes
        )
        return json.dumps(
            {
                "goal": context.goal,
                "evidence_mode": self.config.mode,
                "state_id": context.state_id,
                "deleted_gene_count": len(deleted),
                "deleted_gene_ids_first_64": deleted[:64],
                "evaluations": _active_metrics(context.evaluations, self.evaluator_ids),
                "candidate_preview": tools.list_deletion_candidates(count=8),
                "max_actions": self.config.max_actions,
                "max_genes_per_action": self.config.bundle_size,
            },
            sort_keys=True,
        )

    async def explore(
        self, context: ExplorerContext[GenomeState]
    ) -> ExplorerResult[DeleteGenes]:
        from yggdrisil.agents.pydantic_ai import make_explorer

        tools = _GeneTools(self.evidence, self.view, context.state)
        explorer = make_explorer(
            self.model,
            _action_type(self.view, context.state, self.config.bundle_size),
            tools=[getattr(tools, name) for name in self.config.tool_names],
            instructions=(
                "Propose direct deletion actions using only candidate IDs and evidence from "
                "this invocation. Do not use web or literature knowledge. Avoid essential "
                "genes; prefer experimentally nonessential candidates. Shortlist from the "
                "preview, then use at most one analyze_deletion_bundle call on the final "
                "bundle. Respect max_actions and max_genes_per_action in the prompt."
            ),
            prompt=self.format_prompt,
        )
        explorer.agent.model_settings = self.config.settings
        explorer.agent = _LimitedAgent(explorer.agent, self.limits)
        result = await explorer.explore(context)
        return ExplorerResult(
            actions=[
                DeleteGenes(genes=action.genes)
                for action in result.actions[: self.config.max_actions]
            ],
            note=result.note,
            trace=result.trace,
        )


def make_agent_policy(
    *,
    registry: GeneRegistry,
    essentiality: EssentialityDataset,
    modules: ModuleEvaluator,
    config: AgentSearchConfig,
    evaluator_ids: Mapping[str, str],
    evaluations: Callable[[str], list[EvaluationRecord]],
) -> NavigatorExplorerPolicy[GenomeState, DeleteGenes]:
    """Build the two model roles; the framework owns graph traversal and traces."""
    from dotenv import load_dotenv
    from pydantic_ai import UsageLimits
    from yggdrisil.agents.pydantic_ai import make_navigator

    load_dotenv(Path.home() / ".env", override=False)
    limits = UsageLimits(
        cost_limit=config.max_cost_per_call_usd,
        request_limit=config.max_model_requests,
        tool_calls_limit=config.max_tool_calls,
        output_tokens_limit=config.max_model_requests * config.max_output_tokens,
    )
    explorer = _Explorer(
        GeneTools(registry=registry, essentiality=essentiality, modules=modules),
        _GeneView(registry, config),
        config,
        evaluator_ids,
        limits,
    )

    def navigator_prompt(context: NavigatorContext) -> str:
        # The pinned framework omits evaluator IDs from its recent-state summaries.
        recent = [
            {
                **item,
                "evaluations": _active_metrics(
                    evaluations(item["state_id"]), evaluator_ids
                ),
            }
            for item in context.recent
        ]
        return json.dumps(
            {
                "goal": context.goal,
                "step": context.status.step,
                "select_at_most": config.max_navigator_requests,
                "frontier_ids": context.frontier_ids,
                "recent_states": recent,
                "notes": context.summaries,
            },
            sort_keys=True,
        )

    navigator = make_navigator(
        explorer.model,
        prompt=navigator_prompt,
        instructions=(
            "Select existing frontier states with zero essential deletions, "
            "feasible positive FBA growth, fewer remaining genes and fewer broken "
            "modules. Unknown evidence is risk, not proof of safety."
        ),
    )
    navigator.agent.model_settings = config.settings
    navigator.agent = _LimitedAgent(navigator.agent, limits)
    return NavigatorExplorerPolicy(
        navigator,
        explorer,
        max_requests=config.max_navigator_requests,
        goal="Minimize the MG1655 protein-coding genome for aerobic M9 glucose at 37 C while retaining predicted viability.",
    )


def _active_metrics(
    records: Sequence[EvaluationRecord], evaluator_ids: Mapping[str, str]
) -> dict[str, dict[str, ScalarMetric]]:
    by_id = {record.evaluator_id: record.metrics for record in records}
    try:
        return {name: by_id[identity] for name, identity in evaluator_ids.items()}
    except KeyError as exc:
        raise ValueError(f"state lacks active evaluation: {exc.args[0]}") from exc
