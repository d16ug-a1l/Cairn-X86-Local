from __future__ import annotations

import logging
import time
from collections import deque

from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.contracts import parse_json_output, validate_writeup_payload
from cairn.dispatcher.prompting import load_prompt, render_prompt
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.backend import ExecutionBackend
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.tasks.common import (
    cancel_reason,
    did_timeout,
    preview,
    run_worker_process,
)
from cairn.dispatcher.workers.registry import get_driver
from cairn.server.models import Intent, ProjectDetail
from cairn.server.report_transcripts import SessionRecord, load_execution_details

LOG = logging.getLogger(__name__)

BOOTSTRAP_INTENT_DESCRIPTION = "bootstrap"
BOOTSTRAP_INTENT_CREATOR = "dispatcher.bootstrap"
WRITEUP_TEXT_LIMIT = 1000
WRITEUP_COMMAND_LIMIT = 2000


def find_main_chain(project: ProjectDetail) -> list[Intent]:
    """BFS from origin over concluded intents; return the chain origin -> ... -> goal.

    Falls back to the deepest reached chain when goal is unreachable.
    """
    adjacency: dict[str, list[tuple[str, Intent]]] = {}
    for intent in project.intents:
        if intent.to is None:
            continue
        for src in intent.from_:
            adjacency.setdefault(src, []).append((intent.to, intent))

    prev: dict[str, tuple[str, Intent]] = {}
    depth = {"origin": 0}
    deepest = "origin"
    queue = deque(["origin"])
    while queue:
        cur = queue.popleft()
        for nxt, intent in adjacency.get(cur, []):
            if nxt in prev or nxt == "origin":
                continue
            prev[nxt] = (cur, intent)
            depth[nxt] = depth[cur] + 1
            if depth[nxt] > depth[deepest]:
                deepest = nxt
            queue.append(nxt)

    node = "goal" if "goal" in prev else deepest
    chain: list[Intent] = []
    while node != "origin":
        parent, intent = prev[node]
        chain.append(intent)
        node = parent
    chain.reverse()
    return chain


def _render_main_chain(project: ProjectDetail, chain: list[Intent]) -> str:
    facts_by_id = {fact.id: fact.description for fact in project.facts}
    lines: list[str] = []
    for idx, intent in enumerate(chain, 1):
        actor = intent.worker or intent.creator
        srcs = ", ".join(f"`{s}`" for s in intent.from_)
        lines.append(f"### Step {idx}: `{intent.id}` {intent.description}")
        lines.append(f"- Executor: {actor}")
        lines.append(f"- Based on facts: {srcs}")
        lines.append(f"- Time: {intent.created_at} -> {intent.concluded_at}")
        if intent.to == "goal":
            lines.append("- Outcome: **goal achieved**")
        else:
            lines.append(f"- Produced fact `{intent.to}`: {facts_by_id.get(intent.to or '', '')}")
        lines.append("")
    return "\n".join(lines).strip()


def _render_session_details(sessions: list[SessionRecord]) -> list[str]:
    lines: list[str] = []
    for session in sessions:
        if session.texts:
            lines.append("- Operation notes:")
            for note_idx, text in enumerate(session.texts, 1):
                lines.append(f"  {note_idx}. {text}")
        if session.commands:
            lines.append("- Commands executed:")
            for cmd_idx, cmd in enumerate(session.commands, 1):
                suffix = f" — {cmd.description}" if cmd.description else ""
                lines.append(f"  {cmd_idx}. `{cmd.command}`{suffix}")
    return lines


def _collect_execution_details(
    config: DispatchConfig,
    backend: ExecutionBackend,
    project: ProjectDetail,
    chain: list[Intent],
) -> str:
    workspace_root = getattr(backend, "workspace_root", None)
    bootstrap_intent_id = next(
        (
            intent.id
            for intent in project.intents
            if intent.description == BOOTSTRAP_INTENT_DESCRIPTION
            and intent.creator == BOOTSTRAP_INTENT_CREATOR
        ),
        None,
    )
    try:
        by_intent, _unmapped = load_execution_details(
            project.project.id,
            {intent.id for intent in project.intents},
            bootstrap_intent_id,
            workspace_root=workspace_root,
            text_limit=WRITEUP_TEXT_LIMIT,
            command_limit=WRITEUP_COMMAND_LIMIT,
        )
    except Exception:
        LOG.warning("writeup execution details collection failed project=%s", project.project.id, exc_info=True)
        return "(Execution record collection failed; reconstruct the steps from the fact chain only.)"

    lines: list[str] = []
    for idx, intent in enumerate(chain, 1):
        sessions = by_intent.get(intent.id, [])
        if not sessions:
            continue
        lines.append(f"### Step {idx} (`{intent.id}`) execution records")
        lines.extend(_render_session_details(sessions))
        lines.append("")
    if not lines:
        return "(No execution records found for the successful path; reconstruct the steps from the fact chain only.)"
    return "\n".join(lines).strip()


def run_writeup_task(
    config: DispatchConfig,
    client: CairnClient,
    backend: ExecutionBackend,
    project: ProjectDetail,
    worker: WorkerConfig,
    cancellation: TaskCancellation,
) -> str:
    driver = get_driver(worker.type)
    task_started = time.perf_counter()
    project_id = project.project.id
    try:
        workspace = backend.ensure_running(project_id)

        facts_by_id = {fact.id: fact.description for fact in project.facts}
        chain = find_main_chain(project)
        prompt = render_prompt(
            load_prompt(config.runtime.prompt_group, "writeup.md"),
            {
                "project_title": project.project.title,
                "origin": facts_by_id.get("origin", ""),
                "goal": facts_by_id.get("goal", ""),
                "main_chain": _render_main_chain(project, chain),
                "execution_details": _collect_execution_details(config, backend, project, chain),
            },
        )

        session = driver.prepare_session()
        execute = driver.build_execute(worker, prompt, session)
        session = execute.session
        execute_started = time.perf_counter()
        result = run_worker_process(
            backend,
            workspace,
            worker,
            execute.argv,
            phase="writeup",
            timeout_seconds=config.tasks.writeup.timeout,
            lease=None,
            cancellation=cancellation,
        )
        execute_ms = int((time.perf_counter() - execute_started) * 1000)
        total_ms = int((time.perf_counter() - task_started) * 1000)

        cancelled = cancel_reason(result, cancellation)
        if cancelled is not None:
            LOG.info(
                "writeup cancelled project=%s worker=%s reason=%s execute_ms=%s",
                project_id,
                worker.name,
                cancelled,
                execute_ms,
            )
            return "cancelled"
        if did_timeout(result):
            LOG.warning(
                "writeup timed out project=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project_id,
                worker.name,
                execute_ms,
                total_ms,
                preview(result.stdout),
                preview(result.stderr),
            )
            return "failed"
        if result.returncode != 0:
            LOG.warning(
                "writeup command failed project=%s worker=%s code=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project_id,
                worker.name,
                result.returncode,
                execute_ms,
                total_ms,
                preview(result.stdout),
                preview(result.stderr),
            )
            return "failed"
        try:
            model_output = driver.extract_response_text(result.stdout, result.stderr)
            payload = parse_json_output(model_output)
            kind, content = validate_writeup_payload(payload)
        except Exception as exc:
            LOG.warning(
                "writeup parse failed project=%s worker=%s error=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project_id,
                worker.name,
                exc,
                execute_ms,
                total_ms,
                preview(result.stdout),
                preview(result.stderr),
            )
            return "failed"
        if kind == "rejected":
            LOG.warning(
                "writeup rejected project=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s",
                project_id,
                worker.name,
                execute_ms,
                total_ms,
                preview(result.stdout),
            )
            return "rejected"

        assert content is not None
        response = client.put_writeup(project_id, worker.name, content)
        if response.status_code == 409:
            LOG.info(
                "writeup discarded because project is no longer completed project=%s worker=%s",
                project_id,
                worker.name,
            )
            return "cancelled"
        if not response.ok:
            LOG.warning(
                "writeup write failed project=%s worker=%s status=%s body=%s",
                project_id,
                worker.name,
                response.status_code,
                response.text,
            )
            return "failed"
        LOG.info(
            "writeup stored project=%s worker=%s execute_ms=%s total_ms=%s",
            project_id,
            worker.name,
            execute_ms,
            total_ms,
        )
        return "success"
    except Exception:
        LOG.exception("writeup task crashed project=%s worker=%s", project_id, worker.name)
        return "failed"
