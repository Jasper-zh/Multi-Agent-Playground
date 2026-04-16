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


PLANNER_NODE = "planner_core"
VALIDATOR_NODE = "planner_validator"
DISPATCH_NODE = "task_dispatcher"
SYNTH_NODE = "synthesizer"


class PlannerState(TypedDict, total=False):
    user_input: str
    tasks: list[str]
    plan_source: str
    planning_round: int
    replan_required: bool
    task_index: int
    current_task: str
    current_worker_id: str
    current_worker_name: str
    current_route_reason: str
    task_reports: list[str]
    combined_report: str
    assistant_message: str


def event(
    event_type: str,
    title: str,
    detail: str,
    **payload: object,
) -> TraceEvent:
    return TraceEvent(type=event_type, title=title, detail=detail, payload=payload)


def _extract_tasks(user_input: str, max_tasks: int = 4) -> list[str]:
    return llm_gateway._fallback_plan_tasks(user_input, max_tasks=max_tasks)  # type: ignore[attr-defined]


def _needs_replan(user_input: str, tasks: list[str]) -> bool:
    if len(tasks) >= 2:
        return False
    lowered = user_input.lower()
    multi_hints = (
        " and ",
        " also ",
        " then ",
        "同时",
        "另外",
        "并且",
        "然后",
        "接着",
        "最后",
    )
    return any(hint in lowered for hint in multi_hints) or len(user_input.strip()) > 120


def _fallback_split_for_replan(user_input: str) -> list[str]:
    raw = user_input.strip()
    if len(raw) <= 36:
        return [raw]
    midpoint = len(raw) // 2
    return [raw[:midpoint].strip(), raw[midpoint:].strip()]


def _compile_planner_app(
    workflow: WorkflowDefinition,
    workers: list[AgentDefinition],
    planner_node: Callable[[PlannerState], PlannerState],
    validator_node: Callable[[PlannerState], PlannerState],
    dispatcher_node: Callable[[PlannerState], PlannerState],
    make_worker_node: Callable[[AgentDefinition], Callable[[PlannerState], PlannerState]],
    validator_next: Callable[[PlannerState], str],
    dispatch_next: Callable[[PlannerState], str],
    worker_next: Callable[[PlannerState], str],
    synth_node: Callable[[PlannerState], PlannerState] | None = None,
):
    builder = StateGraph(PlannerState)
    builder.add_node(PLANNER_NODE, planner_node, metadata={"kind": "logic", "label": "Planner Core"})
    builder.add_node(VALIDATOR_NODE, validator_node, metadata={"kind": "logic", "label": "Plan Validator"})
    builder.add_node(DISPATCH_NODE, dispatcher_node, metadata={"kind": "logic", "label": "Task Dispatcher"})
    for worker in workers:
        builder.add_node(
            worker.id,
            make_worker_node(worker),
            metadata={"kind": "agent", "label": worker.name},
        )
    if workflow.finalizer_enabled and synth_node is not None:
        builder.add_node(SYNTH_NODE, synth_node, metadata={"kind": "final", "label": "Synthesizer"})

    builder.add_edge(START, PLANNER_NODE)
    builder.add_edge(PLANNER_NODE, VALIDATOR_NODE)
    builder.add_conditional_edges(
        VALIDATOR_NODE,
        validator_next,
        {
            PLANNER_NODE: PLANNER_NODE,
            DISPATCH_NODE: DISPATCH_NODE,
        },
    )

    dispatch_targets = {worker.id: worker.id for worker in workers}
    if workflow.finalizer_enabled and synth_node is not None:
        dispatch_targets[SYNTH_NODE] = SYNTH_NODE
    else:
        dispatch_targets[END] = END
    builder.add_conditional_edges(DISPATCH_NODE, dispatch_next, dispatch_targets)

    for worker in workers:
        builder.add_conditional_edges(worker.id, worker_next, {DISPATCH_NODE: DISPATCH_NODE})

    if workflow.finalizer_enabled and synth_node is not None:
        builder.add_edge(SYNTH_NODE, END)

    return builder.compile()


def build_planner_graph(
    workflow: WorkflowDefinition,
    agents: list[AgentDefinition],
) -> WorkflowGraph:
    if len(agents) < 2:
        raise HTTPException(status_code=400, detail="planner_executor requires at least 2 agents.")

    default_worker = agents[0]

    def noop(_: PlannerState) -> PlannerState:
        return {}

    def noop_dispatch(_: PlannerState) -> PlannerState:
        return {"current_worker_id": default_worker.id}

    def make_noop_worker(_: AgentDefinition):
        return noop

    def validator_next(_: PlannerState) -> str:
        return DISPATCH_NODE

    def dispatch_next(state: PlannerState) -> str:
        selected = str(state.get("current_worker_id", ""))
        return selected if selected else default_worker.id

    def worker_next(_: PlannerState) -> str:
        return DISPATCH_NODE

    app = _compile_planner_app(
        workflow,
        agents,
        planner_node=noop,
        validator_node=noop,
        dispatcher_node=noop_dispatch,
        make_worker_node=make_noop_worker,
        validator_next=validator_next,
        dispatch_next=dispatch_next,
        worker_next=worker_next,
        synth_node=noop if workflow.finalizer_enabled else None,
    )
    return workflow_graph_from_compiled(app)


def run_planner_executor(
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
        raise HTTPException(status_code=400, detail="planner_executor requires at least 2 valid agents.")

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

    def planner_node(state: PlannerState) -> PlannerState:
        planning_round = int(state.get("planning_round", 0))
        is_replan = planning_round > 0
        enter_title = "Re-enter Planner Core" if is_replan else "Enter Planner Core"
        state_title = "Revised Plan Created" if is_replan else "Draft Plan Created"
        enter_detail = (
            "Planner core is revising the task plan."
            if is_replan
            else "Planner core is decomposing user request."
        )

        push(
            trace,
            event(
                "node_entered",
                enter_title,
                enter_detail,
                node_id=PLANNER_NODE,
            ),
        )

        tasks, plan_source = llm_gateway.plan_tasks(
            state["user_input"],
            max_tasks=4,
            force_multi=is_replan,
            agents=workers,
        )
        if not tasks:
            tasks = _extract_tasks(state["user_input"])
            plan_source = "rule"

        if is_replan and len(tasks) < 2:
            tasks = _fallback_split_for_replan(state["user_input"])
            plan_source = "rule"

        push(
            trace,
            event(
                "state_updated",
                state_title,
                f"Planner drafted {len(tasks)} task(s).",
                node_id=PLANNER_NODE,
                plan_source=plan_source,
                tasks=tasks,
            ),
        )
        push(
            trace,
            event(
                "node_exited",
                "Exit Planner Core",
                "Plan produced.",
                node_id=PLANNER_NODE,
            ),
        )
        return {
            "tasks": tasks,
            "plan_source": plan_source,
            "task_index": 0,
        }

    def validator_node(state: PlannerState) -> PlannerState:
        planning_round = int(state.get("planning_round", 0))
        tasks = state.get("tasks", [])
        enter_title = "Re-enter Plan Validator" if planning_round > 0 else "Enter Plan Validator"
        push(
            trace,
            event(
                "node_entered",
                enter_title,
                "Validator is checking plan quality.",
                node_id=VALIDATOR_NODE,
            ),
        )

        should_replan = planning_round == 0 and _needs_replan(state["user_input"], tasks)
        if should_replan:
            push(
                trace,
                event(
                    "state_updated",
                    "Validation Failed",
                    "Plan is too coarse, sending back to planner core for one revision.",
                    node_id=VALIDATOR_NODE,
                ),
            )
            push(
                trace,
                event(
                    "route_selected",
                    "Replan Requested",
                    "Validator routed plan back to planner core.",
                    node_id=VALIDATOR_NODE,
                    next_node_id=PLANNER_NODE,
                ),
            )
            push(
                trace,
                event(
                    "node_exited",
                    "Exit Plan Validator",
                    "Validation requested one revision.",
                    node_id=VALIDATOR_NODE,
                ),
            )
            return {"replan_required": True, "planning_round": planning_round + 1}

        push(
            trace,
            event(
                "state_updated",
                "Validation Passed",
                "Plan approved. Ready for dispatch.",
                node_id=VALIDATOR_NODE,
            ),
        )
        push(
            trace,
            event(
                "node_exited",
                "Exit Plan Validator",
                "Validation completed.",
                node_id=VALIDATOR_NODE,
            ),
        )
        return {"replan_required": False}

    def dispatcher_node(state: PlannerState) -> PlannerState:
        tasks = state.get("tasks", [])
        task_index = int(state.get("task_index", 0))
        if task_index >= len(tasks):
            terminal_node = SYNTH_NODE if workflow.finalizer_enabled else "end"
            push(
                trace,
                event(
                    "node_entered",
                    "Enter Dispatcher",
                    "Dispatcher is checking whether all tasks are complete.",
                    node_id=DISPATCH_NODE,
                ),
            )
            push(
                trace,
                event(
                    "route_selected",
                    "Dispatch Complete",
                    f"Dispatcher routed to {terminal_node}.",
                    node_id=DISPATCH_NODE,
                    next_node_id=terminal_node,
                ),
            )
            push(
                trace,
                event(
                    "node_exited",
                    "Exit Dispatcher",
                    "No remaining tasks to assign.",
                    node_id=DISPATCH_NODE,
                ),
            )
            return {}

        task = tasks[task_index]
        human_index = task_index + 1
        push(
            trace,
            event(
                "node_entered",
                "Enter Dispatcher",
                f"Dispatcher is assigning task {human_index}.",
                node_id=DISPATCH_NODE,
                task_index=human_index,
            ),
        )
        routed_worker_id, route_reason = llm_gateway.route(task, workers)
        worker = worker_by_id[routed_worker_id]
        push(
            trace,
            event(
                "route_selected",
                "Task Assigned",
                f"Task {human_index} routed to {worker.name}.",
                node_id=DISPATCH_NODE,
                next_node_id=worker.id,
                reason=route_reason,
                task=task,
            ),
        )
        push(
            trace,
            event(
                "node_exited",
                "Exit Dispatcher",
                f"Task {human_index} assignment completed.",
                node_id=DISPATCH_NODE,
            ),
        )
        return {
            "current_task": task,
            "current_worker_id": worker.id,
            "current_worker_name": worker.name,
            "current_route_reason": route_reason,
        }

    def make_worker_node(worker: AgentDefinition):
        def worker_node(state: PlannerState) -> PlannerState:
            task_index = int(state.get("task_index", 0))
            tasks = state.get("tasks", [])
            if task_index >= len(tasks):
                return {}

            human_index = task_index + 1
            push(
                trace,
                event(
                    "node_entered",
                    "Enter Worker",
                    f"{worker.name} is working on task {human_index}.",
                    node_id=worker.id,
                    task_index=human_index,
                ),
            )
            prior_reports = list(state.get("task_reports", []))
            prior_reports_text = "\n\n".join(prior_reports) if prior_reports else "None yet."
            worker_input = (
                f"Original user request:\n{state['user_input']}\n\n"
                "What is already available from previous work:\n"
                f"{prior_reports_text}\n\n"
                f"Current task {human_index}:\n{state['current_task']}\n\n"
                "Execute the current task directly.\n"
                "This is an execution task, not a discussion task. Prefer tool actions over prose.\n"
                "Build on the available prior work when relevant instead of ignoring it.\n\n"
                "Hard rules:\n"
                "1. If the task involves files, directories, code, project outputs, desktop paths, downloads paths, or generated artifacts, you must use filesystem tools to verify or create them before making factual claims.\n"
                "2. Do not guess whether a file, directory, project, or output exists.\n"
                "3. If an expected file or directory does not exist and the current task requires it, create it instead of merely describing it.\n"
                "4. If you need the correct path, search or list first, then read or write.\n"
                "5. Do not replace a missing tool action with speculation.\n"
                "6. Do not write a project-management update, a plan recap, or generic suggestions unless the current task is explicitly analysis-only.\n"
                "7. If you changed, created, or verified files, say exactly which paths were involved.\n"
                "8. If you could not complete the task, state the exact blocker and what you already verified with tools.\n"
                "9. If the current task is to build a page, app, feature, or tool, do not treat a static shell or styling-only output as completion when functional behavior is still required by the task.\n"
                "10. Your result message must summarize the concrete implemented behavior, not only visual or structural changes.\n\n"
                "Execution policy:\n"
                "- Need to know whether something exists: check with tools first.\n"
                "- If the target path or deliverable is already clear, prefer directly creating or writing it instead of repeatedly listing or searching first.\n"
                "- Need a directory: create it.\n"
                "- Need a file: write it.\n"
                "- Need file contents: read it.\n"
                "- Need to find the right target: search/list first, then operate.\n\n"
                "Respond in natural language with the concrete result of the current task only. "
                "If the task expects working behavior, describe the implemented behavior explicitly rather than only saying that files or UI were created."
            )
            task_answer = llm_gateway.run_agent(
                worker,
                worker_input,
                history=history,
                trace_hook=make_tool_trace_hook(worker),
            )
            task_reports = prior_reports
            task_runtime_issue = llm_gateway._is_tool_blocked_response(task_answer)  # type: ignore[attr-defined]
            if task_runtime_issue:
                task_reports.append(f"Task {human_index} by {worker.name} encountered an execution issue:\n{task_answer}")
            else:
                task_reports.append(f"Task {human_index} by {worker.name}:\n{task_answer}")
            push(
                trace,
                event(
                    "message_generated",
                    "Worker Issue" if task_runtime_issue else "Worker Output",
                    (
                        f"{worker.name} encountered an execution issue on task {human_index}."
                        if task_runtime_issue
                        else f"{worker.name} finished task {human_index}."
                    ),
                    node_id=worker.id,
                    preview=task_answer[:120],
                ),
            )
            push(
                trace,
                event(
                    "node_exited",
                    "Exit Worker",
                    f"{worker.name} reported task {human_index}.",
                    node_id=worker.id,
                ),
            )

            next_index = len(tasks) if task_runtime_issue else task_index + 1
            push(
                trace,
                event(
                    "route_selected",
                    "Return To Dispatcher",
                    (
                        "Worker returned control to dispatcher after an execution issue."
                        if task_runtime_issue
                        else "Worker returned control to dispatcher for the next routing decision."
                    ),
                    node_id=worker.id,
                    next_node_id=DISPATCH_NODE,
                    task_index=human_index,
                ),
            )

            return {
                "task_reports": task_reports,
                "task_index": next_index,
                "combined_report": "\n\n".join(task_reports),
            }

        return worker_node

    def synth_node(state: PlannerState) -> PlannerState:
        push(
            trace,
            event(
                "node_entered",
                "Enter Synthesizer",
                "Synthesizer is composing the final answer from worker reports.",
                node_id=SYNTH_NODE,
            ),
        )
        combined_report = state.get("combined_report", "")
        finalizer_worker = worker_by_id.get(state.get("current_worker_id", ""), workers[0])
        assistant_message = llm_gateway.finalize(
            user_input=state["user_input"],
            agent=finalizer_worker,
            specialist_answer=combined_report,
        )
        push(
            trace,
            event(
                "node_exited",
                "Exit Synthesizer",
                "Synthesis completed.",
                node_id=SYNTH_NODE,
            ),
        )
        return {"assistant_message": assistant_message}

    def validator_next(state: PlannerState) -> str:
        return PLANNER_NODE if bool(state.get("replan_required")) else DISPATCH_NODE

    def dispatch_next(state: PlannerState) -> str:
        task_index = int(state.get("task_index", 0))
        tasks_len = len(state.get("tasks", []))
        if task_index >= tasks_len:
            return SYNTH_NODE if workflow.finalizer_enabled else END
        worker_id = str(state.get("current_worker_id", ""))
        if worker_id in worker_by_id:
            return worker_id
        return workers[0].id

    def worker_next(state: PlannerState) -> str:
        return DISPATCH_NODE

    app = _compile_planner_app(
        workflow,
        workers,
        planner_node=planner_node,
        validator_node=validator_node,
        dispatcher_node=dispatcher_node,
        make_worker_node=make_worker_node,
        validator_next=validator_next,
        dispatch_next=dispatch_next,
        worker_next=worker_next,
        synth_node=synth_node if workflow.finalizer_enabled else None,
    )
    graph = workflow_graph_from_compiled(app)
    final_state = app.invoke(
        {
            "user_input": user_input,
            "planning_round": 0,
            "task_index": 0,
            "task_reports": [],
        }
    )

    combined_report = str(final_state.get("combined_report", ""))
    if not combined_report:
        combined_report = "\n\n".join(final_state.get("task_reports", []))
    if workflow.finalizer_enabled:
        assistant_message = str(final_state.get("assistant_message", combined_report))
    else:
        assistant_message = combined_report

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
    plan_source = str(final_state.get("plan_source", ""))
    artifacts = RunArtifacts(
        route_agent_id=route_agent_id or None,
        route_agent_name=route_agent_name or None,
        route_reason=(f"Planner source={plan_source}; approved {len(final_state.get('tasks', []))} task(s)."),
        specialist_answer=combined_report or None,
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
