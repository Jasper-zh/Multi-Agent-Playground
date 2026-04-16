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


INTAKE_NODE = "supervisor_intake"
DELEGATION_NODE = "delegation_policy"
REVIEW_NODE = "supervisor_review"
FINALIZE_NODE = "finalize"


class SupervisorState(TypedDict, total=False):
    user_input: str
    max_cycles: int
    cycle: int
    current_focus_task: str
    current_worker_id: str
    current_worker_name: str
    current_route_reason: str
    reports: list[str]
    combined_report: str
    continue_loop: bool
    assistant_message: str


def event(
    event_type: str,
    title: str,
    detail: str,
    **payload: object,
) -> TraceEvent:
    return TraceEvent(type=event_type, title=title, detail=detail, payload=payload)


def _estimate_max_cycles(user_input: str) -> int:
    text = str(user_input or "").strip()
    if not text:
        return 2

    lowered = text.lower()
    if len(text) >= 180:
        return 5
    if any(token in lowered for token in ("compare", "tradeoff", "vs ", "step by step")):
        return 4
    if any(token in text for token in ("以及", "并且", "同时", "另外", "对比", "区别", "优缺点", "先", "再")):
        return 4
    return 3


def _compile_supervisor_app(
    workflow: WorkflowDefinition,
    workers: list[AgentDefinition],
    intake_node: Callable[[SupervisorState], SupervisorState],
    delegation_node: Callable[[SupervisorState], SupervisorState],
    make_worker_node: Callable[[AgentDefinition], Callable[[SupervisorState], SupervisorState]],
    review_node: Callable[[SupervisorState], SupervisorState],
    delegation_next: Callable[[SupervisorState], str],
    review_next: Callable[[SupervisorState], str],
    finalize_node: Callable[[SupervisorState], SupervisorState] | None = None,
):
    builder = StateGraph(SupervisorState)
    builder.add_node(INTAKE_NODE, intake_node, metadata={"kind": "logic", "label": "Supervisor Intake"})
    builder.add_node(DELEGATION_NODE, delegation_node, metadata={"kind": "logic", "label": "Delegation Policy"})
    for worker in workers:
        builder.add_node(
            worker.id,
            make_worker_node(worker),
            metadata={"kind": "agent", "label": worker.name},
        )
    builder.add_node(REVIEW_NODE, review_node, metadata={"kind": "logic", "label": "Supervisor Review"})
    if workflow.finalizer_enabled and finalize_node is not None:
        builder.add_node(FINALIZE_NODE, finalize_node, metadata={"kind": "final", "label": "Finalizer"})

    builder.add_edge(START, INTAKE_NODE)
    builder.add_edge(INTAKE_NODE, DELEGATION_NODE)
    builder.add_conditional_edges(
        DELEGATION_NODE,
        delegation_next,
        {worker.id: worker.id for worker in workers},
    )
    for worker in workers:
        builder.add_edge(worker.id, REVIEW_NODE)

    if workflow.finalizer_enabled and finalize_node is not None:
        builder.add_conditional_edges(
            REVIEW_NODE,
            review_next,
            {
                DELEGATION_NODE: DELEGATION_NODE,
                FINALIZE_NODE: FINALIZE_NODE,
            },
        )
        builder.add_edge(FINALIZE_NODE, END)
    else:
        builder.add_conditional_edges(
            REVIEW_NODE,
            review_next,
            {
                DELEGATION_NODE: DELEGATION_NODE,
                END: END,
            },
        )

    return builder.compile()


def build_supervisor_graph(
    workflow: WorkflowDefinition,
    agents: list[AgentDefinition],
) -> WorkflowGraph:
    if len(agents) < 2:
        raise HTTPException(status_code=400, detail="supervisor_dynamic requires at least 2 agents.")

    default_worker = agents[0]

    def noop(_: SupervisorState) -> SupervisorState:
        return {}

    def noop_delegation(_: SupervisorState) -> SupervisorState:
        return {"current_worker_id": default_worker.id}

    def make_noop_worker(_: AgentDefinition):
        return noop

    def delegation_next(state: SupervisorState) -> str:
        selected = str(state.get("current_worker_id", ""))
        return selected if selected else default_worker.id

    def review_next(_: SupervisorState) -> str:
        return FINALIZE_NODE if workflow.finalizer_enabled else END

    app = _compile_supervisor_app(
        workflow,
        agents,
        intake_node=noop,
        delegation_node=noop_delegation,
        make_worker_node=make_noop_worker,
        review_node=noop,
        delegation_next=delegation_next,
        review_next=review_next,
        finalize_node=noop if workflow.finalizer_enabled else None,
    )
    return workflow_graph_from_compiled(app)


def run_supervisor_dynamic(
    store: InMemoryPlaygroundStore,
    workflow: WorkflowDefinition,
    user_input: str,
    history: list[dict[str, str]] | None = None,
    on_event: Callable[[TraceEvent], None] | None = None,
) -> WorkflowRunResponse:
    workers: list[AgentDefinition] = []
    for agent_id in workflow.specialist_agent_ids:
        agent = store.get_agent(agent_id)
        if agent is not None:
            workers.append(agent)

    if len(workers) < 2:
        raise HTTPException(status_code=400, detail="supervisor_dynamic requires at least 2 valid agents.")

    worker_by_id = {worker.id: worker for worker in workers}

    def push(trace: list[TraceEvent], item: TraceEvent) -> None:
        trace.append(item)
        if on_event is not None:
            on_event(item)

    trace: list[TraceEvent] = []

    def make_tool_trace_hook(agent: AgentDefinition):
        def on_tool_trace(meta: dict[str, Any]) -> None:
            stage = str(meta.get("stage") or "")
            tool_name = str(meta.get("tool_name") or "tool")

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
                reason = str(meta.get("reason") or "Tool execution failed; continuing without this tool.")
                push(
                    trace,
                    event(
                        "state_updated",
                        "Tool Unavailable",
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

    def intake_node(state: SupervisorState) -> SupervisorState:
        push(
            trace,
            event(
                "node_entered",
                "Enter Supervisor Intake",
                "Supervisor intake is parsing user goals.",
                node_id=INTAKE_NODE,
            ),
        )
        max_cycles = _estimate_max_cycles(state["user_input"])
        push(
            trace,
            event(
                "state_updated",
                "Intake Completed",
                f"Supervisor initialized dynamic loop (max {max_cycles} cycle(s)).",
                node_id=INTAKE_NODE,
                max_cycles=max_cycles,
            ),
        )
        push(
            trace,
            event(
                "node_exited",
                "Exit Supervisor Intake",
                "Delegation can begin.",
                node_id=INTAKE_NODE,
            ),
        )
        return {
            "max_cycles": max_cycles,
            "cycle": 0,
            "current_focus_task": state["user_input"],
            "reports": [],
        }

    def delegation_node(state: SupervisorState) -> SupervisorState:
        cycle = int(state.get("cycle", 0)) + 1
        focus_task = str(state.get("current_focus_task") or state["user_input"]).strip() or state["user_input"]
        push(
            trace,
            event(
                "node_entered",
                "Enter Delegation Policy",
                f"Supervisor is selecting worker for cycle {cycle}.",
                node_id=DELEGATION_NODE,
                cycle=cycle,
            ),
        )
        routed_worker_id, route_reason = llm_gateway.route(focus_task, workers)
        worker = worker_by_id[routed_worker_id]
        push(
            trace,
            event(
                "route_selected",
                "Worker Delegated",
                f"Delegation selected {worker.name}.",
                node_id=DELEGATION_NODE,
                next_node_id=worker.id,
                cycle=cycle,
                reason=route_reason,
                focus_task=focus_task,
            ),
        )
        push(
            trace,
            event(
                "node_exited",
                "Exit Delegation Policy",
                f"Delegation finished for cycle {cycle}.",
                node_id=DELEGATION_NODE,
                cycle=cycle,
            ),
        )
        return {
            "cycle": cycle,
            "current_focus_task": focus_task,
            "current_worker_id": worker.id,
            "current_worker_name": worker.name,
            "current_route_reason": route_reason,
        }

    def make_worker_node(worker: AgentDefinition):
        def worker_node(state: SupervisorState) -> SupervisorState:
            cycle = int(state.get("cycle", 0))
            push(
                trace,
                event(
                    "node_entered",
                    "Enter Worker",
                    f"{worker.name} is handling cycle {cycle}.",
                    node_id=worker.id,
                    cycle=cycle,
                ),
            )
            reports = list(state.get("reports", []))
            completed_summary = "\n".join(reports) if reports else "(none yet)"
            worker_input = (
                f"Original user request:\n{state['user_input']}\n\n"
                f"Current focus task (cycle {cycle}):\n{state['current_focus_task']}\n\n"
                f"Completed reports so far:\n{completed_summary}\n\n"
                "Execute the current focus task directly.\n"
                "If the target path or deliverable is already clear, prefer directly creating or writing it instead of repeatedly listing or searching first.\n"
                "Use inspection first only when the path is ambiguous, existing content must be preserved, or you need facts you do not yet have."
            )
            worker_answer = llm_gateway.run_agent(
                worker,
                worker_input,
                history=history,
                trace_hook=make_tool_trace_hook(worker),
            )
            reports.append(f"Cycle {cycle} by {worker.name}:\n{worker_answer}")
            push(
                trace,
                event(
                    "message_generated",
                    "Worker Output",
                    f"{worker.name} completed cycle {cycle}.",
                    node_id=worker.id,
                    cycle=cycle,
                    preview=worker_answer[:120],
                ),
            )
            push(
                trace,
                event(
                    "node_exited",
                    "Exit Worker",
                    f"{worker.name} returned control to supervisor.",
                    node_id=worker.id,
                    cycle=cycle,
                ),
            )

            return {
                "reports": reports,
                "combined_report": "\n\n".join(reports),
            }

        return worker_node

    def review_node(state: SupervisorState) -> SupervisorState:
        cycle = int(state.get("cycle", 0))
        max_cycles = int(state.get("max_cycles", 2))
        reports = list(state.get("reports", []))
        push(
            trace,
            event(
                "node_entered",
                "Enter Supervisor Review",
                f"Supervisor is reviewing cycle {cycle}.",
                node_id=REVIEW_NODE,
                cycle=cycle,
            ),
        )

        should_continue, next_focus_task, review_reason = llm_gateway.supervisor_review_decision(
            user_input=state["user_input"],
            reports=reports,
            cycle=cycle,
            max_cycles=max_cycles,
        )

        if should_continue:
            push(
                trace,
                event(
                    "state_updated",
                    "Review Decision",
                    "Supervisor decided to continue delegation.",
                    node_id=REVIEW_NODE,
                    cycle=cycle,
                    reason=review_reason,
                    next_focus_task=next_focus_task,
                ),
            )
            push(
                trace,
                event(
                    "route_selected",
                    "Loop Continue",
                    "Supervisor routed back to delegation policy.",
                    node_id=REVIEW_NODE,
                    next_node_id=DELEGATION_NODE,
                    cycle=cycle,
                ),
            )
            push(
                trace,
                event(
                    "node_exited",
                    "Exit Supervisor Review",
                    "Continue to next delegation cycle.",
                    node_id=REVIEW_NODE,
                    cycle=cycle,
                ),
            )
            return {
                "continue_loop": True,
                "current_focus_task": next_focus_task or state["user_input"],
            }

        terminal_node = FINALIZE_NODE if workflow.finalizer_enabled else "end"
        push(
            trace,
            event(
                "state_updated",
                "Review Decision",
                "Supervisor decided current result is sufficient to finish.",
                node_id=REVIEW_NODE,
                cycle=cycle,
                reason=review_reason,
            ),
        )
        push(
            trace,
            event(
                "route_selected",
                "Loop Exit",
                f"Supervisor routed to {terminal_node}.",
                node_id=REVIEW_NODE,
                next_node_id=terminal_node,
                cycle=cycle,
            ),
        )
        push(
            trace,
            event(
                "node_exited",
                "Exit Supervisor Review",
                "Supervisor review finished.",
                node_id=REVIEW_NODE,
                cycle=cycle,
            ),
        )
        return {"continue_loop": False, "current_focus_task": next_focus_task or state["user_input"]}

    def finalize_node(state: SupervisorState) -> SupervisorState:
        push(
            trace,
            event(
                "node_entered",
                "Enter Finalizer",
                "Finalizer is composing final response from supervisor loop reports.",
                node_id=FINALIZE_NODE,
            ),
        )
        finalizer_worker = worker_by_id.get(state.get("current_worker_id", ""), workers[0])
        combined_report = state.get("combined_report", "")
        assistant_message = llm_gateway.finalize(
            user_input=state["user_input"],
            agent=finalizer_worker,
            specialist_answer=combined_report,
        )
        push(
            trace,
            event(
                "node_exited",
                "Exit Finalizer",
                "Finalizer completed.",
                node_id=FINALIZE_NODE,
            ),
        )
        return {"assistant_message": assistant_message}

    def review_next(state: SupervisorState) -> str:
        if bool(state.get("continue_loop")):
            return DELEGATION_NODE
        return FINALIZE_NODE if workflow.finalizer_enabled else END

    def delegation_next(state: SupervisorState) -> str:
        worker_id = str(state.get("current_worker_id", ""))
        if worker_id in worker_by_id:
            return worker_id
        return workers[0].id

    app = _compile_supervisor_app(
        workflow,
        workers,
        intake_node=intake_node,
        delegation_node=delegation_node,
        make_worker_node=make_worker_node,
        review_node=review_node,
        delegation_next=delegation_next,
        review_next=review_next,
        finalize_node=finalize_node if workflow.finalizer_enabled else None,
    )
    graph = workflow_graph_from_compiled(app)
    final_state = app.invoke({"user_input": user_input})

    specialist_answer = str(final_state.get("combined_report", ""))
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

    route_agent_id = str(final_state.get("current_worker_id", ""))
    route_agent_name = str(final_state.get("current_worker_name", ""))
    cycle = int(final_state.get("cycle", 0))
    artifacts = RunArtifacts(
        route_agent_id=route_agent_id or None,
        route_agent_name=route_agent_name or None,
        route_reason=f"Supervisor loop finished after {cycle} cycle(s).",
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
