from fastapi import APIRouter, HTTPException, Response

from cairn.server.db import get_conn
from cairn.server.models import PutWriteupRequest, Writeup
from cairn.server.services import get_project_or_404, utcnow

router = APIRouter(tags=["writeups"])


def _writeup_from_row(row) -> Writeup:
    return Writeup(
        project_id=row["project_id"],
        content=row["content"],
        worker=row["worker"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.get("/projects/{project_id}/writeup", response_model=Writeup)
def get_writeup(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        row = conn.execute(
            "SELECT * FROM writeups WHERE project_id = ?", (project_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Writeup not found")
        return _writeup_from_row(row)


@router.put("/projects/{project_id}/writeup", response_model=Writeup)
def put_writeup(project_id: str, body: PutWriteupRequest):
    with get_conn() as conn:
        project = get_project_or_404(conn, project_id)
        if project["status"] != "completed":
            raise HTTPException(409, f"Project is {project['status']}; writeups require a completed project")

        now = utcnow()
        conn.execute(
            """
            INSERT INTO writeups (project_id, content, worker, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(project_id) DO UPDATE SET
                content = excluded.content,
                worker = excluded.worker,
                updated_at = excluded.updated_at
            """,
            (project_id, body.content, body.worker, now, now),
        )
        row = conn.execute(
            "SELECT * FROM writeups WHERE project_id = ?", (project_id,)
        ).fetchone()
        return _writeup_from_row(row)


@router.delete("/projects/{project_id}/writeup", status_code=204)
def delete_writeup(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        cursor = conn.execute(
            "DELETE FROM writeups WHERE project_id = ?", (project_id,)
        )
        if cursor.rowcount == 0:
            raise HTTPException(404, "Writeup not found")
        return Response(status_code=204)
