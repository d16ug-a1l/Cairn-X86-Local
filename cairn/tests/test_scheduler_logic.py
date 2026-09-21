from __future__ import annotations

from concurrent.futures import Future

from cairn.dispatcher.models import ReasonCheckpoint, RunningTask
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.dispatcher.scheduler.worker_select import choose_worker
from cairn.server.models import Fact, ProjectSummary

from conftest import make_config, make_intent, make_project


def _loop() -> DispatcherLoop:
    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.reason_checkpoints = {}
    loop.runtime_project_ids = set()
    loop.cleanup_futures = {}
    loop._cleanup_pending = set()
    loop._inactive_cleanup_done = {}
    loop._writeup_done = set()
    loop._writeup_retry_after = {}
    loop.worker_rejected_until = {}
    loop._log_state = {}
    loop.project_cursor = 0
    return loop


def _summary(project_id: str, status: str) -> ProjectSummary:
    return ProjectSummary(
        id=project_id,
        title=project_id,
        status=status,
        bootstrap_enabled=True,
        created_at="2026-01-01T00:00:00Z",
        fact_count=2,
        intent_count=0,
        working_intent_count=0,
        unclaimed_intent_count=0,
        hint_count=0,
    )


def test_reason_trigger_detects_new_facts_and_open_intent_completion() -> None:
    loop = _loop()
    project = make_project(intents=[make_intent()])
    loop.reason_checkpoints["proj_001"] = ReasonCheckpoint(
        fact_count=3,
        hint_count=1,
        open_intent_count=1,
    )
    project.facts.append(Fact(id="f002", description="new"))
    project.intents = []

    assert loop._reason_trigger(project) == "facts:3->4,open_intents:1->0"


def test_reason_trigger_returns_none_when_graph_is_unchanged() -> None:
    loop = _loop()
    project = make_project(intents=[make_intent()])
    loop.reason_checkpoints["proj_001"] = ReasonCheckpoint(
        fact_count=3,
        hint_count=1,
        open_intent_count=1,
    )

    assert loop._reason_trigger(project) is None


def test_refresh_runtime_projects_discards_active_and_changed_cleanup_markers() -> None:
    loop = _loop()
    loop.runtime_project_ids = {"active", "stopped", "deleted"}
    loop._inactive_cleanup_done = {
        "active": "stopped",
        "stopped": "stopped",
        "changed": "completed",
        "deleted": "completed",
    }

    loop._refresh_runtime_projects(
        [
            _summary("active", "active"),
            _summary("stopped", "stopped"),
            _summary("changed", "stopped"),
        ]
    )

    assert loop.runtime_project_ids == {"active"}
    assert loop._inactive_cleanup_done == {"stopped": "stopped"}


def test_reap_cleanup_future_records_only_successful_inactive_cleanup() -> None:
    loop = _loop()
    succeeded: Future[bool] = Future()
    failed: Future[bool] = Future()
    succeeded.set_result(True)
    failed.set_result(False)
    loop.cleanup_futures = {
        succeeded: ("container-success", "proj-success", "completed"),
        failed: ("container-failed", "proj-failed", "stopped"),
    }
    loop._cleanup_pending = {"container-success", "container-failed"}
    loop._inactive_cleanup_done = {"proj-failed": "stopped"}

    loop._reap_cleanup_futures()

    assert loop.cleanup_futures == {}
    assert loop._cleanup_pending == set()
    assert loop._inactive_cleanup_done == {"proj-success": "completed"}


def test_choose_worker_prefers_priority_then_lower_running_count() -> None:
    workers = make_config().workers
    first = workers[0].model_copy(update={"name": "first", "priority": 0})
    busy = workers[0].model_copy(update={"name": "busy", "priority": 0})
    lower_priority = workers[0].model_copy(update={"name": "lower", "priority": 1})

    ordered = choose_worker(
        [lower_priority, busy, first],
        {"busy": 2, "first": 0, "lower": 0},
    )

    assert [worker.name for worker in ordered] == ["first", "busy", "lower"]


def test_new_fact_dispatches_reason_before_unclaimed_explore_intent() -> None:
    loop = _loop()
    loop.config = make_config()
    loop.futures = {}
    project = make_project(intents=[make_intent()])
    project.intents[0].worker = None
    project.facts.append(Fact(id="f002", description="new"))
    loop.reason_checkpoints["proj_001"] = ReasonCheckpoint(
        fact_count=3,
        hint_count=1,
        open_intent_count=1,
    )
    loop.backend = type("Containers", (), {"project_workspace": lambda _self, project_id: project_id})()
    loop.client = type(
        "Client",
        (),
        {
            "get_project": lambda _self, _project_id: project,
            "export_project": lambda _self, _project_id: "graph",
        },
    )()
    dispatched: list[tuple[str, str]] = []
    loop._dispatch_reason = lambda _project, _graph, trigger: dispatched.append(("reason", trigger)) or True
    loop._dispatch_explore = lambda *_args: dispatched.append(("explore", "")) or True

    assert loop._try_dispatch_project(_summary("proj_001", "active"))
    assert dispatched == [("reason", "facts:3->4")]


def test_initial_enabled_project_without_bootstrap_worker_dispatches_reason() -> None:
    loop = _loop()
    config = make_config()
    loop.config = config.model_copy(
        update={
            "workers": [
                config.workers[0].model_copy(update={"task_types": ["reason", "explore"]})
            ]
        }
    )
    loop.futures = {}
    project = make_project()
    project.facts = project.facts[:2]
    loop.backend = type("Containers", (), {"project_workspace": lambda _self, project_id: project_id})()
    loop.client = type(
        "Client",
        (),
        {
            "get_project": lambda _self, _project_id: project,
            "export_project": lambda _self, _project_id: "graph",
        },
    )()
    dispatched: list[tuple[str, str]] = []
    loop._dispatch_initial_project = lambda _project: dispatched.append(("bootstrap", "")) or True
    loop._dispatch_reason = lambda _project, _graph, trigger: dispatched.append(("reason", trigger)) or True

    assert loop._try_dispatch_project(_summary("proj_001", "active"))
    assert dispatched == [("reason", "initial")]


def test_initial_disabled_project_skips_configured_bootstrap_worker() -> None:
    loop = _loop()
    loop.config = make_config()
    loop.futures = {}
    project = make_project()
    project.project.bootstrap_enabled = False
    project.facts = project.facts[:2]
    loop.backend = type("Containers", (), {"project_workspace": lambda _self, project_id: project_id})()
    loop.client = type(
        "Client",
        (),
        {
            "get_project": lambda _self, _project_id: project,
            "export_project": lambda _self, _project_id: "graph",
        },
    )()
    dispatched: list[tuple[str, str]] = []
    loop._dispatch_initial_project = lambda _project: dispatched.append(("bootstrap", "")) or True
    loop._dispatch_reason = lambda _project, _graph, trigger: dispatched.append(("reason", trigger)) or True

    assert loop._try_dispatch_project(_summary("proj_001", "active"))
    assert dispatched == [("reason", "initial")]


def test_initial_enabled_project_without_bootstrap_worker_skips_bootstrap() -> None:
    loop = _loop()
    config = make_config()
    loop.config = config.model_copy(
        update={
            "workers": [
                config.workers[0].model_copy(update={"task_types": ["reason", "explore"]})
            ]
        }
    )
    project = make_project()
    project.project.bootstrap_enabled = True
    project.facts = project.facts[:2]

    assert not loop._project_requires_bootstrap(project)


def test_initial_enabled_project_keeps_existing_bootstrap_intent_when_workers_change() -> None:
    loop = _loop()
    config = make_config()
    loop.config = config.model_copy(
        update={
            "workers": [
                config.workers[0].model_copy(update={"task_types": ["reason", "explore"]})
            ]
        }
    )
    project = make_project(intents=[make_intent()])
    project.project.bootstrap_enabled = True
    project.facts = project.facts[:2]
    project.intents[0].description = "bootstrap"
    project.intents[0].creator = "dispatcher.bootstrap"
    project.intents[0].from_ = ["origin"]

    assert loop._project_requires_bootstrap(project)


def test_cancel_inactive_tasks_marks_stopped_and_deleted_projects() -> None:
    loop = _loop()
    stopped = TaskCancellation()
    deleted = TaskCancellation()
    loop.futures = {
        Future(): RunningTask("stopped", "explore", "worker", stopped),
        Future(): RunningTask("deleted", "reason", "worker", deleted),
    }

    loop._cancel_inactive_tasks([_summary("stopped", "stopped")])

    assert stopped.reason == "stopped"
    assert deleted.reason == "deleted"


def test_initialize_reason_checkpoint_only_for_active_projects_with_open_intents() -> None:
    loop = _loop()
    active = _summary("active", "active")
    active.unclaimed_intent_count = 1

    loop._initialize_reason_checkpoints(
        [
            active,
            _summary("idle", "active"),
            _summary("stopped", "stopped"),
        ]
    )

    assert loop.reason_checkpoints == {
        "active": ReasonCheckpoint(fact_count=2, hint_count=0, open_intent_count=1)
    }


def test_select_worker_reports_busy_rejected_and_unsupported_workers(monkeypatch) -> None:
    loop = _loop()
    base = make_config()
    busy = base.workers[0].model_copy(update={"name": "busy", "task_types": ["reason"]})
    rejected = base.workers[0].model_copy(update={"name": "rejected", "task_types": ["reason"]})
    unsupported = base.workers[0].model_copy(update={"name": "unsupported", "task_types": ["explore"]})
    loop.config = base.model_copy(update={"workers": [busy, rejected, unsupported]})
    loop.futures = {Future(): RunningTask("proj", "reason", "busy", TaskCancellation())}
    loop.worker_rejected_until = {("proj", "reason", "rejected"): 120.0}
    monkeypatch.setattr("cairn.dispatcher.scheduler.loop.time.time", lambda: 100.0)

    selection = loop._select_worker("proj", "reason")

    assert selection.worker is None
    assert selection.blocked_busy == ["busy(1/1)"]
    assert selection.blocked_rejected == ["rejected(20.0s)"]
    assert selection.blocked_task_type == ["unsupported"]


def _writeup_capable_loop() -> DispatcherLoop:
    loop = _loop()
    config = make_config()
    loop.config = config.model_copy(
        update={
            "workers": [
                config.workers[0].model_copy(update={"task_types": ["writeup"]})
            ]
        }
    )
    loop.futures = {}
    return loop


def test_cancel_inactive_tasks_keeps_writeup_running_on_completed_project() -> None:
    loop = _loop()
    writeup = TaskCancellation()
    explore = TaskCancellation()
    loop.futures = {
        Future(): RunningTask("proj_001", "writeup", "worker", writeup),
        Future(): RunningTask("proj_001", "explore", "worker", explore),
    }

    loop._cancel_inactive_tasks([_summary("proj_001", "completed")])

    assert writeup.reason is None
    assert explore.reason == "completed"


def test_cancel_inactive_tasks_cancels_writeup_when_project_reopens() -> None:
    loop = _loop()
    writeup = TaskCancellation()
    loop.futures = {
        Future(): RunningTask("proj_001", "writeup", "worker", writeup),
    }

    loop._cancel_inactive_tasks([_summary("proj_001", "active")])

    assert writeup.reason == "active"


def test_refresh_runtime_projects_drops_writeup_state_for_non_completed_projects() -> None:
    loop = _loop()
    loop._writeup_done = {"done", "reopened"}
    loop._writeup_retry_after = {"done": 100.0, "reopened": 100.0}

    loop._refresh_runtime_projects(
        [
            _summary("done", "completed"),
            _summary("reopened", "active"),
        ]
    )

    assert loop._writeup_done == {"done"}
    assert loop._writeup_retry_after == {"done": 100.0}


def test_dispatch_writeups_submits_task_and_marks_done_on_success(monkeypatch) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from cairn.dispatcher.protocol.client import ApiResult

    loop = _writeup_capable_loop()
    loop.executor = ThreadPoolExecutor(max_workers=1)
    project = make_project()
    project.project.status = "completed"
    loop.backend = type("Containers", (), {"project_workspace": lambda _self, project_id: project_id})()
    loop.client = type(
        "Client",
        (),
        {
            "get_writeup": lambda _self, _project_id: ApiResult(404, text="not found"),
            "get_project": lambda _self, _project_id: project,
        },
    )()
    monkeypatch.setattr("cairn.dispatcher.scheduler.loop.run_writeup_task", lambda *_a, **_k: "success")

    loop._dispatch_writeups([_summary("proj_001", "completed")])

    assert len(loop.futures) == 1
    task = next(iter(loop.futures.values()))
    assert task.task_type == "writeup"
    assert task.project_id == "proj_001"

    for future in list(loop.futures):
        future.result(timeout=5)
    loop._reap_futures()

    assert loop.futures == {}
    assert loop._writeup_done == {"proj_001"}
    loop.executor.shutdown(wait=True)


def test_dispatch_writeups_marks_existing_writeup_done_without_dispatch() -> None:
    from cairn.dispatcher.protocol.client import ApiResult

    loop = _writeup_capable_loop()
    loop.executor = None
    loop.backend = type("Containers", (), {"project_workspace": lambda _self, project_id: project_id})()
    loop.client = type(
        "Client",
        (),
        {"get_writeup": lambda _self, _project_id: ApiResult(200, {"content": "# w"})},
    )()

    loop._dispatch_writeups([_summary("proj_001", "completed")])

    assert loop._writeup_done == {"proj_001"}
    assert loop.futures == {}


def test_dispatch_writeups_skips_when_no_worker_supports_writeup() -> None:
    loop = _loop()
    loop.config = make_config()
    loop.futures = {}
    loop.client = type(
        "Client",
        (),
        {
            "get_writeup": lambda _self, _project_id: (_ for _ in ()).throw(
                AssertionError("get_writeup should not be called")
            )
        },
    )()

    loop._dispatch_writeups([_summary("proj_001", "completed")])

    assert loop.futures == {}
    assert loop._writeup_done == set()


def test_dispatch_writeups_failure_schedules_retry(monkeypatch) -> None:
    from concurrent.futures import Future as _Future

    loop = _writeup_capable_loop()
    future: _Future[str] = _Future()
    future.set_result("failed")
    cancellation = TaskCancellation()
    loop.futures = {future: RunningTask("proj_001", "writeup", "test-worker", cancellation)}
    monkeypatch.setattr("cairn.dispatcher.scheduler.loop.time.time", lambda: 100.0)

    loop._reap_futures()

    assert loop._writeup_done == set()
    assert loop._writeup_retry_after == {"proj_001": 130.0}


def test_cleanup_completed_projects_waits_for_pending_writeup() -> None:
    loop = _writeup_capable_loop()
    submitted: list[tuple] = []
    loop.cleanup_executor = type(
        "Exec", (), {"submit": lambda _self, fn, *args: submitted.append((fn, *args)) or Future()}
    )()
    loop.backend = type(
        "Containers",
        (),
        {
            "project_workspace": lambda _self, project_id: f"workspace-{project_id}",
            "needs_completed_cleanup": lambda _self, _project_id: True,
            "cleanup_completed": lambda _self, _project_id: True,
        },
    )()

    loop._cleanup_completed_projects([_summary("proj_001", "completed")])
    assert submitted == []

    loop._writeup_done.add("proj_001")
    loop._cleanup_completed_projects([_summary("proj_001", "completed")])
    assert len(submitted) == 1
    assert submitted[0][1] == "proj_001"


def test_cleanup_completed_projects_proceeds_when_no_worker_supports_writeup() -> None:
    loop = _loop()
    loop.config = make_config()
    loop.futures = {}
    submitted: list[tuple] = []
    loop.cleanup_executor = type(
        "Exec", (), {"submit": lambda _self, fn, *args: submitted.append((fn, *args)) or Future()}
    )()
    loop.backend = type(
        "Containers",
        (),
        {
            "project_workspace": lambda _self, project_id: f"workspace-{project_id}",
            "needs_completed_cleanup": lambda _self, _project_id: True,
            "cleanup_completed": lambda _self, _project_id: True,
        },
    )()

    loop._cleanup_completed_projects([_summary("proj_001", "completed")])

    assert len(submitted) == 1
