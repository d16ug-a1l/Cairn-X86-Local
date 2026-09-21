"""LLM worker 查看、切换与自定义配置接口。

Worker（LLM 配置）定义在 dispatcher 的 dispatch.yaml 里，server 本身不管理 worker。
本路由直接读写该配置文件：

- GET /llm/workers            列出已配置的 worker 与当前 active_worker
- PUT /llm/active             切换 active_worker
- POST /llm/workers           新增自定义 worker
- PUT /llm/workers/{name}     修改 worker（API key 留空则保持不变）
- DELETE /llm/workers/{name}  删除 worker（若是 active_worker 则一并清除该行）

所有写操作都是文本级编辑：只触碰目标行/块，保留文件其余内容、注释与格式。
dispatcher 每轮调度都会检查配置文件的修改时间并热加载，因此变更在数秒内生效，
只影响之后派发的新任务，进行中的任务仍由原 worker 执行完。

配置文件路径通过环境变量 CAIRN_DISPATCH_CONFIG 指定，默认 <cwd>/dispatch.yaml。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Literal

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

router = APIRouter(tags=["llm"])

_MODEL_ENV_KEYS = ("ANTHROPIC_MODEL", "CODEX_MODEL", "PI_MODEL")
_BASE_URL_ENV_KEYS = ("ANTHROPIC_BASE_URL", "CODEX_BASE_URL", "PI_BASE_URL")
_KEY_ENV_KEYS = ("ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "PI_API_KEY")

_TYPE_ENV_KEYS = {
    "claudecode": {"model": "ANTHROPIC_MODEL", "base_url": "ANTHROPIC_BASE_URL", "api_key": "ANTHROPIC_AUTH_TOKEN"},
    "codex": {"model": "CODEX_MODEL", "base_url": "CODEX_BASE_URL", "api_key": "OPENAI_API_KEY"},
    "pi": {"model": "PI_MODEL", "base_url": "PI_BASE_URL", "api_key": "PI_API_KEY"},
}

_ACTIVE_LINE_RE = re.compile(r"^active_worker\s*:")
_ACTIVE_COMMENT_RE = re.compile(r"^# 当前启用的 worker")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _config_path() -> Path:
    return Path(os.environ.get("CAIRN_DISPATCH_CONFIG", str(Path.cwd() / "dispatch.yaml")))


def _load_raw() -> dict:
    path = _config_path()
    if not path.is_file():
        raise HTTPException(404, f"dispatch config not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise HTTPException(500, f"dispatch config is not valid YAML: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("workers"), list):
        raise HTTPException(500, "dispatch config has no workers list")
    return data


def _first_env(env: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = env.get(key)
        if value:
            return str(value)
    return None


def _worker_summary(worker: dict) -> dict:
    env = worker.get("env") or {}
    return {
        "name": worker.get("name"),
        "type": worker.get("type"),
        "model": _first_env(env, _MODEL_ENV_KEYS),
        "base_url": _first_env(env, _BASE_URL_ENV_KEYS),
        "has_api_key": bool(_first_env(env, _KEY_ENV_KEYS)),
        "task_types": worker.get("task_types") or [],
        "max_running": worker.get("max_running"),
        "priority": worker.get("priority"),
    }


@router.get("/llm/workers")
def list_workers():
    data = _load_raw()
    return {
        "active_worker": data.get("active_worker"),
        "workers": [_worker_summary(w) for w in data["workers"] if isinstance(w, dict)],
    }


class ActiveWorkerRequest(BaseModel):
    name: str


@router.put("/llm/active")
def set_active_worker(body: ActiveWorkerRequest):
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "name must not be empty")

    data = _load_raw()
    names = {w.get("name") for w in data["workers"] if isinstance(w, dict)}
    if name not in names:
        raise HTTPException(404, f"worker not found: {name}")

    # 文本级更新：只改/加一行 active_worker，保留文件其余内容、注释与格式
    path = _config_path()
    lines = path.read_text(encoding="utf-8").splitlines()
    new_line = f'active_worker: "{name}"'
    for idx, line in enumerate(lines):
        if _ACTIVE_LINE_RE.match(line):
            lines[idx] = new_line
            break
    else:
        new_lines: list[str] = []
        inserted = False
        for line in lines:
            new_lines.append(line)
            if not inserted and re.match(r"^server\s*:", line):
                new_lines.append("")
                new_lines.append("# 当前启用的 worker（LLM）；dispatcher 热加载该字段，仅影响新派发的任务")
                new_lines.append(new_line)
                inserted = True
        if not inserted:
            new_lines.insert(0, new_line)
        lines = new_lines

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"active_worker": name}


# ---- worker 增/改/删（文本级编辑 workers 段） ----

TaskType = Literal["bootstrap", "reason", "explore"]
WorkerType = Literal["claudecode", "codex", "pi"]


class WorkerUpsertRequest(BaseModel):
    name: str
    type: WorkerType
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None  # 编辑时留空表示保持原值
    provider_api: str | None = None  # 仅 pi 类型使用（PI_PROVIDER_API）
    task_types: list[TaskType] = Field(default_factory=lambda: ["bootstrap", "reason", "explore"])
    max_running: int = Field(default=1, gt=0)
    priority: int = Field(default=5, ge=0)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not _NAME_RE.match(value):
            raise ValueError("name 只能包含字母、数字、- _ .，且以字母或数字开头")
        return value

    @field_validator("task_types")
    @classmethod
    def validate_task_types(cls, value: list) -> list:
        if not value:
            raise ValueError("task_types 至少选择一种任务类型")
        return list(dict.fromkeys(value))


def _find_workers_section(lines: list[str]):
    """定位 workers 列表段；返回 (起始行, 结束行, 列表项正则)，不存在则返回 None。"""
    start = next((i for i, line in enumerate(lines) if re.match(r"^workers\s*:", line)), None)
    if start is None:
        return None
    end = len(lines)
    item_re = None
    for j in range(start + 1, len(lines)):
        line = lines[j]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            end = j
            break
        if item_re is None and line.lstrip().startswith("- "):
            item_re = re.compile(rf"^\s{{{indent}}}- ")
    return start, end, item_re


def _worker_blocks(lines: list[str], start: int, end: int, item_re) -> list[dict]:
    """切分每个 worker 的行区间并解析内容；块尾部的空行/注释行属于块间间隙，不计入块。"""
    blocks = []
    if item_re is None:
        return blocks
    heads = [i for i in range(start + 1, end) if item_re.match(lines[i])]
    for k, bstart in enumerate(heads):
        bend = heads[k + 1] if k + 1 < len(heads) else end
        content_end = bend
        while content_end > bstart:
            stripped = lines[content_end - 1].strip()
            if stripped and not stripped.startswith("#"):
                break
            content_end -= 1
        indent = len(lines[bstart]) - len(lines[bstart].lstrip())
        text = "\n".join(line[indent:] for line in lines[bstart:content_end])
        data = yaml.safe_load(text)
        worker = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
        blocks.append({"start": bstart, "end": content_end, "worker": worker})
    return blocks


def _q(value) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _render_worker_block(worker: dict, indent: int = 2) -> list[str]:
    pad = " " * indent
    lines = [
        f"{pad}- name: {_q(worker['name'])}",
        f"{pad}  type: {_q(worker['type'])}",
        f"{pad}  task_types: [{', '.join(worker['task_types'])}]",
        f"{pad}  max_running: {worker['max_running']}",
        f"{pad}  priority: {worker['priority']}",
    ]
    env = worker.get("env") or {}
    if env:
        lines.append(f"{pad}  env:")
        for key, value in env.items():
            lines.append(f"{pad}    {key}: {_q(value)}")
    return lines


def _build_worker(body: WorkerUpsertRequest, existing: dict | None = None) -> dict:
    keys = _TYPE_ENV_KEYS[body.type]
    existing_env = (existing or {}).get("env") or {}
    # 类型未变时，留空的 api_key / provider_api 继承原值
    same_type = (existing or {}).get("type") == body.type

    env: dict[str, str] = {}
    if body.model:
        env[keys["model"]] = body.model
    if body.base_url:
        env[keys["base_url"]] = body.base_url
    if body.api_key:
        env[keys["api_key"]] = body.api_key
    elif same_type and existing_env.get(keys["api_key"]):
        env[keys["api_key"]] = existing_env[keys["api_key"]]
    if body.type == "pi":
        if body.provider_api:
            env["PI_PROVIDER_API"] = body.provider_api
        elif same_type and existing_env.get("PI_PROVIDER_API"):
            env["PI_PROVIDER_API"] = existing_env["PI_PROVIDER_API"]

    return {
        "name": body.name,
        "type": body.type,
        "task_types": body.task_types,
        "max_running": body.max_running,
        "priority": body.priority,
        "env": env,
    }


def _read_lines() -> tuple[Path, list[str]]:
    path = _config_path()
    if not path.is_file():
        raise HTTPException(404, f"dispatch config not found: {path}")
    return path, path.read_text(encoding="utf-8").splitlines()


def _write_lines(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _locate_worker(lines: list[str], name: str):
    section = _find_workers_section(lines)
    if section is None:
        raise HTTPException(500, "dispatch config has no workers section")
    start, end, item_re = section
    for block in _worker_blocks(lines, start, end, item_re):
        if block["worker"].get("name") == name:
            return start, end, item_re, block
    return start, end, item_re, None


@router.post("/llm/workers", status_code=201)
def create_worker(body: WorkerUpsertRequest):
    path, lines = _read_lines()
    start, end, item_re, found = _locate_worker(lines, body.name)
    if found is not None:
        raise HTTPException(409, f"worker already exists: {body.name}")

    worker = _build_worker(body)
    blocks = _worker_blocks(lines, start, end, item_re)
    indent = 2
    if item_re is not None:
        for j in range(start + 1, end):
            if item_re.match(lines[j]):
                indent = len(lines[j]) - len(lines[j].lstrip())
                break
    insert_at = blocks[-1]["end"] if blocks else start + 1
    new_lines = _render_worker_block(worker, indent)
    if insert_at > 0 and lines[insert_at - 1].strip():
        new_lines.insert(0, "")
    lines[insert_at:insert_at] = new_lines
    _write_lines(path, lines)
    return _worker_summary(worker)


@router.put("/llm/workers/{name}")
def update_worker(name: str, body: WorkerUpsertRequest):
    path, lines = _read_lines()
    _, _, _, found = _locate_worker(lines, name)
    if found is None:
        raise HTTPException(404, f"worker not found: {name}")
    if body.name != name:
        _, _, _, conflict = _locate_worker(lines, body.name)
        if conflict is not None:
            raise HTTPException(409, f"worker already exists: {body.name}")

    worker = _build_worker(body, existing=found["worker"])
    indent = len(lines[found["start"]]) - len(lines[found["start"]].lstrip())
    lines[found["start"]:found["end"]] = _render_worker_block(worker, indent)

    # worker 改名时同步 active_worker 引用
    if body.name != name:
        for idx, line in enumerate(lines):
            if _ACTIVE_LINE_RE.match(line):
                current = line.split(":", 1)[1].strip().strip('"').strip("'")
                if current == name:
                    lines[idx] = f'active_worker: {_q(body.name)}'
                break
    _write_lines(path, lines)
    return _worker_summary(worker)


@router.delete("/llm/workers/{name}")
def delete_worker(name: str):
    path, lines = _read_lines()
    _, _, _, found = _locate_worker(lines, name)
    if found is None:
        raise HTTPException(404, f"worker not found: {name}")
    del lines[found["start"]:found["end"]]

    # 删除的是当前 active_worker 时，一并清掉该行（含上方说明注释）
    for idx, line in enumerate(lines):
        if _ACTIVE_LINE_RE.match(line):
            current = line.split(":", 1)[1].strip().strip('"').strip("'")
            if current == name:
                if idx > 0 and _ACTIVE_COMMENT_RE.match(lines[idx - 1]):
                    del lines[idx - 1:idx + 1]
                else:
                    del lines[idx]
            break
    _write_lines(path, lines)
    return {"deleted": name}
