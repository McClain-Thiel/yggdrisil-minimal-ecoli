"""ADK execution and persistence behind the navigator/explorer protocols."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from contextlib import aclosing
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal
from functools import wraps
from inspect import iscoroutinefunction
from pathlib import Path
from typing import Any

from google.adk import Agent, Runner
from google.adk.agents.run_config import RunConfig
from google.adk.apps.app import App, ResumabilityConfig
from google.adk.models.base_llm import BaseLlm
from google.adk.models.lite_llm import LiteLlm, LiteLLMClient
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.adk.sessions.sqlite_session_service import SqliteSessionService
from google.genai import types
from pydantic import BaseModel

from yggdrisil.agents.navigator_explorer import (
    ExplorationRequest,
    ExplorerContext,
    ExplorerResult,
    NavigationPlan,
    NavigatorContext,
    format_explorer_prompt,
    format_navigator_prompt,
)
from yggdrisil.agents.tools import ToolInputError
from yggdrisil.serialize import stable_hash


@dataclass(frozen=True)
class UsageLimits:
    """Per-invocation limits; result submission is not a task tool call."""

    request_limit: int = 6
    tool_calls_limit: int = 16
    cost_limit: Decimal | None = None

    def __post_init__(self) -> None:
        if self.request_limit < 1 or self.tool_calls_limit < 1:
            raise ValueError("Agent request and tool limits must be positive")
        if self.cost_limit is not None and self.cost_limit <= 0:
            raise ValueError("Agent cost limit must be positive")


def retryable_tool(tool: Callable[..., Any]) -> Callable[..., Any]:
    """Only invalid model input becomes feedback; infrastructure errors propagate."""

    @wraps(tool)
    async def call(*args: Any, **kwargs: Any) -> Any:
        try:
            if iscoroutinefunction(tool):
                return await tool(*args, **kwargs)
            return tool(*args, **kwargs)
        except ToolInputError as exc:
            return {"error": str(exc)}

    return call


_billed_cost: ContextVar[Decimal | None] = ContextVar(
    "yggdrisil_billed_cost", default=None
)


class _CostClient(LiteLLMClient):
    async def acompletion(
        self, model: Any, messages: Any, tools: Any = None, **kwargs: Any
    ) -> Any:
        response = await super().acompletion(
            model=model, messages=messages, tools=tools, **kwargs
        )
        # OpenRouter returns provider-billed cost; do not substitute a price estimate.
        usage = response.get("usage") or {}
        cost = usage.get("cost")
        _billed_cost.set(Decimal(str(cost)) if cost is not None else None)
        return response


class _BilledModel(LiteLlm):
    def __init__(self, model: str, **settings: Any) -> None:
        client = _CostClient()
        super().__init__(model=model, llm_client=client, **settings)

    @property
    def capabilities(self) -> Any:
        # ADK's result tool gives domain-validation feedback before ending a turn.
        return super().capabilities.model_copy(
            update={"output_schema_and_tools": False}
        )

    async def generate_content_async(
        self, llm_request: Any, stream: bool = False
    ) -> Any:
        _billed_cost.set(None)
        async for response in super().generate_content_async(llm_request, stream):
            response.custom_metadata = {
                **(response.custom_metadata or {}),
                "cost_usd": str(_billed_cost.get())
                if _billed_cost.get() is not None
                else None,
            }
            yield response


def make_model(model: str | BaseLlm, settings: dict[str, Any] | None = None) -> BaseLlm:
    if isinstance(model, BaseLlm):
        return model
    return _BilledModel(
        model=model.replace("openrouter:", "openrouter/"), **(settings or {})
    )


def trace_events(events: Sequence[Any]) -> list[dict[str, Any]]:
    trace = []
    for event in events:
        if event.error_code:
            trace.append(
                {
                    "role": "error",
                    "agent": event.author,
                    "error_code": event.error_code,
                    "error_message": event.error_message,
                }
            )
        for call in event.get_function_calls():
            trace.append(
                {
                    "role": "tool_call",
                    "agent": event.author,
                    "tool": call.name,
                    "args": call.args,
                }
            )
        for result in event.get_function_responses():
            trace.append(
                {
                    "role": "tool_return",
                    "agent": event.author,
                    "tool": result.name,
                    "content": result.response,
                }
            )
        if event.actions.state_delta:
            trace.append(
                {
                    "role": "state_update",
                    "agent": event.author,
                    "state": event.actions.state_delta,
                }
            )
        usage = event.usage_metadata
        if usage is not None:
            trace.append(
                {
                    "role": "usage",
                    "agent": event.author,
                    "requests": 1,
                    "tool_calls": len(event.get_function_calls()),
                    "input_tokens": usage.prompt_token_count or 0,
                    "output_tokens": (usage.candidates_token_count or 0)
                    + (usage.thoughts_token_count or 0),
                    "cache_read_tokens": usage.cached_content_token_count or 0,
                    "cost_usd": (event.custom_metadata or {}).get("cost_usd"),
                }
            )
    return trace


class AdkInvocation:
    """One native ADK team, with optional SQLite sessions and bounded invocations."""

    def __init__(
        self,
        agent: Agent,
        *,
        session_path: Path | None = None,
        user_id: str = "search",
        session_id: str | None = None,
        usage_limits: UsageLimits | None = None,
    ) -> None:
        if not agent.output_key:
            raise ValueError("ADK bridge requires an output_key")
        self.output_key = agent.output_key
        self.agent = agent
        self.session_path = session_path
        self.user_id = user_id
        self.session_id = session_id or agent.name
        self.usage_limits = usage_limits or UsageLimits()
        self.last_trace: list[dict[str, Any]] = []

    async def run(self, prompt: str, *, request_id: str | None = None) -> Any:
        self.last_trace = []
        request_id = request_id or stable_hash(prompt)
        message = f"REQUEST_ID: {request_id}\n{prompt}"
        if self.session_path:
            self.session_path.parent.mkdir(parents=True, exist_ok=True)
        sessions = (
            SqliteSessionService(str(self.session_path))
            if self.session_path
            else InMemorySessionService()
        )
        agent = self.agent.clone()
        runner = Runner(
            app=App(
                name="yggdrisil",
                root_agent=agent,
                resumability_config=ResumabilityConfig(is_resumable=True),
            ),
            session_service=sessions,
        )
        events = []
        try:
            session = await sessions.get_session(
                app_name="yggdrisil", user_id=self.user_id, session_id=self.session_id
            )
            if session is None:
                session = await sessions.create_session(
                    app_name="yggdrisil",
                    user_id=self.user_id,
                    session_id=self.session_id,
                )
            users = [event for event in session.events if event.author == "user"]
            same_request = bool(
                users
                and users[-1].content
                and users[-1].content.parts
                and users[-1].content.parts[0].text
                and users[-1]
                .content.parts[0]
                .text.startswith(f"REQUEST_ID: {request_id}\n")
            )
            previous = (
                [
                    e
                    for e in session.events
                    if users and e.invocation_id == users[-1].invocation_id
                ]
                if same_request
                else []
            )
            # Replay completed output if ADK committed it before the graph runner did.
            if previous and any(
                self.output_key in e.actions.state_delta and not e.error_code
                for e in previous
            ):
                self.last_trace = trace_events(previous)
                return session.state[self.output_key]
            events.extend(previous)
            for event in previous:
                if event.error_code:
                    logging.getLogger(__name__).warning(
                        "Resuming ADK invocation %s after recorded error %s",
                        event.invocation_id,
                        event.error_code,
                    )
            calls = sum(e.usage_metadata is not None for e in previous)
            tools = sum(
                call.name != "set_model_response"
                for e in previous
                for call in e.get_function_calls()
            )
            if self.usage_limits.cost_limit is not None and any(
                e.usage_metadata is not None
                and (e.custom_metadata or {}).get("cost_usd") is None
                for e in previous
            ):
                raise RuntimeError(
                    "Stored provider cost is missing; cannot resume a bounded invocation"
                )
            cost = sum(
                (
                    Decimal((e.custom_metadata or {}).get("cost_usd") or "0")
                    for e in previous
                ),
                Decimal(0),
            )

            def before_model(callback_context: Any, llm_request: Any) -> None:
                nonlocal calls
                if calls >= self.usage_limits.request_limit:
                    raise RuntimeError("ADK request_limit exhausted")
                if (
                    self.usage_limits.cost_limit is not None
                    and cost >= self.usage_limits.cost_limit
                ):
                    raise RuntimeError("ADK cost_limit exhausted")
                calls += 1
                depth = 0
                current = callback_context.get_invocation_context().agent
                while current.parent_agent is not None:
                    depth += 1
                    current = current.parent_agent
                remaining_tools = max(0, self.usage_limits.tool_calls_limit - tools)
                llm_request.append_instructions(
                    [
                        f"Remaining task tool calls across this team: {remaining_tools}. "
                        "Batch related lookups and leave time to return your result."
                    ]
                )
                if remaining_tools == 0 or (
                    self.usage_limits.request_limit > 2
                    and calls >= max(1, self.usage_limits.request_limit - 1 - 2 * depth)
                ):
                    # Each waiting parent also needs room to finish its result.
                    llm_request.tools_dict = {
                        name: tool
                        for name, tool in llm_request.tools_dict.items()
                        if name == "set_model_response"
                    }
                    llm_request.config.tools = (
                        [
                            types.Tool(
                                function_declarations=[
                                    declaration
                                    for tool in llm_request.config.tools or []
                                    for declaration in tool.function_declarations or []
                                    if declaration.name == "set_model_response"
                                ]
                            )
                        ]
                        if "set_model_response" in llm_request.tools_dict
                        else []
                    )
                    llm_request.append_instructions(
                        [
                            "The tool budget is ending. Return your complete structured result now."
                        ]
                    )

            def after_model(callback_context: Any, llm_response: Any) -> None:
                nonlocal cost
                amount = (llm_response.custom_metadata or {}).get("cost_usd")
                if self.usage_limits.cost_limit is not None and amount is None:
                    raise RuntimeError(
                        "Provider omitted billed cost; cannot enforce cost_limit"
                    )
                cost += Decimal(amount or "0")

            def before_tool(tool: Any) -> dict[str, str] | None:
                nonlocal tools
                # ADK validates structured output through this tool. Allow it to
                # finish even after a parallel batch uses the task-tool budget.
                if tool.name == "set_model_response":
                    return None
                if tools >= self.usage_limits.tool_calls_limit:
                    return {
                        "error": "ADK tool_calls_limit exhausted. This tool was not executed. "
                        "Return your complete structured result using the evidence already collected."
                    }
                tools += 1
                return None

            class InvocationBudget(BasePlugin):
                async def before_model_callback(
                    self, *, callback_context: Any, llm_request: Any
                ) -> None:
                    before_model(callback_context, llm_request)

                async def after_model_callback(
                    self, *, callback_context: Any, llm_response: Any
                ) -> None:
                    after_model(callback_context, llm_response)

                async def before_tool_callback(
                    self, *, tool: Any, tool_args: dict[str, Any], tool_context: Any
                ) -> dict[str, str] | None:
                    return before_tool(tool)

            runner.plugin_manager.register_plugin(InvocationBudget(name="usage_limits"))
            async with aclosing(
                runner.run_async(
                    user_id=self.user_id,
                    session_id=self.session_id,
                    invocation_id=previous[-1].invocation_id if previous else None,
                    new_message=None
                    if previous
                    else types.Content(role="user", parts=[types.Part(text=message)]),
                    run_config=RunConfig(max_llm_calls=self.usage_limits.request_limit),
                )
            ) as stream:
                async for event in stream:
                    events.append(event)
            # Historical failures remain in the trace; only this attempt can fail again.
            errors = [e for e in events[len(previous) :] if e.error_code]
            if errors:
                raise RuntimeError(
                    f"ADK {errors[-1].error_code}: {errors[-1].error_message}"
                )
            session = await sessions.get_session(
                app_name="yggdrisil", user_id=self.user_id, session_id=self.session_id
            )
            if not any(
                e.author == self.agent.name and e.is_final_response() for e in events
            ):
                raise RuntimeError("ADK invocation ended without a final response")
            if session is None:
                raise RuntimeError("ADK session disappeared during invocation")
            return session.state[self.output_key]
        finally:
            if events:
                self.last_trace = trace_events(events)
            await runner.close()
            if isinstance(sessions, SqliteSessionService):
                await sessions.close()


class AdkNavigator:
    def __init__(
        self,
        invocation: AdkInvocation,
        *,
        prompt: Callable[[NavigatorContext], str] | None = None,
    ) -> None:
        self.invocation = invocation
        self.agent = invocation.agent
        self.model = self.agent.canonical_model.model
        self.prompt = prompt or format_navigator_prompt

    @property
    def last_trace(self) -> list[dict[str, Any]]:
        return self.invocation.last_trace

    def format_prompt(self, context: NavigatorContext) -> str:
        return self.prompt(context)

    async def plan(self, context: NavigatorContext) -> NavigationPlan:
        output = await self.invocation.run(
            self.format_prompt(context), request_id=f"navigator-{context.status.step}"
        )
        return NavigationPlan(
            requests=[ExplorationRequest(**item) for item in output["requests"]],
            continue_on_empty=output.get("continue_on_empty", False),
        )


class AdkExplorer:
    def __init__(
        self,
        invocation: AdkInvocation,
        action_type: type[Any],
        *,
        prompt: Callable[[ExplorerContext[Any]], str] | None = None,
    ) -> None:
        self.invocation, self.action_type = invocation, action_type
        self.agent = invocation.agent
        self.model = self.agent.canonical_model.model
        self.prompt = prompt or format_explorer_prompt

    @property
    def last_trace(self) -> list[dict[str, Any]]:
        return self.invocation.last_trace

    def format_prompt(self, context: ExplorerContext[Any]) -> str:
        return self.prompt(context)

    async def explore(self, context: ExplorerContext[Any]) -> ExplorerResult[Any]:
        output = await self.invocation.run(self.format_prompt(context))
        from pydantic import TypeAdapter

        return ExplorerResult(
            actions=TypeAdapter(list[self.action_type]).validate_python(  # type: ignore[name-defined]
                output["actions"]
            ),
            note=output.get("note"),
            trace=self.invocation.last_trace,
        )


def make_navigator(
    model: str | BaseLlm,
    *,
    instructions: str = "Choose existing states for direct exploration.",
    tools: Sequence[Callable[..., Any]] = (),
    prompt: Callable[[NavigatorContext], str] | None = None,
    model_settings: dict[str, Any] | None = None,
    **runtime: Any,
) -> AdkNavigator:
    class Plan(BaseModel):
        requests: list[ExplorationRequest]
        continue_on_empty: bool = False

    agent = Agent(
        name="navigator",
        model=make_model(model, model_settings),
        instruction=instructions,
        tools=[retryable_tool(t) for t in tools],
        output_schema=Plan,
        output_key="plan",
        include_contents="none",
    )
    return AdkNavigator(AdkInvocation(agent, **runtime), prompt=prompt)


def make_explorer(
    model: str | BaseLlm,
    action_type: type[Any],
    *,
    instructions: str = "Propose only direct child actions.",
    tools: Sequence[Callable[..., Any]] = (),
    prompt: Callable[[ExplorerContext[Any]], str] | None = None,
    model_settings: dict[str, Any] | None = None,
    **runtime: Any,
) -> AdkExplorer:
    from pydantic import create_model

    output = create_model(
        "ExplorerOutput",
        actions=(list[action_type], ...),  # type: ignore[valid-type]
        note=(str | None, None),
    )
    agent = Agent(
        name="explorer",
        model=make_model(model, model_settings),
        instruction=instructions,
        tools=[retryable_tool(t) for t in tools],
        output_schema=output,
        output_key="proposal",
        include_contents="none",
    )
    return AdkExplorer(AdkInvocation(agent, **runtime), action_type, prompt=prompt)
