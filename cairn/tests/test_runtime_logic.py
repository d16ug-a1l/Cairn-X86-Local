from __future__ import annotations

from dataclasses import dataclass, field

from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.heartbeat import HeartbeatLease


@dataclass
class FakeProcess:
    cancelled: list[str] = field(default_factory=list)
    kill_count: int = 0

    def cancel(self, reason: str) -> None:
        self.cancelled.append(reason)

    def kill(self) -> None:
        self.kill_count += 1


def test_task_cancellation_keeps_first_reason_and_cancels_late_process() -> None:
    cancellation = TaskCancellation()

    assert cancellation.cancel("project stopped")
    assert not cancellation.cancel("second reason")
    assert cancellation.reason == "project stopped"

    process = FakeProcess()
    cancellation.attach_process(process)
    assert process.cancelled == ["project stopped"]


def test_heartbeat_conflict_failure_kills_attached_process() -> None:
    process = FakeProcess()
    lease = HeartbeatLease(lambda: ApiResult(409, text="lost"), "intent", "worker", interval=60)
    lease.attach_process(process)

    lease._fail(409, "lost")

    assert lease.failure is not None
    assert lease.failure.status_code == 409
    assert process.kill_count == 1
