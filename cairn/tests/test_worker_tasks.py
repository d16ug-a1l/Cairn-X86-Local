from __future__ import annotations

from collections.abc import Iterator

from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.tasks import bootstrap, explore, reason, writeup
from cairn.server.models import Intent, ProjectDetail

from conftest import (
    FakeClient,
    FakeBackend,
    FakeDriver,
    FakeLease,
    make_config,
    make_intent,
    make_project,
)


def _lease_factory(lease: FakeLease):
    return lambda *_args, **_kwargs: lease


def test_reason_writes_graph_snapshot_and_creates_intent(monkeypatch) -> None:
    config = make_config()
    project = make_project()
    client = FakeClient(project)
    containers = FakeBackend()
    driver = FakeDriver()
    lease = FakeLease()
    graph_yaml = "project:\n  title: huge\n" + ("x" * 100_000)

    monkeypatch.setattr(reason, "get_driver", lambda *_a, **_k: driver)
    monkeypatch.setattr(reason.HeartbeatLease, "for_reason", _lease_factory(lease))
    monkeypatch.setattr(
        reason,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{"intents":[{"from":["f001"],"description":"next step"}]}}',
            "",
        ),
    )

    outcome = reason.run_reason_task(
        config,
        client,
        containers,
        project,
        graph_yaml,
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.created_intents == [("proj_001", ["f001"], "next step", "test-worker")]
    assert client.released_reasons == [("proj_001", "test-worker")]
    assert lease.started and lease.stopped
    assert len(containers.writes) == 1
    workspace, path, content = containers.writes[0]
    assert workspace == "workspace-proj_001"
    assert path.startswith("/tmp/cairn-prompts/reason_execute-")
    assert path.endswith("/graph.yaml")
    assert content == graph_yaml
    assert graph_yaml not in driver.execute_prompts[0]
    assert path in driver.execute_prompts[0]


def test_explore_early_plain_text_exit_uses_conclude_fallback(monkeypatch) -> None:
    config = make_config()
    intent = make_intent()
    project = make_project(intents=[intent])
    client = FakeClient(project)
    containers = FakeBackend()
    driver = FakeDriver()
    lease = FakeLease()
    results: Iterator[ProcessResult] = iter(
        [
            ProcessResult(0, "Need inspect files and keep working.", ""),
            ProcessResult(0, '{"accepted":true,"data":{"description":"confirmed fact"}}', ""),
        ]
    )

    monkeypatch.setattr(explore, "get_driver", lambda *_a, **_k: driver)
    monkeypatch.setattr(explore.HeartbeatLease, "for_intent", _lease_factory(lease))
    monkeypatch.setattr(explore, "_run_process", lambda *_args, **_kwargs: next(results))

    outcome = explore.run_explore_task(
        config,
        client,
        containers,
        project,
        "facts:\n- id: f001\n",
        intent,
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.concluded == [("proj_001", "i001", "test-worker", "confirmed fact")]
    assert len(containers.writes) == 2
    assert "/explore_execute-" in containers.writes[0][1]
    assert "/explore_conclude-" in containers.writes[1][1]
    assert len(driver.execute_prompts) == 1
    assert len(driver.conclude_prompts) == 1
    assert lease.started and lease.stopped


def test_bootstrap_success_concludes_fact_then_completes_project(monkeypatch) -> None:
    config = make_config()
    intent = make_intent()
    project = make_project(intents=[intent])
    client = FakeClient(project)
    containers = FakeBackend()
    driver = FakeDriver()
    lease = FakeLease()

    monkeypatch.setattr(bootstrap, "get_driver", lambda *_a, **_k: driver)
    monkeypatch.setattr(bootstrap.HeartbeatLease, "for_intent", _lease_factory(lease))
    monkeypatch.setattr(
        bootstrap,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{"fact":{"description":"solved"},'
            '"complete":{"description":"goal met"}}}',
            "",
        ),
    )

    outcome = bootstrap.run_bootstrap_task(
        config,
        client,
        containers,
        project,
        intent,
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.concluded == [("proj_001", "i001", "test-worker", "solved")]
    assert client.completed == [("proj_001", ["f002"], "goal met", "test-worker")]
    assert lease.started and lease.stopped


def test_reason_complete_treats_inactive_project_as_success(monkeypatch) -> None:
    config = make_config()
    project = make_project()
    client = FakeClient(project)
    containers = FakeBackend()
    lease = FakeLease()

    def complete(*_args, **_kwargs) -> ApiResult:
        return ApiResult(403, text="inactive")

    client.complete = complete  # type: ignore[method-assign]
    monkeypatch.setattr(reason, "get_driver", lambda *_a, **_k: FakeDriver())
    monkeypatch.setattr(reason.HeartbeatLease, "for_reason", _lease_factory(lease))
    monkeypatch.setattr(
        reason,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{"complete":{"from":["f001"],"description":"done"}}}',
            "",
        ),
    )

    outcome = reason.run_reason_task(
        config,
        client,
        containers,
        project,
        "graph",
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.released_reasons == [("proj_001", "test-worker")]


def _completed_project() -> ProjectDetail:
    project = make_project(
        intents=[
            Intent(
                id="i001",
                from_=["origin"],
                to="f001",
                description="scan target",
                creator="reasoner",
                worker="test-worker",
                created_at="2026-01-01T00:00:02Z",
                concluded_at="2026-01-01T00:01:00Z",
            ),
            Intent(
                id="i002",
                from_=["f001"],
                to="goal",
                description="solved",
                creator="test-worker",
                worker="test-worker",
                created_at="2026-01-01T00:02:00Z",
                concluded_at="2026-01-01T00:02:00Z",
            ),
        ]
    )
    project.project.status = "completed"
    return project


def test_writeup_find_main_chain_traces_origin_to_goal() -> None:
    chain = writeup.find_main_chain(_completed_project())

    assert [intent.id for intent in chain] == ["i001", "i002"]


def test_writeup_task_stores_generated_markdown(monkeypatch) -> None:
    config = make_config()
    project = _completed_project()
    client = FakeClient(project)
    containers = FakeBackend()
    driver = FakeDriver()

    monkeypatch.setattr(writeup, "get_driver", lambda *_a, **_k: driver)
    monkeypatch.setattr(
        writeup,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{"writeup":"# Writeup\\n\\nstep one"}}',
            "",
        ),
    )

    outcome = writeup.run_writeup_task(
        config,
        client,
        containers,
        project,
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.writeups == {"proj_001": ("test-worker", "# Writeup\n\nstep one")}
    prompt = driver.execute_prompts[0]
    assert "i001" in prompt and "i002" in prompt
    assert "known fact" in prompt
    assert "No execution records found" in prompt


def test_writeup_task_treats_409_write_as_cancelled(monkeypatch) -> None:
    config = make_config()
    project = _completed_project()
    client = FakeClient(project)
    containers = FakeBackend()

    monkeypatch.setattr(writeup, "get_driver", lambda *_a, **_k: FakeDriver())
    monkeypatch.setattr(
        writeup,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(
            0,
            '{"accepted":true,"data":{"writeup":"content"}}',
            "",
        ),
    )
    monkeypatch.setattr(client, "put_writeup", lambda *_a, **_k: ApiResult(409, text="active"))

    outcome = writeup.run_writeup_task(
        config,
        client,
        containers,
        project,
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "cancelled"
    assert client.writeups == {}


def test_writeup_task_parse_failure_returns_failed(monkeypatch) -> None:
    config = make_config()
    project = _completed_project()
    client = FakeClient(project)
    containers = FakeBackend()

    monkeypatch.setattr(writeup, "get_driver", lambda *_a, **_k: FakeDriver())
    monkeypatch.setattr(
        writeup,
        "run_worker_process",
        lambda *_args, **_kwargs: ProcessResult(0, "not json at all", ""),
    )

    outcome = writeup.run_writeup_task(
        config,
        client,
        containers,
        project,
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "failed"
    assert client.writeups == {}
