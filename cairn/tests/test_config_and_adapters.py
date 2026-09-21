from __future__ import annotations

import pytest
from pydantic import ValidationError

from cairn.dispatcher.config import DispatchConfig, WorkerConfig, validate_prompt_resources

from conftest import make_config


def test_dispatch_config_merges_common_env_with_worker_override() -> None:
    payload = make_config().model_dump()
    payload["common_env"] = {"SHARED": "common", "OVERRIDE": "common"}
    payload["workers"][0]["env"] = {"OVERRIDE": "worker"}

    config = DispatchConfig.model_validate(payload)

    assert config.workers[0].env["SHARED"] == "common"
    assert config.workers[0].env["OVERRIDE"] == "worker"


def test_dispatch_config_rejects_duplicate_workers_and_excess_project_parallelism() -> None:
    payload = make_config().model_dump()
    payload["workers"].append(dict(payload["workers"][0]))
    with pytest.raises(ValidationError, match="worker names must be unique"):
        DispatchConfig.model_validate(payload)

    payload = make_config().model_dump()
    payload["runtime"]["max_project_workers"] = 3
    with pytest.raises(ValidationError, match="max_project_workers cannot exceed max_workers"):
        DispatchConfig.model_validate(payload)


def test_dispatch_config_active_worker_filtering() -> None:
    payload = make_config().model_dump()
    second = dict(payload["workers"][0])
    second["name"] = "worker-b"
    payload["workers"].append(second)

    config = DispatchConfig.model_validate(payload)
    assert config.active_worker is None
    assert {w.name for w in config.eligible_workers()} == {payload["workers"][0]["name"], "worker-b"}

    payload["active_worker"] = "worker-b"
    config = DispatchConfig.model_validate(payload)
    assert [w.name for w in config.eligible_workers()] == ["worker-b"]

    payload["active_worker"] = "missing"
    with pytest.raises(ValidationError, match="active_worker"):
        DispatchConfig.model_validate(payload)


def test_mock_worker_rejects_unknown_phase_configuration() -> None:
    with pytest.raises(ValidationError, match="unsupported mock env keys"):
        WorkerConfig.model_validate(
            {
                "name": "mock",
                "type": "mock",
                "task_types": ["explore"],
                "max_running": 1,
                "priority": 0,
                "env": {"MOCK_UNKNOWN": "{}"},
            }
        )


def test_bundled_prompt_groups_have_required_placeholders() -> None:
    validate_prompt_resources("default")
    validate_prompt_resources("mock")
