"""Extract per-task execution details (commands + operation notes) from agent CLI transcripts.

The dispatcher runs each task as a claude CLI session inside the project workspace on the
host. claude persists full session transcripts under
``~/.claude/projects/<escaped-workspace-path>/<session-id>.jsonl`` — every Bash command the
agent ran (with its own short description) and every assistant narration is recorded there.

This module maps those transcripts back to graph intents:

- explore sessions: the rendered prompt contains ``## Current Intent\n<intent_id>``
- bootstrap sessions: the rendered prompt references the Origin/Goal/Hints context bundle
- anything else (reason sessions, unrecognized prompts): returned as unmapped sessions

Everything degrades gracefully: when no transcript directory exists (or the workspace was
cleaned), the report simply omits execution details.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

TEXT_LIMIT = 300
COMMAND_LIMIT = 300

_INTENT_ID_RE = re.compile(r"##\s*Current Intent\s*\n+\s*`?(i\d{3,})`?")
_BOOTSTRAP_MARKER = "context bundle containing Origin, Goal, and Hints"


@dataclass
class CommandRecord:
    command: str
    description: str


@dataclass
class SessionRecord:
    session_id: str
    started_at: str
    texts: list[str] = field(default_factory=list)
    commands: list[CommandRecord] = field(default_factory=list)


def _clip(text: str, limit: int) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[:limit] + "…"


def _workspace_roots(workspace_root: Path | None = None) -> list[Path]:
    if workspace_root is not None:
        return [workspace_root]
    roots = []
    env_root = os.environ.get("CAIRN_REPORT_WORKSPACE_ROOT")
    if env_root:
        roots.append(Path(env_root))
    roots.append(Path.cwd() / "datas" / "local")
    return roots


def _claude_projects_dir() -> Path:
    return Path(os.environ.get("CAIRN_CLAUDE_PROJECTS_DIR", str(Path.home() / ".claude" / "projects")))


def _find_transcript_files(project_id: str, workspace_root: Path | None = None) -> list[Path]:
    for root in _workspace_roots(workspace_root):
        workspace = root / project_id
        if not workspace.exists():
            continue
        escaped = re.sub(r"[^A-Za-z0-9]", "-", str(workspace.resolve()))
        transcript_dir = _claude_projects_dir() / escaped
        if transcript_dir.is_dir():
            return sorted(transcript_dir.glob("*.jsonl"))
    return []


def _prompt_text(content) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        if parts:
            return "\n".join(parts)
    return None


def _parse_transcript(
    path: Path,
    text_limit: int = TEXT_LIMIT,
    command_limit: int = COMMAND_LIMIT,
) -> tuple[str | None, SessionRecord]:
    """Parse one transcript file; return (mapped intent marker, record).

    The marker is either an intent id (explore) or the literal "bootstrap".
    """
    record = SessionRecord(session_id=path.stem, started_at="")
    prompt: str | None = None

    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            entry_type = entry.get("type")
            ts = entry.get("timestamp") or ""
            if ts and not record.started_at:
                record.started_at = ts

            message = entry.get("message") or {}
            content = message.get("content")

            if entry_type == "user" and prompt is None:
                prompt = _prompt_text(content)
                continue

            if entry_type != "assistant" or not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text" and item.get("text", "").strip():
                    record.texts.append(_clip(item["text"], text_limit))
                elif item.get("type") == "tool_use" and item.get("name") == "Bash":
                    inputs = item.get("input") or {}
                    command = (inputs.get("command") or "").strip()
                    if command:
                        record.commands.append(
                            CommandRecord(
                                command=_clip(command, command_limit),
                                description=_clip(inputs.get("description") or "", text_limit),
                            )
                        )

    marker: str | None = None
    if prompt:
        match = _INTENT_ID_RE.search(prompt)
        if match:
            marker = match.group(1)
        elif _BOOTSTRAP_MARKER in prompt:
            marker = "bootstrap"
    return marker, record


def load_execution_details(
    project_id: str,
    intent_ids: set[str],
    bootstrap_intent_id: str | None,
    workspace_root: Path | None = None,
    text_limit: int = TEXT_LIMIT,
    command_limit: int = COMMAND_LIMIT,
) -> tuple[dict[str, list[SessionRecord]], list[SessionRecord]]:
    """Return (sessions by intent id, unmapped sessions) for a project."""
    by_intent: dict[str, list[SessionRecord]] = {}
    unmapped: list[SessionRecord] = []

    for path in _find_transcript_files(project_id, workspace_root):
        try:
            marker, record = _parse_transcript(path, text_limit, command_limit)
        except OSError:
            continue
        intent_id: str | None = None
        if marker == "bootstrap":
            intent_id = bootstrap_intent_id
        elif marker is not None and marker in intent_ids:
            intent_id = marker

        if intent_id is None:
            unmapped.append(record)
        else:
            by_intent.setdefault(intent_id, []).append(record)

    for sessions in by_intent.values():
        sessions.sort(key=lambda s: s.started_at)
    unmapped.sort(key=lambda s: s.started_at)
    return by_intent, unmapped
