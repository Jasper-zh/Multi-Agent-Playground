from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any, TypedDict

from fastapi import HTTPException
from langgraph.graph import END, START, StateGraph

from ..runtime import llm_gateway
from ..settings_bridge import settings
from ..schemas import (
    AgentDefinition,
    RunArtifacts,
    TraceEvent,
    WorkflowDefinition,
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNode,
    WorkflowRunResponse,
)
from ..store import InMemoryPlaygroundStore


ROUTER_NODE = "first_owner_router"
GROUP_NODE = "peer_pool"
PEER_EXEC_PREFIX = "peer_exec__"
DECISION_NODE = "handoff_decision"
FINALIZER_NODE = "finalize"


class AgentAction(TypedDict, total=False):
    action: str
    target_agent_id: str
    task_title: str
    message: str
    raw_response: str


class PeerState(TypedDict, total=False):
    user_input: str
    current_task_title: str
    confirmed_workspace: str
    confirmed_paths: list[str]
    current_owner_id: str
    current_owner_name: str
    last_worker_id: str
    last_worker_name: str
    route_reason: str
    reports: list[str]
    hop_count: int
    max_hops: int
    assistant_message: str
    terminal_status: str
    pending_action: str
    pending_target_agent_id: str
    pending_task_title: str


class ToolOutcome(TypedDict, total=False):
    blocked: bool
    failed: bool
    ok: bool
    message: str


class ToolArtifacts(TypedDict, total=False):
    output_dir: str
    generated_files: list[str]


class RootCompletionDecision(TypedDict, total=False):
    root_complete: bool
    reason: str
    target_agent_id: str
    next_task: str


PEER_HANDOFF_ACTION_EXAMPLES = (
    "## Examples\n"
    "### Continue\n"
    '{"action":"continue","message":"I confirmed the workspace and existing files. The remaining JavaScript behavior is still missing, so I should continue implementing it myself before handing off or completing."}\n\n'
    "### Handoff\n"
    '{"action":"handoff","target_agent_id":"agent_designer","task_title":"Create UI/UX design based on the completed PRD","message":"I completed the PRD with scope, core features, and acceptance criteria. Please produce the UI/UX design next."}\n\n'
    "### Complete\n"
    '{"action":"complete","message":"I updated the project files, added the missing interaction logic, and verified the calculator now responds correctly."}\n\n'
    "### Block\n"
    '{"action":"block","message":"The required API key is missing, so I cannot call the deployment tool. Please provide the key or let another peer handle a non-deployment path."}\n\n'
    "### Bad\n"
    '- {"action":"block","message":"TOOL_EXECUTION_NO_FINAL_ANSWER ..."}\n'
    '- {"action":"complete","message":"I found the cause. Next I will fix it."}\n'
)

INTERNAL_RUNTIME_MARKERS = (
    "TOOL_EXECUTION_NO_FINAL_ANSWER",
    "TOOL_EXECUTION_BLOCKED",
    "TOOL_UNAVAILABLE",
    "Tool-enabled execution completed",
    "This result should not be treated as task completion.",
    "This result should be retried, continued by the planner, or handed to another step.",
)

INTERNAL_RUNTIME_LINE_PREFIXES = (
    "Selected tools:",
    "Verified evidence:",
    "Tool:",
    "Skill:",
    "Attempts:",
    "Reason:",
    "Error code:",
)


def _build_peer_execution_prompt(
    *,
    user_input: str,
    current_task_title: str,
    peer_directory: str,
    available_outputs: str,
    workspace_context: str,
) -> str:
    return (
        "# Peer Task Execution\n"
        "Execute the current task directly. Do not output workflow action JSON in this step.\n\n"
        "## Execution rules\n"
        "- Focus on doing the current task, not deciding the next workflow action.\n"
        "- If the current task is not fully complete and you can continue, continue executing instead of returning a progress update.\n"
        "- If the task gives a clear target path or deliverable, create or update it directly; inspect first only when the path is ambiguous, existing content must be preserved, or you need facts you do not have.\n"
        "- Prefer doing the necessary file operations over repeatedly listing or describing what should be done.\n"
        "- Make reasonable assumptions and continue when optional preferences or non-critical details are missing.\n"
        "- Do not pause execution just to ask for style, naming, or preference details that can be sensibly defaulted.\n"
        "- Only report files, directories, documents, mockups, screenshots, code, behaviors, or artifacts that you actually created or verified with successful tool results in this run.\n"
        "- If a file or artifact has not been created yet, say it is not yet created; do not imply it already exists.\n"
        "- If functionality has not been implemented or verified yet, say it is still missing; do not describe it as already working.\n"
        "- Never invent tool results, file contents, validation outcomes, browser checks, or successful delivery states.\n"
        "- If you cannot complete the current task, state the real blocker and what you already completed or verified.\n"
        "- If the current task is to build a page, app, tool, or feature, do not treat visual styling alone as completion when interactive behavior or functional logic is still missing.\n"
        "- Summarize only the concrete work actually completed and verified at the end of this execution step.\n\n"
        "## Context\n"
        f"Original user request:\n{user_input}\n\n"
        f"Confirmed delivery/workspace path:\n{workspace_context}\n\n"
        f"What is already available from previous peers:\n{available_outputs}\n\n"
        f"Available peers:\n{peer_directory}\n\n"
        f"Current task:\n{current_task_title}"
    )


PEER_EXECUTION_FINAL_RESPONSE_INSTRUCTION = (
    "Using the tool results already collected above, respond with an execution summary only. "
    "Do not decide workflow actions in this step. "
    "Only claim files, directories, artifacts, validations, and implemented behavior that are explicitly supported by successful tool results in this run. "
    "If something is still missing, say it is still missing. "
    "Do not invent created files, completed functionality, or successful verification that did not happen."
)


def _build_peer_decision_prompt(
    *,
    user_input: str,
    current_task_title: str,
    peer_directory: str,
    available_outputs: str,
    workspace_context: str,
    execution_result: str,
) -> str:
    return (
        "# Peer Handoff Decision\n"
        "You are the same peer agent that just executed the task. Now decide the next workflow action.\n"
        "Return exactly one JSON object and nothing else. Do not use markdown outside the JSON object.\n\n"
        "## Allowed actions\n"
        '- {"action":"continue","message":"<what you will continue doing next>"}\n'
        '- {"action":"handoff","target_agent_id":"<peer-id>","task_title":"<next task>","message":"<handoff reason>"}\n'
        '- {"action":"review","target_agent_id":"<peer-id>","task_title":"<review task>","message":"<review reason>"}\n'
        '- {"action":"complete","message":"<task result>"}\n'
        '- {"action":"respond_user","message":"<final user-facing answer>"}\n'
        '- {"action":"block","message":"<real blocker only>"}\n\n'
        "## Decision rules\n"
        "- Base the decision on the execution result and the original user request.\n"
        "- Use `complete` only if the original user request is fully satisfied by the accumulated outputs.\n"
        "- If the current step is done but the original user request still needs more work, use `handoff` to the most suitable peer with a concrete next task.\n"
        "- If you are still the best peer and more work remains, use `continue` instead of `handoff` or `block`.\n"
        "- Use `respond_user` only when the user explicitly asked for a direct answer now, or when missing information creates a real blocker that prevents sensible progress.\n"
        "- Do not ask the user for optional preferences; make reasonable assumptions and continue via handoff when possible.\n"
        "- Do not claim a file, document, mockup, screenshot, or artifact was created unless the execution result or previous outputs actually support that claim.\n"
        "- Do not claim tool restrictions, permission errors, disabled capabilities, or environment limits unless the execution result or tool outputs explicitly show them.\n"
        "- Never target yourself with `handoff` or `review`; use `continue` when you should keep working.\n\n"
        "## Context\n"
        f"Original user request:\n{user_input}\n\n"
        f"Confirmed delivery/workspace path:\n{workspace_context}\n\n"
        f"Current task:\n{current_task_title}\n\n"
        f"What is already available from previous peers:\n{available_outputs}\n\n"
        f"Latest execution result:\n{execution_result}\n\n"
        f"Available peers:\n{peer_directory}\n\n"
        f"{PEER_HANDOFF_ACTION_EXAMPLES}"
    )


PEER_HANDOFF_FINAL_RESPONSE_INSTRUCTION = (
    "Return ONLY one JSON object that matches one allowed action exactly. "
    "Do not use markdown. Do not output prose outside JSON. "
    "Never copy internal runtime markers into message, including TOOL_EXECUTION_NO_FINAL_ANSWER, TOOL_EXECUTION_BLOCKED, or Tool-enabled execution completed. "
    "The message must contain business context, not runtime diagnostics. "
    "The message must describe concrete completed work, not a generic progress update. "
    "If tool execution did not yield enough useful information, choose handoff or review when another peer can continue. "
    'If no clearly better peer exists for the remaining work, continue executing instead of handing off. '
    'If the current task is not fully complete, do not stop; either continue executing it yourself or hand it off to a more suitable peer. '
    'Make reasonable assumptions and continue when optional preferences or non-critical details are missing. '
    'Use "respond_user" only when the user explicitly asked for a direct answer now, or when missing information creates a real blocker that prevents sensible progress. '
    'Do not return a partial completion, progress update, or next-step note as the terminal action for the current task. '
    'Do not claim files, documents, mockups, screenshots, or other artifacts unless you actually created or verified them with successful tool results. '
    'Use "complete" only for work that has already been executed. '
    'If the message implies future work, remaining implementation, or a next step for another peer, do not use "complete". '
    'Do not use "complete" for a static shell, placeholder UI, or partial implementation when the current task still requires functional behavior. '
    'If downstream work remains, prefer "handoff". '
    'Use "block" only for a real blocker, not for incomplete execution or runtime summary text. '
    'Allowed actions: {"action":"continue","message":"<what you will continue doing next>"}, '
    '{"action":"handoff","target_agent_id":"<peer-id>","task_title":"<next task>","message":"<handoff reason>"}, '
    '{"action":"review","target_agent_id":"<peer-id>","task_title":"<review task>","message":"<review reason>"}, '
    '{"action":"complete","message":"<task result>"}, '
    '{"action":"respond_user","message":"<final user-facing answer>"}, '
    '{"action":"block","message":"<real blocker only>"}.'
)


def event(
    event_type: str,
    title: str,
    detail: str,
    **payload: object,
) -> TraceEvent:
    return TraceEvent(type=event_type, title=title, detail=detail, payload=payload)


def _estimate_max_hops(agent_count: int, user_input: str) -> int:
    base = max(5, min(12, agent_count * 3 + 2))
    text = str(user_input or "").strip()
    if len(text) > 220:
        return min(14, base + 2)
    return base


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    text = str(raw or "").strip()
    if not text:
        return None

    fenced = re.match(r"^```(?:json)?\s*(\{.*\})\s*```$", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _normalize_action_name(raw: str) -> str:
    lowered = str(raw or "").strip().lower()
    if lowered in {"continue", "continue_self", "keep_going"}:
        return "continue"
    if lowered in {"handoff", "delegate", "handoff_to"}:
        return "handoff"
    if lowered in {"review", "review_by", "request_review"}:
        return "review"
    if lowered in {"complete", "done", "finish"}:
        return "complete"
    if lowered in {"respond_user", "respond", "final", "answer_user"}:
        return "respond_user"
    if lowered in {"block", "blocked"}:
        return "block"
    return lowered


def _parse_agent_action(raw_response: str) -> AgentAction | None:
    payload = _extract_json_object(raw_response)
    if payload is None:
        return None

    action = _normalize_action_name(payload.get("action", ""))
    if action not in {"continue", "handoff", "review", "complete", "respond_user", "block"}:
        return None

    result: AgentAction = {
        "action": action,
        "raw_response": raw_response,
        "message": str(payload.get("message") or "").strip(),
    }
    target_agent_id = str(payload.get("target_agent_id") or "").strip()
    task_title = str(payload.get("task_title") or "").strip()
    if target_agent_id:
        result["target_agent_id"] = target_agent_id
    if task_title:
        result["task_title"] = task_title
    return result


def _contains_internal_runtime_text(text: str) -> bool:
    message = str(text or "").strip()
    if not message:
        return False
    return any(marker in message for marker in INTERNAL_RUNTIME_MARKERS)


def _sanitize_action_message(text: str) -> str:
    message = str(text or "").strip()
    if not message:
        return ""

    lines: list[str] = []
    for raw_line in message.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if any(marker in line for marker in INTERNAL_RUNTIME_MARKERS):
            continue
        if any(line.startswith(prefix) for prefix in INTERNAL_RUNTIME_LINE_PREFIXES):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _validate_agent_action(action: AgentAction) -> str | None:
    action_name = str(action.get("action") or "").strip()
    message = str(action.get("message") or "").strip()
    target_agent_id = str(action.get("target_agent_id") or "").strip()
    task_title = str(action.get("task_title") or "").strip()

    if action_name in {"handoff", "review"}:
        if not target_agent_id:
            return f"{action_name} requires target_agent_id"
        if not task_title:
            return f"{action_name} requires task_title"
        if not message:
            return f"{action_name} requires message"
    elif action_name in {"continue", "complete", "respond_user", "block"} and not message:
        return f"{action_name} requires message"

    if message and _contains_internal_runtime_text(message):
        return "message contains internal runtime text"
    return None


def _fallback_action(raw_response: str) -> AgentAction:
    text = _sanitize_action_message(raw_response)
    return {
        "action": "block",
        "message": text or "Agent returned an invalid workflow action payload.",
        "raw_response": raw_response,
    }


def _repair_agent_action(
    *,
    raw_response: str,
    worker: AgentDefinition,
    workers: list[AgentDefinition],
    user_input: str,
    current_task_title: str,
    reports: list[str],
    invalid_reason: str,
) -> AgentAction | None:
    if not llm_gateway.api_configured or llm_gateway.client is None:
        return None

    peer_lines = "\n".join(
        f"- id={peer.id}; name={peer.name}; description={peer.description}"
        for peer in workers
        if peer.id != worker.id
    ) or "(no peers)"
    completed_log = _available_outputs_block(reports)

    prompt = (
        "You are a workflow action repair layer.\n"
        "Your only job is to convert the worker output into ONE valid JSON action.\n"
        "Do not do the task again. Do not add markdown. Do not output prose outside JSON.\n\n"
        "Allowed actions:\n"
        '- {"action":"continue","message":"<what you will continue doing next>"}\n'
        '- {"action":"handoff","target_agent_id":"<peer-id>","task_title":"<next task>","message":"<handoff reason>"}\n'
        '- {"action":"review","target_agent_id":"<peer-id>","task_title":"<review task>","message":"<review reason>"}\n'
        '- {"action":"complete","message":"<task result>"}\n'
        '- {"action":"respond_user","message":"<final user-facing answer>"}\n'
        '- {"action":"block","message":"<real blocker only>"}\n\n'
        "Repair rules:\n"
        "- Preserve the original meaning as much as possible.\n"
        "- Never copy internal runtime markers into message, including TOOL_EXECUTION_NO_FINAL_ANSWER or TOOL_EXECUTION_BLOCKED.\n"
        "- If the original output includes prose outside the JSON object, discard the extra prose and keep only one valid JSON object.\n"
        "- If a message contains internal runtime status text, strip it out and rewrite the action with clean business-facing wording.\n"
        "- Rewrite weak messages into concrete summaries of completed work and remaining work when needed.\n"
        "- Prefer continuing with reasonable assumptions over asking the user for optional preferences.\n"
        "- Use respond_user only for a direct user-facing answer or a real blocker that truly needs user input.\n"
        "- If the same peer should keep working, prefer continue over block or self-handoff.\n"
        "- Do not preserve claims about created files, documents, mockups, screenshots, or artifacts unless the original output clearly states they were actually produced or verified.\n"
        "- Do not preserve vague completion claims for static shells, placeholder UI, or partial implementations when the current task still expects functional behavior.\n"
        "- If the output suggests another specialist should continue, prefer handoff.\n"
        "- If the output indicates a real blocker with no clear next peer, use block.\n"
        "- If the output contains a usable task result and no handoff is needed, use complete.\n"
        "- Never target the current worker with handoff or review.\n\n"
        f"Why the original output is invalid:\n{invalid_reason}\n\n"
        f"Current worker:\n- id={worker.id}; name={worker.name}; description={worker.description}\n\n"
        f"Available peers:\n{peer_lines}\n\n"
        f"Original user request:\n{user_input}\n\n"
        f"Current task title:\n{current_task_title}\n\n"
        f"What is already available from previous peers:\n{completed_log}\n\n"
        "Raw worker output to repair:\n"
        f"{raw_response}"
    )

    try:
        response = llm_gateway.client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception:  # noqa: BLE001
        return None

    repaired = (response.choices[0].message.content or "").strip()
    action = _parse_agent_action(repaired)
    if action is None:
        return None
    validation_error = _validate_agent_action(action)
    if validation_error is not None:
        return None
    cleaned_message = _sanitize_action_message(str(action.get("message") or ""))
    if cleaned_message:
        action["message"] = cleaned_message
    action["raw_response"] = raw_response
    return action


def _peer_directory(workers: list[AgentDefinition], current_agent_id: str) -> str:
    lines: list[str] = []
    for worker in workers:
        role = "current_owner" if worker.id == current_agent_id else "peer"
        lines.append(
            f"- id={worker.id}; name={worker.name}; role={role}; specialty={worker.description}"
        )
    return "\n".join(lines)


def _available_outputs_block(reports: list[str]) -> str:
    if not reports:
        return "(none yet)"

    lines: list[str] = []
    for report in reports:
        text = str(report or "").strip()
        if not text:
            continue
        if text.startswith("Runtime note for "):
            continue

        header, separator, message = text.partition(":\n")
        if not separator:
            continue
        compact_message = _sanitize_action_message(message)
        if not compact_message:
            continue
        lines.append(f"- {header}: {compact_message}")

    if not lines:
        return "(none yet)"
    return "\n".join(lines[-4:])


def _workspace_context_text(state: PeerState) -> str:
    confirmed_workspace = str(state.get("confirmed_workspace") or "").strip()
    confirmed_paths = [str(item).strip() for item in (state.get("confirmed_paths") or []) if str(item).strip()]
    if confirmed_workspace and confirmed_paths:
        paths_block = "\n".join(f"- {path}" for path in confirmed_paths[-6:])
        return f"{confirmed_workspace}\nConfirmed files:\n{paths_block}"
    if confirmed_workspace:
        return confirmed_workspace
    if confirmed_paths:
        return "\n".join(f"- {path}" for path in confirmed_paths[-6:])
    return "Not confirmed yet. If the original request specifies a delivery location, keep using that same location consistently."


def _review_root_completion(
    *,
    user_input: str,
    current_task_title: str,
    workspace_context: str,
    last_worker: AgentDefinition,
    workers: list[AgentDefinition],
    reports: list[str],
    action_name: str,
    action_message: str,
) -> RootCompletionDecision | None:
    if not llm_gateway.api_configured or llm_gateway.client is None:
        return None

    peer_lines = "\n".join(
        f"- id={worker.id}; name={worker.name}; specialty={worker.description}"
        for worker in workers
    ) or "(no peers)"
    available_outputs = _available_outputs_block(reports)
    prompt = (
        "You are the root-task completion reviewer for a peer handoff workflow.\n"
        "Your job is to decide whether the ORIGINAL user request is fully complete, not just whether the current subtask looks complete.\n\n"
        "Return ONLY one JSON object with this schema:\n"
        '{"root_complete": true/false, "reason": "<why>", "target_agent_id": "<peer id or current worker id>", "next_task": "<what still needs to be done>"}\n\n'
        "Rules:\n"
        "- Judge completion against the original user request.\n"
        "- If the original user request is fully satisfied, return root_complete=true.\n"
        "- If anything material is still missing, return root_complete=false and provide the best next task.\n"
        "- Prefer continuing toward completion instead of asking the user for more optional preferences.\n"
        "- Only route back to the user when genuine missing information blocks sensible progress; otherwise pick the most suitable peer and next task.\n"
        "- Do not treat a completed design step, requirement step, or implementation step as equivalent to full user-request completion unless the original request is actually satisfied.\n\n"
        f"Original user request:\n{user_input}\n\n"
        f"Confirmed delivery/workspace path:\n{workspace_context}\n\n"
        f"Current task:\n{current_task_title}\n\n"
        f"Latest worker:\n- id={last_worker.id}; name={last_worker.name}; specialty={last_worker.description}\n\n"
        f"Latest proposed action:\n- action={action_name}\n- message={action_message}\n\n"
        f"What is already available from previous peers:\n{available_outputs}\n\n"
        f"Available peers:\n{peer_lines}"
    )

    try:
        response = llm_gateway.client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception:  # noqa: BLE001
        return None

    content = (response.choices[0].message.content or "").strip()
    payload = _extract_json_object(content)
    if payload is None:
        return None

    raw_complete = payload.get("root_complete")
    if isinstance(raw_complete, bool):
        root_complete = raw_complete
    elif isinstance(raw_complete, str):
        root_complete = raw_complete.strip().lower() in {"true", "yes", "1", "done", "complete"}
    else:
        root_complete = bool(raw_complete)

    result: RootCompletionDecision = {
        "root_complete": root_complete,
        "reason": str(payload.get("reason") or "").strip(),
        "target_agent_id": str(payload.get("target_agent_id") or "").strip(),
        "next_task": str(payload.get("next_task") or "").strip(),
    }
    return result


def _peer_exec_node_id(agent_id: str) -> str:
    return f"{PEER_EXEC_PREFIX}{agent_id}"


def _compile_peer_handoff_app(
    workflow: WorkflowDefinition,
    workers: list[AgentDefinition],
    router_node: Callable[[PeerState], PeerState],
    make_peer_exec_node: Callable[[AgentDefinition], Callable[[PeerState], PeerState]],
    decision_node: Callable[[PeerState], PeerState],
    router_next: Callable[[PeerState], str],
    decision_next: Callable[[PeerState], str],
    finalizer_node: Callable[[PeerState], PeerState] | None = None,
):
    builder = StateGraph(PeerState)
    builder.add_node(ROUTER_NODE, router_node, metadata={"kind": "logic", "label": "First Owner Router"})
    for worker in workers:
        builder.add_node(
            _peer_exec_node_id(worker.id),
            make_peer_exec_node(worker),
            metadata={"kind": "agent", "label": worker.name},
        )
    builder.add_node(DECISION_NODE, decision_node, metadata={"kind": "logic", "label": "Handoff Decision"})
    if workflow.finalizer_enabled and finalizer_node is not None:
        builder.add_node(FINALIZER_NODE, finalizer_node, metadata={"kind": "final", "label": "Finalizer"})

    builder.add_edge(START, ROUTER_NODE)
    builder.add_conditional_edges(
        ROUTER_NODE,
        router_next,
        {_peer_exec_node_id(worker.id): _peer_exec_node_id(worker.id) for worker in workers},
    )
    for worker in workers:
        builder.add_edge(_peer_exec_node_id(worker.id), DECISION_NODE)

    decision_targets = {
        **{_peer_exec_node_id(worker.id): _peer_exec_node_id(worker.id) for worker in workers},
        END: END,
    }
    if workflow.finalizer_enabled and finalizer_node is not None:
        decision_targets[FINALIZER_NODE] = FINALIZER_NODE
    builder.add_conditional_edges(DECISION_NODE, decision_next, decision_targets)

    if workflow.finalizer_enabled and finalizer_node is not None:
        builder.add_edge(FINALIZER_NODE, END)

    return builder.compile()


def build_peer_handoff_graph(
    workflow: WorkflowDefinition,
    agents: list[AgentDefinition],
) -> WorkflowGraph:
    if len(agents) < 2:
        raise HTTPException(status_code=400, detail="peer_handoff requires at least 2 agents.")
    nodes = [
        WorkflowNode(id="start", label="START", kind="start"),
        WorkflowNode(id=ROUTER_NODE, label="First Owner Router", kind="logic"),
        WorkflowNode(id=GROUP_NODE, label="Peer Collaboration Zone", kind="group"),
        *[
            WorkflowNode(id=agent.id, label=agent.name, kind="agent", parent_id=GROUP_NODE)
            for agent in agents
        ],
    ]
    if workflow.finalizer_enabled:
        nodes.append(WorkflowNode(id=FINALIZER_NODE, label="Finalizer", kind="final"))
    nodes.append(WorkflowNode(id="end", label="END", kind="end"))

    edges = [
        WorkflowEdge(source="start", target=ROUTER_NODE),
        WorkflowEdge(source=ROUTER_NODE, target=GROUP_NODE),
    ]
    if workflow.finalizer_enabled:
        edges.append(WorkflowEdge(source=GROUP_NODE, target=FINALIZER_NODE))
        edges.append(WorkflowEdge(source=FINALIZER_NODE, target="end"))
    else:
        edges.append(WorkflowEdge(source=GROUP_NODE, target="end"))
    return WorkflowGraph(nodes=nodes, edges=edges)


def run_peer_handoff(
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
        raise HTTPException(status_code=400, detail="peer_handoff requires at least 2 valid agents.")

    worker_by_id = {worker.id: worker for worker in workers}

    def push(trace: list[TraceEvent], item: TraceEvent) -> None:
        trace.append(item)
        if on_event is not None:
            on_event(item)

    trace: list[TraceEvent] = []
    latest_tool_outcome: dict[str, ToolOutcome] = {}
    latest_tool_artifacts: dict[str, ToolArtifacts] = {}

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
                reason = _sanitize_action_message(
                    str(meta.get("reason") or "Tool execution failed; continuing without this tool.")
                ) or "Tool execution failed; continuing without this tool."
                latest_tool_outcome[agent.id] = {
                    "blocked": True,
                    "failed": True,
                    "ok": False,
                    "message": reason,
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
            latest_tool_outcome[agent.id] = {
                "blocked": False,
                "failed": not ok,
                "ok": ok,
                "message": _sanitize_action_message(str(meta.get("error") or "").strip()),
            }
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

    max_hops = _estimate_max_hops(len(workers), user_input)
    def router_node(state: PeerState) -> PeerState:
        push(
            trace,
            event(
                "node_entered",
                "Enter First Owner Router",
                "Routing the request into the peer collaboration zone.",
                node_id=ROUTER_NODE,
            ),
        )
        routed_worker_id, route_reason = llm_gateway.route(state["user_input"], workers)
        first_worker = worker_by_id[routed_worker_id]
        push(
            trace,
            event(
                "route_selected",
                "First Owner Selected",
                f"Router selected {first_worker.name} as the initial owner.",
                node_id=ROUTER_NODE,
                next_node_id=first_worker.id,
                reason=route_reason,
                focus_task=state["user_input"],
            ),
        )
        push(
            trace,
            event(
                "node_exited",
                "Exit First Owner Router",
                "Initial owner routing completed.",
                node_id=ROUTER_NODE,
            ),
        )
        return {
            "current_owner_id": first_worker.id,
            "current_owner_name": first_worker.name,
            "last_worker_id": first_worker.id,
            "last_worker_name": first_worker.name,
            "route_reason": route_reason,
            "current_task_title": state.get("current_task_title") or state["user_input"],
        }

    def make_peer_exec_node(worker: AgentDefinition):
        def peer_exec_node(state: PeerState) -> PeerState:
            push(
                trace,
                event(
                    "node_entered",
                    "Enter Peer Agent",
                    f"{worker.name} is deciding the next collaboration step.",
                    node_id=worker.id,
                    hop_count=state.get("hop_count", 0),
                    task_title=state.get("current_task_title", user_input),
                ),
            )

            available_outputs = _available_outputs_block(list(state.get("reports", [])))
            workspace_context = _workspace_context_text(state)
            execution_input = _build_peer_execution_prompt(
                user_input=state["user_input"],
                current_task_title=str(state.get("current_task_title", state["user_input"])),
                peer_directory=_peer_directory(workers, worker.id),
                available_outputs=available_outputs,
                workspace_context=workspace_context,
            )
            execution_result = llm_gateway.run_agent(
                worker,
                execution_input,
                history=history,
                trace_hook=make_tool_trace_hook(worker),
                final_response_instruction=PEER_EXECUTION_FINAL_RESPONSE_INSTRUCTION,
            )
            execution_result = _sanitize_action_message(execution_result) or "No execution result was produced."

            push(
                trace,
                event(
                    "message_generated",
                    "Peer Execution",
                    f"{worker.name} executed the current task.",
                    node_id=worker.id,
                    preview=execution_result[:180],
                ),
            )

            decision_input = _build_peer_decision_prompt(
                user_input=state["user_input"],
                current_task_title=str(state.get("current_task_title", state["user_input"])),
                peer_directory=_peer_directory(workers, worker.id),
                available_outputs=available_outputs,
                workspace_context=workspace_context,
                execution_result=execution_result,
            )
            raw_response = llm_gateway.run_agent(
                worker,
                decision_input,
                history=[],
                trace_hook=None,
                final_response_instruction=PEER_HANDOFF_FINAL_RESPONSE_INSTRUCTION,
                response_contract="action_json",
            )
            action = _parse_agent_action(raw_response)
            invalid_reason = _validate_agent_action(action) if action is not None else "output was not a single valid JSON action object"
            if invalid_reason is not None:
                repaired_action = _repair_agent_action(
                    raw_response=raw_response,
                    worker=worker,
                    workers=workers,
                    user_input=state["user_input"],
                    current_task_title=str(state.get("current_task_title", state["user_input"])),
                    reports=[*list(state.get("reports", [])), f"{worker.name} execution on '{state.get('current_task_title', user_input)}':\n{execution_result}"],
                    invalid_reason=invalid_reason,
                )
                if repaired_action is not None:
                    action = repaired_action
                    push(
                        trace,
                        event(
                            "state_updated",
                            "Action Repaired",
                            f"{worker.name}'s output was repaired into a valid workflow action.",
                            node_id=worker.id,
                            repaired_action=action.get("action"),
                        ),
                    )
                else:
                    action = _fallback_action(raw_response)

            action_name = str(action.get("action") or "complete")
            action_message = _sanitize_action_message(str(action.get("message") or "").strip())
            if not action_message:
                action_message = _sanitize_action_message(str(raw_response or "").strip())
            if not action_message:
                action_message = "Agent returned an invalid workflow action payload."
            action["message"] = action_message

            tool_outcome = latest_tool_outcome.get(worker.id, {})
            if tool_outcome.get("failed") and action_name in {"complete", "respond_user"}:
                failure_reason = _sanitize_action_message(str(tool_outcome.get("message") or "").strip())
                action_name = "block"
                action["action"] = "block"
                action_message = (
                    f"Tool execution failed before completion. {failure_reason}".strip()
                    if failure_reason
                    else "Tool execution failed before completion."
                )
                action["message"] = action_message

            reports = list(state.get("reports", []))
            artifacts = latest_tool_artifacts.get(worker.id, {})
            confirmed_workspace = str(state.get("confirmed_workspace") or "").strip()
            new_output_dir = str(artifacts.get("output_dir") or "").strip()
            if new_output_dir:
                confirmed_workspace = new_output_dir
            confirmed_paths = [str(item).strip() for item in (state.get("confirmed_paths") or []) if str(item).strip()]
            for path in artifacts.get("generated_files") or []:
                path_text = str(path).strip()
                if path_text and path_text not in confirmed_paths:
                    confirmed_paths.append(path_text)
            reports.append(
                f"{worker.name} execution on '{state.get('current_task_title', user_input)}':\n{execution_result}"
            )
            reports.append(
                f"{worker.name} [{action_name}] on '{state.get('current_task_title', user_input)}':\n{action_message}"
            )

            push(
                trace,
                event(
                    "message_generated",
                    "Peer Action",
                    f"{worker.name} proposed {action_name}.",
                    node_id=worker.id,
                    action=action_name,
                    preview=action_message[:180],
                    target_agent_id=action.get("target_agent_id"),
                    task_title=action.get("task_title"),
                ),
            )
            push(
                trace,
                event(
                    "node_exited",
                    "Exit Peer Agent",
                    f"{worker.name} returned a structured workflow action.",
                    node_id=worker.id,
                ),
            )

            return {
                "reports": reports,
                "last_worker_id": worker.id,
                "last_worker_name": worker.name,
                "confirmed_workspace": confirmed_workspace,
                "confirmed_paths": confirmed_paths,
                "pending_action": action_name,
                "pending_target_agent_id": str(action.get("target_agent_id") or "").strip(),
                "pending_task_title": str(action.get("task_title") or "").strip(),
                "assistant_message": action_message if action_name == "respond_user" else state.get("assistant_message", ""),
            }

        return peer_exec_node

    def decision_node(state: PeerState) -> PeerState:
        action_name = str(state.get("pending_action") or "complete")
        action_message = ""
        reports = list(state.get("reports", []))
        if reports:
            _, _, last_message = str(reports[-1]).partition(":\n")
            action_message = last_message.strip()

        if action_name == "continue":
            current_worker_id = str(state.get("last_worker_id") or state.get("current_owner_id") or "")
            current_worker_name = str(state.get("last_worker_name") or state.get("current_owner_name") or "")
            rewritten_task = str(state.get("pending_task_title") or "").strip() or str(state.get("current_task_title", user_input))
            push(
                trace,
                event(
                    "route_selected",
                    "Continue Current Peer",
                    f"{current_worker_name} will continue the current task.",
                    node_id=current_worker_id,
                    next_node_id=current_worker_id,
                    reason=action_message[:180],
                    task_title=rewritten_task,
                    hop_count=state.get("hop_count", 0),
                ),
            )
            return {
                "current_owner_id": current_worker_id,
                "current_owner_name": current_worker_name,
                "current_task_title": rewritten_task,
            }

        if action_name in {"handoff", "review"}:
            target_agent_id = str(state.get("pending_target_agent_id") or "").strip()
            next_task_title = str(state.get("pending_task_title") or "").strip() or state.get("current_task_title", user_input)
            target_worker = worker_by_id.get(target_agent_id)
            current_worker_id = str(state.get("last_worker_id") or state.get("current_owner_id") or "")
            current_worker_name = str(state.get("last_worker_name") or state.get("current_owner_name") or "")
            if target_worker is None or target_worker.id == current_worker_id:
                rewritten_task = next_task_title or state.get("current_task_title", user_input)
                reports = list(state.get("reports", []))
                reports.append(
                    f"Runtime note for {current_worker_name}:\n"
                    f"The proposed handoff target was invalid. Continue the remaining work yourself under this task:\n{rewritten_task}"
                )
                push(
                    trace,
                    event(
                        "state_updated",
                        "Handoff Rewritten",
                        "Peer handoff target was invalid, so runtime kept the current agent and continued execution.",
                        node_id=current_worker_id,
                        target_agent_id=target_agent_id,
                        task_title=rewritten_task,
                    ),
                )
                return {
                    "reports": reports,
                    "current_owner_id": current_worker_id,
                    "current_owner_name": current_worker_name,
                    "current_task_title": rewritten_task,
                    "pending_action": "handoff",
                }

            next_hop = int(state.get("hop_count", 0)) + 1
            if next_hop >= max_hops:
                push(
                    trace,
                    event(
                        "state_updated",
                        "Hop Limit Reached",
                        f"Reached max peer handoff budget ({max_hops}).",
                        node_id=current_worker_id,
                        hop_count=next_hop,
                        max_hops=max_hops,
                    ),
                )
                return {"terminal_status": "max_hops", "pending_action": "max_hops"}

            push(
                trace,
                event(
                    "route_selected",
                    "Review Requested" if action_name == "review" else "Peer Handoff",
                    f"{current_worker_name} routed work to {target_worker.name}.",
                    node_id=current_worker_id,
                    next_node_id=target_worker.id,
                    reason=action_message[:180],
                    task_title=next_task_title,
                    hop_count=next_hop,
                ),
            )
            return {
                "current_owner_id": target_worker.id,
                "current_owner_name": target_worker.name,
                "current_task_title": next_task_title,
                "hop_count": next_hop,
            }

        if action_name == "respond_user":
            return {"terminal_status": "respond_user"}

        if action_name == "block":
            return {"terminal_status": "blocked"}

        current_worker_id = str(state.get("last_worker_id") or state.get("current_owner_id") or "")
        current_worker = worker_by_id.get(current_worker_id, workers[0])
        root_decision = _review_root_completion(
            user_input=state["user_input"],
            current_task_title=str(state.get("current_task_title", state["user_input"])),
            workspace_context=_workspace_context_text(state),
            last_worker=current_worker,
            workers=workers,
            reports=list(state.get("reports", [])),
            action_name=action_name,
            action_message=action_message,
        )
        if root_decision is not None and not bool(root_decision.get("root_complete")):
            next_task = str(root_decision.get("next_task") or "").strip() or str(state.get("current_task_title", state["user_input"]))
            target_agent_id = str(root_decision.get("target_agent_id") or "").strip()
            target_worker = worker_by_id.get(target_agent_id) or current_worker
            next_hop = int(state.get("hop_count", 0)) + 1
            if next_hop >= max_hops:
                push(
                    trace,
                    event(
                        "state_updated",
                        "Hop Limit Reached",
                        f"Reached max peer handoff budget ({max_hops}).",
                        node_id=current_worker.id,
                        hop_count=next_hop,
                        max_hops=max_hops,
                    ),
                )
                return {"terminal_status": "max_hops", "pending_action": "max_hops"}

            decision_reason = str(root_decision.get("reason") or "").strip() or "Root task is not complete yet."
            push(
                trace,
                event(
                    "state_updated",
                    "Root Task Incomplete",
                    "Current step completed, but the original user request still needs more work.",
                    node_id=current_worker.id,
                    reason=decision_reason,
                    next_task=next_task,
                    next_agent_id=target_worker.id,
                ),
            )
            push(
                trace,
                event(
                    "route_selected",
                    "Continue Peer Work",
                    f"{current_worker.name} completed the current step, so workflow continued toward the unfinished root task.",
                    node_id=current_worker.id,
                    next_node_id=target_worker.id,
                    reason=decision_reason[:180],
                    task_title=next_task,
                    hop_count=next_hop,
                ),
            )
            return {
                "current_owner_id": target_worker.id,
                "current_owner_name": target_worker.name,
                "current_task_title": next_task,
                "hop_count": next_hop,
                "pending_action": "handoff",
            }

        return {"terminal_status": "complete"}

    def router_next(state: PeerState) -> str:
        owner_id = str(state.get("current_owner_id") or "")
        return _peer_exec_node_id(owner_id) if owner_id else _peer_exec_node_id(workers[0].id)

    def decision_next(state: PeerState) -> str:
        action_name = str(state.get("pending_action") or "")
        if action_name in {"continue", "handoff", "review"} and not state.get("terminal_status"):
            owner_id = str(state.get("current_owner_id") or "")
            return _peer_exec_node_id(owner_id) if owner_id else _peer_exec_node_id(workers[0].id)
        if workflow.finalizer_enabled:
            return FINALIZER_NODE
        return END

    def finalizer_node(state: PeerState) -> PeerState:
        push(
            trace,
            event(
                "node_entered",
                "Enter Finalizer",
                "Finalizer is composing the visible answer from peer collaboration reports.",
                node_id=FINALIZER_NODE,
            ),
        )
        combined_report = "\n\n".join(list(state.get("reports", [])))
        assistant_message = str(state.get("assistant_message") or "").strip()
        finalizer_worker = worker_by_id.get(str(state.get("last_worker_id") or ""), workers[0])
        specialist_answer = combined_report
        if assistant_message:
            specialist_answer = f"{combined_report}\n\nDirect user-ready answer:\n{assistant_message}".strip()
        assistant_message = llm_gateway.finalize(
            user_input=user_input,
            agent=finalizer_worker,
            specialist_answer=specialist_answer or assistant_message or "No specialist output was produced.",
        )
        push(
            trace,
            event(
                "node_exited",
                "Exit Finalizer",
                "Finalizer completed.",
                node_id=FINALIZER_NODE,
            ),
        )
        return {"assistant_message": assistant_message}

    app = _compile_peer_handoff_app(
        workflow,
        workers,
        router_node=router_node,
        make_peer_exec_node=make_peer_exec_node,
        decision_node=decision_node,
        router_next=router_next,
        decision_next=decision_next,
        finalizer_node=finalizer_node if workflow.finalizer_enabled else None,
    )
    graph = build_peer_handoff_graph(workflow, workers)
    final_state = app.invoke(
        {
            "user_input": user_input,
            "current_task_title": user_input,
            "reports": [],
            "hop_count": 0,
            "max_hops": max_hops,
        }
    )

    combined_report = "\n\n".join(list(final_state.get("reports", [])))
    assistant_message = str(final_state.get("assistant_message") or "").strip()
    if not workflow.finalizer_enabled and not assistant_message:
        assistant_message = combined_report or "Workflow finished without a visible answer."

    push(
        trace,
        event(
            "run_finished",
            "Run Finished",
            "Workflow completed.",
            workflow_id=workflow.id,
            terminal_status=final_state.get("terminal_status", "complete"),
        ),
    )

    artifacts = RunArtifacts(
        route_agent_id=final_state.get("current_owner_id"),
        route_agent_name=final_state.get("current_owner_name"),
        route_reason=(
            f"First owner: {str(final_state.get('current_worker_name') or final_state.get('last_worker_name') or final_state.get('current_owner_name') or '')}. "
            f"Peer hops used: {final_state.get('hop_count', 0)}/{max_hops}. "
            f"Terminal status: {final_state.get('terminal_status', 'complete')}."
        ),
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
