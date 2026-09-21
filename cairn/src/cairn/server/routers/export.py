from collections import deque

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from datetime import datetime
import yaml

from cairn.server.db import get_conn
from cairn.server.report_transcripts import load_execution_details
from cairn.server.services import expire_reason_leases, expire_workers, get_project_or_404

router = APIRouter(tags=["export"])


def format_export_timestamp(value: str | None) -> str | None:
    if not value:
        return value
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _load_project_data(conn, project_id: str):
    expire_workers(conn, project_id)
    expire_reason_leases(conn, project_id)
    proj = get_project_or_404(conn, project_id)

    facts = conn.execute(
        "SELECT id, description FROM facts WHERE project_id = ?", (project_id,)
    ).fetchall()
    hints = conn.execute(
        "SELECT content, creator, created_at FROM hints WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    intents = conn.execute(
        "SELECT * FROM intents WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()

    sources_by_intent = {}
    for i in intents:
        rows = conn.execute(
            "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ? ORDER BY rowid",
            (i["id"], project_id),
        ).fetchall()
        sources_by_intent[i["id"]] = [r["fact_id"] for r in rows]

    return proj, facts, hints, intents, sources_by_intent


def _export_yaml(conn, project_id: str) -> str:
    proj, facts, hints, intents, sources_by_intent = _load_project_data(conn, project_id)

    origin_desc = ""
    goal_desc = ""
    for f in facts:
        if f["id"] == "origin":
            origin_desc = f["description"]
        elif f["id"] == "goal":
            goal_desc = f["description"]

    data: dict = {
        "project": {
            "title": proj["title"],
            "origin": origin_desc,
            "goal": goal_desc,
            "bootstrap_enabled": bool(proj["bootstrap_enabled"]),
        }
    }

    if hints:
        data["hints"] = [
            {
                "content": h["content"],
                "creator": h["creator"],
                "created_at": format_export_timestamp(h["created_at"]),
            }
            for h in hints
        ]

    data["facts"] = [{"id": f["id"], "description": f["description"]} for f in facts]

    intent_list = []
    for i in intents:
        entry: dict = {
            "from": sources_by_intent.get(i["id"], []),
            "to": i["to_fact_id"],
            "description": i["description"],
            "creator": i["creator"],
            "worker": i["worker"],
            "created_at": format_export_timestamp(i["created_at"]),
            "concluded_at": format_export_timestamp(i["concluded_at"]),
        }
        intent_list.append(entry)

    if intent_list:
        data["intents"] = intent_list

    return yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _export_timeline(conn, project_id: str) -> str:
    proj, facts, hints, intents, sources_by_intent = _load_project_data(conn, project_id)

    facts_by_id = {f["id"]: f["description"] for f in facts}

    events: list[tuple[str, int, str]] = []  # (timestamp, order, text)
    order = 0

    origin_desc = facts_by_id.get("origin", "")
    goal_desc = facts_by_id.get("goal", "")
    ts = format_export_timestamp(proj["created_at"]) or ""
    block = f"[{ts}] PROJECT CREATED\n  origin: {origin_desc}\n  goal: {goal_desc}"
    events.append((proj["created_at"] or "", order, block))
    order += 1

    for h in hints:
        ts = format_export_timestamp(h["created_at"]) or ""
        block = f"[{ts}] HINT by {h['creator']}\n  {h['content']}"
        events.append((h["created_at"] or "", order, block))
        order += 1

    for i in intents:
        src = sources_by_intent.get(i["id"], [])
        from_str = ", ".join(src)

        ts = format_export_timestamp(i["created_at"]) or ""
        meta = f"  from: {from_str}"
        if i["worker"] and not i["concluded_at"]:
            meta += f"\n  worker: {i['worker']} (in progress)"
        block = f"[{ts}] INTENT DECLARED {i['id']} by {i['creator']}\n{meta}\n  {i['description']}"
        events.append((i["created_at"] or "", order, block))
        order += 1

        if not i["concluded_at"] or not i["to_fact_id"]:
            continue

        ts = format_export_timestamp(i["concluded_at"]) or ""
        actor = i["worker"] or i["creator"]

        if i["to_fact_id"] == "goal":
            block = f"[{ts}] PROJECT COMPLETED by {actor}\n  via: {i['id']} from {from_str}"
        else:
            fact_desc = facts_by_id.get(i["to_fact_id"], "")
            block = f"[{ts}] INTENT CONCLUDED {i['id']} by {actor}\n  from: {from_str}\n  produced: {i['to_fact_id']}\n  {fact_desc}"

        events.append((i["concluded_at"] or "", order, block))
        order += 1

    events.sort(key=lambda e: (e[0], e[1]))

    return "\n\n".join(e[2] for e in events) + "\n"


_STATUS_CN = {"active": "进行中", "stopped": "已停止", "completed": "已完成"}


def _append_execution_details(lines: list[str], sessions) -> None:
    """Render per-step execution details (operation notes + commands) from transcripts."""
    if not sessions:
        return
    lines.append("- 执行过程：")
    for session in sessions:
        if session.texts:
            lines.append("  - 操作说明：")
            for idx, text in enumerate(session.texts, 1):
                lines.append(f"    {idx}. {text}")
        if session.commands:
            lines.append("  - 执行的命令：")
            for idx, cmd in enumerate(session.commands, 1):
                suffix = f" — {cmd.description}" if cmd.description else ""
                lines.append(f"    {idx}. `{cmd.command}`{suffix}")


def _find_main_chain(intents, sources_by_intent):
    """BFS from origin over concluded intents; return (chain, completed).

    chain is an ordered list of intents forming the path origin -> ... -> goal
    (or, when goal is unreachable, the deepest reached fact). Each chain item
    is the intent row that produced the next node.
    """
    adjacency: dict[str, list] = {}
    for i in intents:
        if not i["to_fact_id"]:
            continue
        for src in sources_by_intent.get(i["id"], []):
            adjacency.setdefault(src, []).append((i["to_fact_id"], i))

    prev: dict[str, tuple[str, object]] = {}
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

    completed = "goal" in prev
    node = "goal" if completed else deepest
    chain = []
    while node != "origin":
        parent, intent = prev[node]
        chain.append(intent)
        node = parent
    chain.reverse()
    return chain, completed


def _export_report(conn, project_id: str) -> str:
    proj, facts, hints, intents, sources_by_intent = _load_project_data(conn, project_id)
    facts_by_id = {f["id"]: f["description"] for f in facts}

    chain, completed = _find_main_chain(intents, sources_by_intent)
    on_path_ids = {i["id"] for i in chain}

    bootstrap_intent_id = next(
        (
            i["id"]
            for i in intents
            if i["description"] == "bootstrap" and i["creator"] == "dispatcher.bootstrap"
        ),
        None,
    )
    exec_by_intent, exec_unmapped = load_execution_details(
        project_id,
        {i["id"] for i in intents},
        bootstrap_intent_id,
    )

    lines: list[str] = []
    lines.append(f"# 任务报告：{proj['title']}")
    lines.append("")
    lines.append("## 项目信息")
    lines.append("")
    lines.append(f"- 项目 ID：{project_id}")
    lines.append(f"- 状态：{_STATUS_CN.get(proj['status'], proj['status'])}")
    lines.append(f"- 创建时间：{format_export_timestamp(proj['created_at'])}")
    if completed:
        goal_edge = chain[-1]
        lines.append(f"- 完成时间：{format_export_timestamp(goal_edge['concluded_at'])}")
    lines.append(f"- 起点：{facts_by_id.get('origin', '')}")
    lines.append(f"- 终点：{facts_by_id.get('goal', '')}")

    if hints:
        lines.append("")
        lines.append("## 提示")
        lines.append("")
        for h in hints:
            ts = format_export_timestamp(h["created_at"])
            lines.append(f"- [{ts}] {h['creator']}：{h['content']}")

    lines.append("")
    if completed:
        lines.append("## 正确利用路径")
        lines.append("")
        lines.append(f"从起点到终点共 {len(chain)} 步：")
    else:
        lines.append("## 当前探索进展")
        lines.append("")
        if chain:
            lines.append("目标尚未达成。以下为当前已确认的最深探索链：")
        else:
            lines.append("目标尚未达成，且暂无已结论的探索。")

    for idx, intent in enumerate(chain, 1):
        to = intent["to_fact_id"]
        actor = intent["worker"] or intent["creator"]
        srcs = "、".join(f"`{s}`" for s in sources_by_intent.get(intent["id"], []))
        created = format_export_timestamp(intent["created_at"])
        concluded = format_export_timestamp(intent["concluded_at"])
        lines.append("")
        lines.append(f"### 步骤 {idx}：{intent['description']}")
        lines.append("")
        lines.append(f"- 执行者：{actor}")
        lines.append(f"- 依据：{srcs}")
        lines.append(f"- 时间：{created} → {concluded}")
        if to == "goal":
            lines.append("- 结论：**目标达成**")
        else:
            lines.append(f"- 产出事实 `{to}`：{facts_by_id.get(to, '')}")
        _append_execution_details(lines, exec_by_intent.get(intent["id"], []))

    off_path = [
        i for i in intents
        if i["to_fact_id"] and i["to_fact_id"] != "goal" and i["id"] not in on_path_ids
    ]
    if off_path:
        lines.append("")
        lines.append("## 附录：其他已结论探索（不在主路径上）")
        lines.append("")
        for i in off_path:
            lines.append(
                f"- `{i['id']}` {i['description']} → `{i['to_fact_id']}`："
                f"{facts_by_id.get(i['to_fact_id'], '')}"
            )

    unmapped_with_commands = [s for s in exec_unmapped if s.commands]
    if unmapped_with_commands:
        lines.append("")
        lines.append("## 附录：未关联到路径步骤的执行记录")
        lines.append("")
        for session in unmapped_with_commands:
            ts = format_export_timestamp(session.started_at) or "未知时间"
            lines.append(f"### 会话 `{session.session_id}`（{ts}）")
            lines.append("")
            for cmd in session.commands:
                lines.append(f"- `{cmd.command}`" + (f" — {cmd.description}" if cmd.description else ""))
            lines.append("")

    return "\n".join(lines) + "\n"


@router.get("/projects/{project_id}/export")
def export_project(project_id: str, format: str = "yaml"):
    if format not in ("yaml", "timeline", "report"):
        raise HTTPException(400, "Supported formats: yaml, timeline, report")

    with get_conn() as conn:
        if format == "timeline":
            text = _export_timeline(conn, project_id)
        elif format == "report":
            text = _export_report(conn, project_id)
        else:
            text = _export_yaml(conn, project_id)

        media_type = "text/markdown" if format == "report" else "text/plain"
        return Response(content=text, media_type=media_type)
