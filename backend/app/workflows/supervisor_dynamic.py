from __future__ import annotations

from collections.abc import Callable
from pathlib import PurePosixPath
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
    workspace_dir: str
    artifacts: list[str]
    tool_evidence: list[str]
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


def _derive_workspace_dir(artifacts: list[str]) -> str:
    cleaned = [str(item).strip().replace("\\", "/") for item in artifacts if str(item).strip()]
    if not cleaned:
        return ""
    parents: list[PurePosixPath] = []
    for item in cleaned:
        candidate = PurePosixPath(item)
        parents.append(candidate if item.endswith("/") else candidate.parent)
    if not parents:
        return ""
    common = parents[0]
    for parent in parents[1:]:
        prefix_parts: list[str] = []
        for left, right in zip(common.parts, parent.parts):
            if left != right:
                break
            prefix_parts.append(left)
        if not prefix_parts:
            return ""
        common = PurePosixPath(*prefix_parts)
    common_text = common.as_posix().strip()
    return "" if common_text in {"", "."} else common_text


def _workspace_context_text(workspace_dir: str, artifacts: list[str]) -> str:
    artifact_lines = "\n".join(f"- {item}" for item in artifacts[-8:]) if artifacts else "(none yet)"
    return (
        f"Shared workspace directory: {workspace_dir or '(not confirmed yet)'}\n"
        f"Known artifacts:\n{artifact_lines}"
    )


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
    tool_evidence: list[str] = []
    latest_tool_artifacts: dict[str, dict[str, Any]] = {}

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
                tool_evidence.append(f"{agent.name} tool {tool_name} blocked: {reason[:300]}")
                latest_tool_artifacts[agent.id] = {
                    "output_dir": "",
                    "generated_files": [],
                }
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
            output_dir = str(meta.get("output_dir") or "").strip()
            error_text = str(meta.get("error") or "").strip()
            preview = str(meta.get("result_preview") or "").strip()
            evidence_parts = [
                f"agent={agent.name}",
                f"tool={tool_name}",
                f"ok={ok}",
            ]
            if output_dir:
                evidence_parts.append(f"output_dir={output_dir}")
            if files:
                evidence_parts.append("generated_files=" + ", ".join(str(item) for item in files[:8]))
            if error_text:
                evidence_parts.append(f"error={error_text[:240]}")
            if preview:
                evidence_parts.append(f"preview={preview[:240]}")
            tool_evidence.append("; ".join(evidence_parts))
            latest_tool_artifacts[agent.id] = {
                "output_dir": output_dir,
                "generated_files": [str(item).strip() for item in files if str(item).strip()],
            }
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
            "workspace_dir": "",
            "artifacts": [],
            "tool_evidence": [],
            "current_focus_task": state["user_input"],
            "reports": [],
        }

    def delegation_node(state: SupervisorState) -> SupervisorState:
        cycle = int(state.get("cycle", 0)) + 1
        focus_task = str(state.get("current_focus_task") or state["user_input"]).strip() or state["user_input"]
        workspace_dir = str(state.get("workspace_dir") or "").strip()
        artifacts = [str(item).strip() for item in (state.get("artifacts") or []) if str(item).strip()]
        workspace_context = _workspace_context_text(workspace_dir, artifacts)
        reports = list(state.get("reports", []))
        reports_block = "\n".join(reports) if reports else "(none yet)"
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
        routing_input = (
            f"{focus_task}\n\n"
            f"Original user request:\n{state['user_input']}\n\n"
            f"Shared workspace context:\n{workspace_context}\n\n"
            f"Completed reports so far:\n{reports_block}\n\n"
            "Select the worker whose responsibility best matches the current task. "
            "Use the team when another specialist can materially improve the result before completion, but do not force handoffs when the current best worker should keep going. "
            "If there are no reports yet and the request reasonably spans product, design, and engineering, prefer a specialist who can clarify or structure the work before final implementation. "
            "After those concerns are covered, prefer the specialist who closes the biggest remaining gap. "
            "If the current task requires creating or modifying files, prefer a worker responsible for implementation or delivery."
        )
        routed_worker_id, route_reason = llm_gateway.route(routing_input, workers)
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
                workspace_dir=workspace_dir,
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
            workspace_dir = str(state.get("workspace_dir") or "").strip()
            artifacts = [str(item).strip() for item in (state.get("artifacts") or []) if str(item).strip()]
            workspace_context = _workspace_context_text(workspace_dir, artifacts)
            worker_input = (
                f"Original user request:\n{state['user_input']}\n\n"
                f"Shared workspace context:\n{workspace_context}\n\n"
                f"Current focus task (cycle {cycle}):\n{state['current_focus_task']}\n\n"
                f"Completed reports so far:\n{completed_summary}\n\n"
                "Execute the current focus task directly.\n"
                "Keep using the shared workspace directory when it is confirmed. If it is not confirmed yet, follow the location stated in the original user request.\n"
                "If the target path or deliverable is already clear, prefer directly creating or writing it instead of repeatedly listing or searching first.\n"
                "Use inspection first only when the path is ambiguous, existing content must be preserved, or you need facts you do not yet have.\n"
                "When you create or change files, report the exact directory and file paths produced."
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
            latest_artifacts = latest_tool_artifacts.get(worker.id, {})
            next_workspace = workspace_dir
            output_dir = str(latest_artifacts.get("output_dir") or "").strip()
            if output_dir:
                next_workspace = output_dir
            next_artifacts = [str(item).strip() for item in (state.get("artifacts") or []) if str(item).strip()]
            for item in latest_artifacts.get("generated_files") or []:
                path_text = str(item).strip()
                if path_text and path_text not in next_artifacts:
                    next_artifacts.append(path_text)
            if not next_workspace:
                next_workspace = _derive_workspace_dir(next_artifacts)

            return {
                "reports": reports,
                "combined_report": "\n\n".join(reports),
                "tool_evidence": list(tool_evidence),
                "workspace_dir": next_workspace,
                "artifacts": next_artifacts,
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

        workspace_dir = str(state.get("workspace_dir") or "").strip()
        artifacts = [str(item).strip() for item in (state.get("artifacts") or []) if str(item).strip()]
        evidence = list(state.get("tool_evidence", tool_evidence))
        workspace_context = _workspace_context_text(workspace_dir, artifacts)
        if not artifacts and cycle < max_cycles:
            next_focus_task = (
                "Create or update the requested deliverable in the shared workspace. "
                f"{workspace_context}. "
                "Use filesystem tools to produce concrete files and report the exact paths created or changed."
            )
            review_reason = "No concrete artifacts are confirmed yet, so the workflow should continue toward real file output."
            push(
                trace,
                event(
                    "state_updated",
                    "Review Decision",
                    "Supervisor decided to continue until concrete artifacts exist.",
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
                "current_focus_task": next_focus_task,
            }
        review_reports = [
            *reports,
            (
                "Supervisor shared workspace state:\n"
                f"{workspace_context}\n"
                "Tool evidence:\n"
                + ("\n".join(evidence[-8:]) if evidence else "(none yet)")
                + "\nSupervisor guidance:\n"
                + "If another specialist can materially improve the result before completion, prefer continuing with a concrete next focus task instead of ending early."
            ),
        ]
        should_continue, next_focus_task, review_reason = llm_gateway.supervisor_review_decision(
            user_input=state["user_input"],
            reports=review_reports,
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
        evidence_block = "\n".join(list(state.get("tool_evidence", tool_evidence))[-8:]) if state.get("tool_evidence") else "(none yet)"
        specialist_answer = (
            f"{combined_report}\n\n"
            "Shared workspace context:\n"
            f"{_workspace_context_text(str(state.get('workspace_dir') or '').strip(), list(state.get('artifacts') or []))}\n\n"
            "Tool evidence:\n"
            f"{evidence_block}"
        ).strip()
        assistant_message = llm_gateway.finalize(
            user_input=state["user_input"],
            agent=finalizer_worker,
            specialist_answer=specialist_answer,
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
