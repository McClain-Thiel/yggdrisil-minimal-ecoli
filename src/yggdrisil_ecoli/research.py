"""Native ADK research delegation and shared, cited documents."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import replace
from typing import Any
from urllib.error import HTTPError

from google.adk import Agent
from google.adk.tools.tool_context import ToolContext
from google.genai import types
from pydantic import BaseModel, Field
from yggdrisil import ReadOnlyStateGraph
from yggdrisil.agents import ToolInputError
from yggdrisil.agents.adk import make_model, retryable_tool
from yggdrisil.exceptions import UnknownStateError

from yggdrisil_ecoli.actions import DeleteGenes
from yggdrisil_ecoli.constants import REFERENCE_ACCESSION
from yggdrisil_ecoli.state import GenomeState
from yggdrisil_ecoli.tools import literature
from yggdrisil_ecoli.tools.edison import EdisonLiterature
from yggdrisil_ecoli.tools.genes import GeneTools

logger = logging.getLogger(__name__)


def memory_tools(
    evidence: GeneTools,
    graph: ReadOnlyStateGraph[GenomeState, DeleteGenes],
    *,
    writable: bool = False,
) -> list[Callable[..., Any]]:
    def list_shared_memory(tool_context: ToolContext) -> list[str]:
        """List saved document keys; read a document before using its findings."""
        keys = list(tool_context.state.get("user:documents", {}))
        if tool_context.state.get("user:initial_dossier"):
            keys.append("literature/initial-dossier")
        return sorted(keys)

    def read_shared_memory(key: str, tool_context: ToolContext) -> dict[str, Any]:
        """Read a saved document. Prose notes have no structured metadata.gene_ids."""
        if key == "literature/initial-dossier" and tool_context.state.get(
            "user:initial_dossier"
        ):
            dossier = tool_context.state["user:initial_dossier"]
            return {
                "content": dossier["content"],
                "metadata": {
                    "source_url": dossier["source_url"],
                    "immutable": True,
                    "kind": "literature_note",
                },
            }
        documents = tool_context.state.get("user:documents", {})
        if key not in documents:
            raise ToolInputError(
                f"Document {key!r} does not exist. Available keys: {sorted(documents)}"
            )
        return dict(documents[key])

    def compare_saved_gene_set(
        key: str,
        state_id: str,
        tool_context: ToolContext,
        offset: int = 0,
        count: int = 50,
    ) -> dict[str, object]:
        """Compare a saved target with metadata.gene_ids and reference NC_000913.3.

        Read a listed document first. Prose notes, including the initial dossier,
        cannot be compared as gene sets.
        """
        document = read_shared_memory(key, tool_context)
        metadata = document["metadata"]
        if (
            metadata.get("reference") != REFERENCE_ACCESSION
            or "gene_ids" not in metadata
        ):
            raise ToolInputError(
                f"Document {key!r} has no structured MG1655 gene set. "
                "Use its prose as guidance, resolve candidate identifiers with resolve_gene_ids, "
                "and let the explorer check their deletion_delta. "
                "Only compare saved documents containing metadata.gene_ids and reference NC_000913.3."
            )
        try:
            node = graph.get_state(state_id)
        except UnknownStateError as exc:
            raise ToolInputError(f"Unknown state_id {state_id!r}") from exc
        return replace(evidence, deleted_genes=node.state.deleted_genes).deletion_delta(
            metadata["gene_ids"], offset, count
        )

    def save_shared_memory(
        key: str, content: str, metadata: dict[str, Any], tool_context: ToolContext
    ) -> dict[str, Any]:
        """Save cited findings. Targets require gene_ids, source_url and reference NC_000913.3.

        Use literature/ for notes and targets/ for resolved deletion sets. Revisions
        are unnecessary: the research specialist is the only document writer.
        """
        if key == "literature/initial-dossier":
            raise ToolInputError(
                "The initial dossier is immutable; save interpretation under another key"
            )
        if not key.startswith(("literature/", "targets/")) or not content.strip():
            raise ToolInputError(
                "Use a literature/ or targets/ key and nonempty content"
            )
        if not metadata.get("source_url") or not tool_context.state.get(
            "user:evidence_retrieved"
        ):
            raise ToolInputError(
                "Retrieve actual source evidence before saving a cited document"
            )
        if key.startswith("targets/") or any(
            k in metadata for k in ("gene_ids", "canonical_gene_ids")
        ):
            genes = metadata.get("gene_ids")
            if (
                not isinstance(genes, list)
                or not genes
                or not all(isinstance(g, str) for g in genes)
            ):
                raise ToolInputError(
                    "Use metadata.gene_ids with a nonempty canonical ID list"
                )
            unknown = set(genes) - set(evidence.genes.index)
            if unknown or len(genes) != len(set(genes)):
                raise ToolInputError(
                    f"Target needs unique IDs in the prepared registry; unmatched: {sorted(unknown)}"
                )
            if metadata.get("reference") != REFERENCE_ACCESSION:
                raise ToolInputError("Target needs reference='NC_000913.3'")
        document = {"content": content, "metadata": metadata}
        tool_context.state["user:documents"] = {
            **tool_context.state.get("user:documents", {}),
            key: document,
        }
        return {"key": key, **document}

    tools: list[Callable[..., Any]] = [
        list_shared_memory,
        read_shared_memory,
        compare_saved_gene_set,
    ]
    return tools + ([save_shared_memory] if writable else [])


def make_research_agent(
    evidence: GeneTools,
    config: Any,
    graph: ReadOnlyStateGraph[GenomeState, DeleteGenes],
    edison: EdisonLiterature | None,
) -> Agent:
    async def search_literature(
        query: str, tool_context: ToolContext, count: int = 5
    ) -> dict[str, object]:
        """Search Europe PMC for cited papers; start with one distinctive strain identifier."""
        try:
            result = await literature.search_literature(query, count)
        except HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504}:
                raise
            logger.warning("Literature search unavailable (HTTP %s)", exc.code)
            raise ToolInputError(
                f"Literature search unavailable (HTTP {exc.code}) after bounded retries; "
                "try a single distinctive strain identifier instead of combining terms. "
                "Check experimental conditions in the returned abstracts. If still unavailable, "
                "report that no source evidence was retrieved"
            ) from exc
        tool_context.state["user:evidence_retrieved"] = True
        tool_context.state["user:literature_search_count"] = (
            tool_context.state.get("user:literature_search_count", 0) + 1
        )
        return result

    async def read_literature(
        pmcid: str, tool_context: ToolContext, offset: int = 0, count: int = 12000
    ) -> dict[str, object]:
        """Read one accessible full-text page; use another source if unavailable."""
        try:
            result = await literature.read_literature(pmcid, offset, count)
        except HTTPError as exc:
            if exc.code not in {403, 404, 500, 502, 503, 504}:
                raise
            logger.warning("Full text %s unavailable (HTTP %s)", pmcid, exc.code)
            raise ToolInputError(
                f"Full text unavailable (HTTP {exc.code}); use another accessible source"
            ) from exc
        tool_context.state["user:evidence_retrieved"] = True
        return result

    async def before_research(callback_context: Any) -> types.Content | None:
        if callback_context.state.get("temp:research_called"):
            return types.Content(
                parts=[
                    types.Part(
                        text=json.dumps(
                            {
                                "note": "Research was already requested this turn. Reuse its findings and return a plan."
                            }
                        )
                    )
                ]
            )
        callback_context.state["temp:research_called"] = True
        return None

    class Report(BaseModel):
        note: str = Field(min_length=1)

    tools: list[Callable[..., Any]] = [
        search_literature,
        read_literature,
        evidence.resolve_gene_ids,
        *memory_tools(evidence, graph, writable=True),
    ]
    if edison:

        async def submit_edison(
            query: str, tool_context: ToolContext
        ) -> dict[str, Any]:
            """Submit one slow research job, or reuse the exact existing question. Return immediately."""
            assert edison is not None
            result = await edison.submit_edison(query)
            tool_context.state["user:edison_jobs"] = edison.snapshots()
            if result.get("formatted_answer"):
                tool_context.state["user:evidence_retrieved"] = True
            return result

        async def check_edison(
            task_id: str, tool_context: ToolContext, offset: int = 0, count: int = 16000
        ) -> dict[str, Any]:
            """Read one cached report page or check a pending job once; never wait or loop."""
            assert edison is not None
            result = await edison.check_edison(task_id, offset, count)
            tool_context.state["user:edison_jobs"] = edison.snapshots()
            if result.get("formatted_answer"):
                tool_context.state["user:evidence_retrieved"] = True
            return result

        tools += [submit_edison, check_edison]

    def instructions(context: Any) -> str:
        return (
            (
                "Research MG1655 genome reduction for aerobic M9 glucose at 37 C. You do not propose genome actions. "
                "Read shared documents for context, then perform a fresh Europe PMC search for the "
                "requested question. The initial dossier does not replace this search. "
                "Use at most two Europe PMC search queries "
                "and read at most one full-text page per question. Save what is supported and return; "
                "a complete literature review is unnecessary for one search decision. "
                "Begin search with a single distinctive strain identifier, then check experimental conditions "
                "in the returned abstracts; complex queries can fail even when simple queries work. "
                "Save concise findings with save_shared_memory, source_url, tested conditions and uncertainties. "
                "For targets resolve gene IDs and save metadata.gene_ids, reference='NC_000913.3', source_url. "
                "Only protein-coding genes in the prepared registry are actionable; exclude unmatched/noncoding loci. "
                "Publications suggest targets; they do not establish simulated viability. Return a short note. "
                + (
                    "Edison takes 13–20 minutes. At most ONE new question in this entire experiment. "
                    "Use submit_edison once, then immediately return its pending ID. Never wait or loop. "
                    "Reuse the exact saved question and existing reports; resolve completed reports into targets. "
                    "A pending job is not evidence for a target. "
                    if edison
                    else "Edison is disabled. "
                )
            )
            + "\nSHARED_MEMORY_KEYS: "
            + json.dumps(sorted(context.state.get("user:documents", {})))
            + "\nEDISON_JOBS: "
            + json.dumps(context.state.get("user:edison_jobs", []))
        )

    return Agent(
        name="research_literature",
        mode="single_turn",
        description="Answer one focused literature question and save cited, resolved findings for the navigator and explorer.",
        model=make_model(
            f"openrouter:{config.model}",
            {**config.settings, "max_tokens": config.max_manager_output_tokens},
        ),
        instruction=instructions,
        tools=[retryable_tool(t) for t in tools],
        before_agent_callback=before_research,
        output_schema=Report,
        output_key="research_report",
        include_contents="none",
    )
