"""Bounded model search with explicit blinded or annotated biological evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator
from yggdrisil import NavigatorExplorerPolicy
from yggdrisil.agents import ExplorerContext, ExplorerResult, NavigatorContext
from yggdrisil.types import EvaluationRecord

from yggdrisil_ecoli.actions import DeleteGenes
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

    def metadata(self, genes: pd.DataFrame) -> dict[str, object]:
        aliases = _aliases(genes, self.seed) if self.mode == "closed-book" else None
        return {
            **self.model_dump(mode="json"),
            "provider": "openrouter",
            "prompt_version": 3,
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
class _LimitedAgent:
    """Pass PydanticAI budgets through the framework's minimal agent.run interface."""

    inner: Any
    limits: Any

    async def run(self, prompt: str) -> Any:
        return await self.inner.run(prompt, usage_limits=self.limits)


@dataclass
class _Explorer:
    evidence: GeneTools
    config: AgentSearchConfig
    evaluator_ids: Mapping[str, str]
    limits: Any

    @property
    def model(self) -> str:
        return f"openrouter:{self.config.model}"

    def format_prompt(self, context: ExplorerContext[GenomeState]) -> str:
        tools = replace(self.evidence, deleted_genes=context.state.deleted_genes)
        deleted = sorted(tools.public(gene) for gene in context.state.deleted_genes)
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

        tools = replace(self.evidence, deleted_genes=context.state.deleted_genes)
        explorer = make_explorer(
            self.model,
            _action_type(tools, self.config.bundle_size),
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
    genes: pd.DataFrame,
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
        GeneTools(
            genes,
            modules,
            aliases=_aliases(genes, config.seed)
            if config.mode == "closed-book"
            else None,
            order=_gene_order(genes, config.seed),
        ),
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
