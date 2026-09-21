from __future__ import annotations

import json
import re

from fastapi.testclient import TestClient
import pytest

from cairn.server import db
from cairn.server.app import app


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    with TestClient(app) as test_client:
        yield test_client


def _create_project(client: TestClient) -> str:
    response = client.post(
        "/projects",
        json={
            "title": "test",
            "origin": "starting point",
            "goal": "finish",
            "hints": [{"content": "initial clue", "creator": "human"}],
        },
    )
    assert response.status_code == 201
    assert response.json()["project"]["bootstrap_enabled"] is True
    return response.json()["project"]["id"]


def test_project_workflow_create_conclude_complete_and_reopen(client: TestClient) -> None:
    project_id = _create_project(client)

    response = client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "investigate", "creator": "reasoner", "worker": None},
    )
    assert response.status_code == 201
    assert response.json()["id"] == "i001"

    response = client.post(
        f"/projects/{project_id}/intents/i001/heartbeat",
        json={"worker": "explorer"},
    )
    assert response.status_code == 200
    assert response.json()["worker"] == "explorer"

    response = client.post(
        f"/projects/{project_id}/intents/i001/conclude",
        json={"worker": "explorer", "description": "new fact"},
    )
    assert response.status_code == 200
    assert response.json()["fact"] == {"id": "f001", "description": "new fact"}

    response = client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["f001"], "description": "solved", "worker": "reasoner"},
    )
    assert response.status_code == 200
    assert response.json()["to"] == "goal"

    response = client.post(
        f"/projects/{project_id}/reopen",
        json={"description": "human correction", "creator": "human"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["project"]["status"] == "active"
    assert payload["fact"] == {"id": "f002", "description": "human correction"}
    assert payload["intent"]["from"] == ["f001"]
    assert payload["intent"]["to"] == "f002"


def test_stopping_project_releases_claims_and_reason_but_keeps_hints_writable(client: TestClient) -> None:
    project_id = _create_project(client)
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "work", "creator": "worker-a", "worker": "worker-a"},
    )
    client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-b", "trigger": "facts:2->3"},
    )

    response = client.put(f"/projects/{project_id}/status", json={"status": "stopped"})
    assert response.status_code == 200
    assert response.json()["reason"] is None

    detail = client.get(f"/projects/{project_id}").json()
    assert detail["intents"][0]["worker"] is None
    assert client.post(
        f"/projects/{project_id}/hints",
        json={"content": "manual note", "creator": "human"},
    ).status_code == 201
    assert client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "blocked", "creator": "reasoner", "worker": None},
    ).status_code == 403


def test_intent_creation_rejects_goal_source_and_mismatched_initial_worker(client: TestClient) -> None:
    project_id = _create_project(client)

    assert client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["goal"], "description": "invalid", "creator": "reasoner", "worker": None},
    ).status_code == 400
    assert client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "invalid", "creator": "reasoner", "worker": "explorer"},
    ).status_code == 400


def test_settings_and_export_are_backed_by_the_same_database(client: TestClient) -> None:
    project_id = _create_project(client)

    response = client.put("/settings", json={"intent_timeout": 30, "reason_timeout": 45})
    assert response.status_code == 200
    assert client.get("/settings").json() == {"intent_timeout": 30, "reason_timeout": 45}

    exported = client.get(f"/projects/{project_id}/export?format=yaml")
    assert exported.status_code == 200
    assert "origin: starting point" in exported.text
    assert "goal: finish" in exported.text
    assert client.get(f"/projects/{project_id}/export?format=invalid").status_code == 400


def test_report_export_renders_exploitation_path(client: TestClient) -> None:
    project_id = _create_project(client)

    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "port scan", "creator": "worker-a", "worker": "worker-a"},
    )
    client.post(
        f"/projects/{project_id}/intents/i001/conclude",
        json={"worker": "worker-a", "description": "found open port 80"},
    )
    # Off-path concluded exploration should land in the appendix, not the main path.
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["f001"], "description": "try ssh", "creator": "worker-a", "worker": "worker-a"},
    )
    client.post(
        f"/projects/{project_id}/intents/i002/conclude",
        json={"worker": "worker-a", "description": "ssh not available"},
    )
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["f001"], "description": "exploit web", "creator": "worker-a", "worker": "worker-a"},
    )
    client.post(
        f"/projects/{project_id}/intents/i003/conclude",
        json={"worker": "worker-a", "description": "got flag{abc}"},
    )
    client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["f003"], "description": "flag satisfies goal", "worker": "worker-a"},
    )

    response = client.get(f"/projects/{project_id}/export?format=report")
    assert response.status_code == 200
    assert "正确利用路径" in response.text
    assert "port scan" in response.text
    assert "exploit web" in response.text
    assert "got flag{abc}" in response.text
    assert "目标达成" in response.text
    # Main path must not contain the off-path step; it appears only in the appendix.
    main_path, appendix = response.text.split("附录：其他已结论探索", 1)
    assert "try ssh" not in main_path
    assert "try ssh" in appendix


def test_report_export_for_unfinished_project(client: TestClient) -> None:
    project_id = _create_project(client)
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "port scan", "creator": "worker-a", "worker": "worker-a"},
    )
    client.post(
        f"/projects/{project_id}/intents/i001/conclude",
        json={"worker": "worker-a", "description": "found open port 80"},
    )

    response = client.get(f"/projects/{project_id}/export?format=report")
    assert response.status_code == 200
    assert "当前探索进展" in response.text
    assert "目标尚未达成" in response.text
    assert "port scan" in response.text


def test_report_export_includes_transcript_commands(client: TestClient, tmp_path, monkeypatch) -> None:
    project_id = _create_project(client)
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "port scan", "creator": "worker-a", "worker": "worker-a"},
    )
    client.post(
        f"/projects/{project_id}/intents/i001/conclude",
        json={"worker": "worker-a", "description": "found open port 80"},
    )

    workspace = tmp_path / "ws" / project_id
    workspace.mkdir(parents=True)
    escaped = re.sub(r"[^A-Za-z0-9]", "-", str(workspace.resolve()))
    transcript_dir = tmp_path / "claude_projects" / escaped
    transcript_dir.mkdir(parents=True)
    entries = [
        {
            "type": "user",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": "user", "content": "# Task\n...\n## Current Intent\ni001\n..."},
        },
        {
            "type": "assistant",
            "timestamp": "2026-01-01T00:00:05Z",
            "message": {
                "content": [
                    {"type": "text", "text": "I will run a full port scan first."},
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "nmap -p- 10.0.0.1", "description": "Full port scan"},
                    },
                ]
            },
        },
    ]
    with open(transcript_dir / "session-1.jsonl", "w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")

    monkeypatch.setenv("CAIRN_REPORT_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("CAIRN_CLAUDE_PROJECTS_DIR", str(tmp_path / "claude_projects"))

    response = client.get(f"/projects/{project_id}/export?format=report")
    assert response.status_code == 200
    assert "操作说明" in response.text
    assert "I will run a full port scan first." in response.text
    assert "执行的命令" in response.text
    assert "nmap -p- 10.0.0.1" in response.text
    assert "Full port scan" in response.text


def test_llm_workers_listing_and_switch(client: TestClient, tmp_path, monkeypatch) -> None:
    config_file = tmp_path / "dispatch.yaml"
    config_file.write_text(
        '# comment line\n'
        'server: "http://127.0.0.1:8000"\n'
        '\n'
        'workers:\n'
        '  - name: "w-a"\n'
        '    type: "claudecode"\n'
        '    task_types: [bootstrap]\n'
        '    max_running: 1\n'
        '    priority: 0\n'
        '    env:\n'
        '      ANTHROPIC_MODEL: "model-a"\n'
        '      ANTHROPIC_BASE_URL: "https://api.example.com/anthropic"\n'
        '      ANTHROPIC_AUTH_TOKEN: "sk-secret"\n'
        '  - name: "w-b"\n'
        '    type: "claudecode"\n'
        '    task_types: [explore]\n'
        '    max_running: 1\n'
        '    priority: 1\n'
        '    env:\n'
        '      ANTHROPIC_MODEL: "model-b"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CAIRN_DISPATCH_CONFIG", str(config_file))

    response = client.get("/llm/workers")
    assert response.status_code == 200
    data = response.json()
    assert data["active_worker"] is None
    assert [w["name"] for w in data["workers"]] == ["w-a", "w-b"]
    assert data["workers"][0]["model"] == "model-a"
    assert data["workers"][0]["base_url"] == "https://api.example.com/anthropic"
    assert data["workers"][0]["has_api_key"] is True
    assert "sk-secret" not in response.text

    response = client.put("/llm/active", json={"name": "w-b"})
    assert response.status_code == 200
    assert response.json()["active_worker"] == "w-b"
    assert client.get("/llm/workers").json()["active_worker"] == "w-b"

    # 文本级更新：原文件注释与其余内容保持不变
    text = config_file.read_text(encoding="utf-8")
    assert 'active_worker: "w-b"' in text
    assert "# comment line" in text

    # 再次切换：替换已有行而不是追加
    client.put("/llm/active", json={"name": "w-a"})
    text = config_file.read_text(encoding="utf-8")
    assert text.count("active_worker") == 1
    assert 'active_worker: "w-a"' in text

    assert client.put("/llm/active", json={"name": "missing"}).status_code == 404


_LLM_CONFIG_TEMPLATE = (
    '# header comment\n'
    'server: "http://127.0.0.1:8000"\n'
    '\n'
    'active_worker: "w-a"\n'
    '\n'
    'workers:\n'
    '  - name: "w-a"\n'
    '    type: "claudecode"\n'
    '    task_types: [bootstrap, reason, explore]\n'
    '    max_running: 2\n'
    '    priority: 0\n'
    '    env:\n'
    '      ANTHROPIC_MODEL: "model-a"\n'
    '      ANTHROPIC_BASE_URL: "https://api.example.com/anthropic"\n'
    '      ANTHROPIC_AUTH_TOKEN: "sk-a"\n'
    '\n'
    '# trailing comment\n'
)


def _setup_llm_config(tmp_path, monkeypatch):
    config_file = tmp_path / "dispatch.yaml"
    config_file.write_text(_LLM_CONFIG_TEMPLATE, encoding="utf-8")
    monkeypatch.setenv("CAIRN_DISPATCH_CONFIG", str(config_file))
    return config_file


def test_llm_worker_create_update_delete(client: TestClient, tmp_path, monkeypatch) -> None:
    config_file = _setup_llm_config(tmp_path, monkeypatch)

    # 新增
    response = client.post("/llm/workers", json={
        "name": "w-b",
        "type": "codex",
        "model": "model-b",
        "base_url": "https://api.example.com/v1",
        "api_key": "sk-b",
        "task_types": ["explore"],
        "max_running": 1,
        "priority": 3,
    })
    assert response.status_code == 201
    assert response.json()["model"] == "model-b"
    assert client.post("/llm/workers", json={"name": "w-b", "type": "codex"}).status_code == 409

    text = config_file.read_text(encoding="utf-8")
    assert "# header comment" in text and "# trailing comment" in text
    assert 'CODEX_MODEL: "model-b"' in text
    assert 'OPENAI_API_KEY: "sk-b"' in text

    # 编辑：留空 api_key 保持原值（类型变了则不继承）
    response = client.put("/llm/workers/w-b", json={
        "name": "w-b",
        "type": "codex",
        "model": "model-b2",
        "task_types": ["reason", "explore"],
        "max_running": 3,
        "priority": 4,
    })
    assert response.status_code == 200
    text = config_file.read_text(encoding="utf-8")
    assert 'CODEX_MODEL: "model-b2"' in text
    assert 'OPENAI_API_KEY: "sk-b"' in text  # 未提供时保留
    assert "task_types: [reason, explore]" in text
    assert "max_running: 3" in text
    assert "# trailing comment" in text

    # 删除 active worker：active_worker 行一并清除
    assert client.delete("/llm/workers/w-a").status_code == 200
    text = config_file.read_text(encoding="utf-8")
    assert "w-a" not in text
    assert "active_worker" not in text
    assert 'CODEX_MODEL: "model-b2"' in text
    assert "# trailing comment" in text
    assert client.get("/llm/workers").json()["active_worker"] is None

    assert client.delete("/llm/workers/missing").status_code == 404


def test_expired_intent_and_reason_leases_can_be_reclaimed(client: TestClient) -> None:
    project_id = _create_project(client)
    client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "work", "creator": "worker-a", "worker": "worker-a"},
    )
    client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-a", "trigger": "bootstrap"},
    )
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE intents SET last_heartbeat_at = '2000-01-01T00:00:00Z' WHERE project_id = ?",
            (project_id,),
        )
        conn.execute(
            "UPDATE projects SET reason_last_heartbeat_at = '2000-01-01T00:00:00Z' WHERE id = ?",
            (project_id,),
        )

    response = client.post(
        f"/projects/{project_id}/intents/i001/heartbeat",
        json={"worker": "worker-b"},
    )
    assert response.status_code == 200
    assert response.json()["worker"] == "worker-b"

    response = client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-b", "trigger": "facts:2->3"},
    )
    assert response.status_code == 200
    assert response.json()["reason"]["worker"] == "worker-b"


def test_live_reason_lease_rejects_competing_worker(client: TestClient) -> None:
    project_id = _create_project(client)
    assert client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-a", "trigger": "bootstrap"},
    ).status_code == 200

    response = client.post(
        f"/projects/{project_id}/reason/claim",
        json={"worker": "worker-b", "trigger": "facts:2->3"},
    )

    assert response.status_code == 409
    assert "worker-a" in response.json()["detail"]


def test_project_creation_persists_disabled_bootstrap_and_exports_it(client: TestClient) -> None:
    response = client.post(
        "/projects",
        json={
            "title": "no bootstrap",
            "origin": "start",
            "goal": "finish",
            "bootstrap_enabled": False,
        },
    )

    assert response.status_code == 201
    project_id = response.json()["project"]["id"]
    assert client.get(f"/projects/{project_id}").json()["project"]["bootstrap_enabled"] is False
    assert "bootstrap_enabled: false" in client.get(f"/projects/{project_id}/export?format=yaml").text


def test_project_creation_rejects_invalid_bootstrap_enabled(client: TestClient) -> None:
    response = client.post(
        "/projects",
        json={
            "title": "invalid bootstrap",
            "origin": "start",
            "goal": "finish",
            "bootstrap_enabled": "sometimes",
        },
    )

    assert response.status_code == 422


def _complete_project(client: TestClient, project_id: str) -> None:
    response = client.post(
        f"/projects/{project_id}/intents",
        json={"from": ["origin"], "description": "investigate", "creator": "reasoner", "worker": None},
    )
    assert response.status_code == 201
    intent_id = response.json()["id"]
    assert client.post(
        f"/projects/{project_id}/intents/{intent_id}/heartbeat", json={"worker": "explorer"}
    ).status_code == 200
    assert client.post(
        f"/projects/{project_id}/intents/{intent_id}/conclude",
        json={"worker": "explorer", "description": "new fact"},
    ).status_code == 200
    assert client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["f001"], "description": "solved", "worker": "reasoner"},
    ).status_code == 200


def test_writeup_requires_completed_project_and_supports_full_lifecycle(client: TestClient) -> None:
    project_id = _create_project(client)

    assert client.get(f"/projects/{project_id}/writeup").status_code == 404

    response = client.put(
        f"/projects/{project_id}/writeup",
        json={"worker": "writer", "content": "# writeup"},
    )
    assert response.status_code == 409

    _complete_project(client, project_id)

    response = client.put(
        f"/projects/{project_id}/writeup",
        json={"worker": "writer", "content": "# writeup v1"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["project_id"] == project_id
    assert payload["worker"] == "writer"
    assert payload["content"] == "# writeup v1"
    assert payload["created_at"] == payload["updated_at"]

    response = client.put(
        f"/projects/{project_id}/writeup",
        json={"worker": "writer2", "content": "# writeup v2"},
    )
    assert response.status_code == 200
    assert response.json()["content"] == "# writeup v2"
    assert response.json()["worker"] == "writer2"

    response = client.get(f"/projects/{project_id}/writeup")
    assert response.status_code == 200
    assert response.json()["content"] == "# writeup v2"

    assert client.delete(f"/projects/{project_id}/writeup").status_code == 204
    assert client.get(f"/projects/{project_id}/writeup").status_code == 404
    assert client.delete(f"/projects/{project_id}/writeup").status_code == 404


def test_writeup_endpoints_validate_project_and_payload(client: TestClient) -> None:
    assert client.get("/projects/proj_999/writeup").status_code == 404
    assert client.put(
        "/projects/proj_999/writeup", json={"worker": "writer", "content": "x"}
    ).status_code == 404

    project_id = _create_project(client)
    response = client.put(
        f"/projects/{project_id}/writeup",
        json={"worker": " ", "content": "x"},
    )
    assert response.status_code == 422


def test_reopen_project_clears_stored_writeup(client: TestClient) -> None:
    project_id = _create_project(client)
    _complete_project(client, project_id)
    assert client.put(
        f"/projects/{project_id}/writeup",
        json={"worker": "writer", "content": "# writeup"},
    ).status_code == 200
    assert client.get(f"/projects/{project_id}/writeup").status_code == 200

    response = client.post(
        f"/projects/{project_id}/reopen",
        json={"description": "not actually solved", "creator": "human"},
    )
    assert response.status_code == 200

    assert client.get(f"/projects/{project_id}/writeup").status_code == 404
