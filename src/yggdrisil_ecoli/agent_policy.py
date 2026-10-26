"""Bounded model search with explicit blinded or annotated biological evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from functools import partial
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator
from yggdrisil import NavigatorExplorerPolicy
from yggdrisil.agents import ExplorerContext, ExplorerResult
from yggdrisil.types import EvaluationRecord

from yggdrisil_ecoli.actions import DeleteGenes
from yggdrisil_ecoli.open_set import (
    SCHEDULER_VERSION,
    OpenSetConfig,
    RecoverableOpenSetSelector,
)
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
    bundle_size: int = Field(default=1, gt=0, le=20)
    max_actions: int = Field(default=2, gt=0)
    open_set: OpenSetConfig = OpenSetConfig()
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
    def candidate_preview_count(self) -> int:
        return min(50, max(8, self.bundle_size * self.max_actions * 2))

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
            "prompt_version": 4,
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
    selector: RecoverableOpenSetSelector
    limits: Any

    @property
    def model(self) -> str:
        return f"openrouter:{self.config.model}"

    def format_prompt(
        self, context: ExplorerContext[GenomeState], tools: GeneTools | None = None
    ) -> str:
        tools = tools or replace(
            self.evidence, deleted_genes=context.state.deleted_genes
        )
        guidance = json.loads(context.guidance) if context.guidance else {}
        deleted = sorted(tools.public(gene) for gene in context.state.deleted_genes)
        return json.dumps(
            {
                "goal": context.goal,
                "evidence_mode": self.config.mode,
                "state_id": context.state_id,
                "deleted_gene_count": len(deleted),
                "deleted_gene_ids_first_64": deleted[:64],
                "evaluations": _active_metrics(
                    context.evaluations, self.selector.evaluator_ids
                ),
                "candidate_preview": tools.list_deletion_candidates(
                    page=guidance.get("candidate_preview_page", 0),
                    count=self.config.candidate_preview_count,
                ),
                "recovery": guidance,
                "max_actions": self.config.max_actions,
                "max_genes_per_action": self.config.bundle_size,
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
            exposed_ids=set(),
            max_bundle_size=self.config.bundle_size,
        )
        explorer = make_explorer(
            self.model,
            _action_type(tools, self.config.bundle_size),
            tools=[getattr(tools, name) for name in self.config.tool_names],
            instructions=(
                "Propose direct deletion actions using only candidate IDs and evidence from "
                "this invocation. Only use genes exposed in the preview or candidate-list "
                "tool. Do not use web or literature knowledge. Essentiality, modules and "
                "unknown annotations rank risk; only positive feasible FBA gates viability. "
                "The action-size maximum and fallback ceiling are not targets: choose "
                "each size independently from 1 to max_genes_per_action. Do not repeat "
                "previous sibling actions; learn from lethal siblings. Shortlist from the "
                "preview, then use at most one analyze_deletion_bundle call on the final "
                "bundle. Respect max_actions and max_genes_per_action in the prompt."
            ),
            prompt=partial(self.format_prompt, tools=tools),
        )
        # The framework forwards only the prompt; bind native PydanticAI run options.
        explorer.agent = SimpleNamespace(
            run=partial(
                explorer.agent.run,
                usage_limits=self.limits,
                model_settings=self.config.settings,
            )
        )
        result = await explorer.explore(context)
        seen = set(self.selector.attempted_actions(context.state_id))
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
        )


def make_agent_policy(
    *,
    genes: pd.DataFrame,
    modules: ModuleEvaluator,
    config: AgentSearchConfig,
    evaluator_ids: Mapping[str, str],
) -> NavigatorExplorerPolicy[GenomeState, DeleteGenes]:
    """Pair bounded model exploration with deterministic, recoverable scheduling."""
    from dotenv import load_dotenv
    from pydantic_ai import UsageLimits

    load_dotenv(Path.home() / ".env", override=False)
    limits = UsageLimits(
        cost_limit=config.max_cost_per_call_usd,
        request_limit=config.max_model_requests,
        tool_calls_limit=config.max_tool_calls,
        output_tokens_limit=config.max_model_requests * config.max_output_tokens,
    )
    evidence = GeneTools(
        genes,
        modules,
        aliases=_aliases(genes, config.seed) if config.mode == "closed-book" else None,
        order=_gene_order(genes, config.seed),
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
        _Explorer(evidence, config, selector, limits),
        max_requests=config.open_set.parents_per_step,
        request_selector=selector,
        tolerate_explorer_failures=True,
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
