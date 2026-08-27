"""OpenRouter-backed Yggdrisil policy with explicit evidence exposure modes."""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from collections.abc import Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path
from typing import Annotated, Any, Generic, Literal, Protocol, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field, create_model, field_validator
from yggdrisil import NavigatorExplorerPolicy
from yggdrisil.agents import (
    ExplorerContext,
    ExplorerResult,
    NavigatorContext,
)

from yggdrisil_ecoli.actions import DeleteGenes
from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.data.essentiality import EssentialityDataset
from yggdrisil_ecoli.data.registry import GeneRegistry
from yggdrisil_ecoli.scorers.modules import ModuleEvaluator
from yggdrisil_ecoli.state import GenomeState
from yggdrisil_ecoli.tools.genes import GeneTools

AgentMode = Literal["closed-book", "tool-rich"]
PROMPT_VERSION = 2
BLIND_MAP_VERSION = 1


class AgentPolicyError(RuntimeError):
    """A bounded model invocation failed at the external provider boundary."""


@dataclass(frozen=True, slots=True)
class AgentSearchConfig:
    """Reproducible, secret-free configuration for one model arm."""

    model: str
    mode: AgentMode = "closed-book"
    seed: int = 0
    bundle_size: int = 1
    max_actions: int = 2
    max_navigator_requests: int = 1
    max_model_requests: int = 6
    max_tool_calls: int = 16
    max_output_tokens: int = 800
    max_cost_per_call_usd: Decimal = Decimal("0.02")

    def __post_init__(self) -> None:
        normalized = self.model.removeprefix("openrouter:")
        if "/" not in normalized or normalized in {
            "openrouter/auto",
            "openrouter/free",
        }:
            raise ValueError(
                "model must be a fixed OpenRouter model id such as vendor/model"
            )
        object.__setattr__(self, "model", normalized)
        for name in (
            "bundle_size",
            "max_actions",
            "max_navigator_requests",
            "max_model_requests",
            "max_tool_calls",
            "max_output_tokens",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive")
        if self.max_cost_per_call_usd <= 0:
            raise ValueError("max_cost_per_call_usd must be positive")

    @property
    def model_ref(self) -> str:
        return f"openrouter:{self.model}"

    def metadata(self, registry: GeneRegistry) -> dict[str, object]:
        schedule = _CandidateSchedule(registry, self.seed)
        blind = (
            _BlindGeneMap(registry, self.seed) if self.mode == "closed-book" else None
        )
        tool_names = ["analyze_deletion_bundle"]
        if self.mode == "tool-rich":
            tool_names = [
                "list_deletion_candidates",
                "inspect_gene_evidence",
                "analyze_deletion_bundle",
                "inspect_kegg_module",
            ]
        return {
            "provider": "openrouter",
            "model": self.model,
            "mode": self.mode,
            "prompt_version": PROMPT_VERSION,
            "pydantic_ai": version("pydantic-ai"),
            "seed": self.seed,
            "bundle_size": self.bundle_size,
            "max_actions": self.max_actions,
            "max_navigator_requests": self.max_navigator_requests,
            "max_model_requests": self.max_model_requests,
            "max_tool_calls": self.max_tool_calls,
            "max_output_tokens": self.max_output_tokens,
            "max_cost_per_call_usd": str(self.max_cost_per_call_usd),
            "temperature": 0.0,
            "provider_routing": {
                "require_parameters": True,
                "data_collection": "deny",
            },
            "candidate_order_sha256": schedule.fingerprint,
            "blind_map_version": BLIND_MAP_VERSION if blind else None,
            "blind_map_sha256": blind.fingerprint if blind else None,
            "tools": tool_names,
        }


class _BlindDeleteGenes(BaseModel):
    model_config = ConfigDict(frozen=True)

    genes: tuple[str, ...]

    @field_validator("genes")
    @classmethod
    def validate_genes(cls, genes: tuple[str, ...]) -> tuple[str, ...]:
        if not genes:
            raise ValueError("deletion action must contain at least one gene")
        invalid = sorted(
            gene
            for gene in genes
            if len(gene) != 5 or not gene.startswith("g") or not gene[1:].isdigit()
        )
        if invalid:
            raise ValueError(f"expected blinded gene ids, got {invalid}")
        if len(set(genes)) != len(genes):
            raise ValueError("deletion action contains duplicate genes")
        return tuple(sorted(genes))


class _BlindGeneMap:
    def __init__(self, registry: GeneRegistry, seed: int) -> None:
        canonical = sorted(
            registry.search_universe,
            key=lambda gene: _seeded_digest("blind", seed, gene),
        )
        self._to_public = {
            gene: f"g{index:04d}" for index, gene in enumerate(canonical, start=1)
        }
        self._to_canonical = {public: gene for gene, public in self._to_public.items()}
        self.fingerprint = _mapping_hash(self._to_public)

    def public(self, canonical: str) -> str:
        return self._to_public[canonical]

    def canonical(self, public: str) -> str:
        try:
            return self._to_canonical[public]
        except KeyError as exc:
            raise ValueError(f"unknown blinded gene id: {public}") from exc


class _CandidateSchedule:
    def __init__(self, registry: GeneRegistry, seed: int) -> None:
        self.genes = tuple(
            sorted(
                registry.search_universe,
                key=lambda gene: _seeded_digest("candidate", seed, gene),
            )
        )
        self.fingerprint = hashlib.sha256("\n".join(self.genes).encode()).hexdigest()


class _AgentGeneTools:
    def __init__(
        self,
        *,
        registry: GeneRegistry,
        essentiality: EssentialityDataset,
        modules: ModuleEvaluator,
        schedule: _CandidateSchedule,
        state: GenomeState,
        mode: AgentMode,
        blind: _BlindGeneMap | None,
    ) -> None:
        self.registry = registry
        self.essentiality = essentiality
        self.modules = modules
        self.schedule = schedule
        self.state = state
        self.mode = mode
        self.blind = blind
        self.rich = GeneTools(
            registry=registry,
            essentiality=essentiality,
            modules=modules,
        )

    def public(self, canonical: str) -> str:
        return self.blind.public(canonical) if self.blind else canonical

    def canonical(self, public: str) -> str:
        canonical = self.blind.canonical(public) if self.blind else public
        self.registry.require(canonical)
        return canonical

    def deleted_public_ids(self) -> list[str]:
        return sorted(self.public(gene) for gene in self.state.deleted_genes)

    def list_candidates(self, page: int = 0, count: int = 24) -> dict[str, object]:
        if page < 0:
            raise ValueError("page must be non-negative")
        if count < 1 or count > 50:
            raise ValueError("count must be between 1 and 50")
        available = [
            gene for gene in self.schedule.genes if gene not in self.state.deleted_genes
        ]
        start = page * count
        genes = available[start : start + count]
        return {
            "page": page,
            "count": len(genes),
            "remaining_candidates": len(available),
            "candidates": [self._gene_evidence(gene) for gene in genes],
        }

    def inspect_gene(self, public: str) -> dict[str, object]:
        return self._gene_evidence(self.canonical(public), detailed=True)

    def analyze_bundle(self, public_ids: list[str]) -> dict[str, object]:
        canonical = [self.canonical(gene) for gene in public_ids]
        if self.mode == "tool-rich":
            return self.rich.analyze_gene_set(canonical)
        classes = Counter(
            self.essentiality.record(gene).classification for gene in canonical
        )
        module_result = self.modules.score_deleted(
            self.state.deleted_genes.union(canonical)
        )
        records = [self.registry.require(gene) for gene in canonical]
        return {
            "gene_ids": public_ids,
            "essentiality_summary": {
                category: classes.get(category, 0)
                for category in (
                    "essential",
                    "conditionally_essential",
                    "nonessential",
                    "ambiguous",
                    "unknown",
                )
            },
            "projected_modules_complete": module_result.n_complete,
            "projected_modules_broken": module_result.n_broken,
            "model_coverage": {
                "modeled": sum(record.in_iml1515 for record in records),
                "unmodeled": sum(not record.in_iml1515 for record in records),
            },
        }

    def inspect_module(
        self, module_id: str, public_deleted_ids: list[str] | None = None
    ) -> dict[str, object]:
        if self.mode != "tool-rich":
            raise ValueError("module details are unavailable in closed-book mode")
        deleted = [self.canonical(gene) for gene in public_deleted_ids or ()]
        return self.rich.get_module_info(module_id, deleted_genes=deleted)

    def _gene_evidence(
        self, canonical: str, *, detailed: bool = False
    ) -> dict[str, object]:
        record = self.registry.require(canonical)
        essentiality = self.essentiality.record(canonical)
        if self.mode == "closed-book":
            evidence: dict[str, object] = {
                "gene_id": self.public(canonical),
                "essentiality": essentiality.classification,
                "experimental_coverage": essentiality.coverage,
                "metabolic_model_coverage": record.in_iml1515,
                "has_ko_mapping": bool(record.ko_ids),
                "module_membership_count": len(
                    self.modules.modules_for_kos(set(record.ko_ids))
                ),
            }
            if detailed:
                evidence["condition_disagreement"] = essentiality.condition_disagreement
                evidence["evidence_conflict"] = essentiality.evidence_conflict
            return evidence
        return {
            **self.rich.get_gene_info(canonical),
            "essentiality_evidence": self.essentiality.detail(canonical),
        }


_ACTIVE_TOOLS: ContextVar[_AgentGeneTools | None] = ContextVar(
    "yggdrisil_ecoli_agent_tools", default=None
)


def list_deletion_candidates(page: int = 0, count: int = 24) -> dict[str, object]:
    """List a reproducible page of undeleted candidate genes and allowed evidence."""

    return _require_tools().list_candidates(page, count)


def inspect_gene_evidence(gene_id: str) -> dict[str, object]:
    """Inspect the allowed scientific evidence for one candidate gene identifier."""

    return _require_tools().inspect_gene(gene_id)


def analyze_deletion_bundle(gene_ids: list[str]) -> dict[str, object]:
    """Check aggregate evidence for a proposed direct deletion bundle."""

    return _require_tools().analyze_bundle(gene_ids)


def inspect_kegg_module(
    module_id: str, deleted_gene_ids: list[str] | None = None
) -> dict[str, object]:
    """Inspect a KEGG module after an optional proposed deletion set."""

    return _require_tools().inspect_module(module_id, deleted_gene_ids)


def make_agent_policy(
    *,
    registry: GeneRegistry,
    essentiality: EssentialityDataset,
    modules: ModuleEvaluator,
    config: AgentSearchConfig,
) -> NavigatorExplorerPolicy[GenomeState, DeleteGenes]:
    """Build a bounded OpenRouter navigator/explorer policy."""

    _load_openrouter_key()
    from pydantic_ai import UsageLimits
    from pydantic_ai.models.openrouter import OpenRouterModelSettings
    from yggdrisil.agents.pydantic_ai import make_explorer, make_navigator

    schedule = _CandidateSchedule(registry, config.seed)
    blind = (
        _BlindGeneMap(registry, config.seed) if config.mode == "closed-book" else None
    )

    def toolkit(state: GenomeState) -> _AgentGeneTools:
        return _AgentGeneTools(
            registry=registry,
            essentiality=essentiality,
            modules=modules,
            schedule=schedule,
            state=state,
            mode=config.mode,
            blind=blind,
        )

    settings = OpenRouterModelSettings(
        max_tokens=config.max_output_tokens,
        temperature=0.0,
        seed=config.seed,
        openrouter_provider={
            "require_parameters": True,
            "data_collection": "deny",
        },
        openrouter_usage={"include": True},
    )
    limits = UsageLimits(
        cost_limit=config.max_cost_per_call_usd,
        request_limit=config.max_model_requests,
        tool_calls_limit=config.max_tool_calls,
        output_tokens_limit=config.max_model_requests * config.max_output_tokens,
    )
    navigator = make_navigator(
        config.model_ref,
        instructions=(
            "Navigate an E. coli genome-deletion search. Select only existing "
            "frontier state ids. Prefer states with zero essential deletions, "
            "feasible positive FBA growth, fewer remaining genes, and fewer "
            "broken modules. Unknown evidence is risk, not proof of safety."
        ),
        prompt=lambda context: _format_navigator_prompt(
            context, config.max_navigator_requests
        ),
    )
    action_type = _bounded_action_type(config.mode, config.bundle_size)
    tool_functions: list[Callable[..., Any]] = [analyze_deletion_bundle]
    if config.mode == "tool-rich":
        tool_functions = [
            list_deletion_candidates,
            inspect_gene_evidence,
            analyze_deletion_bundle,
            inspect_kegg_module,
        ]
    explorer = make_explorer(
        config.model_ref,
        action_type,
        tools=tool_functions,
        instructions=(
            "Explore one E. coli deletion state using only evidence in the prompt "
            "and the supplied tools. Do not use web or literature knowledge. "
            "Only propose identifiers returned in the candidate preview or by "
            "list_deletion_candidates in this invocation. Do not inspect every "
            "candidate: shortlist using the preview, then batch-check only the "
            "final bundle. Avoid essential genes and prefer experimentally "
            "nonessential candidates. Return direct deletion actions only."
        ),
        prompt=lambda context: _format_explorer_prompt(context, toolkit, config),
    )
    for component in (navigator, explorer):
        component.agent.model_settings = settings
        component.agent = _UsageLimitedAgent(component.agent, limits)

    def translate(action: DeleteGenes | _BlindDeleteGenes) -> DeleteGenes:
        if isinstance(action, DeleteGenes):
            return DeleteGenes(genes=action.genes)
        assert blind is not None
        return DeleteGenes(genes=tuple(blind.canonical(gene) for gene in action.genes))

    bound_explorer = _BoundExplorer(
        explorer,
        toolkit,
        translate,
        max_actions=config.max_actions,
        max_genes_per_action=config.bundle_size,
    )
    return NavigatorExplorerPolicy(
        navigator,
        bound_explorer,
        goal=(
            "Minimize the MG1655 protein-coding genome for aerobic M9 glucose "
            "at 37 C while retaining predicted viability."
        ),
        max_requests=config.max_navigator_requests,
    )


SourceAction = TypeVar("SourceAction")


class _ExplorerLike(Protocol[SourceAction]):
    model: str | None

    async def explore(
        self, context: ExplorerContext[GenomeState]
    ) -> ExplorerResult[SourceAction]: ...

    def format_prompt(self, context: ExplorerContext[GenomeState]) -> str: ...


class _BoundExplorer(Generic[SourceAction]):
    def __init__(
        self,
        inner: _ExplorerLike[SourceAction],
        toolkit: Callable[[GenomeState], _AgentGeneTools],
        translate: Callable[[SourceAction], DeleteGenes],
        *,
        max_actions: int,
        max_genes_per_action: int,
    ) -> None:
        self.inner = inner
        self.model = inner.model
        self.toolkit = toolkit
        self.translate = translate
        self.max_actions = max_actions
        self.max_genes_per_action = max_genes_per_action

    async def explore(
        self, context: ExplorerContext[GenomeState]
    ) -> ExplorerResult[DeleteGenes]:
        toolkit = self.toolkit(context.state)
        token = _ACTIVE_TOOLS.set(toolkit)
        try:
            result = await self.inner.explore(context)
        finally:
            _ACTIVE_TOOLS.reset(token)
        actions: list[DeleteGenes] = []
        rejected: list[str] = []
        for action in result.actions[: self.max_actions]:
            try:
                translated = self.translate(action)
                if len(translated.genes) > self.max_genes_per_action:
                    raise ValueError("action exceeds the configured bundle size")
                for gene in translated.genes:
                    toolkit.registry.require(gene)
                    if gene in context.state.deleted_genes:
                        raise ValueError("action includes an already deleted gene")
                actions.append(translated)
            except (DataValidationError, KeyError, ValueError) as exc:
                rejected.append(str(exc))
        note = result.note
        if rejected:
            suffix = f"adapter rejected {len(rejected)} invalid action(s)"
            note = f"{note}; {suffix}" if note else suffix
        return ExplorerResult(actions=actions, note=note, trace=result.trace)

    def format_prompt(self, context: ExplorerContext[GenomeState]) -> str:
        return self.inner.format_prompt(context)


class _UsageLimitedAgent:
    """Small adapter because Yggdrisil intentionally does not own provider limits."""

    def __init__(self, inner: Any, limits: Any) -> None:
        self.inner = inner
        self.limits = limits

    async def run(self, prompt: str) -> Any:
        try:
            return await self.inner.run(prompt, usage_limits=self.limits)
        except Exception as exc:
            raise AgentPolicyError(f"{type(exc).__name__}: {exc}") from exc


def _format_navigator_prompt(context: NavigatorContext, max_requests: int) -> str:
    recent = []
    for item in context.recent:
        recent.append(
            {
                "state_id": item["state_id"],
                "created_step": item["created_step"],
                "evaluations": _scalar_evaluations(item.get("evaluations", [])),
                "note": context.summaries.get(str(item["state_id"])),
            }
        )
    return "\n".join(
        [
            f"GOAL: {context.goal}",
            f"STEP: {context.status.step}",
            f"SELECT_AT_MOST: {max_requests}",
            f"FRONTIER_IDS: {json.dumps(context.frontier_ids)}",
            f"RECENT_STATES: {json.dumps(recent, sort_keys=True)}",
            "Select existing frontier state ids with the strongest viable evidence.",
        ]
    )


def _format_explorer_prompt(
    context: ExplorerContext[GenomeState],
    toolkit_factory: Callable[[GenomeState], _AgentGeneTools],
    config: AgentSearchConfig,
) -> str:
    toolkit = toolkit_factory(context.state)
    deleted = toolkit.deleted_public_ids()
    shown_deleted = deleted[:64]
    return "\n".join(
        [
            f"GOAL: {context.goal}",
            f"EVIDENCE_MODE: {config.mode}",
            f"CURRENT_STATE_ID: {context.state_id}",
            f"DELETED_GENE_COUNT: {len(deleted)}",
            f"DELETED_GENE_IDS_FIRST_64: {json.dumps(shown_deleted)}",
            "CURRENT_EVALUATIONS: "
            + json.dumps(_scalar_evaluations(context.evaluations), sort_keys=True),
            "CANDIDATE_PREVIEW: "
            + json.dumps(toolkit.list_candidates(page=0, count=8), sort_keys=True),
            f"Return at most {config.max_actions} direct deletion actions; each action "
            f"must contain 1 to {config.bundle_size} candidate gene ids.",
            "Use at most one analyze_deletion_bundle call on the final proposed "
            "bundle; do not call it separately for every candidate.",
        ]
    )


def _scalar_evaluations(records: Sequence[Any]) -> list[dict[str, object]]:
    summaries: list[dict[str, object]] = []
    for record in records:
        if isinstance(record, dict):
            evaluator = record.get("evaluator")
            metrics = record.get("metrics", {})
        else:
            evaluator = record.evaluator
            metrics = record.metrics
        scalar_metrics = {
            str(key): value
            for key, value in metrics.items()
            if value is None or isinstance(value, (bool, int, float, str))
        }
        summaries.append({"evaluator": evaluator, "metrics": scalar_metrics})
    return summaries


def _require_tools() -> _AgentGeneTools:
    tools = _ACTIVE_TOOLS.get()
    if tools is None:
        raise RuntimeError("agent gene tools are unavailable outside exploration")
    return tools


def _load_openrouter_key() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError as exc:
        raise ImportError("install the agents extra: uv sync --extra agents") from exc
    load_dotenv(Path.home() / ".env", override=False)
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise DataValidationError(
            "OPENROUTER_API_KEY is missing; add it to ~/.env, never to Git or CLI args"
        )


def _seeded_digest(namespace: str, seed: int, gene: str) -> bytes:
    return hashlib.sha256(f"{namespace}:{seed}:{gene}".encode()).digest()


def _mapping_hash(mapping: dict[str, str]) -> str:
    payload = json.dumps(mapping, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _bounded_action_type(
    mode: AgentMode, bundle_size: int
) -> type[DeleteGenes] | type[_BlindDeleteGenes]:
    gene_tuple = Annotated[tuple[str, ...], Field(min_length=1, max_length=bundle_size)]
    base: type[BaseModel] = _BlindDeleteGenes if mode == "closed-book" else DeleteGenes
    model = create_model(
        f"{mode.title().replace('-', '')}DeleteGenes{bundle_size}",
        __base__=base,
        genes=(gene_tuple, ...),
    )
    return cast(type[DeleteGenes] | type[_BlindDeleteGenes], model)
