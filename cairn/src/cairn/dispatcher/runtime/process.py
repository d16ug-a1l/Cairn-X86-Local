from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Protocol, runtime_checkable

LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    cancelled: bool = False
    cancel_reason: str | None = None


@runtime_checkable
class ExecProcess(Protocol):
    """A worker process running on the dispatcher host (see LocalProcess).

    The task runners, heartbeat lease and cancellation only depend on this surface.
    """

    def start(self) -> None: ...

    def communicate(self, timeout: float | None) -> ProcessResult: ...

    def kill(self) -> None: ...

    def cancel(self, reason: str) -> None: ...
