from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypedDict

from fastapi import HTTPException
from langgraph.graph import END, START, StateGraph

from ..runtime import llm_gateway
from ..schemas import (
    AgentDefinition,
    RunArtifacts,
    TraceEvent,
    WorkflowDefinition,
    WorkflowGraph,
    WorkflowRunResponse,
)
from ..store import InMemoryPlaygroundStore
from .langgraph_adapter import workflow_graph_from_compiled


ROUTER_NODE = "router"
FINALIZER_NODE = "finalize"


class RouterState(TypedDict, total=False):
    user_input: str
    selected_agent_id: str
    selected_agent_name: str
    route_reason: str
    specialist_answer: str
    assistant_message: str


def event(
    event_type: str,
    title: str,
    detail: str,
    **payload: object,
) -> TraceEvent:
    return TraceEvent(type=event_type, title=title, detail=detail, payload=payload)


def _compile_router_app(
    workflow: WorkflowDefinition,
    agents: list[AgentDefinition],
    router_node: Callable[[RouterState], RouterState],
    make_specialist_node: Callable[[AgentDefinition], Callable[[RouterState], RouterState]],
    route_next: Callable[[RouterState], str],
    finalizer_node: Callable[[RouterState], RouterState] | None = None,
):
    builder = StateGraph(RouterState)
    builder.add_node(ROUTER_NODE, router_node, metadata={"kind": "logic", "label": "Router"})
    for agent in agents:
        builder.add_node(
            agent.id,
            make_specialist_node(agent),
            metadata={"kind": "agent", "label": agent.name},
        )
    if workflow.finalizer_enabled and finalizer_node is not None:
        builder.add_node(FINALIZER_NODE, finalizer_node, metadata={"kind": "final", "label": "Finalizer"})

    builder.add_edge(START, ROUTER_NODE)
    builder.add_conditional_edges(
        ROUTER_NODE,
        route_next,
        {agent.id: agent.id for agent in agents},
    )

    for agent in agents:
        if workflow.finalizer_enabled and finalizer_node is not None:
            builder.add_edge(agent.id, FINALIZER_NODE)
        else:
            builder.add_edge(agent.id, END)

    if workflow.finalizer_enabled and finalizer_node is not None:
        builder.add_edge(FINALIZER_NODE, END)

    return builder.compile()


def build_router_graph(
    workflow: WorkflowDefinition,
    agents: list[AgentDefinition],
) -> WorkflowGraph:
    if not agents:
        raise HTTPException(status_code=400, detail="router_specialists requires at least 1 agent.")

    default_agent = agents[0]

    def noop_router(_: RouterState) -> RouterState:
        return {
            "selected_agent_id": default_agent.id,
            "selected_agent_name": default_agent.name,
            "route_reason": "graph_preview",
        }

    def make_noop_specialist(_: AgentDefinition):
        def specialist(_: RouterState) -> RouterState:
            return {}

        return specialist

    def route_next(state: RouterState) -> str:
        selected = str(state.get("selected_agent_id", ""))
        return selected if selected else default_agent.id

    def noop_finalizer(_: RouterState) -> RouterState:
        return {}

    app = _compile_router_app(
        workflow,
        agents,
        router_node=noop_router,
        make_specialist_node=make_noop_specialist,
        route_next=route_next,
        finalizer_node=noop_finalizer if workflow.finalizer_enabled else None,
    )
    return workflow_graph_from_compiled(app)


def run_router_specialists(
    store: InMemoryPlaygroundStore,
    workflow: WorkflowDefinition,
    user_input: str,
    history: list[dict[str, str]] | None = None,
    on_event: Callable[[TraceEvent], None] | None = None,
) -> WorkflowRunResponse:
    agents: list[AgentDefinition] = []
    for agent_id in workflow.specialist_agent_ids:
        agent = store.get_agent(agent_id)
        if agent is not None:
            agents.append(agent)

    if not agents:
        raise HTTPException(status_code=400, detail="This workflow has no valid specialist agents.")

    agent_by_id = {agent.id: agent for agent in agents}

    def push(trace: list[TraceEvent], item: TraceEvent) -> None:
        trace.append(item)
        if on_event is not None:
            on_event(item)

    trace: list[TraceEvent] = []

    def make_tool_trace_hook(agent: AgentDefinition):
        def on_tool_trace(meta: dict[str, Any]) -> None:
            stage = str(meta.get("stage") or "")
            tool_name = str(meta.get("tool_name") or "tool")

            if stage == "tool_candidates":
                available = int(meta.get("available_count") or 0)
                selected = int(meta.get("selected_count") or 0)
                push(
                    trace,
                    event(
                        "state_updated",
                        "Tool Matching",
                        f"Matched {selected}/{available} tool(s) for this request.",
                        node_id=agent.id,
                        agent_id=agent.id,
                        matching=meta.get("matching", []),
                    ),
                )
                return

            if stage == "tool_started":
                push(
                    trace,
                    event(
                        "state_updated",
                        "Tool Started",
                        f"{agent.name} is running {tool_name}.",
                        node_id=agent.id,
                        agent_id=agent.id,
                        tool_name=tool_name,
                        tool_call_id=meta.get("tool_call_id"),
                        input_keys=meta.get("input_keys", []),
                        skill_id=meta.get("skill_id"),
                        skill_name=meta.get("skill_name"),
                    ),
                )
                return

            if stage == "tool_retry":
                attempt = int(meta.get("attempt") or 1)
                max_attempts = int(meta.get("max_attempts") or attempt)
                reason = str(meta.get("reason") or "Transient failure, retrying.")
                push(
                    trace,
                    event(
                        "state_updated",
                        "Tool Retry",
                        f"{tool_name} attempt {attempt}/{max_attempts} failed: {reason[:120]}",
                        node_id=agent.id,
                        agent_id=agent.id,
                        tool_name=tool_name,
                        tool_call_id=meta.get("tool_call_id"),
                        attempt=attempt,
                        max_attempts=max_attempts,
                        delay_ms=meta.get("delay_ms"),
                        skill_id=meta.get("skill_id"),
                        skill_name=meta.get("skill_name"),
                    ),
                )
                return

            if stage == "tool_blocked":
                reason = str(meta.get("reason") or "Tool execution blocked.")
                push(
                    trace,
                    event(
                        "state_updated",
                        "Tool Blocked",
                        reason[:220],
                        node_id=agent.id,
                        agent_id=agent.id,
                        tool_name=tool_name,
                        tool_call_id=meta.get("tool_call_id"),
                        skill_id=meta.get("skill_id"),
                        skill_name=meta.get("skill_name"),
                        missing_env_vars=meta.get("missing_env_vars", []),
                        missing_shell_dependencies=meta.get("missing_shell_dependencies", []),
                        missing_launchers=meta.get("missing_launchers", []),
                    ),
                )
                return

            if stage != "tool_finished":
                return

            ok = bool(meta.get("ok"))
            generated_files = meta.get("generated_files")
            files = generated_files if isinstance(generated_files, list) else []
            detail = f"{agent.name} finished {tool_name} ({'success' if ok else 'failed'})."
            attempt_count = int(meta.get("attempt_count") or 1)
            max_attempts = int(meta.get("max_attempts") or 1)
            if max_attempts > 1:
                detail += f" Attempts {attempt_count}/{max_attempts}."
            if ok and files:
                detail += f" Generated {len(files)} file(s)."
            if (not ok) and meta.get("error"):
                detail += f" Error: {str(meta.get('error'))[:140]}"
            push(
                trace,
                event(
                    "state_updated",
                    "Tool Finished" if ok else "Tool Failed",
                    detail,
                    node_id=agent.id,
                    agent_id=agent.id,
                    tool_name=tool_name,
                    tool_call_id=meta.get("tool_call_id"),
                    ok=ok,
                    duration_ms=meta.get("duration_ms"),
                    attempt_count=attempt_count,
                    max_attempts=max_attempts,
                    output_dir=meta.get("output_dir"),
                    generated_files=files,
                ),
            )

            result_preview = str(meta.get("result_preview") or "").strip()
            if result_preview:
                push(
                    trace,
                    event(
                        "message_generated",
                        "Tool Output",
                        f"{tool_name} produced output.",
                        node_id=agent.id,
                        agent_id=agent.id,
                        tool_name=tool_name,
                        preview=result_preview[:180],
                    ),
                )

        return on_tool_trace

    push(
        trace,
        event(
            "run_started",
            "Run Started",
            f"Starting workflow: {workflow.name}",
            workflow_id=workflow.id,
            workflow_type=workflow.type,
        ),
    )

    def router_node(state: RouterState) -> RouterState:
        push(
            trace,
            event(
                "node_entered",
                "Enter Router",
                "Router is selecting the best specialist.",
                node_id=ROUTER_NODE,
            ),
        )
        route_agent_id, route_reason = llm_gateway.route(state["user_input"], agents)
        selected_agent = agent_by_id[route_agent_id]
        push(
            trace,
            event(
                "route_selected",
                "Route Selected",
                f"Router selected {selected_agent.name}",
                node_id=ROUTER_NODE,
                next_node_id=selected_agent.id,
                reason=route_reason,
            ),
        )
        push(
            trace,
            event(
                "node_exited",
                "Exit Router",
                f"Routing finished. Next node: {selected_agent.name}",
                node_id=ROUTER_NODE,
            ),
        )
        return {
            "selected_agent_id": selected_agent.id,
            "selected_agent_name": selected_agent.name,
            "route_reason": route_reason,
        }

    def make_specialist_node(agent: AgentDefinition):
        def specialist_node(state: RouterState) -> RouterState:
            push(
                trace,
                event(
                    "node_entered",
                    "Enter Specialist",
                    f"{agent.name} is generating the response.",
                    node_id=agent.id,
                    agent_id=agent.id,
                ),
            )
            specialist_answer = llm_gateway.run_agent(
                agent,
                state["user_input"],
                history=history,
                trace_hook=make_tool_trace_hook(agent),
            )
            push(
                trace,
                event(
                    "message_generated",
                    "Specialist Output",
                    f"{agent.name} generated an answer.",
                    node_id=agent.id,
                    preview=specialist_answer[:120],
                ),
            )
            push(
                trace,
                event(
                    "node_exited",
                    "Exit Specialist",
                    f"{agent.name} finished processing.",
                    node_id=agent.id,
                ),
            )
            return {"specialist_answer": specialist_answer}

        return specialist_node

    def route_next(state: RouterState) -> str:
        selected = str(state.get("selected_agent_id", ""))
        if selected in agent_by_id:
            return selected
        return agents[0].id

    def finalizer_node(state: RouterState) -> RouterState:
        selected_agent = agent_by_id[state["selected_agent_id"]]
        push(
            trace,
            event(
                "node_entered",
                "Enter Finalizer",
                "Finalizer is composing the final answer.",
                node_id=FINALIZER_NODE,
            ),
        )
        assistant_message = llm_gateway.finalize(
            user_input=state["user_input"],
            agent=selected_agent,
            specialist_answer=state["specialist_answer"],
        )
        push(
            trace,
            event(
                "node_exited",
                "Exit Finalizer",
                "Finalizer finished.",
                node_id=FINALIZER_NODE,
            ),
        )
        return {"assistant_message": assistant_message}

    app = _compile_router_app(
        workflow,
        agents,
        router_node=router_node,
        make_specialist_node=make_specialist_node,
        route_next=route_next,
        finalizer_node=finalizer_node if workflow.finalizer_enabled else None,
    )
    graph = workflow_graph_from_compiled(app)
    final_state = app.invoke({"user_input": user_input})

    specialist_answer = str(final_state.get("specialist_answer", ""))
    if workflow.finalizer_enabled:
        assistant_message = str(final_state.get("assistant_message", specialist_answer))
    else:
        assistant_message = specialist_answer

    push(
        trace,
        event(
            "run_finished",
            "Run Finished",
            "Workflow completed.",
            workflow_id=workflow.id,
        ),
    )

    route_agent_id = str(final_state.get("selected_agent_id", ""))
    route_agent_name = str(final_state.get("selected_agent_name", ""))
    route_reason = str(final_state.get("route_reason", ""))
    artifacts = RunArtifacts(
        route_agent_id=route_agent_id or None,
        route_agent_name=route_agent_name or None,
        route_reason=route_reason or None,
        specialist_answer=specialist_answer or None,
        final_answer=assistant_message,
    )
    return WorkflowRunResponse(
        workflow_id=workflow.id,
        user_input=user_input,
        assistant_message=assistant_message,
        trace=trace,
        graph=graph,
        artifacts=artifacts,
    )
