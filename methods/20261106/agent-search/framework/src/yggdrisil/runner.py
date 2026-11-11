from __future__ import annotations

import asyncio
import json
import math
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Generic, TypeVar

from yggdrisil.evaluation import EvaluatorSuite, evaluator_cost, evaluator_identity
from yggdrisil.exceptions import GraphError, SerializationError
from yggdrisil.graph.sqlite import SQLiteStateGraph
from yggdrisil.limits import RunLimits, RunStatus
from yggdrisil.objective import Objective
from yggdrisil.policy import Decision, Policy, PolicyStepError, Proposal
from yggdrisil.problem import Problem
from yggdrisil.serialize import stable_hash
from yggdrisil.types import RunRecord, RunResult

State = TypeVar("State")
Action = TypeVar("Action")

_EVALUATION_COST_METADATA_KEY = "_yggdrisil_evaluation_cost"


def _utcnow_id() -> str:
    return datetime.now(UTC).strftime("run_%Y_%m_%d_%H%M%S_%f")


class Runner(Generic[State, Action]):
    """Evaluate states, persist decisions, and materialize proposed transitions."""

    def __init__(
        self,
        problem: Problem[State, Action],
        policy: Policy[Action],
        graph: SQLiteStateGraph[State, Action],
        limits: RunLimits,
        *,
        objective: Objective[State] | None = None,
        evaluators: EvaluatorSuite[State] | None = None,
        run_id: str | None = None,
        resume: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.problem = problem
        self.policy = policy
        self.graph = graph
        self.limits = limits
        self.objective = objective
        self.evaluators = evaluators
        self.metadata = metadata or {}
        self.resume = resume
        self._requested_run_id = run_id
        self.run_id = run_id or _utcnow_id()
        self._step = 0
        self._best_state_id: str | None = None
        self._best_score: float | None = None
        self._problem_fingerprint: str | None = None
        self._evaluation_cost = 0.0

    async def run(self) -> RunResult:
        started = time.monotonic()
        self._problem_fingerprint = _fingerprint_problem(self.problem)
        self._check_new_run_id()
        restored = self._restore()
        self._check_problem_fingerprint(restored or self.graph.latest_run())
        self._ensure_initial_state()
        goal_found = self._prepare_objective_scores()
        self._persist("running")
        stop_reason = "completed"

        try:
            evaluation_stop = await self._evaluate_existing_states(started)
            if evaluation_stop is not None:
                stop_reason = evaluation_stop
            if restored is not None and stop_reason == "completed":
                recovered_stop = await self._recover_uncheckpointed_step(
                    goal_found=goal_found,
                    started=started,
                )
                if recovered_stop is not None:
                    stop_reason = recovered_stop
            if stop_reason == "completed" and goal_found:
                stop_reason = "objective"

            while stop_reason == "completed":
                elapsed = time.monotonic() - started
                hit = self.limits.reached(
                    unique_states=len(self.graph),
                    step=self._step,
                    elapsed_s=elapsed,
                    evaluation_cost=None,
                )
                if hit:
                    stop_reason = hit
                    break

                try:
                    decisions = await self._policy_step(elapsed)
                except PolicyStepError as exc:
                    self._record_decisions(exc.decisions)
                    raise exc.cause from exc
                if decisions is None:
                    stop_reason = "max_wall_time_s"
                    break
                proposal_events = self._record_decisions(decisions)
                if not proposal_events:
                    if decisions:
                        self._step += 1
                        self._persist("running")
                    if any(decision.continue_on_empty for decision in decisions):
                        continue
                    stop_reason = "no_proposals"
                    break

                stopped = False
                for index, (proposal, event_id) in enumerate(proposal_events):
                    elapsed = time.monotonic() - started
                    hit = self.limits.reached(
                        unique_states=len(self.graph),
                        step=self._step,
                        elapsed_s=elapsed,
                        evaluation_cost=None,
                    )
                    if hit:
                        stop_reason = hit
                        self._skip_events(proposal_events[index:], f"skipped_{hit}")
                        stopped = True
                        break
                    try:
                        goal, child_id, edge_id, edge_created = self._apply(proposal)
                    except Exception as exc:
                        self.graph.finish_proposal_event(
                            event_id,
                            outcome="failed",
                            error=f"{type(exc).__name__}: {exc}",
                        )
                        self._skip_events(
                            proposal_events[index + 1 :],
                            "skipped_failure",
                        )
                        raise
                    self.graph.finish_proposal_event(
                        event_id,
                        outcome="created" if edge_created else "reused",
                        child_id=child_id,
                        edge_id=edge_id,
                    )
                    try:
                        evaluation_stop = await self._evaluate_state(
                            child_id,
                            started,
                        )
                    except Exception:
                        self._skip_events(
                            proposal_events[index + 1 :],
                            "skipped_failure",
                        )
                        raise
                    if evaluation_stop is not None:
                        stop_reason = evaluation_stop
                        self._skip_events(
                            proposal_events[index + 1 :],
                            f"skipped_{evaluation_stop}",
                        )
                        stopped = True
                        break
                    if goal:
                        stop_reason = "objective"
                        self._skip_events(
                            proposal_events[index + 1 :],
                            "skipped_objective",
                        )
                        stopped = True
                        break

                self._step += 1
                self._persist("running")
                if stopped:
                    break
        except Exception as exc:
            self._fail_pending_events(exc)
            self._persist("failed")
            raise

        elapsed_s = time.monotonic() - started
        result = RunResult(
            run_id=self.run_id,
            status="completed",
            stop_reason=stop_reason,
            step=self._step,
            unique_states=len(self.graph),
            edges=self.graph.edge_count(),
            elapsed_s=elapsed_s,
            limits=self.limits,
            best_state_id=self._best_state_id,
            best_score=self._best_score,
            evaluation_cost=self._evaluation_cost,
        )
        self._persist("completed", extra={"stop_reason": stop_reason})
        self._write_manifest(result)
        return result

    async def _policy_step(self, elapsed_s: float) -> list[Decision[Action]] | None:
        remaining = self._remaining_wall_time(elapsed_s)
        if remaining is None:
            return await self.policy.step(
                self.graph.readonly(),
                self._status(elapsed_s),
            )
        if remaining <= 0:
            return None
        timeout = asyncio.timeout(remaining)
        try:
            async with timeout:
                return await self.policy.step(
                    self.graph.readonly(),
                    self._status(elapsed_s),
                )
        except TimeoutError:
            if timeout.expired():
                self._checkpoint_interrupted_policy()
                return None
            raise

    def _checkpoint_interrupted_policy(self) -> None:
        drain = getattr(self.policy, "drain_interrupted_decisions", None)
        if not callable(drain):
            return
        decisions: list[Decision[Action]] = drain()
        if not decisions:
            return
        proposal_events = self._record_decisions(decisions)
        self._skip_events(proposal_events, "skipped_max_wall_time_s")
        self._step += 1
        self._persist("running")

    def _remaining_wall_time(self, elapsed_s: float) -> float | None:
        if self.limits.max_wall_time_s is None:
            return None
        return self.limits.max_wall_time_s - elapsed_s

    def _check_new_run_id(self) -> None:
        if self.resume:
            return
        try:
            self.graph.get_run(self.run_id)
        except KeyError:
            return
        raise GraphError(
            f"run {self.run_id!r} already exists; resume it or choose a new run_id"
        )

    def _restore(self) -> RunRecord | None:
        if not self.resume:
            return None
        record: RunRecord | None
        if self._requested_run_id is not None:
            try:
                record = self.graph.get_run(self._requested_run_id)
            except KeyError:
                return None
        else:
            record = self.graph.latest_run()
        if record is None:
            return None
        self.run_id = record.run_id
        self._step = record.step
        stored_metadata = dict(record.metadata)
        stored_evaluation_cost = stored_metadata.pop(
            _EVALUATION_COST_METADATA_KEY,
            0.0,
        )
        if isinstance(stored_evaluation_cost, bool):
            raise GraphError("stored evaluation cost is invalid")
        try:
            evaluation_cost = float(stored_evaluation_cost)
        except (TypeError, ValueError) as exc:
            raise GraphError("stored evaluation cost is invalid") from exc
        if not math.isfinite(evaluation_cost) or evaluation_cost < 0:
            raise GraphError("stored evaluation cost is invalid")
        self._evaluation_cost = evaluation_cost
        for key in ("best_state_id", "best_score", "stop_reason"):
            stored_metadata.pop(key, None)
        stored_metadata.update(self.metadata)
        self.metadata = stored_metadata
        return record

    def _check_problem_fingerprint(self, record: RunRecord | None) -> None:
        if record is None:
            return
        stored = record.config.get("problem_fingerprint")
        if stored is not None and stored != self._problem_fingerprint:
            raise GraphError(
                "graph was created for a different problem configuration; "
                "use a separate database"
            )

    def _ensure_initial_state(self) -> None:
        state = self.problem.initial_state
        validate = getattr(self.problem, "validate_state", None)
        if validate is not None:
            validate(state)
        state_id = self.problem.state_key(state)

        if len(self.graph) > 0 and not self.graph.has_state(state_id):
            raise GraphError(
                "graph does not contain this problem's initial state; "
                "use a separate database for each search problem"
            )

        metadata: dict[str, Any] = {"origin": "initial"}
        score = self._score(state)
        node = self.graph.add_state(
            state_id,
            state,
            metadata=metadata,
            created_step=0,
        )
        self._consider(node.state_id, score)

    def _record_decisions(
        self,
        decisions: list[Decision[Action]],
    ) -> list[tuple[Proposal[Action], str]]:
        policy_name = f"{type(self.policy).__module__}:{type(self.policy).__qualname__}"
        recorded: list[tuple[Proposal[Action], str]] = []
        created_step = self._step + 1
        decision_offset, sequence_index = self.graph.provenance_counts(
            self.run_id,
            created_step,
        )
        for local_index, decision in enumerate(decisions):
            decision_index = decision_offset + local_index
            decision_metadata = dict(decision.metadata)
            if decision.continue_on_empty:
                decision_metadata["_yggdrisil_continue_on_empty"] = True
            payload = {
                "run_id": self.run_id,
                "created_step": created_step,
                "decision_index": decision_index,
                "policy": policy_name,
                "role": decision.role,
                "model": decision.model,
                "selected_state_ids": decision.selected_state_ids,
                "input_context": decision.input_context,
                "tool_calls": decision.tool_calls,
                "output": decision.output,
                "metadata": decision_metadata,
                "continue_on_empty": decision.continue_on_empty,
                "proposals": [
                    {
                        "parent_id": proposal.parent_id,
                        "action": proposal.action,
                        "metadata": proposal.metadata,
                    }
                    for proposal in decision.proposals
                ],
            }
            if decision.memory_updates:
                payload["memory_updates"] = decision.memory_updates
                payload["memory_namespace"] = decision.memory_namespace
            decision_id = stable_hash(payload)
            self.graph.add_decision(
                decision_id,
                run_id=self.run_id,
                policy=policy_name,
                role=decision.role,
                model=decision.model,
                selected_state_ids=list(decision.selected_state_ids),
                input_context=decision.input_context,
                tool_calls=list(decision.tool_calls),
                output=decision.output,
                metadata=decision_metadata,
                created_step=created_step,
                memory_updates=decision.memory_updates,
                memory_namespace=decision.memory_namespace,
            )
            for proposal_index, proposal in enumerate(decision.proposals):
                event_id = stable_hash(
                    {
                        "decision_id": decision_id,
                        "proposal_index": proposal_index,
                    }
                )
                event = self.graph.add_proposal_event(
                    event_id,
                    decision_id=decision_id,
                    run_id=self.run_id,
                    parent_id=proposal.parent_id,
                    action=proposal.action,
                    metadata=dict(proposal.metadata),
                    created_step=created_step,
                    proposal_index=proposal_index,
                    sequence_index=sequence_index,
                )
                sequence_index += 1
                if event.outcome == "pending":
                    recorded.append((proposal, event_id))
        return recorded

    async def _recover_uncheckpointed_step(
        self,
        *,
        goal_found: bool,
        started: float,
    ) -> str | None:
        events = [
            event
            for event in self.graph.proposal_events(run_id=self.run_id)
            if event.created_step > self._step
        ]
        if not events:
            return None

        created_step = min(event.created_step for event in events)
        events = [event for event in events if event.created_step == created_step]
        last_failure = max(
            (
                event.sequence_index
                for event in events
                if event.outcome in {"failed", "skipped_failure"}
            ),
            default=-1,
        )
        if last_failure >= 0:
            events = [event for event in events if event.sequence_index > last_failure]
        if not events:
            return None

        skip_outcome: str | None = None
        if goal_found:
            skip_outcome = "skipped_objective"
        else:
            skip_outcome = next(
                (
                    event.outcome
                    for event in events
                    if event.outcome.startswith("skipped_")
                ),
                None,
            )
        if skip_outcome is not None:
            for event in events:
                if event.outcome == "pending":
                    self.graph.finish_proposal_event(
                        event.event_id,
                        outcome=skip_outcome,
                    )
            self._step = created_step
            self._persist("running")
            return "objective" if skip_outcome == "skipped_objective" else None

        stop_reason: str | None = None
        materialized = False
        for index, event in enumerate(events):
            if event.outcome != "pending":
                if event.outcome in {"created", "reused"}:
                    materialized = True
                continue
            if materialized:
                hit = self.limits.reached(
                    unique_states=len(self.graph),
                    step=self._step,
                    elapsed_s=0.0,
                    evaluation_cost=None,
                )
                if hit is not None:
                    for remaining in events[index:]:
                        if remaining.outcome == "pending":
                            self.graph.finish_proposal_event(
                                remaining.event_id,
                                outcome=f"skipped_{hit}",
                            )
                    break
            proposal = Proposal(
                parent_id=event.parent_id,
                action=event.action,
                metadata=dict(event.metadata),
            )
            try:
                goal, child_id, edge_id, edge_created = self._apply(
                    proposal,
                    created_step=created_step,
                )
            except Exception as exc:
                self.graph.finish_proposal_event(
                    event.event_id,
                    outcome="failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
                for remaining in events[index + 1 :]:
                    if remaining.outcome == "pending":
                        self.graph.finish_proposal_event(
                            remaining.event_id,
                            outcome="skipped_failure",
                        )
                raise
            self.graph.finish_proposal_event(
                event.event_id,
                outcome="created" if edge_created else "reused",
                child_id=child_id,
                edge_id=edge_id,
            )
            materialized = True
            try:
                evaluation_stop = await self._evaluate_state(
                    child_id,
                    started,
                )
            except Exception:
                for remaining in events[index + 1 :]:
                    if remaining.outcome == "pending":
                        self.graph.finish_proposal_event(
                            remaining.event_id,
                            outcome="skipped_failure",
                        )
                raise
            if evaluation_stop is not None:
                stop_reason = evaluation_stop
                for remaining in events[index + 1 :]:
                    if remaining.outcome == "pending":
                        self.graph.finish_proposal_event(
                            remaining.event_id,
                            outcome=f"skipped_{evaluation_stop}",
                        )
                break
            if goal:
                stop_reason = "objective"
                for remaining in events[index + 1 :]:
                    if remaining.outcome == "pending":
                        self.graph.finish_proposal_event(
                            remaining.event_id,
                            outcome="skipped_objective",
                        )
                break

        self._step = created_step
        self._persist("running")
        return stop_reason

    async def _evaluate_existing_states(self, started: float) -> str | None:
        if self.evaluators is None:
            return None
        for node in self.graph.states():
            stop_reason = await self._evaluate_state(node.state_id, started)
            if stop_reason is not None:
                return stop_reason
        if self._evaluation_budget_exhausted():
            return "max_evaluation_cost"
        return None

    async def _evaluate_state(self, state_id: str, started: float) -> str | None:
        if self.evaluators is None:
            return None
        uncached_cost = self.evaluators.uncached_cost(self.graph, state_id)
        cost_limit = self.limits.max_evaluation_cost
        projected_cost = self._evaluation_cost + uncached_cost
        if (
            cost_limit is not None
            and projected_cost > cost_limit
            and not math.isclose(
                projected_cost,
                cost_limit,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        ):
            return "max_evaluation_cost"
        remaining = self._remaining_wall_time(time.monotonic() - started)
        if remaining is not None and remaining <= 0:
            return "max_wall_time_s"
        try:
            if remaining is None:
                await self.evaluators.evaluate_cached(self.graph, state_id)
            else:
                timeout = asyncio.timeout(remaining)
                try:
                    async with timeout:
                        await self.evaluators.evaluate_cached(self.graph, state_id)
                except TimeoutError:
                    if timeout.expired():
                        return "max_wall_time_s"
                    raise
        finally:
            remaining_cost = self.evaluators.uncached_cost(self.graph, state_id)
            consumed_cost = uncached_cost - remaining_cost
            if consumed_cost > 0:
                self._evaluation_cost += consumed_cost
                self._persist("running")
        if consumed_cost > 0 and self._evaluation_budget_exhausted():
            return "max_evaluation_cost"
        return None

    def _evaluation_budget_exhausted(self) -> bool:
        cost_limit = self.limits.max_evaluation_cost
        if cost_limit is None or self.evaluators is None:
            return False
        if not any(
            evaluator_cost(evaluator) > 0 for evaluator in self.evaluators.evaluators
        ):
            return False
        return self._evaluation_cost >= cost_limit or math.isclose(
            self._evaluation_cost,
            cost_limit,
            rel_tol=0.0,
            abs_tol=1e-12,
        )

    def _skip_events(
        self,
        proposal_events: list[tuple[Proposal[Action], str]],
        outcome: str,
    ) -> None:
        for _, event_id in proposal_events:
            self.graph.finish_proposal_event(event_id, outcome=outcome)

    def _fail_pending_events(self, exc: Exception) -> None:
        error = f"batch aborted: {type(exc).__name__}: {exc}"
        for event in self.graph.proposal_events(run_id=self.run_id):
            if event.created_step > self._step and event.outcome == "pending":
                self.graph.finish_proposal_event(
                    event.event_id,
                    outcome="skipped_failure",
                    error=error,
                )

    def _apply(
        self,
        proposal: Proposal[Action],
        *,
        created_step: int | None = None,
    ) -> tuple[bool, str, str, bool]:
        parent = self.graph.get_state(proposal.parent_id)
        validate_action = getattr(self.problem, "validate_action", None)
        if validate_action is not None:
            validate_action(parent.state, proposal.action)
        child = self.problem.apply(parent.state, proposal.action)
        validate_state = getattr(self.problem, "validate_state", None)
        if validate_state is not None:
            validate_state(child)
        child_id = self.problem.state_key(child)
        score = self._score(child)
        transition_step = self._step + 1 if created_step is None else created_step
        node, edge, _, edge_created = self.graph.add_transition(
            parent_id=proposal.parent_id,
            child_id=child_id,
            child=child,
            action=proposal.action,
            edge_metadata=dict(proposal.metadata),
            created_step=transition_step,
        )
        self._consider(node.state_id, score)
        goal = bool(self.objective is not None and self.objective.goal_reached(child))
        return goal, node.state_id, edge.edge_id, edge_created

    def _prepare_objective_scores(self) -> bool:
        if self.objective is None:
            return False
        self._best_state_id = None
        self._best_score = None
        goal_found = False
        for node in self.graph.states():
            score = self._score(node.state)
            self._consider(node.state_id, score)
            if self.objective.goal_reached(node.state):
                goal_found = True
        return goal_found

    def _score(self, state: State) -> float | None:
        if self.objective is None:
            return None
        return float(self.objective.score(state))

    def _consider(self, state_id: str, score: float | None) -> None:
        if score is None or self.objective is None:
            return
        if self.objective.better(score, self._best_score):
            self._best_state_id = state_id
            self._best_score = score

    def _status(self, elapsed_s: float) -> RunStatus:
        return RunStatus(
            step=self._step,
            unique_states=len(self.graph),
            edges=self.graph.edge_count(),
            elapsed_s=elapsed_s,
            limits=self.limits,
            run_id=self.run_id,
            evaluation_cost=self._evaluation_cost,
        )

    def _persist(self, status: str, extra: dict[str, Any] | None = None) -> None:
        metadata = dict(self.metadata)
        if self._best_state_id is not None:
            metadata["best_state_id"] = self._best_state_id
            metadata["best_score"] = self._best_score
        if extra:
            metadata.update(extra)
        metadata[_EVALUATION_COST_METADATA_KEY] = self._evaluation_cost
        self.graph.save_run(
            self.run_id,
            step=self._step,
            status=status,
            config={
                "max_states": self.limits.max_states,
                "max_steps": self.limits.max_steps,
                "max_wall_time_s": self.limits.max_wall_time_s,
                "max_evaluation_cost": self.limits.max_evaluation_cost,
                "evaluators": self._evaluator_configs(),
                "problem_fingerprint": self._problem_fingerprint,
            },
            metadata=metadata,
        )

    def _write_manifest(self, result: RunResult) -> None:
        if str(self.graph.path) == ":memory:":
            return
        payload = {
            "run_id": result.run_id,
            "status": result.status,
            "stop_reason": result.stop_reason,
            "step": result.step,
            "unique_states": result.unique_states,
            "edges": result.edges,
            "elapsed_s": result.elapsed_s,
            "best_state_id": result.best_state_id,
            "best_score": result.best_score,
            "limits": {
                "max_states": self.limits.max_states,
                "max_steps": self.limits.max_steps,
                "max_wall_time_s": self.limits.max_wall_time_s,
                "max_evaluation_cost": self.limits.max_evaluation_cost,
            },
            "evaluation_cost": result.evaluation_cost,
            "evaluators": self._evaluator_configs(),
        }
        path = Path(self.graph.path).with_suffix(".run.json")
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    def _evaluator_configs(self) -> list[dict[str, Any]]:
        if self.evaluators is None:
            return []
        return [
            {
                "name": evaluator.name,
                "version": evaluator.version,
                "evaluator_id": evaluator_identity(evaluator)[0],
                "cost": evaluator_cost(evaluator),
            }
            for evaluator in self.evaluators.evaluators
        ]


def _fingerprint_problem(problem: Problem[Any, Any]) -> str:
    custom = getattr(problem, "problem_fingerprint", None)
    if callable(custom):
        config = custom()
    elif custom is not None:
        config = custom
    else:
        instance_vars = getattr(problem, "__dict__", {})
        config = {
            name: value
            for name, value in instance_vars.items()
            if not name.startswith("_")
            and name not in {"initial_state", "problem_fingerprint"}
        }
    payload = {
        "problem_type": f"{type(problem).__module__}:{type(problem).__qualname__}",
        "initial_state_id": problem.state_key(problem.initial_state),
        "config": config,
    }
    try:
        return stable_hash(payload)
    except SerializationError as exc:
        raise SerializationError(
            "problem configuration cannot be fingerprinted; define a "
            "problem_fingerprint attribute or method with serializable data"
        ) from exc
