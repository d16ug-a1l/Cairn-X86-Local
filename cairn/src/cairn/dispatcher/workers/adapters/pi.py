from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import Any

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.base import DriverResult, WorkerDriver


class PiDriver(WorkerDriver):
    type_name = "pi"

    def local_binary(self) -> str | None:
        return "pi"

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        return DriverResult(argv=self._local_argv(worker, prompt, session), session=session)

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        return self._local_argv(worker, prompt, session)

    def _local_argv(self, worker: WorkerConfig, prompt: str, session: str | None) -> list[str]:
        # Native pi: no models.json injection and no --provider/--model overrides, so pi uses
        # its own host configuration. A tiny sh wrapper just ensures the session dir exists.
        session_dir = self._session_dir(worker)
        pi_argv = [
            "--mode",
            "json",
            "--session-dir",
            session_dir,
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-context-files",
            "--tools",
            "read,write,edit,bash,grep,find,ls",
        ]
        if session:
            pi_argv.extend(["--session", session])
        pi_argv.extend(["-p", prompt])
        script = 'sdir="$1"\nshift\nmkdir -p "$sdir"\nexec pi "$@"\n'
        return ["/bin/sh", "-lc", script, "--", session_dir, *pi_argv]

    def extract_session(self, session: str | None, stdout: str, stderr: str) -> str | None:
        if session:
            return session
        for event in self._iter_events(stdout):
            if event.get("type") != "session":
                continue
            session_id = event.get("id")
            if isinstance(session_id, str) and session_id:
                return session_id
        return None

    def extract_response_text(self, stdout: str, stderr: str) -> str:
        assistant_message: dict[str, Any] | None = None
        for event in self._iter_events(stdout):
            event_type = event.get("type")
            if event_type == "turn_end":
                message = event.get("message")
                if isinstance(message, dict) and message.get("role") == "assistant":
                    assistant_message = message
            elif event_type == "agent_end":
                messages = event.get("messages")
                if isinstance(messages, list):
                    for message in reversed(messages):
                        if isinstance(message, dict) and message.get("role") == "assistant":
                            assistant_message = message
                            break
        if assistant_message is None:
            return stdout
        content = assistant_message.get("content")
        if not isinstance(content, list):
            return stdout
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "text":
                continue
            text = item.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        return "\n".join(parts).strip() or stdout

    @staticmethod
    def _agent_dir(worker: WorkerConfig) -> str:
        return str(PurePosixPath("/tmp/cairn-pi") / worker.name)

    @staticmethod
    def _session_dir(worker: WorkerConfig) -> str:
        return str(PurePosixPath(PiDriver._agent_dir(worker)) / "sessions")

    @staticmethod
    def _iter_events(stdout: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                events.append(payload)
        return events
